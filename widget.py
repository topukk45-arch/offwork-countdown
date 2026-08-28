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
    "img_file": "", "iw": 45, "ix": 0, "iy": 0,
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


# 数字用 Bahnschrift（Win10 自带的窄体），中文用微软雅黑
F_NUM = lambda s: load_font(["bahnschrift.ttf", "segoeui.ttf"], s)
F_TXT = lambda s: load_font(["msyh.ttc", "msyh.ttf", "segoeui.ttf"], s)
F_MON = lambda s: load_font(["consola.ttf", "cour.ttf", "segoeui.ttf"], s)


# ============================ 业务计算 ============================
def hm(s):
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def days_weekly(now, d):
    return (int(d) - now.weekday() - 1) % 7 if False else (int(d) - (now.weekday() + 1) % 7) % 7


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


def days_date(now, iso):
    try:
        return (datetime.strptime(iso, "%Y-%m-%d").date() - now.date()).days
    except Exception:
        return None


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
            d = days_date(now, sl.get("value", ""))
        if d is not None and d >= 0:
            parts.append("%s %d 天" % (sl.get("label", ""), d))
    return big, sec, cap, money, " · ".join(parts) or "今日收入"


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

    # 背景图：右侧一条，左缘用渐变蒙版淡出
    path = os.path.join(IMGDIR, cfg.get("img_file") or "")
    if cfg.get("img_file") and os.path.exists(path):
        try:
            bw = int(W * max(20, min(70, int(cfg.get("iw", 45)))) / 100)
            band = Image.open(path).convert("RGBA")
            ratio = max(bw / band.width, H / band.height)
            band = band.resize((max(1, int(band.width * ratio)),
                                max(1, int(band.height * ratio))), Image.LANCZOS)
            ox = (band.width - bw) // 2 - int(cfg.get("ix", 0))
            oy = (band.height - H) // 2 - int(cfg.get("iy", 0))
            band = band.crop((ox, oy, ox + bw, oy + H))

            grad = Image.new("L", (bw, 1))
            for x in range(bw):
                grad.putpixel((x, 0), int(255 * min(1.0, x / max(1, bw * 0.55))))
            mask = grad.resize((bw, H))
            band.putalpha(ImageChops.multiply(band.split()[3], mask))

            layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            layer.paste(band, (W - bw, 0), band)
            # 只在卡片形状内显示，别溢出圆角
            layer.putalpha(ImageChops.multiply(layer.split()[3], im.split()[3]))
            im = Image.alpha_composite(im, layer)
        except Exception as e:
            print("image failed:", e)

    d = ImageDraw.Draw(im)
    big, sec, cap, money, capr = compute(cfg)
    has_img = bool(cfg.get("img_file"))
    pad = int(18 * scale)

    if has_img:      # 有图时文字收窄成单列，给右边让位
        f_big = F_NUM(int(30 * scale)); f_sec = F_NUM(int(14 * scale))
        f_cap = F_TXT(int(11 * scale)); f_mon = F_MON(int(16 * scale))
    else:
        f_big = F_NUM(int(38 * scale)); f_sec = F_NUM(int(17 * scale))
        f_cap = F_TXT(int(11 * scale)); f_mon = F_MON(int(23 * scale))

    y = int(15 * scale)
    d.text((pad, y), big, font=f_big, fill=fg)
    bw_ = d.textlength(big, font=f_big)
    if sec:
        d.text((pad + bw_ + int(6 * scale), y + int((f_big.size - f_sec.size) * .75)),
               sec, font=f_sec, fill=fg2)
    y += int(f_big.size * 1.15)
    d.text((pad, y), cap, font=f_cap, fill=fg2)

    if has_img:
        y += int(f_cap.size * 1.9)
        d.text((pad, y), "¥" + money, font=f_mon, fill=accent + (255,))
        mw = d.textlength("¥" + money, font=f_mon)
        d.text((pad + mw + int(9 * scale), y + int(f_mon.size * .35)),
               capr, font=f_cap, fill=fg2)
    else:
        mw = d.textlength("¥" + money, font=f_mon)
        cw = d.textlength(capr, font=f_cap)
        d.text((W - pad - mw, int(15 * scale)), "¥" + money, font=f_mon, fill=accent + (255,))
        d.text((W - pad - cw, int(15 * scale) + int(f_mon.size * 1.35)),
               capr, font=f_cap, fill=fg2)
    return im


# ============================ 分层窗口 ============================
WS_EX_LAYERED, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_EX_NOACTIVATE = 0x80000, 0x80, 8, 0x8000000
WS_POPUP = 0x80000000
ULW_ALPHA = 2
AC_SRC_OVER, AC_SRC_ALPHA = 0, 1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_NOZORDER = 1, 2, 0x10, 4
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SW_HIDE, SW_SHOW = 0, 5
WM_DESTROY, WM_LBUTTONDOWN, WM_RBUTTONUP, WM_TIMER, WM_NCLBUTTONDOWN = 2, 0x201, 0x205, 0x113, 0xA1
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


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSEX(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HANDLE), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]


user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.DefWindowProcW.restype = ctypes.c_longlong
user32.GetDC.restype = wt.HDC
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.restype = wt.HBITMAP


class Widget:
    def __init__(self):
        self.cfg = read_cfg()
        self.theme = system_theme()
        self.hwnd = None
        self.size = (0, 0)
        self._proc = WNDPROC(self.wndproc)

    # ---------- 窗口 ----------
    def create(self):
        hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
        cls = WNDCLASSEX()
        cls.cbSize = ctypes.sizeof(WNDCLASSEX)
        cls.lpfnWndProc = self._proc
        cls.hInstance = hinst
        cls.hCursor = user32.LoadCursorW(None, ctypes.c_wchar_p(32512))
        cls.lpszClassName = "OffworkWidgetCls"
        user32.RegisterClassExW(ctypes.byref(cls))

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

    def wndproc(self, hwnd, msg, wp, lp):
        if msg == WM_TIMER:
            self.paint()
            return 0
        if msg == WM_LBUTTONDOWN:               # 按住卡片任意处拖动窗口
            user32.ReleaseCapture()
            user32.SendMessageW(hwnd, WM_NCLBUTTONDOWN, HTCAPTION, 0)
            self.save_pos()
            return 0
        if msg == WM_RBUTTONUP:
            open_settings(self)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    # ---------- 绘制 ----------
    def paint(self):
        im = render(self.cfg, self.theme)
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
                                   ctypes.byref(blend), ULW_ALPHA)

        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)
        self.size = (w, h)

    def apply_blur(self):
        """毛玻璃模式才开系统模糊；纯色模式靠逐像素 alpha，圆角完全抗锯齿"""
        glass = self.cfg.get("bg") == "glass"
        policy = ACCENTPOLICY(3 if glass else 0, 2, 0, 0)   # 3 = BLURBEHIND
        data = WINCOMPATTRDATA(19, ctypes.pointer(policy), ctypes.sizeof(policy))
        try:
            user32.SetWindowCompositionAttribute(self.hwnd, ctypes.byref(data))
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

    def show(self, v):
        user32.ShowWindow(self.hwnd, SW_SHOW if v else SW_HIDE)

    def quit(self):
        self.save_pos()
        user32.PostMessageW(self.hwnd, WM_DESTROY, 0, 0)


# ============================ 设置窗口 ============================
def open_settings(widget):
    def run():
        import tkinter as tk
        from tkinter import ttk, filedialog

        cfg = read_cfg()
        root = tk.Tk()
        root.title("下班倒计时 · 设置")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        frm = ttk.Frame(root, padding=14)
        frm.grid()

        vars_ = {}

        def row(i, label, var, width=16):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w", pady=3)
            e = ttk.Entry(frm, textvariable=var, width=width)
            e.grid(row=i, column=1, sticky="we", pady=3)
            return e

        for i, (key, label) in enumerate(
                [("start", "上班"), ("end", "下班"), ("salary", "月薪"),
                 ("work_text", "文案")]):
            vars_[key] = tk.StringVar(value=str(cfg.get(key, "")))
            row(i, label, vars_[key])

        ttk.Label(frm, text="背景").grid(row=4, column=0, sticky="w", pady=3)
        vars_["bg"] = tk.StringVar(value=cfg.get("bg", "auto"))
        ttk.Combobox(frm, textvariable=vars_["bg"], width=14, state="readonly",
                     values=["auto", "light", "dark", "glass"]).grid(row=4, column=1,
                                                                    sticky="we", pady=3)

        for i, (key, label, lo, hi) in enumerate(
                [("ui", "缩放 %", 75, 150), ("cardh", "卡片高", 90, 280),
                 ("iw", "图片占宽 %", 20, 70), ("glass", "玻璃浓度", 0, 230)], start=5):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w", pady=3)
            vars_[key] = tk.IntVar(value=int(cfg.get(key, lo)))
            ttk.Scale(frm, from_=lo, to=hi, variable=vars_[key],
                      orient="horizontal", length=150).grid(row=i, column=1,
                                                            sticky="we", pady=3)

        vars_["top"] = tk.BooleanVar(value=bool(cfg.get("top", True)))
        ttk.Checkbutton(frm, text="窗口置顶", variable=vars_["top"]).grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(8, 0))

        auto = tk.BooleanVar(value=os.path.exists(STARTUP_VBS))
        ttk.Checkbutton(frm, text="开机自启", variable=auto).grid(
            row=10, column=0, columnspan=2, sticky="w")

        def pick():
            p = filedialog.askopenfilename(
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
            write_cfg({"img_file": name})
            widget.reload()

        def clear():
            for old in os.listdir(IMGDIR):
                try:
                    os.remove(os.path.join(IMGDIR, old))
                except OSError:
                    pass
            write_cfg({"img_file": ""})
            widget.reload()

        bar = ttk.Frame(frm)
        bar.grid(row=11, column=0, columnspan=2, sticky="we", pady=(10, 0))
        ttk.Button(bar, text="导入图片", command=pick).grid(row=0, column=0, padx=2)
        ttk.Button(bar, text="移除图片", command=clear).grid(row=0, column=1, padx=2)

        def apply_():
            patch = {}
            for k, v in vars_.items():
                val = v.get()
                if k == "salary":
                    try:
                        val = float(val)
                    except ValueError:
                        val = 0
                elif k in ("ui", "cardh", "iw", "glass"):
                    val = int(float(val))
                patch[k] = val
            write_cfg(patch)
            set_autostart(auto.get())
            widget.reload()

        ttk.Button(bar, text="应用", command=apply_).grid(row=0, column=2, padx=2)
        ttk.Button(bar, text="关闭", command=root.destroy).grid(row=0, column=3, padx=2)
        root.mainloop()

    threading.Thread(target=run, daemon=True).start()


def set_autostart(on):
    if not on:
        try:
            os.remove(STARTUP_VBS)
        except OSError:
            pass
        return
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
    icon_img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    dd = D2.Draw(icon_img)
    dd.ellipse((4, 4, 60, 60), fill=(16, 21, 25, 255))
    dd.line((32, 32, 32, 15), fill=(231, 178, 63), width=5)
    dd.line((32, 32, 46, 38), fill=(231, 178, 63), width=5)
    icon = pystray.Icon(APP, icon_img, "下班倒计时", pystray.Menu(
        pystray.MenuItem("显示", lambda *_: widget.show(True), default=True),
        pystray.MenuItem("隐藏", lambda *_: widget.show(False)),
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
