#!/usr/bin/env bash
# AutoInfra test runner.
#   bash tests/run-all.sh            unit + API + Ansible-content tests (~1480 cases, ~4 min, any Linux with the test deps)
#   bash tests/run-all.sh e2e        + real browser tests (Playwright Chromium, 58 cases)
#   sudo bash tests/run-all.sh lab   + REAL two-OS lab: Ubuntu 24.04 + AlmaLinux 9 nodes without internet (37 checks)
# test deps: pip install -r ui/requirements-test.txt ; apt-get install haproxy keepalived nginx rpm dpkg-dev openssh-server sshpass debootstrap
set -Eeuo pipefail
cd "$(dirname "$0")/.."
echo "== shell syntax"; for f in online/*.sh online/inner/*.sh master/*.sh tests/integration/*.sh; do bash -n "$f"; done; echo ok
echo "== JavaScript syntax"
if command -v node >/dev/null; then for f in ui/autoinfra/static/*.js; do node --check "$f"; done; echo ok
else echo "SKIPPED: node not installed (apt-get install nodejs)"; fi
echo "== unit / API / Ansible tests"; python3 -m pytest -q -n "${JOBS:-2}"
[ "${1:-}" = e2e ] || [ "${1:-}" = lab ] || exit 0
echo "== browser end-to-end"; python3 -m pytest -q tests/e2e -p no:logging
[ "${1:-}" = lab ] || exit 0
[ "$EUID" -eq 0 ] || { echo "lab mode needs root"; exit 1; }
[ -d /srv/alma9 ] && [ -d /srv/ubu24 ] || { echo "build the lab rootfs first - see README 'Run the tests yourself'"; exit 1; }
bash tests/integration/os_lab.sh reset
python3 tests/integration/lab_e2e.py "${BUNDLE:-/tmp/ubuntu-noble-bundle.zip}"
