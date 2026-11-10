#!/bin/sh
# Runs Beacon's Teleport Machine ID bot ("beacon") as a continuous renewer.
# First run needs a join token from `tctl bots add beacon --roles=agent`
# (run once by a cluster admin); after that tbot renews from its own stored
# identity in ./storage and the token is never needed again — safe to leave
# this running indefinitely.
#
# The token never goes on a command line, where `ps` shows it to every user on
# the host: give it in the environment or as a file (tbot reads a file path
# for --token).
#
# Usage:
#   TELEPORT_PROXY_SERVER=teleport.example.com:443 ./run.sh   # normal run, reuses ./storage
#   TBOT_TOKEN=<join-token> ./run.sh                          # first run / re-bootstrap after storage is wiped
#   TBOT_TOKEN_FILE=/path/to/token ./run.sh                   # the same, from a file

set -eu
cd "$(dirname "$0")"

: "${TELEPORT_PROXY_SERVER:?set TELEPORT_PROXY_SERVER to your Teleport proxy, e.g. teleport.example.com:443}"
if [ "$#" -gt 0 ]; then
  echo "run.sh no longer takes the token as an argument (ps would show it): use TBOT_TOKEN or TBOT_TOKEN_FILE" >&2
  exit 2
fi

ARGS="--destination=file://$(pwd)/data --storage=file://$(pwd)/storage --proxy-server=$TELEPORT_PROXY_SERVER --join-method=token"

if [ -n "${TBOT_TOKEN_FILE:-}" ]; then
  exec tbot start identity $ARGS --token="$TBOT_TOKEN_FILE"
elif [ -n "${TBOT_TOKEN:-}" ]; then
  # tbot reads the token at start, so a private temp file is enough; no exec,
  # so the trap can remove it when tbot stops.
  tokenfile="$(umask 077 && mktemp)"
  trap 'rm -f "$tokenfile"' EXIT INT TERM
  printf '%s' "$TBOT_TOKEN" > "$tokenfile"
  unset TBOT_TOKEN
  tbot start identity $ARGS --token="$tokenfile"
else
  exec tbot start identity $ARGS
fi
