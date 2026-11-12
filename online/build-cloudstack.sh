#!/usr/bin/env bash
# Builds cloudstack-bundle.zip : CloudStack 4.22 + MySQL + HA/LB + KVM debs, SystemVM template.
source "$(dirname "$0")/lib.sh"
preflight
STAGE="$WORK/stage-cloudstack"; rm -rf "$STAGE" "$WORK/out/cloudstack"; mkdir -p "$STAGE/airgap/www/cloudstack/systemvm"

log "cloudstack 1/2: .deb repo (CloudStack ${CLOUDSTACK_SERIES}, MySQL, keepalived, haproxy, KVM, NFS)"
run_in_ubuntu build-debs.sh REPO_NAME=cloudstack PKG_LISTS=cloudstack.pkgs WITH_CLOUDSTACK_REPO=1

log "cloudstack 2/2: SystemVM template (KVM)"
f="$STAGE/airgap/www/cloudstack/systemvm/$(basename "$SYSTEMVM_KVM_URL")"
curl -fL --retry 5 -o "$f" "$SYSTEMVM_KVM_URL"
bzip2 -t "$f" || die "systemvm template is corrupt"

cp -a "$WORK/out/cloudstack/deb" "$STAGE/airgap/www/cloudstack/deb"
cp "$WORK/out/cloudstack/package-list.txt" "$STAGE/airgap/www/cloudstack/"
ver=$(ls "$STAGE"/airgap/www/cloudstack/deb/cloudstack-management_*.deb 2>/dev/null | head -1 | sed 's/.*management_//;s/_all.deb//')
echo "cloudstack-management=${ver:-UNKNOWN}" > "$STAGE/airgap/www/cloudstack/VERSION"
log "resolved CloudStack package version: ${ver:-UNKNOWN}"
make_zip "cloudstack-bundle.zip" "$STAGE"
