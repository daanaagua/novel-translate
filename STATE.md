# FolioLoom 滚动调度器当前状态

- 日期：2026-07-28
- 分支：`fix/german-100k-gate`
- 规格：`docs/superpowers/specs/2026-07-28-token-ledger-and-scheduler-control-plane-design.md`
- 计划：`docs/superpowers/plans/2026-07-28-token-ledger-and-scheduler-control-plane.md`
- 状态：P0 Token 账本与调度投影已落地；P1–P4 未完成。德语 100k 真实验收仍须在全新库重跑后判定。

## P0 已完成

1. 纯函数 `TokenLedger`：baseline/reserve/settle/release、硬门、重放；
2. `book.db` 事件持久化与 `scheduler_run_projection`；
3. 独立 export 从 store 读取 scheduler metrics（可不传 `options.scheduler`）；
4. `book-runner` 续跑恢复 baseline/spent，主翻译路径 admit→settle；
5. revalidation drain 的模型消耗事后并入同一 run ledger；
6. run 结束与包络耗尽时刷写 projection。

## 仍未完成

1. **P0 深化**：repair / protocol_switch / context_split / anchor 各自独立 requestId 预留（当前二次调用费用计入主 request 的 settle 总量，已计入 spent，但发车前未单独 canReserve）；
2. **P1**：拆 AdmissionController / ExecutionWorker / TelemetrySink；统一 AIMD in-flight 读 ledger.reserved；
3. **P2**：active 下 AIMD 降为拥塞传感，不再 tryAcquire 跳过任务；
4. **P3**：Python/TS 边界文档冻结；
5. **P4**：全新隔离库德语 ~100k `active/balanced` 真跑，确认 `actual ≤ allowed`。

## 不变量

- 逻辑窗口保持不可变；
- `CommitCoordinator` 顺序未改变；
- `book.db` 未新增关系表（仅扩展事件 kind）；
- 质量门与 token 阈值未降低。

## 验证（本机 P0）

- `test/token-ledger.test.ts`：通过
- `test/token-ledger-store.test.ts`：通过
- `test/book-runner.test.ts`：64 通过
- `npx tsc --noEmit`：0 error

合并前建议再跑：`npm test`、`npm run desktop:test`、`npm run typecheck`。
