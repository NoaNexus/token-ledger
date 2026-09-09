# toekn用量统计

Windows 本地优先的 AI Agent 用量查看器，使用 PyQt5 / Qt WebEngine 展示本地索引中的 Codex、Claude Code 和 Antigravity 用量。当前版本：2.4.5。

## 启动

下载 [Windows 便携版](https://github.com/NoaNexus/token-ledger/releases/latest)，完整解压后运行 TokenLedger.exe。不要只复制 exe；需保留旁边的 _internal 目录。

源码运行：

```powershell
python -m pip install -r requirements-gui.txt
python native_app.py
```

浏览器模式：`python -m tokenledger`。只扫描：`python -m tokenledger --scan-only`。
本地服务仅用于环回访问；指定端口被占用时改用系统分配端口，不终止占用进程、不信任未知服务。桌面运行期间约每 60 秒触发增量扫描；扫描任务不会重叠。

数据默认保存在 `%LOCALAPPDATA%\TokenLedger\token-ledger.db`。可用 `TOKEN_LEDGER_DATA_DIR`、`TOKEN_LEDGER_USER_HOME` 或 CLI 参数指定数据与来源目录。

## 数据与统计口径

- Codex 使用累计计数的差值区分新调用与重复快照，不直接累加累计值；分叉会话中能与父会话累计状态核对的继承前缀不重复计数。缺少累计字段时回退到单次用量。
- 对具有稳定累计计数标识的 Codex 独立用量保留历史：日志缩短、移动或暂时缺失不直接抹除已确认事件。旧版索引首次按新口径重建时，继承重复量会减少，不能将旧高值直接当作真实消耗恢复。
- 左上首卡“全周期累计 Token”展示已索引的全部历史总用量；趋势图表与明细数据跟随所选时间范围（7天/30天/全部）动态切换。智能体卡片的“累计”角标单独保留历史口径。
- Codex 日志可能在后续被截短；扫描会保留已观测且没有父会话证据的历史事件，只排除能由父会话证实的分叉继承副本。
- Claude Input 包含普通输入、缓存读取与缓存写入。CC Switch 日汇总和代理日志按可识别覆盖范围核对；不同平台或模型不可互相抵消。
- Antigravity 尽可能读取结构化用量；没有服务端 Token 字段时，可见文本只能标为估算，不能代表完整模型上下文。
- 净用量为 `max(Input - Cached input, 0) + Output`，只是统计指标，不是实际计费 Token。缓存读取和写入也可能收费。
- 同日同平台、同模型、同路由的会话和账户日汇总取较大覆盖；身份未知的记录保留并提示可能重叠。这不是精确账单对账。
- 额度仅展示有依据的服务端快照；获取失败显示未提供或过期缓存。超过重置时间不会自行变成 100%。
- 金额是按当前已核验基础单价计算的 API 参考估算，不是订阅费用、历史账单或实付金额。未知模型不猜价；混合数据标为“部分估算”。人民币换算固定按 1 USD = 7.20 CNY，并非实时汇率。

单价来源、核验日期及限制见 `tokenledger/pricing.py`。不覆盖所有模型、历史调价、长上下文、服务档位或缓存时长。

## 隐私与网络边界

日志解析可能在内存中读取正文以提取统计或进行文本估算；不把提示词、回复、思考正文或会话标题保存到账本。索引仍包含时间、模型、路由、会话标识、来源路径提示等个人信息，不应公开。

CC Switch 集成会读取本地 provider 配置；余额查询仅对已确认的 DeepSeek 官方地址使用对应凭据。Antigravity 可访问本机 Language Server 获取服务端数据。因此应用不是严格离线工具。应用没有统计遥测上传逻辑，也不把密钥存入账本。

2.4.3 首次打开数据库会清理旧版 Claude / Antigravity 衍生元数据和额度缓存，保留用量事件，随后重新扫描。该逻辑清理不等于对旧备份、磁盘页或 Git 历史进行安全擦除。公开文档中的个人样例已移除；旧提交及旧发布副本不因本次更新自动消失。

## 开发与验证

```powershell
python -m pytest -q
python -m PyInstaller --noconfirm --clean TokenLedgerNative.spec
```

[发布说明](docs/RELEASE_NOTES_v2.4.5.md) · [开发交接](docs/CODEX_HANDOVER.md) · [对账口径](docs/TOKEN_RECONCILIATION_UPGRADE.md)
