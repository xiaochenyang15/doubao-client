# 豆包桌面客户端

基于 wxPython 的豆包桌面客户端，通过 Chrome DevTools Protocol 桥接豆包 Web
接口，提供会话管理、流式对话、语音朗读、本机代码执行与 AI Agent 自动化能力。
界面针对 Windows 屏幕阅读器（NVDA / JAWS / 讲述人）与高对比度模式设计。

当前版本：2.0.0

> **免责声明**
>
> 本项目为非官方第三方客户端，与字节跳动（ByteDance）及其关联公司无任何关联，
> 未获其授权或背书。本项目仅供个人学习、技术研究与无障碍辅助使用，禁止商业使用。
> 通过浏览器自动化访问豆包 Web 接口可能与豆包服务条款存在冲突，并可能触发限流、
> 功能受限或账号封禁；是否使用及使用后果由使用者自行承担。完整条款见
> [DISCLAIMER.md](DISCLAIMER.md)。

## 功能概览

| 模块 | 说明 |
| --- | --- |
| 会话 | 会话列表、打开、重命名、置顶、标记已读、清除上下文、批量删除 |
| 对话 | 流式回复、Markdown 渲染、图片粘贴随消息发送、点赞 / 点踩反馈 |
| 朗读 | 使用豆包语音合成按句朗读，BASS 播放，可随时中断 |
| 代码执行 | 解析回复中的 Python 代码块，确认后在本机解释器中运行，缺包自动安装 |
| AI Agent | 截图 → 视觉模型决策 → 键鼠执行的循环，整个任务在同一服务端会话内完成 |
| 包管理 | 查看、安装、升级、卸载随包解释器中的第三方库（设置 → 包管理） |
| 无障碍 | 控件命名、MSAA 事件播报、全键盘操作、高对比度配色适配 |

## 系统要求

- Windows 10 1803 或更高版本（64 位）
- 发布包（`doubao_client.exe`）无需安装 Python；源码运行需要 Python 3.10+

## 快速开始

### 使用发布包

直接运行 `doubao_client.exe`。首次使用「代码执行」或「AI Agent」时，程序会将
便携 Python 与便携 Chrome 下载到 exe 同级目录的 `bin/` 下。

### 获取源码

```bat
git clone https://github.com/xiaochenyang15/doubao-client.git
```

无法直连 GitHub 时，可在原地址前拼接加速代理前缀。这类前缀仅代理下载，不支持推送：

```bat
git clone https://ghfast.top/https://github.com/xiaochenyang15/doubao-client.git
git clone https://gh-proxy.com/https://github.com/xiaochenyang15/doubao-client.git
```

代理服务由第三方提供，可用性随时间变化；直连或自建代理更可靠。

### 从源码运行

```bat
pip install -r requirements.txt
python setup_chrome.py    :: 下载便携 Chrome（约 170 MB），仅在需要桥接时使用
python setup_python.py    :: 下载执行代码用的便携 Python（约 20 MB）
python -m doubao_client
```

`run.bat` 等价于上述流程的封装。

浏览器按以下优先级查找：`bin/chrome-win64/chrome.exe` → 本机 Chrome → 本机 Edge。

## 项目结构

代码采用包组织，按职责分层；服务对象均以类实例提供，避免模块级全局状态。

```
doubao_client/
├── __init__.py          包元信息
├── __main__.py          python -m doubao_client 入口
├── app.py               wx.App、DPI 感知、单实例互斥锁、主循环
├── core/
│   ├── config.py        应用目录解析与 AppConfig（config.json 读写）
│   ├── singleinstance.py Windows 命名互斥体 / POSIX 文件锁
│   ├── runner.py        Interpreter（解释器定位与环境画像）、CodeRunner（执行）
│   ├── packages.py      PackageManager：pip list / install / upgrade / uninstall
│   └── md_render.py     Markdown 纯文本渲染
├── services/
│   ├── api.py           DoubaoAPI：CDP 桥接、会话、流式对话、错误分类
│   ├── imagex.py        ImageX 图片直传（prepare / apply / TOS / commit）
│   ├── cookies.py       浏览器登录态提取与持久化
│   ├── tts.py           豆包语音合成与播放队列
│   ├── bassplayer.py    BASS 播放器封装
│   └── a11y.py          无障碍事件与配色辅助
├── ui/
│   ├── theme.py         配色常量与字体
│   ├── main_window.py   主窗口：会话列表、消息区、输入区、菜单
│   ├── chat.py          消息视图与右键菜单
│   ├── dialogs.py       登录与账号对话框
│   ├── agent_dialog.py  AI Agent 控制台
│   ├── packages_dialog.py 包管理窗口
│   └── runner_dialogs.py  代码执行确认、进度与结果对话框
├── agent/               截图（screen）、界面元素识别（hittest / observe）、
│                        决策解析（decision / prompt）、动作执行（actions）、
│                        循环与错误重试（session）、音效（sounds）
└── sounds/              Agent 状态音效
```

分层约定：

- `core` 不导入 `ui` 与 `services`，可脱离界面单独测试；
- `services` 不导入 `ui`，仅依赖 `core` 与第三方库；
- `ui` 仅通过公开接口调用下层，界面更新统一经 `wx.CallAfter` 回到主线程。

## AI Agent：单会话执行模型

一次任务从开始到结束始终复用同一个服务端会话：

1. 首条决策请求使用会话 id `"0"`，服务端创建会话并在响应中返回新 id；
2. 后续每一步复用该 id，模型因此可见本任务的完整上下文；
3. 任务结束（完成、停止或失败）时删除该临时会话，不占用用户会话列表。

截图上传结果按（路径、修改时间、文件大小）缓存，重试同一张截图时不会重复
上传，减少请求量。

图片上传优先使用 ImageX 直传（`services/imagex.py`）：按网页自身的上传协议依次
调用 `prepare_upload`、`ApplyImageUpload`、TOS 上传、`CommitImageUpload`，全程只
发送 HTTP 请求、不操作页面 DOM，因此不会在输入区生成附件卡片。网页通道的附件
卡片无法从页面外部删除，数量达到 10 张后网页会静默忽略新的文件选择，导致连续
带图对话在第 11 轮上传失败；直传从根本上规避该限制。直传不可用时自动回退网页
通道。

页面通道自愈（`api._recover_page`，网页上传与会话请求共用）：页面运行一段时间
后本地通道可能失效（上传拦截被页面重置、页面被刷新、脚本上下文丢失），表现为
收不到上传结果或流式数据。自愈路径为重新安装拦截钩子或刷新页面重建通道，随后
继续请求。该现象与请求频率无关，因此不设置人为节流或冷却等待。会话请求在
「未发出」时自愈后重发；已发出的不自动重发，避免产生重复消息。

异常处理：

- 瞬时错误（`transient`）立即重试，不设置本地退避或冷却等待；
- 服务端限流（`rate_limit`，错误码 710022004 / 710022002）按服务端语义等待后再试：
  限流期间任何请求都会被拒，立刻重发只会延长恢复时间。等待序列 `RATE_LIMIT_WAITS`
  依次为 30 / 60 / 120 / 240 / 300 / 600 / 900 秒（合计约 37 分钟），全程可被用户
  停止；等满仍被拒则如实停下并给出恢复指引（见「限流与恢复」）；
- 服务端「这一轮生成失败」（流式协议 2005，常见文案「系统错误」，且不属于限流）
  由 `AgentDecider.decide` 换新会话重发一次：提示词自带目标、动作历史与当前
  屏幕状态，换会话不丢上下文；
- 登录失效（`auth`）终止任务并提示重新登录；
- 页面通道故障在 api 层自愈（见上），业务层不再做换会话之类的二次自愈。

同一时间只允许一条会话请求在飞：`start_stream` 会中止上一条未结束的流，
`_stream_once` 在异常退出时也会主动中止。否则残留的生成会让同会话的下一条
请求被服务端以「已有对话流正在输出」拒绝。

早期版本按固定步数轮换会话，导致模型在轮换边界丢失上下文；该机制已移除。

## 限流与恢复

`710022004`（`message` 为 `rate limited`，`error_detail.ext.decision` 中
`from=shark_admin`、`verify_scene=doubao_message_web`）与 `710022002`
（「当前服务访问频繁，请稍后重试」）属于同一类：账号在「网页发消息」场景下的
风控限流。外层只返回「系统错误」，因此必须记下服务端原始报文才能定位。

排查结论：

- 签名与页面一致。客户端请求经页面自身的 `window.fetch` 拦截器签发，
  `msToken` 与 `a_bogus`（约 180 字符）均为完整签名，不存在缺签名问题；
- 与图片无关。160×120 小图连续 15 轮、1920 宽整屏截图连续 14 轮均可通过；
- 与对话轮数无关。被限流时，全新会话、3 个字的消息同样在 0.3 秒内被拒；
- 触发因素是单位时间内的消息条数。Agent 每步一条决策请求，节奏明显高于人工
  使用网页，累计风险分达到阈值后按账号拦截。

恢复办法（按见效速度排序）：

1. 更换网络出口：优先使用手机热点，其次切换宽带；出口 IP 变化后通常立即恢复；
2. 手动过人机验证：从「账号 → 登录 / 切换账号」打开浏览器窗口，在豆包页面
   手动发一条消息，出现滑块验证即完成，验证通过后标记解除；
3. 等待：公开资料给出的窗口为 30 分钟到 2 小时。限流期间不要反复重试，
   重试会延长恢复时间。

参考文献：火山引擎《突发流量处理最佳实践》（客户端整流、平滑流量曲线）、
`doubao-web-mcp` 的 710022004 处置说明（等待冷却 / 换网络 / 手动过验证）、
`doubao2api` 关于浏览器内 `fetch` 架构与原生签名注入的说明。

## 单实例约束

程序启动时先获取互斥锁再创建窗口，防止同一用户重复启动：

- Windows：`CreateMutexW` 创建命名互斥体，`ERROR_ALREADY_EXISTS` 判定已有实例；
- POSIX（开发环境）：用户目录下的文件锁，`fcntl.flock` 非阻塞加锁。

已存在实例时弹出提示并正常退出，不强制激活原窗口。互斥体随进程存活，
并通过 `atexit` 显式释放。

## 包管理

「设置 → 包管理」提供对随包 Python 环境（即代码执行与 Agent `run_code` 所用
解释器）的第三方库管理：

- 列出已安装包及版本，支持名称筛选；
- 安装（输入框支持空格或逗号分隔的多个包名，回车提交）；
- 升级、卸载选中项，卸载前二次确认；
- 后台线程执行 `pip` 子进程，实时输出到只读日志区，操作期间显示进度并
  禁止重复提交；关闭窗口时若操作未完成会拦截关闭；
- 操作成功后刷新环境画像缓存，包列表自动重载。

安装源为「设置 → pip 镜像源」中配置的地址，为空时使用官方 PyPI；安装失败
时自动回落官方源重试。

## 菜单与主界面

菜单仅包含主界面按钮与右键菜单未覆盖的功能，避免同一操作出现两处入口：

- 账号：登录 / 切换账号（Ctrl+L）、刷新登录状态（Ctrl+R）、账号信息（Ctrl+I）、退出
- 对话：AI Agent（Ctrl+Shift+A）、朗读最后一条回复（Ctrl+Shift+V）、停止朗读、
  刷新会话列表（F5）
- 设置：包管理、pip 镜像源
- 帮助：键盘快捷键（Shift+F1）、关于（F1）

新建对话、停止生成、复制消息、运行代码、会话重命名与删除等操作由主界面
按钮、右键菜单或列表快捷键提供。对应快捷键统一在 `EVT_CHAR_HOOK` 中处理，
保证键盘可达性：Ctrl+N 新建对话、Ctrl+Shift+C 复制最后一条消息、
Ctrl+Shift+R 运行回复中的代码、Ctrl+F2 重命名当前会话。

## 配置

`config.json` 与可执行文件（或仓库根目录）同级，首次运行自动生成：

| 键 | 说明 |
| --- | --- |
| `device_id` / `web_id` | 设备与站点标识，随发行包分发默认值 |
| `pip_mirror` | 自动安装第三方包使用的镜像源，空字符串表示官方 PyPI |
| `tts_speaker` | 朗读音色标识 |
| `speak_switches` | 自动朗读开关（用户消息 / 回复） |

## 构建

```bat
python build.py           :: PyInstaller 生成 exe，并组装 dist
python build.py --zip     :: 额外生成 zip
python build.py --no-exe  :: 跳过 PyInstaller，仅重新组装 dist
python build.py --no-pkgs :: 不向随包解释器预装常用库
```

产物：

```
dist/
  doubao_client.exe       主程序（音效资源已打入 exe）
  bin/python/             便携 Python（Agent / 代码执行使用）
  bin/chrome-win64/       便携 Chrome（CDP 桥使用）
  bin/bass/x64/           BASS 放音组件
  config.json             设备标识
```

`build.py` 依赖 `bin/python`、`bin/chrome-win64`、`bin/bass/x64` 三个目录，
缺失时先执行 `setup_python.py` 与 `setup_chrome.py`。`bin/bass` 随源码包提供。

发行包不包含运行期私有数据：`cookies.dat`、`_chrome_profile/`、`_work/`、
`bridge.log`、`agent_anchors.json` 等均被排除。

仅打包单个可执行文件时，可直接使用 PyInstaller：

```bat
python -m PyInstaller --noconfirm --clean doubao_client.spec
```

## 测试

测试脚本依赖 `requirements.txt` 里的完整依赖（wxPython、websocket-client
等），请用装好依赖的解释器运行，例如 `python test_failures.py`；随包的
`bin\python` 是给用户代码执行用的精简环境，不能跑这些脚本。

| 脚本 | 覆盖范围 |
| --- | --- |
| `test_install.py` | 缺包检测、批量安装、超时下限、镜像优先级 |
| `test_failures.py` | 错误分类、限流等待、生成失败换会话、单会话执行、截图上传缓存、页面通道自愈 |
| `test_api.py` | 登录态、会话列表、流式对话联调（需要有效登录态） |
| `e2e_agent.py` | Agent 端到端操作验证（需要可见桌面） |

## 数据与隐私

登录态来自本机浏览器的 Cookies，使用 DPAPI 解密，仅保存在本地
`cookies.dat`，不上传。设置中的包管理、代码执行均在本机完成；对话内容按
豆包 Web 接口规则发送至服务端。

## 第三方组件

- wxPython：界面框架（wxWindows Library Licence）
- BASS / BASS_AAC：音频播放（un4seen 免费许可，仅限非商业用途）
- pycryptodome、pywin32、websocket-client

部分实现参照或衍生自其他开源项目：NVDA 插件 Golden Cursor 与 Vis Aware
（GPL v2）、MyCodex（MIT）、NVDA 布局（GPL v2），图片直传协议参照
doubao2api-oss（Apache-2.0）与 Doubao-AI（MIT）的公开说明并独立实现。
版权归属、许可全文与兼容性说明见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。

## 许可与免责

本项目以 GNU GPL v2 发布，全文见 [LICENSE](LICENSE)。使用前请阅读
[DISCLAIMER.md](DISCLAIMER.md)：本项目为非官方第三方客户端，仅供个人学习、
研究与无障碍辅助使用，使用风险由使用者自行承担。
