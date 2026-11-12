#!/usr/bin/env bash
# ONLINE machine: builds autoinfra-master-<date>.zip = AutoInfra code + Python 3.12 wheelhouse + the master's own OS packages
# (Ubuntu 24.04 debs and/or RHEL 9 rpms). Copy the zip to the air-gapped master and run master/install-autoinfra.sh.
#   TARGETS="ubuntu rhel"   which master OS packages to include (default both; needs docker, or NO_DOCKER=1 as root on the same OS:
#                           sudo NO_DOCKER=1 TARGETS=ubuntu bash online/build-autoinfra-master.sh on Ubuntu 24.04)
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -z "${TARGETS+x}" ] && [ "${NO_DOCKER:-0}" = 1 ]; then      # without docker only this machine's own OS can be downloaded
  TARGETS="$(. /etc/os-release; [ "$ID" = ubuntu ] && echo ubuntu || echo rhel)"
fi
TARGETS="${TARGETS-ubuntu rhel}"
DIST="${DIST:-$ROOT/dist}"; STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
A="$STAGE/autoinfra"; mkdir -p "$A/wheels" "$A/os/ubuntu" "$A/os/rhel" "$DIST"
log() { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%T)" "$*"; }
need() { command -v "$1" >/dev/null || { echo "missing: $1"; exit 1; }; }
need zip; need sha256sum; need python3

log "1/3 Python 3.12 wheelhouse (manylinux, works on Ubuntu 24.04 and RHEL 9 python3.12)"
python3 -m pip download -q --dest "$A/wheels" --only-binary=:all: --implementation cp --python-version 3.12 \
  --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux_2_28_x86_64 \
  -r "$ROOT/ui/requirements.txt" pip setuptools wheel
log "   $(ls "$A/wheels" | wc -l) wheels"

pkgs() { grep -vE '^\s*(#|$)' "$ROOT/online/lists/$1" | tr '\n' ' '; }
if [[ " $TARGETS " == *" ubuntu "* ]]; then
  log "2/3 Ubuntu 24.04 master packages"
  # empty dpkg status: everything is downloaded, also packages already installed on this machine (NO_DOCKER=1 on a used host)
  INNER='set -e; apt-get update -qq; cd /out; : > /out/.empty; apt-get -o Dir::State::status=/out/.empty install -y -qq --download-only --no-install-recommends -o Dir::Cache::archives=/out $PKGS >/dev/null; rm -f /out/lock /out/.empty; rm -rf /out/partial'
  if [ "${NO_DOCKER:-0}" = 1 ]; then PKGS="$(pkgs master-ubuntu.pkgs)" bash -c "${INNER//\/out/$A/os/ubuntu}"
  else docker run --rm -e PKGS="$(pkgs master-ubuntu.pkgs)" -v "$A/os/ubuntu:/out" ubuntu:24.04 bash -c "$INNER"; fi
  log "   $(ls "$A/os/ubuntu"/*.deb 2>/dev/null | wc -l) debs"
fi
if [[ " $TARGETS " == *" rhel "* ]]; then
  log "3/3 RHEL 9 family master packages (Rocky 9 container)"
  INNER='set -e; dnf -q -y install epel-release dnf-plugins-core >/dev/null; dnf -q -y download --resolve --alldeps --destdir /out $PKGS'
  if [ "${NO_DOCKER:-0}" = 1 ]; then PKGS="$(pkgs master-rhel.pkgs)" bash -c "${INNER//\/out/$A/os/rhel}"
  else docker run --rm -e PKGS="$(pkgs master-rhel.pkgs)" -v "$A/os/rhel:/out" rockylinux:9 bash -c "$INNER"; fi
  log "   $(ls "$A/os/rhel"/*.rpm 2>/dev/null | wc -l) rpms"
fi
for d in ui ansible master tests docs online; do cp -a "$ROOT/$d" "$A/"; done
cp "$ROOT/README.md" "$ROOT/VERSIONS.env" "$ROOT/pytest.ini" "$ROOT/run-local.sh" "$A/"
find "$A" -name __pycache__ -prune -exec rm -rf {} +
name="autoinfra-master-$(date +%Y%m%d).zip"
(cd "$STAGE" && zip -q -r -0 "$DIST/$name" autoinfra)
(cd "$DIST" && sha256sum "$name" > "$name.sha256")
log "created $DIST/$name ($(du -h "$DIST/$name" | cut -f1)) - copy it and $name.sha256 to the master"
