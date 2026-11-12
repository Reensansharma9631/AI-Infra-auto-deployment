#!/usr/bin/env bash
# Builds openstack-bundle.zip : docker + kolla-ansible wheels/collections + Kolla images + local registry image.
source "$(dirname "$0")/lib.sh"
preflight
STAGE="$WORK/stage-openstack"; rm -rf "$STAGE" "$WORK/out/openstack"; mkdir -p "$STAGE/airgap/www/openstack/images"

log "openstack 1/4: .deb repo (docker.io, lvm2 ...)"
run_in_ubuntu build-debs.sh REPO_NAME=openstack PKG_LISTS=openstack.pkgs
log "openstack 2/4: kolla-ansible wheelhouse + ansible-galaxy collections"
run_in_ubuntu build-wheels.sh REPO_NAME=openstack REQ_FILE=openstack-requirements.txt WITH_KOLLA=1

log "openstack 3/4: pulling Kolla images ($KOLLA_REGISTRY, tag $KOLLA_TAG)"
IMG="$STAGE/airgap/www/openstack/images"; : > "$IMG/images.txt"; : > "$WORK/missing-images.txt"
while read -r name; do
  [[ -z "$name" || "$name" == \#* ]] && continue
  ref="$KOLLA_REGISTRY/$name:$KOLLA_TAG"
  if docker pull -q "$ref" >/dev/null 2>&1; then echo "$ref" >> "$IMG/images.txt"; log "  ok   $ref"
  else echo "$ref" >> "$WORK/missing-images.txt"; log "  MISS $ref"; fi
done < "$ROOT/online/lists/openstack-images.txt"
# (the master runs Ubuntu's docker-registry package from the apt repo - no registry image needed)

log "openstack 4/4: docker save (this is the big one)"
# shellcheck disable=SC2046
docker save -o "$IMG/kolla-images.tar" $(cat "$IMG/images.txt")
cp "$WORK/missing-images.txt" "$IMG/missing-images.txt"
[ -s "$WORK/missing-images.txt" ] && log "WARNING: $(wc -l < "$WORK/missing-images.txt") image(s) missing, see work/missing-images.txt - fix names/tag before shipping"

cp -a "$WORK/out/openstack/deb" "$WORK/out/openstack/wheels" "$WORK/out/openstack/collections" "$STAGE/airgap/www/openstack/"
cp "$ROOT/online/lists/openstack-requirements.txt" "$STAGE/airgap/www/openstack/requirements.txt"
make_zip "openstack-bundle.zip" "$STAGE"
