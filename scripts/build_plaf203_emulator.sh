#!/usr/bin/env bash
# Reuse pinned qemu-bench integration, preserving existing generated model files.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cache="$(realpath "${1:?Usage: build_plaf203_emulator.sh CACHE_ROOT [--initialize]}")"
initialize=0
if [[ "${2:-}" == --initialize ]]; then initialize=1; elif [[ -n "${2:-}" ]]; then exit 2; fi
bench="$cache/qemu-bench"
[[ "$(git -C "$bench" rev-parse HEAD)" == b011673b31884683a534a3f3e1a2b080d5a99f19 ]]
[[ "$(git -C "$bench/qemu-src" rev-parse HEAD)" == 698104725efad4b29079d857dfdebbd804e34c99 ]]
git -C "$bench" diff --quiet -- qemu
if [[ "$initialize" == 1 ]]; then
  # Initial integration is destructive to a generated source tree: permit only
  # a clean, freshly cloned QEMU checkout, never the working research cache.
  [[ -z "$(git -C "$bench/qemu-src" status --porcelain --untracked-files=all)" ]]
  [[ ! -e "$bench/qemu-src/build" ]]
fi
docker run --rm -i --network none --user "$(id -u):$(id -g)" \
  --cap-drop ALL --security-opt no-new-privileges \
  -v "$bench:/work" -v "$repo:/recipe:ro" -w /work/qemu-src \
  "${PLAF203_BUILD_IMAGE:-plaf203-qemu-bringup:review}" bash -s -- "$initialize" <<'BUILD'
set -euo pipefail
initialize="$1"
python3 - <<'CHECK'
import hashlib,json
from pathlib import Path
lock=json.loads(Path('/recipe/firmware/plaf203/qemu/source-lock.json').read_text())
for file,key in [('/work/qemu/setup.sh','upstream_setup_sha256'),('/work/qemu/hw/misc/hisi-fmc.c','upstream_fmc_sha256')]:
    assert hashlib.sha256(Path(file).read_bytes()).hexdigest()==lock[key],file
CHECK
if [[ "$initialize" == 1 ]]; then
  # Execute the pinned upstream setup's integration portion, not a second copy
  # of its hundreds of model-copy/Kconfig/Meson changes or its all-target build.
  prefix="$(awk '/^#.*5\. Build/{exit} {print}' /work/qemu/setup.sh)"
  [[ "$prefix" != "$(cat /work/qemu/setup.sh)" ]]
  bash -c "$prefix" /work/qemu/setup.sh
fi
python3 - <<'PATCH'
import hashlib,json,subprocess
from pathlib import Path
directory=Path('/recipe/firmware/plaf203/qemu')
lock=json.loads((directory/'source-lock.json').read_text())
patch=directory/lock['patch']; source=Path('hw/misc/hisi-fmc.c')
assert hashlib.sha256(patch.read_bytes()).hexdigest()==lock['patch_sha256']
digest=hashlib.sha256(source.read_bytes()).hexdigest()
if digest==lock['upstream_fmc_sha256']:
    subprocess.run(['patch','--batch','--forward','-p1','-i',str(patch)],check=True)
elif digest!=lock['patched_fmc_sha256']:
    raise SystemExit('Unknown FMC source changes: preserve and review, not overwrite')
assert hashlib.sha256(source.read_bytes()).hexdigest()==lock['patched_fmc_sha256']
PATCH
if [[ ! -f build/build.ninja ]]; then
  mkdir -p build
  (cd build && ../configure --target-list=arm-softmmu --disable-docs)
fi
ninja -C build -j4 qemu-system-arm
sha256sum build/qemu-system-arm
BUILD
