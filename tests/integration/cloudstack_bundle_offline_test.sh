#!/usr/bin/env bash
# Installs EVERYTHING the CloudStack nodes need from dist/cloudstack-bundle.zip into a fresh minimal Ubuntu 24.04
# chroot that has no network (own netns, loopback only) and checks the binaries are there.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BASE_CHROOT="${BASE_CHROOT:-/tmp/chroot-master}"; RUN=/tmp/chroot-cs
cleanup() { umount "$RUN/proc" "$RUN/dev" 2>/dev/null || true; }; trap cleanup EXIT
cleanup; rm -rf "$RUN"; cp -a "$BASE_CHROOT" "$RUN"
mount -t proc proc "$RUN/proc"; mount --bind /dev "$RUN/dev"
: > "$RUN/etc/apt/sources.list"; rm -f "$RUN"/etc/apt/sources.list.d/*
printf '#!/bin/sh\nexit 101\n' > "$RUN/usr/sbin/policy-rc.d"; chmod +x "$RUN/usr/sbin/policy-rc.d"
mkdir -p "$RUN/opt"; unzip -q "$ROOT/dist/cloudstack-bundle.zip" -d "$RUN/opt"
echo "bundle: $(du -sh "$RUN/opt/airgap/www/cloudstack" | cut -f1), $(ls "$RUN/opt/airgap/www/cloudstack/deb"/*.deb | wc -l) debs, VERSION=$(cat "$RUN/opt/airgap/www/cloudstack/VERSION")"
ls -la "$RUN/opt/airgap/www/cloudstack/systemvm" | tail -1
echo "deb [trusted=yes] file:/opt/airgap/www/cloudstack/deb ./" > "$RUN/etc/apt/sources.list.d/airgap.list"
PKGS="$(grep -vE '^\s*(#|$)' "$ROOT/online/lists/cloudstack.pkgs" | tr '\n' ' ')"
unshare -n bash -c "ip link set lo up; chroot '$RUN' /bin/bash -c '
  export DEBIAN_FRONTEND=noninteractive PATH=/usr/sbin:/usr/bin:/sbin:/bin
  apt-get update -qq 2>&1 | tail -2
  apt-get install -y --no-install-recommends $PKGS > /tmp/install.log 2>&1 || { tail -25 /tmp/install.log; echo INSTALL-FAILED; exit 1; }
  for b in mysqld keepalived haproxy exportfs libvirtd qemu-system-x86_64 cloudstack-setup-databases cloudstack-setup-agent ovs-vsctl; do
    printf \"%-28s\" \$b; command -v \$b || echo MISSING; done
  dpkg -l | awk \"/^ii/{n++} END{print n\\\" packages installed\\\"}\"
  dpkg -s cloudstack-management | grep -E \"^(Version|Status)\"
'"
echo "CLOUDSTACK BUNDLE OFFLINE INSTALL DONE"
