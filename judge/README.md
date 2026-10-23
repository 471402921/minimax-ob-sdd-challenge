# OB 判题对接程序

MiniMax 赛题（`questionId=600077`，子题 `800514`，满分 20）在 OB 数据库大赛
判题平台上的领取—评测—回传实现。

协议依据：OB 方《数据库大赛联调相关文档》(2026-10-21)，`campId=21`。

## 快速开始

```bash
cp judge/.env.example judge/.env     # 填入 OB_JUDGE_TOKEN
set -a; . judge/.env; set +a         # 载入环境变量

# 自检：49 项安全与协议断言，不需要 token、不接触平台
python3 judge/test_harness.py

# 安全探测：验证连通性/Token/请求格式，保证不领取任何任务
python3 judge/probe.py

# 本地验证证据采集，完全不接触平台
python3 judge/run_judge.py --local-repo /path/to/some/repo

# 联调：走完整往返，处理一条任务后退出
python3 judge/run_judge.py --once --mode stub-pass
```

## 🔥 探测必须用 probe.py，不要直接打领取接口

`queryOneTaskRecord` **不是只读查询**。返回非 null 即代表任务已进入执行中，
必须评测并回传。

**2026-10-23 真实事故**：用 `questionId=600077` 跑了一次"看看有没有任务"，
结果真的领走了队列里的一条任务；更糟的是响应只做了摘要未落盘，
`taskId` 与 `gitRepo` 丢失，导致既不知道领了谁的任务、也无法回传
（回传必须 `taskId` + 完整 commit）。因回传不可逆，只能等 30 分钟超时让任务回队列。

`probe.py` 用**无交集的 questionId**（默认 seekdb 的 600069）探测：
文档 §2.3 说明可领范围 = 授权赛题 ∩ 请求筛选，两者无交集时服务端必然返回
`data:null`，**不可能领到任务**。用它验证连通性是零风险的，
并且它会直接拒绝 `--question-id 600077`。

`run_judge.py` 则会把完整领取响应先落到 `workdir/claims/` 再处理，
即使中途崩溃也能查清领了什么。

## ⚠️ 凭证安全

本仓库是 **PUBLIC**。以下规则没有例外：

| 凭证 | 处理方式 |
| --- | --- |
| MiniMax Token | 只经 `OB_JUDGE_TOKEN` 环境变量传入；`repr()` 已屏蔽；`.env` 已 gitignore |
| 选手 GitHub PAT | 只经 `GIT_ASKPASS` 环境变量传给 git 子进程；不进 argv、不进 `.git/config`、不进日志 |

`git clone https://user:pat@github.com/...` 是**错误做法** —— PAT 会出现在
`ps` 进程列表，也会写进 `.git/config`。本实现用 `GIT_ASKPASS` 绕开这两条泄漏路径，
并对所有输出（git stderr、异常消息、runlog）统一走 `redact()`。

`.gitignore` 已覆盖 `OB方参考/`、`.env`、`secrets/`、`runs/`、`workdir/`。
提交前可用 `git status --porcelain --ignored | grep '^!!'` 复核。

## 文件

| 文件 | 职责 |
| --- | --- |
| `ob_client.py` | 领取 `queryOneTaskRecord` + 回传 `synchronizeTaskRecord`，业务 code 校验 |
| `repo_fetch.py` | PAT 安全拉取、branch/commit 解析、failMsg 映射 |
| `evaluate.py` | 客观证据采集（只读）+ judge 接口 |
| `run_judge.py` | 主循环：领取 → 拉取 → 评测 → 回传 |
| `probe.py` | 零风险连通性探测（不可能领取任务） |
| `test_harness.py` | 安全与协议回归测试（49 项断言） |

## 平台协议要点（易错处）

这些是文档里明确警告、或会导致回传被拒的细节：

1. **不要拿领取接口做连通性探测。** 它有副作用 —— 返回 `taskId` 即代表任务
   已进入执行中，拿完不处理会一直挂到超时被重领。`data: null` 只表示本轮无任务。
   探测请用 `probe.py`（无交集 questionId）。
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
计划用 Jev（`typesafe_evaluate`）做结构化辅助判定，需注意建议书的一条硬约束：

> 「不能用一个黑箱大模型印象分替代本项」

所以 Jev 应当拆成**多个具名问题**（每题只判一件事、有明确 criteria、输出概率），
而不是丢一句"这个作品好不好"。同时需保留复核样本与争议处理路径。
`ob_client.py` 已实现 `resultType=2` 分支，万一 OB 后续改配置可直接启用。

## 联调前的待确认清单

- [x] 连通性 / Token / 请求格式已验证（`probe.py`，零领取风险）
- [x] 确认 600077/800514 保持 `resultType=1` 二值（通过满分 / 不通过 0）
- [x] 满分确认为 **20 分**（此前 30 为联调占位值）
- [ ] 向 OB 确认队列里现存任务是否为联调预埋的测试提交
- [ ] 确认评测耗时，据此定 `runTaskTimeOutMinutes`（默认 30min 待实测）
- [ ] 确认 `gitRepo` 里 PAT 的权限范围与过期时间
- [ ] 冻结通过标准后实现 `judge()` 自动判定（计划用 Jev 做结构化辅助）
