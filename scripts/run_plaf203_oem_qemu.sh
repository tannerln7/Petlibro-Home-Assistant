#!/usr/bin/env bash
# Human/automation entry point for the same isolated OEM runner.
set -euo pipefail
umask 077
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
recon="${PLAF203_RECONSTRUCTION_ROOT:-$(dirname "$repo")/plaf203-firmware-reconstruction}"
cache="${PLAF203_CACHE_ROOT:-/srv/cache/plaf203-bringup-research}"
inventory="$recon/provenance/replacement-platform-research/device-observations.json"
[[ -f "$inventory" ]]
backup="$(python3 - "$recon" "$inventory" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve()
path=(root/json.loads(Path(sys.argv[2]).read_text())['primary_oem_flash_backup']['repository_copy']).resolve()
assert path.is_relative_to(root/'inputs') and (path/'plaf203-flash-32m.bin').is_file()
print(path)
PY
)"
cache="$(realpath "$cache")"
[[ -x "$cache/qemu-bench/qemu-src/build/qemu-system-arm" ]]
private="$cache/oem-reference-private"
install -d -m 0700 "$private"
setfacl -b -k "$private"
chmod 0700 "$private"
chmod g-s "$private"
terminal=()
for option in "$@"; do
  if [[ "$option" == --interactive ]]; then terminal=(-it); fi
done
exec docker run --rm "${terminal[@]}" --network none --user "$(id -u):$(id -g)" \
  --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 64 --memory 512m --cpus 1 --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=16m \
  --ulimit core=0 --ulimit fsize=134217728 \
  -v "$inventory:/evidence/provenance/replacement-platform-research/device-observations.json:ro" \
  -v "$backup:/evidence/$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["primary_oem_flash_backup"]["repository_copy"])' "$inventory"):ro" \
  -v "$repo/scripts:/work/scripts:ro" \
  -v "$cache/qemu-bench/qemu-src/build:$cache/qemu-bench/qemu-src/build:ro" \
  -v "$private:/experiments:rw" \
  "${PLAF203_BUILD_IMAGE:-plaf203-qemu-bringup:review}" \
  python3 /work/scripts/run_plaf203_oem_qemu.py \
    --reconstruction-root /evidence --cache-root "$cache" --work-root /experiments "$@"
