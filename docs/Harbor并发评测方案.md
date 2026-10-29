# Harbor 并发评测方案

更新：2026-10-29。当前 Harbor 已跑通单条合成试点和一次真实提交的独立复核，但真实提交曾因
依赖安装耗时触发30分钟 AgentTimeout。本文给出扩并发前的工程结论；**当前不建议直接开几十/几百并发**。

## 1. 当前已知瓶颈

| 瓶颈 | 证据 | 影响 |
| --- | --- | --- |
| Agent依赖安装 | 真实task47213508重复pip安装长时间卡住；评测方后来离线装wheel才继续 | 每trial不应联网安装依赖 |
| PowerContext依赖 | 真实提交的完整状态链要服务端和根仓库依赖 | 每trial应有预构建服务/镜像，不能让Agent临时搭建 |
| seekdb依赖 | 固定提交嵌入式seekdb可运行，但初始化耗时/内存需单独测 | 后端资源池需限流，不能与Agent并发数等同 |
| Harbor网络控制 | 本机没有nftables动态网络控制 | Docker verifier 用 `network_mode:none`；其他provider要重新验证 |
| 评审Agent耗时 | 合成负例首次约170秒Agent执行；真实提交Agent因安装阶段超过30分钟 | 先用5–10条样本测p50/p95，再定并发 |
| 报告/超时 | Agent超时可能没有report.json；人工仍需区分评测失败与选手失败 | 超时保存trajectory/日志，不能直接给选手0分 |
| 任务租约 | 平台任务领取后必须在时限内回传；不能批量先领再排队 | 控制层领取量必须小于可立即执行容量 |

## 2. 推荐并发分层

### Phase 0：单条回归

每次只跑一条已知提交，确认：

- Harbor environment build 使用缓存镜像；
- Agent workspace 无OB Token/PAT/宿主Pi auth；
- seekdb/PowerContext 是trial专属服务；
- verifier实际断网；
- report schema、证据hash和trajectory都能落盘；
- 超时、网络错误、缺依赖可以分类。

### Phase 1：2–4并发影子评测

只对已人工知道结论的正例/负例/环境故障样本运行。建议：

- `n_concurrent_trials=2` 起步，稳定后4；
- `n_concurrent_agents` 不超过trial并发；
- 每个trial独立 project_id、PowerContext scope、seekdb数据目录和临时端口；
- 预装评测镜像和所有常用Python wheel，禁止Agent执行无界pip安装；
- 每个后端设置并发/内存上限，任何一个服务池满时新任务排队，不领取平台任务；
- 记录每条的环境构建、Agent、verifier时长，输入/缓存/输出token，失败类型和报告完整率。

### Phase 2：8–16并发容量试验

只有Phase 1的结果稳定后执行。测试目标不是分数，而是：

- p50/p95总时长和Agent时长；
- p95 token、RPM/TPM限流、429/5xx比例；
- seekdb/PowerContext CPU、内存、句柄、磁盘和服务错误；
- Harbor环境构建缓存命中率；
- report_valid率和超时率；
- 任务租约剩余时间是否足够完成回传。

若模型出现限流，先降低Agent并发或增加退避；不要通过重复claim增加吞吐。

### Phase 3：生产批次

生产批次应由控制层负责：

```text
只读queue-count → 检查worker/后端/模型容量 → claim少量任务
    → 固定commit安全快照 → Harbor trial
    → 人工/策略审核 → frozen outbox + ACK
    → 成功回传后清理任务凭据
```

- `waitCount` 只是待执行任务数，不是建议并发数；`runCount` 包含超时未结束任务。
- 一个taskId只允许一个owner；领取数不超过当前可执行槽位。
- 领取时即开始租约，不能先领取几百条再等待模型资源。
- 回传网络不确定时冻结原请求并重试同一请求，不能改分数/理由/版本。
- 过期任务只有用户/平台明确允许时才能用显式 `--allow-expired` 尝试；这不是生产队列策略。

## 3. 任务镜像和依赖策略

当前 template 镜像已经预装 Node 22.23.2 和 Pi 1.4.0，但选手项目依赖仍可能触发下载。
生产化应增加两类预构建镜像/缓存：

1. **Agent基础镜像**：Pi、Python、Git、通用测试工具和固定wheel缓存。
2. **服务镜像**：seekdb、PowerContext固定版本；服务健康检查通过后才启动Agent。

不允许把学生提交的Dockerfile/Compose直接在共享宿主执行；需要额外服务时，用经过审查的
provider sidecar或专用VM。提交说明里缺服务时标 `blocked`，不要临时接宿主已有服务。

## 4. MiniMax M Plan 单Key并发结论

官方M Plan FAQ只公开说明：

- 有RPM/TPM速率限制，超出通常约一分钟恢复，高峰期可能动态收紧；
- 文本模型受5小时窗口和周窗口限制；
- M Plan面向个人交互式场景，生产环境官方建议使用按量付费；
- 文档没有给出可据此承诺的固定并发数。套餐Go/Explore/Build主要是相对额度，不等于固定并发槽位。

因此不能从Key类型或套餐名称推导“支持N个并发”。用户提供的QPS=4可作为**初始影子评测上限**，
不是生产保证；QPS不等于完整Agent审阅并发，因为长上下文会受TPM限制。遇到429/限速/延迟抖动
立即退回2或1。每一级至少记录：

- 10–20条短请求：RPM、首Token延迟、总时延、429比例；
- 3–5条真实审阅任务：输入/缓存/输出token、p95、超时率；
- 5小时/周额度变化（只能在用户同意下观察，不要输出Key）；
- 高峰期重复一次，不能只测一次得结论。

模型并发和Harbor并发是两个上限：即使M Plan暂时承受4个请求，seekdb/PowerContext或Docker也可能只能承受2个。
生产几千份评测应使用按量付费/专用网关，并做每Key RPM/TPM配额、短期Key、模型白名单和成本限额；
不要把个人M Plan Key当作生产吞吐保证。

## 5. 当前建议配置

当前 `harbor/jobs/pi-local.yaml` 已将初始影子并发设为4，适配用户提供的QPS=4；这仍是
shadow cap，不是生产承诺。建议生产控制层根据429/TPM/p95和后端压力动态降到2/1：

```yaml
n_concurrent_trials: 4
retry:
  max_retries: 0
```

模型Key仍只通过 `OB_EVAL_MODEL_KEY` 的外部私密环境注入。不要把它写入YAML、CLI参数、任务目录、
Harbor artifacts或Agent prompt。扩容前先完成真实任务的报告收尾、依赖预装和PowerContext sidecar方案。
