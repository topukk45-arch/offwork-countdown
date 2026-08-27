# -*- coding: utf-8 -*-
"""
下班倒计时 · 自绘版
分层窗口 (UpdateLayeredWindow) + Pillow 渲染，不使用 WebView2。
依赖: pip install pillow pystray
运行: pythonw widget.py
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import socket
import sys
import threading
import time
from datetime import date, datetime

from PIL import Image, ImageChops, ImageDraw, ImageFont

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

APP = "OffworkWidget"
RES = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
FROZEN = getattr(sys, "frozen", False)
DATA = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP)
CFG = os.path.join(DATA, "config.json")
IMGDIR = os.path.join(DATA, "images")
STARTUP_VBS = os.path.join(
    os.environ.get("APPDATA", ""),
    r"Microsoft\Windows\Start Menu\Programs\Startup", "OffworkWidget.vbs")
os.makedirs(IMGDIR, exist_ok=True)

SS = 3              # 圆角超采样倍数，画大 3 倍再缩回来就有抗锯齿了
RADIUS = 16

DEFAULTS = {
    "start": "09:30", "end": "18:30", "salary": 12000,
    "work_text": "距下班",
    "bg": "auto",              # auto | light | dark | glass
    "glass": 90,
    "ui": 100,                 # 缩放百分比
    "cardh": 104,
    "img_file": "", "iw": 45, "iw_auto": True, "ix": 0, "iy": 0, "fade": 45,
    "alttab": False,           # 是否出现在 Alt+Tab 列表里
    "top": True, "wx": None, "wy": None,
    "slots": [{"label": "周五", "type": "weekly", "value": "5"},
              {"label": "发薪", "type": "monthly", "value": "10"}],
}


# ============================ 配置 ============================
def read_cfg():
    cfg = dict(DEFAULTS)
    try:
        with open(CFG, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def write_cfg(patch):
    cur = read_cfg()
    cur.update(patch)
    try:
        with open(CFG, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print("save failed:", e)


# ============================ 系统信息 ============================
def system_theme():
    out = {"dark": True, "accent": (76, 194, 255)}
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        out["dark"] = winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except Exception:
        pass
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\DWM")
        v = winreg.QueryValueEx(k, "AccentColor")[0]        # AABBGGRR
        out["accent"] = (v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF)
    except Exception:
        pass
    return out


def load_font(names, size):
    for n in names:
        for d in (r"C:\Windows\Fonts", ""):
            try:
                return ImageFont.truetype(os.path.join(d, n) if d else n, size)
            except Exception:
                continue
    return ImageFont.load_default()


# 数字用 Bahnschrift（Win10 自带的窄体），中文用微软雅黑。
# Bahnschrift 没有中文字形，拿它渲染「已下班」只会得到一排豆腐块，
# 所以主显示区按内容里有没有中日韩字符来选字体。
def has_cjk(t):
    return any("\u3400" <= c <= "\u9fff" or "\uff00" <= c <= "\uffef" for c in str(t))


F_NUM = lambda s: load_font(["bahnschrift.ttf", "segoeui.ttf"], s)
F_TXT = lambda s: load_font(["msyh.ttc", "msyh.ttf", "segoeui.ttf"], s)
F_MON = lambda s: load_font(["consola.ttf", "cour.ttf", "segoeui.ttf"], s)


# ============================ 业务计算 ============================
def hm(s):
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def days_monthly(now, day):
    day = max(1, min(28, int(day)))
    today = now.date()
    y, m = today.year, today.month
    nxt = date(y, m, day)
    if nxt < today:
        m += 1
        if m > 12:
            m, y = 1, y + 1
        nxt = date(y, m, day)
    return (nxt - today).days


def days_date(now, iso, yearly=False):
    """yearly=True 时过期自动滚到下一年，生日国庆这类填一次就够"""
    try:
        t = datetime.strptime(iso, "%Y-%m-%d").date()
    except Exception:
        return None
    today = now.date()
    if yearly:
        try:
            t = t.replace(year=today.year)
        except ValueError:              # 2 月 29 日这种，退到 28 日
            t = t.replace(year=today.year, day=28)
        if t < today:
            try:
                t = t.replace(year=today.year + 1)
            except ValueError:
                t = t.replace(year=today.year + 1, day=28)
    return (t - today).days


def compute(cfg):
    """返回渲染需要的全部文字和进度"""
    now = datetime.now()
    cur = now.hour * 60 + now.minute + now.second / 60
    st, ed = hm(cfg["start"]), hm(cfg["end"])
    weekend = now.weekday() >= 5
    prog, big, sec = 0.0, "", ""

    if weekend:
        big, cap = "休息日", "不计薪"
    elif cur >= ed:
        big, cap, prog = "已下班", "今天到此为止", 1.0
        sec = ""
    elif cur < st:
        d = round((st - cur) * 60)
        big, sec = "%02d:%02d" % (d // 3600, d // 60 % 60), "%02d" % (d % 60)
        cap = "距上班 · " + cfg["start"]
    else:
        d = round((ed - cur) * 60)
        big, sec = "%02d:%02d" % (d // 3600, d // 60 % 60), "%02d" % (d % 60)
        prog = (cur - st) / (ed - st)
        cap = "%s · %s" % (cfg.get("work_text") or "距下班", cfg["end"])

    money = "%.2f" % (float(cfg.get("salary") or 0) / 21.75 * prog)

    parts = []
    for sl in cfg.get("slots", []):
        t = sl.get("type")
        if t == "weekly":
            d = (int(sl["value"]) - (now.weekday() + 1) % 7) % 7
        elif t == "monthly":
            d = days_monthly(now, sl["value"])
        else:
            d = days_date(now, sl.get("value", ""), bool(sl.get("yearly")))
        if d is not None and d >= 0:
            parts.append("%s %d 天" % (sl.get("label", ""), d))
    return big, sec, cap, money, parts


# ============================ 渲染 ============================
def render(cfg, theme):
    scale = max(0.75, min(1.5, float(cfg.get("ui", 100)) / 100))
    W = int(320 * scale)
    H = int(max(90, min(280, float(cfg.get("cardh", 104)))) * scale)
    r = int(RADIUS * scale)

    dark = theme["dark"] if cfg["bg"] in ("auto", "glass") else (cfg["bg"] == "dark")
    glass = cfg["bg"] == "glass"
    accent = theme["accent"]

    if glass:
        # 只铺一层薄色，底下留给系统模糊
        a = max(0, min(230, int(cfg.get("glass", 90))))
        base = (26, 26, 26, a) if dark else (242, 242, 242, a)
    else:
        base = (25, 28, 33, 255) if dark else (255, 255, 255, 255)

    fg = (255, 255, 255, 245) if dark else (16, 18, 22, 240)
    fg2 = (255, 255, 255, 130) if dark else (16, 18, 22, 122)

    # 卡片形状：画大 SS 倍再缩回来，圆角自带抗锯齿
    big_im = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    ImageDraw.Draw(big_im).rounded_rectangle(
        (0, 0, W * SS - 1, H * SS - 1), radius=r * SS, fill=base)
    im = big_im.resize((W, H), Image.LANCZOS)

    # ---- 先量文字，再决定图片能占多宽 ----
    path = os.path.join(IMGDIR, cfg.get("img_file") or "")
    has_img = bool(cfg.get("img_file")) and os.path.exists(path)

    probe = ImageDraw.Draw(im)
    big, sec, cap, money, parts = compute(cfg)
    single = has_img
    pad = int(18 * scale)

    # 字号跟着卡片高度走，否则卡片拉高之后字还是那么小，比例失衡
    avail = H / scale - 24
    if single:
        big_sz = max(22, min(44, avail * 0.36))
        mon_sz = big_sz * 0.52
    else:
        big_sz = max(26, min(52, avail * 0.45))
        mon_sz = big_sz * 0.60
    if has_cjk(big):
        big_sz *= 0.72        # 中文方块字比窄体数字宽得多，压一点才放得下
    cap_sz = max(10, min(15, big_sz * 0.30))
    f_big = (F_TXT if has_cjk(big) else F_NUM)(int(big_sz * scale))
    f_sec = F_NUM(int(big_sz * 0.45 * scale))
    f_cap = F_TXT(int(cap_sz * scale))
    f_mon = F_MON(int(mon_sz * scale))

    bw = 0
    if has_img:
        need = max(
            probe.textlength(big, font=f_big)
            + (probe.textlength(" " + sec, font=f_sec) if sec else 0),
            probe.textlength(cap, font=f_cap),
            probe.textlength("¥" + money, font=f_mon),
            # 倒计时按两项一行排，取最宽的一对
            max([probe.textlength(" · ".join(parts[i:i + 2]), font=f_cap)
                 for i in range(0, len(parts), 2)] or [0]),
        )
        room_for_img = W - int(need) - pad - int(10 * scale)
        if cfg.get("iw_auto", True):
            # cover 只有在"条的宽高比 == 图片比例"时才不裁，
            # 所以直接把宽度算成 卡片高 × 图片比例，图片刚好完整填满这条。
            try:
                with Image.open(path) as _p:
                    ideal = int(H * _p.width / _p.height)
            except Exception:
                ideal = int(W * 0.5)
            bw = max(int(W * 0.2), min(int(W * 0.72), room_for_img, ideal))
        else:
            bw = int(W * max(20, min(70, int(cfg.get("iw", 45)))) / 100)
            bw = min(bw, max(int(W * 0.2), room_for_img))

    if has_img:
        try:
            band = prep_image(path, bw, H, cfg)
            # 左缘渐变，不然是条生硬的直边
            fade = max(0, min(100, int(cfg.get("fade", 45)))) / 100
            if fade > 0:
                grad = Image.new("L", (bw, 1))
                span = max(1, bw * fade)
                for x in range(bw):
                    grad.putpixel((x, 0), int(255 * min(1.0, x / span)))
                band.putalpha(ImageChops.multiply(band.split()[3],
                                                  grad.resize((bw, H))))

            layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            layer.paste(band, (W - bw, 0), band)
            layer.putalpha(ImageChops.multiply(layer.split()[3], im.split()[3]))
            im = Image.alpha_composite(im, layer)
        except Exception as e:
            print("image failed:", e)
            has_img = False

    # 轻微颗粒，纯色块看着太平
    noise = Image.effect_noise((W, H), 28).convert("L")
    nl = Image.merge("RGBA", (noise, noise, noise, Image.new("L", (W, H), 12)))
    nl.putalpha(ImageChops.multiply(nl.split()[3], im.split()[3]))
    im = Image.alpha_composite(im, nl)

    d = ImageDraw.Draw(im)
    # 文字区右边界：有图时不能压到图片上
    text_right = (W - bw - int(6 * scale)) if single else (W - pad)
    avail_w = max(30, text_right - pad)

    def shrink(text, size, maxw, floor=8):
        """先缩字号再说，尽量不出现省略号"""
        sz = size
        while sz > floor:
            f = F_TXT(int(sz))
            if d.textlength(text, font=f) <= maxw:
                return f
            sz -= 1
        return F_TXT(int(floor))

    def wrap(items, font, maxw):
        """贪心折行：能塞进一行就继续塞，塞不下另起一行"""
        lines, cur = [], ""
        for it in items:
            cand = (cur + " · " + it) if cur else it
            if cur and d.textlength(cand, font=font) > maxw:
                lines.append(cur)
                cur = it
            else:
                cur = cand
        if cur:
            lines.append(cur)
        return lines

    # 两列模式右下角可用宽度受时间文字挤压
    # 无图时右侧那块最多占一半多一点，否则「已下班」这种短文字会让它一路铺到左边
    room = avail_w if single else max(
        60, min(int(W * 0.52),
                W - pad * 2 - int(d.textlength(big, font=f_big)) - int(14 * scale)))
    f_capr = f_cap
    slot_lines = wrap(parts, f_capr, room) if parts else []
    while slot_lines and any(d.textlength(l, font=f_capr) > room for l in slot_lines) \
            and f_capr.size > 8:
        f_capr = F_TXT(f_capr.size - 1)
        slot_lines = wrap(parts, f_capr, room)

    # 行距跟着相邻行里较大的那个字号走。之前一律按说明文字的 11px 算，
    # 结果 30px 的大字下面只留 6px，挤成一团。
    g_big = int(f_big.size * 0.34)          # 大字与下方说明之间
    g_mid = int(f_mon.size * 0.42)          # 说明与金额之间

    def measure(fc, lines):
        sg = max(2, int(fc.size * 0.5))     # 倒计时各行之间
        if single:
            rows = f_big.size + f_cap.size + f_mon.size + fc.size * len(lines)
            gaps = g_big + g_mid + (int(fc.size * 0.9) if lines else 0) \
                   + sg * max(0, len(lines) - 1)
        else:
            rows = f_big.size + f_cap.size
            gaps = g_big
        return rows + gaps, g_big, sg

    guard = int(10 * scale) * 2
    block, line_gap, slot_gap = measure(f_capr, slot_lines)
    # 切换图片会重算卡片高度，可能一下子变矮，文字得跟着收，否则会画到卡片外面
    while block > H - guard and f_capr.size > 8:
        f_capr = F_TXT(f_capr.size - 1)
        slot_lines = wrap(parts, f_capr, room)
        block, line_gap, slot_gap = measure(f_capr, slot_lines)
    while slot_lines and block > H - guard:      # 缩到底还放不下就少显示几行
        slot_lines.pop()
        block, line_gap, slot_gap = measure(f_capr, slot_lines)

    if single:
        y = max(int(10 * scale), (H - block) // 2)

        d.text((pad, y), big, font=f_big, fill=fg)
        bw_ = d.textlength(big, font=f_big)
        if sec:
            d.text((pad + bw_ + int(6 * scale), y + int((f_big.size - f_sec.size) * .75)),
                   sec, font=f_sec, fill=fg2)
        y += f_big.size + g_big

        f_capl = f_cap if d.textlength(cap, font=f_cap) <= avail_w \
            else shrink(cap, f_cap.size, avail_w)
        d.text((pad, y), cap, font=f_capl, fill=fg2)

        y += f_cap.size + g_mid
        d.text((pad, y), "¥" + money, font=f_mon, fill=accent + (255,))
        y += f_mon.size + int(f_capr.size * 0.9)
        for i, line in enumerate(slot_lines):
            d.text((pad, y + i * (f_capr.size + slot_gap)), line, font=f_capr, fill=fg2)
    else:
        # 两行两列：第一行主字与金额底部对齐，第二行说明与倒计时顶部对齐。
        # 之前左右两块各自垂直居中，两边基线对不上，看着是歪的。
        slot_h = (len(slot_lines) * f_capr.size
                  + slot_gap * max(0, len(slot_lines) - 1)) if slot_lines else 0
        r1 = max(f_big.size, f_mon.size)
        r2 = max(f_cap.size, slot_h)
        blk2 = r1 + g_big + r2
        y = max(int(10 * scale), (H - blk2) // 2)

        base = y + r1                       # 第一行的公共底线
        d.text((pad, base - f_big.size), big, font=f_big, fill=fg)
        bw_ = d.textlength(big, font=f_big)
        if sec:
            d.text((pad + bw_ + int(6 * scale), base - f_sec.size - int(f_big.size * .08)),
                   sec, font=f_sec, fill=fg2)
        mw = d.textlength("¥" + money, font=f_mon)
        d.text((W - pad - mw, base - f_mon.size), "¥" + money,
               font=f_mon, fill=accent + (255,))

        y2 = base + g_big                   # 第二行的公共顶线
        lim = int(W * 0.42)
        f_capl = f_cap if d.textlength(cap, font=f_cap) <= lim \
            else shrink(cap, f_cap.size, lim)
        d.text((pad, y2), cap, font=f_capl, fill=fg2)
        for i, line in enumerate(slot_lines):
            cw = d.textlength(line, font=f_capr)
            d.text((W - pad - cw, y2 + i * (f_capr.size + slot_gap)),
                   line, font=f_capr, fill=fg2)

    # ---- 设置入口：鼠标悬停时才浮现 ----
    if cfg.get("_hover"):
        cx, cy = W - int(16 * scale), int(14 * scale)
        rr = max(1, int(1.6 * scale))
        for k in (-1, 0, 1):
            ox = cx + k * int(6 * scale)
            d.ellipse((ox - rr, cy - rr, ox + rr, cy + rr), fill=fg2)

    return im


def prep_image(path, bw, bh, cfg):
    """铺满目标框并居中裁切，偏移量用来挑选裁哪一块"""
    src = Image.open(path).convert("RGBA")
    ratio = max(bw / src.width, bh / src.height)
    src = src.resize((max(1, int(src.width * ratio)),
                      max(1, int(src.height * ratio))), Image.LANCZOS)
    out = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    out.paste(src, ((bw - src.width) // 2 + int(cfg.get("ix", 0)),
                    (bh - src.height) // 2 + int(cfg.get("iy", 0))), src)
    return out


# ============================ 分层窗口 ============================
WS_EX_LAYERED, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_EX_NOACTIVATE = 0x80000, 0x80, 8, 0x8000000
WS_POPUP = 0x80000000
WS_EX_APPWINDOW = 0x40000
GWL_EXSTYLE = -20
ULW_ALPHA = 2
AC_SRC_OVER, AC_SRC_ALPHA = 0, 1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_NOZORDER = 1, 2, 0x10, 4
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SW_HIDE, SW_SHOW = 0, 5
WM_DESTROY, WM_LBUTTONDOWN, WM_RBUTTONUP, WM_TIMER, WM_NCLBUTTONDOWN = 2, 0x201, 0x205, 0x113, 0xA1
WM_LBUTTONDBLCLK = 0x203
WM_MOUSEMOVE, WM_MOUSELEAVE = 0x200, 0x2A3
CS_DBLCLKS = 8
TME_LEAVE = 2


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("dwFlags", wt.DWORD),
                ("hwndTrack", wt.HWND), ("dwHoverTime", wt.DWORD)]
HTCAPTION = 2


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class ACCENTPOLICY(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]


class WINCOMPATTRDATA(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.POINTER(ACCENTPOLICY)),
                ("SizeOfData", ctypes.c_size_t)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wt.HWND, wt.UINT,
                             ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8
                             else ctypes.c_ulong,
                             ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8
                             else ctypes.c_long)


class WNDCLASSEX(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HANDLE), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]


# 64 位下必须把 argtypes 也声明齐：ctypes 默认按 C int 传参，
# 句柄和 LPARAM 超过 32 位就会 OverflowError: int too long to convert。
LRESULT = ctypes.c_longlong
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

def _sig(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn

_sig(user32, "CreateWindowExW", wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
     ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
     wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID)
_sig(user32, "DefWindowProcW", LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_sig(user32, "SendMessageW", LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_sig(user32, "PostMessageW", wt.BOOL, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_sig(user32, "RegisterClassExW", wt.ATOM, ctypes.c_void_p)
_sig(user32, "LoadCursorW", wt.HANDLE, wt.HINSTANCE, wt.LPCWSTR)
_sig(user32, "ShowWindow", wt.BOOL, wt.HWND, ctypes.c_int)
_sig(user32, "SetWindowPos", wt.BOOL, wt.HWND, wt.HWND,
     ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT)
_sig(user32, "GetWindowRect", wt.BOOL, wt.HWND, ctypes.POINTER(wt.RECT))
_sig(user32, "SystemParametersInfoW", wt.BOOL, wt.UINT, wt.UINT, ctypes.c_void_p, wt.UINT)
_sig(user32, "SetTimer", ULONG_PTR, wt.HWND, ULONG_PTR, wt.UINT, wt.LPVOID)
_sig(user32, "GetDC", wt.HDC, wt.HWND)
_sig(user32, "ReleaseDC", ctypes.c_int, wt.HWND, wt.HDC)
_sig(user32, "ReleaseCapture", wt.BOOL)
_sig(user32, "TrackMouseEvent", wt.BOOL, ctypes.c_void_p)
_sig(user32, "GetWindowLongPtrW", ctypes.c_longlong, wt.HWND, ctypes.c_int)
_sig(user32, "SetWindowLongPtrW", ctypes.c_longlong, wt.HWND, ctypes.c_int,
     ctypes.c_longlong)
_sig(user32, "PostQuitMessage", None, ctypes.c_int)
_sig(user32, "UpdateLayeredWindow", wt.BOOL, wt.HWND, wt.HDC,
     ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE), wt.HDC,
     ctypes.POINTER(wt.POINT), wt.DWORD, ctypes.c_void_p, wt.DWORD)
_sig(user32, "GetMessageW", wt.BOOL, ctypes.c_void_p, wt.HWND, wt.UINT, wt.UINT)
_sig(user32, "TranslateMessage", wt.BOOL, ctypes.c_void_p)
_sig(user32, "DispatchMessageW", LRESULT, ctypes.c_void_p)

_sig(gdi32, "CreateCompatibleDC", wt.HDC, wt.HDC)
_sig(gdi32, "CreateDIBSection", wt.HBITMAP, wt.HDC, ctypes.c_void_p, wt.UINT,
     ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD)
_sig(gdi32, "SelectObject", wt.HANDLE, wt.HDC, wt.HANDLE)
_sig(gdi32, "DeleteObject", wt.BOOL, wt.HANDLE)
_sig(gdi32, "DeleteDC", wt.BOOL, wt.HDC)

try:
    _sig(user32, "SetWindowCompositionAttribute", wt.BOOL, wt.HWND, ctypes.c_void_p)
except AttributeError:
    pass


class Widget:
    def __init__(self):
        self.cfg = read_cfg()
        self.theme = system_theme()
        self.hwnd = None
        self.size = (0, 0)
        self.hover = False
        self._proc = WNDPROC(self.wndproc)

    # ---------- 窗口 ----------
    def create(self):
        hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
        cls = WNDCLASSEX()
        cls.cbSize = ctypes.sizeof(WNDCLASSEX)
        cls.lpfnWndProc = self._proc
        cls.hInstance = hinst
        cls.hCursor = user32.LoadCursorW(None, ctypes.cast(32512, wt.LPCWSTR))
        cls.style = CS_DBLCLKS          # 不设这个收不到双击消息
        cls.lpszClassName = "OffworkWidgetCls"
        user32.RegisterClassExW(ctypes.cast(ctypes.byref(cls), ctypes.c_void_p))

        x = self.cfg.get("wx"); y = self.cfg.get("wy")
        self.hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
            "OffworkWidgetCls", "下班倒计时", WS_POPUP,
            int(x) if x is not None else 200, int(y) if y is not None else 200,
            320, 104, None, None, hinst, None)
        user32.ShowWindow(self.hwnd, SW_SHOW)
        self.apply_blur()
        self.set_topmost(bool(self.cfg.get("top", True)))
        user32.SetTimer(self.hwnd, 1, 1000, None)
        self.paint()
        if self.cfg.get("alttab"):
            self.apply_alttab()

    def wndproc(self, hwnd, msg, wp, lp):
        if msg == WM_TIMER:
            self.paint()
            return 0
        if msg == WM_MOUSEMOVE:
            if not self.hover:
                self.hover = True
                tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT),
                                      TME_LEAVE, hwnd, 0)
                user32.TrackMouseEvent(ctypes.cast(ctypes.byref(tme), ctypes.c_void_p))
                self.paint()
            return 0
        if msg == WM_MOUSELEAVE:
            self.hover = False
            self.paint()
            return 0
        if msg == WM_LBUTTONDOWN:
            x = ctypes.c_short(lp & 0xFFFF).value           # lParam 低位是 x，高位是 y
            y = ctypes.c_short((lp >> 16) & 0xFFFF).value
            w, h = self.size
            if x >= w - int(34 * self.uiscale()) and y <= int(30 * self.uiscale()):
                open_settings(self)                          # 点右上角那三个点
                return 0
            user32.ReleaseCapture()                          # 其余位置拖动窗口
            user32.SendMessageW(hwnd, WM_NCLBUTTONDOWN, HTCAPTION, 0)
            self.save_pos()
            return 0
        if msg == WM_RBUTTONUP or msg == WM_LBUTTONDBLCLK:
            open_settings(self)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    # ---------- 绘制 ----------
    def paint(self):
        cfg = dict(self.cfg)
        cfg["_hover"] = self.hover
        im = render(cfg, self.theme)
        w, h = im.size

        # UpdateLayeredWindow 要预乘 alpha 的 BGRA
        r, g, b, a = im.split()
        src = Image.merge("RGBA", (ImageChops.multiply(b, a),
                                   ImageChops.multiply(g, a),
                                   ImageChops.multiply(r, a), a)).tobytes()

        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h            # 负数 = 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(mem_dc, ctypes.byref(bmi), 0,
                                      ctypes.byref(bits), None, 0)
        ctypes.memmove(bits, src, len(src))
        old = gdi32.SelectObject(mem_dc, hbmp)

        size = wt.SIZE(w, h)
        src_pt = wt.POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(self.hwnd, screen_dc, None, ctypes.byref(size),
                                   mem_dc, ctypes.byref(src_pt), 0,
                                   ctypes.cast(ctypes.byref(blend), ctypes.c_void_p), ULW_ALPHA)

        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)
        self.size = (w, h)
        self.nudge_onscreen()

    def apply_alttab(self):
        """WS_EX_TOOLWINDOW 会让窗口从 Alt+Tab 里消失，这是小组件的常规做法，
        但想用 Alt+Tab 找回窗口时就得把它摘掉，换成 WS_EX_APPWINDOW。"""
        show = bool(read_cfg().get("alttab", False))
        ex = user32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        if show:
            ex = (ex & ~WS_EX_TOOLWINDOW & ~WS_EX_NOACTIVATE) | WS_EX_APPWINDOW
        else:
            ex = (ex & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        user32.ShowWindow(self.hwnd, SW_HIDE)          # 样式要隐藏再显示才生效
        user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, ex)
        user32.ShowWindow(self.hwnd, SW_SHOW)
        self.paint()
        self.set_topmost(bool(self.cfg.get("top", True)))

    def center(self):
        """窗口拖丢了用这个找回来"""
        wa = wt.RECT()
        user32.SystemParametersInfoW(0x0030, 0,
                                     ctypes.cast(ctypes.byref(wa), ctypes.c_void_p), 0)
        w, h = self.size
        x = wa.left + (wa.right - wa.left - w) // 2
        y = wa.top + (wa.bottom - wa.top - h) // 2
        user32.SetWindowPos(self.hwnd, None, x, y, 0, 0,
                            SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOZORDER)
        user32.ShowWindow(self.hwnd, SW_SHOW)
        self.save_pos()

    def uiscale(self):
        return max(0.75, min(1.5, float(self.cfg.get("ui", 100)) / 100))

    def apply_blur(self):
        """毛玻璃模式才开系统模糊；纯色模式靠逐像素 alpha，圆角完全抗锯齿"""
        glass = self.cfg.get("bg") == "glass"
        policy = ACCENTPOLICY(3 if glass else 0, 2, 0, 0)   # 3 = BLURBEHIND
        data = WINCOMPATTRDATA(19, ctypes.pointer(policy), ctypes.sizeof(policy))
        try:
            user32.SetWindowCompositionAttribute(
                self.hwnd, ctypes.cast(ctypes.byref(data), ctypes.c_void_p))
        except Exception:
            pass

    def set_topmost(self, on):
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST,
                            0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)

    def save_pos(self):
        r = wt.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        write_cfg({"wx": r.left, "wy": r.top})

    def reload(self):
        self.cfg = read_cfg()
        self.theme = system_theme()
        self.apply_blur()
        self.set_topmost(bool(self.cfg.get("top", True)))
        self.paint()

    def nudge_onscreen(self):
        """卡片变高后可能戳出屏幕，挪回来"""
        r = wt.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        wa = wt.RECT()
        user32.SystemParametersInfoW(0x0030, 0,
                                     ctypes.cast(ctypes.byref(wa), ctypes.c_void_p), 0)
        w, h = self.size
        nx = max(wa.left, min(r.left, wa.right - w))
        ny = max(wa.top, min(r.top, wa.bottom - h))
        if (nx, ny) != (r.left, r.top):
            user32.SetWindowPos(self.hwnd, None, nx, ny, 0, 0,
                                SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOZORDER)

    def show(self, v):
        user32.ShowWindow(self.hwnd, SW_SHOW if v else SW_HIDE)

    def quit(self):
        self.save_pos()
        user32.PostMessageW(self.hwnd, WM_DESTROY, 0, 0)


# ============================ 设置窗口 ============================
_settings_open = threading.Event()


def auto_fit(cfg):
    """按图片实际比例反推卡片高度，占宽超限时反向压缩占宽"""
    path = os.path.join(IMGDIR, cfg.get("img_file") or "")
    if not cfg.get("img_file") or not os.path.exists(path):
        return cfg
    try:
        with Image.open(path) as im:
            ratio = im.width / im.height
    except Exception:
        return cfg
    W, H_MIN, H_MAX, IW_MIN, IW_MAX = 320, 90, 280, 20, 70
    iw = max(IW_MIN, min(IW_MAX, int(cfg.get("iw", 45))))
    h = (W * iw / 100) / ratio
    if h > H_MAX:
        h = H_MAX
        iw = max(IW_MIN, min(IW_MAX, round(h * ratio / W * 100)))
        h = (W * iw / 100) / ratio
    # 倒计时占几行也要算进去，否则换成宽幅图后卡片变矮，文字被挤出去
    n_lines = (len(cfg.get("slots", [])) + 1) // 2
    need = 96 + 18 * max(0, n_lines - 1)
    cfg["iw"] = iw
    cfg["cardh"] = int(max(H_MIN, need, min(H_MAX, round(h))))
    cfg["ix"] = cfg["iy"] = 0
    return cfg


def open_settings(widget):
    if _settings_open.is_set():
        return                       # 已经开着就不重复弹
    _settings_open.set()

    def run():
        import traceback
        try:
            _build(widget)
        except Exception:
            traceback.print_exc()
        finally:
            _settings_open.clear()

    threading.Thread(target=run, daemon=True).start()


def _build(widget):
    import tkinter as tk
    from tkinter import ttk, filedialog

    cfg = read_cfg()
    root = tk.Tk()
    root.title("下班倒计时 · 设置")
    root.resizable(False, False)
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass

    dirty = {}

    def commit(delay=250):
        """改动攒一下再落盘重绘，拖滑块时才不会卡"""
        if getattr(commit, "job", None):
            root.after_cancel(commit.job)
        commit.job = root.after(delay, _flush)

    def _flush():
        commit.job = None
        if dirty:
            write_cfg(dirty)
            dirty.clear()
        widget.reload()

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=10, pady=10)

    # ---------- 通用控件 ----------
    def limiter(n):
        return (root.register(lambda P: len(P) <= n), "%P")

    def add_entry(parent, r, label, key, width=14, cast=str, maxlen=20):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=4)
        v = tk.StringVar(value=str(cfg.get(key, ""))[:maxlen])
        e = ttk.Entry(parent, textvariable=v, width=width,
                      validate="key", validatecommand=limiter(maxlen))
        e.grid(row=r, column=1, columnspan=2, sticky="we", pady=4)

        def on(*_):
            val = v.get()
            if cast is float:
                try:
                    val = float(val)
                except ValueError:
                    return
            dirty[key] = val
            commit(500)
        v.trace_add("write", on)
        return v

    def add_scale(parent, r, label, key, lo, hi, default):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=4)
        v = tk.DoubleVar(value=float(cfg.get(key, default)))
        out = ttk.Label(parent, width=4, text=str(int(v.get())))
        sc = ttk.Scale(parent, from_=lo, to=hi, variable=v, orient="horizontal", length=170)
        sc.grid(row=r, column=1, sticky="we", pady=4)
        out.grid(row=r, column=2, sticky="e")

        def on(*_):
            n = int(float(v.get()))
            out.config(text=str(n))
            dirty[key] = n
            commit()
        v.trace_add("write", on)
        return v

    def add_check(parent, r, label, key, default, cb=None):
        v = tk.BooleanVar(value=bool(cfg.get(key, default)))
        ttk.Checkbutton(parent, text=label, variable=v,
                        command=lambda: (dirty.__setitem__(key, v.get()),
                                         cb() if cb else None, commit(0))
                        ).grid(row=r, column=0, columnspan=3, sticky="w", pady=3)
        return v

    # ================= 工作 =================
    t1 = ttk.Frame(nb, padding=12)
    nb.add(t1, text="工作")
    add_entry(t1, 0, "上班时间", "start", maxlen=5)
    add_entry(t1, 1, "下班时间", "end", maxlen=5)
    add_entry(t1, 2, "月薪", "salary", cast=float, maxlen=9)
    add_entry(t1, 3, "上班文案", "work_text", maxlen=12)
    ttk.Label(t1, text="留空用「距下班」，可打 emoji",
              foreground="#888").grid(row=4, column=0, columnspan=3, sticky="w")

    # ================= 外观 =================
    t2 = ttk.Frame(nb, padding=12)
    nb.add(t2, text="外观")
    ttk.Label(t2, text="背景").grid(row=0, column=0, sticky="w", pady=4)
    BG = {"跟随系统深浅": "auto", "纯白": "light", "纯黑": "dark", "毛玻璃": "glass"}
    bg_rev = {v: k for k, v in BG.items()}
    bgv = tk.StringVar(value=bg_rev.get(cfg.get("bg", "auto"), "跟随系统深浅"))
    cb = ttk.Combobox(t2, textvariable=bgv, width=16, state="readonly",
                      values=list(BG.keys()))
    cb.grid(row=0, column=1, columnspan=2, sticky="we", pady=4)

    def on_bg(*_):
        dirty["bg"] = BG[bgv.get()]
        glass_row(BG[bgv.get()] == "glass")
        commit(0)
    bgv.trace_add("write", on_bg)

    add_scale(t2, 1, "整体缩放", "ui", 75, 150, 100)
    add_scale(t2, 2, "卡片高度", "cardh", 90, 280, 104)
    gl = add_scale(t2, 3, "玻璃浓度", "glass", 0, 230, 90)

    def glass_row(show):
        for w in t2.grid_slaves(row=3):
            w.grid_remove() if not show else w.grid()
    glass_row(cfg.get("bg") == "glass")

    add_check(t2, 4, "窗口置顶", "top", True)
    alt_v = tk.BooleanVar(value=bool(cfg.get("alttab", False)))
    ttk.Checkbutton(t2, text="在 Alt+Tab 中显示", variable=alt_v,
                    command=lambda: (write_cfg({"alttab": alt_v.get()}),
                                     widget.apply_alttab())
                    ).grid(row=6, column=0, columnspan=3, sticky="w", pady=3)
    ttk.Button(t2, text="回到屏幕中央", command=widget.center
               ).grid(row=7, column=0, columnspan=3, sticky="we", pady=(8, 0))

    auto_v = tk.BooleanVar(value=os.path.exists(STARTUP_VBS))
    ttk.Checkbutton(t2, text="开机自启", variable=auto_v,
                    command=lambda: set_autostart(auto_v.get())
                    ).grid(row=8, column=0, columnspan=3, sticky="w", pady=3)

    # ================= 图片 =================
    t3 = ttk.Frame(nb, padding=12)
    nb.add(t3, text="图片")
    lbl = ttk.Label(t3, text="", foreground="#888")
    lbl.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))

    def refresh_img_label():
        f = read_cfg().get("img_file")
        lbl.config(text=("当前：" + f) if f else "未导入图片")

    def pick():
        p = filedialog.askopenfilename(
            parent=root,
            filetypes=[("图片", "*.png *.jpg *.jpeg *.gif *.webp *.bmp")])
        if not p:
            return
        for old in os.listdir(IMGDIR):
            try:
                os.remove(os.path.join(IMGDIR, old))
            except OSError:
                pass
        name = "bg" + os.path.splitext(p)[1].lower()
        shutil.copyfile(p, os.path.join(IMGDIR, name))
        c = read_cfg()
        c["img_file"] = name
        write_cfg(auto_fit(c))          # 导入后按比例自动调好卡片
        refresh_img_label()
        widget.reload()

    def clear():
        for old in os.listdir(IMGDIR):
            try:
                os.remove(os.path.join(IMGDIR, old))
            except OSError:
                pass
        write_cfg({"img_file": "", "cardh": 104})
        refresh_img_label()
        widget.reload()

    def refit():
        write_cfg(auto_fit(read_cfg()))
        widget.reload()

    bar = ttk.Frame(t3)
    bar.grid(row=1, column=0, columnspan=3, sticky="we")
    ttk.Button(bar, text="导入", command=pick, width=8).grid(row=0, column=0, padx=2)
    ttk.Button(bar, text="移除", command=clear, width=8).grid(row=0, column=1, padx=2)
    ttk.Button(bar, text="按比例调整", command=refit, width=11).grid(row=0, column=2, padx=2)

    iwa = tk.BooleanVar(value=bool(cfg.get("iw_auto", True)))
    ttk.Checkbutton(t3, text="自动占宽（文字用多少留多少，其余给图片）",
                    variable=iwa,
                    command=lambda: (write_cfg({"iw_auto": iwa.get()}), widget.reload())
                    ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 2))
    add_scale(t3, 3, "手动占宽 %", "iw", 20, 70, 45)
    add_scale(t3, 4, "水平偏移", "ix", -300, 300, 0)
    add_scale(t3, 5, "垂直偏移", "iy", -300, 300, 0)
    add_scale(t3, 6, "淡出", "fade", 0, 100, 45)
    ttk.Label(t3, text="图片铺满右侧并裁切，偏移用来挑裁哪一块",
              foreground="#888").grid(row=7, column=0, columnspan=3,
                                      sticky="w", pady=(6, 0))
    refresh_img_label()

    # ================= 倒计时 =================
    t4 = ttk.Frame(nb, padding=12)
    nb.add(t4, text="倒计时")
    holder = ttk.Frame(t4)
    holder.grid(row=0, column=0, sticky="we")
    TYPES = {"每周": "weekly", "每月": "monthly", "指定日": "date"}
    trev = {v: k for k, v in TYPES.items()}
    WEEK = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]

    def save_slots(slots):
        write_cfg({"slots": slots})
        widget.reload()

    def draw_slots():
        for w in holder.winfo_children():
            w.destroy()
        slots = read_cfg().get("slots", [])
        for i, sl in enumerate(slots):
            lv = tk.StringVar(value=sl.get("label", ""))
            tv = tk.StringVar(value=trev.get(sl.get("type"), "每周"))
            vv = tk.StringVar(value=str(sl.get("value", "")))

            ttk.Entry(holder, textvariable=lv, width=7, validate="key",
                      validatecommand=limiter(5)).grid(row=i, column=0, padx=1, pady=2)
            ttk.Combobox(holder, textvariable=tv, width=6, state="readonly",
                         values=list(TYPES.keys())).grid(row=i, column=1, padx=1)
            if sl.get("type") == "weekly":
                ent = ttk.Combobox(holder, textvariable=vv, width=6, state="readonly",
                                   values=WEEK)
                vv.set(WEEK[int(sl.get("value", 5)) % 7])
            else:
                ent = ttk.Entry(holder, textvariable=vv, width=11, validate="key",
                                validatecommand=limiter(10))
            ent.grid(row=i, column=2, padx=1)

            def mk(idx, lv=lv, tv=tv, vv=vv):
                def on(*_):
                    s = read_cfg().get("slots", [])
                    if idx >= len(s):
                        return
                    old_type = s[idx].get("type")
                    s[idx]["label"] = lv.get()
                    s[idx]["type"] = TYPES[tv.get()]
                    if s[idx]["type"] != old_type:      # 类型换了，值给个合理默认
                        s[idx]["value"] = {"weekly": "5", "monthly": "10"}.get(
                            s[idx]["type"], date.today().isoformat())
                        save_slots(s)
                        root.after(10, draw_slots)
                        return
                    v = vv.get()
                    if s[idx]["type"] == "weekly":
                        v = str(WEEK.index(v)) if v in WEEK else "5"
                    elif s[idx]["type"] == "monthly":
                        try:
                            v = str(max(1, min(28, int(v))))
                        except ValueError:
                            return
                    s[idx]["value"] = v
                    save_slots(s)
                return on
            for var in (lv, tv, vv):
                var.trace_add("write", mk(i))

            def rm(idx=i):
                s = read_cfg().get("slots", [])
                if idx < len(s):
                    s.pop(idx)
                    save_slots(s)
                    draw_slots()
            ttk.Button(holder, text="×", width=2, command=rm).grid(row=i, column=3, padx=1)

            if sl.get("type") == "date":
                yv = tk.BooleanVar(value=bool(sl.get("yearly")))

                def toggle(idx=i, yv=yv):
                    ss = read_cfg().get("slots", [])
                    if idx < len(ss):
                        ss[idx]["yearly"] = yv.get()
                        save_slots(ss)
                        root.after(10, draw_slots)
                ttk.Checkbutton(holder, text="每年", variable=yv,
                                command=toggle).grid(row=i, column=4, padx=(4, 0))
                # 过期的项在卡片上不显示，这里得说清楚，否则用户不知道为什么没了
                dd = days_date(datetime.now(), sl.get("value", ""), yv.get())
                if dd is not None and dd < 0:
                    ttk.Label(holder, text="已过期", foreground="#c33").grid(
                        row=i, column=5, padx=(4, 0))

    def add_slot():
        s = read_cfg().get("slots", [])
        if len(s) >= 6:
            return
        s.append({"label": "节日", "type": "date", "value": date.today().isoformat()})
        save_slots(s)
        draw_slots()

    ttk.Button(t4, text="+ 添加一项", command=add_slot).grid(row=1, column=0,
                                                             sticky="we", pady=(8, 0))
    ttk.Label(t4, text="最多 6 项，放不下自动换行。过期的不显示，勾「每年」可年年重复",
              foreground="#888").grid(row=2, column=0, sticky="w", pady=(6, 0))
    draw_slots()

    # ---------- 底部 ----------
    foot = ttk.Frame(root)
    foot.pack(fill="x", padx=10, pady=(0, 10))
    ttk.Button(foot, text="隐藏到托盘",
               command=lambda: widget.show(False)).pack(side="left")
    ttk.Button(foot, text="退出程序",
               command=lambda: (root.destroy(), widget.quit())).pack(side="left", padx=6)
    ttk.Button(foot, text="关闭", command=root.destroy).pack(side="right")

    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()


def set_autostart(on):
    if not on:
        try:
            os.remove(STARTUP_VBS)
        except OSError:
            pass
        return
    if FROZEN:
        inner = '""%s""' % sys.executable
    else:
        exe = sys.executable.replace("python.exe", "pythonw.exe")
        inner = '""%s"" ""%s""' % (exe, os.path.abspath(__file__))
    vbs = 'CreateObject("WScript.Shell").Run "%s", 0, False\r\n' % inner
    os.makedirs(os.path.dirname(STARTUP_VBS), exist_ok=True)
    for enc in ("mbcs", "utf-8"):
        try:
            with open(STARTUP_VBS, "w", encoding=enc) as f:
                f.write(vbs)
            return
        except (LookupError, UnicodeEncodeError):
            continue


# ============================ 托盘 ============================
def build_tray(widget):
    try:
        import pystray
        from PIL import ImageDraw as D2
    except ImportError:
        return
    try:
        icon_img = Image.open(os.path.join(RES, "app.ico"))
    except Exception:
        icon_img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        dd = D2.Draw(icon_img)
        dd.ellipse((4, 4, 60, 60), fill=(16, 21, 25, 255))
        dd.line((32, 32, 32, 15), fill=(231, 178, 63), width=5)
        dd.line((32, 32, 46, 38), fill=(231, 178, 63), width=5)
    icon = pystray.Icon(APP, icon_img, "下班倒计时", pystray.Menu(
        pystray.MenuItem("显示", lambda *_: widget.show(True), default=True),
        pystray.MenuItem("隐藏", lambda *_: widget.show(False)),
        pystray.MenuItem("回到屏幕中央", lambda *_: widget.center()),
        pystray.MenuItem("设置", lambda *_: open_settings(widget)),
        pystray.MenuItem("退出", lambda i, *_: (i.stop(), widget.quit())),
    ))
    icon.run()


def single_instance():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 51721))
        s.listen(1)
        globals()["_lock"] = s
        return True
    except OSError:
        return False


if __name__ == "__main__":
    if not single_instance():
        sys.exit(0)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

    w = Widget()
    w.create()
    threading.Thread(target=build_tray, args=(w,), daemon=True).start()

    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
