# FolioLoom 滚动调度器状态

- 日期：2026-07-27
- 分支：`fix/german-100k-gate`
- 已验证实现提交：`701c367`
- 当前阶段：任务 1–8 已完成并通过独立定向复核；准备连续执行任务 9–11。

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

## 验证证据

- Node 24.18.0 任务 1–4 定向测试：39 通过、0 失败。
- Node 24.18.0 TypeScript：`tsc --noEmit` 通过。
- Node 24.18.0 任务 1–8 组合测试：82 通过、0 失败。
- 500 bundle 上下文规划样本：17.4 ms（预热后，要求低于 50 ms）。
- 12 项与 16 项滚动规划性能门分别通过 `<50 ms` 与 `<250 ms` 断言。
- `git diff --check`：通过。
- Linux/Node 22 全量基线：749 通过、4 失败、1 跳过；失败均在本阶段改动之外：
  - 3 项既有测试使用 Windows 路径语义；
  - 1 项要求预先存在 desktop build 产物。
  这些门将在桌面阶段和最终验证前按不降级标准处理。

## 阻塞与下一步

- 外部阻塞：无。
- 下一步：在同一 `book-runner.ts` 连续上下文中按严格 TDD 完成任务 9–11：
  影子适配器、回溯受控并行和普通翻译 active 调度。
