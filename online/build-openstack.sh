#!/usr/bin/env bash
# Builds openstack-bundle.zip WITHOUT Docker anywhere:
#   .deb repo for the nodes (podman, catatonit, python3-podman ...) + kolla-ansible wheels/collections
#   + every Kolla image as ONE OCI image layout (skopeo; shared layers stored once) + its image list.
# Run on an online Ubuntu 24.04 machine:   sudo NO_DOCKER=1 bash online/build-openstack.sh
# (with NO_DOCKER unset the .deb/wheel steps still use a throw-away ubuntu container - images never need Docker)
source "$(dirname "$0")/lib.sh"
preflight
command -v skopeo >/dev/null || { [ "$EUID" -eq 0 ] && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq skopeo >/dev/null; } || true
command -v skopeo >/dev/null || die "skopeo is required to download the Kolla images (apt-get install skopeo)"
STAGE="$WORK/stage-openstack"; rm -rf "$STAGE" "$WORK/out/openstack"; mkdir -p "$STAGE/airgap/www/openstack/images"

log "openstack 1/4: .deb repo (podman, catatonit, python3-podman, lvm2 ...)"
run_in_ubuntu build-debs.sh REPO_NAME=openstack PKG_LISTS=openstack.pkgs
log "openstack 2/4: kolla-ansible wheelhouse + ansible-galaxy collections"
run_in_ubuntu build-wheels.sh REPO_NAME=openstack REQ_FILE=openstack-requirements.txt WITH_KOLLA=1

log "openstack 3/4: Kolla images -> OCI layout with skopeo ($KOLLA_REGISTRY, tag $KOLLA_TAG)"
IMG="$STAGE/airgap/www/openstack/images"; OCI="$IMG/kolla-oci"; : > "$IMG/images.txt"; : > "$WORK/missing-images.txt"
while read -r name; do
  [[ -z "$name" || "$name" == \#* ]] && continue
  ref="$KOLLA_REGISTRY/$name:$KOLLA_TAG"
  ok=0
  for try in 1 2 3; do
    if skopeo copy -q --override-os linux --override-arch amd64 "docker://$ref" "oci:$OCI:openstack.kolla/$name:$KOLLA_TAG" >/dev/null 2>&1; then ok=1; break; fi
    sleep $((try * 5))
  done
  if [ "$ok" = 1 ]; then echo "$ref" >> "$IMG/images.txt"; log "  ok   $ref"
  else echo "$ref" >> "$WORK/missing-images.txt"; log "  MISS $ref"; fi
done < "$ROOT/online/lists/openstack-images.txt"
cp "$WORK/missing-images.txt" "$IMG/missing-images.txt"
(cd "$IMG" && find kolla-oci -type f -print0 | sort -z | xargs -0 sha256sum > kolla-oci.sha256)
[ -s "$WORK/missing-images.txt" ] && log "WARNING: $(wc -l < "$WORK/missing-images.txt") image(s) missing, see images/missing-images.txt - fix names/tag before shipping"

log "openstack 4/4: packing"
cp -a "$WORK/out/openstack/deb" "$WORK/out/openstack/wheels" "$WORK/out/openstack/collections" "$STAGE/airgap/www/openstack/"
cp "$ROOT/online/lists/openstack-requirements.txt" "$STAGE/airgap/www/openstack/requirements.txt"
make_zip "openstack-bundle.zip" "$STAGE"
[ ! -s "$WORK/missing-images.txt" ]
