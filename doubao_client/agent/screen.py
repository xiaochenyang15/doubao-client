from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import struct
import tempfile
import time
import zlib
from ctypes import wintypes
from dataclasses import dataclass

from .actions import screen_bounds

logger = logging.getLogger("agent")

DEFAULT_MAX_WIDTH = 1920      # 超过就等比缩小；1920 能保住小字号控件的可读性
JPEG_QUALITY = 88
KEEP_IMAGES = 8               # 临时目录里最多留几张历史截图
GRID_CELLS = 10               # 网格格数：10 格 = 每 100 归一化单位一条线
DRAW_GRID = True              # 是否在图上画归一化网格标尺

_SRCCOPY = 0x00CC0020
_CAPTUREBLT = 0x40000000
_DIB_RGB_COLORS = 0


class ScreenCaptureError(RuntimeError):
    """抓屏失败（无桌面会话、GDI 资源创建失败等）。"""


@dataclass
class Screenshot:
    """一张截图的元信息。坐标一律是「整屏像素」，与动作层同一坐标系。"""
    path: str
    width: int
    height: int
    screen_left: int
    screen_top: int
    screen_width: int
    screen_height: int
    digest: str = ""
    signature: str = ""        # 8x8 灰度指纹，用于「屏幕有没有变」的粗判定
    grid: bool = False         # 图上是否画了归一化网格
    window_title: str = ""
    created_at: float = 0.0

    def context(self) -> str:
        """给提示词用的一行画面说明。"""
        title = " ".join((self.window_title or "").split())[:120]
        bits = [f"截图是整块屏幕（{self.screen_width}x{self.screen_height} 像素，"
                f"左边界 {self.screen_left}，上边界 {self.screen_top}）"]
        if title:
            bits.append(f"当前前台窗口标题：{title!r}")
        bits.append(f"图片尺寸 {self.width}x{self.height}")
        if self.grid:
            bits.append("图上已画好归一化网格：每条红线旁的数字就是该处的 x（顶部）"
                        "或 y（左侧）坐标，直接读数字即可，不用换算图片像素")
        else:
            bits.append("坐标按整屏 0-1000 归一化，看图时请按比例换算")
        return "。".join(bits) + "。"


# --------------------------------------------------------------------------- #
# GDI 抓像素
# --------------------------------------------------------------------------- #
class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _win32_gdi():
    """声明好 GDI/USER32 的签名。

    这一步**不能省**：ctypes 默认把返回值当 32 位有符号 int，而 64 位 Windows 上
    HDC/HBITMAP 是 64 位句柄——句柄值一大就会被截断成负数，后续调用报
    ``OverflowError: int too long to convert``，表现为「截图时灵时不灵」。
    """
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]

    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, wintypes.HDC,
                             ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                                wintypes.UINT, ctypes.c_void_p,
                                ctypes.POINTER(_BITMAPINFO), wintypes.UINT]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    return user32, gdi32


def _grab_bgra(left: int, top: int, width: int, height: int) -> bytes:
    """用 GDI 抓一块屏幕，返回 width*height*4 的 BGRA（自上而下）。"""
    user32, gdi32 = _win32_gdi()

    hdc = user32.GetDC(None)
    if not hdc:
        raise ScreenCaptureError("GetDC 失败，可能没有可用的桌面会话")
    mem_dc = gdi32.CreateCompatibleDC(hdc)
    bitmap = gdi32.CreateCompatibleBitmap(hdc, width, height)
    if not mem_dc or not bitmap:
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        user32.ReleaseDC(None, hdc)
        raise ScreenCaptureError("创建 GDI 位图失败")
    old = gdi32.SelectObject(mem_dc, bitmap)
    try:
        ok = gdi32.BitBlt(mem_dc, 0, 0, width, height, hdc, left, top,
                          _SRCCOPY | _CAPTUREBLT)
        if not ok:
            raise ScreenCaptureError("BitBlt 抓屏失败")
        bi = _BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = width
        bi.bmiHeader.biHeight = -height          # 负数 = 自上而下
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0           # BI_RGB
        buf = ctypes.create_string_buffer(width * height * 4)
        got = gdi32.GetDIBits(mem_dc, bitmap, 0, height, buf,
                              ctypes.byref(bi), _DIB_RGB_COLORS)
        if got == 0:
            raise ScreenCaptureError("GetDIBits 读取像素失败")
        return buf.raw
    finally:
        try:
            gdi32.SelectObject(mem_dc, old)
        except Exception:
            pass
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, hdc)


def _bgra_to_rgb(data: bytes, width: int, height: int) -> bytearray:
    """BGRA → RGB（去掉 alpha）。"""
    n = width * height
    out = bytearray(n * 3)
    src = memoryview(data)
    # 逐像素搬比较慢，用切片批量跳 alpha 更实际：按 4 字节步长
    for i in range(n):
        s = i * 4
        d = i * 3
        out[d] = src[s + 2]      # R
        out[d + 1] = src[s + 1]  # G
        out[d + 2] = src[s]      # B
    return out


def _shrink(rgb: bytearray, width: int, height: int, max_width: int):
    """最近邻缩小到 max_width 以内（保持宽高比）。"""
    if max_width <= 0 or width <= max_width:
        return rgb, width, height
    scale = max_width / float(width)
    nw = max(1, int(width * scale))
    nh = max(1, int(height * scale))
    out = bytearray(nw * nh * 3)
    src_stride = width * 3
    for y in range(nh):
        sy = min(height - 1, int(y / scale))
        row = sy * src_stride
        base = y * nw * 3
        for x in range(nw):
            sx = min(width - 1, int(x / scale)) * 3
            s = row + sx
            d = base + x * 3
            out[d] = rgb[s]
            out[d + 1] = rgb[s + 1]
            out[d + 2] = rgb[s + 2]
    return out, nw, nh


def _thumb_signature(rgb: bytearray, width: int, height: int, cells: int = 8) -> str:
    """把画面压成 cells×cells 的灰度指纹（每格量化到 0-15）。

    直接拿整图 md5 当指纹太敏感——光标闪一下、时钟跳一秒就算「变了」，
    会让「连续无变化就停」的判定永远失效。用粗粒度的灰度格网能忽略这种噪声，
    又能感知「窗口开了 / 页面跳了 / 弹窗出来了」这类真实变化。
    """
    if width <= 0 or height <= 0:
        return ""
    out: list[str] = []
    for gy in range(cells):
        y0 = gy * height // cells
        y1 = max(y0 + 1, (gy + 1) * height // cells)
        step_y = max(1, (y1 - y0) // 6)
        for gx in range(cells):
            x0 = gx * width // cells
            x1 = max(x0 + 1, (gx + 1) * width // cells)
            step_x = max(1, (x1 - x0) // 6)
            total = 0
            count = 0
            for y in range(y0, y1, step_y):
                row = y * width
                for x in range(x0, x1, step_x):
                    i = (row + x) * 3
                    total += (rgb[i] * 299 + rgb[i + 1] * 587 + rgb[i + 2] * 114) // 1000
                    count += 1
            avg = total // max(count, 1)
            out.append("%x" % min(15, avg // 16))
    return "".join(out)


# --------------------------------------------------------------------------- #
# 归一化网格标尺
# --------------------------------------------------------------------------- #
# 视觉模型对「绝对坐标回归」很不稳：让它看着一张 1600×900 的图直接报 0-1000 的
# 归一化值，误差经常到几十个单位（整屏几百像素），点下去就偏了。图上画一层带读数
# 的网格，模型只要「看线读数」，误差能压到一个格子以内。
_GRID_LINE = (255, 72, 72)      # 网格线：偏红，浅色/深色背景上都看得见
_GRID_INK = (255, 255, 255)     # 读数文字
_GRID_BOX = (24, 24, 28)        # 读数底衬（保证白字在任何背景上都可读）

# 3×5 点阵数字（够画 0-1000 的读数，比引入字体渲染省事）
_DIGITS = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "001", "001", "001"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
}


def _blend(rgb: bytearray, width: int, height: int,
           x: int, y: int, colour: tuple[int, int, int], alpha: float) -> None:
    """把一个像素朝 colour 混合 alpha（0-1），越界自动忽略。"""
    if x < 0 or y < 0 or x >= width or y >= height:
        return
    i = (y * width + x) * 3
    keep = 1.0 - alpha
    rgb[i] = int(rgb[i] * keep + colour[0] * alpha)
    rgb[i + 1] = int(rgb[i + 1] * keep + colour[1] * alpha)
    rgb[i + 2] = int(rgb[i + 2] * keep + colour[2] * alpha)


def _fill(rgb: bytearray, width: int, height: int, x0: int, y0: int,
          x1: int, y1: int, colour: tuple[int, int, int], alpha: float) -> None:
    for y in range(max(0, y0), min(height, y1)):
        for x in range(max(0, x0), min(width, x1)):
            _blend(rgb, width, height, x, y, colour, alpha)


def _draw_text(rgb: bytearray, width: int, height: int, x: int, y: int,
               text: str, scale: int) -> None:
    """在 (x, y) 处画一串数字（带深色底衬，保证可读）。"""
    text = "".join(ch for ch in str(text) if ch in _DIGITS)
    if not text:
        return
    adv = 4 * scale
    box_w = len(text) * adv + scale
    box_h = 5 * scale + 2 * scale
    _fill(rgb, width, height, x - scale, y - scale, x + box_w, y + box_h,
          _GRID_BOX, 0.72)
    for n, ch in enumerate(text):
        glyph = _DIGITS[ch]
        gx = x + n * adv
        for row, bits in enumerate(glyph):
            for col, bit in enumerate(bits):
                if bit == "1":
                    _fill(rgb, width, height,
                          gx + col * scale, y + row * scale,
                          gx + (col + 1) * scale, y + (row + 1) * scale,
                          _GRID_INK, 0.95)


def draw_grid(rgb: bytearray, width: int, height: int,
              cells: int = GRID_CELLS) -> None:
    """在 RGB 缓冲上画归一化网格：每 1000/cells 个单位一条线，并标注读数。

    读数直接是**归一化坐标值**（0-1000），和模型要输出的 x/y 同一把尺子，
    所以它不用做任何比例换算，照读即可。
    """
    if width < 80 or height < 80 or cells < 2:
        return
    scale = max(2, min(4, width // 620))
    step_x = width / float(cells)
    step_y = height / float(cells)
    unit = 1000.0

    # 竖线 + 顶部读数
    for i in range(1, cells):
        px = int(step_x * i)
        for t in range(2):
            for y in range(height):
                _blend(rgb, width, height, px + t, y, _GRID_LINE, 0.42)
        _draw_text(rgb, width, height, min(px + scale, width - 6 * scale),
                   scale, str(int(round(unit * i / cells))), scale)

    # 横线 + 左侧读数
    for j in range(1, cells):
        py = int(step_y * j)
        for t in range(2):
            for x in range(width):
                _blend(rgb, width, height, x, py + t, _GRID_LINE, 0.42)
        _draw_text(rgb, width, height, scale, max(scale, py - 6 * scale),
                   str(int(round(unit * j / cells))), scale)


def _encode_jpeg(path: str, width: int, height: int, rgb: bytearray) -> bool:
    """用 wx.Image 编 JPEG；wx 不可用或失败返回 False。"""
    try:
        import wx  # noqa: F401
    except Exception:
        return False
    try:
        img = wx.Image(width, height, bytes(rgb))
        img.SetOption(wx.IMAGE_OPTION_QUALITY, str(JPEG_QUALITY))
        ok = img.SaveFile(path, wx.BITMAP_TYPE_JPEG)
        return bool(ok and os.path.exists(path) and os.path.getsize(path) > 0)
    except Exception as exc:  # noqa: BLE001
        logger.debug("wx JPEG 编码失败，回退 PNG：%s", exc)
        try:
            os.path.exists(path) and os.remove(path)
        except Exception:
            pass
        return False


def _encode_png(path: str, width: int, height: int, rgb: bytearray) -> None:
    """纯标准库写 PNG（truecolor，无 alpha）。"""
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)                                 # filter type 0
        raw += rgb[y * stride:(y + 1) * stride]
    comp = zlib.compress(bytes(raw), 6)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", comp)
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


# --------------------------------------------------------------------------- #
# 临时文件管理
# --------------------------------------------------------------------------- #
def _shot_dir() -> str:
    d = os.path.join(tempfile.gettempdir(), "doubao_agent_shots")
    os.makedirs(d, exist_ok=True)
    return d


def _cleanup(keep: int = KEEP_IMAGES) -> None:
    try:
        files = [os.path.join(_shot_dir(), n) for n in os.listdir(_shot_dir())]
        files = [f for f in files if f.lower().endswith((".jpg", ".png"))]
        files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
        for f in files[keep:]:
            try:
                os.remove(f)
            except Exception:
                pass
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def capture(max_width: int = DEFAULT_MAX_WIDTH,
            window_title: str = "",
            grid: bool = DRAW_GRID) -> Screenshot:
    """抓一张整屏截图并落盘，返回元信息。"""
    left, top, width, height = screen_bounds()
    if width <= 0 or height <= 0:
        raise ScreenCaptureError("屏幕尺寸无效")

    started = time.time()
    bgra = _grab_bgra(left, top, width, height)
    rgb = _bgra_to_rgb(bgra, width, height)
    rgb, out_w, out_h = _shrink(rgb, width, height, max_width)
    # 指纹要在画网格之前算：网格是固定图案，画上去纯属浪费计算
    signature = _thumb_signature(rgb, out_w, out_h)
    if grid:
        try:
            draw_grid(rgb, out_w, out_h, GRID_CELLS)
        except Exception:  # noqa: BLE001
            logger.debug("画归一化网格失败，继续用原图", exc_info=True)
            grid = False

    stamp = time.strftime("%H%M%S") + f"_{int(started * 1000) % 1000:03d}"
    jpg = os.path.join(_shot_dir(), f"shot_{stamp}.jpg")
    if not _encode_jpeg(jpg, out_w, out_h, rgb):
        png = os.path.join(_shot_dir(), f"shot_{stamp}.png")
        _encode_png(png, out_w, out_h, rgb)
        path = png
    else:
        path = jpg

    with open(path, "rb") as f:
        head = f.read(65536)
    digest = hashlib.md5(head).hexdigest()[:12]

    _cleanup()
    shot = Screenshot(
        path=path, width=out_w, height=out_h,
        screen_left=left, screen_top=top,
        screen_width=width, screen_height=height,
        digest=digest, signature=signature, grid=bool(grid),
        window_title=window_title, created_at=started,
    )
    logger.debug("截图完成：%s (%dx%d, %.0f ms)", path, out_w, out_h,
                 (time.time() - started) * 1000)
    return shot
