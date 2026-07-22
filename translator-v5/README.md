# FolioLoom V5

`translator-v5/` 是 FolioLoom 的 TypeScript 翻译内核，也是本地桌面 Alpha 的运行目录。它保留命令行工作流，并提供一个只读的 Electron 文稿工作台，用于查看已初始化项目的状态和运行无模型检查。

## 安装

要求：Node.js 24+ 与 Windows。

```powershell
Set-Location translator-v5
npm.cmd install
```

完整的原文导入、模型配置和 `book` 命令说明见仓库根目录的 [README](../README.md)。

## 本地桌面 Alpha

桌面 Alpha 是开发预览，不是已经发布的安装包。它只能打开由既有工作流初始化的项目：选择 `source_manifest.json`，并可选地选择对应的 V5 `book.db`。

```powershell
Set-Location translator-v5
npm.cmd install
npm.cmd run desktop:dev
```

它会展示原文长度、源语言、运行窗口状态，并可执行只读的 `doctor` 检查来报告覆盖、结构、异常和术语情况。没有状态库时，也可以只查看 manifest 对应的原文状态。

### Alpha 边界

- 不会打包、读取或要求 API Key；桌面检查不构造模型 provider。
- 不会导入 TXT、EPUB、DOCX 或 Markdown；请先使用根目录工作流建立项目。
- 不会运行翻译、编辑术语或写入所选的 `book.db`、原文和知识库。
- 应用只会在自身 userData 目录保存最近一次已选择项目的偏好；项目数据始终由用户显式选择。
- `desktop:dist` 仅为未来 Windows x64 portable 构建预留元数据。Alpha 验证不运行它，也不会生成或发布 `.exe`、`.zip` 或安装包。

## 开发验证

```powershell
npm.cmd run desktop:test
npm.cmd run desktop:typecheck
npm.cmd run typecheck
npm.cmd run desktop:build
npm.cmd test
```

`desktop:build` 只构建本地 Electron 资源，并断言主进程实际使用的 preload 与 renderer 入口存在。它不会调用模型或创建翻译输出。
