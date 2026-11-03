from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


# --------------------------------------------------------------------------- #
# 动作定义
# --------------------------------------------------------------------------- #
ACTION_NAMES = [
    "none",
    "click_at", "click",
    "double_click_at", "double_click",
    "triple_click_at", "triple_click",
    "right_click_at", "right_click",
    "middle_click_at", "middle_click",
    "hover_at", "move_to", "move",
    "click_anchor", "save_anchor",
    "type_text_at", "type",
    "key_combination", "hotkey", "press_key",
    "key_down", "key_up",
    "scroll_at", "scroll_document", "scroll",
    "drag_and_drop", "drag_to", "drag_by",
    "mouse_down", "mouse_up",
    "wait", "wait_for_change",
    "go_back", "go_forward", "navigate",
    "run_code",
]

ACTION_ALIASES = {
    "click": "click_at",
    "double_click": "double_click_at",
    "triple_click": "triple_click_at",
    "right_click": "right_click_at",
    "middle_click": "middle_click_at",
    "move_to": "hover_at",
    "move": "hover_at",
    "type": "type_text_at",
    "hotkey": "key_combination",
    "press_key": "key_combination",
    "scroll": "scroll_at",
    "drag_to": "drag_and_drop",
    "click_named": "click_anchor",
    "click_position": "click_anchor",
    "save_position": "save_anchor",
    "remember_position": "save_anchor",
}

# 给豆包看的 JSON schema（用中文描述，降低它理解门槛）
AGENT_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["action", "finish", "ask_user"],
            "description": "需要继续操作就填 action；任务已经完成填 finish；需要用户补充信息填 ask_user。",
        },
        "message": {
            "type": "string",
            "description": "一句话说明你要做什么，用中文。",
        },
        "action": {
            "type": "string",
            "enum": ACTION_NAMES,
            "description": "下一个要执行的动作名。status 为 action 时必填，否则填 none。",
        },
        "finished": {
            "type": "boolean",
            "description": "这个动作做完是否就代表任务完成。需要肉眼确认结果时填 false。",
        },
        "step_budget": {
            "type": "integer", "minimum": 1, "maximum": 500,
            "description": "你预估这个任务还需要多少步（可省略）。步数不限、什么时候收工由你"
                           "判断，这个字段只用来给自己定节奏；用完时系统会提醒你一次，你"
                           "可以接着做，也可以就此收尾。",
        },
        "x": {"type": "integer", "minimum": 0, "maximum": 1000,
              "description": "横坐标，相对整屏归一化的 0-1000 整数（左上 0，右下 1000）。"},
        "y": {"type": "integer", "minimum": 0, "maximum": 1000,
              "description": "纵坐标，相对整屏归一化的 0-1000 整数。"},
        "destination_x": {"type": "integer", "minimum": 0, "maximum": 1000,
              "description": "拖拽目标横坐标（drag_and_drop），归一化 0-1000。"},
        "destination_y": {"type": "integer", "minimum": 0, "maximum": 1000,
              "description": "拖拽目标纵坐标（drag_and_drop），归一化 0-1000。"},
        "delta_x": {"type": "integer", "minimum": -5000, "maximum": 5000,
              "description": "拖拽横向偏移（drag_by），单位屏幕像素。"},
        "delta_y": {"type": "integer", "minimum": -5000, "maximum": 5000,
              "description": "拖拽纵向偏移（drag_by），单位屏幕像素。"},
        "text": {"type": "string", "description": "要输入的文字（type_text_at）。"},
        "anchor": {"type": "string",
                   "description": "锚点名字（click_anchor / save_anchor）。就是给某个固定"
                                  "位置起的中文短名，如「发送按钮」。click_anchor 用它点名"
                                  "点击；save_anchor 用它记住当前位置（不填 x/y 就记鼠标现在"
                                  "停的地方）。"},
        "note": {"type": "string", "description": "给人看的备注（save_anchor 可省略）。"},
        "url": {"type": "string", "description": "要打开的网址（navigate）。"},
        "keys": {"type": "string",
              "description": "按键或组合键，如 enter、control+a、alt+f4、control+shift+a。"},
        "key": {"type": "string", "description": "单个按键（key_down / key_up 用），如 control、shift。"},
        "direction": {"type": "string", "enum": ["up", "down", "left", "right"],
              "description": "滚动方向。"},
        "magnitude": {"type": "integer", "minimum": 0, "maximum": 999,
              "description": "滚动量，0-999。"},
        "seconds": {"type": "integer", "minimum": 1, "maximum": 10,
              "description": "等待秒数（wait）。"},
        "press_enter": {"type": "boolean", "description": "输入文字后是否按回车。"},
        "clear_before_typing": {"type": "boolean", "description": "输入前是否先清空目标输入框，默认 true。"},
        "code": {"type": "string",
              "description": "要执行的 Python 代码（run_code）。在本机 Python 解释器里跑，"
                             "stdout/stderr 会回传给你；缺的第三方包会自动 pip 安装后重跑；"
                             "stdin 是空的（别用 input()），单次 300 秒上限。"
                             "务必用 print() 输出关键结果。"},
    },
    "required": ["status", "message", "action", "finished"],
    "additionalProperties": False,
}


@dataclass
class AgentAction:
    name: str
    arguments: dict[str, Any]
    message: str = ""


@dataclass
class AgentDecision:
    status: str                 # "action" | "finish" | "ask_user"
    message: str
    action: AgentAction | None = None
    finish_after_action: bool = False
    step_budget: int | None = None   # 模型自报的剩余步数预算（None = 没报）


class DecisionError(Exception):
    """豆包没有返回可用决策时抛出。"""


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def _extract_json(text: str) -> str | None:
    """从模型回复里抠出 JSON 对象字符串，容忍 ```json 围栏和前后废话。"""
    if not text:
        return None
    # 1) 代码围栏
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    # 2) 第一个 { 到最后一个 }（模型把 JSON 包在正文中）
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start:end + 1]
    return None


def parse_agent_decision(text: str) -> AgentDecision:
    """把豆包回复文本解析成 AgentDecision；解析不出直接抛 DecisionError。"""
    raw = _extract_json(text)
    if not raw:
        raise DecisionError("回复里没有找到 JSON 动作对象")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DecisionError(f"JSON 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise DecisionError("JSON 不是对象")

    status = str(data.get("status") or "").strip().lower()
    message = str(data.get("message") or "").strip()
    action_name = str(data.get("action") or "none").strip().lower()
    finished = _norm_bool(data.get("finished", False))
    step_budget = _norm_budget(data.get("step_budget"))

    if status not in {"action", "finish", "ask_user"}:
        raise DecisionError(f"未知的 status：{status!r}")
    if status == "finish":
        return AgentDecision(status="finish", message=message, step_budget=step_budget)
    if status == "ask_user":
        return AgentDecision(status="ask_user", message=message, step_budget=step_budget)
    if action_name == "none":
        raise DecisionError("status 是 action 但没有给出 action")

    normalized = ACTION_ALIASES.get(action_name, action_name)
    if normalized == "none" or normalized not in ACTION_NAMES:
        raise DecisionError(f"不支持的动作：{action_name!r}")

    arguments = {
        k: v for k, v in data.items()
        if k not in {"status", "message", "action", "finished", "step_budget"}
        and v is not None
    }
    _norm_coords(arguments)
    for name, default in (("press_enter", False), ("clear_before_typing", True)):
        if name in arguments:
            arguments[name] = _norm_bool(arguments[name], default)
    if normalized == "type_text_at":
        txt = str(arguments.get("text") or "")
        if txt.endswith(("\r", "\n")):
            arguments["press_enter"] = arguments.get("press_enter", False) or True
            arguments["text"] = txt.rstrip("\r\n")
        arguments.setdefault("press_enter", False)
        arguments.setdefault("clear_before_typing", True)

    return AgentDecision(
        status="action",
        message=message,
        action=AgentAction(name=normalized, arguments=arguments, message=message),
        finish_after_action=finished,
        step_budget=step_budget,
    )


def _norm_budget(value: Any) -> int | None:
    """把模型自报的步数预算规整成 1-500 的整数；没给或给不出就返回 None。"""
    if value is None or value == "":
        return None
    try:
        budget = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(1, min(500, budget))


def _norm_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s == "true":
            return True
        if s == "false":
            return False
    raise DecisionError(f"布尔值格式不对：{value!r}")


def _norm_coords(arguments: dict[str, Any]) -> None:
    for x_name, y_name in (("x", "y"), ("destination_x", "destination_y")):
        if x_name not in arguments and y_name not in arguments:
            continue
        xv = arguments.get(x_name)
        yv = arguments.get(y_name)
        if isinstance(xv, (list, tuple)) and len(xv) >= 2 and (yv is None or yv == xv):
            arguments[x_name], arguments[y_name] = int(round(float(xv[0]))), int(round(float(xv[1])))
            continue
        if x_name in arguments:
            arguments[x_name] = int(round(float(xv)))
        if y_name in arguments:
            arguments[y_name] = int(round(float(yv)))
    for name in ("delta_x", "delta_y"):
        if name in arguments:
            arguments[name] = int(round(float(arguments[name])))
