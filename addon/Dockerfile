ARG GO_VERSION=1.26.5
FROM golang:${GO_VERSION}-alpine3.23 AS go2rtc-builder

ARG GO_BUILD_PROCS=""

COPY addon/go2rtc-source.lock /tmp/go2rtc-source.lock
RUN set -eu; \
    . /tmp/go2rtc-source.lock; \
    wget -q -O /tmp/go2rtc-source.tar.gz "${GO2RTC_SOURCE_URL}"; \
    printf '%s  %s\n' "${GO2RTC_SOURCE_SHA256}" /tmp/go2rtc-source.tar.gz \
        | sha256sum -c -; \
    mkdir -p /src/go2rtc; \
    tar -xzf /tmp/go2rtc-source.tar.gz --strip-components=1 -C /src/go2rtc; \
    test -f /src/go2rtc/go.mod; \
    test -f /src/go2rtc/cmd/petlibro-resolve/main.go; \
    rm -f /tmp/go2rtc-source.tar.gz
WORKDIR /src/go2rtc
RUN go mod download
RUN set -eu; \
    set --; \
    if [ -n "${GO_BUILD_PROCS}" ]; then \
        export GOMAXPROCS="${GO_BUILD_PROCS}"; \
        set -- "-p=${GO_BUILD_PROCS}"; \
    fi; \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
        go build "$@" -trimpath -ldflags="-s -w" -o /out/go2rtc .; \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
        go build "$@" -trimpath -ldflags="-s -w" -o /out/petlibro-resolve ./cmd/petlibro-resolve

FROM ghcr.io/home-assistant/amd64-base-python:3.13-alpine3.23

ARG BUILD_ARCH=amd64
ARG BUILD_VERSION=dev

RUN apk add --no-cache ffmpeg jq \
    && python -m venv /opt/appdaemon-venv \
    && /opt/appdaemon-venv/bin/pip install --no-cache-dir "AppDaemon>=4.5,<5" "cryptography>=45,<47"

COPY --from=go2rtc-builder /out/go2rtc /usr/local/bin/go2rtc
COPY --from=go2rtc-builder /out/petlibro-resolve /usr/local/bin/petlibro-resolve
COPY addon/appdaemon/src/ /opt/petlibro-local/appdaemon/
COPY state-agent/release-public-key.hex /opt/petlibro-local/release-public-key.hex
COPY addon/templates/ /opt/petlibro-local/templates/
COPY addon/render_config.py /usr/local/bin/petlibro-render-config
COPY addon/run.sh /usr/local/bin/petlibro-local-run
COPY addon/rootfs/ /

RUN chmod 0755 \
        /usr/local/bin/go2rtc \
        /usr/local/bin/petlibro-resolve \
        /usr/local/bin/petlibro-render-config \
        /usr/local/bin/petlibro-local-run \
        /etc/services.d/go2rtc/run \
        /etc/services.d/appdaemon/run

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}" \
    org.opencontainers.image.title="Petlibro Local backend" \
    org.opencontainers.image.description="Local PLAF203 MQTT control and camera streaming backend" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.source="https://github.com/tannerln7/Petlibro-Home-Assistant"
