#!/bin/bash
# Copy what the access point needs to ~/$REMOTE there: chtc/ (with the bundle), config/,
# and a local_paths.py whose DATA_ROOT points at a folder in $HOME on the access point
# (everything else — your NetID, the container, the output share — is carried over from
# your own config/local_paths.py). Run from the repository root after
# `uv run python chtc/make_bundle.py`:
#
#   bash chtc/deploy.sh
#
# Requires the `chtc` host alias in ~/.ssh/config (docs/getting_started.md) and
# config/local_paths.py to exist here.
set -euo pipefail
cd "$(dirname "$0")/.."

HOST=${CHTC_HOST:-chtc}
REMOTE=${CHTC_PROJECT_NAME:-hytools-chtc}

test -f config/local_paths.py || { echo "config/local_paths.py is missing" >&2; exit 1; }

ssh "$HOST" "mkdir -p ~/$REMOTE/config ~/$REMOTE/chtc/runs"
scp -q config/__init__.py config/paths.py config/local_paths.example.py "$HOST:~/$REMOTE/config/"
scp -q -r chtc/*.sh chtc/*.sub chtc/*.py chtc/*.def chtc/dagman.config "$HOST:~/$REMOTE/chtc/"
scp -q -r src "$HOST:~/$REMOTE/"
scp -q -r chtc/bundle "$HOST:~/$REMOTE/chtc/"

# The access point needs the same settings with a DATA_ROOT of its own.
grep -v '^DATA_ROOT' config/local_paths.py > /tmp/ap_local_paths.py
printf 'DATA_ROOT = "/home/%s/%s-data"\n' "$(ssh "$HOST" whoami)" "$REMOTE" >> /tmp/ap_local_paths.py
scp -q /tmp/ap_local_paths.py "$HOST:~/$REMOTE/config/local_paths.py"
rm -f /tmp/ap_local_paths.py

ssh "$HOST" "cd ~/$REMOTE && chmod +x chtc/*.sh && ls -la chtc chtc/bundle | head -40"
echo "deployed to $HOST:~/$REMOTE"
