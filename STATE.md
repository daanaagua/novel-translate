# FolioLoom 滚动调度器当前状态

- 日期：2026-07-27
- 分支：`fix/german-100k-gate`
- 离线验证基线提交：`d0604a4`
- 真实验证与修复：本文件所在提交
- 状态：计划任务 1–13 的确定性实现和离线验证已完成；后续德语《变形记》
  前 99,953 字符真实运行完成并通过无损审计，但暴露 token 硬包络和调度
  metrics 持久化缺陷，整体验收尚未通过。
- 不变量：逻辑窗口保持不可变，`CommitCoordinator` 顺序未改变，
  `book.db` schema 未改变，质量门和 token 阈值未降低。

## 主要提交

- `03ae3e4`：修复成本快照和调度遥测输入。
- `2fb8a59`、`6c9c47b`：完成任务 5–6 的上下文规划和请求投影。
- `a932b97`、`f6e8843`、`2de3d1d`、`701c367`：完成任务 7–8 的任务图、
  滚动规划及复核修复。
- `669d496`：完成任务 9 的 off、shadow、active 调度适配。
- `d009539`：完成任务 10 的受控并行知识回溯。
- `2bb450e`：完成任务 11 的普通翻译滚动调度。
- `3d3388b`、`824a83e`：完成任务 12 的 CLI、桌面端和合法 effort 修复。
- `aed8992`：完成任务 13 的 metrics、离线回放、多语言一致性、
  benchmark 副本准备器、README 和验证报告。
- `d0604a4`：记录任务 1–13 离线完成状态。
- 本文件所在提交：把 provider 流 `terminated` 归类为可重试故障，并阻止
  active fallback 绕过 token 包络。

## 验证证据

- `npm test`：825 通过、0 失败、1 跳过。
- `npm run typecheck`：通过。
- `npm run desktop:test`：desktop Node 109 通过、renderer 66 通过。
- `npm run desktop:typecheck`：通过。
- `npm run desktop:build`：main、preload、renderer 和产物校验通过。
- `git diff --check`：通过。
- 任务 13 组合测试：116 通过、0 失败；benchmark 准备器：4 通过、0 失败。
- 英语、德语、韩语、日语短样本的 off/shadow 提示覆盖、输出摘要和校验结果
  一致；临时数据库均已删除。
- Kafka 五任务离线回放：静态串行基线 492,000 ms，动态预测 213,000 ms，
  降幅 56.7%；token 为 227,700/273,240，资源冲突为 0，规划耗时
  12.823 ms。
- 本次真实验证修复后的相关测试：94 通过、0 失败。
- 本次修复后的全量 Node 测试：830 通过、0 失败、1 跳过；renderer：
  66 通过、0 失败；核心和 desktop TypeScript 均为 0 error；production
  build 和产物校验通过。

完整证据见
`docs/superpowers/reports/2026-07-27-rolling-horizon-scheduler-validation.md`。

## 真实验证结果

- 样本：Project Gutenberg 德语《变形记》正文前 99,953 Unicode scalar。
- 模型与模式：`deepseek-v4-flash`、`active/balanced`。
- 完成度：19/19 窗口、20/20 逻辑块、43 次模型调用。
- 审计：原文 SHA-256 不变，`coverageMissing=0`、
  `knowledgeConverged=true`、`strictExportable=true`、0 incident。
- 最后一次续跑 token：baseline 237,599、allowed 261,358、
  actual 359,975，超出允许值 98,617（37.7%）。
- 独立严格导出：成功，但 metrics 中 `scheduler=null`。

完整证据见
`docs/superpowers/reports/2026-07-27-kafka-german-100k-live-validation.md`。

## 未完成项

1. 持久化跨 `book run` 续跑的累计 baseline、allowed、actual 和 usage 完整性；
2. 让独立 `book export` 读取持久化调度 metrics；
3. 将协议降级、repair、上下文拆分和并发在途请求纳入同一 token 硬门；
4. 在全新隔离数据库上重新执行真实运行，确认 active 模式不再超限。
