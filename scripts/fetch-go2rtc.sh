#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
lock_file="${repo_root}/addon/go2rtc-source.lock"

# This tracked file contains only validated source coordinates and a checksum.
# shellcheck source=/dev/null
source "${lock_file}"

require_hex() {
    local name=$1 value=$2 length=$3
    if [[ ${#value} -ne ${length} || ${value} == *[!0-9a-f]* ]]; then
        printf '%s must contain exactly %d lowercase hexadecimal characters\n' \
            "${name}" "${length}" >&2
        exit 1
    fi
}

require_hex GO2RTC_REVISION "${GO2RTC_REVISION}" 40
require_hex GO2RTC_SOURCE_SHA256 "${GO2RTC_SOURCE_SHA256}" 64
if [[ ! ${GO2RTC_VERSION} =~ ^v[0-9]+\.[0-9]+\.[0-9]+-petlibro\.[0-9]+$ ]]; then
    printf 'Invalid GO2RTC_VERSION in %s\n' "${lock_file}" >&2
    exit 1
fi
expected_url="https://github.com/tannerln7/go2rtc/releases/download/${GO2RTC_VERSION}/go2rtc_${GO2RTC_VERSION}_source.tar.gz"
if [[ ${GO2RTC_SOURCE_URL} != "${expected_url}" ]]; then
    printf 'GO2RTC_SOURCE_URL does not match the locked release version\n' >&2
    exit 1
fi

cache_root="${GO2RTC_SOURCE_CACHE:-${repo_root}/build/go2rtc-source}"
archive="${cache_root}/${GO2RTC_SOURCE_SHA256}.tar.gz"
source_dir="${cache_root}/${GO2RTC_REVISION}"
marker="${source_dir}/.petlibro-source-revision"

if [[ -f ${marker} ]] && [[ $(<"${marker}") == "${GO2RTC_REVISION}" ]] \
    && [[ -f ${source_dir}/go.mod ]] \
    && [[ -f ${source_dir}/cmd/petlibro-resolve/main.go ]]; then
    printf '%s\n' "${source_dir}"
    exit 0
fi
if [[ -e ${source_dir} ]]; then
    printf 'Cached go2rtc source is incomplete or does not match %s: %s\n' \
        "${GO2RTC_REVISION}" "${source_dir}" >&2
    exit 1
fi

mkdir -p "${cache_root}"
if [[ -f ${archive} ]]; then
    printf '%s  %s\n' "${GO2RTC_SOURCE_SHA256}" "${archive}" | sha256sum -c - >/dev/null
else
    archive_tmp="$(mktemp "${cache_root}/.go2rtc-archive.XXXXXX")"
    trap 'rm -f -- "${archive_tmp}"' EXIT
    curl --fail --location --silent --show-error \
        --output "${archive_tmp}" "${GO2RTC_SOURCE_URL}"
    printf '%s  %s\n' "${GO2RTC_SOURCE_SHA256}" "${archive_tmp}" \
        | sha256sum -c - >/dev/null
    mv -- "${archive_tmp}" "${archive}"
    trap - EXIT
fi

prefix="go2rtc-${GO2RTC_VERSION}/"
if ! tar -tzf "${archive}" | awk -v prefix="${prefix}" '
    index($0, prefix) != 1 || $0 ~ /(^|\/)\.\.(\/|$)/ || $0 ~ /^\// { exit 1 }
'; then
    printf 'go2rtc source archive has an unexpected layout\n' >&2
    exit 1
fi

extract_tmp="$(mktemp -d "${cache_root}/.go2rtc-source.XXXXXX")"
trap 'rm -rf -- "${extract_tmp}"' EXIT
tar -xzf "${archive}" --strip-components=1 -C "${extract_tmp}"
test -f "${extract_tmp}/go.mod"
test -f "${extract_tmp}/cmd/petlibro-resolve/main.go"
printf '%s\n' "${GO2RTC_REVISION}" > "${extract_tmp}/.petlibro-source-revision"
mv -- "${extract_tmp}" "${source_dir}"
trap - EXIT

printf '%s\n' "${source_dir}"
