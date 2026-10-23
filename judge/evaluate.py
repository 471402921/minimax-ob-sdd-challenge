"""评测器：把选手仓库变成一个可回传的结论。

⚠️ **当前 rubric 尚未定版**，所以这里刻意分成两层：

1. ``collect_evidence()`` —— 客观事实采集。只读、确定性、可复跑。
   这层现在就能用，不依赖评分口径。
2. ``judge()`` —— 把证据映射成通过/不通过。**默认不自动判过**。

平台的 600077/800514 是单子题固定分（通过 30 / 不通过 0），无法表达
《评测与出题实施说明.md》里 S=60B+25A+15H 的分层与权重。所以在 OB 为
我们开出多子题或数值积分（resultType=2）之前，所有细粒度判断都只能
沉淀在本地证据文件里，最终对外压缩成一个布尔值。
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# 依赖/配置文件里出现这些标记，说明真的接了指定基础设施
_SEEKDB_MARKERS = ("seekdb", "oceanbase", "pyobvector", "sqlalchemy-oceanbase", "obvector")
_POWERCONTEXT_MARKERS = ("powercontext", "power_context", "power-context")
_SPEC_DIR_MARKERS = ("openspec", "specs", "spec", ".kiro", "spec-kit", "requirements")
_DOC_NAMES = ("readme", "技术说明", "design", "architecture", "方案", "说明")

_SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build", ".next", "target"}
_CODE_EXT = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".java", ".rs", ".rb", ".php",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".kt", ".swift", ".sql", ".sh",
}
_DEP_FILES = (
    "requirements.txt", "pyproject.toml", "setup.py", "package.json",
    "go.mod", "pom.xml", "build.gradle", "Cargo.toml", "Pipfile", "environment.yml",
)


@dataclass
class Verdict:
    """单个子题的结论。"""

    sub_question_id: int
    passed: bool
    message: str = ""
    score: float | None = None


@dataclass
class Judgement:
    """整道一级题的评测结果。"""

    verdicts: list[Verdict]
    fail_msg: int | None = None  # 非 None 表示无法完成评测，应回传 taskStatus=3
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def is_execution_failure(self) -> bool:
        return self.fail_msg is not None


def _iter_files(root: Path, limit: int = 20000):
    count = 0
    for path in root.rglob("*"):
        if count >= limit:
            return
        if path.is_dir():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        count += 1
        yield path


def _read_head(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_bytes()[:limit].decode("utf-8", "ignore")
    except OSError:
        return ""


def collect_evidence(repo: Path, *, commit_id: str = "", branch: str = "") -> dict[str, Any]:
    """采集仓库客观事实。只读，不执行选手代码。

    采集的都是**可确定性复核**的东西：文件、依赖声明、git 历史。
    不做语义判断——那部分归 judge()，且需要人工或固定裁判参与。
    """
    ev: dict[str, Any] = {
        "commit_id": commit_id,
        "branch": branch,
        "repo_path": str(repo),
    }

    files = list(_iter_files(repo))
    ev["file_count"] = len(files)
    ev["truncated"] = len(files) >= 20000

    ext_counter = Counter(p.suffix.lower() for p in files if p.suffix)
    ev["top_extensions"] = dict(ext_counter.most_common(10))
    ev["code_file_count"] = sum(1 for p in files if p.suffix.lower() in _CODE_EXT)

    # ── git 历史：能看出是不是临时糊出来的提交 ──
    try:
        log = subprocess.run(
            ["git", "log", "--format=%H%x1f%an%x1f%aI%x1f%s", "-n", "200"],
            cwd=str(repo), capture_output=True, text=True, timeout=60, check=False,
        )
        if log.returncode == 0:
            commits = [ln for ln in log.stdout.splitlines() if ln.strip()]
            ev["commit_count"] = len(commits)
            ev["recent_commits"] = [
                dict(zip(("sha", "author", "date", "subject"), ln.split("\x1f")))
                for ln in commits[:20]
            ]
            authors = {ln.split("\x1f")[1] for ln in commits if "\x1f" in ln}
            ev["author_count"] = len(authors)
    except (OSError, subprocess.SubprocessError):
        ev["commit_count"] = None

    # ── 依赖声明：是否真的引入 seekdb / PowerContext ──
    dep_text = ""
    found_dep_files = []
    for name in _DEP_FILES:
        for p in files:
            if p.name == name and len(p.relative_to(repo).parts) <= 3:
                dep_text += "\n" + _read_head(p)
                found_dep_files.append(str(p.relative_to(repo)))
    ev["dependency_files"] = sorted(set(found_dep_files))

    low_dep = dep_text.lower()
    ev["declares_seekdb_dep"] = any(m in low_dep for m in _SEEKDB_MARKERS)
    ev["declares_powercontext_dep"] = any(m in low_dep for m in _POWERCONTEXT_MARKERS)

    # ── 全仓扫描：区分「真的写了代码」和「只在文档里提了一句」 ──
    # 这个区分至关重要：否则一篇 README 提到 seekdb 就能冒充实现。
    doc_ext = {".md", ".rst", ".txt", ".adoc"}
    cfg_ext = {".yaml", ".yml", ".toml", ".json", ".ini", ".env", ".cfg"}
    seekdb_code: list[str] = []
    seekdb_doc: list[str] = []
    pc_code: list[str] = []
    pc_doc: list[str] = []
    for p in files:
        ext = p.suffix.lower()
        if ext not in _CODE_EXT and ext not in doc_ext and ext not in cfg_ext:
            continue
        text = _read_head(p, 80_000).lower()
        rel = str(p.relative_to(repo))
        is_doc = ext in doc_ext
        if any(m in text for m in _SEEKDB_MARKERS):
            (seekdb_doc if is_doc else seekdb_code).append(rel)
        if any(m in text for m in _POWERCONTEXT_MARKERS):
            (pc_doc if is_doc else pc_code).append(rel)

    ev["seekdb_code_files"] = sorted(seekdb_code)[:40]
    ev["seekdb_code_file_count"] = len(seekdb_code)
    ev["seekdb_doc_mentions"] = len(seekdb_doc)
    ev["powercontext_code_files"] = sorted(pc_code)[:40]
    ev["powercontext_code_file_count"] = len(pc_code)
    ev["powercontext_doc_mentions"] = len(pc_doc)
    # 兼容旧字段名，但语义收紧为「代码/配置中的实现证据」
    ev["seekdb_files"] = ev["seekdb_code_files"]
    ev["seekdb_file_count"] = len(seekdb_code)
    ev["powercontext_files"] = ev["powercontext_code_files"]
    ev["powercontext_file_count"] = len(pc_code)

    # ── 规格材料与测试 ──
    top_dirs = {p.name.lower() for p in repo.iterdir() if p.is_dir()} if repo.is_dir() else set()
    all_dirs = {p.name.lower() for p in files for p in [p.parent]} if files else set()
    ev["has_spec_material"] = bool(
        {m for m in _SPEC_DIR_MARKERS} & (top_dirs | {d for d in all_dirs if d in _SPEC_DIR_MARKERS})
    )
    ev["spec_dirs"] = sorted({d for d in all_dirs if d in _SPEC_DIR_MARKERS})[:20]

    test_files = [
        str(p.relative_to(repo)) for p in files
        if re.search(r"(^|/)(tests?|__tests__|spec)/", str(p.relative_to(repo)).replace("\\", "/"))
        or re.search(r"(test_|_test\.|\.spec\.|\.test\.)", p.name)
    ]
    ev["test_file_count"] = len(test_files)
    ev["test_files_sample"] = sorted(test_files)[:30]

    docs = [
        str(p.relative_to(repo)) for p in files
        if p.suffix.lower() in {".md", ".rst"} and any(n in p.name.lower() for n in _DOC_NAMES)
    ]
    ev["doc_files"] = sorted(docs)[:20]

    return ev


def summarize(evidence: dict[str, Any]) -> str:
    """给人看的一行摘要（不含任何凭证）。"""
    return (
        f"commit={evidence.get('commit_id', '')[:12]} branch={evidence.get('branch')} "
        f"files={evidence.get('file_count')} code={evidence.get('code_file_count')} "
        f"commits={evidence.get('commit_count')} authors={evidence.get('author_count')} "
        f"seekdb[code={evidence.get('seekdb_code_file_count')},"
        f"doc={evidence.get('seekdb_doc_mentions')},"
        f"dep={evidence.get('declares_seekdb_dep')}] "
        f"pc[code={evidence.get('powercontext_code_file_count')},"
        f"doc={evidence.get('powercontext_doc_mentions')},"
        f"dep={evidence.get('declares_powercontext_dep')}] "
        f"spec={evidence.get('has_spec_material')} tests={evidence.get('test_file_count')} "
        f"docs={len(evidence.get('doc_files') or [])}"
    )


# ── judge 实现 ────────────────────────────────────────────────────────
# 联调阶段需要能走通三条回传路径，所以提供显式 stub。
# 默认是 manual：只采证据、拒绝自动下结论，避免误把不合格作品判成 30 分。

def judge(
    repo: Path,
    sub_questions: list[tuple[int, str]],
    *,
    mode: str = "manual",
    evidence: dict[str, Any] | None = None,
) -> Judgement:
    """把证据映射成回传结论。

    mode:
      ``manual``      —— 只采证据、不下结论，抛错要求人工判定（**默认**）
      ``stub-pass``   —— 联调用：全部子题通过
      ``stub-fail``   —— 联调用：子题不通过（taskStatus 仍为 2）
      ``stub-error``  —— 联调用：无法完成评测（taskStatus=3, failMsg=100）
    """
    evidence = evidence if evidence is not None else collect_evidence(repo)

    if mode == "stub-pass":
        return Judgement(
            verdicts=[Verdict(sid, True, "联调 stub：标记通过") for sid, _ in sub_questions],
            evidence=evidence,
        )
    if mode == "stub-fail":
        return Judgement(
            verdicts=[Verdict(sid, False, "联调 stub：标记不通过") for sid, _ in sub_questions],
            evidence=evidence,
        )
    if mode == "stub-error":
        return Judgement(verdicts=[], fail_msg=100, evidence=evidence)

    if mode == "manual":
        raise NotImplementedError(
            "rubric 未定版：已采集证据但拒绝自动判定。"
            " 请人工审阅 evidence 后用 --override 指定结论，或先跑 --mode stub-* 联调。"
        )
    raise ValueError(f"未知 mode: {mode}")


def write_evidence(evidence: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
