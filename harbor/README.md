# Harbor + Pi 审阅试点

**用途：复现人工审阅，不是让 Agent 替选手完成作业。**

跨模块当前状态与凭据加载统一见 [当前状态](../docs/当前状态.md) / [运行手册](../docs/运行手册.md)。
平台只读计数已可用；`batch.py` 已提供对**已领取且已固定快照**的资源受控批运行，仍不接队列领取或自动回传。完整方案见 [批量评测运行器](../docs/批量评测运行器.md)。

已核实 Harbor **0.25.0** 内置 `pi` Agent（支持版本固定、模型端点覆盖、session/trajectory
和用量记录）。`pi_preinstalled.py` 仅覆写安装步骤：镜像预装 Node **22.23.2** / Pi **1.4.0**，
避免每任务下载 nvm（本机 GitHub raw 会 reset），其余执行和轨迹逻辑继承 Harbor Pi。
不复制本机 `~/.pi/`、auth、记忆、MCP、
扩展配置。评测模型使用单独的 `OB_EVAL_MODEL_KEY`，不是当前 Pi 的凭据，也不是
`OB_JUDGE_TOKEN`。本试点没有领取/回传代码。

## 当前实现与边界

- `prepare.py`：从本地 Git **指定完整 commit 的对象**导出 `minimax/`，不使用可能被改动
  的工作树、不带 `.git` / PAT / 任务领取响应；拒绝软链接、submodule、疑似凭据和超大提交。
  文件模式和每个文件 SHA-256 写入 manifest；实际快照保留可执行位。
- `reviewer-skill/`：复用本次人工复核流程，明确区分实际复跑、静态检查和选手原始记录。
- `run_pilot.py`：专用 key 必填，空白 HOME，环境变量白名单；不继承平台/PAT/云密钥/宿主 Pi 配置。
- `batch.py`：默认只打印计划；`--execute` 才运行，硬上限4个trial/2个backend槽，检查资源、租约、固定快照、任务锁，按进程组监督并保存状态；无自动重试、claim或callback。
- `resources.py`：只读主机可用内存、磁盘、PSI、CPU和swap计数，核心信号缺失时fail-closed。
- `prewarm.py` + `images/`：离线依赖、seekdb预检和PowerContext服务预热；基础镜像使用本地不可变sha256 ID，选手快照只在运行时注入。
- `prewarm_wheels.py`：推荐路径。同一份已解析 wheel 集同时生成服务端 venv 和选手 `PIP_FIND_LINKS`，避免服务端1.2.0/客户端1.4.0 这类版本偏离；断网选手 bootstrap 实测 26 秒。旧 `prewarm.py`（源码装1.2.0）仅供对照。
- `dependency_audit.py`：只读扫 `requirements*.txt`/`pyproject` 的精确 pin，核对本地 wheel 元数据；`batch.py` 对未命中缓存的依赖默认拒绝启动（`--allow-uncached` 需明示）。这是“已解析依赖是否缓存”的检查，不证明传递闭包或官方源可用。
- `template/`：Agent 可联网，非 root、无 sudo、无宿主 Docker socket；固定提交在只读
  `/submission/minimax`，可在 `/work/review` 复制后运行；输出到 `/output`。
- 独立 verifier：无需模型，不执行学生代码，只核对报告身份、结构、证据文件哈希。
  本机缺 Harbor nftables 网络控制支持，使用 `tests/docker-compose.yaml` 的 Docker
  `network_mode: none` 断网，已实查容器 NetworkMode=none；task.toml 标 public 以避开
  不支持的 Harbor 动态策略。这是 **Docker 专用**实现，切换 provider 前须重新验证。
- **`reward=1` 只代表报告协议校验通过，不代表选手通过。** 真实失败报告也可以 reward=1。
  `batch.py`同样不把reward翻译成选手结论，后续仍需可信执行记录与独立复跑。
- `oracle` 只测试报告传递；不是实际作品全流程重放。真实 Pi 已使用独立 M Plan key
  对空的合成样例完成审阅，未判通过。task47213747已在Harbor外独立补验真实seekdb/PowerContext，详见当前状态。

本仓库公开，学生源码/运行日志不得放在可提交目录或上传 Harbor Hub。任务只生成到
已忽略的 `workdir/harbor/`，结果只放 `runs/harbor/`。不要执行 `harbor upload/publish`。

## 当前验证状态

- 批处理运行器已实际完成2路和4路合成 oracle 批次；4路批次4/4 finished，资源采样期间最低 MemAvailable约35.28GiB；无模型调用、无平台操作。
- 预热镜像 `harbor/prewarm:local` 已构建并通过健康检查：Pi1.4.0、seekdb `1.4.0.post2` 预检、PowerContext ready/scope/0600服务描述均通过。服务版本和依赖替换仍须在报告中披露。
- `harbor/review-wheels:local` 已构建（同源 wheel 集，PowerContext **1.4.0** 服务端）：选手 `requirements.txt` 断网安装25秒、`pip check` 通过；选手自启服务10秒、完整样例回放95秒。
- 内存对照（同一热路径）：1GiB 完成但 cgroup `max` 触顶3804次；2GiB 零触顶零OOM且耗时相同。默认改为普通任务2GiB、后端4GiB，拒绝1GiB默认。对照未含Pi模型进程。
- Harbor离线测试现为143项通过；新增资源、批处理、预热和服务启动失败保护测试。

- 既有 Harbor 试点离线回归和新增批处理/预热测试通过；既有 judge 回归185项通过、0 FAIL、0 skip；Harbor lint 无问题。
- 预热健康检查在本机需要通过一个仅用于sudo docker CLI的shim执行；`prewarm.py`本身不自动sudo，生产worker应给运行用户Docker权限。
- 用 `--print-config` 核对了单个 Pi Agent、明确 model、Pi 1.4.0 和专用 key 变量名。
  Harbor 0.25.0 的 `--model` 必须配 `--agent` 才会覆盖配置；wrapper 已处理。
- 无专用 key 时 wrapper 退出码 2，未启动 Harbor，也未使用宿主 Pi 认证。
- 初次 Docker 构建停在 Debian 包索引下载；改可配置的 HTTPS USTC 镜像源后构建成功。
  APT 签名校验未关闭，超时/重试有界。Oracle `oracle-none-20261028-145456` 全链路完成，
  独立 verifier `reward=1/report_valid=1`，无异常。
- `pi-preinstalled-20261028-150017`：MiniMax-M3.1-Flash + Pi 1.4.0 实际审阅完成，
  六项 blocked、recommendation=review、报告引用20个证据文件（目录共产出24个）、report_valid=1，没有平台回传。
  首次空样例过度探测，已收紧提示并禁止 env/printenv/凭据探测。
- `pi-lean-20261028-150753`：再次实际运行通过，六项 not_run、recommendation=review、
  8 个证据文件。Agent 阶段约170秒；仍需优化成本。详细用量、限制见 [VALIDATION.md](VALIDATION.md)。
- 尚未完成真实模型4路峰值、正反例校准或自动队列调度；不能用于自动给分。合成4路只验证容器生命周期和调度护栏。

## 本地检查（不领任务、不调用模型）

```bash
python3 -m unittest discover -s harbor/tests -v
python3 harbor/prepare.py --task-id 1 --synthetic
# 输出目录示例：workdir/harbor/review-1-000000000000
uvx --from 'harbor==0.25.0' harbor lint workdir/harbor/review-1-000000000000
uvx --from 'harbor==0.25.0' harbor run \
  --path workdir/harbor/review-1-000000000000 \
  --agent oracle --env docker --n-concurrent 1 --max-retries 0 \
  --jobs-dir runs/harbor
```

Docker daemon 权限属于运行机配置。本机需要 `sudo -n`，但不要 `sudo -E` 继承全部凭据、
修改共享 Docker 权限，或把 socket 挂进 Agent。生产应放专用 worker/VM。

模板通过 `DEBIAN_MIRROR` 构建参数选择源（当前默认 https://mirrors.ustc.edu.cn），
不更改宿主软件源。基础镜像固定版本标签，但**未锁 digest / OS/npm 传递依赖快照**；这仍是复现性限制。
发布生产镜像前要固定 digest、扫描镜像并记录 lock。不要对不认识的学生 compose 直接
在宿主机 `up`；每任务的 sidecar 与 Compose 支持要逐种 sandbox provider 验证。

## 真实提交快照（只读取，不调用平台）

```bash
python3 harbor/prepare.py \
  --task-id '<taskId>' \
  --repo 'workdir/task-<taskId>' \
  --commit '<完整40或64位SHA>'
```

导出不会覆盖已有任务。若发现可疑凭据、软链接等，暂停送入 Agent，让人决定脱敏/审查，
不能自动归为选手不通过。当前仅做有限凭据特征检查，**不保证检出所有秘密**。

## 独立 Pi 审阅（需专用模型凭据）

在专用 worker 的终端/secret manager 设置 `OB_EVAL_MODEL_KEY`；不在聊天、CLI 参数、
YAML 或仓库写明文值。本机配置文件为 `~/.config/minimax-ob-judge/mplan.env`（600），
启动评测前在子shell中 `set +x; source "$HOME/.config/minimax-ob-judge/mplan.env"`。
其中 `MINIMAX_CN_API_KEY` 是Pi原生别名，runner仅转发专用 `OB_EVAL_MODEL_KEY`。
模型/端点已实测如下；任务路径占位符须替换为实际生成目录：

```bash
# KEY 已由外部环境注入；wrapper 只转发专用 key，使用空白 HOME。
# 缺专用 key 会在启动 Harbor 前退出，绝不回退到当前 Pi 的凭据。
python3 harbor/run_pilot.py \
  --task 'workdir/harbor/review-<taskId>-<sha前12位>' \
  --model 'anthropic/MiniMax-M3.1-Flash' \
  --base-url 'https://api.minimax.cn/anthropic' \
  --api-format anthropic_messages
```

这里 `anthropic/` 是 Harbor 识别的**协议前缀**，实际 endpoint 是 MiniMax 国内，不会调用
Anthropic 服务。Pi 原生 provider 名是 `minimax-cn`，但 Harbor 的模型解析不识别该前缀，
直接使用会把整个字符串当 model ID；故使用已验证的上述映射。

`run_pilot.py` 不自动 sudo。需在有 Docker 权限的专用 worker 执行。协议也可选
`openai_chat_completions` / `openai_responses`。传 `--dry-run` 只验证配置（仍要求专用 key）。

不要同时在 YAML 和 CLI 追加不同 Agent；先用 `--print-config` / `--dry-run` 检查解析结果，
不要把含实际凭据的环境覆盖写进 config。未提供 key 时必须停止，不回退到 Pi 宿主认证。

### 联网不是不隔离

- Agent 当前 `public` 网络，可装依赖、访问模型/文档。正式 worker 需阻断云 metadata、
  宿主/其他试题服务和不必要内网；`public` 本身不是这些限制的保证。
- 固定提交不能改；源码修补后的成功不得算原提交成功。修改运行路径/端口也须留痕。
- OB token / PAT 只在控制层使用，不进入 Agent。Agent 只能建议，不能回传。
- **模型 key 若注入 installed Pi 环境，同进程工具及学生子进程可能读到。** 建议接网关，
  每 trial 短期凭据、独立额度、限流、模型白名单；不要交给它长期 master key。
- 无宿主挂载、无 privileged / host network。需要 Docker 的提交用受控 sidecar 或专用
  微虚机，不靠 Agent 在共享宿主上 sudo。前期遇到不支持的环境标 `blocked` 转人工。

## 数千份提交的后续架构（尚未实现）

```text
OB 平台 → 控制层领取/冻结版本 → 私密快照 → Harbor trial(Pi 审阅 + 专属后端)
                                         ↓
           人工/可信执行复核 ← 报告 + 日志 + trajectory + 用量 + 明确未验证项
                     ↓
           冻结 outbox → 回传 → 核对确认 → 原样重试
```

- 控制层仅在有 worker 空位时领取，租约覆盖构建/运行/复核/回传；OB 无独立续租接口。
- 一个 taskId 一个有效 owner；重领/重试冻结 commit、rubric、模型和结果，不能改分碰运气。
- 回传即使是人工也可能网络不确定，仍需要 immutable outbox + ACK 核对，不是自动化才需要。
- 首批 shadow review：正例、缺项、规格失效、伪造日志、skip/no-test、环境故障都要校准；
  不能只要求复现一条人工 pass，以免把人工失误当标准。rubric 未冻结前不自动提交。
- 缓存固定基础镜像/依赖，trial 独立数据，不共享任务状态；分开 Agent 并发、后端资源、
  模型 RPM/TPM；先测 p50/p95 耗时和用量，再定吞吐/成本和资源，不直接开百并发。

参考：[Harbor custom agents](https://harborframework.com/docs/agents/custom-agents)、
[Harbor 源码](https://github.com/harbor-framework/harbor)。API 以固定 0.25.0 实测 schema 为准。
