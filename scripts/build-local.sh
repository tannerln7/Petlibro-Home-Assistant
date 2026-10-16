#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
image="${IMAGE:-petlibro-local:local}"
go_build_procs="${GO_BUILD_PROCS:-2}"

exec docker build \
    --file "${repo_root}/addon/Dockerfile" \
    --build-arg BUILD_ARCH=amd64 \
    --build-arg BUILD_VERSION=local \
    --build-arg "GO_BUILD_PROCS=${go_build_procs}" \
    --tag "${image}" \
    "$@" \
    "${repo_root}"
