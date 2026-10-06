#!/usr/bin/env python3
"""Validate add-on release channels and emit their exact image tags."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path


STABLE_VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
DEVELOPMENT_VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-beta\.([1-9][0-9]*)$"
)


class ReleaseValidationError(ValueError):
    """Raised when a branch and version do not form a valid release channel."""


@dataclass(frozen=True)
class ReleaseMetadata:
    """Validated release metadata consumed by the image workflow."""

    branch: str
    channel: str
    version: str
    image_tags: tuple[str, ...]

    @property
    def publishes_latest(self) -> bool:
        return "latest" in self.image_tags


def release_metadata(branch: str, version: str) -> ReleaseMetadata:
    """Return fail-closed release metadata for a supported branch."""
    if branch == "main":
        if STABLE_VERSION_RE.fullmatch(version) is None:
            raise ReleaseValidationError(
                "main requires a stable x.y.z version without a prerelease suffix"
            )
        return ReleaseMetadata(branch, "stable", version, (version, "latest"))

    if branch == "develop":
        if DEVELOPMENT_VERSION_RE.fullmatch(version) is None:
            raise ReleaseValidationError(
                "develop requires a prerelease version in the form x.y.z-beta.N"
            )
        return ReleaseMetadata(branch, "development", version, (version,))

    raise ReleaseValidationError(
        f"unsupported release branch {branch!r}; expected 'main' or 'develop'"
    )


def write_github_output(path: Path, metadata: ReleaseMetadata) -> None:
    """Write validated values using GitHub Actions' output-file format."""
    with path.open("a", encoding="utf-8") as output:
        output.write(f"channel={metadata.channel}\n")
        output.write(
            f"publish_latest={'true' if metadata.publishes_latest else 'false'}\n"
        )
        output.write("image_tags<<PETLIBRO_IMAGE_TAGS\n")
        output.write("\n".join(metadata.image_tags))
        output.write("\nPETLIBRO_IMAGE_TAGS\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        metadata = release_metadata(args.branch, args.version)
    except ReleaseValidationError as error:
        print(f"release validation failed: {error}", file=sys.stderr)
        return 2

    write_github_output(args.github_output, metadata)
    print(
        f"Validated {metadata.channel} release {metadata.version} on "
        f"{metadata.branch}; tags: {', '.join(metadata.image_tags)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
