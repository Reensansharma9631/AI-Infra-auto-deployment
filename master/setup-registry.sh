#!/usr/bin/env bash
# Local container registry on the master for Kolla images (no Docker needed on the master):
#   Ubuntu's `docker-registry` (distribution) + `skopeo`, installed from the openstack bundle's apt repo.
# usage: sudo bash setup-registry.sh [images.tar images.txt]   (images default to /opt/airgap/www/openstack/images/)
set -Eeuo pipefail
AIRGAP=${AIRGAP:-/opt/airgap}; REG_PORT=${REG_PORT:-5000}; REG_DIR=${REG_DIR:-/var/lib/airgap-registry}
IMG_DIR=$AIRGAP/www/openstack/images
TAR=${1:-$IMG_DIR/kolla-images.tar}; LIST=${2:-$IMG_DIR/images.txt}; OCI=${OCI:-$IMG_DIR/kolla-oci}
log() { printf '\033[1;34m[registry]\033[0m %s\n' "$*"; }
[ "$EUID" -eq 0 ] || { echo "run as root"; exit 1; }
[ -f "$AIRGAP/www/openstack/deb/Packages.gz" ] || { echo "openstack bundle not unpacked ($AIRGAP/www/openstack/deb)"; exit 1; }

if ! command -v docker-registry >/dev/null || ! command -v skopeo >/dev/null; then
  echo "deb [trusted=yes] file:$AIRGAP/www/openstack/deb ./" > /etc/apt/sources.list.d/airgap-openstack-local.list
  O=(-o Dir::Etc::sourcelist=/etc/apt/sources.list.d/airgap-openstack-local.list -o Dir::Etc::sourceparts=/dev/null -o APT::Get::List-Cleanup=0)
  apt-get "${O[@]}" update -qq
  DEBIAN_FRONTEND=noninteractive apt-get "${O[@]}" install -y -qq --no-install-recommends docker-registry skopeo
fi
mkdir -p "$REG_DIR" /etc/docker/registry
# Ubuntu's docker-registry runs as user docker-registry: it must own its storage (root-owned -> HTTP 500 'permission denied')
id docker-registry >/dev/null 2>&1 && chown -R docker-registry:docker-registry "$REG_DIR"
cat > /etc/docker/registry/config.yml <<EOT
version: 0.1
log: {level: info}
storage:
  filesystem: {rootdirectory: $REG_DIR}
  delete: {enabled: true}
http:
  addr: :$REG_PORT
EOT
if [ -d /run/systemd/system ] && [ -z "${NO_SYSTEMD:-}" ]; then systemctl enable --now docker-registry && systemctl restart docker-registry
else nohup docker-registry serve /etc/docker/registry/config.yml > /var/log/airgap-registry.log 2>&1 & fi
for _ in $(seq 1 30); do curl -fs "http://127.0.0.1:$REG_PORT/v2/" >/dev/null && break; sleep 1; done
curl -fs "http://127.0.0.1:$REG_PORT/v2/" >/dev/null || { echo "registry did not start"; exit 1; }
log "registry up on :$REG_PORT"

[ -f "$LIST" ] && { [ -d "$OCI" ] || [ -f "$TAR" ]; } || { log "no images given/found - registry is empty (load later with this script)"; exit 0; }
if [ -d "$OCI" ] && [ -f "$IMG_DIR/kolla-oci.sha256" ]; then
  log "verifying the image layout (sha256)"; (cd "$IMG_DIR" && sha256sum --quiet -c kolla-oci.sha256) || { echo "image layout corrupted"; exit 1; }
fi
n=0; fail=0
while read -r ref; do
  [ -z "$ref" ] && continue
  dest="127.0.0.1:$REG_PORT/${ref#*/}"            # quay.io/openstack.kolla/nova-api:TAG -> openstack.kolla/nova-api:TAG
  if [ -d "$OCI" ]; then src="oci:$OCI:${ref#*/}"; else src="docker-archive:$TAR:$ref"; fi    # OCI layout (no Docker) or an older docker-save tar
  if skopeo copy -q --dest-tls-verify=false "$src" "docker://$dest"; then n=$((n+1)); else echo "FAILED: $ref"; fail=$((fail+1)); fi
done < "$LIST"
log "loaded $n image(s), $fail failed"
curl -s "http://127.0.0.1:$REG_PORT/v2/_catalog" | head -c 600; echo
[ "$fail" -eq 0 ]
