#!/bin/bash
# Auto-submit triage auto-pass candidates without leaking the token into argv.
# Usage: harbor/submit_auto_pass.sh BATCH_DIR [--max N]
set -euo pipefail
BATCH="${1:?batch dir required}"; shift || true
ENVF="${OB_PLATFORM_ENV:-$HOME/.config/minimax-ob-judge/platform.env}"
[[ -r "$ENVF" ]] || { echo "platform env not readable: $ENVF" >&2; exit 2; }
exec sudo -n bash -c 'set +x; source "$1"; shift; cd /home/Jet.D/minimax-ob-sdd-challenge && python3 harbor/triage.py "$@"' _ "$ENVF" --batch "$BATCH" --submit-auto-pass --yes "$@"
