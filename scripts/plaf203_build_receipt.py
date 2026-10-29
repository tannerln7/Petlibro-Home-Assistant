#!/usr/bin/env python3
"""Verify pinned Buildroot inputs and describe a built userspace, not a release.

No key/seed contents, environment dumps, absolute cache paths or wall-clock
timestamps enter the receipt. Package versions/downloads come from show-info;
their expected SHA-256 values come from the verified Buildroot source tree.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_tree(archive, tree):
    """Detect changed/missing upstream files without erasing a reused cache."""
    known = set()
    with tarfile.open(archive) as source:
        for member in source:
            if Path(member.name).is_absolute():
                raise ValueError("Unsafe archive member")
            relative = Path(*Path(member.name).parts[1:])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe archive member")
            target = tree / relative
            if member.isfile() or member.islnk():
                known.add(relative)
                if target.is_symlink() or not target.is_file():
                    raise ValueError(f"Missing/nonregular Buildroot input: {relative}")
                with source.extractfile(member) as stream:
                    expected = hashlib.file_digest(stream, "sha256").hexdigest()
                if sha256(target) != expected:
                    raise ValueError(f"Modified Buildroot input: {relative}")
                if target.stat().st_mode & 0o111 != member.mode & 0o111:
                    raise ValueError(f"Modified Buildroot executable mode: {relative}")
            elif member.issym():
                known.add(relative)
                if not target.is_symlink() or os.readlink(target) != member.linkname:
                    raise ValueError(f"Modified Buildroot link: {relative}")
    for target in tree.rglob("*"):
        relative = target.relative_to(tree)
        if relative.parts[0] != "dl" and (target.is_file() or target.is_symlink()) and relative not in known:
            raise ValueError(f"Unexpected Buildroot input: {relative}")


def downloads(buildroot, info, hash_roots=(), local_sources=None):
    expected = {}
    hash_files = list(buildroot.rglob("*.hash"))
    for root in hash_roots:
        hash_files.extend(root.rglob("*.hash"))
    for path in sorted(hash_files):
        # dl/ is untrusted cache content, not the source of expected hashes.
        if path.is_relative_to(buildroot / "dl"):
            continue
        for line in path.read_text().splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[0] == "sha256":
                expected.setdefault(fields[2], set()).add(fields[1])
    result = {}
    for name, package in info.items():
        for item in package.get("downloads", []):
            if name in (local_sources or {}):
                if item.get("uris", [None])[0] != "local+" + str(local_sources[name]):
                    raise ValueError(f"Unexpected local source URI: {name}")
                # Buildroot local packages are rsynced, not downloaded archives.
                # Their actual build-tree sources are verified in receipt().
                continue
            relative = Path(package["dl_dir"]) / item["source"]
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe package download path")
            actual = sha256(buildroot / "dl" / relative)
            if actual not in expected.get(item["source"], set()):
                raise ValueError(f"Unverified package download: {relative}")
            result[relative.as_posix()] = actual
    return result


def receipt(repo, buildroot, output, info, image_id, epoch):
    profile = repo / "firmware/plaf203"
    inputs = [profile / "sources.lock.json", profile / "post-build.sh", profile / "S50sshd"]
    if (profile / "post-image.sh").exists():
        inputs.append(profile / "post-image.sh")
    for directory in ("configs", "overlay", "board", "patches", "runtime", "package"):
        inputs.extend(path for path in (profile / directory).rglob("*") if path.is_file())
    inputs.extend(profile / name for name in ("Config.in", "external.mk", "external.desc"))
    inputs.extend(repo / "scripts" / name for name in ("build_plaf203_qemu.sh", "plaf203_build_receipt.py"))
    compiler = subprocess.check_output(
        ["/opt/plaf203-toolchain/bin/arm-openipc-linux-musleabi-gcc", "--version"], text=True
    ).splitlines()[0]
    result = {
        "schema": 1,
        "scope": "source-built development userspace and kernel/DTB; OEM bootloader remains a separate input",
        "source_date_epoch": epoch,
        "container_image_id": image_id,
        "sources": json.loads((profile / "sources.lock.json").read_text()),
        "compiler": compiler,
        "profile_sha256": {p.relative_to(repo).as_posix(): sha256(p) for p in sorted(inputs)},
        "configuration_sha256": sha256(output / ".config"),
        "busybox_configuration_sha256": sha256(next((output / "build").glob("busybox-*/.config"))),
        "package_sources_sha256": downloads(buildroot, info, [profile / "patches"],
                                             {"plaf203-runtime": profile / "runtime"}),
        "packages": {name: {k: p[k] for k in ("version", "licenses", "type") if k in p}
                     for name, p in sorted(info.items())},
        "development_enrollment": (output / "target/etc/plaf203/enrollment.pub").exists(),
        "test_entropy_seed": (output / "target/etc/plaf203/test.seed").exists(),
        "artifacts": {"rootfs.squashfs": sha256(output / "images/rootfs.squashfs")},
    }
    if "plaf203-runtime" in info:
        source = profile / "runtime"
        tree = output / info["plaf203-runtime"]["source_dir"]
        local = {}
        expected = {p.name for p in source.iterdir() if p.is_file()}
        for p in source.iterdir():
            if p.is_file():
                if (tree / p.name).is_symlink() or sha256(tree / p.name) != sha256(p):
                    raise ValueError(f"Stale local runtime build source: {p.name}")
                local[p.name] = sha256(p)
        metadata = {".files-list.txt", ".files-list-host.txt", ".files-list-images.txt", ".files-list-staging.txt"}
        for p in tree.iterdir():
            if p.name not in expected | metadata | {"plaf203-runtime", "plaf203-runtime-selftest", "plaf203-feeding-selftest", "plaf203-ctl"} and not p.name.startswith(".stamp_"):
                raise ValueError(f"Unexpected local runtime build input: {p.name}")
        result["runtime"] = {"source_sha256": local, "backend": "synthetic, non-actuating"}
        for name in ("usr/sbin/plaf203-runtime", "usr/libexec/plaf203-runtime-selftest",
                     "usr/libexec/plaf203-feeding-selftest", "usr/bin/plaf203-ctl"):
            result["runtime"][name] = sha256(output / "target" / name)
    if "linux" in info:
        tree = output / info["linux"]["source_dir"]
        result["kernel"] = {
            "release": (tree / "include/config/kernel.release").read_text().strip(),
            "configuration_sha256": sha256(tree / ".config"),
            "vmlinux_sha256": sha256(tree / "vmlinux"),
            "modules_sha256": {p.relative_to(output / "target").as_posix(): sha256(p)
                               for p in sorted((output / "target/lib/modules").rglob("*.ko"))},
        }
        for name in ("zImage", "plaf203-qemu.dtb", "zImage+dtb", "uImage", "kernel.config", "Module.symvers", "vmlinux"):
            result["artifacts"][name] = sha256(output / "images" / name)
        result["kernel"]["host_image_tool"] = subprocess.check_output(["mkimage", "-V"], text=True).strip()
        result["kernel"]["kernel_dtc"] = subprocess.check_output(
            [str(tree / "scripts/dtc/dtc"), "--version"], text=True).strip()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify-tree")
    verify.add_argument("archive", type=Path)
    verify.add_argument("tree", type=Path)
    record = commands.add_parser("record")
    record.add_argument("--repo", type=Path, required=True)
    record.add_argument("--buildroot", type=Path, required=True)
    record.add_argument("--output", type=Path, required=True)
    record.add_argument("--image-id", required=True)
    record.add_argument("--epoch", type=int, required=True)
    args = parser.parse_args()
    if args.command == "verify-tree":
        verify_tree(args.archive, args.tree)
    else:
        info = json.loads((args.output / "packages.json").read_text())
        result = receipt(args.repo, args.buildroot, args.output, info, args.image_id, args.epoch)
        target = args.output / "images/build-receipt.json"
        temporary = target.with_suffix(".json.new")
        temporary.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
        temporary.replace(target)


if __name__ == "__main__":
    main()
