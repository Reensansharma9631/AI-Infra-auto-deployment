#!/usr/bin/env bash
# ONLINE machine -> ONE file to carry into the air-gapped site.
#   autoinfra-offline-<date>.zip = master bundle (AutoInfra + wheels + master OS packages)
#                                + one package zip per product (complete dependency sets, also the basic packages)
#                                + SHA256SUMS + MISSING.txt (packages no repository on this machine offers) + README-OFFLINE.txt
#
# Without Docker (run on the same OS as the master/nodes, e.g. Ubuntu 24.04):
#     sudo NO_DOCKER=1 bash online/build-all.sh
# With Docker (any Linux):
#     bash online/build-all.sh
#
# Options (environment):
#   PRODUCTS="core cloudstack kvm databases"   kits to build (also: monitoring openstack ollama)
#   NODE_OS=ubuntu-24.04                       OS of the nodes (ubuntu-24.04 | rocky-9 | alma-9 | rhel-9)
#   CLOUDSTACK_VERSION=4.22.0                  version for the cloudstack kit
#   DIST=./dist                                output folder
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="${DIST:-$ROOT/dist}"; mkdir -p "$DIST"; DIST="$(cd "$DIST" && pwd)"
PRODUCTS="${PRODUCTS:-core cloudstack kvm databases}"
NODE_OS="${NODE_OS:-ubuntu-24.04}"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
log() { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%T)" "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
for c in zip unzip sha256sum python3 curl; do command -v "$c" >/dev/null || die "missing command: $c (apt-get install $c)"; done
python3 -m pip --version >/dev/null 2>&1 || die "missing: python3-pip (apt-get install python3-pip)"
if [ "${NO_DOCKER:-0}" = 1 ]; then
  [ "$EUID" -eq 0 ] || die "NO_DOCKER=1 needs root: sudo NO_DOCKER=1 bash $0"
  . /etc/os-release
  case "$NODE_OS" in
    ubuntu-*) [ "$ID" = ubuntu ] && [ "$VERSION_ID" = "${NODE_OS#ubuntu-}" ] || die "NO_DOCKER=1: this machine is $PRETTY_NAME but NODE_OS=$NODE_OS - packages must be downloaded on the same OS";;
    *) command -v dnf >/dev/null || die "NO_DOCKER=1 with NODE_OS=$NODE_OS needs a RHEL/Rocky/Alma 9 machine";;
  esac
  export NO_DOCKER
  : "${TARGETS:=$([ "$ID" = ubuntu ] && echo ubuntu || echo rhel)}"
else
  command -v docker >/dev/null || die "docker not found - install it, or run: sudo NO_DOCKER=1 bash $0"
  : "${TARGETS:=ubuntu rhel}"
fi
export TARGETS

log "A. master bundle (TARGETS=$TARGETS)"
DIST="$WORK/master" bash "$ROOT/online/build-autoinfra-master.sh"
MASTER_ZIP="$(ls "$WORK"/master/autoinfra-master-*.zip)"

log "B. kit generator (the same code the UI uses for Package Repository > Online Download)"
unzip -q -j "$MASTER_ZIP" 'autoinfra/wheels/*' -d "$WORK/wheels"
python3 -m pip install -q --no-index --find-links "$WORK/wheels" --target "$WORK/pylib" -r "$ROOT/ui/requirements.txt" >/dev/null 2>&1 \
  || python3 -m pip install -q --target "$WORK/pylib" -r "$ROOT/ui/requirements.txt" >/dev/null 2>&1 || die "cannot install the kit generator's Python packages"
mkdir -p "$WORK/kits" "$WORK/bundle"
: > "$WORK/bundle/MISSING.txt"
for p in $PRODUCTS; do
  PYTHONPATH="$WORK/pylib:$ROOT/ui" AUTOINFRA_DATA="$WORK/data" python3 - "$p" "$NODE_OS" "${CLOUDSTACK_VERSION:-}" "$WORK/kits" <<'PY'
import os, sys
from autoinfra import create_app, catalog, packages
product, node_os, csver, out = sys.argv[1:5]
if product not in catalog.PRODUCTS:
    sys.exit(f"unknown product '{product}' (known: {' '.join(catalog.PRODUCTS)})")
fam = "rhel" if node_os.startswith(("rocky", "alma", "rhel", "ol")) else "debian"
ver = (csver if product == "cloudstack" and csver else catalog.PRODUCTS[product]["versions"][0])
with create_app({"TESTING": True}).app_context():
    zipname, script, pkgs = packages.build_kit(product, fam, node_os.split("-", 1)[1], ver)
open(os.path.join(out, f"kit-{product}.sh"), "w").write(script)
print(f"   kit-{product}.sh -> {zipname} ({len(pkgs)} seed packages)")
PY
done

n=0
for p in $PRODUCTS; do
  n=$((n + 1)); log "C.$n download: $p"
  rc=0; (cd "$WORK/bundle" && bash "$WORK/kits/kit-$p.sh") > "$WORK/kit-$p.log" 2>&1 || rc=$?
  grep -E 'NOT AVAILABLE|^DONE' "$WORK/kit-$p.log" || true
  if [ "$rc" = 0 ]; then
    grep 'NOT AVAILABLE' "$WORK/kit-$p.log" | sed "s/^/$p: /" >> "$WORK/bundle/MISSING.txt" || true
  else
    echo "$p: KIT FAILED - see the output above" >> "$WORK/bundle/MISSING.txt"
    log "   FAILED: $p (continuing; listed in MISSING.txt)"
    grep -v '^W: ' "$WORK/kit-$p.log" | tail -5 || true
  fi
done
rm -f "$WORK"/bundle/*.sha256

log "D. one file"
cp "$MASTER_ZIP" "$WORK/bundle/"
cat > "$WORK/bundle/README-OFFLINE.txt" <<EOT
AutoInfra offline set - built $(date -u +%Y-%m-%dT%H:%MZ) on $(. /etc/os-release; echo "$PRETTY_NAME") for nodes: $NODE_OS
Products: $PRODUCTS

On the AIR-GAPPED master:
  1. sha256sum -c SHA256SUMS                                  (every line must say OK)
  2. sudo unzip $(basename "$MASTER_ZIP") -d /opt
     sudo bash /opt/autoinfra/master/install-autoinfra.sh      (prints the URL and the admin password once)
  3. In the UI: Package Repository > Upload Packages - upload each *-packages.zip; choose the repository with the
     same name as the first word of the file (core-... -> core, cloudstack-... -> cloudstack, kvm-... -> kvm, databases-... -> databases).
  4. Pre-Deployment: run the checks. Any package listed as missing is not in these zips - see MISSING.txt.
  5. Follow docs/DEPLOY-AND-VALIDATE.md.

MISSING.txt lists packages that no repository on the online machine offered (vendor repositories, e.g. Percona, MongoDB,
Zabbix) and kits that failed. An empty file means everything requested was downloaded.
EOT
(cd "$WORK/bundle" && sha256sum *.zip > SHA256SUMS)
OUT="$DIST/autoinfra-offline-$(date +%Y%m%d).zip"
rm -f "$OUT"
(cd "$WORK/bundle" && zip -q -0 "$OUT" ./*)
(cd "$DIST" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256")
log "created $OUT ($(du -h "$OUT" | cut -f1))"
echo "contents:"; unzip -l "$OUT" | sed -n '4,$p' | head -20
if [ -s "$WORK/bundle/MISSING.txt" ]; then echo; echo "NOT COMPLETE - missing:"; cat "$WORK/bundle/MISSING.txt"; exit 2; fi
echo "complete: nothing missing"
