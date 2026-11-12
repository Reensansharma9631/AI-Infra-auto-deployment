#!/usr/bin/env bash
# Runs INSIDE ubuntu:24.04 (same python3.12 as the target hosts). Builds an offline wheelhouse.
set -Eeuo pipefail
: "${REPO_NAME:?}" "${REQ_FILE:?}"
OUT="/out/$REPO_NAME/wheels"; mkdir -p "$OUT"
apt-get update -qq
apt-get install -y -qq --no-install-recommends python3 python3-pip python3-venv python3-dev build-essential \
  libffi-dev libssl-dev git ca-certificates >/dev/null
python3 -m venv /tmp/v
/tmp/v/bin/pip install -q -U pip wheel setuptools
/tmp/v/bin/pip wheel -q -w "$OUT" pip setuptools wheel -r "/lists/$REQ_FILE"
echo "[build-wheels] $(ls "$OUT" | wc -l) wheels in $OUT"

if [ "${WITH_KOLLA:-0}" = "1" ]; then
  /tmp/v/bin/pip install -q --no-index --find-links "$OUT" -r "/lists/$REQ_FILE"
  REQ_YML=$(find /tmp/v -name requirements.yml -path '*kolla-ansible*' | head -1)
  [ -n "$REQ_YML" ] || { echo "kolla-ansible requirements.yml not found"; exit 1; }
  SHARE=$(dirname "$REQ_YML")
  echo "[build-wheels] kolla-ansible $(/tmp/v/bin/pip show kolla-ansible | awk '/^Version/{print $2}'): validating image names"
  /tmp/v/bin/python /inner/validate-kolla-images.py "$SHARE" /lists/openstack-images.txt

  # 1) the 7 galaxy collections Kolla needs must come bundled with the 'ansible' wheel (no galaxy access offline)
  for c in ansible.netcommon ansible.posix ansible.utils community.crypto community.general community.docker containers.podman; do
    /tmp/v/bin/ansible-galaxy collection list "$c" 2>/dev/null | grep -q "$c" || { echo "collection $c missing from the ansible package"; exit 1; }
  done
  # 2) the git collection (galaxy cannot DOWNLOAD git sources): build a tarball from the pinned branch
  REF=$(awk '/ansible-collection-kolla/{f=1} f&&/version:/{print $2; exit}' "$SHARE/requirements.yml")
  mkdir -p "/out/$REPO_NAME/collections"; rm -rf /tmp/ack
  for url in https://opendev.org/openstack/ansible-collection-kolla https://github.com/openstack/ansible-collection-kolla; do
    git clone -q --depth 1 -b "$REF" "$url" /tmp/ack 2>/dev/null && { echo "[build-wheels] collection from $url@$REF"; break; }
  done
  [ -d /tmp/ack ] || { echo "cannot clone ansible-collection-kolla@$REF"; exit 1; }
  (cd /tmp/ack && /tmp/v/bin/ansible-galaxy collection build --force --output-path "/out/$REPO_NAME/collections")
  ls -la "/out/$REPO_NAME/collections"
fi
