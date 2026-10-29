#!/bin/sh
set -eu
target="$1"
mkdir -p "$target/state" "$target/etc/plaf203"
mkdir -p "$target/root"
chmod 0700 "$target/root"
ln -sfn /run/plaf203-ssh "$target/root/.ssh"
# Replace upstream auto-key/volatile-seed policy with the board lifecycle below.
rm -f "$target/etc/init.d/S50dropbear" "$target/etc/init.d/S01seedrng" "$target/etc/init.d/S20seedrng"
cp "$BR2_EXTERNAL_PLAF203_PATH/S50sshd" "$target/etc/init.d/S50sshd"
chmod 0755 "$target/init" "$target/etc/init.d/S50sshd" "$target/etc/init.d/S20state"
chmod 0755 "$target/etc/init.d/S40runtime" "$target/usr/libexec/plaf203-runtime-supervise"
sed -i '\|::respawn:/usr/libexec/plaf203-runtime-supervise|d' "$target/etc/inittab"
printf '\n::respawn:/usr/libexec/plaf203-runtime-supervise\n' >> "$target/etc/inittab"
rm -f "$target/etc/plaf203/enrollment.pub" "$target/etc/plaf203/test.seed"
if [ -n "${PLAF203_ENROLLMENT_PUBKEY:-}" ]; then
    ssh-keygen -l -f "$PLAF203_ENROLLMENT_PUBKEY" >/dev/null
    test "$(awk '{print $1}' "$PLAF203_ENROLLMENT_PUBKEY")" = ssh-ed25519
    cp "$PLAF203_ENROLLMENT_PUBKEY" "$target/etc/plaf203/enrollment.pub"
fi
# A fresh, private, per-experiment seed only; never a product image seed.
if [ -n "${PLAF203_TEST_SEED:-}" ]; then
    test "$(wc -c < "$PLAF203_TEST_SEED")" -eq 32
    cp "$PLAF203_TEST_SEED" "$target/etc/plaf203/test.seed"
    chmod 0400 "$target/etc/plaf203/test.seed"
fi
