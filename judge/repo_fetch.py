"""安全拉取选手私有仓库。

选手提交的 ``gitRepo`` 形如::

    https://<github_user>:<github_pat>@github.com/<github_user>/<github_repo>

PAT 是短期凭证，必须当作密钥对待：

- **不进 argv**：``git clone https://user:pat@...`` 会让 PAT 出现在 ``ps``
  进程列表里，也会写进 ``.git/config``。这里改用 ``GIT_ASKPASS``，
  凭证只经环境变量传给 git 子进程。
- **不进检出目录**：clone 用清洗后的 URL；任务恢复凭据由 credential_store 单独私密保存。
- **不进日志**：``redact()`` 在任何输出路径上把凭证替换成 ``***``。
- **不进异常**：git 的 stderr 也可能带 URL，统一脱敏后才抛出。
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlparse, urlunparse

# https://user:pat@host/... —— 只匹配 URL authority 里的 userinfo 段
_CRED_IN_URL = re.compile(r"(?<=://)([^/@\s:]+):([^/@\s]+)@")
_FULL_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

_ASKPASS_SCRIPT = """#!/bin/sh
# 由 repo_fetch.py 临时生成，退出即删除。不回显任何凭证到终端。
case "$1" in
  *[Uu]sername*) printf '%s' "$OB_JUDGE_GIT_USER" ;;
  *[Pp]assword*) printf '%s' "$OB_JUDGE_GIT_PAT" ;;
esac
"""


def redact(text: str) -> str:
    """把 URL 里的 ``user:pat@`` 凭证替换成 ``***:***@``。

    对 git stderr、异常消息、任何要写日志的字符串都应先过这一层。
    """
    if not text:
        return text
    return _CRED_IN_URL.sub(r"\1:***@", text)


@dataclass(frozen=True)
class RepoCredentials:
    clean_url: str
    username: str
    pat: str

    def __repr__(self) -> str:  # 防止 dataclass repr 泄露 PAT
        return f"RepoCredentials(clean_url={self.clean_url!r}, username={self.username!r}, pat=***)"


@dataclass(frozen=True)
class Checkout:
    path: Path
    branch: str
    commit_id: str


class RepoFetchError(RuntimeError):
    """拉取失败，携带应回传给平台的 failMsg。"""

    def __init__(self, message: str, fail_msg: int) -> None:
        super().__init__(redact(message))
        self.fail_msg = fail_msg


def parse_repo_url(raw_url: str) -> RepoCredentials:
    """从带凭证的 gitRepo 拆出干净 URL 与凭证。

    兼容选手漏填 user 或 pat 的情况：缺凭证不报错，让后续 clone
    失败并映射成 failMsg=4，比在这里猜测更准确。
    """
    raw_url = (raw_url or "").strip()
    if not raw_url:
        raise RepoFetchError("gitRepo 为空", 4)

    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"}:
        raise RepoFetchError(f"仅支持 http(s) 仓库地址，收到 scheme={parsed.scheme!r}", 4)

    username = unquote(parsed.username or "")
    pat = unquote(parsed.password or "")
    # 去掉 userinfo，得到干净 URL —— 这个才会被写进 .git/config
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    clean = urlunparse(
        (parsed.scheme, host, parsed.path, parsed.params, parsed.query, parsed.fragment)
    )
    if not parsed.hostname or not parsed.path.strip("/"):
        raise RepoFetchError(f"仓库地址不完整: {redact(raw_url)}", 4)
    return RepoCredentials(clean_url=clean, username=username, pat=pat)


def _git_env(creds: RepoCredentials, askpass: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    # 禁止任何交互式提示，避免判题进程卡死
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = str(askpass) if askpass else ""
    env["OB_JUDGE_GIT_USER"] = creds.username
    env["OB_JUDGE_GIT_PAT"] = creds.pat

    # ── 关键：隔离宿主的 git 配置 ──
    # git 的认证顺序是先问 credential.helper，helper 给了凭证就不会调 askpass。
    # 若不隔离，~/.gitconfig 里 gh 装的 helper（!/usr/bin/gh auth git-credential）
    # 会交出**我们自己的 token**，选手的 PAT 根本不会被使用，
    # 访问别人的私有仓库必然 404。
    # 实测：用完全无效的 PAT + 全局配置生效时，clone 依然成功 —— 证明 helper 盖过了 PAT。
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


# 必须在每次 git 调用上显式清空 credential helper：
# 环境变量隔离的是配置文件，但命令行 -c 的优先级更高，能彻底抵消任何残留 helper。
_GIT_ISOLATION_ARGS = (
    "-c", "credential.helper=",
    "-c", "credential.https://github.com.helper=",
)


def _run_git(
    args: list[str],
    *,
    cwd: Path | None,
    env: dict[str, str],
    timeout: float,
    fail_msg: int,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["git", *_GIT_ISOLATION_ARGS, *args],
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RepoFetchError(f"git {' '.join(args[:2])} 超时（{timeout}s）", fail_msg) from exc
    except OSError as exc:
        raise RepoFetchError(f"git 不可执行: {exc}", fail_msg) from exc

    if proc.returncode != 0:
        detail = redact((proc.stderr or proc.stdout or "").strip())[:600]
        raise RepoFetchError(f"git {' '.join(args[:2])} 失败 (exit={proc.returncode}): {detail}", fail_msg)
    return proc


def clone_and_checkout(
    raw_git_repo: str,
    dest: Path,
    *,
    branch: str | None = None,
    commit_id: str | None = None,
    clone_timeout: float = 300.0,
    depth: int | None = None,
) -> Checkout:
    """克隆并检出到任务指定版本，返回实际运行的 branch 与完整 commit SHA。

    failMsg 映射（文档 3.5）：clone 失败=4，clone 超时=5，
    branch 未找到=1，commit 未找到=2，reset 失败=6。

    ``depth`` 默认 None（完整克隆）。注意：**如果任务指定了 commitId，
    不要用浅克隆**——浅历史可能不含该 commit，会误判成 "commit 未找到"。
    """
    creds = parse_repo_url(raw_git_repo)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # askpass 脚本落在临时目录，0600，退出即删
    tmpdir = Path(tempfile.mkdtemp(prefix="ob-judge-askpass-"))
    askpass = tmpdir / "askpass.sh"
    try:
        askpass.write_text(_ASKPASS_SCRIPT, encoding="utf-8")
        askpass.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        env = _git_env(creds, askpass)

        clone_args = ["clone"]
        if branch:
            clone_args += ["--branch", branch]
        if depth and not commit_id:
            clone_args += ["--depth", str(depth)]
        clone_args += ["--", creds.clean_url, str(dest)]

        try:
            # 先按 failMsg=4（clone 失败）兜底；超时与分支缺失在下面按 stderr 细分
            _run_git(clone_args, cwd=None, env=env, timeout=clone_timeout, fail_msg=4)
        except RepoFetchError as exc:
            # 分支不存在时 git 也会 clone 失败，靠 stderr 区分出 failMsg=1
            if branch and _looks_like_missing_ref(exc):
                raise RepoFetchError(f"branch 未找到: {branch}", 1) from exc
            if _looks_like_timeout(exc):
                raise RepoFetchError(str(exc), 5) from exc
            raise

        # 必须在 detach 之前记录分支名：一旦 checkout --detach，HEAD 就不再是
        # symbolic ref，事后用 symbolic-ref 取分支会失败（exit 128）。
        actual_branch = branch or _current_branch(dest, env)

        if commit_id:
            want = commit_id.lower()
            if not re.fullmatch(r"[0-9a-f]{7,64}", want):
                raise RepoFetchError("任务 commitId 不是十六进制 SHA", 2)
            try:
                rev_parse = _run_git(
                    ["rev-parse", "--verify", f"{want}^{{commit}}"],
                    cwd=dest, env=env, timeout=60, fail_msg=2,
                )
            except RepoFetchError:
                # A full clone can omit an unadvertised commit. Fetch the exact
                # object while the task credential/askpass is still available.
                if not _FULL_SHA_RE.fullmatch(want):
                    raise
                try:
                    _run_git(["fetch", "--no-tags", "--", creds.clean_url, want],
                             cwd=dest, env=env, timeout=clone_timeout, fail_msg=4)
                except RepoFetchError as exc:
                    if any(s in str(exc).lower() for s in ("not our ref", "couldn't find remote ref", "unadvertised object")):
                        raise RepoFetchError("远端拒绝提供指定 commit", 2) from None
                    raise  # Authentication/network failure is not proof of a missing commit.
                rev_parse = _run_git(
                    ["rev-parse", "--verify", f"{want}^{{commit}}"],
                    cwd=dest, env=env, timeout=60, fail_msg=2,
                )
            resolved = rev_parse.stdout.strip().lower()
            if not _FULL_SHA_RE.match(resolved):
                raise RepoFetchError(f"无法解析出完整 commit: {resolved!r}", 2)
            # 用 checkout 而非 reset：只读评测不该改动分支指针或丢弃工作区
            _run_git(["checkout", "--detach", resolved], cwd=dest, env=env, timeout=120, fail_msg=6)
            full_sha = resolved
        else:
            full_sha = _run_git(
                ["rev-parse", "HEAD"], cwd=dest, env=env, timeout=60, fail_msg=2
            ).stdout.strip().lower()
            if not _FULL_SHA_RE.match(full_sha):
                raise RepoFetchError(f"HEAD 不是完整 commit: {full_sha!r}", 2)

        return Checkout(path=dest, branch=actual_branch, commit_id=full_sha)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _current_branch(repo: Path, env: dict[str, str]) -> str:
    """取当前分支名。失败时回退到远端默认分支，最终兜底 ``main``。

    回传时 branch 必填，拿不到也不能把整个评测判失败。
    """
    try:
        proc = subprocess.run(
            ["git", *_GIT_ISOLATION_ARGS, "symbolic-ref", "--short", "HEAD"],
            cwd=str(repo), env=env, capture_output=True, text=True, timeout=30, check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    # 已 detach 或异常时，问远端 HEAD 指向哪个分支
    try:
        proc = subprocess.run(
            ["git", *_GIT_ISOLATION_ARGS, "rev-parse", "--abbrev-ref", "origin/HEAD"],
            cwd=str(repo), env=env, capture_output=True, text=True, timeout=30, check=False,
        )
        name = proc.stdout.strip()
        if proc.returncode == 0 and name.startswith("origin/"):
            return name[len("origin/"):]
    except (OSError, subprocess.SubprocessError):
        pass
    return "main"


def _looks_like_missing_ref(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("remote branch", "not found in upstream", "couldn't find remote ref", "unknown revision")
    )


def _looks_like_timeout(obj: object) -> bool:
    return "超时" in str(obj)
