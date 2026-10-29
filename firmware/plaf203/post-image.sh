#!/bin/sh
set -eu
# Vendor uImage rules append their own default DTB. Build zImage/our DTB via
# Buildroot instead, then package exactly one tree with the established header.
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:?reproducible build epoch required}"
cat "$1/zImage" "$1/plaf203-qemu.dtb" > "$1/zImage+dtb"
mkimage -A arm -O linux -T kernel -C none -a 0x40008000 -e 0x40008000 \
    -n Linux-4.9.37-plaf203-qemu -d "$1/zImage+dtb" "$1/uImage" >/dev/null
