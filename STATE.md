# FolioLoom 调度控制面当前状态

- 日期：2026-07-29
- 承接分支：`fix/execution-worker-local-20260729`
- 回并目标：`fix/german-100k-gate`
- 规格：`docs/superpowers/specs/2026-07-28-token-ledger-and-scheduler-control-plane-design.md`
- 计划：`docs/superpowers/plans/2026-07-28-token-ledger-and-scheduler-control-plane.md`
- 真实验收报告：`docs/superpowers/reports/2026-07-28-kafka-german-100k-token-ledger-live-validation.md`
- 状态：**P0–P3 完成；P4 德语 100k 真实验收通过；ExecutionWorker 抽取完成。**

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

## P1/P2/P3 完成

- `admission-controller.ts`：唯一发车/结算入口
- `congestion-sensor.ts`：AIMD 拥塞传感
- `telemetry-sink.ts`：成本模型与 profile 观察
- `execution-worker.ts`：请求预算、fragment admission、模型调用、protocol/context recovery 与 usage 归集
- active 模式 `tokenGate: "external"`：并发仍由 AIMD，token 硬门只信 ledger
- `docs/ARCHITECTURE.md`：冻结 TypeScript 生产内核与 Python 导入/历史边界
- lexical anchor：成功调用使用供应商真实 usage 结算；缺失 usage 时继续保守标记不完整，不伪造 usage

## 不变量

- 逻辑窗口不可变；`CommitCoordinator` 顺序不变；不加 book.db 关系表；质量门未降。

## 2026-07-29 验证

- `npm test`：855 tests，854 pass，0 fail，1 skip；
- `npm run typecheck`：0 error；
- `npm run desktop:test`：Node 108 pass / 1 skip；Renderer 66 pass；
- `npm run desktop:typecheck`：0 error；
- `npm run desktop:build`：production build 与 preload 验证通过；
- `git diff --check`：通过；
- 德语 100k 沿用已隔离且通过的 P4 真跑结果，未覆盖唯一真跑数据库。
