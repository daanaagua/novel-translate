# FolioLoom 滚动调度器状态

- 日期：2026-07-27
- 分支：`fix/german-100k-gate`
- 已验证实现提交：`2bb450e`
- 当前阶段：任务 1–11 已完成；下一步执行任务 12。

## 已完成

- 完整读取交接、设计和 13 项实施计划；确认分支包含 `9f7ccce`，未修改 `book.db` schema。
- 对 `b05ca5f`、`4e402e1`、`3212d43`、`6338556` 完成一次轻量综合审查。
- 以失败回归测试修复两项 Important：
  - 越界 token ratio 快照现在确定性回退为 `snapshotStatus="invalid"` 的保守冷启动模型，不再产生 0-token 预测。
  - scheduler decision 只持久化明确的数值和受限 task ID 字段，未知正文或敏感字符串字段不会进入 runtime profile DB。
- 原审查代理定向复核：两项均关闭，无新增 Critical/Important。
- 任务 5：实现确定性上下文 evidence bundle 动态规划、依赖闭包、强制证据、
  风险覆盖、重复衰减与三档预算。
- 任务 6：公开结构化知识候选，按明确结构字段映射风险，支持请求级 revision
  精确选择，并按明确块距离衰减 utility；系统提示词和工具 schema 保持不变。
- 任务 7：实现统一任务 DAG、稳定资源冲突边、Kahn 完整性检查、滚动 horizon、
  直接 predecessor 查询和并发兼容判断。
- 任务 8：实现质量硬门、累计 token 包络、epsilon-Pareto subset DP、固定
  reservation、deadline/bounded/fallback 与三档外层滚动规划。
- 独立复核发现并以失败回归测试关闭 3 项 Important：reservation 完成事件
  不再延迟后继；存在合法部分计划时返回 bounded；前置关系读取不再执行
  `O(H × N²)` 的 ready-frontier 反推。
- 任务 9：实现 off/shadow/active 动态调度适配器和结构化回退；runner 的
  shadow 模式保持既有派发和调用次数，写入纯数值决策、观察与运行汇总；
  partial metrics 只投影聚合调度字段。
- 任务 10：提取共享单任务回溯状态机；active 模式按概念和翻译资源冲突
  建图，使用精确 CAS 领取、并发与在途 token permit、`allSettled` 独立提交
  和求解失败串行回退；off/shadow 保持原领取顺序。
- 任务 11：普通翻译 active 模式按 physical request 建图并生成合法的上下文、
  runtime 和协议变体；每个请求完成后立即写观察、更新成本模型并重规划，
  同时保留无损窗口、顺序提交、预算和既有恢复语义。
- 独立复核发现并以失败回归测试关闭 3 项 Important：重试不再扩大累计静态
  token 基线；校验失败不再作为成功样本学习；距离至少 24 块的可靠证据强制
  使用 rich 上下文。恢复失败和后续成功的 token 观察已分离。

## 验证证据

- Node 24.18.0 任务 1–4 定向测试：39 通过、0 失败。
- Node 24.18.0 TypeScript：`tsc --noEmit` 通过。
- Node 24.18.0 任务 1–8 组合测试：82 通过、0 失败。
- Node 24.18.0 任务 9 的 runner、adapter、report 与 desktop fixture
  组合测试：70 通过、0 失败。
- Node 24.18.0 任务 10 定向回溯、store、知识收敛与 Kafka 重放：
  15 通过、0 失败；executor 与完整 store 组合：44 通过、0 失败。
- Node 24.18.0 任务 11 定向测试：10 通过、0 失败；runner、planner、
  fault injection、store、report 与 desktop 组合：186 通过、0 失败。
- 任务 11 后 `tsc --noEmit` 与 `git diff --check` 均通过。
- 500 bundle 上下文规划样本：17.4 ms（预热后，要求低于 50 ms）。
- 12 项与 16 项滚动规划性能门分别通过 `<50 ms` 与 `<250 ms` 断言。
- `git diff --check`：通过。
- Linux/Node 22 全量基线：749 通过、4 失败、1 跳过；失败均在本阶段改动之外：
  - 3 项既有测试使用 Windows 路径语义；
  - 1 项要求预先存在 desktop build 产物。
  这些门将在桌面阶段和最终验证前按不降级标准处理。

## 阻塞与下一步

- 外部阻塞：无。
- 下一步：按严格 TDD 完成任务 12 的 CLI、桌面 runtime profile、三档入口和
  GUI 调度摘要。
