#!/bin/sh
# Runs Beacon's Teleport Machine ID bot ("beacon") as a continuous renewer.
# First run needs a join token from `tctl bots add beacon --roles=agent`
# (run once by a cluster admin) passed as --token; after that tbot renews
# from its own stored identity in ./storage and the token is never needed
# again — safe to leave this running indefinitely.
#
# Usage:
#   ./run.sh                    # normal run, reuses ./storage
#   ./run.sh <join-token>       # first run / re-bootstrap after storage is wiped

set -eu
cd "$(dirname "$0")"

TOKEN="${1:-}"
ARGS="--destination=file://$(pwd)/data --storage=file://$(pwd)/storage --proxy-server=teleport.cqre.net:443 --join-method=token"

if [ -n "$TOKEN" ]; then
  ARGS="$ARGS --token=$TOKEN"
fi

exec tbot start identity $ARGS
