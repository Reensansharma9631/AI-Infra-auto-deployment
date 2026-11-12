#!/usr/bin/env bash
# The MASTER as an extra NFS server for CloudStack (e.g. a second primary + secondary storage).
# Packages come ONLY from the master's own AutoInfra repositories (offline); the master's other apt/dnf sources are not used
# and not changed.
#
#   sudo NFS_EXPORTS="/export/primary2 /export/secondary2" NFS_CLIENTS="172.27.15.0/24 10.200.0.0/24" bash master/setup-nfs.sh
#
#   NFS_EXPORTS   directories to create and export (required)
#   NFS_CLIENTS   networks allowed to mount (default: the /24 of MASTER_IP)
#   REPO_ROOT     AutoInfra repositories (default /var/lib/autoinfra/repo)
# Safe to re-run: /etc/exports.d/autoinfra.exports is rewritten, everything else is left alone.
set -Eeuo pipefail
die() { echo "ERROR: $*" >&2; exit 1; }
[ "$EUID" -eq 0 ] || die "run as root"
[ -n "${NFS_EXPORTS:-}" ] || die "NFS_EXPORTS is empty - e.g. NFS_EXPORTS=\"/export/primary2 /export/secondary2\""
REPO_ROOT="${REPO_ROOT:-/var/lib/autoinfra/repo}"
if [ -z "${NFS_CLIENTS:-}" ]; then
  ip="${MASTER_IP:-$(hostname -I | awk '{print $1}')}"
  NFS_CLIENTS="${ip%.*}.0/24"
fi
for p in $NFS_EXPORTS; do case "$p" in /*) ;; *) die "export path must be absolute: $p";; esac; done
. /etc/os-release

install_offline() {
  if command -v apt-get >/dev/null; then
    dpkg -s nfs-kernel-server >/dev/null 2>&1 && return 0
    local src; src="$(mktemp)"
    for d in "$REPO_ROOT"/*/deb; do [ -f "$d/Packages" ] && echo "deb [trusted=yes] file:$d ./" >> "$src"; done
    [ -s "$src" ] || die "no AutoInfra apt repository under $REPO_ROOT - upload the cloudstack package zip first"
    local o=(-o "Dir::Etc::sourcelist=$src" -o "Dir::Etc::sourceparts=-" -o "APT::Get::List-Cleanup=0")
    apt-get "${o[@]}" update -q >/dev/null
    DEBIAN_FRONTEND=noninteractive apt-get "${o[@]}" install -y -q --no-install-recommends nfs-kernel-server \
      || die "nfs-kernel-server is not in the AutoInfra repositories ($REPO_ROOT)"
    rm -f "$src"
    apt-get update -q >/dev/null 2>&1 || true      # restore the normal package lists (no effect offline)
  else
    rpm -q nfs-utils >/dev/null 2>&1 && return 0
    local args=(--disablerepo='*')
    for d in "$REPO_ROOT"/*/rpm; do [ -f "$d/repodata/repomd.xml" ] && args+=("--repofrompath=ai-$(basename "$(dirname "$d")"),$d"); done
    [ "${#args[@]}" -gt 1 ] || die "no AutoInfra rpm repository under $REPO_ROOT"
    dnf -y -q "${args[@]}" --nogpgcheck install nfs-utils || die "nfs-utils is not in the AutoInfra repositories"
  fi
}

install_offline
svc=nfs-kernel-server; command -v apt-get >/dev/null || svc=nfs-server
mkdir -p /etc/exports.d
{
  echo "# managed by AutoInfra master/setup-nfs.sh"
  for p in $NFS_EXPORTS; do
    mkdir -p "$p"; chmod 0755 "$p"
    line="$p"; for c in $NFS_CLIENTS; do line+=" $c(rw,sync,no_root_squash,no_subtree_check)"; done
    echo "$line"
  done
} > /etc/exports.d/autoinfra.exports
systemctl enable --now "$svc" >/dev/null 2>&1 || systemctl restart "$svc"
exportfs -ra
# open the firewall only when one is really active
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q '^Status: active'; then
  for c in $NFS_CLIENTS; do ufw allow from "$c" to any port nfs >/dev/null; ufw allow from "$c" to any port 111 >/dev/null; ufw allow from "$c" to any port 20048 >/dev/null; done
elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  firewall-cmd -q --permanent --add-service=nfs --add-service=mountd --add-service=rpc-bind && firewall-cmd -q --reload
fi
command -v restorecon >/dev/null && for p in $NFS_EXPORTS; do restorecon -R "$p" || true; done
echo "NFS on the master: $(exportfs | awk '/^\//{print $1}' | sort -u | tr '\n' ' ')- clients: $NFS_CLIENTS"
