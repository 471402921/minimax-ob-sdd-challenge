# Harbor pilot validation — 2026-10-28

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
- New Harbor code and private output directories remain uncommitted; no upload.

## Remaining gates

- Concurrency is intentionally unvalidated. Keep `n_concurrent_trials=1` until dependency images,
  PowerContext/seekdb service isolation, timeout budgets and M Plan rate limits are measured. See
  `docs/Harbor并发评测方案.md`.
- The official M Plan documentation exposes RPM/TPM and 5-hour/weekly windows, not a fixed concurrency
  number. Do not infer a single-key concurrency limit from the Go/Explore/Build tier name.

- Real-submission review with live seekdb/PowerContext backends (trial-local only).
- Positive, negative, stale-spec, fake-log, skipped-test and prompt-injection cases.
- Prevent reviewer code execution from accessing model master credentials; prefer
  per-trial short-lived gateway keys or an external model loop.
- Independent trustworthy execution evidence. Current verifier validates report
  consistency, NOT correctness; reward=1 must never map directly to 20 points.
- Fixed official rubric, manual review policy, lease-aware worker queue, frozen
  callback outbox/ACK reconciliation, capacity and M Plan quota validation.
  The manual judge path also needs request freezing and ACK reconciliation; this
  is not only a production-scale requirement (see judge/README.md).
