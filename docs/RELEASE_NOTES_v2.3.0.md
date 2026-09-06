# Token Ledger v2.3.0 发行说明

### 🌟 核心更新亮点

#### 1. ⚡ Windows 原生客户端最大化卡死彻底修复
- 解除 DirectComposition 同步锁，解决此前点击最大化窗口黑屏或无响应卡死问题。
- 引入 DPI 穿透（PassThrough）策略与 QWidget 几何解耦容器，使原生窗口全屏/还原流畅丝滑。

#### 2. 📊 Google Antigravity 官方语言服务实时额度直连
- 接入 Antigravity 官方 Language Server RPC 服务（`LanguageServerService/RetrieveUserQuotaSummary`）。
- 实时展示 Gemini 每周限额（如 83.8%）与 5 小时限额双长条可视化进度条。
- 智能适配 Gemini 3.1 Pro、Gemini 3.7 Flash、Gemini 3.8 Flash 模型明细与思维链上下文统计。

#### 3. 🔄 CC Switch 多服务商余额与实时路由联动
- 实时探测并接入 CC Switch 当前激活路由（如 DeepSeek），展示账户实时余额（如 `60.24 CNY`）及健康可用状态条。
- 在 Claude 详情抽屉中完整呈现 CC Switch 配置的所有 5 个上游渠道（DeepSeek、智谱 GLM、阿里云百炼、Agnes、Claude 官方通道），提供渠道状态说明与控制台直达链接。

#### 4. 🎯 净用量核算与前端视觉优化
- 引入全新“范围实际净用量”计算公式：`(总输入 − 缓存读取) + 模型输出`，精准反映真实计费与消耗基准。
- 采用 ClearType 零字体发虚样式设计，提升高分屏下文字细腻度与发光走势图质感。

---

### 🛠️ 质量与安全
- 已通过全量 24 项自动化单元测试。
- 采用 30s 内存 TTL 缓存与断网/异常平滑本地降级机制，严格保护敏感鉴权信息，保证界面永不假死。
