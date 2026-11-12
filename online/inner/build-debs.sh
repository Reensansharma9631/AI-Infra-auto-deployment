#!/usr/bin/env bash
# Runs INSIDE a clean ubuntu:24.04 container. Resolves the full dependency closure of the
# packages in $PKG_LISTS and downloads every .deb, then builds a flat apt repo (Packages.gz).
set -Eeuo pipefail
: "${REPO_NAME:?}" "${PKG_LISTS:?}"
OUT="/out/$REPO_NAME/deb"; mkdir -p "$OUT"; rm -f "$OUT"/*.deb

apt-get update -qq
apt-get install -y -qq --no-install-recommends ca-certificates curl gnupg dpkg-dev >/dev/null

if [ "${WITH_CLOUDSTACK_REPO:-0}" = "1" ]; then
  mkdir -p /etc/apt/keyrings
  curl -fsSL "$CLOUDSTACK_GPG_URL" | gpg --batch --yes --dearmor -o /etc/apt/keyrings/cloudstack.gpg
  echo "deb [signed-by=/etc/apt/keyrings/cloudstack.gpg] $CLOUDSTACK_APT_URL $UBUNTU_CODENAME $CLOUDSTACK_SERIES" \
    > /etc/apt/sources.list.d/cloudstack.list
  apt-get update -qq
fi

PKGS=""
for l in $PKG_LISTS; do PKGS="$PKGS $(grep -vE '^\s*(#|$)' "/lists/$l" | tr '\n' ' ')"; done
echo "[build-debs] seed packages:$PKGS"

# full closure (Depends + PreDepends only, no recommends), then keep only names with a real candidate
apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks \
  --no-replaces --no-enhances $PKGS 2>/dev/null | grep '^[a-zA-Z0-9]' | grep -v ':' | sort -u > /tmp/closure.txt
: > /tmp/names.txt
while read -r p; do
  c=$(apt-cache policy "$p" 2>/dev/null | awk '/Candidate:/{print $2}')
  [ -n "$c" ] && [ "$c" != "(none)" ] && echo "$p" >> /tmp/names.txt
done < /tmp/closure.txt
echo "[build-debs] downloading $(wc -l < /tmp/names.txt) packages"

cd "$OUT"
xargs -a /tmp/names.txt -n 50 apt-get -o APT::Sandbox::User=root download -qq
for p in $PKGS; do ls "$OUT"/"${p}"_*.deb >/dev/null 2>&1 || echo "[build-debs] WARNING: seed package not downloaded: $p"; done

dpkg-scanpackages --multiversion . /dev/null 2>/dev/null | gzip -9c > Packages.gz
cp -f /tmp/names.txt "$OUT/../package-list.txt"
echo "[build-debs] repo ready: $OUT ($(ls *.deb | wc -l) debs, $(du -sh . | cut -f1))"
