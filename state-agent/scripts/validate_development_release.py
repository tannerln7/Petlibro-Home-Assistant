#!/usr/bin/env python3
"""Validate CI State Agent artifacts and publish the opt-in development feed."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib import error, request


REPOSITORY = "tannerln7/Petlibro-Home-Assistant"
RELEASE_BRANCH = "state-agent-releases"
PLATFORM = "linux-armv7-eabihf"
METADATA_SCHEMA_VERSION = 1
DEVELOPMENT_ROOT = Path("state-agent/dev")
STABLE_FILES = (Path("state-agent/latest.json"), Path("state-agent/latest.json.sig"))
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


class ReleaseError(RuntimeError):
    """A development release invariant was not satisfied."""


class _RejectRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ReleaseError("published release URL redirected")


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=capture,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ReleaseError(
            f"command failed ({' '.join(command)})" + (f": {detail}" if detail else "")
        )
    return result


def validate_version(version: str) -> str:
    match = SEMVER_RE.fullmatch(version)
    if match is None:
        raise ReleaseError("development State Agent version must be SemVer")
    prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
    if not prerelease:
        raise ReleaseError("development State Agent version must be a prerelease")
    if any(
        part.isdigit() and len(part) > 1 and part.startswith("0")
        for part in prerelease
    ):
        raise ReleaseError("numeric prerelease identifiers must not have leading zeros")
    return version


def _semver_components(version: str) -> tuple[tuple[int, int, int], tuple[str, ...]]:
    validate_version(version)
    match = SEMVER_RE.fullmatch(version)
    assert match is not None
    return (
        (int(match.group(1)), int(match.group(2)), int(match.group(3))),
        tuple(match.group(4).split(".")),
    )


def compare_versions(left: str, right: str) -> int:
    left_core, left_pre = _semver_components(left)
    right_core, right_pre = _semver_components(right)
    if left_core != right_core:
        return -1 if left_core < right_core else 1
    for left_part, right_part in zip(left_pre, right_pre):
        if left_part == right_part:
            continue
        left_numeric = left_part.isdigit()
        right_numeric = right_part.isdigit()
        if left_numeric and right_numeric:
            return -1 if int(left_part) < int(right_part) else 1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return -1 if left_part < right_part else 1
    if len(left_pre) == len(right_pre):
        return 0
    return -1 if len(left_pre) < len(right_pre) else 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_metadata(
    *, artifact: Path, source_revision: str, output: Path, source_root: Path
) -> None:
    if re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
        raise ReleaseError("source revision must be a full lower-case commit SHA")
    version = validate_version(
        (source_root / "state-agent/VERSION").read_text(encoding="utf-8").strip()
    )
    key_bytes = bytes.fromhex(
        (source_root / "state-agent/release-public-key.hex")
        .read_text(encoding="ascii")
        .strip()
    )
    if len(key_bytes) != 32:
        raise ReleaseError("release public key must contain 32 bytes")
    metadata = {
        "schema_version": METADATA_SCHEMA_VERSION,
        "source_revision": source_revision,
        "version": version,
        "platform": PLATFORM,
        "release_public_key_sha256": hashlib.sha256(key_bytes).hexdigest(),
        "artifact": {
            "filename": "plaf203-state-agent",
            "sha256": sha256_file(artifact),
            "size": artifact.stat().st_size,
        },
    }
    output.write_text(
        json.dumps(metadata, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _required_object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseError(f"{name} must be an object")
    return value


def validate_metadata(
    *,
    metadata_path: Path,
    artifact_path: Path,
    source_root: Path,
    source_revision: str,
) -> str:
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("build metadata is not valid UTF-8 JSON") from exc
    metadata = _required_object(metadata, "metadata")
    expected = {
        "schema_version",
        "source_revision",
        "version",
        "platform",
        "release_public_key_sha256",
        "artifact",
    }
    if set(metadata) != expected:
        raise ReleaseError("build metadata fields are invalid")
    if metadata["schema_version"] != METADATA_SCHEMA_VERSION:
        raise ReleaseError("unsupported build metadata schema")
    if metadata["source_revision"] != source_revision:
        raise ReleaseError("workflow artifact source revision does not match")
    if metadata["platform"] != PLATFORM:
        raise ReleaseError("workflow artifact platform does not match")

    source_version = run(
        ["git", "show", f"{source_revision}:state-agent/VERSION"], cwd=source_root
    ).stdout.strip()
    version = validate_version(source_version)
    if metadata["version"] != version:
        raise ReleaseError("workflow artifact version does not match committed source")

    source_key = run(
        ["git", "show", f"{source_revision}:state-agent/release-public-key.hex"],
        cwd=source_root,
    ).stdout.strip()
    try:
        source_key_bytes = bytes.fromhex(source_key)
    except ValueError as exc:
        raise ReleaseError("committed release public key is invalid") from exc
    if len(source_key_bytes) != 32:
        raise ReleaseError("committed release public key must contain 32 bytes")
    if metadata["release_public_key_sha256"] != hashlib.sha256(
        source_key_bytes
    ).hexdigest():
        raise ReleaseError("workflow artifact trust anchor does not match source")

    artifact = _required_object(metadata["artifact"], "artifact")
    if set(artifact) != {"filename", "sha256", "size"}:
        raise ReleaseError("build metadata artifact fields are invalid")
    if artifact["filename"] != "plaf203-state-agent":
        raise ReleaseError("unexpected workflow artifact filename")
    if artifact["sha256"] != sha256_file(artifact_path):
        raise ReleaseError("workflow artifact SHA-256 does not match metadata")
    if artifact["size"] != artifact_path.stat().st_size:
        raise ReleaseError("workflow artifact size does not match metadata")
    return version


def verify_arm_elf(path: Path) -> None:
    file_output = run(["file", str(path)]).stdout
    if not all(
        fragment in file_output
        for fragment in ("ELF 32-bit", "ARM", "statically linked")
    ):
        raise ReleaseError("artifact is not a static 32-bit ARM ELF")
    header = run(["readelf", "-h", str(path)]).stdout
    attributes = run(["readelf", "-A", str(path)]).stdout
    program_headers = run(["readelf", "-l", str(path)]).stdout
    if "Machine:                           ARM" not in header:
        raise ReleaseError("artifact ELF machine is not ARM")
    if "hard-float ABI" not in header or "Tag_ABI_VFP_args: VFP registers" not in attributes:
        raise ReleaseError("artifact does not use the expected hard-float ABI")
    if "INTERP" in program_headers:
        raise ReleaseError("artifact unexpectedly contains a dynamic interpreter")


def _tree_hashes(root: Path, paths: tuple[Path, ...]) -> dict[Path, str]:
    values: dict[Path, str] = {}
    for relative in paths:
        path = root / relative
        if not path.is_file():
            raise ReleaseError(f"required stable release file is missing: {relative}")
        values[relative] = sha256_file(path)
    return values


def _assert_stable_unchanged(root: Path, expected: dict[Path, str]) -> None:
    if _tree_hashes(root, tuple(expected)) != expected:
        raise ReleaseError("stable State Agent feed changed during development publication")


def _download_exact(url: str, *, retries: int = 8) -> bytes:
    opener = request.build_opener(_RejectRedirect())
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with opener.open(url, timeout=15) as response:
                if response.status != 200 or response.geturl() != url:
                    raise ReleaseError("published release URL changed or was unavailable")
                return response.read()
        except (OSError, error.URLError, ReleaseError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2)
    raise ReleaseError(f"published file could not be verified: {last_error}")


def _git_clean(worktree: Path) -> None:
    if run(["git", "status", "--porcelain"], cwd=worktree).stdout:
        raise ReleaseError("release worktree must be clean")


def _verify_release_worktree(worktree: Path, remote: str) -> None:
    branch = run(["git", "branch", "--show-current"], cwd=worktree).stdout.strip()
    if branch != RELEASE_BRANCH:
        raise ReleaseError(f"release worktree must be on {RELEASE_BRANCH}")
    _git_clean(worktree)
    run(["git", "fetch", remote, RELEASE_BRANCH], cwd=worktree, capture=False)
    local = run(["git", "rev-parse", "HEAD"], cwd=worktree).stdout.strip()
    upstream = run(
        ["git", "rev-parse", f"{remote}/{RELEASE_BRANCH}"], cwd=worktree
    ).stdout.strip()
    if local != upstream:
        raise ReleaseError("release worktree is not at the current remote branch head")


def _workflow_artifact(
    *, run_id: str, source_revision: str, source_root: Path, output: Path
) -> tuple[Path, Path]:
    details = json.loads(
        run(
            [
                "gh",
                "run",
                "view",
                run_id,
                "--repo",
                REPOSITORY,
                "--json",
                "conclusion,headSha,name,url",
            ]
        ).stdout
    )
    if details.get("name") != "Build State Agent release artifact":
        raise ReleaseError("workflow run is not the State Agent release build")
    if details.get("conclusion") != "success":
        raise ReleaseError("State Agent release workflow did not succeed")
    if details.get("headSha") != source_revision:
        raise ReleaseError("workflow run head SHA does not match source revision")
    version = validate_version(
        run(
            ["git", "show", f"{source_revision}:state-agent/VERSION"],
            cwd=source_root,
        ).stdout.strip()
    )
    name = f"state-agent-{version}-{source_revision[:12]}"
    output.mkdir(parents=True, exist_ok=False)
    run(
        [
            "gh",
            "run",
            "download",
            run_id,
            "--repo",
            REPOSITORY,
            "--name",
            name,
            "--dir",
            str(output),
        ],
        capture=False,
    )
    return output / "plaf203-state-agent", output / "build-metadata.json"


def _existing_feed_version(worktree: Path) -> str | None:
    manifest = worktree / DEVELOPMENT_ROOT / "latest.json"
    if not manifest.exists():
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("existing development manifest is invalid") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("version"), str):
        raise ReleaseError("existing development manifest has no valid version")
    return validate_version(payload["version"])


def _only_paths_changed(worktree: Path, allowed: set[str]) -> None:
    changed = set(
        filter(
            None,
            run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=worktree,
            ).stdout.splitlines(),
        )
    )
    changed_paths = {line[3:] for line in changed}
    if changed_paths != allowed:
        raise ReleaseError(f"unexpected release worktree changes: {sorted(changed_paths)}")


def _commit_and_push(worktree: Path, remote: str, message: str) -> str:
    run(["git", "commit", "-m", message], cwd=worktree, capture=False)
    commit = run(["git", "rev-parse", "HEAD"], cwd=worktree).stdout.strip()
    run(
        ["git", "push", remote, f"HEAD:{RELEASE_BRANCH}"],
        cwd=worktree,
        capture=False,
    )
    return commit


def _artifact_public_url(commit: str, version: str) -> str:
    return (
        f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}/"
        f"state-agent/dev/{version}/plaf203-state-agent"
    )


def _development_manifest_url() -> str:
    return (
        f"https://raw.githubusercontent.com/{REPOSITORY}/{RELEASE_BRANCH}/"
        "state-agent/dev/latest.json"
    )


def _verify_with_addon(source_root: Path, manifest_url: str, version: str) -> None:
    sys.path.insert(0, str(source_root / "addon/appdaemon/src"))
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from state_agent_updates import fetch_verified_release_bundle

    public_key = Ed25519PublicKey.from_public_bytes(
        bytes.fromhex(
            (source_root / "state-agent/release-public-key.hex")
            .read_text(encoding="ascii")
            .strip()
        )
    )
    bundle = fetch_verified_release_bundle(
        manifest_url=manifest_url,
        public_key=public_key,
        fetch_artifact=True,
    )
    if bundle.manifest.version != version or bundle.artifact_bytes is None:
        raise ReleaseError("published development bundle did not verify as expected")


def publish(args: argparse.Namespace) -> None:
    source_root = args.source_root.resolve()
    worktree = args.release_worktree.resolve()
    source_revision = args.source_revision
    if re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
        raise ReleaseError("source revision must be a full lower-case commit SHA")
    run(["git", "cat-file", "-e", f"{source_revision}^{{commit}}"], cwd=source_root)
    if run(["git", "rev-parse", "HEAD"], cwd=source_root).stdout.strip() != source_revision:
        raise ReleaseError("source worktree HEAD must equal the published revision")
    _git_clean(source_root)
    if not os.environ.get("PETLIBRO_STATE_AGENT_SIGNING_KEY"):
        raise ReleaseError("PETLIBRO_STATE_AGENT_SIGNING_KEY is required")

    _verify_release_worktree(worktree, args.remote)
    stable_hashes = _tree_hashes(worktree, STABLE_FILES)

    with tempfile.TemporaryDirectory(prefix="plaf203-state-agent-release-") as raw:
        temporary = Path(raw)
        artifact, metadata = _workflow_artifact(
            run_id=args.workflow_run_id,
            source_revision=source_revision,
            source_root=source_root,
            output=temporary / "workflow",
        )
        version = validate_metadata(
            metadata_path=metadata,
            artifact_path=artifact,
            source_root=source_root,
            source_revision=source_revision,
        )
        verify_arm_elf(artifact)

        previous = _existing_feed_version(worktree)
        if previous == version and args.resume:
            _verify_with_addon(source_root, _development_manifest_url(), version)
            print(f"Development release {version} is already published and verified")
            return
        if previous is not None and compare_versions(version, previous) <= 0:
            raise ReleaseError(
                f"development feed version must advance beyond {previous}; got {version}"
            )

        destination = worktree / DEVELOPMENT_ROOT / version / "plaf203-state-agent"
        relative_destination = destination.relative_to(worktree).as_posix()
        if destination.exists():
            if not args.resume:
                raise ReleaseError(
                    f"immutable development artifact already exists: {destination}"
                )
            if sha256_file(destination) != sha256_file(artifact):
                raise ReleaseError("existing immutable artifact differs from CI artifact")
            commits = run(
                [
                    "git",
                    "log",
                    "--diff-filter=A",
                    "--format=%H",
                    "--",
                    relative_destination,
                ],
                cwd=worktree,
            ).stdout.splitlines()
            if not commits:
                raise ReleaseError("existing immutable artifact has no add commit")
            artifact_commit = commits[0]
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(artifact, destination)
            destination.chmod(
                stat.S_IRUSR
                | stat.S_IWUSR
                | stat.S_IXUSR
                | stat.S_IRGRP
                | stat.S_IXGRP
                | stat.S_IROTH
                | stat.S_IXOTH
            )
            _assert_stable_unchanged(worktree, stable_hashes)
            _only_paths_changed(worktree, {relative_destination})
            run(["git", "add", "--", relative_destination], cwd=worktree)
            artifact_commit = _commit_and_push(
                worktree,
                args.remote,
                f"Publish development State Agent {version} artifact",
            )
        artifact_url = _artifact_public_url(artifact_commit, version)
        published_artifact = _download_exact(artifact_url)
        if hashlib.sha256(published_artifact).hexdigest() != sha256_file(artifact):
            raise ReleaseError("published immutable artifact does not match CI artifact")

        generated = temporary / "signed"
        release_url = (
            f"https://github.com/{REPOSITORY}/tree/{artifact_commit}/"
            f"state-agent/dev/{version}"
        )
        run(
            [
                sys.executable,
                str(source_root / "state-agent/scripts/build_release_manifest.py"),
                "--artifact-path",
                str(artifact),
                "--artifact-url",
                artifact_url,
                "--release-url",
                release_url,
                "--version-file",
                str(source_root / "state-agent/VERSION"),
                "--public-key-file",
                str(source_root / "state-agent/release-public-key.hex"),
                "--output-dir",
                str(generated),
            ],
            capture=False,
        )
        manifest = json.loads((generated / "latest.json").read_text(encoding="utf-8"))
        if manifest.get("channel") != "stable" or manifest.get("version") != version:
            raise ReleaseError("generated manifest does not preserve the update protocol")

        dev_root = worktree / DEVELOPMENT_ROOT
        dev_root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(generated / "latest.json", dev_root / "latest.json")
        shutil.copyfile(generated / "latest.json.sig", dev_root / "latest.json.sig")
        manifest_paths = {
            (DEVELOPMENT_ROOT / "latest.json").as_posix(),
            (DEVELOPMENT_ROOT / "latest.json.sig").as_posix(),
        }
        _assert_stable_unchanged(worktree, stable_hashes)
        _only_paths_changed(worktree, manifest_paths)
        run(["git", "add", "--", *sorted(manifest_paths)], cwd=worktree)
        manifest_commit = _commit_and_push(
            worktree,
            args.remote,
            f"Publish signed development State Agent {version} manifest",
        )
        _assert_stable_unchanged(worktree, stable_hashes)
        _git_clean(worktree)

        manifest_url = _development_manifest_url()
        _verify_with_addon(source_root, manifest_url, version)
        print(f"Published State Agent development release {version}")
        print(f"Source revision: {source_revision}")
        print(f"Artifact commit: {artifact_commit}")
        print(f"Manifest commit: {manifest_commit}")
        print(f"Artifact URL: {artifact_url}")
        print(f"Development manifest URL: {manifest_url}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    version = subparsers.add_parser("version", help="validate a development version")
    version.add_argument("value")
    metadata = subparsers.add_parser("metadata", help="write CI build metadata")
    metadata.add_argument("--artifact", type=Path, required=True)
    metadata.add_argument("--source-revision", required=True)
    metadata.add_argument("--output", type=Path, required=True)
    publish_parser = subparsers.add_parser(
        "publish", help="download, sign, and publish a successful CI build"
    )
    publish_parser.add_argument("--workflow-run-id", required=True)
    publish_parser.add_argument("--source-revision", required=True)
    publish_parser.add_argument("--release-worktree", type=Path, required=True)
    publish_parser.add_argument(
        "--source-root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    publish_parser.add_argument("--remote", default="origin")
    publish_parser.add_argument(
        "--resume",
        action="store_true",
        help="resume after an identical immutable artifact was already published",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "version":
            print(validate_version(args.value))
        elif args.command == "metadata":
            write_metadata(
                artifact=args.artifact,
                source_revision=args.source_revision,
                output=args.output,
                source_root=Path(__file__).resolve().parents[2],
            )
        else:
            publish(args)
    except (OSError, ReleaseError, ValueError) as exc:
        print(f"development release failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
