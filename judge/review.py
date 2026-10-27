#!/usr/bin/env python3
"""人工审阅两阶段流程：先领取并留证，人工看过代码后再回传。

为什么需要它
------------
通过标准尚未冻结，所以不能自动判分；但结论必须**看到代码之后**才能下，
而代码要**领取之后**才有。旧的 ``run_judge.py --mode manual`` 要求事先给
``--override-file``，是个死结，而且 ``judge()`` 抛出的 ``NotImplementedError``
没被捕获——领取后崩溃、不回传、``finally`` 还会把检出目录删掉。

本模块把它拆成两步，中间留给人：

    # 阶段 1：领取 → 拉取 → 采证 → 保留检出 → 写 pending → **不回传**
    python3 judge/review.py fetch

    # （人工审阅 workdir/task-<id>/ 与 workdir/task-<id>.evidence.json）

    # 阶段 2：核对版本、核对时限 → 回传
    python3 judge/review.py submit --task-id <id> --verdict pass --reason "..."
    python3 judge/review.py submit --task-id <id> --verdict fail --reason "..."

    # 随时查看哪些任务还悬着、剩多少时间
    python3 judge/review.py list

铁律
----
- 领取有副作用：拿到 taskId 即进入执行中，**必须在超时前回传**。
  ``fetch`` 会打印硬截止时间，``list`` 会按剩余时间排序并标红将超时的。
- 回传不可逆（平台拒绝改分），所以 ``submit`` 要求显式 ``--verdict``，
  没有默认值，也不提供批量接口。
- ``submit`` 重新核对磁盘上的检出 commit 与 pending 记录一致，
  防止"看的是 A 提交、回传的是 B 提交"。
- 已回传的任务拒绝二次回传；已过截止时间的任务拒绝回传（提示改为失败回传或人工处理）。
- 阶段 1 任何一步异常都**不删现场**，并明确提示任务仍悬着。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from evaluate import DEFAULT_SCOPE_DIR, collect_evidence, summarize, write_evidence  # noqa: E402
from ob_client import (  # noqa: E402
    FAIL_OTHER,
    OBJudgeClient,
    OBProtocolError,
    OBRemoteError,
    Task,
    _parse_task,
)
from repo_fetch import RepoFetchError, clone_and_checkout, redact  # noqa: E402

DEFAULT_QUESTION_ID = 600077
ROOT = Path(__file__).parent.parent
DEFAULT_WORKDIR = ROOT / "workdir"
DEFAULT_RUNLOG = ROOT / "runs" / "judge.jsonl"
# 距平台超时留出的安全余量（秒）。回传本身要时间，网络也可能重试。
SAFETY_SECONDS = 180

PENDING_SUFFIX = ".pending.json"
SUBMITTED_SUFFIX = ".submitted.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log(path: Path, event: str, **fields: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": _now_iso(), "event": event, **fields}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(redact(json.dumps(rec, ensure_ascii=False, default=str)) + "\n")


def _pending_path(workdir: Path, task_id: int) -> Path:
    return workdir / f"task-{task_id}{PENDING_SUFFIX}"


def _submitted_path(workdir: Path, task_id: int) -> Path:
    return workdir / f"task-{task_id}{SUBMITTED_SUFFIX}"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """先写临时文件再 rename，避免写一半崩溃留下损坏的状态文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _fmt_remaining(seconds: float) -> str:
    if seconds <= 0:
        over = int(-seconds)
        return f"已超时 {over // 60} 分 {over % 60} 秒" if over >= 60 else f"已超时 {over} 秒"
    return f"剩 {int(seconds // 60)} 分 {int(seconds % 60)} 秒"


def _head_commit(repo: Path) -> str:
    """读取检出目录当前 HEAD 的完整 SHA。独立于 repo_fetch，作为二次核验。"""
    out = subprocess.run(
        ["git", "-c", "credential.helper=", "rev-parse", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, timeout=30, check=False,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
    )
    return out.stdout.strip().lower() if out.returncode == 0 else ""


def _client_from_env(args: argparse.Namespace, workdir: Path) -> OBJudgeClient:
    token = os.environ.get(args.token_env, "").strip()
    if not token:
        print(
            f"错误：环境变量 {args.token_env} 未设置。\n"
            f"  export {args.token_env}='<MiniMax Token>'\n"
            "Token 绝不能写进代码、命令行或本仓库（本仓库是 public）。",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return OBJudgeClient(
        token=token,
        question_id=args.question_id,
        timeout_minutes=args.timeout_minutes,
        retry=args.retry,
        claim_dump_dir=workdir / "claims",
    )


# ─────────────────────────── 阶段 1：fetch ───────────────────────────


def cmd_fetch(args: argparse.Namespace) -> int:
    workdir = args.workdir.resolve()
    runlog = args.runlog.resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    # 防呆：已有悬着的任务就不再领新的。领取是有副作用的，
    # 在手上还有未回传任务时再领，只会放大悬空面。
    #
    # 只统计**还没过平台硬超时**的。已超时的任务平台已放回队列，submit 对它必然被拒，
    # 本地又没有清除它的命令——若仍算「悬着」，一次超时就会永久拦住所有后续 fetch。
    # 它们的 pending 文件保留以备查，`list` 里仍显示为 🔴。
    now = time.time()
    all_pending = _list_pending(workdir)
    live = [p for p in all_pending if p.get("platform_deadline_epoch", 0) > now]
    stale = [p for p in all_pending if p.get("platform_deadline_epoch", 0) <= now]
    if stale:
        print(f"提示：忽略 {len(stale)} 个已超过平台超时的 pending "
              f"（taskId={', '.join(str(p['task_id']) for p in stale)}）——"
              "平台已将它们放回队列；本地记录保留以备查。")
    if live and not args.allow_multiple:
        print("拒绝领取：手上仍有未回传的任务，请先处理（或加 --allow-multiple 明确放行）：", file=sys.stderr)
        for p in live:
            print(f"  taskId={p['task_id']} {_fmt_remaining(p['deadline_epoch'] - time.time())}",
                  file=sys.stderr)
        return 3

    client = _client_from_env(args, workdir)
    _log(runlog, "review_fetch_start", question_id=args.question_id,
         timeout_minutes=args.timeout_minutes)

    try:
        task = client.claim()
    except OBRemoteError as exc:
        _log(runlog, "claim_error", code=exc.code, error=str(exc))
        print(f"领取失败 code={exc.code}: {exc}", file=sys.stderr)
        return 1
    except OBProtocolError as exc:
        _log(runlog, "claim_protocol_error", error=str(exc))
        print(f"领取协议异常: {exc}", file=sys.stderr)
        return 1

    if task is None:
        print("暂无任务（code=200, data=null），这不是错误。稍后再试，建议间隔 10~30 秒以上。")
        return 0

    claimed_epoch = task.claimed_at or time.time()
    platform_deadline = claimed_epoch + client.timeout_minutes * 60
    safe_deadline = platform_deadline - SAFETY_SECONDS
    checkout_dir = workdir / f"task-{task.task_id}"

    # 尽早落一个 pending 记录——哪怕后面 clone 就崩了，也留得下"领过它"的凭据。
    pending: dict[str, Any] = {
        "task_id": task.task_id,
        "question_id": task.question_id,
        "player": task.player,
        "group_id": task.group_id,
        "branch_requested": task.branch,
        "commit_requested": task.commit_id,
        "sub_questions": [s.sub_question_id for s in task.sub_questions],
        "max_score": task.max_score,
        "claimed_at": datetime.fromtimestamp(claimed_epoch, timezone.utc).isoformat(timespec="seconds"),
        "claimed_epoch": claimed_epoch,
        "platform_deadline_epoch": platform_deadline,
        "deadline_epoch": safe_deadline,
        "timeout_minutes": client.timeout_minutes,
        "stage": "claimed",
        "checkout_dir": str(checkout_dir),
    }
    _atomic_write_json(_pending_path(workdir, task.task_id), pending)
    _log(runlog, "review_claimed", task=task.describe(), git_repo=redact(task.git_repo))

    print("=" * 66)
    print(f"已领取 taskId={task.task_id}  player={task.player}")
    print(f"  任务已进入「执行中」。必须在 {client.timeout_minutes} 分钟内回传，")
    print(f"  建议截止：{datetime.fromtimestamp(safe_deadline).strftime('%H:%M:%S')}"
          f"（{_fmt_remaining(safe_deadline - time.time())}）")
    print("=" * 66)

    # ── 拉取 ──
    try:
        checkout = clone_and_checkout(
            task.git_repo, checkout_dir, branch=task.branch, commit_id=task.commit_id,
        )
    except RepoFetchError as exc:
        pending.update(stage="fetch_failed", fetch_fail_msg=exc.fail_msg, fetch_error=str(exc))
        _atomic_write_json(_pending_path(workdir, task.task_id), pending)
        _log(runlog, "review_fetch_failed", task_id=task.task_id, fail_msg=exc.fail_msg, error=str(exc))
        print(f"\n拉取失败（failMsg={exc.fail_msg}）：{exc}")
        print("任务仍悬着。无法评测时请回传失败：")
        print(f"  python3 judge/review.py submit --task-id {task.task_id} "
              f"--verdict error --fail-msg {exc.fail_msg}")
        return 4

    # ── 采证 ──
    scope = args.scope_dir or None
    evidence = collect_evidence(
        checkout.path, commit_id=checkout.commit_id, branch=checkout.branch, scope_dir=scope,
    )
    evidence.update(task_id=task.task_id, player=task.player, group_id=task.group_id)
    evidence_path = workdir / f"task-{task.task_id}.evidence.json"
    write_evidence(evidence, evidence_path)

    pending.update(
        stage="awaiting_review",
        branch=checkout.branch,
        commit_id=checkout.commit_id,
        scope_dir=scope,
        evidence_path=str(evidence_path),
        evidence_summary=summarize(evidence),
    )
    _atomic_write_json(_pending_path(workdir, task.task_id), pending)
    _log(runlog, "review_ready", task_id=task.task_id, branch=checkout.branch,
         commit_id=checkout.commit_id, summary=summarize(evidence))

    print(f"\n检出：{checkout.path}")
    print(f"  branch = {checkout.branch}")
    print(f"  commit = {checkout.commit_id}")
    print(f"证据：{evidence_path}")
    print(f"摘要：{summarize(evidence)}")
    if evidence.get("scope_dir_missing"):
        print(f"\n⚠️ 选手仓库里没有 {scope}/ 目录——这条提交很可能是空交付。")
    elif evidence.get("substantive_file_count") == 0:
        print(f"\n⚠️ {scope}/ 下只有占位文件，没有实质内容。")
    print("\n人工审阅完成后回传（回传不可逆，没有默认值）：")
    print(f"  python3 judge/review.py submit --task-id {task.task_id} --verdict pass|fail --reason '…'")
    return 0


# ─────────────────────────── 阶段 2：submit ───────────────────────────


def cmd_submit(args: argparse.Namespace) -> int:
    workdir = args.workdir.resolve()
    runlog = args.runlog.resolve()
    task_id = args.task_id

    if _submitted_path(workdir, task_id).exists():
        done = json.loads(_submitted_path(workdir, task_id).read_text(encoding="utf-8"))
        print(f"拒绝：taskId={task_id} 已回传过（{done.get('verdict')} @ {done.get('submitted_at')}）。"
              "平台不接受改分。", file=sys.stderr)
        return 5

    pend_path = _pending_path(workdir, task_id)
    if not pend_path.exists():
        print(f"拒绝：找不到 {pend_path.name}。只能回传由 `review.py fetch` 领取并留证的任务。",
              file=sys.stderr)
        return 5
    pending = json.loads(pend_path.read_text(encoding="utf-8"))

    claim_path = workdir / "claims" / f"claim-{task_id}.json"
    if not claim_path.exists():
        print(f"拒绝：找不到落盘的领取响应 {claim_path.name}，无法还原任务。", file=sys.stderr)
        return 5

    remaining = pending["platform_deadline_epoch"] - time.time()
    if remaining <= 0:
        print(f"拒绝：taskId={task_id} 已超过平台超时（{_fmt_remaining(remaining)}），"
              "任务可能已被重新领取，此时回传可能被拒或覆盖他人结果。\n"
              "  请先 `review.py list` 并联系 OB 确认任务状态。", file=sys.stderr)
        return 6

    # 还原 Task。claim 文件里的 gitRepo 已脱敏（***），所以只用它来取 subQuestions
    # 等结构字段，回传不需要仓库地址。
    raw = json.loads(claim_path.read_text(encoding="utf-8"))
    task = _parse_task(raw)

    client = _client_from_env(args, workdir)

    # ── 错误回传：clone/评测无法完成 ──
    if args.verdict == "error":
        fail_msg = args.fail_msg if args.fail_msg is not None else FAIL_OTHER
        try:
            client.report_failure(task, fail_msg)
        except (OBProtocolError, OBRemoteError) as exc:
            _log(runlog, "review_submit_error_rejected", task_id=task_id, error=str(exc))
            print(f"回传被拒：{exc}", file=sys.stderr)
            return 7
        _finish(workdir, runlog, pending, verdict="error", reason=args.reason or "",
                extra={"fail_msg": fail_msg})
        print(f"已回传失败 taskId={task_id} failMsg={fail_msg}")
        return 0

    # ── 正常回传：必须有人工审阅过的检出 ──
    if pending.get("stage") != "awaiting_review":
        print(f"拒绝：任务阶段为 {pending.get('stage')}，没有完成采证，不能按 pass/fail 回传。\n"
              "  无法评测时用 --verdict error。", file=sys.stderr)
        return 5

    checkout_dir = Path(pending["checkout_dir"])
    if not checkout_dir.is_dir():
        print(f"拒绝：检出目录已不存在（{checkout_dir}）。没有现场就没有依据，不能回传结论。",
              file=sys.stderr)
        return 5

    # 二次核验：磁盘上现在的 HEAD 必须就是 fetch 时记录的 commit，
    # 防止"看的是 A、回传的是 B"。
    head_now = _head_commit(checkout_dir)
    if head_now != pending["commit_id"].lower():
        print("拒绝：检出目录 HEAD 与 fetch 时记录的 commit 不一致。\n"
              f"  记录：{pending['commit_id']}\n  当前：{head_now or '(读取失败)'}\n"
              "  审阅依据与回传版本不一致，请重新 fetch 或确认现场未被改动。", file=sys.stderr)
        return 5

    if not (args.reason or "").strip():
        print("拒绝：必须提供 --reason。二值结论不可逆，没有理由将无法申诉与复核。", file=sys.stderr)
        return 5

    passed = args.verdict == "pass"
    sub_ids = [s.sub_question_id for s in task.sub_questions]
    verdicts = {sid: passed for sid in sub_ids}
    messages = {sid: args.reason.strip()[:480] for sid in sub_ids}

    try:
        client.report_success(
            task, branch=pending["branch"], commit_id=pending["commit_id"],
            verdicts=verdicts, messages=messages,
        )
    except (OBProtocolError, OBRemoteError) as exc:
        _log(runlog, "review_submit_rejected", task_id=task_id, verdict=args.verdict, error=str(exc))
        print(f"回传被拒：{exc}\n任务状态未变，仍可修正后重试（注意截止时间）。", file=sys.stderr)
        return 7

    _finish(workdir, runlog, pending, verdict=args.verdict, reason=args.reason.strip(),
            extra={"branch": pending["branch"], "commit_id": pending["commit_id"],
                   "sub_questions": sub_ids})
    print(f"已回传 taskId={task_id} 结论={'通过' if passed else '不通过'} "
          f"commit={pending['commit_id'][:12]}")
    return 0


def _finish(workdir: Path, runlog: Path, pending: dict[str, Any], *, verdict: str,
            reason: str, extra: dict[str, Any]) -> None:
    """回传成功后：落 submitted 记录并移走 pending，避免之后被误当成悬着的任务。"""
    task_id = pending["task_id"]
    record = {
        "task_id": task_id,
        "player": pending.get("player"),
        "verdict": verdict,
        "reason": reason,
        "submitted_at": _now_iso(),
        "evidence_summary": pending.get("evidence_summary"),
        **extra,
    }
    _atomic_write_json(_submitted_path(workdir, task_id), record)
    _pending_path(workdir, task_id).unlink(missing_ok=True)
    _log(runlog, "review_submitted", task_id=task_id, verdict=verdict, reason=reason, **extra)


# ─────────────────────────── list ───────────────────────────


def _list_pending(workdir: Path) -> list[dict[str, Any]]:
    out = []
    for p in sorted(workdir.glob(f"task-*{PENDING_SUFFIX}")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda r: r.get("deadline_epoch", 0))


def cmd_list(args: argparse.Namespace) -> int:
    workdir = args.workdir.resolve()
    pend = _list_pending(workdir)
    done = sorted(workdir.glob(f"task-*{SUBMITTED_SUFFIX}"))
    print(f"悬着（未回传）: {len(pend)}    已回传: {len(done)}")
    for r in pend:
        left = r["deadline_epoch"] - time.time()
        hard = r["platform_deadline_epoch"] - time.time()
        flag = "🔴" if hard <= 0 else ("🟠" if left <= 0 else "🟢")
        print(f"  {flag} taskId={r['task_id']} player={r.get('player')} stage={r.get('stage')} "
              f"{_fmt_remaining(left)}（平台硬超时：{_fmt_remaining(hard)}）")
        if r.get("evidence_summary"):
            print(f"       {r['evidence_summary']}")
    for p in done:
        d = json.loads(p.read_text(encoding="utf-8"))
        print(f"  ✔ taskId={d['task_id']} {d['verdict']} @ {d['submitted_at']}  {d.get('reason', '')[:60]}")
    return 0


# ─────────────────────────── CLI ───────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="人工审阅两阶段判题：fetch → 人看 → submit")
    p.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    p.add_argument("--runlog", type=Path, default=DEFAULT_RUNLOG)
    p.add_argument("--token-env", default="OB_JUDGE_TOKEN")
    p.add_argument("--question-id", type=int,
                   default=int(os.environ.get("OB_QUESTION_ID", DEFAULT_QUESTION_ID)))
    p.add_argument("--timeout-minutes", type=int, default=30,
                   help="runTaskTimeOutMinutes：须长于「人工审阅 + 回传」的总耗时")
    p.add_argument("--retry", type=int, default=3)
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="领取一条任务并拉取、采证（不回传）")
    f.add_argument("--scope-dir", default=DEFAULT_SCOPE_DIR,
                   help=f"只评测该子目录（默认 {DEFAULT_SCOPE_DIR}）；传空字符串扫全仓")
    f.add_argument("--allow-multiple", action="store_true",
                   help="手上已有未回传任务时仍继续领取（默认拒绝）")
    f.set_defaults(func=cmd_fetch)

    s = sub.add_parser("submit", help="人工结论后回传")
    s.add_argument("--task-id", type=int, required=True)
    s.add_argument("--verdict", required=True, choices=["pass", "fail", "error"],
                   help="pass/fail=子题通过/不通过（taskStatus=2）；error=无法评测（taskStatus=3）")
    s.add_argument("--reason", default="", help="判定理由（pass/fail 必填，写入平台子题 message）")
    s.add_argument("--fail-msg", type=int, default=None,
                   help="仅 --verdict error 用：1 branch 缺失/2 commit 缺失/4 clone 失败/100 其他")
    s.set_defaults(func=cmd_submit)

    ls = sub.add_parser("list", help="列出悬着与已回传的任务及剩余时间")
    ls.set_defaults(func=cmd_list)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
