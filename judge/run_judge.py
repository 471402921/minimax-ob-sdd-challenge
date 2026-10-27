#!/usr/bin/env python3
"""OB 判题主循环：领取 → 拉取 → 评测 → 回传。

    # 联调：只处理一条任务，走完整往返
    python3 judge/run_judge.py --once --mode stub-pass

    # 本地验证：不接触平台，只对本地仓库采证据
    python3 judge/run_judge.py --local-repo /path/to/repo

    # 正式判题不要用本脚本，请用 judge/review.py（两阶段人工审阅）

设计约束（来自 OB 文档与判题安全）：
- **串行领取**。领取有副作用，超额领取会撞上 30 分钟超时重领，
  导致两个进程同时处理同一 taskId。
- **不把领取接口当连通性探测**。data=null 只表示本轮无任务。
- 接近超时会主动回传失败，而不是静默过期让任务被别人重领。
- 本地 runlog 记录全部证据；平台只收一个布尔值，细粒度结论留在这里。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from evaluate import (  # noqa: E402
    DEFAULT_SCOPE_DIR,
    Judgement,
    Verdict,
    collect_evidence,
    judge,
    summarize,
    write_evidence,
)
from ob_client import (  # noqa: E402
    FAIL_OTHER,
    OBJudgeClient,
    OBProtocolError,
    OBRemoteError,
    Task,
)
from repo_fetch import RepoFetchError, clone_and_checkout, redact  # noqa: E402

DEFAULT_QUESTION_ID = 600077
DEFAULT_WORKDIR = Path(__file__).parent.parent / "workdir"
DEFAULT_RUNLOG = Path(__file__).parent.parent / "runs" / "judge.jsonl"
# 留出余量：超时后任务可被别人重领，宁可自己先判失败
_TIMEOUT_SAFETY_MARGIN = 0.8


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunLog:
    """本地结构化运行日志。写入前统一脱敏。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: Any) -> None:
        record = {"ts": _now(), "event": event, **fields}
        line = redact(json.dumps(record, ensure_ascii=False, default=str))
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _resolve_verdicts(
    task: Task, judgement: Judgement, override: dict[str, Any] | None
) -> dict[int, bool]:
    """把 override 覆盖到评测结论上，并校验子题集合完全一致。"""
    verdicts = {v.sub_question_id: v.passed for v in judgement.verdicts}
    if not override:
        return verdicts
    forced = {int(k): bool(v) for k, v in (override.get("verdicts") or {}).items()}
    unknown = set(forced) - {s.sub_question_id for s in task.sub_questions}
    if unknown:
        raise OBProtocolError(f"override 含不属于本任务的子题: {sorted(unknown)}")
    missing = {s.sub_question_id for s in task.sub_questions} - set(forced)
    if missing:
        raise OBProtocolError(f"override 未覆盖全部子题，缺: {sorted(missing)}")
    return forced


def process_task(
    client: OBJudgeClient,
    task: Task,
    *,
    mode: str,
    workdir: Path,
    runlog: RunLog,
    override: dict[str, Any] | None = None,
    keep_checkout: bool = False,
    scope_dir: str | None = None,
) -> str:
    """处理一条已领取的任务。返回结果描述。

    无论成功失败都必须回传，否则任务会一直挂在执行中直到超时被重领。
    """
    deadline = task.claimed_at + client.timeout_minutes * 60 * _TIMEOUT_SAFETY_MARGIN
    subs = [(s.sub_question_id, s.test_case) for s in task.sub_questions]
    checkout_dir = workdir / f"task-{task.task_id}"
    evidence_path = workdir / f"task-{task.task_id}.evidence.json"

    runlog.write("claimed", task=task.describe(), git_repo=redact(task.git_repo))

    try:
        # ── 拉取 ──
        try:
            checkout = clone_and_checkout(
                task.git_repo, checkout_dir,
                branch=task.branch, commit_id=task.commit_id,
            )
        except RepoFetchError as exc:
            runlog.write("fetch_failed", task_id=task.task_id, fail_msg=exc.fail_msg, error=str(exc))
            client.report_failure(task, exc.fail_msg)
            runlog.write("reported_failure", task_id=task.task_id, fail_msg=exc.fail_msg)
            return f"拉取失败 failMsg={exc.fail_msg}"

        runlog.write(
            "checked_out", task_id=task.task_id,
            branch=checkout.branch, commit_id=checkout.commit_id,
        )

        # ── 采集证据 ──
        evidence = collect_evidence(
            checkout.path, commit_id=checkout.commit_id, branch=checkout.branch,
            scope_dir=scope_dir,
        )
        evidence["task_id"] = task.task_id
        evidence["player"] = task.player
        evidence["group_id"] = task.group_id
        write_evidence(evidence, evidence_path)
        runlog.write("evidence", task_id=task.task_id, summary=summarize(evidence),
                     path=str(evidence_path))

        if time.time() > deadline:
            runlog.write("timeout", task_id=task.task_id)
            client.report_failure(task, FAIL_OTHER)
            return "超过评测时限，主动回传失败避免被重领"

        # ── 评测 ──
        judgement = judge(checkout.path, subs, mode=mode, evidence=evidence)
        if judgement.is_execution_failure:
            client.report_failure(task, judgement.fail_msg or FAIL_OTHER)
            runlog.write("reported_failure", task_id=task.task_id,
                         fail_msg=judgement.fail_msg)
            return f"评测无法完成 failMsg={judgement.fail_msg}"

        verdicts = _resolve_verdicts(task, judgement, override)
        messages = {v.sub_question_id: v.message for v in judgement.verdicts if v.message}
        scores = {
            v.sub_question_id: v.score
            for v in judgement.verdicts
            if v.score is not None
        }
        if override and "scores" in override:
            scores.update({int(k): float(v) for k, v in override["scores"].items()})

        client.report_success(
            task, branch=checkout.branch, commit_id=checkout.commit_id,
            verdicts=verdicts, messages=messages, scores=scores or None,
        )
        result = "通过" if all(verdicts.values()) else "不通过"
        runlog.write(
            "reported_success", task_id=task.task_id, result=result,
            verdicts={str(k): v for k, v in verdicts.items()},
            branch=checkout.branch, commit_id=checkout.commit_id,
        )
        return f"评测完成：{result} {verdicts}"

    except OBProtocolError as exc:
        # 我方拼装问题：不回传（会再被平台拒），记录后人工介入
        runlog.write("protocol_error", task_id=task.task_id, error=str(exc))
        return f"协议错误，未回传（需人工处理）: {exc}"
    except OBRemoteError as exc:
        runlog.write("remote_error", task_id=task.task_id, code=exc.code,
                     retryable=exc.retryable, error=str(exc))
        return f"平台拒绝 code={exc.code}: {exc}"
    finally:
        if not keep_checkout and checkout_dir.exists():
            shutil.rmtree(checkout_dir, ignore_errors=True)


def run_loop(
    client: OBJudgeClient,
    *,
    mode: str,
    workdir: Path,
    runlog: RunLog,
    once: bool,
    poll_min: float,
    poll_max: float,
    override: dict[str, Any] | None,
    keep_checkout: bool,
    scope_dir: str | None = None,
) -> int:
    processed = 0
    while True:
        try:
            task = client.claim()
        except OBRemoteError as exc:
            runlog.write("claim_error", code=exc.code, retryable=exc.retryable, error=str(exc))
            print(f"[{_now()}] 领取失败 code={exc.code}: {exc}", flush=True)
            if not exc.retryable:
                return 1
            time.sleep(poll_max)
            continue
        except OBProtocolError as exc:
            runlog.write("claim_protocol_error", error=str(exc))
            print(f"[{_now()}] 领取协议异常: {exc}", flush=True)
            return 1

        if task is None:
            # 正常情况：本轮无可领任务。文档建议 10~30s 后重试，不要高频空转。
            print(f"[{_now()}] 暂无任务，等待中…", flush=True)
            if once:
                return 0
            time.sleep(random.uniform(poll_min, poll_max))
            continue

        print(f"[{_now()}] 领到任务 {task.describe()}", flush=True)
        outcome = process_task(
            client, task, mode=mode, workdir=workdir, runlog=runlog,
            override=override, keep_checkout=keep_checkout, scope_dir=scope_dir,
        )
        processed += 1
        print(f"[{_now()}] {outcome}", flush=True)

        if once:
            return 0
        # 串行：处理完再领，避免超领撞超时重领
        time.sleep(random.uniform(1.0, 3.0))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="OB 数据库大赛 MiniMax 赛题判题程序")
    p.add_argument("--token-env", default="OB_JUDGE_TOKEN",
                   help="读取 Token 的环境变量名（不要把 Token 写进命令行）")
    p.add_argument("--question-id", type=int,
                   default=int(os.environ.get("OB_QUESTION_ID", DEFAULT_QUESTION_ID)))
    p.add_argument("--timeout-minutes", type=int, default=30,
                   help="runTaskTimeOutMinutes，必须长于正常评测耗时")
    p.add_argument("--retry", type=int, default=3, help="runTaskRetry")
    p.add_argument("--mode", default="manual",
                   choices=["manual", "stub-pass", "stub-fail", "stub-error"],
                   help="评测模式。默认 manual：只采证据，拒绝自动判定")
    p.add_argument("--once", action="store_true", help="只处理一条任务后退出（联调用）")
    p.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    p.add_argument("--runlog", type=Path, default=DEFAULT_RUNLOG)
    p.add_argument("--override-file", type=Path,
                   help="人工判定结果 JSON：{\"verdicts\":{\"800514\":true}}")
    p.add_argument("--keep-checkout", action="store_true",
                   help="保留选手仓库检出（默认评测后删除）")
    p.add_argument("--poll-min", type=float, default=10.0)
    p.add_argument("--poll-max", type=float, default=30.0)
    p.add_argument("--scope-dir", default=DEFAULT_SCOPE_DIR,
                   help=f"只评测该子目录（五家共用 scaffold 仓库，默认 {DEFAULT_SCOPE_DIR}）；"
                        "传空字符串则扫全仓")
    p.add_argument("--local-repo", type=Path,
                   help="只对本地仓库采证据，完全不接触平台")
    args = p.parse_args(argv)

    workdir = args.workdir.resolve()
    runlog = RunLog(args.runlog.resolve())

    # ── 本地模式：不领取、不回传，用于验证证据采集 ──
    if args.local_repo:
        ev = collect_evidence(args.local_repo.resolve(), scope_dir=args.scope_dir or None)
        print(summarize(ev))
        out = workdir / "local.evidence.json"
        write_evidence(ev, out)
        print(f"证据已写入 {out}")
        return 0

    token = os.environ.get(args.token_env, "").strip()
    if not token:
        print(
            f"错误：环境变量 {args.token_env} 未设置。\n"
            f"  export {args.token_env}='<MiniMax Token>'\n"
            "Token 绝不能写进代码、命令行或本仓库（本仓库是 public）。",
            file=sys.stderr,
        )
        return 2

    override = None
    if args.override_file:
        override = json.loads(args.override_file.read_text(encoding="utf-8"))

    if args.mode == "manual":
        # 死结 + 悬空任务风险：结论必须看到代码后才能下，代码又要领取后才有，
        # 所以 --override-file 根本无法事先给出；且过去 judge() 的 NotImplementedError
        # 未被捕获，会在领取后崩溃、回传 0 次、finally 删掉检出目录。
        # 所以在**领取之前**就拒绝，绝不让它碰平台。
        print(
            "拒绝运行：--mode manual 已废弃，它会在领取后崩溃并留下悬空任务。\n"
            "  正式判题请用两阶段流程（先领取留证、人工看过代码、再回传）：\n"
            "    python3 judge/review.py fetch\n"
            "    python3 judge/review.py submit --task-id <id> --verdict pass|fail --reason '…'\n"
            "  联调自测请显式使用 --mode stub-pass / stub-fail / stub-error。",
            file=sys.stderr,
        )
        return 2

    workdir.mkdir(parents=True, exist_ok=True)
    client = OBJudgeClient(
        token=token, question_id=args.question_id,
        timeout_minutes=args.timeout_minutes, retry=args.retry,
        # 领取响应先落盘：响应丢失就既无法回传也无法查清领了谁的任务
        claim_dump_dir=workdir / "claims",
    )

    print(f"[{_now()}] 判题启动 questionId={args.question_id} mode={args.mode} "
          f"scope={args.scope_dir or '全仓'} "
          f"timeout={args.timeout_minutes}min once={args.once}", flush=True)
    runlog.write("startup", question_id=args.question_id, mode=args.mode, once=args.once,
                 scope_dir=args.scope_dir or None)

    try:
        return run_loop(
            client, mode=args.mode, workdir=workdir, runlog=runlog, once=args.once,
            poll_min=args.poll_min, poll_max=args.poll_max,
            override=override, keep_checkout=args.keep_checkout,
            scope_dir=args.scope_dir or None,
        )
    except KeyboardInterrupt:
        runlog.write("interrupted")
        print("\n已中断。注意：若中断时有任务在执行中，它会挂到超时后被重领。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
