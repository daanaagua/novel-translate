# DeepNovel-Translator

面向长篇、高难度小说的两层AI翻译工具。当前版本支持TXT、Markdown、DOCX和EPUB，默认使用 `deepseek-v4-flash`。

## 当前流程

1. 第一层对照原文生成忠实初译，同时输出必要的难点说明、术语建议、实体关系和更新后的全书滚动摘要。
2. 第二层重新对照原文、初译、术语与上下文，修正误译和中文表达，只输出最终译文。
3. 每块完成后立即保存；再次运行会跳过已经完成的块。

每次翻译只有两次模型调用。逻辑分析和滚动摘要包含在第一层中，不另行增加第三次摘要请求。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

复制并修改配置：

```powershell
Copy-Item config\config.example.yaml config\config.yaml
```

本机配置使用 `opencode://deepseek` 时，程序会在运行时读取 `~/.config/opencode/opencode.json` 中DeepSeek Provider的 `apiKey`，不会把密钥复制进项目或日志。其他机器应使用 `${DEEPSEEK_API_KEY}` 环境变量。

## 使用

初始化EPUB：

```powershell
.\.venv\Scripts\python.exe main.py init incandescence "D:\path\Incandescence.epub"
```

先试译一个块：

```powershell
.\.venv\Scripts\python.exe main.py translate incandescence --chapters v01_ch01 --max-chunks 1 --glossary-mode manual --quiet
```

继续翻译时直接重复命令；已经完成的块会自动跳过。去掉 `--max-chunks` 即处理所选章节的全部剩余块，去掉 `--chapters` 即处理全书。

全书完成后导出TXT和EPUB：

```powershell
.\.venv\Scripts\python.exe main.py export incandescence
```

默认会拦截不完整项目；只有显式添加 `--allow-incomplete` 才会生成试读版。

## 项目数据

每本书位于 `projects/<book_id>/`：

- `source.txt`：从源文件抽取、清洗后的原文。
- `artifacts/chapters/*.json`：分块原文、初译、定稿、难点说明及每块记忆快照。
- `artifacts/long_term_memory.json`：当前全书摘要和最近两个块的衔接文本。
- `glossary/auto_generated.json`：模型提出的术语。
- `entities/`：实体档案及关系。

`manual` 模式下新术语标记为待审核，但会作为低权重一致性建议提供给后文；`auto` 模式则直接把新术语作为硬约束。

## 当前默认参数

- 分块上限：约1100个英文词量级。
- 物理重叠：0句；衔接由记忆上下文完成。
- 全书滚动摘要：最多1200个汉字。
- 最近上下文：2个块，每块最多600个原文字符和1000个译文字符。
- 第一层温度：0.1；第二层温度：0.2。
- 两层模型：`deepseek-v4-flash`，Thinking开启，`reasoning_effort=high`。

