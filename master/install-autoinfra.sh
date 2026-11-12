#!/usr/bin/env bash
# AutoInfra - install on the AIR-GAPPED master (Ubuntu 24.04 or RHEL/Rocky/Alma 9). Nothing is downloaded.
#   unzip autoinfra-master-YYYYMMDD.zip -d /opt          -> /opt/autoinfra
#   sudo bash /opt/autoinfra/master/install-autoinfra.sh
# Options (environment): MASTER_IP=10.0.0.5  ADMIN_PASSWORD=...  UI_PORT=443  REPO_PORT=8081  NTP_ALLOW=10.0.0.0/16
#   NFS_EXPORTS="/export/primary2 /export/secondary2" NFS_CLIENTS="10.0.0.0/24"  the master also serves NFS (master/setup-nfs.sh;
#   needs the cloudstack package zip uploaded first - re-run the installer afterwards)
#                        NO_SYSTEMD=1 (containers/chroots: start gunicorn + nginx directly)  SKIP_OS_PACKAGES=1
set -Eeuo pipefail
APP=/opt/autoinfra DATA=/var/lib/autoinfra ETC=/etc/autoinfra
UI_PORT="${UI_PORT:-443}" REPO_PORT="${REPO_PORT:-8081}"
log() { printf '\033[1;34m[autoinfra]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
[ "$EUID" -eq 0 ] || die "run as root (sudo)"
[ -f "$APP/ui/autoinfra/__init__.py" ] || die "$APP/ui missing - unzip the bundle with: unzip autoinfra-master-*.zip -d /opt"
. /etc/os-release
case "$ID" in
  ubuntu) FAMILY=debian; [ "${VERSION_ID}" = 24.04 ] || log "WARNING: tested on Ubuntu 24.04, this is $VERSION_ID" ;;
  rhel|rocky|almalinux|centos|ol) FAMILY=rhel; [ "${VERSION_ID%%.*}" = 9 ] || log "WARNING: tested on RHEL-family 9, this is $VERSION_ID" ;;
  *) die "unsupported OS $ID $VERSION_ID (Ubuntu 24.04 or RHEL/Rocky/Alma 9)" ;;
esac
MASTER_IP="${MASTER_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
[ -n "$MASTER_IP" ] || die "cannot detect the master IP - run: MASTER_IP=x.x.x.x bash $0"
log "OS: $PRETTY_NAME ($FAMILY)  master IP: $MASTER_IP"

# 1) OS packages from the bundle only (all online sources are ignored)
if [ "${SKIP_OS_PACKAGES:-0}" != 1 ]; then
  if [ "$FAMILY" = debian ]; then
    ls "$APP"/os/ubuntu/*.deb >/dev/null 2>&1 || die "no Ubuntu packages in $APP/os/ubuntu (bundle built with TARGETS=ubuntu?)"
    log "installing master packages from $APP/os/ubuntu (offline)"
    # index with dpkg-deb (part of every Ubuntu) - dpkg-dev/apt-utils are not installed yet on a fresh master
    python3 - "$APP/os/ubuntu" <<'PYEOF'
import hashlib, os, subprocess, sys
d = sys.argv[1]; out = []
for f in sorted(os.listdir(d)):
    if f.endswith(".deb"):
        p = os.path.join(d, f); data = open(p, "rb").read()
        ctl = subprocess.run(["dpkg-deb", "-f", p], capture_output=True, text=True, check=True).stdout.rstrip("\n")
        out.append(f"{ctl}\nFilename: ./{f}\nSize: {len(data)}\nSHA256: {hashlib.sha256(data).hexdigest()}\nMD5sum: {hashlib.md5(data).hexdigest()}")
open(os.path.join(d, "Packages"), "w").write("\n\n".join(out) + "\n")
print(f"indexed {len(out)} packages")
PYEOF
    echo "deb [trusted=yes] file:$APP/os/ubuntu ./" > /etc/apt/sources.list.d/autoinfra-master.list
    APT=(-o Dir::Etc::sourcelist=/etc/apt/sources.list.d/autoinfra-master.list -o Dir::Etc::sourceparts=/dev/null -o APT::Get::List-Cleanup=0)
    apt-get "${APT[@]}" update -qq
    DEBIAN_FRONTEND=noninteractive apt-get "${APT[@]}" install -y -qq --no-install-recommends \
      $(grep -vE '^\s*(#|$)' "$APP/online/lists/master-ubuntu.pkgs" | grep -v dpkg-dev | tr '\n' ' ')
    PY=python3; NGX_USER=www-data
  else
    ls "$APP"/os/rhel/*.rpm >/dev/null 2>&1 || die "no RHEL packages in $APP/os/rhel (bundle built with TARGETS=rhel?)"
    log "installing master packages from $APP/os/rhel (offline)"
    dnf -q -y --disablerepo='*' install "$APP"/os/rhel/*.rpm || rpm -Uvh --replacepkgs "$APP"/os/rhel/*.rpm
    PY=python3.12; NGX_USER=nginx
  fi
else
  PY=$(command -v python3.12 || command -v python3); NGX_USER=$(id -u www-data >/dev/null 2>&1 && echo www-data || echo nginx)
fi
"$PY" -c 'import sys; assert sys.version_info >= (3, 10), sys.version' || die "Python >= 3.10 needed ($PY)"

# 2) service user, directories
id autoinfra >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin autoinfra 2>/dev/null || useradd -r -d "$DATA" -s /sbin/nologin autoinfra
mkdir -p "$DATA"/{repo,uploads,jobs,work,reports,ssh,openstack} "$ETC"/tls
chown -R autoinfra:autoinfra "$DATA"; chmod 750 "$DATA"; chmod 755 "$DATA/repo"; chmod 700 "$DATA/ssh" "$DATA/uploads"
# nginx must traverse to the repo (read only)
chmod o+x "$DATA"

# 3) Python virtualenv from the wheelhouse (no index)
log "creating $APP/venv from the offline wheelhouse"
"$PY" -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --no-index --find-links "$APP/wheels" -r "$APP/ui/requirements.txt"
"$APP/venv/bin/python" -c "import flask, paramiko, ansible, openpyxl, reportlab, createrepo_c" || die "python packages incomplete"

# 4) secrets + admin password (shown once)
if [ ! -f "$ETC/autoinfra.env" ]; then
  ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-16)Aa1!}"
  HASH="$("$APP/venv/bin/python" -c 'import sys;from werkzeug.security import generate_password_hash as g;print(g(sys.argv[1]))' "$ADMIN_PASSWORD")"
  umask 077
  cat > "$ETC/autoinfra.env" <<EOT
AUTOINFRA_DATA=$DATA
AUTOINFRA_REPO=$DATA/repo
AUTOINFRA_MASTER_IP=$MASTER_IP
AUTOINFRA_SECRET=$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-48)
AUTOINFRA_ADMIN_HASH='$HASH'
AUTOINFRA_ANSIBLE_DIR=$APP/ansible
AUTOINFRA_BEHIND_PROXY=1
PATH=$APP/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EOT
  umask 022
  chown root:autoinfra "$ETC/autoinfra.env"; chmod 640 "$ETC/autoinfra.env"
  NEW_PW=1
fi

# 5) TLS certificate for the UI (self-signed, offline - replace with your CA's)
if [ ! -f "$ETC/tls/ui.crt" ]; then
  openssl req -x509 -nodes -newkey rsa:3072 -days 1825 -keyout "$ETC/tls/ui.key" -out "$ETC/tls/ui.crt" \
    -subj "/CN=autoinfra-master" -addext "subjectAltName=IP:$MASTER_IP,DNS:$(hostname)" 2>/dev/null
  chmod 600 "$ETC/tls/ui.key"
fi

# 6) nginx: UI on https :$UI_PORT -> gunicorn 127.0.0.1:8000 ; repositories on http :$REPO_PORT/repo/
NGX_CONF=/etc/nginx/conf.d/autoinfra.conf
[ "$FAMILY" = debian ] && rm -f /etc/nginx/sites-enabled/default
cat > "$NGX_CONF" <<EOT
# managed by AutoInfra
server {
    listen $REPO_PORT default_server;
    location /repo/ { alias $DATA/repo/; autoindex on; sendfile on; }
    location = / { return 302 /repo/; }
}
server {
    listen $UI_PORT ssl;
    ssl_certificate     $ETC/tls/ui.crt;
    ssl_certificate_key $ETC/tls/ui.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 16m;            # uploads arrive in 8 MB chunks
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 300s;
        proxy_request_buffering off;
    }
}
EOT
if [ "$FAMILY" = rhel ] && command -v getenforce >/dev/null && [ "$(getenforce)" != Disabled ]; then
  log "SELinux: allowing nginx to read the repo and proxy to the UI"
  semanage fcontext -a -t httpd_sys_content_t "$DATA/repo(/.*)?" 2>/dev/null || true
  restorecon -R "$DATA/repo" 2>/dev/null || chcon -R -t httpd_sys_content_t "$DATA/repo" || true
  semanage port -a -t http_port_t -p tcp "$REPO_PORT" 2>/dev/null || true
  setsebool -P httpd_can_network_connect 1 || true
fi
nginx -t

# 7) chrony: the master is the NTP source of the air-gapped estate
CHRONY_DIR=/etc/chrony/conf.d; [ -d "$CHRONY_DIR" ] || CHRONY_DIR=/etc/chrony.d; mkdir -p "$CHRONY_DIR"
printf 'local stratum 10\nallow %s\n' "${NTP_ALLOW:-all}" | sed 's/allow all/allow/' > "$CHRONY_DIR/autoinfra.conf"
grep -q "$CHRONY_DIR" /etc/chrony.conf 2>/dev/null || { [ -f /etc/chrony.conf ] && echo "confdir $CHRONY_DIR" >> /etc/chrony.conf; } || true

# 8) firewall
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q active; then ufw allow "$UI_PORT/tcp"; ufw allow "$REPO_PORT/tcp"; ufw allow 123/udp; fi
if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  for p in "$UI_PORT/tcp" "$REPO_PORT/tcp" 123/udp; do firewall-cmd -q --permanent --add-port=$p; done; firewall-cmd -q --reload
fi

# 9) service (one gunicorn worker: deployment jobs are threads inside it)
GUNI="$APP/venv/bin/gunicorn --workers 1 --threads 16 --timeout 300 --bind 127.0.0.1:8000 --chdir $APP/ui 'autoinfra:create_app()'"
cat > /etc/systemd/system/autoinfra.service <<EOT
[Unit]
Description=AutoInfra (air-gapped CloudStack/OpenStack deployment UI)
After=network-online.target
[Service]
User=autoinfra
Group=autoinfra
EnvironmentFile=$ETC/autoinfra.env
ExecStart=$GUNI
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
[Install]
WantedBy=multi-user.target
EOT
if [ "${NO_SYSTEMD:-0}" = 1 ]; then
  log "NO_SYSTEMD=1: starting gunicorn and nginx directly"
  pkill -f "gunicorn.*autoinfra" 2>/dev/null || true
  su -s /bin/sh autoinfra -c "set -a; . $ETC/autoinfra.env; set +a; nohup $GUNI > $DATA/gunicorn.log 2>&1 &"
  nginx -s reload 2>/dev/null || nginx
else
  systemctl daemon-reload
  systemctl enable --now autoinfra nginx
  systemctl enable --now chrony 2>/dev/null || systemctl enable --now chronyd 2>/dev/null || true
  systemctl restart nginx autoinfra
fi
for i in $(seq 1 30); do curl -fsk -o /dev/null "https://127.0.0.1:$UI_PORT/login" && break; sleep 1; done
curl -fsk -o /dev/null "https://127.0.0.1:$UI_PORT/login" || die "UI did not come up - check: journalctl -u autoinfra (or $DATA/gunicorn.log)"

if [ -n "${NFS_EXPORTS:-}" ]; then
  if ls "$DATA"/repo/*/deb/Packages "$DATA"/repo/*/rpm/repodata/repomd.xml >/dev/null 2>&1; then
    REPO_ROOT="$DATA/repo" MASTER_IP="$MASTER_IP" bash "$(dirname "$0")/setup-nfs.sh"
  else
    echo "NOTE: NFS_EXPORTS set, but no package repository is uploaded yet - upload the cloudstack zip, then re-run this script"
  fi
fi

cat <<EOT

=====================================================================
 AutoInfra is running.
   UI          : https://$MASTER_IP$( [ "$UI_PORT" = 443 ] || echo ":$UI_PORT")/    (self-signed certificate)
   Repositories: http://$MASTER_IP:$REPO_PORT/repo/
$( [ "${NEW_PW:-0}" = 1 ] && echo "   Login       : admin / $ADMIN_PASSWORD      <-- shown ONCE, change it in Settings > My account" )
   Service     : systemctl status autoinfra ; journalctl -u autoinfra -f
   Data        : $DATA  (database, repositories, job logs, reports, SSH key)
=====================================================================
EOT
