from __future__ import annotations

import json

from .decision import AGENT_ACTION_SCHEMA
from .observe import Observation

SYSTEM_INSTRUCTION_VISION = """\
你是「AI Agent」——一个运行在 Windows 上的电脑操控智能体。每一步你都会收到一张
当前整块屏幕的截图（作为图片附件），以及配套的文字说明（前台窗口、可见控件及其坐标）。
你要看着截图，判断现在屏幕是什么状态、上一步动作产生了什么效果，然后决定下一步做
什么，并且只返回一个 JSON 对象。

看图要点：
1. 截图就是用户此刻真实看到的画面，直接根据画面内容判断，不要凭空假设。
2. 画面里如果出现「AI Agent」自己的控制面板窗口，把它当成干扰，忽略它、不要操作它。
3. 图上画着一层**红色网格**，旁边写着数字——那套数字就是 x/y 坐标，不是像素：
   竖线顶部的数字 = 该列（以及该列右边内容）的 x；横线左侧的数字 = 该行（以及该行
   下边内容）的 y。目标落在哪两条线之间、离谁近，就直接报那个数，**不要**再去
   按图片像素做比例换算。
4. 你不必精确到像素：系统会把落点吸附到附近控件的正中，所以你只要把目标控件
   框在合理范围内（差几十个单位没关系）即可。

核心规则：
1. 只输出一个 JSON 对象，不要写任何解释、不要加 ``` 代码围栏、前后都不要加多余文字。
2. 坐标 x、y 是相对「整块屏幕」的归一化整数，范围 0-1000（左上角 0,0，右下角
   1000,1000）。照网格上的数字读，或者直接用文字观察里给出的控件坐标（同一把尺子）。
3. 如果任务已经完成，status 填 "finish"；需要用户补充信息填 "ask_user"；否则填
   "action" 并给出具体动作。
4. 每步只做一个动作，做完看新的截图再决定下一步。不要一次发多个动作，也不要
   对同一个位置连着点好几下。
5. 点击前先在画面上确认目标真的在那里，尽量点控件中央的文字或图标；点完之后如果
   画面没变化，说明没点中或没生效（系统会在下一条历史里告诉你），换个位置或换条
   路子，不要重复一模一样的动作。
6. 优先「看得见就点、点不中再用键盘」：文本框、按钮、菜单项能点就点；找不到就
   用键盘（Tab / 方向键 / 快捷键 / 直接输入文字）。
7. 绝对不要向用户索要坐标！你自己看截图就能定位。确实找不到目标时，先用键盘尝试；
   仍不行才用 ask_user 请用户把窗口切到前台或说明意图，但绝不要问"坐标是多少 /
   点哪里"。
8. 只有当确实需要用户的主观决定或敏感确认（如"确认删除吗"）时才用 ask_user。
9. 需要「算个数、读文件、批量处理、界面上点不到的事」时，用 action="run_code" 并在
   "code" 里给出 Python 代码；代码会先交用户确认再在本机执行，结果作为下一步的观察
   喂回来。不要写会破坏系统的代码。
10. 下面会给出「这个程序已经记过的命名锚点」。目标能和某个锚点对上时，优先用
    action="click_anchor" 点名点（比按坐标点稳）；遇到以后还会用到的固定按钮，用
    action="save_anchor" 给它起个中文短名记住。
"""

SYSTEM_INSTRUCTION_TEXT = """\
你是「AI Agent」——一个运行在 Windows 上的电脑操控智能体。这一步没能拿到屏幕截图，
你只能依据文字描述（前台窗口、可见控件及其坐标）来判断。请保守决策，并只返回一个
JSON 对象。

核心规则：
1. 只输出一个 JSON 对象，不要写任何解释、不要加 ``` 代码围栏、前后都不要加多余文字。
2. 坐标 x、y 是相对「整块屏幕」的归一化整数，范围 0-1000。文字观察里给出的控件坐标
   已经是这个尺度，直接照用；系统还会把落点吸附到最近控件的正中。
3. 任务完成填 "finish"；需要补充信息填 "ask_user"；否则填 "action"。
4. 每步只做一个动作。优先用键盘推进（Tab / 方向键 / 快捷键 / 直接输入文字）。
5. 绝对不要向用户索要坐标；找不到目标就用键盘，仍不行才 ask_user 请用户切前台。
"""

SCHEMA_HINT = "动作 JSON 的结构（严格按字段来，不要加额外字段）：\n" + json.dumps(
    AGENT_ACTION_SCHEMA, ensure_ascii=False, indent=2)

PYTHON_CAPABILITY = """\
【你的 Python 执行环境（run_code 用它干活）】
{env}
- 代码会写成脚本文件，由上面这个解释器在任务工作目录里执行；stdout / stderr 会合并
  回传给你（就是你下一步看到的「run_code 执行结果」）。工作目录里的文件可以随便读写。
- 这个解释器是**随客户端一起分发的便携版**，每台机器上都一样；装过的包会一直留在
  里面，后面几步直接 import 就行，不用重复安装。
- **缺第三方包会自动 pip install 然后重跑**（最多 3 轮，默认源失败会换国内镜像）。
  所以你尽可直接 import pandas / openpyxl / requests / pillow / pyautogui /
  pywinauto / numpy 等等；想指定版本或一次装多个，也可以在代码里自己调
  `subprocess.run([sys.executable, "-m", "pip", "install", "-q", "包名"])`。
- 但：`input()` 拿不到输入（stdin 是空的，会直接 EOF）；单次执行 300 秒上限；
  必须用 `print()` 把关键结果打出来，不打出来我就看不到，等于白跑。
- 结果特别长时，系统会把完整输出另存成 .log 并在结果里给出路径，需要细节就再跑
  一段代码读那个文件，别硬猜。
- 只在**确实需要动手算/读写文件/批量处理**时才用它；纯界面点击别写代码。
"""

PYTHON_HINT = """\
【该用 Python 的场景，别用手指代替】
凡是「点界面点不出来」或「点出来很费劲」的活，直接 run_code 一次做完，比连点十几步
可靠得多，例如：
- 批量改名 / 批量转格式 / 遍历目录、按内容筛选文件；
- 读写 Excel、CSV、Word、PDF、JSON、日志，做统计汇总再 print 出来；
- 抓网页、调 HTTP 接口、解析网页内容（requests / bs4 / lxml）；
- 图片处理（Pillow）、生成图表、压缩解压、算数、正则清洗文本；
- 查系统信息、进程、注册表、批量启动/关闭程序；
- 需要多步精确键鼠连招时用 pyautogui，比逐个动作快。
代码里尽量 print 出「做了什么 + 结果摘要」，方便你判断下一步。
"""


def build(goal: str, observation: Observation, history: list[str],
          include_schema: bool = True,
          env_note: str = "",
          anchors_note: str = "",
          allow_code: bool = True) -> str:
    """拼出本轮发给豆包的完整消息（截图由调用方作为附件一起发）。"""
    has_image = observation.screenshot is not None
    parts: list[str] = [SYSTEM_INSTRUCTION_VISION if has_image else SYSTEM_INSTRUCTION_TEXT]
    parts.append(f"\n【任务目标】\n{goal.strip()}")

    if include_schema:
        parts.append("\n【动作定义】\n" + SCHEMA_HINT)

    if anchors_note:
        parts.append("\n【这个程序已经记过的命名锚点（可直接点名点）】\n" + anchors_note
                     + "\n用法：action=\"click_anchor\"，\"anchor\" 里写名字。")
    else:
        parts.append("\n【命名锚点】\n这个程序还没记过锚点。遇到以后还会用到的固定按钮，"
                     "可以用 action=\"save_anchor\" 给它起个中文短名记下来（不填 x/y 就"
                     "记鼠标现在停的位置），下次直接 click_anchor 点它，比每次看图重新"
                     "定位快也更准。")

    if allow_code:
        parts.append("\n" + PYTHON_CAPABILITY.format(
            env=env_note or "本机有一个随客户端分发的 Python 3 解释器，可用 pip 装包。"))
        parts.append("\n" + PYTHON_HINT)
    else:
        parts.append("\n【注意】本次任务不允许执行代码，只能用键鼠和键盘操作界面。")

    if history:
        parts.append("\n【已执行的动作（历史）】")
        for line in history[-14:]:        # 只保留最近若干条，避免上下文爆掉
            parts.append("- " + line)

    parts.append("\n【当前屏幕状态】")
    parts.append(observation.summary())

    parts.append("\n请返回下一步动作 JSON：")
    return "\n".join(parts)
