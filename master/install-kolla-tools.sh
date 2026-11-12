#!/usr/bin/env bash
# Offline Kolla-Ansible control tools on the master: venv from the wheelhouse + openstack.kolla collection +
# /etc/kolla/{globals,passwords}.yml with generated passwords. Needs the openstack bundle unpacked.
set -Eeuo pipefail
AIRGAP=${AIRGAP:-/opt/airgap}; W=$AIRGAP/www/openstack; ETC_KOLLA=${ETC_KOLLA:-/etc/kolla}
V=$AIRGAP/venv-kolla; log() { printf '\033[1;34m[kolla-tools]\033[0m %s\n' "$*"; }
[ -d "$W/wheels" ] || { echo "openstack bundle not unpacked ($W/wheels missing)"; exit 1; }
python3 -m venv "$V"
"$V/bin/pip" install -q --no-index --find-links "$W/wheels" pip setuptools wheel -r "$W/requirements.txt"
mkdir -p "$AIRGAP/collections"
"$V/bin/ansible-galaxy" collection install "$W"/collections/openstack-kolla-*.tar.gz -p "$AIRGAP/collections" --offline >/dev/null
SHARE=$("$V/bin/python" -c 'import sys,os;print(os.path.join(sys.prefix,"share","kolla-ansible"))')
mkdir -p "$ETC_KOLLA"
[ -f "$ETC_KOLLA/globals.yml" ]   || cp "$SHARE/etc_examples/kolla/globals.yml" "$ETC_KOLLA/globals.yml"
[ -f "$ETC_KOLLA/passwords.yml" ] || { cp "$SHARE/etc_examples/kolla/passwords.yml" "$ETC_KOLLA/passwords.yml"; "$V/bin/kolla-genpwd" -p "$ETC_KOLLA/passwords.yml"; }
chmod 600 "$ETC_KOLLA/passwords.yml"
cat > "$AIRGAP/kolla-env.sh" <<EOT
# source this before using kolla-ansible:   source $AIRGAP/kolla-env.sh
export PATH=$V/bin:\$PATH
export ANSIBLE_COLLECTIONS_PATH=$AIRGAP/collections
EOT
log "kolla-ansible $("$V/bin/pip" show kolla-ansible | awk '/^Version/{print $2}'), $("$V/bin/ansible" --version 2>/dev/null | sed -n 1p)"
log "collections: $("$V/bin/ansible-galaxy" collection list -p "$AIRGAP/collections" 2>/dev/null | grep -c openstack.kolla) openstack.kolla; passwords: $(grep -c ':' "$ETC_KOLLA/passwords.yml") entries in $ETC_KOLLA/passwords.yml"

# The nodes install podman, catatonit, python3-podman ... with apt from the master's AutoInfra repositories only:
# publish the bundle's .debs as the 'openstack' repository (01-common then adds it to every node's sources).
DATA=$(sed -n 's/^AUTOINFRA_DATA=//p' /etc/autoinfra/autoinfra.env 2>/dev/null | tail -1); DATA=${DATA:-/var/lib/autoinfra}
if [ -d "$DATA/repo" ] && ls "$W"/deb/*.deb >/dev/null 2>&1; then
  mkdir -p "$DATA/repo/openstack/deb"
  rsync -a --delete --include='*.deb' --exclude='*' "$W/deb/" "$DATA/repo/openstack/deb/"
  chown -R autoinfra:autoinfra "$DATA/repo/openstack"
  APP=/opt/autoinfra
  su -s /bin/sh autoinfra -c "set -a; . /etc/autoinfra/autoinfra.env; set +a; cd $APP/ui && $APP/venv/bin/python -c \"
from autoinfra import create_app, packages
with create_app().app_context(): packages.reindex('openstack')\"" && log "published $(ls "$DATA"/repo/openstack/deb/*.deb | wc -l) packages as the 'openstack' repository"
fi
