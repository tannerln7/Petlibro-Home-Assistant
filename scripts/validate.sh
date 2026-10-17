#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON_BIN:-python3}"
strict=0

case "${1:-}" in
    "") ;;
    --strict) strict=1 ;;
    *)
        printf 'Usage: %s [--strict]\n' "${0##*/}" >&2
        exit 2
        ;;
esac
if (( $# > 1 )); then
    printf 'Usage: %s [--strict]\n' "${0##*/}" >&2
    exit 2
fi

cd "${repo_root}"

has_appdaemon=0
has_arm_compiler=0
has_shellcheck=0
"${python_bin}" -c 'import appdaemon' >/dev/null 2>&1 && has_appdaemon=1
command -v arm-linux-gnueabihf-gcc >/dev/null 2>&1 && has_arm_compiler=1
command -v shellcheck >/dev/null 2>&1 && has_shellcheck=1

if (( strict )); then
    missing=()
    (( has_appdaemon )) || missing+=("AppDaemon Python dependencies")
    (( has_arm_compiler )) || missing+=("arm-linux-gnueabihf-gcc")
    (( has_shellcheck )) || missing+=("shellcheck")
    if (( ${#missing[@]} )); then
        printf -v missing_list '%s, ' "${missing[@]}"
        printf 'Strict validation requires: %s\n' "${missing_list%, }" >&2
        exit 2
    fi
fi

skipped=()

# State Agent integration tests start loopback HTTP servers. Never send their
# bearer-authenticated probes through an ambient development/CI proxy.
export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,${NO_PROXY}}"
export no_proxy="127.0.0.1,localhost${no_proxy:+,${no_proxy}}"

mapfile -t python_sources < <(
    find addon installer scripts state-agent/tests \
        -type f -name '*.py' \
        -not -path '*/__pycache__/*' \
        -print | sort
)
"${python_bin}" -m py_compile "${python_sources[@]}"
"${python_bin}" scripts/check-doc-links.py

"${python_bin}" -m pytest \
    addon/tests \
    state-agent/tests \
    installer/tests \
    -q

if (( has_appdaemon )); then
    "${python_bin}" -m pytest addon/appdaemon/tests -q
else
    printf 'Skipping AppDaemon controller tests: install addon/appdaemon/requirements-dev.txt\n'
    skipped+=("AppDaemon controller tests")
fi

make -C state-agent clean all

if (( has_arm_compiler )); then
    make -C state-agent clean arm-release
    for binary in \
        state-agent/plaf203-state-agent \
        state-agent/plaf203-update-fs; do
        file "${binary}" | grep -Eq 'ELF 32-bit.*ARM.*statically linked'
        readelf -h "${binary}" | grep -Eq 'Class:[[:space:]]+ELF32'
        readelf -h "${binary}" | grep -Eq 'Machine:[[:space:]]+ARM'
        readelf -h "${binary}" | grep -Eq 'Flags:.*hard-float ABI'
        readelf -A "${binary}" | grep -Eq 'Tag_CPU_arch: v7'
        readelf -A "${binary}" | grep -Eq 'Tag_ABI_VFP_args: VFP registers'
        if readelf -l "${binary}" | grep -q 'INTERP'; then
            printf '%s contains a dynamic interpreter\n' "${binary}" >&2
            exit 1
        fi
    done
else
    printf 'Skipping ARM release validation: arm-linux-gnueabihf-gcc is unavailable\n'
    skipped+=("ARM release validation")
fi

go2rtc_source="$("${repo_root}/scripts/fetch-go2rtc.sh")"
(
    cd "${go2rtc_source}"
    go test ./pkg/petlibro ./cmd/petlibro-resolve -count=1
    go test ./internal/streams \
        -run '^Test(TimestampContinuity|StreamCorrelationIDs)' -count=1
    go test ./pkg/rtsp \
        -run '^Test(PacketWriter|PacketWriters|WriteInterleavedData)' -count=1
    go test -race ./pkg/petlibro ./cmd/petlibro-resolve -count=1
    go test -race ./internal/streams \
        -run '^Test(TimestampContinuity|StreamCorrelationIDs)' -count=1
    go test -race ./pkg/rtsp \
        -run '^Test(PacketWriter|PacketWriters|WriteInterleavedData)' -count=1
    go vet ./internal/streams ./pkg/petlibro ./pkg/rtsp ./cmd/petlibro-resolve
    go build -o /tmp/petlibro-local-go2rtc .
    go build -o /tmp/petlibro-resolve ./cmd/petlibro-resolve
)

docker compose --env-file docker/.env.example \
    -f docker/docker-compose.yml config --quiet

for script in scripts/*.sh addon/run.sh addon/rootfs/etc/services.d/*/run; do
    bash -n "${script}"
done

for script in \
    state-agent/app_start_snippet.sh \
    state-agent/runit/*/run \
    state-agent/runit/plaf203-update-supervisor/supervisor.sh; do
    /bin/sh -n "${script}"
done

if (( has_shellcheck )); then
    shellcheck \
        scripts/*.sh \
        state-agent/app_start_snippet.sh \
        state-agent/runit/*/run \
        state-agent/runit/plaf203-update-supervisor/supervisor.sh \
        addon/run.sh \
        addon/rootfs/etc/services.d/*/run
else
    printf 'Skipping ShellCheck: shellcheck is unavailable\n'
    skipped+=("ShellCheck")
fi

git diff --check
git diff --cached --check

if (( ${#skipped[@]} )); then
    printf -v skipped_list '%s, ' "${skipped[@]}"
    printf '\nPermissive validation passed with optional checks skipped: %s.\n' \
        "${skipped_list%, }"
elif (( strict )); then
    printf '\nAll repository checks passed in strict mode.\n'
else
    printf '\nAll repository checks passed.\n'
fi
