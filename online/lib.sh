#!/usr/bin/env bash
# Shared helpers for the online builders (run on an internet-connected Ubuntu 24.04 + Docker host).
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/VERSIONS.env"
# optional local overrides (e.g. an internal mirror URL) - same KEY="value" syntax
# shellcheck disable=SC1091
[ -f "$ROOT/versions.local.env" ] && source "$ROOT/versions.local.env"
DIST="${DIST:-$ROOT/dist}"
WORK="${WORK:-$ROOT/work}"

log() { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%T)" "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing command: $1 (apt-get install $1)"; }

preflight() {
  need zip; need curl; need sha256sum
  if [ "${NO_DOCKER:-0}" = "1" ]; then
    # build directly on this host: use a THROWAWAY Ubuntu 24.04 VM (it installs build tools and downloads packages)
    [ "$EUID" -eq 0 ] || die "NO_DOCKER=1 needs root (sudo -E NO_DOCKER=1 bash $0)"
    grep -q '^VERSION_ID="24.04"' /etc/os-release || die "NO_DOCKER=1 requires Ubuntu 24.04 (the target OS)"
  else
    need docker
    docker info >/dev/null 2>&1 || die "docker daemon not reachable (is your user in the docker group?)"
  fi
  curl -fsI --max-time 15 http://archive.ubuntu.com/ubuntu/ >/dev/null || die "no internet access to archive.ubuntu.com"
  mkdir -p "$DIST" "$WORK/out"
  # env file handed to the build containers (strip quotes/comments)
  cat "$ROOT/VERSIONS.env" $( [ -f "$ROOT/versions.local.env" ] && echo "$ROOT/versions.local.env" ) \
    | grep -vE '^\s*(#|$)' | sed 's/"//g' > "$WORK/env.list"
}

# run_in_ubuntu <inner-script> [KEY=VAL ...]
run_in_ubuntu() {
  local script="$1"; shift
  if [ "${NO_DOCKER:-0}" = "1" ]; then
    for l in out:"$WORK/out" inner:"$ROOT/online/inner" lists:"$ROOT/online/lists"; do
      [ -e "/${l%%:*}" ] && [ ! -L "/${l%%:*}" ] && die "/${l%%:*} exists and is not a symlink; cannot use NO_DOCKER mode"
      ln -sfn "${l#*:}" "/${l%%:*}"
    done
    local kv; mapfile -t kv < "$WORK/env.list"
    env "${kv[@]}" "$@" DEBIAN_FRONTEND=noninteractive bash "/inner/$script"
    return
  fi
  local extra=()
  for kv in "$@"; do extra+=(-e "$kv"); done
  docker run --rm -e DEBIAN_FRONTEND=noninteractive --env-file "$WORK/env.list" "${extra[@]}" \
    -v "$WORK/out:/out" -v "$ROOT/online/inner:/inner:ro" -v "$ROOT/online/lists:/lists:ro" \
    "ubuntu:${UBUNTU_RELEASE}" bash "/inner/$script"
}

# make_zip <zip-name> <stage-dir>   (stage-dir contains the top-level 'airgap/' folder)
make_zip() {
  local name="$1" stage="$2"
  rm -f "$DIST/$name"
  (cd "$stage" && zip -r -0 -q "$DIST/$name" airgap)
  (cd "$DIST" && sha256sum "$name" > "$name.sha256")
  log "created $DIST/$name ($(du -h "$DIST/$name" | cut -f1))"
}
