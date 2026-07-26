# FolioLoom 滚动调度器最终状态

- 日期：2026-07-27
- 分支：`fix/german-100k-gate`
- 已验证实现提交：`aed8992`
- 状态：计划任务 1–13 的确定性实现和离线验证已完成，并已推送到
  `origin/fix/german-100k-gate`。
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

完整证据见
`docs/superpowers/reports/2026-07-27-rolling-horizon-scheduler-validation.md`。

## 外部验证限制

本机没有可用的 DeepSeek 凭据，也没有可复制的真实 `book.db`，因此未执行
真实《变形记》五块 active/balanced 回溯，未产生真实 provider 的墙钟、
usage、缓存、限流及 `coverageMissing=0`、`knowledgeConverged=true`、
0 incident 审计证据。这不影响确定性实现和离线质量门结论。

获得源数据库和原模型凭据后，唯一剩余步骤是关闭源库写入者，使用
`folioloom/scripts/prepare-revalidation-benchmark.ts` 创建并校验 SHA-256
不变的副本，
再只对副本执行五块回溯；不得修改源库、阈值或质量门。
