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
