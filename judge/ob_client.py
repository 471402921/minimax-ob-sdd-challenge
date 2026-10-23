"""OB 数据库大赛判题平台对接客户端。

协议依据：OB 方《数据库大赛联调相关文档》(2026-10-21)，campId=21。

要点（与文档严格对齐）：
- 鉴权走 JSON body 的 ``token`` 字段，不用 Bearer header，也不传 evaluatorCode。
- 领取 ``queryOneTaskRecord`` **有副作用**：返回 taskId 即代表任务已转入执行中，
  因此绝不能拿它做连通性探测。
- 回传 ``synchronizeTaskRecord`` 必须一次覆盖 ``subQuestions`` 全部子题，
  不允许分批回传。
- HTTP 200 不代表成功，必须检查响应体里的业务 ``code``。
- CASES 协议禁止顶层 score / message，说明要放进子题项。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from repo_fetch import redact

BASE_URL = "https://openwebapi.oceanbase.com/api/techcamp/testplatform/taskrecord"
CLAIM_PATH = f"{BASE_URL}/queryOneTaskRecord"
SYNC_PATH = f"{BASE_URL}/synchronizeTaskRecord"

# 文档 3.5 节的 failMsg 枚举
FAIL_BRANCH_NOT_FOUND = 1
FAIL_COMMIT_NOT_FOUND = 2
FAIL_COMPILE = 3
FAIL_CLONE = 4
FAIL_CLONE_TIMEOUT = 5
FAIL_RESET = 6
FAIL_CMAKE = 7
FAIL_VERIFY = 8
FAIL_OTHER = 100

FAIL_MSG_TEXT = {
    FAIL_BRANCH_NOT_FOUND: "branch 未找到",
    FAIL_COMMIT_NOT_FOUND: "commit 未找到",
    FAIL_COMPILE: "编译失败",
    FAIL_CLONE: "git clone 失败",
    FAIL_CLONE_TIMEOUT: "git clone 超时",
    FAIL_RESET: "git reset 失败",
    FAIL_CMAKE: "cmake 失败",
    FAIL_VERIFY: "验证失败",
    FAIL_OTHER: "其他错误",
}

# 完整 SHA：40 位（SHA-1）或 64 位（SHA-256）十六进制
_FULL_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHORT_SHA_RE = re.compile(r"^[0-9a-f]{7,39}$")

# taskStatus：2 = 评测完成，3 = 执行失败
TASK_DONE = 2
TASK_FAILED = 3
# result_status：1 = 通过，2 = 不通过（与 taskStatus 数字含义不同）
CASE_PASS = 1
CASE_FAIL = 2

# 不应对其无限重试的业务 code（文档第五节）
_NON_RETRYABLE = {400, 403, 409}


class OBProtocolError(RuntimeError):
    """我方请求不合约：拼装错误、子题缺失、commit 格式错等。不该重试。"""

    def __init__(self, message: str, *, response: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.response = response or {}


class OBRemoteError(RuntimeError):
    """平台侧拒绝或异常：可能可重试，取决于 code。"""

    def __init__(self, message: str, *, code: int | None, response: dict[str, Any]) -> None:
        super().__init__(message)
        self.code = code
        self.response = response

    @property
    def retryable(self) -> bool:
        # 503 = 报名队伍校验暂时不可用；500 = 平台配置异常。两者都值得退避重试。
        return self.code not in _NON_RETRYABLE


@dataclass(frozen=True)
class SubQuestion:
    sub_question_id: int
    test_case: str
    max_score: float


@dataclass(frozen=True)
class Task:
    """一条领取到的评测任务。"""

    task_id: int
    question_id: int
    camp_id: int | None
    group_id: int | None
    player: str
    git_repo: str
    branch: str | None
    commit_id: str | None
    submission_type: str
    result_format: str
    result_type: int
    max_score: float
    sub_questions: tuple[SubQuestion, ...]
    extra_params: dict[str, Any] = field(default_factory=dict)
    current_retry: str = "0"
    claimed_at: float = 0.0

    @property
    def is_numeric_scoring(self) -> bool:
        """resultType 2 = 数值积分，通过项必须带 score。"""
        return self.result_type == 2

    def describe(self) -> str:
        """可安全写日志的摘要——绝不包含 git_repo 原值（内含 PAT）。"""
        return (
            f"taskId={self.task_id} questionId={self.question_id} "
            f"player={self.player} groupId={self.group_id} "
            f"branch={self.branch} subQuestions="
            f"{[s.sub_question_id for s in self.sub_questions]} "
            f"resultType={self.result_type} maxScore={self.max_score} "
            f"retry={self.current_retry}"
        )


def _as_float(value: Any, default: float = 0.0) -> float:
    """maxScore 文档示例是字符串 "30"，实际可能给数字，两种都兼容。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_task(data: dict[str, Any]) -> Task:
    subs = tuple(
        SubQuestion(
            sub_question_id=int(item["subQuestionId"]),
            test_case=str(item.get("testCase") or ""),
            max_score=_as_float(item.get("maxScore")),
        )
        for item in (data.get("subQuestions") or [])
    )
    if not subs:
        raise OBProtocolError(f"taskId={data.get('taskId')} 的 subQuestions 为空，无法回传")

    return Task(
        task_id=int(data["taskId"]),
        question_id=int(data.get("questionId") or 0),
        camp_id=data.get("campId"),
        group_id=data.get("groupId"),
        player=str(data.get("player") or ""),
        git_repo=str(data.get("gitRepo") or ""),
        branch=data.get("branch") or None,
        commit_id=data.get("commitId") or None,
        submission_type=str(data.get("submissionType") or ""),
        result_format=str(data.get("resultFormat") or ""),
        result_type=int(data.get("resultType") or 1),
        max_score=_as_float(data.get("maxScore")),
        sub_questions=subs,
        extra_params=dict(data.get("extraParams") or {}),
        current_retry=str(data.get("currentRetry") or "0"),
        claimed_at=time.time(),
    )


def _check_business_code(payload: dict[str, Any], what: str) -> dict[str, Any]:
    """HTTP 200 不代表成功，必须看业务 code。"""
    code = payload.get("code")
    if code == 200:
        return payload
    message = payload.get("message")
    # 拼上 traceId，方便按文档第五节向平台反馈
    trace = payload.get("traceId") or payload.get("requestId")
    suffix = f" traceId={trace}" if trace else ""
    raise OBRemoteError(f"{what}失败: code={code} message={message}{suffix}", code=code, response=payload)


class OBJudgeClient:
    """领取任务 + 回传成绩。所有请求都做业务 code 校验。"""

    def __init__(
        self,
        token: str,
        *,
        question_id: int,
        timeout_minutes: int = 30,
        retry: int = 3,
        connect_timeout: float = 5.0,
        read_timeout: float = 30.0,
        session: requests.Session | None = None,
        claim_dump_dir: Path | None = None,
    ) -> None:
        if not token:
            raise ValueError("token 不能为空：请通过环境变量注入，不要硬编码")
        if timeout_minutes <= 0:
            raise ValueError("runTaskTimeOutMinutes 必须大于 0")
        if retry < 0:
            raise ValueError("runTaskRetry 不能为负")
        self._token = token
        self.question_id = question_id
        self.timeout_minutes = timeout_minutes
        self.retry = retry
        self._timeout = (connect_timeout, read_timeout)
        self._session = session or requests.Session()
        self._claim_dump_dir = claim_dump_dir

    def __repr__(self) -> str:  # 防止 token 被打进异常栈或日志
        return f"OBJudgeClient(question_id={self.question_id}, token=***)"

    def _persist_raw_claim(self, data: dict[str, Any]) -> None:
        """完整保存领取响应（脱敏后），先落盘再解析。

        血的教训：响应只做摘要而未落盘时，一旦出问题既无法回传
        （需要 taskId + gitRepo + 完整 commit），也无法查清领了谁的任务。
        """
        if self._claim_dump_dir is None:
            return
        try:
            self._claim_dump_dir.mkdir(parents=True, exist_ok=True)
            raw = json.loads(redact(json.dumps(data, ensure_ascii=False)))
            out = self._claim_dump_dir / f"claim-{raw.get('taskId', 'unknown')}.json"
            out.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass  # 落盘失败不应阻断判题

    def _post(self, url: str, body: dict[str, Any], what: str) -> dict[str, Any]:
        try:
            resp = self._session.post(
                url,
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise OBRemoteError(f"{what}网络异常: {exc}", code=None, response={}) from exc
        try:
            payload = resp.json()
        except ValueError as exc:
            raise OBRemoteError(
                f"{what}返回非 JSON: HTTP {resp.status_code} body={resp.text[:300]}",
                code=None,
                response={},
            ) from exc
        return _check_business_code(payload, what)

    def claim(self) -> Task | None:
        """领取一条 MiniMax 一级题任务。

        返回 None 表示本轮无可领任务（业务 code 200 + data null），
        这是正常情况，不是错误。**调用即产生副作用**——拿到 taskId 后
        必须评测并回传，不能丢弃。
        """
        body = {
            "token": self._token,
            "questionId": self.question_id,
            "runTaskTimeOutMinutes": self.timeout_minutes,
            "runTaskRetry": self.retry,
        }
        payload = self._post(CLAIM_PATH, body, "领取任务")
        data = payload.get("data")
        if data is None:
            return None
        # 先把完整响应落盘再做任何解析：响应一旦丢失就既无法回传
        # （回传需要 taskId + 完整 commit）也无法查清领了谁的任务。
        self._persist_raw_claim(data)
        task = _parse_task(data)
        if task.question_id and task.question_id != self.question_id:
            raise OBProtocolError(
                f"领取到非本赛题任务: 期望 questionId={self.question_id} 实际={task.question_id}"
            )
        return task

    def _build_case_results(
        self,
        task: Task,
        verdicts: dict[int, bool],
        messages: dict[int, str] | None = None,
        scores: dict[int, float] | None = None,
    ) -> list[dict[str, Any]]:
        """按领取响应的 subQuestions 组装完整结果，强制全覆盖。"""
        messages = messages or {}
        scores = scores or {}
        unknown = set(verdicts) - {s.sub_question_id for s in task.sub_questions}
        if unknown:
            raise OBProtocolError(f"回传了不属于本任务的子题 ID: {sorted(unknown)}")

        cases: list[dict[str, Any]] = []
        for sub in task.sub_questions:
            if sub.sub_question_id not in verdicts:
                raise OBProtocolError(
                    f"子题 {sub.sub_question_id} 缺少评测结论；"
                    f"成功回传必须覆盖全部子题（任务共 {len(task.sub_questions)} 个）"
                )
            passed = verdicts[sub.sub_question_id]
            item: dict[str, Any] = {
                "subQuestionId": sub.sub_question_id,
                "result_status": CASE_PASS if passed else CASE_FAIL,
            }
            if sub.sub_question_id in messages:
                item["message"] = messages[sub.sub_question_id][:500]
            if passed and task.is_numeric_scoring:
                # 数值积分题：通过项必须带 score，否则平台拒绝
                if sub.sub_question_id not in scores:
                    raise OBProtocolError(
                        f"resultType=2 数值积分题，子题 {sub.sub_question_id} 通过时必须提供 score"
                    )
                item["score"] = round(float(scores[sub.sub_question_id]), 3)
            cases.append(item)
        return cases

    @staticmethod
    def _validate_version(task: Task, branch: str, commit_id: str) -> None:
        if not branch or len(branch) > 128:
            raise OBProtocolError(f"branch 非法（长度需 1..128）: {branch!r}")
        if not _FULL_SHA_RE.match(commit_id or ""):
            raise OBProtocolError(f"commitId 必须是完整 40/64 位十六进制 SHA，收到: {commit_id!r}")
        if task.branch and task.branch != branch:
            raise OBProtocolError(
                f"branch 与领取时指定的不一致: 任务要求 {task.branch!r}，实际运行 {branch!r}"
            )
        if task.commit_id:
            want = task.commit_id.lower()
            got = commit_id.lower()
            if _FULL_SHA_RE.match(want):
                if want != got:
                    raise OBProtocolError(f"commitId 与任务指定不一致: 要求 {want} 实际 {got}")
            elif _SHORT_SHA_RE.match(want):
                if not got.startswith(want):
                    raise OBProtocolError(f"commitId 不以任务指定的短 SHA {want} 为前缀: 实际 {got}")
            else:
                raise OBProtocolError(f"任务下发的 commitId 格式无法识别: {task.commit_id!r}")

    def report_success(
        self,
        task: Task,
        *,
        branch: str,
        commit_id: str,
        verdicts: dict[int, bool],
        messages: dict[int, str] | None = None,
        scores: dict[int, float] | None = None,
    ) -> dict[str, Any]:
        """回传评测完成（taskStatus=2）。

        "成功" 指评测已执行完毕，**不代表子题都通过**。子题不通过请用
        result_status=2，不要误报成 taskStatus=3。
        """
        self._validate_version(task, branch, commit_id)
        cases = self._build_case_results(task, verdicts, messages, scores)
        body = {
            "token": self._token,
            "taskId": task.task_id,
            "taskStatus": TASK_DONE,
            "branch": branch,
            "commitId": commit_id.lower(),
            # testCaseResult 是「序列化后的字符串」，不是嵌套数组
            "testCaseResult": json.dumps(cases, ensure_ascii=False, separators=(",", ":")),
        }
        return self._post(SYNC_PATH, body, f"回传成绩 taskId={task.task_id}")

    def report_failure(self, task: Task, fail_msg: int) -> dict[str, Any]:
        """回传无法完成评测（taskStatus=3），如 clone / 编译失败。

        可省略 branch、commitId、testCaseResult。
        注意：某个子题测试不通过属于 taskStatus=2 + result_status=2，
        不该走到这里。
        """
        if fail_msg not in FAIL_MSG_TEXT:
            raise OBProtocolError(f"未知的 failMsg: {fail_msg}")
        body = {
            "token": self._token,
            "taskId": task.task_id,
            "taskStatus": TASK_FAILED,
            "failMsg": fail_msg,
        }
        return self._post(SYNC_PATH, body, f"回传失败 taskId={task.task_id}")
