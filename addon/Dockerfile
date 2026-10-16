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

FROM debian:bookworm-slim AS bootstrap-artifact-builder

ARG DROPBEAR_REPOSITORY=https://github.com/mkj/dropbear.git
ARG DROPBEAR_TAG=DROPBEAR_2026.94
ARG DROPBEAR_COMMIT=28216cd9af822732a1549b78621c2ea9a76a0fe5
ARG ZIG_VERSION=0.16.0

RUN apt-get update -o APT::Update::Error-Mode=any \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git make gcc-arm-linux-gnueabihf libc6-dev-armhf-cross binutils xz-utils \
    && rm -rf /var/lib/apt/lists/*

COPY state-agent/ /src/state-agent/
RUN make -C /src/state-agent arm-release \
    && mkdir -p /out/state-agent \
    && cp \
        /src/state-agent/plaf203-state-agent \
        /src/state-agent/plaf203-update-fs \
        /src/state-agent/app_start_snippet.sh \
        /out/state-agent/ \
    && cp -R /src/state-agent/runit /out/state-agent/runit

RUN set -eux; \
    case "$(uname -m)" in \
      x86_64) zig_arch=x86_64; build_triplet=x86_64-pc-linux-gnu; zig_sha=70e49664a74374b48b51e6f3fdfbf437f6395d42509050588bd49abe52ba3d00 ;; \
      aarch64) zig_arch=aarch64; build_triplet=aarch64-unknown-linux-gnu; zig_sha=ea4b09bfb22ec6f6ceac57ab63efb6b46e17ab08d21f69f3a48b38e1534f17 ;; \
      *) echo "unsupported build host architecture" >&2; exit 1 ;; \
    esac; \
    zig_archive="/tmp/zig-${zig_arch}-linux-${ZIG_VERSION}.tar.xz"; \
    curl --fail --location --retry 3 \
      "https://ziglang.org/download/${ZIG_VERSION}/zig-${zig_arch}-linux-${ZIG_VERSION}.tar.xz" \
      --output "${zig_archive}"; \
    echo "${zig_sha}  ${zig_archive}" | sha256sum --check -; \
    tar -xJf "${zig_archive}" -C /opt; \
    ln -s "/opt/zig-${zig_arch}-linux-${ZIG_VERSION}/zig" /usr/local/bin/zig; \
    git clone --depth 1 --branch "${DROPBEAR_TAG}" --single-branch \
      "${DROPBEAR_REPOSITORY}" /src/dropbear; \
    test "$(git -C /src/dropbear rev-parse HEAD)" = "${DROPBEAR_COMMIT}"; \
    printf '%s\n' \
      '#define DROPBEAR_SVR_PASSWORD_AUTH 0' \
      '#define DROPBEAR_SVR_PAM_AUTH 0' \
      '#define DROPBEAR_SVR_PUBKEY_AUTH 1' \
      '#define DROPBEAR_CLI_PASSWORD_AUTH 0' \
      '#define DROPBEAR_X11FWD 0' \
      > /src/dropbear/localoptions.h; \
    printf '%s\n' '#!/bin/sh' \
      'exec zig cc -target arm-linux-musleabihf -mcpu=cortex_a7 "$@"' \
      > /usr/local/bin/arm-linux-musleabihf-zig-cc; \
    chmod 0755 /usr/local/bin/arm-linux-musleabihf-zig-cc; \
    cd /src/dropbear; \
    CC=/usr/local/bin/arm-linux-musleabihf-zig-cc \
      AR='zig ar' RANLIB='zig ranlib' SOURCE_DATE_EPOCH=0 \
      ./configure \
        --build="${build_triplet}" \
        --host=arm-linux-musleabihf \
        --disable-harden --disable-zlib --disable-lastlog \
        --disable-utmp --disable-utmpx --disable-wtmp --disable-wtmpx \
        --disable-syslog; \
    CC=/usr/local/bin/arm-linux-musleabihf-zig-cc \
      AR='zig ar' RANLIB='zig ranlib' SOURCE_DATE_EPOCH=0 \
      make 'PROGRAMS=dropbear dropbearkey' MULTI=1 STATIC=1 \
        'CFLAGS=-Os -g0 -fstack-protector-strong -D_FORTIFY_SOURCE=2 -I/src/dropbear -I/src/dropbear/src -ffile-prefix-map=/src/dropbear=.' \
        'LDFLAGS=-s -Wl,--build-id=none'; \
    readelf -h -A /src/dropbear/dropbearmulti | grep -q 'hard-float ABI'; \
    ! readelf -l /src/dropbear/dropbearmulti | grep -q INTERP; \
    mkdir -p /out/dropbear; \
    cp /src/dropbear/dropbearmulti /out/dropbear/dropbearmulti; \
    cp /src/dropbear/LICENSE /out/dropbear/LICENSE.dropbear; \
    dropbear_sha="$(sha256sum /out/dropbear/dropbearmulti | cut -d ' ' -f 1)"; \
    printf '{\n  "schema": 1,\n  "repository": "%s",\n  "tag": "%s",\n  "commit": "%s",\n  "zig_version": "%s",\n  "target": "arm-linux-musleabihf",\n  "cpu": "cortex_a7",\n  "programs": ["dropbear", "dropbearkey"],\n  "password_auth": false,\n  "dropbearmulti_sha256": "%s"\n}\n' \
      "${DROPBEAR_REPOSITORY}" "${DROPBEAR_TAG}" "${DROPBEAR_COMMIT}" \
      "${ZIG_VERSION}" "${dropbear_sha}" \
      > /out/dropbear/BUILD_INFO.dropbear.json

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
COPY --from=management-builder /src/management/worker/assets/ /opt/petlibro-management-worker/assets/
COPY --from=bootstrap-artifact-builder /out/ /opt/petlibro-bootstrap-artifacts/
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
        /etc/services.d/management-gateway/finish \
        /etc/services.d/management-worker/run

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}" \
    org.opencontainers.image.title="Petlibro Local backend" \
    org.opencontainers.image.description="Local PLAF203 MQTT control and camera streaming backend" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.source="https://github.com/tannerln7/Petlibro-Home-Assistant"
