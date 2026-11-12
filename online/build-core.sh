#!/usr/bin/env bash
# Kept for old instructions: the master bundle is now built by build-autoinfra-master.sh
exec bash "$(dirname "$0")/build-autoinfra-master.sh" "$@"
