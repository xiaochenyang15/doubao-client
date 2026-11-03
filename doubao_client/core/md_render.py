from __future__ import annotations

import re
import webbrowser
from dataclasses import dataclass, field

import wx
import wx.richtext as rt

# ---------------------------------------------------------------- 块级正则

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*(\S*)")
_QUOTE = re.compile(r"^ {0,3}>[ \t]?(.*)$")
_UL = re.compile(r"^([ \t]*)[-*+][ \t]+(.*)$")
_OL = re.compile(r"^([ \t]*)(\d{1,9})[.)][ \t]+(.*)$")
_HR = re.compile(r"^ {0,3}([-*_])[ \t]*(?:\1[ \t]*){2,}$")
_TABLE_ROW = re.compile(r"^ {0,3}\|(.+)\|[ \t]*$")
_TABLE_SEP = re.compile(
    r"^ {0,3}\|?[ \t]*:?-{1,}:?[ \t]*(?:\|[ \t]*:?-{1,}:?[ \t]*)+\|?[ \t]*$")

# ---------------------------------------------------------------- 行内正则
# 顺序有讲究：*** 要先于 ** 和 *，否则加粗斜体会被拆成加粗 + 一个星号。
_INLINE = re.compile(
    r"(?P<code>`(?P<code_t>[^`\n]+)`)"
    r"|(?P<bi>\*\*\*(?P<bi_t>\S(?:[^*]*?\S)?)\*\*\*)"
    r"|(?P<b>\*\*(?P<b_t>\S(?:.*?\S)?)\*\*)"
    r"|(?P<b2>__(?P<b2_t>\S(?:.*?\S)?)__)"
    r"|(?P<i>\*(?P<i_t>\S(?:[^*\n]*?\S)?)\*)"
    r"|(?P<i2>_(?P<i2_t>\S(?:[^_\n]*?\S)?)_)"
    r"|(?P<s>~~(?P<s_t>\S(?:.*?\S)?)~~)"
    r"|(?P<lnk>\[(?P<l_t>[^\]\n]+)\]\((?P<l_u>[^)\s]*)\))"
    r"|(?P<auto><(?P<a_u>https?://[^>\s]+)>)"
)

# 判断「这段正文到底是不是 Markdown」时只看这些明确信号，
# 免得把 "3 * 4 * 5" 或路径里的下划线误当成语法染色。
_MD_BLOCK_SIGNAL = re.compile(
    r"(?m)^[ \t]*(?:#{1,6}[ \t]+\S|`{3}|~{3}|\|[^\n]*\|[ \t]*$|>[ \t]?\S"
    r"|[-*+][ \t]+\S|\d{1,9}[.)][ \t]+\S)")
_MD_INLINE_SIGNAL = re.compile(
    r"`[^`\n]+`|\*\*\S|\*\*\*|~~\S|\[\S*\s?\]\([^)\s]+\)|<https?://")

_MONO_FACES = ("Consolas", "Cascadia Mono", "Courier New")
# 表格里中英混排多，优先中文等宽（新宋体），退而求其次再用西文等宽
_TABLE_FACES = ("NSimSun", "Consolas", "Cascadia Mono", "Courier New")


# ---------------------------------------------------------------- 数据
@dataclass
class Block:
    """一个排版单元。"""

    kind: str                                   # heading/para/code/quote/list/hr/table
    text: str = ""                              # heading/para/quote 的正文（可含换行）
    level: int = 0                              # 标题级别，或列表层级
    lang: str = ""                              # 代码块语言标记
    ordered: bool = False                       # 有序列表
    start: int = 1                              # 有序列表起始号
    lines: list[str] = field(default_factory=list)      # 代码块的行
    items: list[tuple[int, str]] = field(default_factory=list)  # 列表项 (层级, 文本)


# ---------------------------------------------------------------- 判定
def looks_like_markdown(text: str) -> bool:
    """粗判一段正文是不是 Markdown，决定要不要排版。

    宁可漏判也不要误判：纯文本消息原样显示，一个字符都不动。
    """
    if not text or len(text) < 2:
        return False
    return bool(_MD_BLOCK_SIGNAL.search(text) or _MD_INLINE_SIGNAL.search(text))


def strip_markdown(text: str) -> str:
    """去掉行内标记符号，给列表里的一行摘要用；正文本身不受影响。"""
    if not text or not looks_like_markdown(text):
        return text
    out = []
    pos = 0
    for m in _INLINE.finditer(text):
        out.append(text[pos:m.start()])
        if m.group("code"):
            out.append(m.group("code_t"))
        elif m.group("lnk"):
            out.append(m.group("l_t"))
        elif m.group("auto"):
            out.append(m.group("a_u"))
        else:
            out.append(_inner(m))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def summary(text: str) -> str:
    """压成一行摘要：去掉块级符号（# - > ``` |），保留全部文字。"""
    if not text or not looks_like_markdown(text):
        return text
    parts: list[str] = []
    for b in parse(text):
        if b.kind == "code":
            parts.extend(b.lines)
        elif b.kind in ("heading", "para", "quote"):
            parts.append(b.text)
        elif b.kind == "list":
            parts.extend(t for _lv, t in b.items)
        elif b.kind == "table":
            parts.extend(" ".join(r) for r in b.lines)
    joined = strip_markdown(" ".join(p for p in parts if p.strip()))
    return joined or text


def _inner(m: re.Match) -> str:
    for key in ("bi_t", "b_t", "b2_t", "i_t", "i2_t", "s_t"):
        val = m.groupdict().get(key)
        if val is not None:
            return val
    return m.group(0)


# ---------------------------------------------------------------- 解析
def parse(text: str) -> list[Block]:
    """把正文切成排版单元。未知结构一律落到普通段落，字符不丢。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[Block] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        m = _FENCE.match(line)
        if m:
            fence, lang = m.group(1), m.group(2)
            body: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith(fence[:3]):
                body.append(lines[i])
                i += 1
            i += 1 if i < n else 0
            blocks.append(Block("code", lang=lang, lines=body))
            continue

        if _HR.match(line):
            blocks.append(Block("hr"))
            i += 1
            continue

        m = _HEADING.match(line)
        if m:
            blocks.append(Block("heading", text=m.group(2).strip(),
                                level=len(m.group(1))))
            i += 1
            continue

        if _QUOTE.match(line):
            body = []
            while i < n:
                q = _QUOTE.match(lines[i])
                if not q:
                    break
                body.append(q.group(1))
                i += 1
            blocks.append(Block("quote", text="\n".join(body)))
            continue

        if (_TABLE_ROW.match(line) and i + 1 < n
                and _TABLE_SEP.match(lines[i + 1])):
            rows = [_cells(line)]
            i += 2
            while i < n and _TABLE_ROW.match(lines[i]):
                rows.append(_cells(lines[i]))
                i += 1
            blocks.append(Block("table", lines=rows))  # type: ignore[arg-type]
            continue

        m = _UL.match(line)
        if m:
            items, i = _list_items(lines, i, ordered=False)
            blocks.append(Block("list", ordered=False, items=items))
            continue

        m = _OL.match(line)
        if m:
            items, i = _list_items(lines, i, ordered=True)
            blocks.append(Block("list", ordered=True,
                                start=int(m.group(2)), items=items))
            continue

        para: list[str] = []
        while i < n and lines[i].strip() and not _block_starts(lines, i):
            para.append(lines[i].rstrip())
            i += 1
        if para:
            blocks.append(Block("para", text="\n".join(para)))
    return blocks


def _block_starts(lines, i) -> bool:
    """第 i 行是否开启一个新块（用于收拢段落）。"""
    line = lines[i]
    if _FENCE.match(line) or _HR.match(line) or _HEADING.match(line):
        return True
    if _QUOTE.match(line) or _UL.match(line) or _OL.match(line):
        return True
    return bool(_TABLE_ROW.match(line) and i + 1 < len(lines)
                and _TABLE_SEP.match(lines[i + 1]))


def _list_items(lines, i, ordered):
    pat = _OL if ordered else _UL
    items: list[tuple[int, str]] = []
    while i < len(lines):
        m = pat.match(lines[i])
        if not m:
            # 列表项的续行：缩进 >= 2 且不是新列表项时并到上一项
            if items and lines[i].strip() and lines[i].startswith(("  ", "\t")):
                lv, txt = items[-1]
                items[-1] = (lv, txt + "\n" + lines[i].strip())
                i += 1
                continue
            break
        indent = m.group(1).expandtabs(2)
        txt = m.group(3) if ordered else m.group(2)
        items.append((min(len(indent) // 2, 4), txt.strip()))
        i += 1
    return items, i


def _cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in body.split("|")]


# ---------------------------------------------------------------- 渲染
class _Ctx:
    """渲染时共用的一点点状态。"""

    def __init__(self, rtc, fg, bg, base_pt, muted):
        self.rtc = rtc
        self.fg = fg
        self.bg = bg
        self.base = base_pt
        self.muted = muted
        self.code_fg = fg
        self.code_bg = _shade(bg, -12 if _luma(bg) > 128 else 18)
        self.rule = muted
        self.head = [base_pt + 6, base_pt + 4, base_pt + 3,
                     base_pt + 2, base_pt + 1, base_pt + 1]
        self.mono = None
        self.table_mono = None
        self.links: list[tuple[int, int, str]] = []


def _luma(c: wx.Colour) -> int:
    return (c.Red() * 299 + c.Green() * 587 + c.Blue() * 114) // 1000


def _shade(c: wx.Colour, delta: int) -> wx.Colour:
    f = lambda v: max(0, min(255, v + delta))  # noqa: E731
    return wx.Colour(f(c.Red()), f(c.Green()), f(c.Blue()))


def _uniform(fnt) -> bool:
    """这个字体是不是「中文正好两倍宽、西文等宽」。

    表格和代码块靠空格对齐，字体不严格等宽就会错列，所以先量一量。
    """
    dc = wx.MemoryDC(wx.Bitmap(8, 8))
    try:
        dc.SetFont(fnt)
        wa = dc.GetTextExtent("a")[0]
        return (wa > 0 and dc.GetTextExtent("i")[0] == wa
                and dc.GetTextExtent("W")[0] == wa
                and dc.GetTextExtent("中")[0] == 2 * wa)
    finally:
        dc.SelectObject(wx.NullBitmap)


def _pick_font(faces, size):
    """在候选字体里挑第一个真正合格等宽的，全不合格就用系统等宽。"""
    info = wx.FontInfo(max(size, 7))
    for face in faces:
        f = wx.Font(info.FaceName(face))
        if f.IsOk() and _uniform(f):
            return f
    return wx.Font(info.Family(wx.FONTFAMILY_TELETYPE))


def _mono_attr(font, colour=None, bg=None) -> rt.RichTextAttr:
    """等宽文本的富文本属性。

    只 SetFont 不够：wxRichTextAttr 有时不认字体名，控件会退回默认字体，
    表格按空格对齐就会错列，所以字体名单独再设一遍。
    """
    attr = rt.RichTextAttr()
    attr.SetFont(font)
    attr.SetFontFaceName(font.GetFaceName())
    attr.SetFontSize(font.GetPointSize())
    if colour is not None:
        attr.SetTextColour(colour)
    if bg is not None:
        attr.SetBackgroundColour(bg)
    return attr


def _mono_font(ctx, size):
    if ctx.mono is None:
        ctx.mono = _pick_font(_MONO_FACES, size)
    ctx.mono.SetPointSize(max(size, 7))
    return ctx.mono


def _table_font(ctx, size):
    if ctx.table_mono is None:
        ctx.table_mono = _pick_font(_TABLE_FACES, size)
    ctx.table_mono.SetPointSize(max(size, 7))
    return ctx.table_mono


def render(rtc, text: str, fg=None, bg=None, font=None, markdown=True):
    """把 ``text`` 排进 ``rtc``（调用前先清空）。

    ``markdown=False`` 时按纯文本原样铺，一个字符都不改。
    """
    fg = fg or rtc.GetForegroundColour()
    bg = bg or rtc.GetBackgroundColour()
    base = (font or rtc.GetFont()).GetPointSize()
    ctx = _Ctx(rtc, fg, bg, base, DEF_MUTED if _luma(bg) > 128 else LIGHT_MUTED)

    rtc.Freeze()
    try:
        rtc.BeginSuppressUndo()
        rtc.SetBackgroundColour(bg)
        rtc.Clear()
        rtc.BeginTextColour(fg)
        if markdown:
            for b in parse(text):
                _block(ctx, b)
            if _shrunk(text, rtc.GetValue()):
                # 兜底：排版吃掉了太多字符，宁可退回原样也不丢正文
                rtc.Clear()
                rtc.BeginTextColour(fg)
                _plain(ctx, text)
                rtc.EndTextColour()
        else:
            _plain(ctx, text)
        rtc.EndTextColour()
        rtc.EndSuppressUndo()
    finally:
        rtc.Thaw()
    rtc._md_links = ctx.links
    rtc.SetInsertionPoint(0)
    rtc.SetEditable(False)


MUTED_LIGHT = wx.Colour(0x5A, 0x62, 0x70)
MUTED_DARK = wx.Colour(0xB6, 0xBE, 0xCA)
DEF_MUTED, LIGHT_MUTED = MUTED_LIGHT, MUTED_DARK


def _plain(ctx, text):
    """纯文本：原样写入。RichTextCtrl 自己处理 \n 为换行。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for k, ln in enumerate(lines):
        if ln:
            ctx.rtc.WriteText(ln)
        if k != len(lines) - 1:
            ctx.rtc.Newline()


def _squash(s: str) -> str:
    return "".join(ch for ch in s if not ch.isspace())


def _shrunk(orig: str, got: str) -> bool:
    """排版后的字符数是否明显少于原文（说明解析器吃掉了内容）。"""
    a, b = _squash(orig), _squash(got)
    if not a:
        return False
    return len(b) < len(a) * 55 // 100


def _nl(ctx, n: int = 1):
    """换 n 行（RichTextCtrl.Newline 不吃参数，只能一行一行来）。"""
    for _ in range(max(1, n)):
        ctx.rtc.Newline()


def _inline(ctx, text: str):
    """写一段带行内格式的文本。"""
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            ctx.rtc.WriteText(text[pos:m.start()])
        _run(ctx, m)
        pos = m.end()
    if pos < len(text):
        ctx.rtc.WriteText(text[pos:])


def _run(ctx, m: re.Match):
    rtc = ctx.rtc
    if m.group("code"):
        attr = _mono_attr(_mono_font(ctx, ctx.base - 1),
                          ctx.code_fg, ctx.code_bg)
        rtc.BeginStyle(attr)
        rtc.WriteText(m.group("code_t"))
        rtc.EndStyle()
        return
    if m.group("lnk") or m.group("auto"):
        label = m.group("l_t") if m.group("lnk") else m.group("a_u")
        url = m.group("l_u") if m.group("lnk") else m.group("a_u")
        _link(ctx, label, url)
        return
    if m.group("bi"):
        rtc.BeginBold()
        rtc.BeginItalic()
        _inline(ctx, m.group("bi_t"))
        rtc.EndItalic()
        rtc.EndBold()
        return
    if m.group("b") or m.group("b2"):
        rtc.BeginBold()
        _inline(ctx, m.group("b_t") or m.group("b2_t"))
        rtc.EndBold()
        return
    if m.group("i") or m.group("i2"):
        rtc.BeginItalic()
        _inline(ctx, m.group("i_t") or m.group("i2_t"))
        rtc.EndItalic()
        return
    if m.group("s"):
        # wx 的 RichTextAttr 没有删除线属性，用弱化色表示划掉
        rtc.BeginTextColour(ctx.muted)
        _inline(ctx, m.group("s_t"))
        rtc.EndTextColour()
        return
    rtc.WriteText(m.group(0))


def _link(ctx, label: str, url: str):
    rtc = ctx.rtc
    target = url or label
    start = rtc.GetLastPosition()
    rtc.BeginTextColour(wx.Colour(0x1A, 0x62, 0xC9))
    rtc.BeginUnderline()
    try:
        rtc.BeginURL(target)
        rtc.WriteText(label or target)
        rtc.EndURL()
    finally:
        rtc.EndUnderline()
        rtc.EndTextColour()
    ctx.links.append((start, rtc.GetLastPosition(), target))


def _block(ctx, b: Block):
    rtc = ctx.rtc
    if b.kind == "code":
        _code(ctx, b)
    elif b.kind == "heading":
        size = ctx.head[min(b.level, 6) - 1]
        rtc.BeginFontSize(size)
        rtc.BeginBold()
        _inline(ctx, b.text)
        rtc.EndBold()
        rtc.EndFontSize()
        _nl(ctx, 2)
    elif b.kind == "quote":
        rtc.BeginTextColour(ctx.muted)
        # 一条竖线就够说明引用了，不再额外缩进（缩进叠上去反而和别的块错位）
        for ln in b.text.split("\n"):
            rtc.WriteText(("│ " + ln) if ln else "│")
            rtc.Newline()
        rtc.EndTextColour()
        rtc.Newline()
    elif b.kind == "list":
        _list(ctx, b)
    elif b.kind == "hr":
        rtc.BeginTextColour(ctx.rule)
        rtc.WriteText("─" * 32)
        rtc.EndTextColour()
        _nl(ctx, 2)
    elif b.kind == "table":
        _table(ctx, b)
    else:
        _inline(ctx, b.text)
        _nl(ctx, 2)


def _code(ctx, b: Block):
    rtc = ctx.rtc
    attr = _mono_attr(_mono_font(ctx, ctx.base - 1), ctx.code_fg, ctx.code_bg)
    rtc.BeginLeftIndent(ctx.base * 4, 0)
    rtc.BeginStyle(attr)
    # 先空一段：段落底色只有「段落开始之后」才铺满整行，先换行再写，
    # 后面的每一行才会继承到整段填充
    rtc.Newline()
    for ln in (b.lines or [""]):
        rtc.WriteText(ln if ln else " ")
        rtc.Newline()
    rtc.EndStyle()
    rtc.EndLeftIndent()
    rtc.Newline()


def _list(ctx, b: Block):
    rtc = ctx.rtc
    num = b.start
    for level, txt in b.items:
        rtc.BeginLeftIndent(ctx.base * 12 + level * ctx.base * 12, 0)
        if b.ordered:
            rtc.WriteText(f"{num}. ")
            num += 1
        else:
            rtc.WriteText("• ")
        _inline(ctx, txt)
        rtc.EndLeftIndent()
        rtc.Newline()
    rtc.Newline()


def _table(ctx, b: Block):
    """表格排成「竖线分列」的等宽文本块：表头加粗，逐行列出。

    wx 的富文本表格接口在这个控件里没暴露，空格补齐又受字体回退影响
    （中文宽度不一定正好两倍半角），所以不做补齐：只用竖线分列，
    行高一致、列内容对齐到各自的单元格开头，任何字体下都不会错位。
    """
    rows = [[strip_markdown(c) for c in r] for r in b.lines
            if isinstance(r, list)]
    if not rows:
        return
    lines = [" │ ".join(r) for r in rows]

    rtc = ctx.rtc
    attr = _mono_attr(_table_font(ctx, ctx.base), ctx.fg)
    rtc.BeginStyle(attr)
    rtc.BeginLeftIndent(ctx.base * 4, 0)
    rtc.BeginBold()
    rtc.WriteText(lines[0])
    rtc.EndBold()
    rtc.Newline()
    for line in lines[1:]:
        rtc.WriteText(line)
        rtc.Newline()
    rtc.EndLeftIndent()
    rtc.EndStyle()
    rtc.Newline()


def _disp_width(s: str) -> int:
    """等宽字体下的大致列宽：全角算 2，半角算 1。"""
    return sum(2 if _wide(ch) else 1 for ch in s)


def _wide(ch: str) -> bool:
    o = ord(ch)
    return (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF
            or 0xAC00 <= o <= 0xD7A3 or 0xF900 <= o <= 0xFAFF
            or 0xFE30 <= o <= 0xFE6F or 0xFF00 <= o <= 0xFF60
            or 0xFFE0 <= o <= 0xFFE6 or 0x1F300 <= o <= 0x1FAFF)


# ---------------------------------------------------------------- 链接点击
def link_at(rtc, point) -> str:
    """点在哪条链接上就返回它的 URL，否则空串。"""
    spans = getattr(rtc, "_md_links", None) or []
    if not spans:
        return ""
    pos = None
    hit = rtc.HitTest(point)
    if isinstance(hit, tuple) and len(hit) == 2 and isinstance(hit[1], int):
        pos = hit[1]
    if pos is None:
        pos = rtc.GetInsertionPoint()
    for start, end, url in spans:
        if start <= pos <= end:
            return url
    return ""


def bind_links(rtc):
    """让富文本里的链接可以点开（默认在系统浏览器里打开）。

    wxPython 没有暴露 richedit 的 URL 事件，只能自己记下每条链接的
    字符区间，点击时用命中位置反查。
    """

    def _click(evt):
        url = link_at(rtc, evt.GetPosition())
        if url:
            webbrowser.open(url)
            return
        evt.Skip()

    rtc.Bind(rt.EVT_RICHTEXT_LEFT_CLICK, _click)
