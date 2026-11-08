import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "state-agent" / "scripts" / "validate_development_release.py"
SPEC = importlib.util.spec_from_file_location("validate_development_release", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


@pytest.mark.parametrize(
    "version", ("0.4.0-rc.1", "0.4.0-beta.2", "1.0.0-dev.3")
)
def test_development_versions_accept_valid_prereleases(version):
    assert release.validate_version(version) == version


@pytest.mark.parametrize("version", ("0.4.0", "0.4", "0.4.0-rc.01"))
def test_development_versions_reject_stable_or_invalid_versions(version):
    with pytest.raises(release.ReleaseError):
        release.validate_version(version)


def test_development_version_progression_is_semver_ordered():
    assert release.compare_versions("0.4.0-rc.2", "0.4.0-rc.1") > 0
    assert release.compare_versions("0.4.0-rc.10", "0.4.0-rc.2") > 0
    assert release.compare_versions("0.4.1-rc.1", "0.4.0-rc.10") > 0
    assert release.compare_versions("0.4.0-rc.1", "0.4.0-rc.1") == 0


def _committed_source(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    agent = source / "state-agent"
    agent.mkdir(parents=True)
    (agent / "VERSION").write_text("0.4.0-rc.1\n", encoding="ascii")
    (agent / "release-public-key.hex").write_text("11" * 32 + "\n", encoding="ascii")
    subprocess.run(["git", "init", "-q", "-b", "develop"], cwd=source, check=True)
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=source,
        check=True,
    )
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return source, revision


def test_build_metadata_binds_artifact_revision_version_and_key(tmp_path):
    source, revision = _committed_source(tmp_path)
    artifact = tmp_path / "plaf203-state-agent"
    artifact.write_bytes(b"synthetic-arm-elf")
    metadata = tmp_path / "build-metadata.json"

    release.write_metadata(
        artifact=artifact,
        source_revision=revision,
        output=metadata,
        source_root=source,
    )

    assert (
        release.validate_metadata(
            metadata_path=metadata,
            artifact_path=artifact,
            source_root=source,
            source_revision=revision,
        )
        == "0.4.0-rc.1"
    )
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    payload["artifact"]["sha256"] = "0" * 64
    metadata.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(release.ReleaseError, match="SHA-256"):
        release.validate_metadata(
            metadata_path=metadata,
            artifact_path=artifact,
            source_root=source,
            source_revision=revision,
        )


def test_stable_feed_hash_guard_detects_any_change(tmp_path):
    stable = tmp_path / "state-agent"
    stable.mkdir()
    (stable / "latest.json").write_bytes(b"stable-manifest")
    (stable / "latest.json.sig").write_bytes(b"stable-signature")
    expected = release._tree_hashes(tmp_path, release.STABLE_FILES)

    release._assert_stable_unchanged(tmp_path, expected)
    (stable / "latest.json").write_bytes(b"changed")
    with pytest.raises(release.ReleaseError, match="stable State Agent feed changed"):
        release._assert_stable_unchanged(tmp_path, expected)
