#!/usr/bin/env bash
# Builds a SIGNED apt repository that has the same layout as download.cloudstack.org/ubuntu
# (dists/noble/4.22/..., pool, Release/InRelease, release.asc) with MOCK cloudstack-* packages whose Depends mirror
# the real ones, plus a tiny fake systemvm template. Serves it on 127.0.0.1:8090. For testing the BUILDER only.
set -Eeuo pipefail
W=/tmp/mock-cs; rm -rf "$W"; mkdir -p "$W"/{site/ubuntu/dists/noble/4.22/binary-amd64,site/ubuntu/dists/noble/4.22/pool,site/systemvm/4.22,pkgs,gnupg}; chmod 700 "$W/gnupg"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends gnupg apt-utils dpkg-dev bzip2 >/dev/null
V=4.22.1.1
mk() { local n=$1 d="$W/pkgs/$1"; mkdir -p "$d/DEBIAN"
  printf 'Package: %s\nVersion: %s\nArchitecture: all\nMaintainer: test <t@t>\nDepends: %s\nDescription: MOCK %s (builder test only)\n' "$n" "$V" "$2" "$n" > "$d/DEBIAN/control"
  dpkg-deb --build "$d" "$W/site/ubuntu/dists/noble/4.22/pool/${n}_${V}_all.deb" >/dev/null; }
mk cloudstack-common     "genisoimage, nfs-common, python3-pip, python3-distutils | python3-distutils-extra, python3-netaddr, uuid-runtime"
mk cloudstack-management "cloudstack-common (= $V), openjdk-17-jre-headless | java17-runtime-headless, mysql-client | mariadb-client, python3-pymysql, iproute2"
mk cloudstack-agent      "cloudstack-common (= $V), qemu-kvm, libvirt-daemon-system, libvirt-clients, openjdk-17-jre-headless, ethtool, iproute2"
mk cloudstack-usage      "cloudstack-common (= $V), openjdk-17-jre-headless"
cd "$W/site/ubuntu"
dpkg-scanpackages -m dists/noble/4.22/pool /dev/null > dists/noble/4.22/binary-amd64/Packages 2>/dev/null
gzip -9kf dists/noble/4.22/binary-amd64/Packages
cat > "$W/ftp.conf" <<EOT
APT::FTPArchive::Release::Origin "mock-cloudstack";
APT::FTPArchive::Release::Suite "noble";
APT::FTPArchive::Release::Codename "noble";
APT::FTPArchive::Release::Architectures "amd64 all";
APT::FTPArchive::Release::Components "4.22";
EOT
apt-ftparchive -c "$W/ftp.conf" release dists/noble > dists/noble/Release
export GNUPGHOME="$W/gnupg"
cat > "$W/keyparams" <<EOK
%no-protection
Key-Type: EDDSA
Key-Curve: ed25519
Key-Usage: sign
Name-Real: Mock CloudStack
Name-Email: mock@example.test
Expire-Date: 0
%commit
EOK
timeout 90 gpg --batch --gen-key "$W/keyparams" 2>/dev/null
gpg --batch --yes --clearsign -o dists/noble/InRelease dists/noble/Release
gpg --batch --yes -abs -o dists/noble/Release.gpg dists/noble/Release
gpg --export --armor > "$W/site/release.asc"
head -c 200000 /dev/urandom | bzip2 > "$W/site/systemvm/4.22/systemvmtemplate-4.22.0-x86_64-kvm.qcow2.bz2"
pkill -f "http.server 8090" 2>/dev/null || true
(cd "$W/site" && setsid nohup python3 -m http.server 8090 --bind 127.0.0.1 > "$W/http.log" 2>&1 < /dev/null &)
sleep 1; curl -fs http://127.0.0.1:8090/release.asc | head -1; echo "mock CloudStack repo up on :8090"
