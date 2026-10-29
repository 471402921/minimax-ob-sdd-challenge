#!/usr/bin/env python3
"""只读能力查询：GET /submissionOptions?questionId=…

来源：OB 对接 skill（psiace/powercontext-2026 分支 2026-competition-platform-review，
skills/techcamp-platform-integration）。协议要点：
  - 不带 Token、不领取任务，也**不能**证明领取/回传权限；
  - 不是队列长度，查不到积压数；
  - 预发官网与生产 API 共用任务，不能据此判断是否为隔离环境。

用途：评审前确认题目仍可提测，并核对满分 / 题型有没有被平台改过
（满分从 30 → 20 就发生过，联调记录 §6.2）。偏离预期只警告，不阻断。

退出码：0 一致；1 与预期有偏差（仅警告）；2 请求失败或响应不合协议。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "https://openwebapi.oceanbase.com/api/techcamp/testplatform/taskrecord"
QUESTION_ID = 600077
# 与联调记录、review.sh 一致的预期；平台改了要人工确认，而不是静默沿用
EXPECT = {"submissionType": "CODE", "resultFormat": "CASES", "maxScore": "20"}


def fetch(question_id: int = QUESTION_ID, timeout: float = 20.0) -> dict:
    url = f"{BASE}/submissionOptions?questionId={question_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "ob-judge-check"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 固定 https 地址
        payload = json.load(resp)
    if payload.get("code") != 200 or not isinstance(payload.get("data"), dict):
        raise ValueError(f"业务 code={payload.get('code')} message={payload.get('message')}")
    data = payload["data"]
    if data.get("questionId") != question_id:
        raise ValueError(f"响应的 questionId={data.get('questionId')} 与请求 {question_id} 不符")
    return data


def diff_expected(data: dict) -> list[str]:
    problems = []
    if not data.get("managed"):
        problems.append("managed=false：平台未托管该题")
    if not data.get("available"):
        problems.append("available=false：该题当前不可提测")
    for key, want in EXPECT.items():
        got = data.get(key)
        if str(got) != want:
            problems.append(f"{key}={got!r}，预期 {want!r}（平台可能改了配置，回传前先确认）")
    return problems


def main() -> int:
    try:
        data = fetch()
    except (urllib.error.URLError, ValueError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"  ✗ 能力查询失败：{exc}", file=sys.stderr)
        return 2
    keys = ("questionId", "managed", "available", "submissionType", "resultFormat", "maxScore")
    print("  " + " ".join(f"{k}={data.get(k)}" for k in keys))
    problems = diff_expected(data)
    for p in problems:
        print(f"  ⚠ {p}", file=sys.stderr)
    if not problems:
        print("  ✓ 题目可提测，满分与题型符合预期")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
