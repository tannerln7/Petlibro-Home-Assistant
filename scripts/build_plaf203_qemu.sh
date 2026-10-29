#!/usr/bin/env bash
# Build the minimal userspace and pinned vendor-generation kernel/DTB.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cache="${1:?Usage: build_plaf203_qemu.sh CACHE_ROOT}"
offline="${PLAF203_OFFLINE:-0}"
[[ "$offline" == 0 || "$offline" == 1 ]] || { echo 'PLAF203_OFFLINE must be 0 or 1' >&2; exit 1; }
image="$(docker image inspect "${PLAF203_BUILD_IMAGE:-plaf203-qemu-bringup:review}" --format '{{.Id}}')"
lock="$repo/firmware/plaf203/sources.lock.json"
version="$(jq -r '.buildroot.version' "$lock")"
epoch="${SOURCE_DATE_EPOCH:-$(jq -r '.source_date_epoch' "$lock")}"
[[ "$epoch" =~ ^[0-9]+$ ]] || { echo 'Invalid SOURCE_DATE_EPOCH' >&2; exit 1; }
mkdir -p "$cache/downloads" "$cache/buildroot" "$cache/toolchain" "$cache/minimal-output"
chmod 0700 "$cache/minimal-output"
cache="$(realpath "$cache")"
fetch() {
  local file="$1" digest="$2" url="$3"
  if [[ ! -f "$cache/downloads/$file" ]]; then
    [[ "$offline" == 0 ]] || { printf 'Offline input missing: %s\n' "$file" >&2; return 1; }
    curl -fL --retry 2 --max-time 300 "$url" -o "$cache/downloads/$file.part"
    printf '%s  %s\n' "$digest" "$cache/downloads/$file.part" | sha256sum -c -
    mv "$cache/downloads/$file.part" "$cache/downloads/$file"
  fi
  printf '%s  %s\n' "$digest" "$cache/downloads/$file" | sha256sum -c -
}
for component in buildroot toolchain kernel; do
  fetch "$(jq -r ".$component.file" "$lock")" "$(jq -r ".$component.sha256" "$lock")" "$(jq -r ".$component.url" "$lock")"
done
[[ -d "$cache/buildroot/buildroot-$version" ]] || tar -xf "$cache/downloads/buildroot-$version.tar.xz" -C "$cache/buildroot"
python3 "$repo/scripts/plaf203_build_receipt.py" verify-tree \
  "$cache/downloads/buildroot-$version.tar.xz" "$cache/buildroot/buildroot-$version"
[[ -x "$cache/toolchain/bin/arm-openipc-linux-musleabi-gcc" ]] || \
  tar -xf "$cache/downloads/toolchain.goke-gk7205v200.tgz" -C "$cache/toolchain" --strip-components=1
enrollment=()
for name in PLAF203_ENROLLMENT_PUBKEY PLAF203_TEST_SEED; do
  if [[ -n "${!name:-}" ]]; then
    input="$(realpath "${!name}")"
    [[ -f "$input" ]]
    enrollment+=(-v "$input:/$name:ro" -e "$name=/$name")
  fi
done
network=host
[[ "$offline" == 0 ]] || network=none
docker run --rm --network="$network" \
  -e OWNER_UID="$(id -u)" -e OWNER_GID="$(id -g)" \
  -e SOURCE_DATE_EPOCH="$epoch" -e BUILDROOT_VERSION="$version" -e BUILD_IMAGE_ID="$image" \
  -v "$repo:/work:ro" -v "$cache:/build" -v "$cache/toolchain:/opt/plaf203-toolchain" \
  "${enrollment[@]}" "$image" \
  sh -ec '
    umask 077
    trap '\''chown -R "$OWNER_UID:$OWNER_GID" /build/minimal-output'\'' EXIT
    /opt/plaf203-toolchain/relocate-sdk.sh >/dev/null
    # A failed build must not leave a previous receipt looking current.
    rm -f /build/minimal-output/images/build-receipt.json
    make -s -C "/build/buildroot/buildroot-$BUILDROOT_VERSION" O=/build/minimal-output \
      BR2_EXTERNAL=/work/firmware/plaf203 plaf203_qemu_defconfig
    make -s -C "/build/buildroot/buildroot-$BUILDROOT_VERSION" O=/build/minimal-output -j4
    make -s -C "/build/buildroot/buildroot-$BUILDROOT_VERSION" O=/build/minimal-output \
      show-info > /build/minimal-output/packages.json
    python3 /work/scripts/plaf203_build_receipt.py record --repo /work \
      --buildroot "/build/buildroot/buildroot-$BUILDROOT_VERSION" --output /build/minimal-output \
      --image-id "$BUILD_IMAGE_ID" --epoch "$SOURCE_DATE_EPOCH"
  '
