# OB 判题对接程序

MiniMax 赛题（`questionId=600077`，子题 `800514`，满分 20）在 OB 数据库大赛
判题平台上的领取—评测—回传实现。

协议依据：OB 方《数据库大赛联调相关文档》(2026-10-21) 及新版 skill 的只读积压补充，`campId=21`。

**当前接续入口（2026-10-29）：[运行手册](../docs/运行手册.md) / [当前状态](../docs/当前状态.md)。**
平台已支持 `queryTaskQueueCount`，不要用领取接口探测积压。`review.sh list` 仅列本机记录。
Harbor 的进度见 [试点验证](../harbor/VALIDATION.md)，不是生产自动判题/回传系统。

## 快速开始

```bash
set +x
source "$HOME/.config/minimax-ob-judge/platform.env"  # 用户私密文件，权限600

# 自检：最近185项安全与协议断言；不领取/回传真实任务，部分断言会访问GitHub探针仓库
python3 judge/test_harness.py

# 安全探测：验证连通性/Token/请求格式，保证不领取任何任务
python3 judge/probe.py

# 本地验证证据采集，完全不接触平台
python3 judge/run_judge.py --local-repo /path/to/some/repo

# 不在日常操作中运行 --mode stub-*：会对真实领取任务直接写入示例结果。
```

## 🧑‍⚖️ 正式判题：人工审阅入口（推荐 `review.sh`）

> ⚠️ **正式判题不要用 `run_judge.py --mode stub-*`**——stub 会不看代码就回传结果，
> 而回传不可逆。也**不要用 `--mode manual`**：旧 manual 模式已被安全拦截。

正式判题推荐使用带交互护栏的脚本 `judge/review.sh`（底座引擎为 `review.py`），把「领取留证」与「回传」拆成两步，中间留给人审阅：

```bash
# 0. 预检环境与连通性（无交集假设下不领取；包含submissionOptions，不含队列计数）
./judge/review.sh check

# 阶段 1：领取 1 条 → 拉取 → 采证 → 保留检出 → 写 pending → 不回传
./judge/review.sh fetch                # 默认 60 分钟时限，终端敲 yes 确认
./judge/review.sh fetch --minutes 90   # 审阅复杂作品时可调大超时
./judge/review.sh fetch --minutes 90 --allow-multiple  # 已有未回传任务时再领一条（默认拒绝）

# （人工审阅 workdir/task-<id>/ 与 workdir/task-<id>.evidence.json）

# 阶段 2：核对版本与时限后回传（回传不可逆，终端要求敲入 taskId 确认防误触）
./judge/review.sh submit <taskId> pass --reason "OceanBase 向量检索实现完整，测试通过"
./judge/review.sh submit <taskId> fail --reason "缺少 seekdb/OceanBase 集成，minimax/ 目录未修改"
./judge/review.sh submit <taskId> error --fail-msg 4   # 未核实为选手交付缺陷的拉取异常；需人工决定
# 经用户/平台方核实仓库确实不可访问：Python专用例外，判0分，不虚构检出
python3 judge/review.py submit --task-id <taskId> --verdict fail \
  --confirmed-repo-unavailable --reason '核实来源与无法访问依据；未执行代码测试'

# 随时查看当前悬着哪些任务、还剩多少时间
./judge/review.sh list
```

底层直接调用 Python 也是支持的（参数更自由）：
```bash
python3 judge/review.py --timeout-minutes 60 fetch
python3 judge/review.py submit --task-id <id> --verdict pass --reason "…"
python3 judge/review.py list
```

**铁律与内置守卫**（均有回归测试，且经变异检验确认测试能抓到）：

| 守卫 | 防的事故 |
| --- | --- |
| `fetch` 不回传任何东西 | 未经审阅就把结果写进平台 |
| `fetch` 打印硬截止时间；`pending` 截止早于平台超时 3 分钟 | 审阅拖过超时，任务被重领 |
| 手上有未超过平台硬超时的 pending 时拒绝再次 `fetch`；过期记录留档但不阻塞 | 悬空面扩大或历史超时导致永久死锁 |
| `submit` 重新核对检出 `HEAD` == fetch 时记录的 commit | 看的是 A 提交、回传的是 B 提交 |
| 已回传任务拒绝二次回传 | 回传不可逆，不能改分 |
| 已超平台超时默认拒绝；平台/用户明确允许才显式 `--allow-expired` | 仅绕本地时间护栏，可能被拒或已重新分配 |
| `pass`/`fail` 必须带 `--reason` | 二值结论无理由则无法申诉与复核 |
| 未采证的任务默认不能按 `pass`/`fail` 回传 | 已核实仓库不可访问的显式fail0例外除外；不是自动从clone失败推断 |
| 回传的 `commitId` 必须是完整 40/64 位 | 平台拒绝短 SHA |
| `fetch` 任一步异常都**不删现场** | 旧 `manual` 路径的现场被毁问题 |

**审阅时限**：`review.sh fetch` 默认60分钟；底层 `review.py` 默认30分钟，重领次数按请求配置。
入口脚本用 `--minutes` 调整，底层Python用全局 `--timeout-minutes`（须放在fetch前）。
时限必须覆盖审阅与回传，无独立续租接口；`fetch` 会打印截止时间。

## 失败恢复与私密仓库凭据

`review.py fetch` 现在在clone前私密保存任务仓库凭据（仓库外700/600），普通领取记录和日志
仍脱敏。完整commit本地缺失时，会先带认证定向fetch；认证失败不能证明commit不存在。
`python3 judge/review.py recover --help` 查看恢复入口：不重新领取、不续租、不回传，保留旧现场。
仅支持fetch_failed、有固定commit及匹配凭据、租约有效的任务；恢复不支持--allow-expired。
当前只接入review.py路径，旧run_judge.py不包含此恢复能力。
回传ACK明确匹配taskId后清理凭据；未确认/过期任务不自动丢弃恢复数据。
位置、权限、失败处理及测试见[运行手册](../docs/运行手册.md)。

## 平台积压计数（只读，新增）

使用本机私有 `skills/techcamp-platform-integration/SKILL.md` 对应的 `queue-count`（不随公开clone提供），
命令与凭据映射见 [运行手册 §2](../docs/运行手册.md)。新版固定提交为 `e7943125`。
`waitCount` 为待执行任务条数；`runCount` 为执行中，包含超时未结束任务；不是队伍数。
该查询不领取/锁定/续租。错误、404、鉴权失败不能当0。`review.sh check/list` 尚未集成这个计数。

## 🔥 不要用领取接口探测积压；旧 probe 的适用边界

`queryOneTaskRecord` **不是只读查询**。返回非 null 即代表任务已进入执行中，
必须评测并回传。

**2026-10-23 真实事故**：用 `questionId=600077` 跑了一次"看看有没有任务"，
结果真的领走了队列里的一条任务；更糟的是响应只做了摘要未落盘，
`taskId` 与 `gitRepo` 丢失，导致既不知道领了谁的任务、也无法回传
（回传必须 `taskId` + 完整 commit）。因回传不可逆，只能等 30 分钟超时让任务回队列。

`probe.py` 用**无交集的 questionId**（默认 seekdb 的 600069）探测：
文档 §2.3 说明可领范围 = 授权赛题 ∩ 请求筛选，两者无交集时服务端必然返回
`data:null`。这是依赖Token授权与筛选无交集的旧探测，不是只读接口本身的保证；
授权范围若变更须重新核实。它会拒绝 `--question-id 600077`。
现在查看积压请直接使用只读 `queue-count`。

`run_judge.py` 则会把完整领取响应先落到 `workdir/claims/` 再处理，
即使中途崩溃也能查清领了什么。

## ⚠️ 凭证安全

本仓库是 **PUBLIC**。以下规则没有例外：

| 凭证 | 处理方式 |
| --- | --- |
| OB MiniMax赛题评分Token | 从本机私密 `platform.env` 载入 `OB_JUDGE_TOKEN`；不是模型API Key；不进argv/日志/仓库 |
| 选手 GitHub PAT | 只经 `GIT_ASKPASS` 环境变量传给 git 子进程；不进 argv、不进 `.git/config`、不进日志 |

`git clone https://user:pat@github.com/...` 是**错误做法** —— PAT 会出现在
`ps` 进程列表，也会写进 `.git/config`。本实现用 `GIT_ASKPASS` 绕开这两条泄漏路径，
并对所有输出（git stderr、异常消息、runlog）统一走 `redact()`。

`.gitignore` 已覆盖 `OB方参考/`、`.env`、`secrets/`、`runs/`、`workdir/`。
提交前可用 `git status --porcelain --ignored | grep '^!!'` 复核。
`skills/`、`harbor/`、`docs/` 未被整体忽略：不要盲目 `git add -A`。
私有来源的 `skills/techcamp-platform-integration/` 尚未确认公开再分发许可，暂勿stage/commit/push。

## 文件

| 文件 | 职责 |
| --- | --- |
| `ob_client.py` | 领取 `queryOneTaskRecord` + 回传 `synchronizeTaskRecord`，业务 code 校验 |
| `repo_fetch.py` | PAT安全拉取、完整SHA认证补取、branch/commit解析、failMsg映射 |
| `credential_store.py` | 仓库外任务级恢复凭据；租约/版本绑定；700/600；终态清理 |
| `test_recovery.py` | 离线私密存储/恢复/ACK/认证补取回归 |
| `evaluate.py` | 客观证据采集（只读）+ judge 接口 |
| `run_judge.py` | 主循环：领取 → 拉取 → 评测 → 回传 |
| `probe.py` | 旧无交集题号探测；依赖授权边界成立，不用于计数 |
| `submission_options.py` | 不带Token的题目能力查询（非队列计数） |
| `review.sh` / `review.py` | 带护栏的人工fetch → 审阅 → submit，两阶段入口 |
| `test_harness.py` | 安全与协议回归测试（最近185项断言，0 FAIL/skip；探针仓库不可用时网络断言转skip） |
| `../skills/techcamp-platform-integration/` | 上游私有来源本地副本；含queue-count/outbox客户端，不能直接替换本机pending流程 |

## 平台协议要点（易错处）

这些是文档里明确警告、或会导致回传被拒的细节：

1. **不要拿领取接口做连通性探测。** 它有副作用 —— 返回 `taskId` 即代表任务
   已进入执行中，拿完不处理会一直挂到超时被重领。`data: null` 只表示本轮无任务。
   现在查看积压用 `queryTaskQueueCount`；`probe.py` 仅是旧无交集探测。
2. **HTTP 200 不代表成功**，必须检查响应体的业务 `code`。
3. **`testCaseResult` 是序列化后的字符串**，不是嵌套 JSON 数组。
4. **CASES 协议禁止顶层 `score` / `message`**，说明要放进子题项内。
5. **必须一次覆盖 `subQuestions` 全部子题**，不能分批回传（平台返回 400 并列出缺失 ID）。
6. **子题不通过 ≠ 任务失败。** 某子题不通过应回 `taskStatus=2` + `result_status=2`；
   只有 clone/编译这类无法完成评测的情况才回 `taskStatus=3` + `failMsg`。
7. **`commitId` 必须是完整 40/64 位十六进制 SHA**，短 SHA 会被拒。任务指定短 SHA 时，
   完整 SHA 须以它为前缀。
8. **`result_status` 与 `taskStatus` 数字含义不同**：前者 1=通过/2=不通过，
   后者 2=评测完成/3=执行失败。
9. **不要对 400/403/409 无限重试。** 503（队伍校验暂不可用）可退避重试。
10. **领取只按 `questionId` 筛选**，不要传 `testCases` / `player`，也不要用 `testSuite`
    按名称筛（中文名与空格易错）。
11. **人工回传也需要冻结请求与ACK核对。** 上游skill已有outbox；本地review.py尚未完整接入。
    网络结果不确定时不要改理由/分数后重试。

## ⚠️ 当前约束：平台只接受二值结果

《附件/评测与出题实施说明.md》定义的是分层评分 `S = 60B + 25A + 15H`，
含 F/L/T/E/D 五个维度与子项权重。

600077/800514 的配置是 **单子题 + `resultType=1` 固定分**：通过满分（现为 **20 分**，
已确认为正式分值），不通过 0 分，无法表达任何中间态。**已确认沿用该配置**。

⚠️ 分值从领取响应的 `maxScore` 读取，**不要硬编码 20** —— 平台可能再次调整。

因此所有细粒度判断只能沉淀在 `runs/judge.jsonl` 与 `workdir/task-*.evidence.json`，
最终对外压缩成一个布尔值。实现上分两层：

- `collect_evidence()` —— 客观事实采集，现在就能跑，不依赖评分口径
- `judge()` —— 映射成布尔值，**默认 `manual` 模式，拒绝自动判分**

### 二值制下通过标准必须可证伪

只有「过 / 不过」两档，标准的可证伪性比分层制更重要，否则评委间无法对齐。
早期曾建议用 Jev（`typesafe_evaluate`）辅助；当前在试验 Harbor + Pi 实际复跑，尚未冻结
自动判分规则。两者都必须遵守建议书的约束：

> 「不能用一个黑箱大模型印象分替代本项」

若使用 Jev，应拆成**多个具名问题**（每题只判一件事、有明确 criteria、输出概率），
而不是丢一句"这个作品好不好"。同时需保留复核样本与争议处理路径。
`ob_client.py` 已实现 `resultType=2` 分支，万一 OB 后续改配置可直接启用。

## 当前待确认清单

- [x] 连通性 / Token / 请求格式已验证；新版只读queue-count也已在线返回有效结果
- [x] 确认 600077/800514 保持 `resultType=1` 二值（通过满分 / 不通过 0）
- [x] 满分确认为 **20 分**（此前 30 为联调占位值）
- [ ] 计数接口只能确认规模；若需区分测试/真实提交，仍须向OB确认，不能从计数推断
- [ ] 按真实作品批次统计耗时/容量，据此定租约；review.sh默认60min不代表足够覆盖所有作品
- [ ] 确认 `gitRepo` 里 PAT 的权限范围与过期时间
- [ ] 冻结通过标准，完成Harbor真实作品正反例校准，再决定自动判定/人工复核策略
- [ ] 完整接入不可变outbox、回传ACK及不确定结果原样重试
- [ ] 合并queue-count到统一入口，并修正check能力查询报警后仍打印全部通过的文案
