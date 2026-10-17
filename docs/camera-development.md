# Camera development

The Petlibro camera implementation is maintained independently in the
[`tannerln7/go2rtc`](https://github.com/tannerln7/go2rtc) fork. The fork is a
direct GitHub fork of AlexxIT/go2rtc and keeps the upstream module path
`github.com/AlexxIT/go2rtc`. Its `petlibro` branch owns the Kalay/TUTK
transport, producer integration, stream/timestamp and RTSP patches,
`petlibro-resolve`, protocol fixtures, regression tests, and detailed
[development guide](https://github.com/tannerln7/go2rtc/blob/petlibro/docs/development.md).

This repository owns only Home Assistant packaging and runtime integration.
The exact standalone source is recorded in
[`addon/go2rtc-source.lock`](../addon/go2rtc-source.lock) as a full commit,
versioned source release URL, and SHA-256 checksum. Both
[`scripts/validate.sh`](../scripts/validate.sh) and the add-on Docker build use
that lock; production never follows the moving `petlibro` branch.

## Validate the pinned source

From this repository root:

```bash
go2rtc_source=$(scripts/fetch-go2rtc.sh)
(cd "$go2rtc_source" && go test ./pkg/petlibro ./cmd/petlibro-resolve -count=1)
(cd "$go2rtc_source" && go test ./internal/streams \
  -run '^Test(TimestampContinuity|StreamCorrelationIDs)' -count=1)
(cd "$go2rtc_source" && go test ./pkg/rtsp \
  -run '^Test(PacketWriter|PacketWriters|WriteInterleavedData)' -count=1)
```

The fetch helper verifies the release checksum before extracting into the
ignored `build/go2rtc-source` cache. `./scripts/validate.sh` additionally runs
the maintained race tests, vet checks, and both binary builds.

## Make camera changes

1. Clone `https://github.com/tannerln7/go2rtc` and branch from `petlibro`.
2. Preserve the protocol and lifecycle invariants documented in the fork.
3. Add focused synthetic tests and run its `Test` workflow; keep captures,
   device identifiers, credentials, and media out of Git.
4. Tag a new immutable `v<upstream>-petlibro.<revision>` release. Its release
   workflow publishes source and Linux amd64 artifacts plus checksums.
5. In a separate Home Assistant change, update all four lock values together,
   test a clean download, run `./scripts/validate.sh`, and build the add-on
   image.

Do not combine a source-pin update with unrelated camera behavior changes.
Publishing a standalone camera release does not publish or version the add-on.
Runtime configuration, `petlibro://` URLs, and packaged binary locations remain
owned by this repository.

For transport logs, dump handling, and live-test interpretation, use the
fork's [debugging guide](https://github.com/tannerln7/go2rtc/blob/petlibro/docs/debugging.md)
and this repository's [live qualification harness](../addon/tests/live/README.md).
