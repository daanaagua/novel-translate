# FolioLoom 滚动调度器当前状态

- 日期：2026-07-28
- 分支：`fix/german-100k-gate`
- 规格：`docs/superpowers/specs/2026-07-28-token-ledger-and-scheduler-control-plane-design.md`
- 计划：`docs/superpowers/plans/2026-07-28-token-ledger-and-scheduler-control-plane.md`
- 真实验收报告：`docs/superpowers/reports/2026-07-28-kafka-german-100k-token-ledger-live-validation.md`
- 状态：**P0 + 德语 100k active/balanced 真实验收通过**。P1–P3 未做。

## 已完成

### P0 Token 账本

1. 纯函数 `TokenLedger`：baseline/reserve/settle/release、硬门、重放；
2. `book.db` 事件持久化与 `scheduler_run_projection`；
3. 独立 export 从 store 读取 scheduler metrics；
4. `book-runner` 续跑恢复包络；主译 admit→settle；
5. revalidation drain 并入同一 run ledger；
6. protocol/context 二次调用发车前包络预检（临时 reserve，usage 归 parent settle）；
7. lexical anchor 独立 baseline/reserve/settle。

### P4 真实验收（2026-07-28）

- 项目：`projects/kafka_verwandlung_100k_gate2/`（全新库）
- run：`836e57f2-db0b-487c-ab09-b6b443e0bfb3`
- 模型：`deepseek-v4-flash`，`active/balanced`
- 19/19 窗口完成；严格导出通过；`scheduler ≠ null`
- token：baseline 575,480 / allowed 633,028 / actual **422,510**（未超限）

## 仍未完成

1. **P1**：拆 AdmissionController / ExecutionWorker / TelemetrySink；AIMD in-flight 读 ledger.reserved；
2. **P2**：active 下 AIMD 降为拥塞传感；
3. **P3**：Python/TS 边界文档冻结；
4. anchor 路径补齐真实供应商 usage（当前 `tokenUsageComplete` 可能为 false）。

## 不变量

- 逻辑窗口不可变；`CommitCoordinator` 顺序不变；不加 book.db 关系表；质量门未降。

## 验证

- `npm test`：核心套件绿（偶发 planner 50ms 性能抖动与负载相关）；
- `npx tsc --noEmit`：0 error；
- 德语 100k 真跑：见上报告。
