#!/usr/bin/env bash
# Run AutoInfra directly on HOST:PORT (default 0.0.0.0:8000) - no nginx, no systemd, no root needed.
# For trying it out and for labs. Production: master/install-autoinfra.sh (TLS, service user, systemd).
#   bash run-local.sh                    -> http://<this-ip>:8000/   (admin password printed on first start)
#   PORT=9000 bash run-local.sh          other port
#   TLS=1 bash run-local.sh              https with a self-signed certificate
#   MASTER_IP=10.0.0.5 bash run-local.sh the IP the nodes use to reach this machine
# Offline: if ./wheels exists (master bundle) Python packages are installed from it, otherwise from PyPI.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
HOST="${HOST:-0.0.0.0}" PORT="${PORT:-8000}"
DATA="${AUTOINFRA_DATA:-$ROOT/.data}"
VENV="${VENV:-$ROOT/.venv}"
PY="$(command -v python3.12 || command -v python3)"
"$PY" -c 'import sys; assert sys.version_info >= (3, 10)' || { echo "Python >= 3.10 needed"; exit 1; }
MASTER_IP="${MASTER_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"; MASTER_IP="${MASTER_IP:-127.0.0.1}"
mkdir -p "$DATA"; chmod 700 "$DATA"

if [ ! -x "$VENV/bin/gunicorn" ]; then
  echo "[autoinfra] creating $VENV"
  "$PY" -m venv "$VENV"
  if [ -d "$ROOT/wheels" ]; then "$VENV/bin/pip" install -q --no-index --find-links "$ROOT/wheels" -r "$ROOT/ui/requirements.txt"
  else "$VENV/bin/pip" install -q -r "$ROOT/ui/requirements.txt"; fi
fi

[ -f "$DATA/.secret" ] || head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' > "$DATA/.secret"
chmod 600 "$DATA/.secret"
if [ ! -f "$DATA/autoinfra.db" ]; then
  ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-14)Aa1!}"
  export AUTOINFRA_ADMIN_PASSWORD="$ADMIN_PASSWORD"
  FIRST=1
fi

SCHEME=http; GUNI_TLS=()
if [ "${TLS:-0}" = 1 ]; then
  [ -f "$DATA/tls.crt" ] || openssl req -x509 -nodes -newkey rsa:3072 -days 1825 -keyout "$DATA/tls.key" -out "$DATA/tls.crt" \
      -subj "/CN=autoinfra" -addext "subjectAltName=IP:$MASTER_IP" 2>/dev/null
  SCHEME=https; GUNI_TLS=(--certfile "$DATA/tls.crt" --keyfile "$DATA/tls.key")
fi

export AUTOINFRA_DATA="$DATA" AUTOINFRA_REPO="$DATA/repo" AUTOINFRA_SECRET="$(cat "$DATA/.secret")" \
       AUTOINFRA_MASTER_IP="$MASTER_IP" AUTOINFRA_REPO_URL="http://$MASTER_IP:$PORT/repo" \
       AUTOINFRA_COOKIE_SECURE="$([ "$SCHEME" = https ] && echo 1 || echo 0)" PATH="$VENV/bin:$PATH"
[ "$SCHEME" = https ] && AUTOINFRA_REPO_URL="https://$MASTER_IP:$PORT/repo"
if [ "$SCHEME" = https ]; then
  echo "[autoinfra] NOTE: with TLS=1 the nodes fetch packages over https with a self-signed certificate;"
  echo "            for apt/dnf use plain http (default) or install-autoinfra.sh (nginx serves :8081 over http)."
fi

cat <<EOT

  AutoInfra listening on $HOST:$PORT
  Open:   $SCHEME://$MASTER_IP:$PORT/        (also $SCHEME://127.0.0.1:$PORT/ on this machine)
  Repo:   $AUTOINFRA_REPO_URL/   (what the nodes use)
  Data:   $DATA
$( [ "${FIRST:-0}" = 1 ] && echo "  Login:  admin / $ADMIN_PASSWORD      <-- shown once, change it in Settings > My account" )
  Stop:   Ctrl+C
EOT
[ "$SCHEME" = http ] && [ "$HOST" = 0.0.0.0 ] && echo "  WARNING: plain HTTP on all interfaces - use only on a trusted network (TLS=1 for https)."
echo
cd "$ROOT/ui"
exec "$VENV/bin/gunicorn" --workers 1 --threads 16 --timeout 300 --bind "$HOST:$PORT" "${GUNI_TLS[@]}" \
     --access-logfile - "autoinfra:create_app()"
