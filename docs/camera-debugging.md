# Camera diagnostics

Camera transport diagnostics, correlation fields, trace controls, dump formats,
offline replay tests, and protocol interpretation are documented in the
standalone fork's
[debugging guide](https://github.com/tannerln7/go2rtc/blob/petlibro/docs/debugging.md).
Use the guide matching the revision in
[`addon/go2rtc-source.lock`](../addon/go2rtc-source.lock) when behavior differs
between releases.

For packaged runtime investigation:

- use the [troubleshooting guide](troubleshooting.md) for add-on logs and
  service health;
- use the [configuration reference](configuration.md) for bounded logging and
  camera protocol tracing options;
- use the [live qualification harness](../addon/tests/live/README.md) for RTSP,
  WHEP, chained-consumer, decoder, and teardown checks.

Raw packet captures, decrypted dumps, media, camera UIDs, local addresses, and
credentials must remain outside this repository. Reduce durable protocol
findings to sanitized fixtures and focused tests in the standalone fork.
