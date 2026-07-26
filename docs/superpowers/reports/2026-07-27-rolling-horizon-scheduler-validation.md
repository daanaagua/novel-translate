# 滚动窗口动态调度器验证报告

- 日期：2026-07-27
- 分支：`fix/german-100k-gate`
- 验证范围：任务 1–13 的确定性实现、离线回放、影子一致性、数据库副本准备器、CLI、桌面端和生产构建
- 结论：确定性实现与离线质量门通过；真实 DeepSeek 五块回溯因本机缺少凭据和源数据库而未执行

## 验证环境

- 操作系统：Ubuntu Linux，内核 `6.8.0-101-generic`，x86_64，KVM
- CPU：2 个在线 vCPU，AMD EPYC 9754
- 内存：7.4 GiB，交换空间 1.9 GiB
- Node.js：24.18.0
- npm：10.9.7
- SQLite：3.53.1
- 模型与 API：未连接；本机未设置 DeepSeek、OpenAI 或 Anthropic API 凭据
- provider cache：未使用；离线回放使用固定遥测，短样本使用本地确定性 faux provider

## Kafka 五任务离线回放

固定回放只包含任务时长、token、风险和资源标识，不包含原文、译文或提示词。`speed` 档位的结果如下：

| 指标 | 结果 | 门槛 |
| --- | ---: | ---: |
| 静态串行基线 | 492,000 ms | — |
| 动态计划墙钟 | 213,000 ms | ≤ 246,000 ms |
| 相对基线降幅 | 56.7% | ≥ 50% |
| 已计划任务 | 5 | 5 |
| 资源冲突 | 0 | 0 |
| 静态 baseline token | 227,700 | — |
| 动态计划 token | 227,700 | ≤ 273,240 |
| speed token 比例 | 1.00× | ≤ 1.20× |
| 单次规划墙钟 | 12.823 ms | < 50 ms |
| 规划状态 | `optimal` | 非 `fallback` |

回放测试同时检查每个并发 action 的写资源不相交，并确认五项任务各出现一次。

## 英语、德语、韩语和日语影子一致性

测试使用仓库已有的短小合成语言样本，不下载或保存小说文本。每种语言分别在独立临时数据库中执行 `off` 和 `shadow`：

- 模型可见提示词以 SHA-256 比较，四种语言均逐请求一致；
- 请求覆盖的 window 和 block ID 完全一致；
- 活动译文只比较私有摘要，逐块一致；
- window 状态、警告、错误和整本结果一致；
- 实际 context profile、baseline token 和实际 token 一致；
- `shadow` 只增加规划状态、预测与 decision 数值，不改变派发或提交；
- 四种语言均为 0 fallback、0 incident，测试结束后删除临时数据库。

token 预测误差按 `|预测 token - 实际 token|` 计算并验证为有限非负值。由于 faux provider 不是供应商计费实现，该误差只用于验证 off/shadow 的统计路径，不作为真实模型精度结论。

## 最终调度 metrics

`v5-book-scheduler-metrics-1` 输出包含：

- profile、mode 和 planner status；
- legacy、planned 和 actual 墙钟；
- baseline、allowed、predicted 和 actual token，以及包络是否超限；
- context profile、effort 和协议计数；
- planner deadline、fallback、限流和恢复计数；
- token usage 完整性。

导出只保留固定枚举和聚合数值。测试确认未知 effort 会合并为 `unknown`，不会输出原文、译文、提示词、API Key、模型原始响应或任意 prose-shaped 字段。

## 数据库副本准备器

`scripts/prepare-revalidation-benchmark.ts` 的测试覆盖以下行为：

1. 拒绝相同输入输出路径；
2. 拒绝覆盖已有输出；
3. 使用 `copyFileSync` 创建新数据库；
4. 在副本事务中选择五项已完成精确重译及其当前活动 replacement；
5. 为活动 translation 创建五项新的 pending benchmark task；
6. 只在副本中清空对应 binding usage 并标记为 `stale`；
7. 数量不足时回滚并删除副本；
8. 源数据库准备前后 SHA-256 一致；
9. 检测到源数据库字节变化时以 `BENCHMARK_SOURCE_MUTATED` 失败并删除副本。

临时 SQLite 测试为 4 通过、0 失败。源库保持 0 项 pending，副本恰有 5 项引用活动 translation 的 pending task。没有修改 `book.db` schema。

## 自动质量门

从 `folioloom/` 使用 Node.js 24 执行：

```text
npm test
npm run typecheck
npm run desktop:test
npm run desktop:typecheck
npm run desktop:build
git diff --check
```

结果：

- 核心 Node：825 通过、0 失败、1 跳过；
- 核心 TypeScript：0 error；
- desktop Node：109 通过、0 失败；
- renderer：66 通过、0 失败；
- desktop TypeScript：0 error；
- Electron production build：main、preload、renderer 和构建产物校验通过；
- `git diff --check`：通过。

第一次全量运行暴露两项测试环境问题：一个旧测试在 Linux 上使用 Windows 绝对路径样本，另一个构建产物测试在执行 production build 前找不到 preload。前者改为宿主平台绝对临时路径且保留原安全断言；后者由规定的 `desktop:build` 生成产物。随后全量运行通过，未降低任何阈值。

## 真实五块回溯限制

本机未发现可用的 DeepSeek 凭据，也未在工作区或 `/home/ubuntu` 下发现可复制的 `book.db`。因此没有：

- 对真实项目调用 benchmark 准备器；
- 发起付费模型请求；
- 测量真实 provider 墙钟、usage、缓存、限流或波动；
- 生成真实 `coverageMissing=0`、`knowledgeConverged=true` 和 0 incident 审计；
- 证明真实运行低于 246 秒或 token 不超过静态基线 110%。

这是一项外部验证限制，不是确定性实现失败。具备源数据库和原模型凭据后，必须先关闭写入该数据库的进程，使用准备器创建新副本并确认源 SHA-256 不变，再对副本执行 `active/balanced` 五块回溯。真实结果不得通过修改阈值、质量门或源数据库来补齐。
