# 前端架构

界面为 web/index.html、web/styles.css 与 web/app.js，由本地 API 服务提供，封装在 PyQt5 Qt WebEngine 窗口中。不是 PySide6 Qt Widgets 前端。

保留深浅主题、模型表格、用量图表和额度视图。主题 API 只接受支持的主题值。启动不终止端口占用进程，不复用未知服务。

交互要求：键盘可达、焦点可见、窄窗口可用、尊重 prefers-reduced-motion。涉及服务端或文件来源的文本须转义。旧 Chromium 环境避免不必要的新 JavaScript API。

鼠标点击不显示默认黄色焦点框，键盘操作仍保留焦点提示；通过输入方式标记兼容旧版 Qt Chromium。品牌图标与文字使用 12px 外边距，不依赖旧内核不支持的 flex gap。

金额须保留“未提供”和“部分估算”语义，不得将数值占位零展示成已知免费；不得把净用量标成实付或展示硬编码节省百分比。
