#!/bin/bash
# Start one explicitly requested batch under USER systemd, without blocking chat.
# Dedicated secret file is sourced here, never in unit properties/argv.
set -euo pipefail
set +x
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "${1:-}" == "--entry" ]]; then
  shift
  source "$HOME/.config/minimax-ob-judge/mplan.env"
  : "${OB_EVAL_MODEL_KEY:?Dedicated key missing}"
  export OB_EVAL_MODEL_KEY
  exec sudo -n --preserve-env=OB_EVAL_MODEL_KEY /usr/bin/python3 -B "$ROOT/harbor/service_entry.py" "$@"
fi
if [[ $# -ne 2 || ! "$2" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,60}$ ]]; then
  echo 'Usage: bash harbor/start_service.sh MANIFEST UNIQUE_BATCH_ID' >&2
  exit 2
fi
MANIFEST="$(realpath -- "$1")"
BATCH="$2"
cd "$ROOT"
# Read-only plan includes lease + resource checks; no secret or platform operation.
python3 -B harbor/batch.py --manifest "$MANIFEST" --batch-id "$BATCH" \
  --trials 1 --backends 1 --timeout 2400
systemd-run --user --unit="harbor-$BATCH" \
  --property=WorkingDirectory="$ROOT" --property=KillMode=control-group \
  --property=TimeoutStopSec=45 --property=RuntimeMaxSec=2520 \
  /bin/bash "$ROOT/harbor/start_service.sh" --entry \
  --manifest "$MANIFEST" --batch-id "$BATCH" --trials 1 --backends 1 --timeout 2400 --execute
printf 'Started service harbor-%s. No auto-callback.\n' "$BATCH"
printf 'Status: systemctl --user status harbor-%s --no-pager\n' "$BATCH"
