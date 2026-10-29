#!/bin/sh
set -eu
# Oracle exercises artifact transfer + report validation. NOT an evaluator.
mkdir -p /output/evidence
python - <<'PY'
import json
from pathlib import Path
expected = json.loads(Path('/solution/expected.json').read_text())
report = dict(expected, schema_version=1, recommendation='review',
              summary='Synthetic oracle: report transport validated; no contestant evaluation performed.',
              environment_adjustments=[], checks={})
for name in ('runtime', 'minimax', 'context', 'database', 'state', 'code_change'):
    report['checks'][name] = dict(status='not_run', basis='not_run',
                                detail='Oracle transport test only.', evidence=[])
Path('/output/report.json').write_text(json.dumps(report, indent=2))
PY
