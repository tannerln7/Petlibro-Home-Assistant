#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON_BIN:-python3}"

cd "${repo_root}"

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

if "${python_bin}" -c 'import appdaemon' >/dev/null 2>&1; then
    "${python_bin}" -m pytest addon/appdaemon/tests -q
else
    printf 'Skipping AppDaemon controller tests: install addon/appdaemon/requirements-dev.txt\n'
fi

make -C state-agent clean all

if command -v arm-linux-gnueabihf-gcc >/dev/null 2>&1; then
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
fi

(
    cd addon/go2rtc
    go test ./pkg/petlibro -count=1
    go test -race ./pkg/petlibro -count=1
    go test ./cmd/petlibro-resolve -count=1
    go vet ./pkg/petlibro ./cmd/petlibro-resolve
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

if command -v shellcheck >/dev/null 2>&1; then
    shellcheck \
        scripts/*.sh \
        state-agent/app_start_snippet.sh \
        state-agent/runit/*/run \
        state-agent/runit/plaf203-update-supervisor/supervisor.sh \
        addon/run.sh \
        addon/rootfs/etc/services.d/*/run
fi

git diff --check
git diff --cached --check

printf "\nAll configured repository checks passed.\n"
