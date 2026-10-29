# Harbor 并发评测方案

更新：2026-10-29。实现和命令以[批量评测运行器](批量评测运行器.md)为准；本页讲容量和分阶段计划。
**QPS不是trial并发。** 模型请求速率、在途请求、Pi session、隔离评测容器必须分别管理。

## 1. 当前建议与实测依据

本机为共享48逻辑CPU/约91GiB内存主机，2026-10-29下午采样可用内存约36.5GiB，
磁盘剩约300GiB，已有其他服务运行。CPU当时空闲较多，但内存、磁盘及既有服务波动限制并发。
数据不是实时状态，启动前由resources.py重新采样。

- batch.py总trial硬上限4，backend profile硬上限2；每trial先保持1个Pi session。
- 当前backend槽为整条trial占用：4个全是backend时最多2个同时跑，其余等候，不是4个完整后端并行。
- start_service.sh为单真实任务试运行入口，固定trials=1/backends=1。
- YAML的n_concurrent_trials=4不决定batch总量：batch每个子Harbor显式单并发，自己分配槽位。
- 2路、4路合成oracle已实际完成，但负载很轻，不能推断真实4路峰值或稳定吞吐。
- 真实单样本Harbor约16分25秒，另有独立后端补验耗时；不据此承诺固定任务/小时。

## 2. 资源预算和阶段

light槽暂按4GiB、backend槽按6GiB准入预算；从基线可用内存扣除16GiB预留
（12宿主波动、4控制/校验），启动前再次检查实时余量。模板agent环境2CPU/4GiB，
PowerContext现放同trial容器内而不是独立2GiB sidecar；额外2GiB是保守调度预算，不是已设置的服务配额。

资源不足停止启动，不通过杀其他用户服务回收内存。当前未施加整个worker父cgroup总预算，
还需测memory.peak/OOM/CPU/I/O；配置限制不等于实测峰值。

1. **当前：1条真实任务流程**。健康检查、报告落盘、终态与清理验证，人工看结果。
2. **下一阶段：2路真实影子评测**。记录后端初始化、模型延迟、峰值、超时/报告率，不能只挑通过样例。
3. **条件满足再4路混合负载**。部分light、部分backend，总量不超过4且heavy不超过2。
4. **6/8以上需重新容量评估**。资源回收或扩机后再试，不沿用早期“8–16”作为当前授权目标。

初始资源拒绝线：MemAvailable<12GiB、磁盘free<150GiB、memory PSI some avg10>1%；
核心信号缺失fail-closed。持续换页、OOM或其他服务压力须暂停扩容，阈值需真实负载校准。

## 3. 解决环境时间浪费

已落地：

- 固定Node/Pi基础层，不每trial下载nvm；预热PowerContext独立venv和seekdb wheel缓存。
- Git对象导出基础设施源码并记录hash，不把旧选手容器commit成共享镜像。
- bootstrap在agent前建立PowerContext健康服务及服务端scope，临时token仅通过环境注入。
- 每次安装限180秒，pip超时/重试有界；明确兼容版本替换，不篡改选手源代码。
- 早期报告checkpoint、保留原始记录；真实服务测试删除scratch本地镜像，避免fallback假通过。
- 普通supervisor+systemd托管，不让LLM轮询pgrep等待。用户问“耗时太长”不是取消授权。

尚待实现：独立clone/build/verifier队列、跨阶段后端槽租借、模型网关、公平排队、自动claim。
预热镜像构建应在真实任务领取前完成。SQLite fallback、验证脚本exit0和report_valid都不能冒充后端通过。

## 4. 安全和租约

- 仅容量就绪再claim；batch不是claim入口，真实任务需pending与commit一致且安全租约覆盖wall上限+15分钟。
- 独立数据目录、scope、容器；不共享选手状态或连接已有宿主服务。
- verifier断网；seekdb可能在network none下初始化失败。专属internal网络仍不是完整宿主/metadata隔离，
  生产需额外访问控制；当前public agent网络同样不能作为隔离保证。
- installed Pi的master key仍可能被不可信子进程读取，短期网关未实现。不能把服务token临时化误称模型key已隔离。
- taskId锁只覆盖使用本运行器的进程，不是平台分布式owner；终态需人工审核与匹配ACK。
- 不确定回传冻结请求、不改分碰运气；outbox尚未完整接入。过期覆盖不是批量解决方案。
- 任务中断后先核对本批容器清理再重试；新trial保留旧证据，不能叫模型会话断点续跑。

## 5. 模型吞吐与机器容量分开

早期用户给出的QPS=4和后续高并发key均不构成trial容量证据。当前专用key已配置，
但此文不承诺其套餐/RPM/TPM或100 QPS压测结果。旧M Plan文档和短探针见[历史评估](MPlan并发评估.md)。

多session可以拆独立审查维度，但需要独立证据目录、报告汇总、资源预算；当前仍采用1 session/trial。
先把测试、缓存、后端准备和过程监督交给确定性程序，避免重复读代码和重复装环境，比盲加session更可控。
