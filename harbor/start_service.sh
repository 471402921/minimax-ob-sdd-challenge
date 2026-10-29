#!/bin/bash
# Start one explicitly requested batch under USER systemd, without blocking chat.
# Dedicated secret file is sourced here, never in unit properties/argv.
set -euo pipefail
set +x
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "${1:-}" == "--entry" ]]; then
  shift
  KEY_FILE="${OB_HARBOR_KEY_FILE:-$HOME/.config/minimax-ob-judge/mplan.env}"
  [[ -r "$KEY_FILE" ]] || { echo "Key file not readable; no task started" >&2; exit 2; }
  if [[ "${HARBOR_BATCH_TRACE:-0}" == "1" ]]; then
    export HARBOR_BATCH_TRACE=1
    SUDO_ENV=(--preserve-env=HARBOR_BATCH_TRACE)
  else
    SUDO_ENV=()
  fi
  # Only non-secret flags cross sudo; the key VALUE is read from the 0600 file
  # by the entry script, so it never appears in sudo's journal ENV= line or ps.
  exec sudo -n "${SUDO_ENV[@]}" /usr/bin/python3 -B "$ROOT/harbor/service_entry.py" \
    --key-file "$KEY_FILE" "$@"
fi
if [[ $# -ne 2 || ! "$2" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,60}$ ]]; then
  echo 'Usage: bash harbor/start_service.sh MANIFEST UNIQUE_BATCH_ID' >&2
  exit 2
fi
MANIFEST="$(realpath -- "$1")"
BATCH="$2"
WHEELS="${WHEELS:-$ROOT/workdir/wheelhouse-pc140}"
TRIALS="${TRIALS:-1}"
BACKENDS="${BACKENDS:-1}"
if [[ ! "$TRIALS" =~ ^[1-4]$ || ! "$BACKENDS" =~ ^[1-2]$ ]]; then
  echo 'TRIALS must be 1..4 and BACKENDS 1..2' >&2; exit 2
fi
EXTRA=()
# Cold installs can exceed the 10-minute target, so they must be deliberate.
if [[ "${ALLOW_UNCACHED:-0}" == "1" ]]; then EXTRA+=(--allow-uncached); fi
cd "$ROOT"
# Read-only plan includes dependency audit, lease and resource checks; no secret
# or platform operation. The audit is what stops a 12-minute cold pip install.
python3 -B harbor/batch.py --manifest "$MANIFEST" --batch-id "$BATCH" \
  --trials "$TRIALS" --backends "$BACKENDS" --timeout 2400 --wheels "$WHEELS" "${EXTRA[@]}"
EXTRA_SYSTEMD=()
if [[ "${HARBOR_BATCH_TRACE:-0}" == "1" ]]; then EXTRA_SYSTEMD+=(--setenv=HARBOR_BATCH_TRACE=1); fi
systemd-run --user --unit="harbor-$BATCH" \
  --property=WorkingDirectory="$ROOT" --property=KillMode=control-group \
  --property=TimeoutStopSec=45 --property=RuntimeMaxSec=4200 \
  "${EXTRA_SYSTEMD[@]}" \
  /bin/bash "$ROOT/harbor/start_service.sh" --entry \
  --manifest "$MANIFEST" --batch-id "$BATCH" --trials "$TRIALS" --backends "$BACKENDS" \
  --timeout 2400 --wheels "$WHEELS" "${EXTRA[@]}" --execute
printf 'Started service harbor-%s. No auto-callback.\n' "$BATCH"
printf 'Status: systemctl --user status harbor-%s --no-pager\n' "$BATCH"
