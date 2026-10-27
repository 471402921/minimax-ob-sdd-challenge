"""安全与协议回归测试：PAT 不落盘、不进 argv、不进日志；子题全覆盖；commit 校验。

    python3 judge/test_harness.py

这些断言针对的是**会造成不可逆后果**的路径（凭证泄露、误回传、格式被平台拒绝），
所以做成可复跑的检查，而不是一次性验证。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from evaluate import collect_evidence, summarize  # noqa: E402
from ob_client import (  # noqa: E402
    CASE_FAIL,
    CASE_PASS,
    OBJudgeClient,
    OBProtocolError,
    SubQuestion,
    Task,
)
from repo_fetch import (  # noqa: E402
    RepoFetchError,
    clone_and_checkout,
    parse_repo_url,
    redact,
)

SENTINEL_PAT = "ghp_SENTINEL_MUST_NEVER_APPEAR_0123456789"
RAW_URL = f"https://471402921:{SENTINEL_PAT}@github.com/471402921/minimax-ob-sdd-challenge.git"
# 用于验证认证隔离的私有仓库。公开仓库测不出 credential helper 劫持。
# 可用 OB_JUDGE_PROBE_REPO 覆盖；仓库不存在时该网络断言会跳过（见 skip）。
PRIVATE_PROBE_REPO = os.environ.get(
    "OB_JUDGE_PROBE_REPO", "github.com/471402921/ob-judge-askpass-probe"
)

_failures: list[str] = []
_skips: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        _failures.append(label)


def skip(label: str, why: str) -> None:
    """跳过某项检查。与 FAIL 区分开：环境不具备 ≠ 代码有问题。"""
    print(f"  skip {label} —— {why}")
    _skips.append(f"{label} ({why})")


def test_url_parsing() -> None:
    print("\n[1] gitRepo 解析")
    creds = parse_repo_url(RAW_URL)
    check("干净 URL 不含凭证", SENTINEL_PAT not in creds.clean_url, creds.clean_url)
    check("干净 URL 正确", creds.clean_url == "https://github.com/471402921/minimax-ob-sdd-challenge.git")
    check("用户名解析正确", creds.username == "471402921")
    check("PAT 解析正确", creds.pat == SENTINEL_PAT)
    check("repr 不泄露 PAT", SENTINEL_PAT not in repr(creds), repr(creds))

    # 选手可能漏填凭证：不该在这里炸，留给 clone 去失败并映射 failMsg
    plain = parse_repo_url("https://github.com/foo/bar.git")
    check("无凭证 URL 也能解析", plain.pat == "" and plain.username == "")

    for bad, why in [("", "空地址"), ("ftp://x/y", "非法 scheme"), ("https://github.com/", "缺仓库路径")]:
        try:
            parse_repo_url(bad)
            check(f"拒绝{why}", False)
        except RepoFetchError:
            check(f"拒绝{why}", True)


def test_redaction() -> None:
    print("\n[2] 日志脱敏")
    git_stderr = f"fatal: unable to access '{RAW_URL}': Authentication failed"
    out = redact(git_stderr)
    check("git stderr 中 PAT 被脱敏", SENTINEL_PAT not in out, out)
    check("脱敏后仍保留可读 URL", "github.com/471402921" in out, out)

    check("空串安全", redact("") == "")
    check("无凭证文本不被破坏", redact("just a normal message") == "just a normal message")

    try:
        raise RepoFetchError(f"clone 失败: {RAW_URL}", 4)
    except RepoFetchError as exc:
        check("RepoFetchError 消息已脱敏", SENTINEL_PAT not in str(exc), str(exc))


def test_clone_no_credential_persistence() -> None:
    print("\n[3] clone 后凭证不落盘")
    tmp = Path(tempfile.mkdtemp(prefix="ob-judge-test-"))
    try:
        # 公开仓库 + 假 PAT：认证会被忽略，但凭证传递路径与真实场景一致
        checkout = clone_and_checkout(RAW_URL, tmp / "repo", clone_timeout=120)
        check("clone 成功", (checkout.path / ".git").is_dir())
        check("拿到完整 40 位 SHA", len(checkout.commit_id) == 40, checkout.commit_id)

        config = (checkout.path / ".git" / "config").read_text(encoding="utf-8")
        check(".git/config 不含 PAT", SENTINEL_PAT not in config)
        check(".git/config 不含用户名凭证段", "@github.com" not in config, config)

        # 全工作区扫描：任何文件都不该留下 PAT 痕迹。
        # 排除 judge/：那是本测试套件自身，源码里就写着 sentinel 字符串，
        # 扫到属于自指假阳性，不是泄漏。
        leaked = [
            str(p.relative_to(checkout.path))
            for p in checkout.path.rglob("*")
            if p.is_file()
            and "judge" not in p.relative_to(checkout.path).parts
            and SENTINEL_PAT in p.read_bytes().decode("utf-8", "ignore")
        ]
        check("工作区无 PAT 残留（排除 judge/ 自指）", not leaked, str(leaked[:3]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_clone_branch_and_commit() -> None:
    print("\n[4] branch / commit 检出版本")
    tmp = Path(tempfile.mkdtemp(prefix="ob-judge-test-"))
    try:
        base = clone_and_checkout(RAW_URL, tmp / "a", branch="main", clone_timeout=120)
        check("指定 branch 可检出", base.branch == "main", base.branch)

        # 任务指定 commitId 时必须能解析成完整 SHA
        pinned = clone_and_checkout(RAW_URL, tmp / "b", commit_id=base.commit_id, clone_timeout=120)
        check("指定 commit 可检出", pinned.commit_id == base.commit_id)

        short = clone_and_checkout(RAW_URL, tmp / "c", commit_id=base.commit_id[:8], clone_timeout=120)
        check("短 SHA 也能解析为完整 SHA", short.commit_id == base.commit_id)

        try:
            clone_and_checkout(RAW_URL, tmp / "d", branch="no-such-branch-xyz", clone_timeout=120)
            check("不存在的 branch 应失败", False)
        except RepoFetchError as exc:
            check("不存在的 branch 映射 failMsg=1", exc.fail_msg == 1, f"实际={exc.fail_msg}")

        try:
            clone_and_checkout(RAW_URL, tmp / "e", commit_id="0" * 40, clone_timeout=120)
            check("不存在的 commit 应失败", False)
        except RepoFetchError as exc:
            check("不存在的 commit 映射 failMsg=2", exc.fail_msg == 2, f"实际={exc.fail_msg}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _fake_task(*, result_type: int = 1, branch: str | None = None, commit: str | None = None) -> Task:
    return Task(
        task_id=12345678, question_id=600077, camp_id=21, group_id=4286,
        player="team-4286", git_repo=RAW_URL, branch=branch, commit_id=commit,
        submission_type="CODE", result_format="CASES", result_type=result_type, max_score=30.0,
        sub_questions=(SubQuestion(800514, "SDD 上下文管理", 30.0),),
    )


class _CapturingClient(OBJudgeClient):
    """拦截实际 HTTP，只检查我们拼出来的请求体。"""

    def __init__(self) -> None:
        super().__init__(token="fake-token", question_id=600077)
        self.body: dict = {}

    def _post(self, url: str, body: dict, what: str) -> dict:
        self.body = body
        return {"code": 200, "message": "操作成功", "data": {"taskId": body.get("taskId")}}


def test_report_payload() -> None:
    print("\n[5] 回传请求体合约")
    sha = "a" * 40
    client = _CapturingClient()
    task = _fake_task()

    client.report_success(task, branch="main", commit_id=sha, verdicts={800514: True},
                          messages={800514: "验收通过"})
    body = client.body
    check("taskStatus=2 表示评测完成", body["taskStatus"] == 2)
    check("commitId 完整", body["commitId"] == sha)
    check("testCaseResult 是字符串不是数组", isinstance(body["testCaseResult"], str))
    check("无顶层 score", "score" not in body)
    check("无顶层 message", "message" not in body)

    cases = json.loads(body["testCaseResult"])
    check("子题 result_status=1 表示通过", cases[0]["result_status"] == CASE_PASS)
    check("说明写在子题项内", cases[0]["message"] == "验收通过")

    # 子题不通过仍属 taskStatus=2 —— 文档明确警告不要误报成任务失败
    client.report_success(task, branch="main", commit_id=sha, verdicts={800514: False})
    body = client.body
    check("子题不通过仍是 taskStatus=2", body["taskStatus"] == 2)
    check("子题不通过 result_status=2", json.loads(body["testCaseResult"])[0]["result_status"] == CASE_FAIL)

    # 缺子题必须拦住，不能发出不完整回传（平台会 400）
    try:
        client.report_success(task, branch="main", commit_id=sha, verdicts={})
        check("缺子题应被拦截", False)
    except OBProtocolError:
        check("缺子题应被拦截", True)

    # 短 SHA 必须被拒
    try:
        client.report_success(task, branch="main", commit_id="abc1234", verdicts={800514: True})
        check("短 SHA 应被拒", False)
    except OBProtocolError:
        check("短 SHA 应被拒", True)

    # 任务指定了 branch 时不允许跑别的分支
    try:
        client.report_success(_fake_task(branch="release"), branch="main", commit_id=sha,
                              verdicts={800514: True})
        check("branch 不一致应被拒", False)
    except OBProtocolError:
        check("branch 不一致应被拒", True)

    # 任务指定了 commit 时必须一致
    try:
        client.report_success(_fake_task(commit="b" * 40), branch="main", commit_id=sha,
                              verdicts={800514: True})
        check("commit 不一致应被拒", False)
    except OBProtocolError:
        check("commit 不一致应被拒", True)

    # 短 commit 前缀匹配应放行
    client.report_success(_fake_task(commit=sha[:8]), branch="main", commit_id=sha,
                          verdicts={800514: True})
    check("短 commit 前缀匹配放行", client.body["commitId"] == sha)

    client.report_failure(task, 4)
    check("失败回传 taskStatus=3", client.body["taskStatus"] == 3)
    check("失败回传带 failMsg", client.body["failMsg"] == 4)
    check("失败回传省略 commitId", "commitId" not in client.body)
    check("失败回传省略 testCaseResult", "testCaseResult" not in client.body)


def test_numeric_scoring() -> None:
    print("\n[6] resultType=2 数值积分（若平台为 MiniMax 开启）")
    sha = "c" * 40
    client = _CapturingClient()

    client.report_success(_fake_task(result_type=2), branch="main", commit_id=sha,
                          verdicts={800514: True}, scores={800514: 22.5})
    case = json.loads(client.body["testCaseResult"])[0]
    check("通过项带 score", case["score"] == 22.5, str(case))

    # 文档 3.4：数值积分题通过但没 score 会被平台拒绝，必须提前拦
    try:
        client.report_success(_fake_task(result_type=2), branch="main", commit_id=sha,
                              verdicts={800514: True})
        check("数值积分题缺 score 应被拦", False)
    except OBProtocolError:
        check("数值积分题缺 score 应被拦", True)

    # 不通过计 0 分，不需要 score
    client.report_success(_fake_task(result_type=2), branch="main", commit_id=sha,
                          verdicts={800514: False})
    case = json.loads(client.body["testCaseResult"])[0]
    check("不通过无需 score", "score" not in case, str(case))


def test_token_never_leaks() -> None:
    print("\n[7] Token 不经 repr 泄露")
    # 用明显是假的占位值，不要写真实 token 的任何片段（含前缀）
    client = OBJudgeClient(token="FAKE_TOKEN_FOR_REPR_TEST", question_id=600077)
    check("client repr 隐藏 token", "FAKE_TOKEN" not in repr(client), repr(client))

    for bad_kwargs, why in [
        ({"token": "", "question_id": 1}, "空 token"),
        ({"token": "t", "question_id": 1, "timeout_minutes": 0}, "非法超时"),
        ({"token": "t", "question_id": 1, "retry": -1}, "负数重试"),
    ]:
        try:
            OBJudgeClient(**bad_kwargs)
            check(f"拒绝{why}", False)
        except ValueError:
            check(f"拒绝{why}", True)


def test_host_credential_helper_cannot_hijack() -> None:
    """回归：宿主 ~/.gitconfig 里的 credential.helper 不得盖过选手 PAT。

    2026-10-23 真实事故：git 先问 credential.helper，helper 给了凭证就不再调
    askpass。机器上 gh 装的 helper 交出了**我们自己的 token**，导致拉选手私有
    仓库时必然 404（GitHub 对无权访问统一报 "Repository not found"）。

    当时的自测无法发现：用我们自己的 token 拉我们自己的仓库，helper 恰好也能
    满足。**能证伪的测法是用一个完全无效的 PAT —— 它必须失败。**
    """
    print("\n[8] 宿主 credential helper 隔离（事故回归）")

    from repo_fetch import _GIT_ISOLATION_ARGS, _git_env, parse_repo_url

    creds = parse_repo_url(f"https://someuser:{SENTINEL_PAT}@github.com/o/r")
    env = _git_env(creds)
    check("禁用了全局 git 配置", env.get("GIT_CONFIG_GLOBAL") == os.devnull,
          f"实际={env.get('GIT_CONFIG_GLOBAL')}")
    check("禁用了系统 git 配置", env.get("GIT_CONFIG_SYSTEM") == os.devnull)
    check("命令行显式清空 credential.helper",
          "credential.helper=" in _GIT_ISOLATION_ARGS)

    # 决定性断言（确定性，不依赖网络）：隔离后 git 看不到任何 credential helper。
    # 宿主上 gh 装的 helper 是 `!/usr/bin/gh auth git-credential`，若它仍然可见，
    # 就会接管认证并交出我们自己的 token。
    probe = subprocess.run(
        ["git", *_GIT_ISOLATION_ARGS, "config", "--get-all", "credential.https://github.com.helper"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    check("隔离后读不到 github.com 的 credential helper",
          probe.stdout.strip() == "",
          f"仍能读到: {probe.stdout.strip()!r}")

    baseline = subprocess.run(
        ["git", "config", "--get-all", "credential.https://github.com.helper"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if baseline.stdout.strip():
        # 恰好能证明本机存在会被劫持的 helper，这条回归测在这里才有意义
        check("宿主确实存在会被劫持的 helper（回归有效）", True)

    # 网络层断言：无效 PAT 必须失败。
    # ⚠️ 必须用**私有仓库**：公开仓库不需要认证，垃圾 PAT 也能拉通，测不出问题。
    #
    # 探针仓库可能不存在（临时创建、后续被删）。先用我们自己的凭证确认它确实
    # 存在且可访问，否则跳过——否则「仓库已删」会伪装成「测试通过」。
    probe_token = os.environ.get("OB_JUDGE_PROBE_TOKEN") or ""
    if not probe_token:
        try:
            got = subprocess.run(["gh", "auth", "token"], capture_output=True,
                                 text=True, timeout=20, check=False)
            if got.returncode == 0:
                probe_token = got.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass

    if not probe_token:
        skip("无效 PAT 拉私有仓库必须失败", "无可用凭证确认探针仓库存在性")
    else:
        ls = subprocess.run(
            ["git", "ls-remote", "--exit-code", f"https://x:{probe_token}@{PRIVATE_PROBE_REPO}"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if ls.returncode != 0:
            skip("无效 PAT 拉私有仓库必须失败",
                 f"探针仓库不可访问（{PRIVATE_PROBE_REPO}），确定性断言已覆盖隔离逻辑")
        else:
            tmp = Path(tempfile.mkdtemp(prefix="ob-judge-helper-"))
            try:
                bogus = f"https://someuser:ghp_THIS_TOKEN_DOES_NOT_EXIST_0000000000@{PRIVATE_PROBE_REPO}"
                try:
                    clone_and_checkout(bogus, tmp / "r", clone_timeout=120)
                    check("无效 PAT 拉私有仓库必须失败", False,
                          "clone 竟然成功 —— 认证被宿主 helper 接管了")
                except RepoFetchError as exc:
                    check("无效 PAT 拉私有仓库必须失败", True)
                    check("失败映射为 failMsg=4", exc.fail_msg == 4, f"实际={exc.fail_msg}")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)


def test_scope_dir_isolation() -> None:
    """回归：五家共用 scaffold 仓库，评测必须只扫 minimax/ 子目录。

    2026-10-23 联调发现：选手仓库结构是 dify/ gaode/ minimax/ qoder/ stepfun/
    五个目录，每家一个。若扫全仓，别家的实现会被算成我方成绩。
    """
    print("\n[9] 子目录路由隔离（五家共用 scaffold）")

    from evaluate import DEFAULT_SCOPE_DIR

    check("默认 scope 是 minimax", DEFAULT_SCOPE_DIR == "minimax", DEFAULT_SCOPE_DIR)

    tmp = Path(tempfile.mkdtemp(prefix="ob-judge-scope-"))
    try:
        repo = tmp / "repo"
        for d in ("dify", "gaode", "minimax", "qoder", "stepfun"):
            (repo / d).mkdir(parents=True)
            (repo / d / ".gitkeep").write_text("")
        # 别家目录塞满内容，若扫描越界就会被误认为我方实现
        (repo / "dify" / "main.py").write_text(
            "import seekdb, powercontext\nfrom oceanbase import connect\n" * 20
        )
        (repo / "dify" / "openspec").mkdir()
        (repo / "dify" / "openspec" / "spec.md").write_text("# spec\n")
        (repo / "qoder" / "tests").mkdir()
        (repo / "qoder" / "tests" / "test_a.py").write_text("def test_x(): pass\n" * 10)
        subprocess.run(["git", "init", "-q", "."], cwd=repo, check=False,
                       capture_output=True, timeout=60)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=False,
                       capture_output=True, timeout=60)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "init"], cwd=repo, check=False,
                       capture_output=True, timeout=60)

        scoped = collect_evidence(repo, scope_dir="minimax")
        check("scope=minimax 时只看到占位文件", scoped["file_count"] == 1,
              f"实际={scoped['file_count']}")
        check("scope=minimax 时实体文件数为 0", scoped["substantive_file_count"] == 0)
        check("scope=minimax 时不把别家 seekdb 代码算成我方",
              scoped["seekdb_code_file_count"] == 0,
              str(scoped.get("seekdb_code_files")))
        check("scope=minimax 时不把别家测试算成我方", scoped["test_file_count"] == 0)
        check("scope=minimax 时不把别家 spec 算成我方", not scoped["has_spec_material"])

        full = collect_evidence(repo, scope_dir=None)
        check("不限定 scope 时确实会越界（证明该测试有效）",
              full["seekdb_code_file_count"] > 0, "全仓扫描竟然也是 0，测试无区分力")

        # 目录不存在时要明确标出，而不是静默当空仓
        import shutil as _sh
        _sh.rmtree(repo / "minimax")
        missing = collect_evidence(repo, scope_dir="minimax")
        check("scope 目录缺失时明确标记", missing.get("scope_dir_missing") is True)
        check("scope 目录缺失时 file_count=0", missing["file_count"] == 0)
        check("摘要里标出 scope 缺失", "缺失" in summarize(missing), summarize(missing))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_review_two_phase_flow() -> None:
    """回归：人工审阅两阶段流程（fetch 留证不回传 → submit 回传）。

    起因：旧的 ``--mode manual`` 在领取后会因未捕获的 NotImplementedError 崩溃，
    回传 0 次且 finally 删掉检出目录，造成悬空任务。全部打桩，不接触平台。
    """
    print("\n[10] 人工审阅两阶段流程（悬空任务事故回归）")

    import argparse
    import contextlib
    import io
    import time as _time

    import review

    def call(fn, *a, **k):
        """调用并捕获 stderr，返回 (rc, stderr文本)，用来断言「哪一层守卫」在拒绝。"""
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = fn(*a, **k)
        return rc, buf.getvalue()

    import ob_client
    import repo_fetch

    tmp = Path(tempfile.mkdtemp(prefix="ob-judge-review-"))
    try:
        # ── 本地假选手仓库（含 minimax/ 实质内容）──
        src = tmp / "src"
        (src / "minimax").mkdir(parents=True)
        (src / "minimax" / "app.py").write_text("print('hello')\n")
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull}

        def g(*a: str, cwd: Path = src) -> str:
            return subprocess.run(["git", *a], cwd=cwd, env=env, check=True,
                                  capture_output=True, text=True).stdout.strip()

        g("init", "-q", "-b", "main")
        g("add", "-A")
        g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
        good_sha = g("rev-parse", "HEAD")

        # ── 桩：领取返回固定任务；clone 复制本地目录；回传记录入参 ──
        raw_claim = {
            "taskId": 9001, "questionId": 600077, "campId": 21, "groupId": 7,
            "player": "team-7", "gitRepo": "https://u:p@github.com/o/r", "branch": None,
            "commitId": None, "submissionType": "CODE", "resultFormat": "CASES",
            "resultType": 1, "maxScore": "20", "currentRetry": "0", "extraParams": {},
            "subQuestions": [{"subQuestionId": 800514, "testCase": "t", "maxScore": "20"}],
        }
        posts: list[dict] = []
        claim_count = {"n": 0}

        class FakeClient(OBJudgeClient):
            def __init__(self, *a, **k) -> None:
                super().__init__(token="FAKE_TOKEN_FOR_REVIEW_TEST", question_id=600077,
                                 claim_dump_dir=k.get("claim_dump_dir"))

            def claim(self):
                claim_count["n"] += 1
                self._persist_raw_claim(raw_claim)
                t = ob_client._parse_task(raw_claim)
                return t

            def _post(self, url, body, what):
                posts.append(body)
                return {"code": 200, "message": "ok", "data": {"taskId": body.get("taskId")}}

        def fake_clone(raw, dest, **kw):
            shutil.copytree(src, dest)
            return repo_fetch.Checkout(path=dest, branch="main", commit_id=good_sha)

        review._client_from_env = lambda args, workdir: FakeClient(claim_dump_dir=workdir / "claims")
        review.clone_and_checkout = fake_clone

        work = tmp / "work"
        ns = lambda **kw: argparse.Namespace(  # noqa: E731
            workdir=work, runlog=tmp / "r.jsonl", token_env="X", question_id=600077,
            timeout_minutes=30, retry=3, **kw)

        # ── 阶段 1 ──
        rc = review.cmd_fetch(ns(scope_dir="minimax", allow_multiple=False))
        check("fetch 成功返回 0", rc == 0, f"rc={rc}")
        check("fetch 阶段不回传任何东西", posts == [], f"posts={posts}")
        check("fetch 保留检出目录（供人工审阅）", (work / "task-9001").is_dir())
        pend = json.loads((work / "task-9001.pending.json").read_text())
        check("pending 记录阶段为 awaiting_review", pend["stage"] == "awaiting_review")
        check("pending 记录了完整 commit", pend["commit_id"] == good_sha)
        check("pending 记录了硬截止时间", pend["platform_deadline_epoch"] > _time.time())
        check("pending 截止早于平台超时（留了安全余量）",
              pend["deadline_epoch"] < pend["platform_deadline_epoch"])
        check("领取响应已先行落盘", (work / "claims" / "claim-9001.json").exists())

        # 防呆：手上已有悬着任务时，不再领新的
        # ❗ 必须把调用包起来：守卫一旦失效，第二次 fetch 会被放行去 clone，
        # 擞上已存在的 task-9001 目录抛 FileExistsError —— 整个测试函数中途死掉，
        # 后面所有断言都不再执行，_failures 里反而是 0。这正是这条断言最该报警的场景。
        try:
            rc2 = review.cmd_fetch(ns(scope_dir="minimax", allow_multiple=False))
        except Exception as exc:  # noqa: BLE001
            rc2 = f"守卫失效，第二次 fetch 被放行并抛 {type(exc).__name__}"
        check("有悬着任务时拒绝再次领取", rc2 == 3, f"rc={rc2}")
        check("  └ 拒绝发生在 claim 之前（claim 次数不增）", claim_count["n"] == 1,
              f"claim 被调用 {claim_count['n']} 次")
        check("拒绝领取时没有产生任何网络调用", posts == [])

        # ── 阶段 2：各类应被拒绝的情形（都不得产生回传）──
        sub = lambda **kw: argparse.Namespace(  # noqa: E731
            workdir=work, runlog=tmp / "r.jsonl", token_env="X", question_id=600077,
            timeout_minutes=30, retry=3, task_id=9001, fail_msg=None, **kw)

        rc = review.cmd_submit(sub(verdict="pass", reason=""))
        check("缺 --reason 被拒", rc == 5 and posts == [], f"rc={rc}")

        rc = review.cmd_submit(sub(verdict="pass", reason="  "))
        check("纯空白 --reason 被拒", rc == 5 and posts == [], f"rc={rc}")

        # 篡改检出：HEAD 与记录不一致 → 必须拒绝（防止看 A 回传 B）
        co = work / "task-9001"
        (co / "minimax" / "extra.py").write_text("x=1\n")
        subprocess.run(["git", "add", "-A"], cwd=co, env=env, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "tamper"],
                       cwd=co, env=env, check=True, capture_output=True)
        rc = review.cmd_submit(sub(verdict="pass", reason="看过了"))
        check("检出 HEAD 被改动后拒绝回传", rc == 5 and posts == [], f"rc={rc}")
        subprocess.run(["git", "reset", "-q", "--hard", good_sha], cwd=co, env=env,
                       check=True, capture_output=True)

        # 已过平台超时 → 拒绝
        p_path = work / "task-9001.pending.json"
        orig = p_path.read_text()
        expired = json.loads(orig)
        expired["platform_deadline_epoch"] = _time.time() - 5
        p_path.write_text(json.dumps(expired))
        rc = review.cmd_submit(sub(verdict="pass", reason="看过了"))
        check("已超时任务拒绝回传", rc == 6 and posts == [], f"rc={rc}")
        p_path.write_text(orig)

        # 隔离「检出目录须存在」：目录被删后，若没有这层守卫会落到「HEAD 不一致」，
        # 诊断含糊（看上去像被篡改，实际是现场丢失）。要求给出「没有现场」这个准确原因。
        co_bak = work / "task-9001.bak"
        shutil.move(str(work / "task-9001"), str(co_bak))
        rc, err = call(review.cmd_submit, sub(verdict="pass", reason="看过了"))
        check("检出目录丢失时拒绝回传", rc == 5 and posts == [], f"rc={rc}")
        check("  └ 且明确指出是「检出目录不存在」而非含糊的 HEAD 不一致",
              "检出目录已不存在" in err, err.strip()[:70])
        shutil.move(str(co_bak), str(work / "task-9001"))

        # 隔离「必须由 fetch 领取并留证」：pending 缺失时，若没有这层守卫会落到「找不到领取响应」，
        # 诊断同样含糊。要求给出「只能回传由 fetch 领取的任务」这个准确原因。
        p_path.rename(work / "task-9001.pending.bak")
        rc, err = call(review.cmd_submit, sub(verdict="pass", reason="看过了"))
        check("pending 缺失时拒绝回传", rc == 5 and posts == [], f"rc={rc}")
        check("  └ 且明确指出「只能回传由 fetch 领取并留证的任务」",
              "只能回传由" in err, err.strip()[:70])
        (work / "task-9001.pending.bak").rename(p_path)

        # ── 正常回传 ──
        rc = review.cmd_submit(sub(verdict="fail", reason="minimax/ 内无实质实现"))
        check("人工 fail 回传成功", rc == 0, f"rc={rc}")
        check("仅产生 1 次回传", len(posts) == 1, f"n={len(posts)}")
        body = posts[0]
        check("taskStatus=2（评测完成）", body["taskStatus"] == 2)
        check("commitId 为完整 SHA 且等于审阅版本", body["commitId"] == good_sha)
        case = json.loads(body["testCaseResult"])[0]
        check("fail → result_status=2", case["result_status"] == 2)
        check("理由写入子题 message", "无实质实现" in case["message"])
        check("无顶层 score/message", "score" not in body and "message" not in body)

        # 回传后：pending 清除、submitted 落盘，且拒绝二次回传
        check("回传后 pending 已清除", not p_path.exists())
        check("回传后 submitted 已落盘", (work / "task-9001.submitted.json").exists())
        rc, err = call(review.cmd_submit, sub(verdict="pass", reason="改主意了"))
        check("已回传任务拒绝二次回传（不可逆）", rc == 5 and len(posts) == 1, f"rc={rc}")
        check("  └ 且是「已回传」守卫在拦（而非别的守卫遮挡）", "已回传过" in err, err.strip()[:60])
        # 专门隔离：pending 残留时（如恢复备份），只有 submitted 守卫能拦住二次回传
        p_path.write_text(orig)
        rc, err = call(review.cmd_submit, sub(verdict="pass", reason="改主意了"))
        check("pending 残留时 submitted 守卫仍能拦住二次回传",
              rc == 5 and len(posts) == 1 and "已回传过" in err, f"rc={rc} {err.strip()[:60]}")
        p_path.unlink(missing_ok=True)

        # ── error 路径：无法评测 → taskStatus=3 ──
        raw_claim["taskId"] = 9002
        rc = review.cmd_fetch(ns(scope_dir="minimax", allow_multiple=False))
        check("悬着任务清空后可再次领取", rc == 0)
        n_before = len(posts)
        sub2 = lambda **kw: argparse.Namespace(  # noqa: E731
            workdir=work, runlog=tmp / "r.jsonl", token_env="X", question_id=600077,
            timeout_minutes=30, retry=3, task_id=9002, **kw)
        rc = review.cmd_submit(sub2(verdict="error", reason="", fail_msg=4))
        check("error 回传成功", rc == 0, f"rc={rc}")
        eb = posts[n_before]
        check("error → taskStatus=3 且带 failMsg", eb["taskStatus"] == 3 and eb["failMsg"] == 4)
        check("error 回传不带 commitId/testCaseResult",
              "commitId" not in eb and "testCaseResult" not in eb)

        # ── fetch 阶段 clone 失败：不得删现场，任务应被标记为悬着 ──
        raw_claim["taskId"] = 9003
        def boom(raw, dest, **kw):
            raise repo_fetch.RepoFetchError("git clone 失败", 4)
        review.clone_and_checkout = boom
        n_before = len(posts)
        rc = review.cmd_fetch(ns(scope_dir="minimax", allow_multiple=False))
        check("clone 失败时 fetch 返回非 0", rc == 4, f"rc={rc}")
        check("clone 失败时 fetch 不擅自回传", len(posts) == n_before)
        pend3 = json.loads((work / "task-9003.pending.json").read_text())
        check("clone 失败的任务仍记为悬着（stage=fetch_failed）", pend3["stage"] == "fetch_failed")
        rc = review.cmd_submit(argparse.Namespace(
            workdir=work, runlog=tmp / "r.jsonl", token_env="X", question_id=600077,
            timeout_minutes=30, retry=3, task_id=9003, verdict="pass", reason="硬过", fail_msg=None))
        check("未采证的任务不能按 pass 回传", rc == 5 and len(posts) == n_before, f"rc={rc}")

        # 隔离「阶段守卫」：补上检出目录与匹配的 HEAD，使「检出不存在」「HEAD 不一致」两层都放行，
        # 此时只有「阶段必须是 awaiting_review」能拦。否则它会被后面的守卫遮挡而成为死代码。
        shutil.copytree(src, work / "task-9003")
        pend3 = json.loads((work / "task-9003.pending.json").read_text())
        pend3["commit_id"] = good_sha
        pend3["branch"] = "main"
        (work / "task-9003.pending.json").write_text(json.dumps(pend3))
        rc, err = call(review.cmd_submit, argparse.Namespace(
            workdir=work, runlog=tmp / "r.jsonl", token_env="X", question_id=600077,
            timeout_minutes=30, retry=3, task_id=9003, verdict="pass", reason="硬过", fail_msg=None))
        check("检出与 HEAD 都就绪但阶段为 fetch_failed 时仍拒绝",
              rc == 5 and len(posts) == n_before, f"rc={rc}")
        check("  └ 且是「阶段」守卫在拦", "没有完成采证" in err, err.strip()[:60])

        # ── 已超平台硬超时的 pending 不应永久拦住新的 fetch ──
        # 起因：任务超时后平台已放回队列，submit 对它必然被拒，本地又没有清除它的命令。
        # 若仍算「悬着」，一次审阅超时就会永久拦住所有后续 fetch。
        # 先用「未超时」做对照，证明这里确实会拦，再改成已超时断言不再拦——
        # 否则断言可能因为别的原因恰好通过。
        review.clone_and_checkout = fake_clone
        raw_claim["taskId"] = 9004
        claims_before = claim_count["n"]
        rc, err = call(review.cmd_fetch, ns(scope_dir="minimax", allow_multiple=False))
        check("对照：未超时的 pending（9003）仍然拦住新的 fetch", rc == 3, f"rc={rc}")
        check("  └ 且拦在 claim 之前", claim_count["n"] == claims_before)

        p9003 = work / "task-9003.pending.json"
        stale = json.loads(p9003.read_text())
        stale["platform_deadline_epoch"] = _time.time() - 600
        stale["deadline_epoch"] = _time.time() - 780
        p9003.write_text(json.dumps(stale))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = review.cmd_fetch(ns(scope_dir="minimax", allow_multiple=False))
        check("已超时的 pending 不再拦住新的 fetch", rc == 0, f"rc={rc}")
        check("  └ 确实领取了新任务（claim 被调用）", claim_count["n"] == claims_before + 1)
        check("  └ 提示了忽略的过期 pending", "忽略 1 个已超过平台超时" in buf.getvalue()
              and "9003" in buf.getvalue(), buf.getvalue()[:80])
        check("  └ 过期 pending 文件保留以备查", p9003.exists())
        check("  └ 新任务的 pending 已写入", (work / "task-9004.pending.json").exists())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_deprecated_manual_mode_refuses_before_claim() -> None:
    """回归：废弃的 --mode manual 必须在**领取之前**就拒绝运行。

    它曾在领取后因未捕获的 NotImplementedError 崩溃，回传 0 次，
    finally 还删掉检出目录——留下一个悬空任务。唯一安全的拒绝时机是碰平台之前。
    """
    print("\n[11] 废弃的 --mode manual 在领取前拒绝")

    import run_judge

    claimed = {"n": 0}
    orig_claim = OBJudgeClient.claim

    def spy_claim(self):  # 若走到这里说明已经碰了平台
        claimed["n"] += 1
        return None

    OBJudgeClient.claim = spy_claim  # type: ignore[method-assign]
    old_tok = os.environ.get("OB_JUDGE_TOKEN")
    os.environ["OB_JUDGE_TOKEN"] = "FAKE_TOKEN_FOR_MANUAL_TEST"
    try:
        import contextlib
        import io

        # 关键：必须覆盖「带了 --override-file」的场景。事故版只在「没给 override」时才拒绝，
        # 一旦给了 override 就会放行、领取、再在 judge() 里崩溃。只测无 override 会漏掉它。
        ov = Path(tempfile.mkdtemp(prefix="ob-judge-ov-")) / "ov.json"
        ov.write_text(json.dumps({"verdicts": {"800514": True}}))
        for argv, label in (
            (["--once"], "默认 mode（即 manual）"),
            (["--once", "--mode", "manual"], "显式 --mode manual"),
            (["--once", "--mode", "manual", "--override-file", str(ov)], "manual + --override-file"),
        ):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                rc = run_judge.main(argv)
            check(f"{label} 被拒（退出码 2）", rc == 2, f"rc={rc}")
            check(f"  └ {label} 在领取之前退出（claim 调用 0 次）", claimed["n"] == 0,
                  f"claim 被调用 {claimed['n']} 次")
            check(f"  └ {label} 的提示指向 review.py", "review.py" in buf.getvalue())
    finally:
        OBJudgeClient.claim = orig_claim  # type: ignore[method-assign]
        if old_tok is None:
            os.environ.pop("OB_JUDGE_TOKEN", None)
        else:
            os.environ["OB_JUDGE_TOKEN"] = old_tok


def test_review_sh_guards() -> None:
    """review.sh 的护栏：沙箱 git 仓库 + 桩引擎，真实运行脚本，不碰平台。

    守卫都要**单独触发**并断言特征文案——多层守卫互相遮挡时，测试只验证「最终被拒」，
    某一层被改坏后照样是绿的（本项目的 review.py 测试首版就栽在这里）。
    """
    print("\n[12] review.sh 护栏（沙箱 + 桩引擎，不碰平台）")

    import pty
    import select
    import time as _time

    SENT = "FAKE_SENTINEL_TOKEN_0123456789ABCDEF0123456789ABCDEF"
    script_src = Path(__file__).parent / "review.sh"
    check("review.sh 存在且可执行", script_src.is_file() and os.access(script_src, os.X_OK))

    stub = (
        "import json, os, pathlib, sys\n"
        "log = pathlib.Path(os.environ['STUB_LOG'])\n"
        "rec = {'tool': pathlib.Path(__file__).name, 'argv': sys.argv[1:],\n"
        "       'token_in_env': bool(os.environ.get('OB_JUDGE_TOKEN'))}\n"
        "with log.open('a') as f: f.write(json.dumps(rec, ensure_ascii=False) + '\\n')\n"
        "a = sys.argv[1:]\n"
        "if 'list' in a: print('悬着（未回传）: 0    已回传: 0')\n"
        "if 'fetch' in a and os.environ.get('STUB_CLAIM') == '1':\n"
        "    wd = pathlib.Path(a[a.index('--workdir') + 1]); wd.mkdir(parents=True, exist_ok=True)\n"
        "    (wd / 'task-777.pending.json').write_text(json.dumps({'task_id': 777}))\n"
        "if 'list' in a: sys.exit(int(os.environ.get('STUB_LIST_RC', '0')))\n"
        "sys.exit(int(os.environ.get('STUB_RC', '0')) if ('fetch' in a or 'submit' in a) else 0)\n"
    )

    def sandbox(root: Path, *, ignore: str = "workdir/\nruns/\n") -> Path:
        (root / "judge").mkdir(parents=True)
        shutil.copy(script_src, root / "judge" / "review.sh")
        for name in ("review.py", "probe.py"):
            (root / "judge" / name).write_text(stub)
        (root / ".gitignore").write_text(ignore)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True,
                       env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull})
        return root / "judge" / "review.sh"

    def base_env(log: Path, **extra: str) -> dict[str, str]:
        e = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "HOME": str(log.parent),
             "GIT_CONFIG_GLOBAL": os.devnull, "STUB_LOG": str(log)}
        e.update(extra)
        return e

    def calls(log: Path) -> list[dict]:
        return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []

    transcript: list[str] = []  # 所有场景的全部输出，最后统一扫 token

    def run(script: Path, args: list[str], env: dict[str, str], *, tty_answers=None):
        """tty_answers=None 走非交互（stdin=/dev/null）；给列表则用 pty 模拟终端输入。"""
        if tty_answers is None:
            r = subprocess.run(["bash", str(script), *args], env=env, cwd=script.parent.parent,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
            out = r.stdout + r.stderr
            transcript.append(out)
            return r.returncode, out
        master, slave = pty.openpty()
        proc = subprocess.Popen(["bash", str(script), *args], env=env, cwd=script.parent.parent,
                                stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        for a in tty_answers:
            os.write(master, (a + "\n").encode())
        buf = b""
        end = _time.time() + 30
        while _time.time() < end:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
            elif proc.poll() is not None:
                break
        proc.wait(timeout=10)
        os.close(master)
        out = buf.decode("utf-8", "replace")
        transcript.append(out)
        return proc.returncode, out

    tmp = Path(tempfile.mkdtemp(prefix="ob-judge-sh-"))
    try:
        root = tmp / "a"
        sh = sandbox(root)
        log = tmp / "calls.log"
        env_ok = base_env(log, OB_JUDGE_TOKEN=SENT)
        env_none = base_env(log)

        # ── token 守卫 ──
        rc, out = run(sh, ["check"], env_none)
        check("未设置 token → check 退出 2", rc == 2, f"rc={rc}")
        check("  └ 指明是 OB_JUDGE_TOKEN 未设置", "OB_JUDGE_TOKEN 未设置" in out, out[:80])
        check("  └ 在碰平台前就退出（桩引擎 0 次调用）", calls(log) == [], str(calls(log)))

        for bad_tok, label, needle in (
            ("FAKE_SENTINEL_TOKEN with a space 0123456789ABCDEF", "含空格", "空白字符"),
            ("'" + SENT + "'", "首尾带单引号", "引号"),
            ('"' + SENT + '"', "首尾带双引号", "引号"),
        ):
            rc, out = run(sh, ["check"], base_env(log, OB_JUDGE_TOKEN=bad_tok))
            check(f"token {label} → 退出 2 并点明原因", rc == 2 and needle in out, f"rc={rc} {out[:60]}")
        check("  └ 坏 token 全程没有调用桩引擎", calls(log) == [])

        # ── check：通过路径 ──
        rc, out = run(sh, ["check"], env_ok)
        check("check 通过 → 退出 0", rc == 0, f"rc={rc} {out[-120:]}")
        tools = [c["tool"] for c in calls(log)]
        check("  └ 调用了 probe 与 list（顺序：probe → list）", tools == ["probe.py", "review.py"], str(tools))
        check("  └ 引擎通过环境变量拿到 token（而非 argv）",
              all(c["token_in_env"] for c in calls(log)))
        log.unlink()

        # ── fetch：参数校验（每条都不得触达引擎）──
        for bad_args, label in (
            (["fetch", "--yes", "--minutes", "0"], "--minutes 0"),
            (["fetch", "--yes", "--minutes", "241"], "--minutes 超上限"),
            (["fetch", "--yes", "--minutes", "abc"], "--minutes 非数字"),
            (["fetch", "--yes", "--minutes", "-5"], "--minutes 负数"),
            (["fetch", "--yes", "--minutes"], "--minutes 缺值"),
            (["fetch", "--yes", "--bogus"], "未知参数"),
        ):
            rc, out = run(sh, bad_args, env_ok)
            check(f"fetch {label} 被拒", rc == 2 and calls(log) == [], f"rc={rc} {out[:60]}")

        # ── fetch：非交互未加 --yes ──
        rc, out = run(sh, ["fetch"], env_ok)
        check("非交互 + 未加 --yes → 拒绝领取", rc == 2, f"rc={rc}")
        check("  └ 且点明是「不是交互终端」而非别的守卫", "不是交互终端" in out, out[-100:])
        check("  └ 引擎的 fetch 一次都没被调用",
              not any("fetch" in c["argv"] for c in calls(log)), str(calls(log)))
        log.unlink(missing_ok=True)

        # ── fetch：--yes 的调用形态（固定 questionId、不含 token、分钟数正确）──
        rc, out = run(sh, ["fetch", "--yes", "--minutes", "08"], env_ok | {"STUB_CLAIM": "1",
                                                                          "OB_QUESTION_ID": "600069"})
        fcalls = [c for c in calls(log) if "fetch" in c["argv"]]
        check("fetch --yes 成功 → 退出 0", rc == 0, f"rc={rc} {out[-100:]}")
        check("  └ 引擎被调用恰好 1 次 fetch", len(fcalls) == 1, str(fcalls))
        argv = fcalls[0]["argv"] if fcalls else []
        check("  └ questionId 固定为 600077（环境变量 OB_QUESTION_ID=600069 不能覆盖）",
              "--question-id" in argv and argv[argv.index("--question-id") + 1] == "600077", str(argv))
        check("  └ 08 被当十进制 8 分钟而非八进制报错",
              "--timeout-minutes" in argv and argv[argv.index("--timeout-minutes") + 1] == "8", str(argv))
        check("  └ token 不在 argv 里", SENT not in json.dumps(argv))
        check("  └ 领到任务后提示下一步", "下一步" in out, out[-120:])
        (root / "workdir" / "task-777.pending.json").unlink()
        log.unlink()

        # ── fetch：没领到任务时的提示（token 不属于本题时 data:null 与「暂无任务」无法区分）──
        rc, out = run(sh, ["fetch", "--yes"], env_ok)  # STUB_CLAIM 未设 → 不产生 pending
        check("没领到任务 → 退出 0 并提示可能是 token 问题",
              rc == 0 and "没有领到任务" in out and "token" in out, f"rc={rc} {out[-140:]}")
        log.unlink()

        # ── fetch：引擎失败时透传退出码并提示任务可能仍悬着 ──
        rc, out = run(sh, ["fetch", "--yes"], env_ok | {"STUB_RC": "4"})
        check("引擎 fetch 失败 → 透传退出码 4", rc == 4, f"rc={rc}")
        check("  └ 提示任务可能仍悬着", "仍然悬着" in out, out[-120:])
        log.unlink()

        # ── fetch：list 只是信息展示，它失败不应让整个流程无声退出 ──
        rc, out = run(sh, ["fetch", "--yes"], env_ok | {"STUB_LIST_RC": "9", "STUB_CLAIM": "1"})
        check("list 展示失败 → 给出提示而不是无声退出", "无法列出悬着的任务" in out, out[-160:])
        check("  └ 流程仍继续并调用了引擎 fetch", any("fetch" in c["argv"] for c in calls(log)))
        (root / "workdir" / "task-777.pending.json").unlink(missing_ok=True)
        log.unlink(missing_ok=True)

        # ── fetch：交互确认（pty）──
        rc, out = run(sh, ["fetch"], env_ok, tty_answers=["no"])
        check("pty：回答 no → 取消，退出 1", rc == 1, f"rc={rc}")
        check("  └ 取消时引擎 fetch 未被调用", not any("fetch" in c["argv"] for c in calls(log)))
        rc, out = run(sh, ["fetch"], env_ok, tty_answers=["y"])
        check("pty：只回答 y 不算确认（必须敲 yes）", rc == 1 and
              not any("fetch" in c["argv"] for c in calls(log)), f"rc={rc}")
        rc, out = run(sh, ["fetch"], env_ok, tty_answers=["yes"])
        check("pty：回答 yes → 领取", rc == 0 and any("fetch" in c["argv"] for c in calls(log)),
              f"rc={rc} {out[-80:]}")
        log.unlink(missing_ok=True)

        # ── 守卫：workdir/runs 未被 gitignore → 拒绝在领取前 ──
        root2 = tmp / "b"
        sh2 = sandbox(root2, ignore="# 空\n")
        log2 = tmp / "calls2.log"
        rc, out = run(sh2, ["fetch", "--yes"], base_env(log2, OB_JUDGE_TOKEN=SENT))
        check("workdir/runs 未被忽略 → fetch 被拒", rc == 2, f"rc={rc}")
        check("  └ 点明是 .gitignore 问题（选手代码会推上公网）", "没有被 .gitignore 忽略" in out, out[:100])
        check("  └ 引擎一次都没被调用", calls(log2) == [])
        root3 = tmp / "c"
        sh3 = sandbox(root3, ignore="workdir/\n")  # 只忽略一个，另一个漏了
        rc, out = run(sh3, ["fetch", "--yes"], base_env(tmp / "calls3.log", OB_JUDGE_TOKEN=SENT))
        check("只漏忽略 runs/ 也要拒绝，并指名 runs/", rc == 2 and "runs/" in out and "workdir/" not in
              out.split("没有被 .gitignore 忽略")[1].split("\n")[0], f"rc={rc} {out[:120]}")

        # ── submit：参数校验（都不得触达引擎）──
        log.unlink(missing_ok=True)
        for bad_args, label, needle in (
            (["submit", "abc", "pass", "--reason", "x", "--yes"], "taskId 非数字", "taskId 必须是数字"),
            (["submit", "123", "maybe", "--reason", "x", "--yes"], "非法结论", "pass / fail / error"),
            (["submit", "123", "pass", "--yes"], "pass 缺 --reason", "必须带 --reason"),
            (["submit", "123", "fail", "--reason", "   ", "--yes"], "fail 纯空白 --reason", "必须带 --reason"),
            (["submit", "123", "pass", "--reason", "x", "--fail-msg", "4", "--yes"], "--fail-msg 配 pass", "只能配合 error"),
            (["submit", "123", "error", "--fail-msg", "x", "--yes"], "--fail-msg 非数字", "必须是数字"),
            (["submit", "123"], "缺结论", "用法"),
        ):
            rc, out = run(sh, bad_args, env_ok)
            check(f"submit {label} 被拒并点明原因", rc == 2 and needle in out and calls(log) == [],
                  f"rc={rc} {out[:70]}")

        rc, out = run(sh, ["submit", "123", "pass", "--reason", "x", "--yes"], env_none)
        check("submit 无 token → 在确认之前就退出", rc == 2 and "OB_JUDGE_TOKEN 未设置" in out and calls(log) == [])

        # ── submit：非交互未加 --yes ──
        rc, out = run(sh, ["submit", "123", "fail", "--reason", "无实质实现"], env_ok)
        check("submit 非交互 + 未加 --yes → 拒绝", rc == 2 and "不是交互终端" in out, f"rc={rc}")
        check("  └ 引擎 submit 未被调用", calls(log) == [])

        # ── submit：摘要内容 + 调用形态 ──
        (root / "workdir").mkdir(exist_ok=True)
        (root / "workdir" / "task-123.pending.json").write_text(json.dumps({
            "task_id": 123, "player": "team-9", "branch": "main", "stage": "awaiting_review",
            "commit_id": "c" * 40, "max_score": 20,
            "platform_deadline_epoch": _time.time() + 900}))
        rc, out = run(sh, ["submit", "123", "fail", "--reason", "无实质实现", "--yes"], env_ok)
        sc = [c for c in calls(log) if "submit" in c["argv"]]
        check("submit --yes → 调用引擎一次", rc == 0 and len(sc) == 1, f"rc={rc} {out[-80:]}")
        a = sc[0]["argv"] if sc else []
        check("  └ 参数完整且 questionId 固定", all(x in a for x in
              ("--task-id", "123", "--verdict", "fail", "--reason", "无实质实现", "--question-id", "600077")), str(a))
        check("  └ 全局参数在子命令之前（argparse 要求）", a.index("--question-id") < a.index("submit"), str(a))
        check("  └ 摘要显示选手/完整 commit/满分", "team-9" in out and "c" * 40 in out and "满分" in out, out[:200])
        check("  └ 提醒理由会对外可见", "对外" in out)
        log.unlink()

        # ── submit：pty 确认——必须敲 taskId，不是 yes ──
        rc, out = run(sh, ["submit", "123", "pass", "--reason", "ok"], env_ok, tty_answers=["yes"])
        check("pty：回答 yes 不算确认（要敲 taskId）", rc == 1 and calls(log) == [], f"rc={rc}")
        rc, out = run(sh, ["submit", "123", "pass", "--reason", "ok"], env_ok, tty_answers=["999"])
        check("pty：敲错 taskId → 取消", rc == 1 and calls(log) == [], f"rc={rc}")
        rc, out = run(sh, ["submit", "123", "pass", "--reason", "ok"], env_ok, tty_answers=["123"])
        check("pty：敲对 taskId → 回传", rc == 0 and any("submit" in c["argv"] for c in calls(log)),
              f"rc={rc} {out[-80:]}")
        log.unlink(missing_ok=True)

        # ── error 结论 ──
        rc, out = run(sh, ["submit", "123", "error", "--fail-msg", "4", "--yes"], env_ok)
        sc = [c for c in calls(log) if "submit" in c["argv"]]
        check("submit error 不要求 --reason，且透传 --fail-msg",
              rc == 0 and sc and "--fail-msg" in sc[0]["argv"] and "4" in sc[0]["argv"], f"rc={rc}")
        log.unlink(missing_ok=True)

        # ── list 不需要 token ──
        rc, out = run(sh, ["list"], env_none)
        check("list 无需 token", rc == 0 and any("list" in c["argv"] for c in calls(log)), f"rc={rc}")

        # ── xtrace：bash -x 不得把展开后的 token 打出来 ──
        r = subprocess.run(["bash", "-x", str(sh), "check"], env=env_ok, cwd=root, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60)
        transcript.append(r.stdout + r.stderr)
        check("bash -x 运行 check 时 token 不出现在 xtrace 里", SENT not in (r.stdout + r.stderr))
        r = subprocess.run(["bash", str(sh), "check"], env={**env_ok, "SHELLOPTS": "xtrace"}, cwd=root,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        transcript.append(r.stdout + r.stderr)
        check("SHELLOPTS=xtrace 时 token 同样不出现", SENT not in (r.stdout + r.stderr))

        # ── 总扫描：token 不得出现在任何输出与桩日志里 ──
        everything = "\n".join(transcript) + (log.read_text() if log.exists() else "")
        check("全部场景的输出与引擎调用日志里都没有 token", SENT not in everything)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    print("=" * 62)
    print("OB 判题对接 —— 安全与协议回归测试")
    print("=" * 62)
    test_url_parsing()
    test_redaction()
    test_clone_no_credential_persistence()
    test_clone_branch_and_commit()
    test_report_payload()
    test_numeric_scoring()
    test_token_never_leaks()
    test_host_credential_helper_cannot_hijack()
    test_scope_dir_isolation()
    test_review_two_phase_flow()
    test_deprecated_manual_mode_refuses_before_claim()
    test_review_sh_guards()

    print("\n" + "=" * 62)
    if _skips:
        print(f"跳过 {len(_skips)} 项（环境不具备，非代码问题）：")
        for s in _skips:
            print(f"  - {s}")
    if _failures:
        print(f"失败 {len(_failures)} 项：")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("全部通过" if not _skips else "其余全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
