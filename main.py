# -*- coding: utf-8 -*-
"""
下班倒计时 · Windows 桌面小组件
开发运行: python main.py        打包: build.bat
"""
import base64
import ctypes
import json
import os
import shutil
import socket
import sys
import threading
import time
from ctypes import wintypes

import webview

APP = "OffworkWidget"
TITLE = "下班倒计时"
FROZEN = getattr(sys, "frozen", False)
RES = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP)
CFG = os.path.join(DATA, "config.json")
IMGDIR = os.path.join(DATA, "images")
STARTUP_VBS = os.path.join(
    os.environ.get("APPDATA", ""),
    r"Microsoft\Windows\Start Menu\Programs\Startup", "OffworkWidget.vbs")
os.makedirs(IMGDIR, exist_ok=True)

CORNER_RADIUS = 18          # 硬边裁剪必有锯齿，但半径太小会直接看不出圆角，权衡下来 18 合适

win = None
tray = None
pos = {}
_lock = None
_hwnd = 0

res = lambda name: os.path.join(RES, name)


# ============ 配置 ============
def read_cfg():
    try:
        with open(CFG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_cfg(patch):
    cur = read_cfg()
    cur.update(patch)
    try:
        with open(CFG, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print("save failed:", e)


def img_to_dataurl(path):
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
    mime = "jpeg" if ext in ("jpg", "jpeg") else ext
    with open(path, "rb") as f:
        return f"data:image/{mime};base64," + base64.b64encode(f.read()).decode()


# ============ Windows 视觉效果 ============
class ACCENTPOLICY(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]


class WINCOMPATTRDATA(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.POINTER(ACCENTPOLICY)),
                ("SizeOfData", ctypes.c_size_t)]


ACCENT_OFF, ACCENT_GRADIENT, ACCENT_BLUR, ACCENT_ACRYLIC = 0, 1, 3, 4
WCA_ACCENT_POLICY = 19
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_QUIET = 0x0001 | 0x0002 | 0x0010
SW_HIDE, SW_SHOW = 0, 5


def get_hwnd():
    global _hwnd
    if _hwnd:
        return _hwnd
    try:
        _hwnd = int(win.native.Handle.ToInt64())
    except Exception:
        _hwnd = ctypes.windll.user32.FindWindowW(None, TITLE) or 0
    return _hwnd


def apply_blur(dark=True, acrylic=False, alpha=90, glass=True, color="#191c21"):
    """窗口背景。GradientColor 是 AABBGGRR，不是常见的 RGBA。

    纯色模式不能用 ACCENT_DISABLED —— pywebview 的窗口透明依赖这条合成链路，
    一关掉，WebView2 没画到的区域会露出默认白底。改用 ACCENT_ENABLE_GRADIENT
    让 DWM 直接填一层不透明纯色，链路不断，也就没有白块了。
    """
    hwnd = get_hwnd()
    if not hwnd:
        return False
    if glass:
        # Win10 的 ACRYLIC 只在窗口激活时才真模糊，失焦就退化成一块纯色。
        # 常驻置顶的小组件基本永远不是焦点窗口，所以默认用老式 BLURBEHIND，
        # 质感差一点但任何时候都在模糊。
        state = ACCENT_ACRYLIC if acrylic else ACCENT_BLUR
        a = max(0, min(255, int(alpha)))
        base = 0x1A1A1A if dark else 0xF2F2F2   # BBGGRR，灰色前后对称
    else:
        state = ACCENT_GRADIENT
        a = 255
        try:
            r, g, b = (int(color[i:i+2], 16) for i in (1, 3, 5))
        except Exception:
            r, g, b = 0x19, 0x1C, 0x21
        base = (b << 16) | (g << 8) | r
    tint = (a << 24) | base
    policy = ACCENTPOLICY(state, 2, tint, 0)
    data = WINCOMPATTRDATA(WCA_ACCENT_POLICY, ctypes.pointer(policy),
                           ctypes.sizeof(policy))
    try:
        ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
        return True
    except Exception:
        return False


GCL_STYLE = -26
CS_DROPSHADOW = 0x00020000


def add_shadow():
    """无边框窗口默认没有投影，补一个，玻璃才有浮起来的感觉"""
    hwnd = get_hwnd()
    if not hwnd:
        return
    u = ctypes.windll.user32
    get = getattr(u, "GetClassLongPtrW", None) or u.GetClassLongW
    put = getattr(u, "SetClassLongPtrW", None) or u.SetClassLongW
    try:
        put(hwnd, GCL_STYLE, get(hwnd, GCL_STYLE) | CS_DROPSHADOW)
    except Exception:
        pass


def round_corners():
    """毛玻璃是矩形的，得手动裁圆角。必须在窗口尺寸真正变完之后调。"""
    hwnd = get_hwnd()
    if not hwnd:
        return
    r = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return
    try:
        scale = ctypes.windll.user32.GetDpiForWindow(hwnd) / 96.0
    except Exception:
        scale = 1.0
    d = max(2, int(CORNER_RADIUS * 2 * scale))
    rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, d, d)
    ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def work_area():
    """当前窗口所在显示器的可用区域（扣掉任务栏），多屏也准"""
    hwnd = get_hwnd()
    mon = ctypes.windll.user32.MonitorFromWindow(hwnd, 2)   # NEAREST
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if ctypes.windll.user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        return mi.rcWork
    r = wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
    return r


def ensure_on_screen():
    """面板展开后窗口变高，可能戳出屏幕底部，往回挪"""
    hwnd = get_hwnd()
    if not hwnd:
        return
    wa = work_area()
    r = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    nx = max(wa.left, min(r.left, wa.right - w))
    ny = max(wa.top, min(r.top, wa.bottom - h))
    if (nx, ny) != (r.left, r.top):
        SWP_NOSIZE_NOZORDER_NOACTIVATE = 0x0001 | 0x0004 | 0x0010
        ctypes.windll.user32.SetWindowPos(
            hwnd, 0, nx, ny, 0, 0, SWP_NOSIZE_NOZORDER_NOACTIVATE)


def set_topmost(on):
    """不用 win.on_top —— 它对 GUI 线程做同步 Invoke，
    而 js_api 回调本身就在 GUI 线程上，会自己等自己造成死锁。
    但光调 SetWindowPos 也不够：WinForms 窗体的 TopMost 属性还是 true，
    窗口一激活就会把置顶加回来，所以属性本身也得改，放后台线程里做。"""
    hwnd = get_hwnd()
    if hwnd:
        ctypes.windll.user32.SetWindowPos(
            hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST, 0, 0, 0, 0, SWP_QUIET)

    def clear_prop():
        try:
            if win is not None and win.native is not None:
                win.native.TopMost = bool(on)
        except Exception:
            pass
    threading.Thread(target=clear_prop, daemon=True).start()


def show_window(visible):
    hwnd = get_hwnd()
    if not hwnd:
        return
    ctypes.windll.user32.ShowWindow(hwnd, SW_SHOW if visible else SW_HIDE)
    if visible:
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        round_corners()


def system_theme():
    out = {"dark": True, "accent": "#4cc2ff"}
    if os.name != "nt":
        return out
    import winreg
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        out["dark"] = winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        pass
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\DWM")
        v = winreg.QueryValueEx(k, "AccentColor")[0]
        out["accent"] = "#%02x%02x%02x" % (v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF)
    except OSError:
        pass
    return out


# ============ 暴露给前端 ============
class Api:
    def load_config(self):
        cfg = read_cfg()
        name = cfg.get("img_file")
        if name:
            p = os.path.join(IMGDIR, name)
            cfg["img_data"] = img_to_dataurl(p) if os.path.exists(p) else ""
        return cfg

    def save_config(self, cfg):
        cfg.pop("img_data", None)
        write_cfg(cfg)
        return True

    def get_theme(self):
        return system_theme()

    def refresh_glass(self, acrylic=False, alpha=90, glass=True, color="#191c21"):
        t = system_theme()
        apply_blur(t["dark"], bool(acrylic), int(alpha), bool(glass), str(color))
        write_cfg({"solid_color": str(color)})
        round_corners()
        add_shadow()
        return t

    # ---- 图片 ----
    def pick_image(self):
        r = win.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=("图片 (*.png;*.jpg;*.jpeg;*.gif;*.webp;*.bmp)",))
        if not r:
            return None
        src = r[0]
        for old in os.listdir(IMGDIR):
            try:
                os.remove(os.path.join(IMGDIR, old))
            except OSError:
                pass
        name = "bg" + os.path.splitext(src)[1].lower()
        dst = os.path.join(IMGDIR, name)
        try:
            shutil.copyfile(src, dst)
        except shutil.SameFileError:
            pass
        return {"file": name, "data": img_to_dataurl(dst)}

    def clear_image(self):
        for f in os.listdir(IMGDIR):
            try:
                os.remove(os.path.join(IMGDIR, f))
            except OSError:
                pass
        return True

    # ---- 窗口 ----
    def set_topmost(self, on):
        set_topmost(bool(on))
        write_cfg({"top": bool(on)})
        return True

    def get_workarea(self):
        wa = work_area()
        return {"w": wa.right - wa.left, "h": wa.bottom - wa.top}

    def resize(self, w, h):
        w, h = int(w), int(h)
        win.resize(w, h)
        # 必须宽高都比对：只等高度的话，拖尺寸滑块（只变宽）会立刻通过，
        # 于是按旧宽度裁剪，右边多出一条直角的没裁到的区域。
        for _ in range(12):
            time.sleep(0.02)
            r = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(get_hwnd(), ctypes.byref(r))
            if abs((r.right - r.left) - w) <= 2 and abs((r.bottom - r.top) - h) <= 2:
                break
        c = read_cfg()
        t = system_theme()
        apply_blur(t["dark"], c.get("acrylic", False), int(c.get("glass", 90)),
                   c.get("bg", "auto") == "glass",
                   c.get("solid_color", "#191c21"))
        round_corners()
        ensure_on_screen()
        return True

    def minimize(self):
        show_window(False)
        return True

    def quit(self):
        save_pos()
        if tray:
            tray.stop()
        threading.Thread(target=win.destroy, daemon=True).start()
        return True

    # ---- 开机自启：用启动文件夹，不碰注册表 Run 键 ----
    def get_autostart(self):
        return os.path.exists(STARTUP_VBS)

    def set_autostart(self, on):
        if os.name != "nt":
            return False
        if not on:
            try:
                os.remove(STARTUP_VBS)
            except OSError:
                pass
            return False
        parts = [sys.executable] if FROZEN else [
            sys.executable.replace("python.exe", "pythonw.exe"),
            os.path.abspath(__file__)]
        inner = " ".join('""%s""' % p for p in parts)
        vbs = 'CreateObject("WScript.Shell").Run "%s", 0, False\r\n' % inner
        os.makedirs(os.path.dirname(STARTUP_VBS), exist_ok=True)
        for enc in ("mbcs", "utf-8"):
            try:
                with open(STARTUP_VBS, "w", encoding=enc) as f:
                    f.write(vbs)
                return True
            except (LookupError, UnicodeEncodeError):
                continue
        return False


# ============ 杂项 ============
def save_pos():
    if pos:
        write_cfg({"wx": pos.get("x"), "wy": pos.get("y")})


def on_moved(x, y):
    pos["x"], pos["y"] = int(x), int(y)


def build_tray():
    global tray
    try:
        import pystray
        from PIL import Image
    except ImportError:
        return
    try:
        icon = Image.open(res("app.ico"))
    except Exception:
        from PIL import ImageDraw
        icon = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(icon)
        d.ellipse((4, 4, 60, 60), fill=(16, 21, 25, 255))
        d.line((32, 32, 32, 15), fill=(231, 178, 63), width=5)
        d.line((32, 32, 46, 38), fill=(231, 178, 63), width=5)
    tray = pystray.Icon(APP, icon, TITLE, pystray.Menu(
        pystray.MenuItem("显示", lambda *_: show_window(True), default=True),
        pystray.MenuItem("隐藏", lambda *_: show_window(False)),
        pystray.MenuItem("退出", lambda i, *_: (
            save_pos(), i.stop(),
            threading.Thread(target=win.destroy, daemon=True).start())),
    ))
    tray.run()


def single_instance():
    global _lock
    _lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _lock.bind(("127.0.0.1", 51720))
        _lock.listen(1)
        return True
    except OSError:
        return False


def on_start():
    threading.Thread(target=build_tray, daemon=True).start()
    time.sleep(0.45)
    c = read_cfg()
    t = system_theme()
    apply_blur(t["dark"], c.get("acrylic", False), int(c.get("glass", 90)),
               c.get("bg", "auto") == "glass",
               c.get("solid_color", "#191c21"))
    round_corners()
    add_shadow()


if __name__ == "__main__":
    if not single_instance():
        sys.exit(0)

    page = res("ui.html")
    if not os.path.exists(page):
        raise SystemExit("找不到 ui.html，应与 main.py 放在同一目录：" + page)
    with open(page, encoding="utf-8") as f:
        html = f.read()

    saved = read_cfg()
    win = webview.create_window(
        TITLE, html=html, js_api=Api(),
        width=320, height=104,
        x=saved.get("wx"), y=saved.get("wy"),
        frameless=True,
        easy_drag=False,
        transparent=True,
        on_top=saved.get("top", True),
        resizable=True,
        background_color="#000000",
    )
    try:
        win.events.moved += on_moved
        win.events.closing += save_pos
    except Exception:
        pass

    webview.start(on_start, gui="edgechromium", debug=False)
