ARG GO_VERSION=1.26.5
ARG NODE_VERSION=24.21.0
FROM golang:${GO_VERSION}-alpine3.23 AS go2rtc-builder

ARG GO_BUILD_PROCS=""

WORKDIR /src/go2rtc
COPY addon/go2rtc/go.mod addon/go2rtc/go.sum ./
RUN go mod download
COPY addon/go2rtc/ ./
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

FROM node:${NODE_VERSION}-alpine3.23 AS management-builder

WORKDIR /src/management
COPY management/package.json management/package-lock.json ./
RUN npm ci
COPY management/ ./
RUN PETLIBRO_MANAGEMENT_BASE_PATH=/__PETLIBRO_BASE__/ \
    PETLIBRO_MANAGEMENT_SERVER_BASE_PATH=/ \
    npm run build

FROM ghcr.io/home-assistant/amd64-base-python:3.13-alpine3.23

ARG BUILD_ARCH=amd64
ARG BUILD_VERSION=dev

RUN apk add --no-cache ffmpeg jq libstdc++ nginx \
    && python -m venv /opt/appdaemon-venv \
    && /opt/appdaemon-venv/bin/pip install --no-cache-dir "AppDaemon>=4.5,<5" "cryptography>=45,<47"

COPY --from=go2rtc-builder /out/go2rtc /usr/local/bin/go2rtc
COPY --from=go2rtc-builder /out/petlibro-resolve /usr/local/bin/petlibro-resolve
COPY --from=management-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=management-builder /src/management/.output/ /opt/petlibro-management/
COPY addon/appdaemon/src/ /opt/petlibro-local/appdaemon/
COPY --from=management-builder /src/management/.worker/run.mjs /opt/petlibro-management-worker/run.mjs
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
        /etc/services.d/appdaemon/run \
        /etc/services.d/management/run \
        /etc/services.d/management-gateway/run \
        /etc/services.d/management-worker/run

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}" \
    org.opencontainers.image.title="Petlibro Local backend" \
    org.opencontainers.image.description="Local PLAF203 MQTT control and camera streaming backend" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.source="https://github.com/tannerln7/Petlibro-Home-Assistant"
