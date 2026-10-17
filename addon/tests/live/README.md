# PLAF203 live release qualification (v1)

Live hardware tests of the **installed** Petlibro Home Assistant add-on. These tests supplement, but **do not replace**, `scripts/validate.sh` and `go test -race`. By default they only attach consumers to a pre-existing stream: they do not restart the add-on, change feeder settings, issue MQTT or State Agent writes, or manipulate firmware.

This suite is a *qualification aid*, not a blanket guarantee of complete client interoperability. In particular, a successful FFmpeg software decode does **not** prove Frigate hardware/QSV decoding works, and WHEP through go2rtc does **not** prove browsers can render after an in-band SPS transition. Such gaps are named in the report rather than silently counted as covered.

## Prerequisites

- Python 3.10+, FFmpeg with `ffmpeg`, Node.js and npm (manage these with `mise` if desired)
- The installed add-on is reachable over LAN, with go2rtc API and RTSP enabled
- Go 1.24+ **only** when compiling the optional intermediate go2rtc or using the direct Go harness
- A Chromium browser installed for Playwright (no root changes needed if OS libraries already exist)

From the repo root:

```sh
cp addon/tests/live/.env.example addon/tests/live/.env
# Edit addon/tests/live/.env with real LAN endpoints, stream name, and options.
cd addon/tests/live
npm install --no-audit --no-fund
npx playwright install chromium
cd ../../..
python3 -m unittest discover -s addon/tests/live -p 'test_*.py'
python3 addon/tests/live/run.py --list
python3 addon/tests/live/run.py --suite smoke
```

For `mise` environments, prefix commands with `mise exec --`. `npx playwright` working from another directory is not sufficient: `browser.cjs` needs this directory's `node_modules/playwright` package. **Never run `playwright install-deps` or change system permissions automatically**. If OS dependencies are missing, report the blocker and address it explicitly.

To enable two-hop RTSP and WHEP scenarios, compile the exact locked standalone
go2rtc revision, then set `TEST_GO2RTC_BINARY` in `.env`:

```sh
go2rtc_source=$(scripts/fetch-go2rtc.sh)
(cd "$go2rtc_source" && go build -o /tmp/petlibro-qualification-go2rtc .)
```

Example release run:

```sh
python3 addon/tests/live/run.py --suite release --soak-seconds 300
```

`smoke` is quick; `release` includes A/V (when declared), RTSP and WHEP chained consumers, concurrent viewers and abrupt viewer teardown, a software decoder soak, and optional Frigate restream endpoint. `stress` additionally exposes an **explicitly gated** direct physical Go harness and marks uninduced codec changes as REVIEW. All direct-session tests are sequential, not implicitly simultaneous with consumer tests.

## Configuration

Populate `addon/tests/live/.env`; this is Git-ignored. Configuration consists only of external endpoints, optional binary paths, and test environment assertions. Use the supplied `.env.example` as the authoritative key list. Setting `TEST_EXPECT_AUDIO=1` asserts that microphone audio is enabled on the installed add-on; when false, A/V tests are SKIPPED rather than spuriously passed. Release/stress preflight compares this assertion with the generated FFmpeg audio source and fails on a mismatch; smoke remains usable for video-only checks. For strongest audio proof, play a non-sensitive repeatable stimulus within the feeder's microphone range and set `TEST_AUDIO_STIMULUS_PRESENT=1`. Without that flag, silent decoded audio returns REVIEW rather than an unsupported assertion of audible sound.

To test a direct physical session, set `TEST_FEEDER_IP`, `TEST_FEEDER_UID`, and `TEST_NATIVE_HARNESS_DIR` (the directory containing Codex's private `main.go` and `go.mod`), then run `--suite stress --allow-direct-session`. The runner creates a private source file, never passes the feeder UID on the shell command line, and removes that source file after the run. **The native tool is not bundled or silently copied from ignored investigation artifacts.** Its JSONL events are checked in addition to its exit code because early investigation versions could return success after read failures.

`TEST_FRIGATE_RESTREAM_URL` optionally points to the RTSP restream endpoint exposed by an existing Frigate installation. This tests its restream availability to FFmpeg, **not** Frigate's detection, recording, or GPU decoding. End-to-end Frigate qualification remains a manual/extended integration requirement.

`TEST_ADDON_IMAGE_DIGEST` is an optional *operator-asserted* provenance field. The go2rtc API does not independently report the Home Assistant add-on image digest. Do not call an unverified digest a tested release artifact; confirm the installed image externally before promoting a stable release.

## Evidence and verdicts

Each run creates a mode-0700 directory under ignored `addon/tests/live/artifacts/run-*`, containing JSON/Markdown summaries, FFmpeg progress/stderr, and browser result metadata. Raw media is not recorded. The browser verifies **decoded H.264 frame-count advancement, frame presentation callbacks, media-element playback time**, and—when audio is enabled—the Opus receiver, decoded audio sample progression, and optionally non-silent PCM via Web Audio. A connected WebRTC peer or rising RTP counters alone never count as a pass.

Exit codes: `0`=all PASS, `1`=any FAIL, `2`=incomplete (`REVIEW` or `SKIPPED`) or bad config. These statuses are deliberately strict. Tests with missing tools do not silently become PASS. The `results.json` contains scenario verdicts plus coarse graph states, not bearer tokens, private URLs, SDP or camera packets. Other stderr and browser artifacts can still reveal LAN IPs; keep the entire artifacts directory private.

After media tests, the runner compares the active go2rtc graph with its initial state. Configured stream definitions and FFmpeg *template* sources legitimately remain after disconnect; only active producers/consumers should disappear. If other viewers were already present before the run, teardown proof is REVIEW, not a false clean-idle claim.

## Known v1 boundaries and future work

- Frigate detection/hardware decoder integration and Home Assistant Advanced Camera Card need a genuine installation-specific test adapter; do not infer them from FFmpeg or go2rtc WHEP success.
- Resolution changes can occur after the initial HD preference window. The fixed start/end codec and FFmpeg frame checks do not prove persistent-browser decode through mid-session SPS changes; a deliberately induced transition needs separate coverage.
- Static scenes defeat simple frame-uniqueness checks; the browser currently verifies presentation callbacks, not semantic video correctness. A moving timestamp/shape stimulus and objective pixel inspection are future additions. No general-purpose computer vision "good picture" claim is made.
- Audio absence does not prove defective transport unless the installed add-on is configured for audio. Audio energy requires a stimulus to establish a meaningful audible-content expectation.
- Network interruptions and deliberate feeder interference are not run by default. Do not import device-side `block-camera-once.sh` into this suite.
- There is no privileged operation, Docker restart, or automatic permission repair. Test executables only bind temporary local ports and terminate their own process groups.

Use `docs/camera-development.md` and `docs/camera-debugging.md` for source and
transport references. Keep regression unit tests (including
ACK/assembler/receiver races) as separate deterministic gates.
