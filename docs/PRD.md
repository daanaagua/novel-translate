# DeepNovel-Translator (DNT) 产品需求文档 (PRD)
**版本：** v1.0
**日期：** 2026-01-23
**状态：** Draft

## 1. 项目概述
构建一个专用于超长篇、高难度文学小说（如《新日之书》）的 AI 翻译系统。
核心目标是解决通用大模型在长文本翻译中的四大痛点：
1.  **上下文遗忘**：前文剧情丢失。
2.  **术语僵化**：无法根据语境灵活调整专有名词译法。
3.  **指代歧义**：复杂的跨段落逻辑连接错误（如 Foila 陷阱）。
4.  **文风割裂**：不同章节文风不统一。

## 2. 技术栈与形式
*   **语言：** Python 3.10+
*   **交互形式：** **CLI (命令行界面) + WebUI (可选后期)**
    *   *决策理由：* 初期专注于核心逻辑验证和批量处理效率，CLI 最适合开发和调试。后期可使用 Streamlit 或 Gradio 快速搭建可视化审阅界面。
*   **核心库：** 
    *   `langchain` / `ell` (待定，视复杂度而定，目前建议原生 API 调用以保持控制力)
    *   `pydantic` (严格的数据结构校验)
    *   `rich` (美观的 CLI 输出)
*   **LLM 支持：** DeepSeek-V3 (主力逻辑/翻译), Claude 3.5 Sonnet (备选润色), GPT-4o (备选逻辑分析)。

## 3. 系统架构模块

### 3.1 预处理流水线 (Preprocessing)
*   **Chapter Parser:** 基于正则将 txt/epub 切分为章节。
*   **Semantic Chunker:** 智能分块，保留完整段落，识别场景分隔符。
*   **Glossary Extractor:** 预扫描提取潜在术语。

### 3.2 逻辑分析流水线 (Logic Analysis) - *Core Feature*
*   **Ambiguity Detector:** 扫描 Chunk，识别高风险衔接词 (`I mean`, `Which is`)。
*   **Logic Resolver:** 调用 LLM 生成 `logic_map.json`，显式解析指代关系。
    *   *Input:* 复杂文本段落。
    *   *Output:* JSON 结构的逻辑解析（修饰对象、时间关系）。

### 3.3 翻译核心流水线 (Translation Engine)
采用 **"直译 -> 润色"** 双流架构。

*   **Step 1: 直译 (Drafting)**
    *   **输入：** 原文 + 结构化术语表 + 逻辑解析图。
    *   **温度：** Low (0.1).
    *   **目标：** 语义准确，逻辑严密，无视文采。
*   **Step 2: 润色 (Polishing)**
    *   **输入：** 直译文 + 风格 Prompt + 风格参考 (Few-shot)。
    *   **温度：** High (0.7).
    *   **目标：** 文学性重构，严禁修改术语和逻辑。

### 3.4 数据与状态管理
*   **Context Manager:** 维护滑动窗口摘要 (Summary Buffer)。
*   **Glossary Manager:** 加载并根据上下文筛选相关术语规则。

## 4. 核心数据结构

### 4.1 术语库 (Glossary Item)
```json
{
  "src": "Autarch",
  "default_tgt": "独裁官",
  "rules": [
    {
      "condition": "respectful_address",
      "tgt": "至尊",
      "example": "Yes, Autarch -> 是的，至尊"
    }
  ]
}
```

### 4.2 逻辑映射 (Logic Map)
```json
{
  "ambiguities": [
    {
      "segment": "I mean while...",
      "modifies": "I thought of that",
      "note": "Timing clarification"
    }
  ]
}
```

## 5. 交互流程 (CLI)
1.  `python main.py init <book_path>` -> 生成项目结构和配置文件。
2.  `python main.py extract-terms` -> 扫描并生成初版术语表。
3.  `python main.py translate --chapter 1` -> 启动翻译流水线。
    *   显示进度条。
    *   遇到高风险歧义时（可选）暂停询问用户。
4.  `python main.py review` -> 启动 TUI (Terminal UI) 逐段审阅。

## 6. 里程碑
*   **P0:** 跑通 "Foila" 片段的逻辑分析+翻译流程。
*   **P1:** 完成全书拆解与基础翻译引擎。
*   **P2:** 实现风格化润色与术语库动态注入。
