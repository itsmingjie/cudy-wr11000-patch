#!/bin/sh
# Restore opkg on Cudy WR11000 stock firmware.
#
# This installs only the opkg executable/key helper from official OpenWrt
# 21.02.7 for arm_cortex-a7_neon-vfpv4, preserves Cudy's package database,
# and configures public userland feeds. It intentionally does not add a
# target/kmods feed because Cudy's ipq53xx/ipq53xx_32 target is not available
# in official 21.02 package archives.

set -eu

RELEASE="21.02.7"
ARCH="arm_cortex-a7_neon-vfpv4"
OPKG_FILE="opkg_2021-06-13-1bf042dd-2_${ARCH}.ipk"
OPKG_URL="https://downloads.openwrt.org/releases/${RELEASE}/packages/${ARCH}/base/${OPKG_FILE}"
OPKG_SHA256="8e6cb8140cc260c5345d0b7de3f4b46438834a1b67b44e21ef7c73caf0b50f4a"
BACKUP_DIR="/root/opkg-restore-backup-$(date +%Y%m%d-%H%M%S)"
WORK="/tmp/opkg-restore"

echo "[*] Backing up package manager state to $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
cp -a /etc/opkg "$BACKUP_DIR/etc-opkg" 2>/dev/null || true
cp -a /etc/opkg.conf "$BACKUP_DIR/opkg.conf" 2>/dev/null || true
cp -a /usr/lib/opkg "$BACKUP_DIR/usr-lib-opkg" 2>/dev/null || true
cp -a /bin/opkg "$BACKUP_DIR/opkg.bin" 2>/dev/null || true
cp -a /usr/sbin/opkg-key "$BACKUP_DIR/opkg-key" 2>/dev/null || true

echo "[*] Downloading official OpenWrt opkg package"
rm -rf "$WORK"
mkdir -p "$WORK/ipk" "$WORK/root"
if command -v curl >/dev/null 2>&1; then
	curl -L -f -o "$WORK/$OPKG_FILE" "$OPKG_URL"
else
	wget -O "$WORK/$OPKG_FILE" "$OPKG_URL"
fi

echo "${OPKG_SHA256}  $WORK/$OPKG_FILE" | sha256sum -c -

echo "[*] Extracting opkg payload"
tar -xzf "$WORK/$OPKG_FILE" -C "$WORK/ipk"
tar -xzf "$WORK/ipk/data.tar.gz" -C "$WORK/root"

echo "[*] Installing opkg binary and key helper"
mkdir -p /usr/sbin /lib/upgrade/keep.d /etc/opkg /etc/opkg/keys /usr/lib/opkg/info /usr/lib/opkg/lists /var/opkg-lists
cp "$WORK/root/bin/opkg" /bin/opkg
chmod 0755 /bin/opkg
cp "$WORK/root/usr/sbin/opkg-key" /usr/sbin/opkg-key
chmod 0755 /usr/sbin/opkg-key
cp -a "$WORK/root/lib/upgrade/keep.d/opkg" /lib/upgrade/keep.d/opkg 2>/dev/null || true

echo "[*] Writing opkg.conf"
cat > /etc/opkg.conf <<'EOF'
dest root /
dest ram /tmp
lists_dir ext /var/opkg-lists
option overlay_root /overlay
option check_signature
EOF

echo "[*] Writing userland feeds for OpenWrt 21.02.7"
cat > /etc/opkg/distfeeds.conf <<EOF
# Official OpenWrt userland feeds. These may work for pure userland packages.
# Kernel packages are intentionally omitted: Cudy WR11000 uses a vendor ipq53xx
# target/kernel ABI that official OpenWrt 21.02.7 does not publish.
src/gz openwrt_base https://downloads.openwrt.org/releases/${RELEASE}/packages/${ARCH}/base
src/gz openwrt_packages https://downloads.openwrt.org/releases/${RELEASE}/packages/${ARCH}/packages
src/gz openwrt_luci https://downloads.openwrt.org/releases/${RELEASE}/packages/${ARCH}/luci
src/gz openwrt_routing https://downloads.openwrt.org/releases/${RELEASE}/packages/${ARCH}/routing
src/gz openwrt_telephony https://downloads.openwrt.org/releases/${RELEASE}/packages/${ARCH}/telephony
EOF

touch /etc/opkg/customfeeds.conf

echo "[*] opkg version:"
/bin/opkg --version

echo "[*] Updating package lists"
/bin/opkg update

echo
echo "Done. Backup: $BACKUP_DIR"
echo
echo "Use userland packages conservatively. Avoid kmod-* packages unless you build"
echo "them for this exact Cudy kernel/ABI."
echo
echo "Examples:"
echo "  opkg list | grep qos"
echo "  opkg install luci-app-nft-qos"
echo
echo "Do NOT blindly run:"
echo "  opkg upgrade"
