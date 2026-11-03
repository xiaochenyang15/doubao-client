"""豆包桌面客户端主包。

子包结构：
    core       配置、单实例互斥锁、代码执行与包管理、Markdown 渲染
    services   豆包 API、登录态存取、语音合成与播放、无障碍辅助
    ui         主窗口与全部对话框（wxWidgets 实现）
    agent      屏幕观察-决策-执行循环（AI Agent）
"""

APP_NAME = "豆包客户端"
__version__ = "2.0.0"
