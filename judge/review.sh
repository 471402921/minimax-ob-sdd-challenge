#!/usr/bin/env bash
#
# judge/review.sh —— 人工审阅判题的带护栏入口（MiniMax 赛题 questionId=600077）
#
# 只做三件事：① 检查环境  ② 在不可逆动作前让人确认  ③ 用固定参数调用 review.py。
# 判题逻辑全在 review.py，这里没有任何评分判断。
#
# Token 只从环境变量 OB_JUDGE_TOKEN 读取，绝不进入命令行参数、日志或本仓库
# （本仓库是 PUBLIC）。推荐用静默输入，这样 token 也不会进 shell 历史：
#
#     read -rs OB_JUDGE_TOKEN && export OB_JUDGE_TOKEN
#
# 用法：
#     judge/review.sh check                      预检 + 平台探测（保证不领取任务）
#     judge/review.sh fetch [--minutes N] [--yes] [--allow-multiple]  领取 1 条 → 拉取 → 采证（不回传）
#     judge/review.sh list                       查看悬着的任务与剩余时间
#     judge/review.sh submit <taskId> <pass|fail|error> [--reason 理由] [--fail-msg N] [--yes]
#
# 领取有副作用：拿到 taskId 的那一刻任务就进入「执行中」，必须在时限内回传。
# 回传不可逆：平台不接受改分。所以 fetch / submit 默认都要你在终端里亲自确认；
# 非交互环境（管道、CI、agent）必须显式加 --yes，表示「我已经确认过了」。

set -euo pipefail
# 关闭 xtrace：bash -x 或 SHELLOPTS=xtrace 会把「展开后的 token」打到终端和日志里。
set +x

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKDIR="$ROOT_DIR/workdir"
REVIEW_PY="$SCRIPT_DIR/review.py"
PROBE_PY="$SCRIPT_DIR/probe.py"
OPTIONS_PY="$SCRIPT_DIR/submission_options.py"
PY="${OB_JUDGE_PYTHON:-python3}"

readonly QUESTION_ID=600077   # MiniMax 一级赛题；Token 也只授权这一题，不接受环境变量覆盖
readonly DEFAULT_MINUTES=60   # 人工审阅比自动评测慢，比平台默认的 30 分钟宽
readonly MAX_MINUTES=240      # 防手误：任务悬空时，别人要等这么久才能重领

say() { printf '%s\n' "$*"; }
# ok/bad 都写 stderr：预检是给人看的一份报告，分到两个流里上下文会断开。
ok()  { printf '  ✓ %s\n' "$*" >&2; }
bad() { printf '  ✗ %s\n' "$*" >&2; }
die() { printf '错误：%s\n' "$*" >&2; exit 2; }

usage() {
  cat <<'EOF'
用法：
  judge/review.sh check
      预检 + 平台探测。用无交集的 questionId 探测，保证不领取任何任务。
  judge/review.sh fetch [--minutes N] [--yes] [--allow-multiple]
      领取 1 条任务 → 拉取 → 采证，保留检出供人工审阅，不回传。
      --minutes N  平台超时分钟数（默认 60，范围 1~240）
  judge/review.sh list
      查看悬着（未回传）的任务与剩余时间。
  judge/review.sh submit <taskId> <pass|fail|error> [--reason 理由] [--fail-msg N] [--allow-expired] [--yes]
      人工审阅后回传（不可逆）。pass/fail 必须带 --reason；
      error 表示无法评测（--fail-msg：1 branch 缺失 / 2 commit 缺失 / 4 clone 失败 / 100 其他）。

通用：
  --yes   跳过终端确认。仅在你已经明确决定时使用；非交互环境必须加。

Token：只读环境变量 OB_JUDGE_TOKEN（必须 export）。
  read -rs OB_JUDGE_TOKEN && export OB_JUDGE_TOKEN
EOF
}

# ───────────────────────────── 预检 ─────────────────────────────

check_token() {
  local t="${OB_JUDGE_TOKEN:-}"
  if [[ -z "$t" ]]; then
    bad "环境变量 OB_JUDGE_TOKEN 未设置"
    cat >&2 <<'EOF'
      设置方式（静默输入，不会进 shell 历史）：
        read -rs OB_JUDGE_TOKEN && export OB_JUDGE_TOKEN
      注意必须 export，否则子进程读不到。
      Token 绝不要写进命令行、代码或本仓库（本仓库是 public）。
EOF
    return 1
  fi
  case "$t" in
    *[[:space:]]*)
      bad "OB_JUDGE_TOKEN 含空白字符（多半是复制时带上了换行或空格）"
      return 1 ;;
    \"*|\'*|*\"|*\')
      bad "OB_JUDGE_TOKEN 首尾带引号（export 时多套了一层引号？）"
      return 1 ;;
  esac
  if ((${#t} < 32)); then
    # 只告警不拦：残缺的 token 最多换来一个 403，没有副作用。
    printf '  ⚠ OB_JUDGE_TOKEN 只有 %s 个字符，疑似不完整\n' "${#t}" >&2
  fi
  # 只报长度，不输出任何字符——连前缀也不给，部分泄露同样是泄露。
  ok "OB_JUDGE_TOKEN 已配置（${#t} 个字符；内容不会被打印）"
}

check_tools() {
  local miss=0
  command -v git >/dev/null 2>&1 || { bad "找不到 git"; miss=1; }
  if ! command -v "$PY" >/dev/null 2>&1; then
    bad "找不到 $PY"
    return 1
  fi
  "$PY" -c 'import requests' 2>/dev/null \
    || { bad "$PY 缺少 requests 库（pip install requests）"; miss=1; }
  [[ -f "$REVIEW_PY" ]] || { bad "缺少 $REVIEW_PY"; miss=1; }
  [[ -f "$PROBE_PY" ]]  || { bad "缺少 $PROBE_PY"; miss=1; }
  ((miss == 0)) || return 1
  ok "依赖就绪（git / $PY / requests / review.py / probe.py）"
}

# 选手私有仓库的代码会被检出到 workdir/，而本仓库是 PUBLIC。
# 若这两个目录没被 .gitignore 忽略，一次 `git add -A` 就会把选手的私有代码推上公网。
check_ignored() {
  if ! git -C "$ROOT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    ok "不在 git 仓库内，跳过「检出目录是否被忽略」检查"
    return 0
  fi
  local d
  local missing=()
  for d in workdir runs; do
    # check-ignore 退出码：0=被忽略，1=未被忽略，128=出错；非 0 一律按「没被忽略」处理
    if ! git -C "$ROOT_DIR" check-ignore -q "$d/.guard" 2>/dev/null; then
      missing+=("$d/")
    fi
  done
  if ((${#missing[@]} > 0)); then
    bad "这些目录没有被 .gitignore 忽略：${missing[*]}"
    bad "选手私有仓库的代码会被检出到这里，而本仓库是 PUBLIC —— 先补 .gitignore 再继续"
    return 1
  fi
  ok "workdir/ 与 runs/ 已被 .gitignore 忽略（选手代码不会被误提交）"
}

# 不在第一个失败处就停：一次性把所有问题报全，省得来回修。
preflight_local() {
  local rc=0
  check_tools   || rc=1
  check_token   || rc=1
  check_ignored || rc=1
  return "$rc"
}

# ───────────────────────────── 工具函数 ─────────────────────────────

# 数 workdir 下的 pending 记录。用 for+glob，不依赖 shopt nullglob。
count_pending() {
  local f n=0
  for f in "$WORKDIR"/task-*.pending.json; do
    if [[ -e "$f" ]]; then n=$((n + 1)); fi
  done
  printf '%s' "$n"
}

# 用 python 算时刻：GNU date 与 BSD date 的 -d / -v 语法不通用。
deadline_hhmm() {
  "$PY" -c 'import sys, time; print(time.strftime("%H:%M", time.localtime(time.time() + int(sys.argv[1]) * 60)))' "$1"
}

# confirm <期望输入> <提示> <是否已加 --yes>
# 非交互环境没有「人」可以确认，必须显式 --yes，而不是悄悄放行。
confirm() {
  local expect="$1" prompt="$2" yes="$3" ans=""
  if [[ "$yes" == 1 ]]; then
    return 0
  fi
  if [[ ! -t 0 ]]; then
    die "当前不是交互终端，无法确认。已经确认要继续的话请显式加 --yes。"
  fi
  read -r -p "$prompt" ans || ans=""
  if [[ "$ans" != "$expect" ]]; then
    say "已取消，没有做任何改动。" >&2
    exit 1
  fi
}

# ───────────────────────────── 子命令 ─────────────────────────────

cmd_check() {
  say "== 本地预检 =="
  if ! preflight_local; then
    say "" >&2
    say "预检未通过。没有接触平台。" >&2
    exit 2
  fi
  say ""
  say "== 平台探测（用无交集的 questionId，保证不领取任何任务）=="
  if ! "$PY" "$PROBE_PY"; then
    bad "平台探测失败，见上方输出"
    exit 2
  fi
  say ""
  say "== 题目能力（只读 GET submissionOptions，不带 Token）=="
  # 偏差只警告：平台改了满分/题型要人工确认，但不该挡住审阅本身
  "$PY" "$OPTIONS_PY" || true
  say ""
  say "== 当前悬着的任务 =="
  "$PY" "$REVIEW_PY" --workdir "$WORKDIR" list
  say ""
  say "全部检查通过。下一步：judge/review.sh fetch"
}

cmd_list() {
  exec "$PY" "$REVIEW_PY" --workdir "$WORKDIR" list
}

cmd_fetch() {
  local minutes="$DEFAULT_MINUTES" yes=0 allow_multiple=0
  while (($# > 0)); do
    case "$1" in
      --minutes)
        [[ $# -ge 2 ]] || die "--minutes 需要一个数值"
        minutes="$2"; shift 2 ;;
      --yes|-y) yes=1; shift ;;
      --allow-multiple) allow_multiple=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "未知参数：$1（见 judge/review.sh --help）" ;;
    esac
  done
  [[ "$minutes" =~ ^[0-9]+$ ]] || die "--minutes 必须是正整数，收到：$minutes"
  # 10# 前缀：避免 08 / 09 被当成八进制
  if ! ((10#$minutes >= 1 && 10#$minutes <= MAX_MINUTES)); then
    die "--minutes 必须在 1~${MAX_MINUTES} 之间（任务悬空时别人要等这么久才能重领），收到：$minutes"
  fi
  minutes=$((10#$minutes))

  say "== 本地预检 =="
  if ! preflight_local; then
    say "" >&2
    say "预检未通过。没有接触平台，也没有领取任何任务。" >&2
    exit 2
  fi

  say ""
  say "== 当前悬着的任务 =="
  # 这里只是信息展示：list 失败（比如某个 pending JSON 损坏）不该让整个流程无声退出。
  # 真正拦住「手上还有悬着任务」的是 review.py fetch 自己的守卫，不依赖这次展示。
  "$PY" "$REVIEW_PY" --workdir "$WORKDIR" list \
    || bad "无法列出悬着的任务（详见上方输出）。继续前请先核对 workdir/ 里的 *.pending.json"
  local before
  before="$(count_pending)"

  local deadline
  deadline="$(deadline_hhmm "$minutes")"
  say ""
  say "即将领取 1 条 MiniMax（questionId=${QUESTION_ID}）任务："
  say "  · 领取有副作用：拿到 taskId 的那一刻起，任务进入「执行中」。"
  say "  · 必须在 ${minutes} 分钟内回传（建议 ${deadline} 前）；超时任务会被放回队列，并消耗一次重领次数。"
  say "  · 回传不可逆，平台不接受改分。"
  confirm "yes" "确认现在领取？输入 yes 继续：" "$yes"

  local rc=0
  local allow_args=()
  ((allow_multiple)) && allow_args+=(--allow-multiple)
  "$PY" "$REVIEW_PY" --workdir "$WORKDIR" --question-id "$QUESTION_ID" \
        --timeout-minutes "$minutes" fetch "${allow_args[@]}" || rc=$?

  if ((rc != 0)); then
    say "" >&2
    say "提示：若上面已经出现「已领取 taskId=…」，该任务仍然悬着，不会自己消失。" >&2
    say "      查看剩余时间：judge/review.sh list" >&2
    exit "$rc"
  fi

  local after
  after="$(count_pending)"
  say ""
  if ((after > before)); then
    say "下一步："
    say "  1. 审阅上面列出的检出目录与证据文件"
    say "  2. 决定后回传（不可逆）：judge/review.sh submit <taskId> pass|fail --reason '…'"
    say "  3. 随时查看剩余时间：judge/review.sh list"
  else
    say "（本次没有领到任务。code=200 且 data=null 既可能是队列暂时没有，"
    say "  也可能是 token 不属于 MiniMax 赛题 —— 久久没有任务时，请核对 token。）"
  fi
}

cmd_submit() {
  [[ $# -ge 2 ]] || die "用法：judge/review.sh submit <taskId> <pass|fail|error> [--reason 理由] [--fail-msg N] [--yes]"
  local task_id="$1" verdict="$2" reason="" fail_msg="" allow_expired=0 yes=0
  shift 2
  while (($# > 0)); do
    case "$1" in
      --reason)
        [[ $# -ge 2 ]] || die "--reason 需要内容"
        reason="$2"; shift 2 ;;
      --fail-msg)
        [[ $# -ge 2 ]] || die "--fail-msg 需要一个数值"
        fail_msg="$2"; shift 2 ;;
      --allow-expired) allow_expired=1; shift ;;
      --yes|-y) yes=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "未知参数：$1（见 judge/review.sh --help）" ;;
    esac
  done

  [[ "$task_id" =~ ^[0-9]+$ ]] || die "taskId 必须是数字，收到：$task_id"
  case "$verdict" in
    pass|fail|error) ;;
    *) die "结论必须是 pass / fail / error，收到：$verdict" ;;
  esac
  if [[ "$verdict" != error ]]; then
    [[ -n "${reason//[[:space:]]/}" ]] \
      || die "pass/fail 必须带 --reason：二值结论不可逆，没有理由将无法申诉与复核"
  fi
  if [[ -n "$fail_msg" ]]; then
    [[ "$verdict" == error ]] || die "--fail-msg 只能配合 error 使用"
    [[ "$fail_msg" =~ ^[0-9]+$ ]] || die "--fail-msg 必须是数字，收到：$fail_msg"
  fi

  # submit 需要 token（回传要鉴权）。缺了就在锁定任何东西之前退出。
  check_token || exit 2

  say "即将回传（不可撤销，平台不接受改分）："
  local pend="$WORKDIR/task-${task_id}.pending.json"
  if [[ -f "$pend" ]]; then
    "$PY" - "$pend" <<'PY'
import json, sys, time
p = json.load(open(sys.argv[1], encoding="utf-8"))
left = p.get("platform_deadline_epoch", 0) - time.time()
fmt = (lambda s: f"已超时 {int(-s)} 秒") if left <= 0 else (lambda s: f"剩 {int(s // 60)} 分 {int(s % 60)} 秒")
print(f"  taskId  = {p.get('task_id')}")
print(f"  选手    = {p.get('player')}")
print(f"  branch  = {p.get('branch')}")
print(f"  commit  = {p.get('commit_id')}")
print(f"  阶段    = {p.get('stage')}")
print(f"  满分    = {p.get('max_score')}")
print(f"  时限    = {fmt(left)}（平台硬超时）")
PY
  else
    say "  taskId  = ${task_id}   （本地没有 pending 记录；review.py 会拒绝这次回传）"
  fi
  case "$verdict" in
    pass)  say "  结论    = 通过（pass）" ;;
    fail)  say "  结论    = 不通过（fail）→ 0 分" ;;
    error) say "  结论    = 无法评测（error）→ taskStatus=3，failMsg=${fail_msg:-100}" ;;
  esac
  say "  理由    = ${reason:-（无）}"
  say "            ↑ 会写进平台子题的 message，OB 与选手都可能看到，措辞请按「对外」来写"
  # 要求敲 taskId 而不是 y：避免肌肉记忆式确认一个不可逆动作。
  confirm "$task_id" "确认回传？请输入 taskId（${task_id}）：" "$yes"

  local args=(--workdir "$WORKDIR" --question-id "$QUESTION_ID"
              submit --task-id "$task_id" --verdict "$verdict")
  if [[ -n "$reason" ]]; then args+=(--reason "$reason"); fi
  if [[ -n "$fail_msg" ]]; then args+=(--fail-msg "$fail_msg"); fi
  if ((allow_expired)); then
    say "            ⚠️ 已显式允许尝试过期任务；平台可能已重新分配，回传可能被拒绝。"
    args+=(--allow-expired)
  fi
  exec "$PY" "$REVIEW_PY" "${args[@]}"
}

# ───────────────────────────── 入口 ─────────────────────────────

if (($# == 0)); then
  usage
  exit 2
fi
sub="$1"
shift
case "$sub" in
  check)  cmd_check  "$@" ;;
  fetch)  cmd_fetch  "$@" ;;
  list)   cmd_list   "$@" ;;
  submit) cmd_submit "$@" ;;
  -h|--help|help) usage ;;
  *) die "未知子命令：$sub（可用：check / fetch / list / submit）" ;;
esac
