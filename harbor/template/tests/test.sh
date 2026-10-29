#!/bin/sh
set -eu
# No model access, network, pip install or candidate code execution here.
mkdir -p /logs/verifier
python /tests/validate_report.py \
  --expected /tests/expected.json \
  --output /output \
  --logs /logs/verifier
