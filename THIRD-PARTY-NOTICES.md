# 第三方组件声明（THIRD-PARTY NOTICES）

本项目（豆包桌面客户端）自身代码以 **GNU GPL v2** 发布，全文见 [`LICENSE`](LICENSE)。
下列第三方项目、组件与资源不属于本项目原创，版权归各自所有者，按其各自条款使用
与分发。本文件即为对上述协议的遵守与告知。

---

## 一、衍生或取自的开源项目

### Golden Cursor 26.1（NVDA 插件）

| 项 | 内容 |
|---|---|
| 来源 | <https://github.com/nvda-es/goldenCursor>（NVDA 插件 goldenCursor 26.1） |
| 版权 | Copyright (C) 2015-2022 Salah Atair、Wafeeq Taher、Joseph Lee、Abdel（Abdelkrim Bensaid）及 NVDA 西语社区等贡献者 |
| 许可 | GNU GPL v2（其仓库 `COPYING.txt`；源码头标注 "License GNU GPL"） |
| 使用范围 | `agent/anchors.py`（命名位置/锚点的存取与匹配）、`agent/hittest.py`（`objectFromPoint` 落点命中测试、鼠标移动边界钳制）、`agent/actions.py`（`mouse_move` 边界钳制、`save_anchor` 不给坐标即记录当前鼠标位置）中的思路与实现衍生自该插件 |
| 遵循方式 | 衍生部分随本项目整体以 GNU GPL v2 发布；三个文件头部保留原作者版权声明与本声明指引 |

### Vis Aware 1.0.0（NVDA 插件）

| 项 | 内容 |
|---|---|
| 来源 | NVDA 插件 Vis Aware 1.0.0（`visAware`，作者 cary-rowen \<cary-rowen@outlook.com\>，发布于 2026/10/21） |
| 版权 | Copyright (C) 2025 Cary-rowen |
| 许可 | GNU GPL v2（其 readme「许可」一节明确：*本项目使用 GNU General Public License version 2*） |
| 使用范围 | `sounds/` 目录下全部 8 个提示音（`action.wav`、`auto.wav`、`cancel.wav`、`error.wav`、`question.wav`、`start.wav`、`success.wav`、`waiting.wav`）取自该插件的 `sounds/` 目录，逐文件一致，用于 AI Agent 与界面提示音（`agent/sounds.py`） |
| 遵循方式 | 随本项目以 GNU GPL v2 分发，此处保留来源、作者与许可信息 |
| 素材注意 | 部分 wav 文件内嵌上游音效素材自带的元数据：`action.wav` 含 *"Household and hotel sfx" / Omar Alvarado / copyright 2017*；`auto.wav` 含 *© 1995-1998 Microsoft Corporation*。这些元数据随上游插件而来，非本项目添加。**若用于商业发行，请自行核实这些声音素材的授权链。** |

### MyCodex

| 项 | 内容 |
|---|---|
| 来源 | MyCodex（`jintonglu6688/MyCodex`，本地参考 `MyCodex-main`） |
| 版权 | Copyright (c) 2026 jintonglu6688 |
| 许可 | MIT License |
| 使用范围 | 界面布局设计参照该项目：左侧会话列表、右侧消息区 + 底部输入栏 + 右侧按钮组。未复制其源码（其为 C#/.NET 项目，本项目为 Python） |
| 遵循方式 | 保留本声明及 MIT 许可全文（见文末附录 A） |

### doubao2api-oss（接口协议参照，未复制源码）

| 项 | 内容 |
|---|---|
| 来源 | <https://github.com/JiashuaiXu/doubao2api-oss> |
| 版权 | Copyright 2025 wangchuxiaoji-oss |
| 许可 | Apache License 2.0 |
| 使用范围 | `services/imagex.py` 的图片直传流程（`prepare_upload` → `ApplyImageUpload` → TOS 上传 → `CommitImageUpload`）与签名参数命名，与该项目公开说明相互核对；协议本身来自火山引擎 ImageX 官方接口文档 |
| 遵循方式 | 本项目为独立实现：使用 Python 标准库 `urllib`，未采用参考项目的 HTTP 客户端与模块划分，未复制其源码，因此不构成 Apache-2.0 作品的衍生。此处履行署名与来源告知义务。**Apache-2.0 与 GPL-2.0 不兼容，本项目不包含任何 Apache-2.0 许可的代码**；若后续确需引入其代码，将改为 GPL-3.0-or-later 或另行取得授权后再合并 |

### Doubao-AI（接口协议参照，未复制源码）

| 项 | 内容 |
|---|---|
| 来源 | <https://github.com/Pafonshaw/Doubao-AI> |
| 版权 | Copyright (c) 2025 Pafonshaw |
| 许可 | MIT License |
| 使用范围 | 同上：ImageX 四步直传流程与 `VolcanoSigner` 签名思路的对照核实；未复制源码 |
| 遵循方式 | 保留本声明及 MIT 许可全文（见文末附录 C） |

### NVDA（布局参照）

| 项 | 内容 |
|---|---|
| 来源 | NVDA 屏幕阅读器（<https://www.nvaccess.org/>），Copyright NV Access Limited |
| 许可 | GNU GPL v2 |
| 使用范围 | 「设置 pip 镜像源」对话框的布局（地址编辑框 + 测试按钮 + 确认/取消）参照 NVDA 的更新镜像设置窗；仅参照布局思路，未复制其源码 |
| 遵循方式 | 此处注明参照来源；本项目整体已以 GPL v2 发布 |

---

## 二、随包分发的二进制组件（非开源，独立许可）

### BASS 2.4 / BASS_AAC（音频放音组件）

| 项 | 内容 |
|---|---|
| 位置 | `bin/bass/x64/bass.dll`、`bin/bass/x64/bass_aac.dll`（各目录说明见 `bin/bass/README.txt`） |
| 来源 | Un4seen Developments，<https://www.un4seen.com/>（`bass24.zip` / `bass_aac24.zip` 的 `x64` 目录） |
| 版权 | Copyright (c) 1999-2025 Un4seen Developments Ltd. All rights reserved. |
| 许可 | BASS 专有免费许可（**非**开源）：**非商业用途免费**；用于商业产品需在 un4seen 购买授权；不得转售或再授权 |
| 用途 | 豆包语音合成返回的 AAC 流放音（`bassplayer.py` / `tts.py`） |
| 遵循方式 | 原样分发、不修改；在此声明其独立许可与商用限制。本项目与 Un4seen Developments 无隶属或背书关系 |

BASS 是独立的专有组件（非本 GPL 项目的衍生作品，也不随本项目以 GPL 授权），
只是与本程序一同分发（mere aggregation），运行时经 ctypes 动态加载。它继续
适用 un4seen 自己的条款；本项目的 GPL v2 授权不适用于 `bin/bass/` 下的文件。

BASS 许可要点（原文，摘自 `bass24.zip` 内 `bass.txt`）：

> BASS is free for non-commercial use. If you are a non-commercial entity
> (eg. an individual) and you are not making any money from your product
> (through sales/advertising/etc), then you can use BASS in it for free.
> If you wish to use BASS in commercial products, then please also see the
> next section.
>
> TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW, BASS IS PROVIDED
> "AS IS", WITHOUT WARRANTY OF ANY KIND, EITHER EXPRESSED OR IMPLIED,
> INCLUDING BUT NOT LIMITED TO THE IMPLIED WARRANTIES OF MERCHANTABILITY
> AND/OR FITNESS FOR A PARTICULAR PURPOSE. THE AUTHORS SHALL NOT BE HELD
> LIABLE FOR ANY DAMAGE THAT MAY RESULT FROM THE USE OF BASS. YOU USE
> BASS ENTIRELY AT YOUR OWN RISK.
>
> Usage of BASS indicates that you agree to the above conditions.
>
> Commercial licensing: SHAREWARE / SINGLE COMMERCIAL / UNLIMITED COMMERCIAL，
> 详见 <https://www.un4seen.com/>。These licences do not allow
> reselling/sublicensing of BASS.

---

## 三、构建或运行时获取的组件（不随本源码包分发）

| 组件 | 获取方式 | 版权与许可 |
|---|---|---|
| Google Chrome（Chrome for Testing） | `setup_chrome.py` 从 Google 官方（或 npmmirror 同源镜像）下载 | Copyright Google LLC. All rights reserved. 按 Google Chrome 服务条款（专有软件）使用 |
| CPython 3.13.15（python-build-standalone） | `setup_python.py` 从 astral-sh 发布（或 npmmirror 同源镜像）下载 | Python 本体：Python Software Foundation License；python-build-standalone 构建：Apache-2.0 |

---

## 四、Python 依赖（pip 安装；运行依赖见 `requirements.txt`，预装见 `build.py`）

| 包 | 许可 |
|---|---|
| wxPython | wxWindows Library License（LGPL-3.0 附例外条款） |
| pycryptodome | BSD-2-Clause（部分为公共领域） |
| pywin32 | Python Software Foundation License |
| websocket-client | Apache-2.0 |
| requests | Apache-2.0 |
| beautifulsoup4 | MIT |
| openpyxl | MIT |
| python-docx | MIT |
| Pillow | MIT-CMU |
| python-dateutil | Apache-2.0 / BSD-3-Clause 双许可 |
| six | MIT |
| tzdata | Apache-2.0 |
| pip | MIT |

---

## 附录 A：MyCodex 的 MIT 许可全文

```
MIT License

Copyright (c) 2026 jintonglu6688

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 附录 B：GNU GPL v2

本项目与 Golden Cursor、Vis Aware、NVDA 相关部分均以 GNU GPL v2 授权，
许可全文见项目根目录 [`LICENSE`](LICENSE)（即 GPL-2.0 全文）。
如未随本项目收到该文件，可从 <https://www.gnu.org/licenses/old-licenses/gpl-2.0.html> 获取。

## 附录 C：Doubao-AI 的 MIT 许可全文

```
MIT License

Copyright (c) 2025 Pafonshaw

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 附录 D：与 Apache-2.0 的兼容性说明

本项目以 GNU GPL v2 发布（见 [`LICENSE`](LICENSE)）。GPL v2 与 Apache-2.0
不兼容，不能把 Apache-2.0 许可的代码并入以 GPL v2 发布的整体作品。

因此 `services/imagex.py` 的图片直传流程依据火山引擎公开的 ImageX 接口文档与
公开协议描述独立实现，未引入 doubao2api-oss 的源代码。若今后需要复用任何
Apache-2.0 代码，应先将本项目升级为 GPL-3.0-or-later（GPL v3 与 Apache-2.0
兼容），或另行取得该部分代码的许可豁免。
