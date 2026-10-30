# Harbor validation log — 2026-10-28 / 2026-10-29

## 2026-10-29 update

- Real-submission Harbor review completed in approximately 16m25s with report_valid=1,
  recommendation=review. Operator follow-up independently verified live seekdb and
  PowerContext; backend checks were separate runs, not one combined end-to-end run.
- Dependency pin failures, slow downloads, verify.sh swallowing failures, and local
  state fallback demonstrated why schema-valid reports and exit0 do not prove success.
- Prewarm image builds and health checks pass: Pi1.4.0, offline seekdb1.4.0.post2
  open/close, real PowerContext readiness and server-assigned scope, private service
  description. The underlying image build used version tags, not fully pinned digests.
- Base-image task build and adapter bootstrap integration passed. BuildKit cannot
  use a bare local sha256 ID as FROM here; use a content-named local tag and verify
  its ID before launch. Dependency replacement remains an explicit experiment.
- Batch supervisor completed both 2-task and 4-task synthetic oracle runs. Four-task
  run: 4/4 finished; minimum sampled host MemAvailable approximately35.28GiB.
  These are lightweight transport checks, NOT real-model/backend capacity proof.
- Offline baseline (latest): 197 Harbor tests, 185 judge assertions, 9 recovery tests and
  3 confirmed-inaccessible tests passed; generated-task lint and shell syntax pass.
- User-systemd single-task launcher now separates chat latency from the40-minute
  wall limit. Current real single-task run is not assigned a result by this document.
- 4-way real batch `batch4-run` (2026-10-29): 4/4 trials completed, 0 errors,
  `report_valid=1` on all four, wall clock 16.3 min at 4 concurrent with 2 GiB each.
  Human-reviewed verdicts: 3 pass, 1 fail (missing declared sibling dependency, client
  API mismatch against powercontext 1.4.0, and an uncommitted data fixture).
- `triage.py` backtest on that batch: 3 auto-pass candidates + 1 human escalation,
  matching the human verdicts 4/4. A single 4-task batch is a smoke test, not calibration.
- Measured waste in that batch: two trials each spent 240.1 s on a network pip attempt
  although every pin was cached. Mitigations landed: optional `PIP_NO_INDEX` when the
  audit passes, and a single hashed reusable venv instead of 6-8 reinstalls. Not yet
  re-measured on a new batch.
- 6-way real batch `batch6-run` + 2 recovered trials (2026-10-29): 8/8 completed, 0 errors,
  `report_valid=1` on every trial. Per-trial wall clock 4.6-10.3 min (median ~6.6) at six
  concurrent with 2 GiB each; the 6-task batch finished in about 10.3 min (~35 tasks/hour
  for that wave). No trial consumed the full 240 s install budget.
- Two fetches initially failed and were recovered **without re-claiming**: one platform
  branch had been deleted (the exact commit was fetched by SHA after a code fix in
  `repo_fetch.py`), one `gitRepo` field concatenated a GitHub PAT with the URL for a
  private repo (access reconstructed from the task's own credential; the local receipt
  copy was then scrubbed). Both recovered on the original lease.
- `47216546` was reported `database: blocked` only because the sandbox had no container
  runtime. Re-verification started the bundled `seekdb` binary (5.7.25-OceanBase
  seekdb-v1.4.0.0) and ran the documented `init-db --reset` / `ingest` /
  `build-context` successfully, so the block was evaluator-side. Its range pin
  (`powercontext[builtin]>=1.2.0`) resolves to 1.4.x today, where its pipeline is
  rejected; only the README's 1.2.0 works. Disclosed, judged pass on the documented
  version.
- `47216316` was reported `code_change: uncertain`; an independent rerun against the
  referenced baseline commit produced 4 failed / 3 passed there versus 7 passed at HEAD
  with the same tests, i.e. the change is effective. It still failed overall: no
  dependency declaration at all and imports (`MemoryCitation`, `GetMemoryRequest`)
  that no published powercontext (1.0.0rc2-1.4.1) exports.
- One submission hit a proxy error during callback; the identical request was retried
  unchanged and acknowledged. No verdict or message was altered between attempts.
- Dependency snapshot caveat: the wheel set is pinned at powercontext 1.4.0 while PyPI
  already publishes 1.4.1, so range-pinned submissions are evaluated against a slightly
  older release than pip would choose today. See the runner document's version policy.
- 8-way batch `batch8-run-b` (2026-10-30): 8 tasks claimed and staged, 7 executed with
  exit 0 and 0 errors, 1 (`47216911`) never started. Admission allows only
  `base_available - 16` GiB of reservations; at 35 GiB available and a 3 GiB light
  reservation that is 7 concurrent trials, so the eighth waited. By the time a slot
  freed, its lease no longer covered `timeout + 900 s`, and the launcher refused it
  as `not_started` instead of oversubscribing. Observed container peaks were below
  1 GiB each, so the reservation is conservative; it was not lowered mid-batch.
- Wall clock for that batch was about 28 min (slowest trial exceeded 20 min), versus
  roughly 10 min for the 6-way batch. Concurrency beyond 7 needs both a higher
  admission budget and a longer lease, not just a bigger `--trials`.
- Triage on the 7 reports: 3 auto-pass candidates, 4 human. Verdicts: 6 pass, 1 fail
  (`47217889`: the documented one-command acceptance script applies a patch file that
  is absent from the commit, RUNBOOK commands hardcode the author's home directory and
  silently import zero specs, and the state client imports symbols only powercontext
  1.2.0 exports while pinning nothing).
- `47217673` was judged pass with `code_change: blocked`: the image has no JDK/Maven and
  the install/tests budgets were not bypassed, so the Java before/after could not run;
  the patch chain, regression content and model records were verified statically.
- Five callbacks first arrived seconds past the platform deadline and were refused by
  the default guard. With explicit operator authorization they were retried unchanged
  under `--allow-expired`; all five were acknowledged, so the platform had not
  reassigned them. One retry hit a proxy error and was resent identically.
- `47216911` was re-claimed after its first lease expired and reached the same commit
  (`f9bd19bf`). It was then run three times: the first at 2 GiB sat at 1.81/2.0 GiB with
  2549 cgroup `max` events and heavy CPU throttling for 14 min without a report; a
  4 GiB rerun was clean (no `max` events) but its 20-minute ceiling was too short.
  139 bash calls at roughly 5 s per model turn had not reached the report stage, and the
  deadline produced only a runtime_guard infrastructure checkpoint, not a substantive
  report. No verdict was submitted; the task returns to the queue. The lesson is that
  this submission needs 4 GiB **and** a lease claimed immediately before a >=30 min run,
  not a 20-minute ceiling.
- The credential heuristic in `prepare.py` produced one verified false positive: a
  submission vendored the JSON-Schema test suite, whose URI format fixtures contain
  `http://user:pass@example.com` vectors (10 files, matched by the content regex only;
  no filename rule fired). Staging was refused by default. An explicit
  `--acknowledge-fixture-secrets` flag now skips ONLY that content heuristic, still
  enforces filename and size rules, and records `credential_scan:
  acknowledged_fixture` in the staged manifest so the exception is auditable.
- `batch5big-run` (2026-10-30) combined the retried large task with four new ones:
  5/5 reports valid, wall clock ~25 min at 5 concurrent. Verdicts: 4 pass, 1 fail
  (`47216911` — documented server-mode `sddctx init` fails on a fresh database with
  MySQL 1049 because the code connects before creating the schema; its model record
  also reports `elapsed_ms: 0` with usage taken from an unrelated session log).
- `47219174` (668 files, 458 tests, mostly a vendored JSON-Schema suite) was reviewed
  directly by the operator instead of a Harbor trial, after the credential heuristic
  quarantined it and the lease setup left no room for a queued run. Reproduced with
  Python 3.11 and its exact pinned wheels: 82 module tests, `init` ingesting
  32 specs / 155 code / 366 tests into an embedded seekdb, PowerContext state save ->
  server restart -> `show-progress` returning `complete` in a new process, and a
  red/green check where reverting `target-project.diff` failed all 10 target tests and
  reapplying passed all 10 with the 1 regression test unchanged. The live MiniMax call
  was not replayed; the supplied record is file-level consistent but is not proof.
- `batch7y-run` (2026-10-30): 7/7 reports valid in 19 min at 7 concurrent (3 tasks at
  4 GiB, 4 at 2 GiB, one staged through the fixture-credential acknowledgement).
  Verdicts: 6 pass, 1 fail (`47219307`: its client reads `hit["text"]` while
  powercontext 1.4.0 returns `hit["memory"]["text"]`, `cmd_context` fills state only
  from the local mirror, and its shipped `pc_scope_id.txt` points at the author's
  scope so `state save` aborts with an unhandled 404 on a fresh server).
- `47219528` passed with a disclosed packaging defect: `scripts/run_e2e.sh` never
  sources `.env`, so the documented one-shot path aborts on an unbound
  `POWERCONTEXT_URL`, while every individual CLI step, the red/green suite and the
  cross-process state round-trip reproduced.
- `batch7z-run` (2026-10-30): 7/7 completed with 0 errors in ~23 min at 7 concurrent
  (three 4 GiB tasks including a 394-file submission). All seven were judged pass.
  `47219954` could not be auto-passed because its report emitted `task_id` as a string,
  so the deterministic gate refused it even though five dimensions were verified; that
  is a report-format defect, not a functional one. Its pinned PowerContext
  `1.2.0.dev0` source commit needs a container runtime that the image lacks, so the
  released 1.4.0 was substituted and disclosed.
- No automatic platform claim/callback. No private artifacts uploaded or committed.

## Historical 2026-10-28 pilot record

Operational updates: [current state](../docs/当前状态.md) / [runbook](../docs/运行手册.md).

This records one oracle and two model-backed reviews of empty synthetic negative
fixtures, not production judging accuracy. No OB tasks were claimed or submitted
**by these Harbor runs**. The separate manual judge path had reviewed a real task;
see the current-state page. No real contestant source was sent to the model here.

## Tested versions

Harbor 0.25.0; Pi 1.4.0; Node 22.23.2; Python image 3.11.15-slim-bookworm.
Model: MiniMax-M3.1-Flash using the dedicated M Plan key and
`https://api.minimax.cn/anthropic`. Harbor uses `anthropic/` as the model protocol
prefix; actual inference is MiniMax, not Anthropic. Only the variable name
`OB_EVAL_MODEL_KEY` is in configuration; no value was passed in CLI arguments.
The dedicated local secret file is `~/.config/minimax-ob-judge/mplan.env` (0600).
The runner forwards only OB_EVAL_MODEL_KEY, not its Pi-native alias MINIMAX_CN_API_KEY.

## Runtime evidence (private, gitignored)

| Run under runs/harbor/ | Outcome |
| --- | --- |
| oracle-none-20261028-145456 | Finished, no trial exception, reward/report_valid=1. Docker verifier inspected with NetworkMode=none. Oracle made no model calls. |
| pi-preinstalled-20261028-150017 | Real Pi/model run, report_valid=1, recommendation=review; all six checks blocked; 20 referenced evidence files (24 files produced in the evidence directory). Too much unnecessary inspection for an empty fixture. |
| pi-lean-20261028-150753 | Real Pi/model run after prompt tightening, report_valid=1, recommendation=review; all six checks not_run; 8 evidence files. No fake pass. |

The final trial's agent execution lasted approximately 170 seconds (setup/build
and verifier excluded). Harbor reported:

| Metric | First Pi run | Revised Pi run |
| --- | ---: | ---: |
| Input tokens, including cache | 496439 | 316840 |
| Cached input tokens | 462217 | 288747 |
| Noncached input tokens (difference) | 34222 | 28093 |
| Output tokens | 17813 | 16467 |
| Monetary cost | unavailable | unavailable |

Do not infer subscription quota consumption or currency cost from these token
counts. This is one empty negative fixture, not a throughput or accuracy study.
It is still expensive for the amount of work; optimize before scaling.

## Fixes discovered by actual execution

1. Debian HTTP and official HTTPS package index downloads stalled. Image-only
   configurable HTTPS USTC mirror completed the dependency step; signatures remain
   enabled, timeout/retries bounded. No host mirror/Docker changes.
2. Harbor no-network enforcement requires kernel capabilities unavailable here.
   A separate verifier Compose service now uses Docker network_mode:none. This is
   Docker-specific and must be validated again for any other sandbox provider.
3. Built-in Pi installation downloaded nvm from GitHub raw and failed with a
   connection reset. Node and Pi are preinstalled in the task image;
   pi_preinstalled.py overrides only installation and preserves native Pi run,
   sessions, trajectory conversion and usage accounting.
4. Harbor's model parser does not recognize minimax-cn. Use the explicit
   Anthropic-compatible protocol and MiniMax endpoint with unchanged model ID.
5. First Agent attempted a redacted environment-variable inventory. Tightened
   instructions forbid env/printenv/process-environment/credential inspection.
   In the revised trajectory, regex matches were shebangs, file glob patterns,
   and explicit statements that it did not inspect env, not actual env dumping.

## Tests and privacy checks

- 24 Harbor-specific offline tests passed.
- Existing judge regression harness: 185 assertions passed, no FAIL, no skip.
- Harbor lint of generated review-6 task: no findings.
- Dedicated key exact-byte scan across 237 local pilot/source/log files: 0 matches.
  This is not proof against encoded exfiltration or malicious future submissions.
- Host Pi auth/config was not mounted. Platform token and PAT were not forwarded.
- At this historical checkpoint, Harbor code was uncommitted. Public code was later
  committed; private output directories remain ignored and must not be uploaded.

## Remaining gates

- Real workload concurrency is unvalidated. Current single-task service uses1;
  batch caps are4 total/2 backend, with each child Harbor forced to1. See
  [capacity plan](../docs/Harbor并发评测方案.md) and [runner](../docs/批量评测运行器.md).
- The official M Plan documentation exposes RPM/TPM and 5-hour/weekly windows, not a fixed concurrency
  number. Do not infer a single-key concurrency limit from the Go/Explore/Build tier name.

- Repeatable prewarmed real-submission reviews with live seekdb/PowerContext
  (individual operator follow-up has passed; automated4-way capacity has not).
- Positive, negative, stale-spec, fake-log, skipped-test and prompt-injection cases.
- Prevent reviewer code execution from accessing model master credentials; prefer
  per-trial short-lived gateway keys or an external model loop.
- Independent trustworthy execution evidence. Current verifier validates report
  consistency, NOT correctness; reward=1 must never map directly to 20 points.
- Fixed official rubric, manual review policy, lease-aware worker queue, frozen
  callback outbox/ACK reconciliation, capacity and M Plan quota validation.
  The manual judge path also needs request freezing and ACK reconciliation; this
  is not only a production-scale requirement (see judge/README.md).
