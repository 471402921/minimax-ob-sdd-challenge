#!/usr/bin/env python3
"""安全连通性探测 —— **保证不会领取任何任务**。

    python3 judge/probe.py

原理：文档 §2.3 明确说明，Token 的可领范围 = 授权赛题 ∩ 请求筛选。
用**不属于我们授权范围的 questionId**（默认 seekdb 的 600069）发起请求时，
两者无交集，服务端必然返回 ``code:200, data:null``，**不可能领到任务**。

用这个来验证连通性、Token 有效性、请求格式，是零风险的。

⚠️ 绝不要用 ``--question-id 600077`` 来"看看有没有任务"——
    那不是查询，是领取。2026-10-23 就是这么误领了一条真实任务的。
    真要领取请走 ``run_judge.py``，它会先落盘完整响应再处理。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ob_client import CLAIM_PATH, SYNC_PATH, OBJudgeClient, OBRemoteError  # noqa: E402
from repo_fetch import redact  # noqa: E402

# 我们被授权的赛题。探测时必须避开，否则等于领取。
AUTHORIZED_QUESTION_ID = 600077
# 无交集的探测 ID：seekdb 的题，MiniMax token 必然取不到
SAFE_PROBE_QUESTION_ID = 600069


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="OB 判题平台安全探测（不领取任务）")
    p.add_argument("--token-env", default="OB_JUDGE_TOKEN")
    p.add_argument("--question-id", type=int, default=SAFE_PROBE_QUESTION_ID,
                   help=f"探测用 questionId，必须不在授权范围内（默认 {SAFE_PROBE_QUESTION_ID}）")
    args = p.parse_args(argv)

    if args.question_id == AUTHORIZED_QUESTION_ID:
        print(
            f"拒绝执行：questionId={AUTHORIZED_QUESTION_ID} 是我们被授权的赛题，"
            "用它调用领取接口会真的领走队列里的任务。\n"
            f"请改用无交集的 ID（如 {SAFE_PROBE_QUESTION_ID}）探测，"
            "或直接用 run_judge.py 走正式判题流程。",
            file=sys.stderr,
        )
        return 2

    token = os.environ.get(args.token_env, "").strip()
    if not token:
        print(f"错误：环境变量 {args.token_env} 未设置。", file=sys.stderr)
        return 2

    print("=" * 58)
    print("OB 判题平台安全探测（不会领取任务）")
    print("=" * 58)

    client = OBJudgeClient(token=token, question_id=args.question_id)
    print(f"探测 questionId = {args.question_id}（与授权范围无交集）")

    try:
        task = client.claim()
    except OBRemoteError as exc:
        print(f"\n✗ 请求被拒：code={exc.code} {redact(str(exc))}")
        print("  → 连通性 OK，但 Token 或请求格式有问题")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ 异常：{redact(str(exc))}")
        return 1

    print(f"  领取端点  {CLAIM_PATH}")
    print(f"  回传端点  {SYNC_PATH}")

    if task is not None:
        # 理论上不可能：授权与筛选无交集时应恒为 null
        print(f"\n⚠️ 异常：竟然返回了任务 {task.describe()}")
        print("  这不符合文档 §2.3 的授权交集规则，请立即联系 OB 核查。")
        return 1

    print("\n✓ 返回 code:200 + data:null —— 符合预期")
    print("  已验证：网络连通、Token 被接受、请求格式正确、授权边界生效")
    print("  未领取任何任务（授权与筛选无交集，服务端不会分配）")
    print("\n下一步：让联调队伍在预发官网提交，再用 run_judge.py 正式领取。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
