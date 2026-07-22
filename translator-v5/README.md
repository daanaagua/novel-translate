# FolioLoom V5

`translator-v5/` 是 FolioLoom 的 TypeScript 翻译内核，也是本地 Electron 工作台的运行目录。它保留完整命令行工作流，并提供书稿导入、模型连接检查与单片段试译入口。

## 安装

要求：Node.js 24+ 与 Windows。

```powershell
Set-Location translator-v5
npm.cmd install
```

完整的原文导入、模型配置和 `book` 命令说明见仓库根目录的 [README](../README.md)。

## 本地桌面工作台

桌面端仍是开发预览，不是已经发布的安装包。普通用户不需要理解内部清单或数据库，按三步操作即可：

1. 选择 TXT、EPUB、DOCX 或 Markdown 书稿；
2. 选择 DeepSeek、Kimi、阿里云百炼、火山方舟、OpenAI、硅基流动或自定义兼容服务，填写 API Key、模型与 provider 原始 effort 值；
3. 完成真实兼容性检查后，试译一个短片段。

```powershell
Set-Location translator-v5
npm.cmd install
npm.cmd run desktop:dev
```

连接检查会验证真实流式响应、工具调用和多轮连续性；只有 `ready` 状态可以开始试译。`limited` 和 `failed` 会显示可执行原因，不会被包装成成功。试译只运行一个串行窗口，不会暗中启动全书翻译。

### 当前边界与密钥策略

- API Key 只进入 Electron 主进程；不会返回渲染器、写入偏好、项目、测试快照或安装包。
- Windows `safeStorage` 可用时仅保存加密密文；不可用时使用明确的会话模式，关闭应用即失效。
- 预设厂商地址由程序固定，普通界面不要求填写协议或 Base URL；只有自定义兼容入口允许填写经过校验的 HTTPS 地址或本机 loopback HTTP 地址。
- 书稿导入复制原始字节并建立可校验项目，绝不覆盖用户选择的原文件。
- 试译写入项目内 `artifacts/folioloom/book.db`，支持读取上次已提交结果；取消或退出不会中断已经进入的原子提交。
- 全书开始、暂停、恢复、批量审阅、术语编辑和导出尚未接入桌面入口，继续使用 CLI。
- `desktop:dist` 可生成本地 Windows x64 portable 构建；仓库目前仍不发布现成安装包。

## 开发验证

```powershell
npm.cmd run desktop:test
npm.cmd run desktop:typecheck
npm.cmd run typecheck
npm.cmd run desktop:build
npm.cmd test
```

`desktop:build` 只构建本地 Electron 资源，并断言主进程实际使用的 preload 与 renderer 入口存在。它不会调用模型或创建翻译输出。
