# Standalone go2rtc source

Petlibro's patched camera backend is maintained in the standalone
[`tannerln7/go2rtc`](https://github.com/tannerln7/go2rtc) repository. GitHub
records it as a direct fork of `AlexxIT/go2rtc`; the maintained default branch
is `petlibro`.

## History and ownership

The standalone history retains upstream go2rtc ancestry, the original
Petlibro integration commits based on upstream commit
`dc1685e9cf7a8c349181f20a1b4a44825ed394c5`, and the later camera commits that
were formerly developed inside this repository. Each extracted commit records
its original Petlibro Home Assistant commit. Before standalone-only files were
added, the extracted Git tree was verified identical to the former
`addon/go2rtc` tree at `e107570b2bb76755f8195cf182049ebfe8d3b76f`.

The standalone repository owns:

- `pkg/petlibro` and `internal/petlibro`;
- `cmd/petlibro-resolve`;
- Petlibro-required stream, timestamp, WebRTC, core, credential-filtering, and
  RTSP modifications outside the dedicated Petlibro packages;
- protocol fixtures, debug utilities, deterministic and race tests;
- camera implementation documentation, CI, tags, and release artifacts.

This repository owns add-on configuration, AppDaemon integration, packaging,
and the selected source revision. Runtime architecture and stream URLs are
unchanged.

## Immutable source lock

[`addon/go2rtc-source.lock`](../addon/go2rtc-source.lock) records four values:

- the fork version tag;
- the full 40-character source commit;
- the versioned source release URL; and
- the source archive SHA-256 checksum.

[`scripts/fetch-go2rtc.sh`](../scripts/fetch-go2rtc.sh) validates those values,
downloads and verifies the archive, rejects an unexpected archive layout, and
extracts it into an ignored revision-keyed cache. Repository validation runs
the same focused, race, vet, and binary-build checks that were used before the
split.

The add-on Docker builder independently downloads the same release archive and
verifies the same checksum before compiling `go2rtc` and
`petlibro-resolve`. No source or network dependency is added to the runtime
image.

## Releases and updates

Standalone tags use `v<upstream>-petlibro.<revision>`. The fork's test workflow
runs on pull requests and `petlibro` pushes. Its release workflow validates a
tag and publishes deterministic source plus Linux amd64 binary archives and a
checksum manifest.

A standalone release never triggers an add-on release. To adopt it, update all
lock values together in a reviewed Home Assistant change, verify a clean-cache
fetch, run `./scripts/validate.sh`, and build the add-on image. Production pins
must never use a moving branch or `latest` URL.
