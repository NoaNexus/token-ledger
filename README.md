# Token Ledger / Token 账本

<p align="center">
  <img src="assets/token-ledger-icon.png" alt="Token Ledger icon" width="112" />
</p>

Token Ledger 是一个 Windows 本地优先的个人 AI Agent 用量桌面应用。它从现有本地会话日志与本地账户汇总中读取用量，建立增量 SQLite 索引，并在 PySide6/Qt 原生 Windows 窗口中展示 Codex、Claude Code 和 Antigravity 的使用情况。

## 第一版能力

- Codex：模型、Input、Cached input、Cache write、Output、Reasoning、Total、净用量、缓存命中率，以及会话返回的服务端额度窗口。
- Claude Code：模型与完整 token/cache 统计；自动读取 CC Switch 的 Claude 账户日汇总，用于恢复已经不在会话目录中的历史，并保留本机会话明细。
- Antigravity：读取本地 transcript 中可见的用户输入、模型回复和思考文本并做本地 Token 估算；界面始终标记“估算”，不冒充 Google 官方计费值，也不伪造本地没有的模型和缓存字段。
- 时间范围：今天、7 天、30 天、全部历史；总览和数据源页面均支持鼠标滚轮、触控板与滚动条。
- 桌面总览：累计/范围/净用量/缓存命中、Input、Cached input、Cache write、Output、Reasoning、模型路由、额度窗口和核算说明。
- 流畅交互：滚轮采用可中断的像素级平滑滚动；趋势图和 Token 流向图使用绘制缓存，后台扫描状态只在变化时刷新界面。
- 数据维度：Agent、CC Switch 路由、平台、模型。
- 本地索引：按文件修改时间增量更新，支持手动重建。
- 自动同步：运行时每 60 秒执行一次增量扫描，页面每 30 秒刷新已完成的索引。

## 启动

直接双击：

```text
dist\TokenLedger.exe
```

这是无控制台的 Windows 原生应用，不会打开命令行窗口，也不依赖浏览器或本机 Python。`launch-native.vbs` 同样可以无黑框启动；`start.bat` 检测到 exe 后也会优先打开原生版本。

应用数据默认保存在：

```text
%LOCALAPPDATA%\TokenLedger\token-ledger.db
```

首次启动会自动迁移项目 `.data` 下的旧账本，因此不需要重新等待完整历史扫描。

旧浏览器开发版仍可通过以下命令启动：

```powershell
python run.py
```

程序只绑定 `127.0.0.1`，启动后会自动打开浏览器。双击启动时，索引保存在项目目录下的 `.data\token-ledger.db`，以避免 Windows 用户目录权限导致启动失败。

开发或迁移时可以指定位置：

```powershell
python -m tokenledger --user-home "%USERPROFILE%" --data-dir .data --no-browser --port 8765
```

只扫描不启动界面：

```powershell
python -m tokenledger --scan-only
```

## 统计口径

- Codex 只累加每个 `token_count` 事件的 `last_token_usage`，不累加会话内累计的 `total_token_usage`。
- Claude Code 的统一 Input 为 `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`。
- CC Switch `input_token_semantics=2` 的账户日汇总采用相同归一化；同一天同时存在账户汇总与 Claude 会话事件时，账户汇总计入总量，会话事件只保留作诊断，不重复累加。
- CC Switch 日汇总没有会话 ID，因此界面只显示仍可从会话日志确认的“已知会话”；汇总覆盖范围外的缺失日期不视为零用量。
- 非缓存 Input = `Input - Cached input`。
- 净用量 = `非缓存 Input + Output`。
- 缓存命中率 = `Cached input / Input`。
- “官方剩余”只来自 Agent 返回的结构化服务端额度字段；没有可靠来源时显示“未提供”，不从 token 数反推。

## 隐私边界

- 解析会话日志时只在内存中提取统计字段，不把提示词、回复、思考或工具正文写入账本。
- 不持久化 API key、access token、cookie 或 CC Switch 的 provider 配置 blob。
- SQLite 会保存时间、Agent、路由、平台、模型、会话 ID、token 数值、额度快照、请求计数、来源路径提示和源文件指纹；因此本地数据库仍属于个人数据，绝不能提交到公开仓库。
- 不向第三方发送统计或遥测数据。
- CC Switch 数据库只读查询 provider 名称及 `usage_daily_rollups` 的 Token/请求汇总白名单列；不会读取 provider 配置 blob、密钥或认证信息。

## 测试

```powershell
python -m pytest -q
```

## 构建原生 exe

构建使用项目隔离环境，不需要修改现有 Python 包：

```powershell
python -m venv .build-venv
.build-venv\Scripts\python.exe -m pip install PyInstaller PySide6-Essentials==6.8.3
.build-venv\Scripts\python.exe -m PyInstaller --noconfirm --clean TokenLedgerNative.spec
```

打包清单会将 Qt、SQLite 和必要运行库收入 exe，用户不需要安装 Python 或 PySide6。最终输出位于 `dist\TokenLedger.exe`。

如需从 PNG 重新生成多分辨率 Windows 图标：

```powershell
python -m pip install Pillow
python scripts\build_icon.py
```

推送 `v*` 标签后，GitHub Actions 会在干净的 Windows 环境运行测试并生成可下载的 `TokenLedger.exe` 构建产物。

## 公开仓库卫生

项目的 `.gitignore` 默认排除本地数据库、JSONL 日志、环境变量、构建目录、错误日志、截图和用户环境缓存。公开 issue 时也不要上传本机会话日志或包含账户信息的截图，详见 [SECURITY.md](SECURITY.md)。
