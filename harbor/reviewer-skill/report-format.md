# Review report contract v1

`/output/report.json` is UTF-8 JSON. No secrets. All paths below are relative to
`/output`; evidence must be regular files under `evidence/` (not symlinks).

```json
{
  "schema_version": 1,
  "task_id": 123,
  "commit_id": "0123456789abcdef0123456789abcdef01234567",
  "scope": "minimax",
  "recommendation": "review",
  "summary": "Actual observations and remaining uncertainty",
  "environment_adjustments": [],
  "checks": {
    "runtime": {
      "status": "blocked",
      "basis": "not_run",
      "detail": "Required service unavailable",
      "evidence": []
    }
  }
}
```

`checks` must have exactly these six keys (evidence dimensions, not a frozen score):
`runtime`, `minimax`, `context`, `database`, `state`, `code_change`.

Each check:
- `status`: `verified`, `failed`, `blocked`, `not_run`, or `uncertain`.
- `basis`: `rerun`, `static`, `supplied_record`, or `not_run`.
- `detail`: nonempty explanation of what was actually observed.
- `evidence`: array of `{ "path": "evidence/NAME.log", "sha256": "64 lowercase hex" }`.
  `verified` and `failed` require at least one evidence file.

`recommendation`: `pass`, `fail`, `review`, or `error`. `pass` requires every check
status to be `verified`, but does NOT imply approval for platform submission. This
is only a pilot report-consistency rule; the actual official rubric must be frozen
separately. `failed` requires an actual observed violation, not missing execution.

`environment_adjustments`: array of strings explaining every deviation from the
submitted run instructions. If source repairs are needed, do not count repaired
behavior as verified original behavior.

Include machine-readable command records alongside logs when possible:
command, cwd, start/end, exit_code, timeout, passed/failed/skipped counts, real or
mock backend, fixed source version. Model record inspection must use
`supplied_record` or `static`, not `rerun` unless it was actually re-executed.

The verifier's `reward=1` means this report is well-formed and its evidence files
match their declared hashes. A truthful report with `recommendation=fail` can
receive `reward=1`. It NEVER means a contestant earned 20 points.
