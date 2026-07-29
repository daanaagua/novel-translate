# FolioLoom 调度控制面当前状态

- 日期：2026-07-29
- 承接分支：`fix/execution-worker-local-20260729`
- 回并目标：`fix/german-100k-gate`
- 规格：`docs/superpowers/specs/2026-07-28-token-ledger-and-scheduler-control-plane-design.md`
- 计划：`docs/superpowers/plans/2026-07-28-token-ledger-and-scheduler-control-plane.md`
- 真实验收报告：`docs/superpowers/reports/2026-07-28-kafka-german-100k-token-ledger-live-validation.md`
- 状态：**ExecutionWorker 抽取完成；2026-07-29 新一轮双语 100k release gate
  未通过，release 冻结。**

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

## 2026-07-29 ExecutionWorker 验证

- `npm test`：855 tests，854 pass，0 fail，1 skip；
- `npm run typecheck`：0 error；
- `npm run desktop:test`：Node 108 pass / 1 skip；Renderer 66 pass；
- `npm run desktop:typecheck`：0 error；
- `npm run desktop:build`：production build 与 preload 验证通过；
- `git diff --check`：通过；
- 德语 100k 沿用已隔离且通过的 P4 真跑结果，未覆盖唯一真跑数据库。

## 2026-07-29 双语 100k Release Gate

报告：
`docs/superpowers/reports/2026-07-29-german-and-children-of-time-100k-release-gate.md`

- 德语全新 run：8 completed、1 warning、10 pending；actual 243,029 >
  allowed 230,184；strict exportable=false；
- 《时间之子》全新 run：单窗口 probe 后 actual 已超 allowed，停止剩余 18
  个窗口以避免继续产生无效费用；
- release、tag、push、版本号更新均未执行。

### 本轮代码修复

1. `NO_LEGAL_PLAN` 返回稳定调度错误；仅在数值确实超限时返回 token envelope
   错误，不再误报并发容量；
2. resume baseline 按 stable logical window ID 去重，physical request 重组不再
   扩大 allowed；
3. quality retry 不重复失败的 framed 协议；
4. active settle 后真实 usage 超包络时不再返回表面成功；
5. 桌面 active pause 改为等待当前 provider wave 到达 durable boundary 后协作
   暂停，避免中断调用产生未知 usage 并令同一 run 无法 resume；
6. 应用退出使用有界 grace；超时 abort，再经有界等待继续退出，provider 卡住
   不会无限阻塞 Electron shutdown。

最终代码门禁：

- `npm test`：861 tests，860 pass，0 fail，1 skip；
- `npm run typecheck`：通过；
- `npm run desktop:test`：Node 109 pass / 1 skip；Renderer 66 pass；
- `npm run desktop:typecheck`：通过；
- `npm run desktop:build`：通过；
- `git diff --check`：通过；
- 独立审查无新增 Critical / Important。

### 剩余阻断项

- high/max effort reasoning reserve 低估；
- lexical anchor 静态预算低估；
- CLI projection 与 durable ledger 最终 spent 存在差异；
- `conservativeHorizonFloor` 仍需按冻结规格接入非零静态下界；
- 修复后必须以全新数据库重新跑通德语和英文两个 100k，再执行全量门禁，才可
  更新 release。
