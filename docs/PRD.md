# DeepNovel-Translator (DNT) 产品需求文档 (PRD)
**版本：** v2.1 (Confidence Mode)
**日期：** 2026-01-26
**状态：** In Progress

## 1. 项目概述
构建一个专用于超长篇、高难度文学小说（如 Gene Wolfe 的《新日之书》）的 AI 翻译系统。
核心理念从“预处理分析”转向**“在线流式处理 (On-the-fly Processing)”**、**“深度记忆增强 (Memory Augmented)”**与**“双模信度管理 (Confidence Modes)”**。

### 核心痛点解决方案
1.  **术语动态化**：放弃静态表，在阅读中动态提取、动态更新、多义词上下文匹配。
2.  **逻辑内化**：利用大模型自身的推理能力，通过 CoT (思维链) 和从句补全技术解决指代歧义。
3.  **长程记忆**：构建“实体档案”与“关系网”，解决跨章节伏笔与人物关系变迁问题。
4.  **信度分级**：引入 AI/人工双模式，平衡效率与准确性。

## 2. 技术栈
*   **语言：** Python 3.10+
*   **交互形式：** 
    *   **CLI:** 核心跑批工具 (Runner)，负责批量翻译、状态监控。
    *   **Streamlit WebUI:** 核心审阅与交互工具，负责术语校对、翻译修正、记忆库可视化。
*   **核心库：** `pydantic` (数据校验), `rich` (CLI 输出), `networkx` (可选，用于关系网构建)。
*   **LLM 支持：** 
    *   **Logic/Translation:** DeepSeek-V3 / GPT-4o / Claude 3.5 Sonnet (需支持长 Context 和 JSON Mode)。
    *   **Test Model:** ep-20260110004038-6h49x (Volcengine).

## 3. 系统架构流水线

### 3.1 翻译主循环 (Translation Loop)
不再分阶段预处理，而是以 **Chunk (文本块)** 为单位进行流式迭代：

1.  **Context Retrieval (上下文检索):**
    *   **Glossary Match:** 检索当前 Chunk 出现的术语（模糊匹配/忽略大小写），注入 Prompt。
    *   **Memory Injection:** 识别文中实体（人/物），检索其“实体档案”与“当前关系状态”，注入 Prompt。
    *   **Recent History:** 注入前文摘要 (Summary) 和上一段原文 (Last Sentence)。

2.  **LLM Inference (推理翻译):**
    *   **Input:** 原文 + 术语表 + 实体记忆 + 逻辑引导 Prompt。
    *   **Logic Enhancement:** 要求模型先补全省略的从句，或输出 `<analysis>` 步骤解析歧义。
    *   **Translation:** 输出最终译文。
    *   **Extraction:** 同时输出本段发现的新术语、更新的实体关系（JSON 格式）。

3.  **System Update (系统状态更新):**
    *   **Glossary Update:** 根据 **Confidence Mode** 决定新术语的状态 (`verified`/`pending`)。
    *   **Memory Update:** 解析实体关系变化，追加到本地知识库/关系网。

### 3.2 翻译模式
*   **直译 (Drafting):** 侧重逻辑准确性，包含从句补全和显式逻辑分析。
*   **润色 (Polishing):** (可选) 基于直译结果进行文学性重写，严禁修改术语和逻辑。

## 4. 核心功能模块详细设计

### 4.1 动态术语管理器 (Dynamic Glossary Manager)
*   **数据结构升级：** 新增 `status` 字段 (`verified`, `pending`, `rejected`)。
*   **置信度模式 (Confidence Modes):**
    1.  **Auto-Trust (AI High Confidence):**
        *   适用：初次批量翻译。
        *   行为：模型返回的新术语直接标记为 `verified`，立即生效。
        *   逻辑：信任模型对语境的判断，优先保证术语库的覆盖率。
    2.  **Human-Review (Manual):**
        *   适用：精细化校对。
        *   行为：模型返回的新术语标记为 `pending`。
        *   逻辑：`pending` 术语在后续翻译中仅作为“低权重建议”或不生效，直到人工在 WebUI 确认。

### 4.2 记忆与关系网 (Memory & Relation Network)
*   **目标：** 解决伏笔与上下文暗示。
*   **实现方案：**
    *   **Entity Dossier (实体档案):** 为每个主要角色/物品维护一个 Markdown/JSON 文件。
    *   **Relation Graph (关系网):** 记录 `(Entity A) --[relation, timestamp]--> (Entity B)`。
    *   **Temporal Awareness:** 处理插叙/倒叙，标注关系的“历史变迁”（如：Enemy -> Ally）。
*   **流程：**
    *   Prompt 中要求模型输出：`{"relations": [{"subject": "Severian", "object": "Foila", "relation": "recognized", "context": "..."}]}`
    *   系统解析并更新本地 Graph。

### 4.3 逻辑增强 (Logic Boosting)
*   **策略:** 不依赖外部正则。
*   **Prompt Engineering:**
    *   **Thinking Stream:** 强制模型输出 `<analysis>...</analysis>`，展示指代消歧过程。
    *   **Clause Completion:** 显式补全省略成分。

## 5. 交互流程

### 5.1 CLI (Runner)
*   `python main.py init <book_id> <file_path>`: 初始化。
*   `python main.py translate <book_id> --glossary-mode [auto|manual]`
    *   `--glossary-mode auto`: 新词自动通过。
    *   `--glossary-mode manual`: 新词需人工审核。
    *   显示：进度条、当前处理的术语、Token 消耗。
    *   **不暂停**，错误记录到日志。

### 5.2 Streamlit WebUI (Reviewer)
*   **Dashboard:** 项目列表、总体进度。
*   **Editor:** 
    *   左侧：原文。
    *   中间：译文（可编辑）。
    *   右侧：
        *   **Glossary Panel:** 显示本段命中的术语，支持新增/修改术语。支持 **Review Mode** 筛选 `pending` 术语。
        *   **Memory Panel:** 显示相关实体档案。
        *   **Logic View:** 显示模型的思维链/补全过程。
*   **Actions:** "Re-translate chunk" (修改术语后重翻), "Approve" (确认并保存)。

## 6. 里程碑 (Roadmap)
*   **Phase 1 (Core Loop):** 
    *   [DATA] 更新 Schema 支持 `status` 和 `thinking`。
    *   [CORE] 重构 TranslationEngine，移除 LogicAnalyzer，接入 Context -> Inference -> Update 闭环。
    *   [CORE] 实现 Glossary Manager 的置信度逻辑。
*   **Phase 2 (Memory System):** 
    *   [FEAT] 实现实体档案 (Entity Dossier) 的读写与注入。
    *   [CORE] 实现关系网更新逻辑。
*   **Phase 3 (Review System):** 
    *   [UI] 完善 Streamlit WebUI，支持人工介入修正术语和记忆。

## 7. 数据结构示例

### 7.1 LLM 响应结构
```json
{
  "analysis": "Here implies the lazaret...",
  "translation": "中文译文...",
  "new_terms": [
    {"src": "lazaret", "tgt": "野战医院", "type": "place"}
  ],
  "relations": [
    {"sub": "Severian", "obj": "Foila", "rel": "acquaintance"}
  ]
}
```
