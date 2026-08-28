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
DOCK_SNAP = 28      # 拖动结束时离边缘小于这个距离就算吸附
RADIUS = 16

DEFAULTS = {
    "start": "09:30", "end": "18:30", "salary": 12000,
    "work_text": "距下班",
    "cur_sym": "¥",            # 货币符号，从设置里的下拉框选
    "sym_gap": False,          # 符号和数字之间空一格
    "show_money": True,
    "bg": "auto",              # auto | light | dark | glass
    "glass": 90,
    "ui": 100,                 # 缩放百分比
    "cardw": 320, "cardh": 104,
    "auto_resize": False,      # 导入图片时是否按比例改卡片尺寸
    "auto_size": False,        # 按当前图片比例自动调整卡片另一维，贴三边且不裁
    "img_fill": "cover",       # cover 贴三边（会裁） | fit 完整显示靠边对齐
    "text_pct": 40,            # 文字区占比%：堆叠时是底部条高度，两列时是文字列宽度
    "img_side": "left",        # 纵向布局时图片靠哪边：left | right | center
    "img_file": "", "iw": 45, "iw_auto": True, "ix": 0, "iy": 0, "fade": 22,
    "slot_rows": 2,            # 倒计时最多占几行，多出来的不显示
    "rotate_min": 0,           # 图片轮换间隔（分钟），0 = 不轮换
    "shuffle": True,           # 随机还是按文件名顺序
    "alttab": False,           # 是否出现在 Alt+Tab 列表里
    "top": True, "wx": None, "wy": None,
    "dock": True,              # 拖到屏幕边缘时自动吸附
    "dock_x": "", "dock_y": "",   # 吸在哪条边：left/right、top/bottom，空=没吸
    "dock_pad": 12,            # 吸附后离边缘留多少像素
    "slots": [{"label": "周五", "type": "weekly", "value": "5"},
              {"label": "发薪", "type": "monthly", "value": "10"}],
}


# ============================ 配置 ============================


def fix_astral(t):
    """把一对孤立的代理项拼回真正的字符（emoji 走 tkinter 输入框会被拆成这样）。
    已经正常的字符串原样返回。"""
    t = str(t)
    try:
        return t.encode("utf-16", "surrogatepass").decode("utf-16")
    except Exception:
        return t


_CFG_LOCK = threading.RLock()
_CFG_CACHE = None
_LAST_SAVE_ERR = [""]      # 最近一次保存失败的原因，设置窗口会显示出来


def read_cfg():
    """读失败时退回上一次的好配置，而不是一整套默认值。

    之前的写法是解析失败就 return DEFAULTS，配合"读出来改几个键再整个写回"
    的保存方式，只要读写撞车（写到一半文件被截断），下一次保存就会把默认值
    整个写回去，用户设置全丢。
    """
    global _CFG_CACHE
    with _CFG_LOCK:
        try:
            with open(CFG, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("bad config")
            cfg = dict(DEFAULTS)
            cfg.update(data)
            _CFG_CACHE = dict(cfg)
            return cfg
        except FileNotFoundError:
            cfg = dict(DEFAULTS)
            _CFG_CACHE = dict(cfg)
            return cfg
        except Exception as e:
            print("config read failed, keeping last good:", e)
            return dict(_CFG_CACHE) if _CFG_CACHE else dict(DEFAULTS)


def _clean(o):
    """把整份配置里的字符串都过一遍 fix_astral。

    Windows 的 emoji 面板是按 UTF-16 码元往输入框送字符的，Tk 收到的是两个
    孤立的代理项。这种字符串编不成 UTF-8，json.dump 会抛 UnicodeEncodeError，
    整次保存全部失败——而且异常被下面的 except 吞掉，用 pythonw 跑连报错都看
    不见，表现就是"设置改了没反应"。所以在写入口统一拼回真字符。
    """
    if isinstance(o, str):
        return fix_astral(o)
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    return o


def write_cfg(patch):
    """先写临时文件再原子替换，避免别的线程读到半截文件"""
    global _CFG_CACHE
    with _CFG_LOCK:
        cur = read_cfg()
        cur.update(patch)
        cur = _clean(cur)
        tmp = CFG + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, CFG)         # 原子替换，读的一方要么旧要么新
            _CFG_CACHE = dict(cur)
        except Exception as e:
            print("save failed:", e)
            _LAST_SAVE_ERR[0] = repr(e)
            try:
                os.remove(tmp)
            except OSError:
                pass
        else:
            _LAST_SAVE_ERR[0] = ""


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


# 字体必须缓存。一次 render 里光收缩循环就要建几十个字体对象，
# 每个 truetype() 都是重新打开并解析一遍 ttf（msyh.ttc 十几 MB），
# 动图每秒十几帧的话这一项就能吃掉大半 CPU。
_FONT_CACHE = {}


def load_font(names, size):
    size = max(1, int(size))
    key = (tuple(names), size)
    hit = _FONT_CACHE.get(key)
    if hit is not None:
        return hit
    f = None
    for n in names:
        for d in (r"C:\Windows\Fonts", ""):
            try:
                f = ImageFont.truetype(os.path.join(d, n) if d else n, size)
                break
            except Exception:
                continue
        if f is not None:
            break
    if f is None:
        f = ImageFont.load_default()
    if len(_FONT_CACHE) > 400:
        _FONT_CACHE.clear()
    _FONT_CACHE[key] = f
    return f


# 数字用 Bahnschrift（Win10 自带的窄体），中文用微软雅黑。
# Bahnschrift 没有中文字形，拿它渲染「已下班」只会得到一排豆腐块，
# 所以主显示区按内容里有没有中日韩字符来选字体。
def has_cjk(t):
    return any("\u3400" <= c <= "\u9fff" or "\uff00" <= c <= "\uffef" for c in str(t))


F_NUM = lambda s: load_font(["bahnschrift.ttf", "segoeui.ttf"], s)
F_TXT = lambda s: load_font(["msyh.ttc", "msyh.ttf", "segoeui.ttf"], s)
F_MON = lambda s: load_font(["consola.ttf", "cour.ttf", "segoeui.ttf"], s)
# 货币符号专用。雅黑的货币符号覆盖很窄（฿ ₫ ₱ 这些都没有），
# Segoe UI 反而全，所以符号优先走它，汉字量词才回落到雅黑。
F_SYM = lambda s: load_font(["segoeui.ttf", "seguisym.ttf", "msyh.ttc"], s)
# seguiemj.ttf 有可能加载不上（系统裁剪过、Pillow/FreeType 版本对彩色字形
# 支持不同）。走 load_font 的话失败会静默退回 ImageFont.load_default()，
# 那是个 11px 的点阵字体，emoji 直接画不出来——而且还看不出是哪一步坏的。
# 所以单独走一条路径，明确区分「拿到彩色字体」和「没拿到」。
_EMO_CACHE = {}


def emoji_font(size):
    """返回 (字体, 是否彩色)。没拿到彩色字体时退回中文字体画单色字形，
    这时候绝不能再传 embedded_color=True——非彩色字体走那条路径是空白。"""
    size = max(1, int(size))
    hit = _EMO_CACHE.get(size)
    if hit is not None:
        return hit
    f = None
    for name in ("seguiemj.ttf", "seguisym.ttf"):
        for d in (r"C:\Windows\Fonts", ""):
            try:
                f = ImageFont.truetype(os.path.join(d, name) if d else name, size)
                break
            except Exception:
                continue
        if f is not None:
            hit = (f, name == "seguiemj.ttf")
            break
    if hit is None:
        hit = (F_TXT(size), False)
    if len(_EMO_CACHE) > 200:
        _EMO_CACHE.clear()
    _EMO_CACHE[size] = hit
    return hit

# 雅黑没有 emoji 字形，画出来是豆腐块。彩色 emoji 在 seguiemj.ttf 里，
# 而且必须用 Pillow 的 embedded_color=True 才会带颜色。
EMOJI_RANGES = (
    (0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2B00, 0x2BFF),
    (0x2190, 0x21FF), (0xFE00, 0xFE0F), (0x1F1E6, 0x1F1FF),
    (0x203C, 0x2049), (0x200D, 0x200D),      # 200D 是零宽连接符，组合 emoji 用
)


def is_emoji(ch):
    o = ord(ch)
    return any(a <= o <= b for a, b in EMOJI_RANGES)


def split_runs(text):
    """把文字切成 emoji 段和普通段，每段用各自的字体画"""
    runs, cur, cur_e = [], "", None
    for ch in str(text):
        e = is_emoji(ch)
        if cur and e != cur_e:
            runs.append((cur_e, cur))
            cur = ""
        cur, cur_e = cur + ch, e
    if cur:
        runs.append((cur_e, cur))
    return runs


def text_w(d, text, font):
    """混排文字的总宽度"""
    total = 0
    for emo, part in split_runs(text):
        total += d.textlength(part, font=emoji_font(font.size)[0] if emo else font)
    return total


def draw_text(d, xy, text, font, fill):
    """混排绘制：emoji 段走彩色字体，其余用传入的字体"""
    x, y = xy
    for emo, part in split_runs(text):
        if emo:
            f, color = emoji_font(font.size)
            if color:
                try:
                    d.text((x, y), part, font=f, embedded_color=True)
                except (TypeError, ValueError):   # 老版本 Pillow 不支持彩色字形
                    d.text((x, y), part, font=f, fill=fill)
            else:
                d.text((x, y), part, font=f, fill=fill)
            x += d.textlength(part, font=f)
        else:
            d.text((x, y), part, font=font, fill=fill)
            x += d.textlength(part, font=font)


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
_CARD_CACHE = {}
_NOISE_CACHE = {}
_GIF_CACHE = {}


def card_shape(W, H, r, base):
    """圆角卡片和它的形状遮罩。超采样后缩放，边缘自带抗锯齿。
    动图每秒要画十几帧，这一步最贵，缓存起来。"""
    key = (W, H, r, base)
    hit = _CARD_CACHE.get(key)
    if hit:
        return hit[0].copy(), hit[1]
    big_im = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    ImageDraw.Draw(big_im).rounded_rectangle(
        (0, 0, W * SS - 1, H * SS - 1), radius=r * SS, fill=base)
    im = big_im.resize((W, H), Image.LANCZOS)
    shape = Image.new("L", (W * SS, H * SS), 0)
    ImageDraw.Draw(shape).rounded_rectangle(
        (0, 0, W * SS - 1, H * SS - 1), radius=r * SS, fill=255)
    shape = shape.resize((W, H), Image.LANCZOS)
    if len(_CARD_CACHE) > 8:
        _CARD_CACHE.clear()
    _CARD_CACHE[key] = (im, shape)
    return im.copy(), shape


def noise_layer(W, H, shape):
    key = (W, H)
    hit = _NOISE_CACHE.get(key)
    if hit is None:
        n = Image.effect_noise((W, H), 28).convert("L")
        hit = Image.merge("RGBA", (n, n, n, Image.new("L", (W, H), 12)))
        hit.putalpha(ImageChops.multiply(hit.split()[3], shape))
        if len(_NOISE_CACHE) > 8:
            _NOISE_CACHE.clear()
        _NOISE_CACHE[key] = hit
    return hit


IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".apng")


def image_pool():
    """图片池就是 images 目录里的所有图，按文件名排序"""
    try:
        return sorted(f for f in os.listdir(IMGDIR)
                      if f.lower().endswith(IMG_EXT))
    except OSError:
        return []


def load_frames(path, bw, bh, cfg, vertical=False, flip=False):
    """GIF / APNG / 动态 WebP 逐帧预处理并缓存。
    返回 (帧列表, 每帧毫秒列表, 总时长)。静态图就是单帧。"""
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return [], [], 0
    key = (path, mt, bw, bh, int(cfg.get("ix", 0)), int(cfg.get("iy", 0)),
           int(cfg.get("fade", 45)), vertical, flip)
    hit = _GIF_CACHE.get(key)
    if hit:
        return hit

    frames, durs = [], []
    try:
        src = Image.open(path)
        total = getattr(src, "n_frames", 1)
        step = max(1, total // 120)        # 帧太多就抽帧，别把内存吃光
        for i in range(0, total, step):
            src.seek(i)
            frames.append(place(src.convert("RGBA"), bw, bh, cfg, vertical, flip))
            durs.append(max(30, src.info.get("duration", 100) * step))
    except Exception as e:
        print("frame load failed:", e)
        return [], [], 0

    _GIF_CACHE.clear()                     # 只缓存当前这张图
    out = (frames, durs, sum(durs))
    _GIF_CACHE[key] = out
    return out


def place(src, bw, bh, cfg, vertical=False, flip=False):
    """铺满目标框并居中裁切，偏移量用来挑裁哪一块。
    横向布局在左缘淡出，纵向布局在下缘淡出。"""
    ratio = max(bw / src.width, bh / src.height)
    src = src.resize((max(1, int(src.width * ratio)),
                      max(1, int(src.height * ratio))), Image.LANCZOS)
    # cover 之后图片必定不小于容器，偏移量只能在"还盖得住"的范围内移动。
    # 不夹住的话，残留的旧偏移会把图片推出画面，边上露出空白。
    ox = (bw - src.width) // 2 + int(cfg.get("ix", 0))
    oy = (bh - src.height) // 2 + int(cfg.get("iy", 0))
    ox = max(min(ox, 0), bw - src.width)
    oy = max(min(oy, 0), bh - src.height)
    out = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    out.paste(src, (ox, oy), src)

    fade = max(0, min(100, int(cfg.get("fade", 45)))) / 100
    if fade > 0:
        if vertical:                      # 图片在上方，往下淡出接住文字
            n = bh
            grad = Image.new("L", (1, n))
            span = max(1, n * fade)
            for i in range(n):
                grad.putpixel((0, i), int(255 * min(1.0, (n - 1 - i) / span)))
        else:                             # 横向：往文字那一侧淡出
            n = bw
            grad = Image.new("L", (n, 1))
            span = max(1, n * fade)
            for i in range(n):
                # flip=True 表示图片在左边，要往右淡出
                t = (n - 1 - i) if flip else i
                grad.putpixel((i, 0), int(255 * min(1.0, t / span)))
        out.putalpha(ImageChops.multiply(out.split()[3], grad.resize((bw, bh))))
    return out


W_MIN, W_MAX = 180, 720
H_MIN, H_MAX = 90, 640


def clamp(v, lo, hi, dflt):
    try:
        return max(lo, min(hi, int(float(v))))
    except (TypeError, ValueError):
        return dflt


def render(cfg, theme, clock_ms=0):
    scale = max(0.75, min(1.5, float(cfg.get("ui", 100)) / 100))
    W = int(clamp(cfg.get("cardw", 320), W_MIN, W_MAX, 320) * scale)
    H = int(clamp(cfg.get("cardh", 104), H_MIN, H_MAX, 104) * scale)
    r = int(RADIUS * scale)
    pad = int(18 * scale)

    dark = theme["dark"] if cfg["bg"] in ("auto", "glass") else (cfg["bg"] == "dark")
    glass = cfg["bg"] == "glass"
    accent = theme["accent"]

    if glass:
        # 下限不能是 0：分层窗口里 alpha=0 的像素鼠标是穿透的，
        # 全透明就等于整张卡点不中，既拖不动也打不开设置。
        a = max(14, min(230, int(cfg.get("glass", 90))))
        base = (26, 26, 26, a) if dark else (242, 242, 242, a)
    else:
        base = (25, 28, 33, 255) if dark else (255, 255, 255, 255)

    # 文字必须完全不透明。"变灰"靠往底色方向混合，不能靠降 alpha——
    # 在半透明卡片上降 alpha 等于让桌面直接透过文字。
    fg_rgb = (255, 255, 255) if dark else (16, 18, 22)
    fg = fg_rgb + (255,)
    fg2 = tuple(int(x * 0.55 + y * 0.45) for x, y in zip(fg_rgb, base[:3])) + (255,)

    im, shape = card_shape(W, H, r, base)
    probe = ImageDraw.Draw(im)

    big, sec, cap, money, parts = compute(cfg)
    show_money = bool(cfg.get("show_money", True))
    sym = str(cfg.get("cur_sym", "¥"))
    # 金额数字走 consola / courier 等宽体——它每秒都在变，不等宽会左右抖。
    # 但这两个字体只覆盖西文和少数几个货币符号，₩ ₽ ₹ 元 都没有，画出来
    # 是豆腐块。所以符号单独判断：等宽体有的就用等宽体（保持 ¥ 原来的字形），
    # 没有的交给雅黑。
    # 等宽体（consola）覆盖全部 ASCII 和这几个符号，RM / M$ / HK$ 这类
    # 字母组合走它跟数字最搭；覆盖不到的交给 Segoe UI；汉字量词交给雅黑。
    MONO_SAFE = "$¥£¢€"
    _sym_mono = bool(sym) and all(c in MONO_SAFE or (" " <= c <= "~") for c in sym)

    def _f_sym(f):
        if _sym_mono:
            return f
        return F_TXT(f.size) if has_cjk(sym) else F_SYM(f.size)

    # 「元」这类汉字量词在中文里是跟在数字后面的（288.91 元），
    # 只有 ¥ $ € 这些符号才前置。按是不是汉字自动判断。
    _sym_after = has_cjk(sym)

    def _sym_dy(f):
        """两段用的不是同一个字体，ascent 不一样。draw_text 是按顶端定位的，
        直接用同一个 y 会一高一低，得按基线差补一下。"""
        fs = _f_sym(f)
        if fs is f:
            return 0
        try:
            return f.getmetrics()[0] - fs.getmetrics()[0]
        except Exception:
            return 0

    # 空格宽度按字号算，不画真的空格字符——不同字体的空格宽度差很多，
    # 而且行尾空格在测宽和绘制里的处理不一定一致，容易对不齐。
    def _sym_gap(f):
        return int(f.size * 0.3) if (sym and cfg.get("sym_gap")) else 0

    def money_w(dd, f):
        """符号加数字的总宽。所有靠右对齐和字号收缩都要用这个，
        不能用 text_w(符号+数字) —— 两段字体不同，量出来是错的。"""
        if not sym:
            return text_w(dd, money, f)
        return text_w(dd, sym, _f_sym(f)) + _sym_gap(f) + text_w(dd, money, f)

    def draw_money(dd, xy, f, fill):
        x, y = xy
        if sym and not _sym_after:
            draw_text(dd, (x, y + _sym_dy(f)), sym, _f_sym(f), fill)
            x += text_w(dd, sym, _f_sym(f)) + _sym_gap(f)
        draw_text(dd, (x, y), money, f, fill)
        if sym and _sym_after:
            x += text_w(dd, money, f) + _sym_gap(f)
            draw_text(dd, (x, y + _sym_dy(f)), sym, _f_sym(f), fill)
    path = os.path.join(IMGDIR, cfg.get("img_file") or "")
    has_img = bool(cfg.get("img_file")) and os.path.exists(path)

    ratio, src_w, src_h = 1.0, 0, 0
    if has_img:
        try:
            with Image.open(path) as _p:
                src_w, src_h = _p.size
                ratio = src_w / src_h
        except Exception:
            has_img = False

    # ---------- 布局：宽卡片图片贴右侧，方卡片图片放上方 ----------
    # 卡片一变窄，"文字左图片右"就挤不下了，得改成上下堆叠。
    # 自动尺寸：固定用户设的一个维度，另一维按图片比例反推，
    # 让图片刚好贴满三边又不裁切。不写回配置，换图后自己会重算。
    if has_img and cfg.get("auto_size"):
        # 两种填充模式都要支持。之前限定只在 cover 下生效，
        # 结果配置里是 fit 的用户按了开关完全没反应。
        tp = clamp(cfg.get("text_pct", 40), 25, 65, 40) / 100
        if ratio >= 0.95:                       # 横图：宽度不动，算高度
            need = (W / ratio) / (1 - tp)
            if need > H_MAX * scale:
                # 算出来超过高度上限，反过来收窄卡片，比硬裁一半强
                W = int(clamp(H_MAX * ratio * (1 - tp), W_MIN, W_MAX, 320) * scale)
                need = (W / ratio) / (1 - tp)
            H = int(clamp(need / scale, H_MIN, H_MAX, 104) * scale)
            # 小图别硬撑大：放大超过 2 倍就糊，按原始分辨率封顶
            # 注意变量名：cap 是上面 compute() 返回的说明文字，别在这里复用！
            cap_px = int(src_w * 2 * scale)
            if src_w and W > cap_px:
                W = int(clamp(cap_px / scale, W_MIN, W_MAX, 320) * scale)
                H = int(clamp((W / ratio) / (1 - tp) / scale, H_MIN, H_MAX, 104) * scale)
        else:                                   # 竖图：高度不动，算宽度
            need = (H * ratio) / (1 - tp)
            if need > W_MAX * scale:
                H = int(clamp(W_MAX / ratio * (1 - tp), H_MIN, H_MAX, 104) * scale)
                need = (H * ratio) / (1 - tp)
            W = int(clamp(need / scale, W_MIN, W_MAX, 320) * scale)
            cap_px = int(src_h * 2 * scale)
            if src_h and H > cap_px:
                H = int(clamp(cap_px / scale, H_MIN, H_MAX, 104) * scale)
                W = int(clamp((H * ratio) / (1 - tp) / scale, W_MIN, W_MAX, 320) * scale)
        im, shape = card_shape(W, H, r, base)
        probe = ImageDraw.Draw(im)

    # 布局按图片方向定，不按卡片形状：竖构图天然适合和文字并排成两列，
    # 堆叠会把文字挤成上下两块，读起来是断的。只有横图和方图才堆叠。
    stack = has_img and (W / max(1, H)) < 1.55 and ratio >= 0.95
    use_L = False
    ax = aw = ay = ah = 0
    bw = bh = bx = by = 0
    tx, ty, tw, th = pad, 0, W - pad * 2, H          # 文字区

    if has_img and not stack:
        need = max(
            text_w(probe, big, F_NUM(int(20 * scale)))
            + (text_w(probe, " " + sec, F_NUM(int(10 * scale))) if sec else 0),
            text_w(probe, cap, F_TXT(int(11 * scale))),
        ) * 1.6                                      # 粗估，字号还没定
        room = W - int(need) - pad - int(10 * scale)
        if cfg.get("iw_auto", True):
            # cover 只在"条的宽高比 == 图片比例"时才不裁，
            # 所以宽度直接算成 卡片高 × 图片比例。
            bw = max(int(W * 0.2), min(int(W * 0.72), room, int(H * ratio)))
        else:
            bw = int(W * clamp(cfg.get("iw", 45), 20, 70, 45) / 100)
            bw = min(bw, max(int(W * 0.2), room))
        if cfg.get("img_fill", "cover") == "cover":
            # 文字列占多少由滑块定，剩下全给图片，图片贴住上下和一侧三条边
            tpct = clamp(cfg.get("text_pct", 40), 25, 65, 40) / 100
            bw = W - int(W * tpct)
            bh, by = H, 0
        else:
            # 完整显示模式下也让文字区占比生效，否则这个滑块只在贴三边时有用
            tpct = clamp(cfg.get("text_pct", 40), 25, 65, 40) / 100
            bw = min(bw, W - int(W * tpct))
            bh = min(H, max(1, int(bw / max(0.05, ratio))))
            by = 0                             # 完整显示时贴上边，留白留在下方
        # 靠边方向对两列布局同样生效，之前只在堆叠布局里用了
        gap = int(9 * scale)                   # 贴着图片那侧只留小缝
        if cfg.get("img_side", "left") == "left":
            bx, tx = 0, bw + gap
            tw = W - bw - gap - pad
        else:
            bx, tx = W - bw, pad
            tw = W - bw - gap - pad
    elif stack:
        # 图片占上方。底部文字条留多少由「文字区占比」定。
        tpct = clamp(cfg.get("text_pct", 40), 25, 65, 40) / 100
        if cfg.get("img_fill", "cover") == "cover":
            bh = H - int(H * tpct)             # 贴满左右和上边，多余的裁掉
            bw, bx = W, 0
        else:
            # 完整显示：图片不超过占比留给它的高度，靠角对齐不居中
            bh = min(H - int(H * tpct),
                     max(int(H * 0.25), int(W / max(0.05, ratio))))
            bw = min(W, max(1, int(bh * ratio)))
            side = cfg.get("img_side", "left")
            bx = 0 if side == "left" else (W - bw if side == "right"
                                           else (W - bw) // 2)
        by = 0                                 # 始终贴顶边
        ty, th = bh, H - bh                    # 文字区紧贴图片下沿
        tw = W - pad * 2
        # 图片没占满宽度时，旁边那块空白别浪费，主时间挪进去（L 形布局）
        side_w = W - bw
        flush = (bx == 0) or (bx + bw >= W)     # 图片必须贴住某一侧
        # 阈值要保证塞进去的字还像样，否则不如老实放下方。
        # 居中的图片两边各有一半空白，没有连续可用区域，直接不走 L 形。
        if cfg.get("img_fill", "cover") == "cover":
            pass                               # cover 下文字本来就独占一条，不需要 L 形
        elif flush and side_w >= max(int(112 * scale), int(W * 0.32)):
            use_L = True
            ax = pad if bx > 0 else bw + pad    # 图片靠右则文字在左，反之在右
            aw = side_w - pad * 2
            ay, ah = 0, bh
            ty, th = bh, H - bh

    # ---------- 字号：按文字区高度和行数反推 ----------
    # 无图 + 接近正方形时也走竖排。两列布局会把内容压成中间一小块，
    # 正方形卡片上下各空一大片。
    tall_empty = (not has_img) and (W / max(1, H)) < 1.55
    # 堆叠布局下文字区一高，两行两列就不合适了——把行距拉开只是中间空一块，
    # 本质还是两行。够高就改成竖排四行，每行各占一档，才算真正铺开。
    stack_tall = stack and th >= int(112 * scale)
    single = (has_img and not stack) or tall_empty or stack_tall
    if use_L:
        # 主时间独占侧边那块，按它的高度算；下方只放收入和倒计时
        big_sz = max(18, min(52, (ah / scale) / 2.1))
        if has_cjk(big):
            big_sz *= 0.72
        mon_sz = max(12, min(30, (th / scale) / (2.0 if show_money else 3.0)))
        cap_sz = max(9, min(15, big_sz * 0.30))
        f_big = (F_TXT if has_cjk(big) else F_NUM)(int(big_sz * scale))
        f_sec = F_NUM(int(big_sz * 0.45 * scale))
        f_cap = F_TXT(int(cap_sz * scale))
        f_mon = F_MON(int(mon_sz * scale))
        # 秒数画在主时间右边，必须算进总宽。只测主时间的话，
        # 加上秒数就会顶出侧边区域压到图片上。
        def _line_w():
            w = text_w(probe, big, f_big)
            if sec:
                w += int(5 * scale) + text_w(probe, sec, f_sec)
            return w
        while _line_w() > aw and big_sz > 12:
            big_sz -= 1
            f_big = (F_TXT if has_cjk(big) else F_NUM)(int(big_sz * scale))
            f_sec = F_NUM(int(big_sz * 0.45 * scale))
            cap_sz = max(9, min(15, big_sz * 0.30))
            f_cap = F_TXT(int(cap_sz * scale))
        avail = th / scale
    else:
        avail = th / scale - (14 if stack else 24)
        if tall_empty:
            avail = th / scale - 20
    if use_L:
        pass
    elif single:
        # 上限跟着可用高度走。写死 44 的话，250px 高的卡片主时间才 34px，
        # 跟卡片完全不成比例。
        hi_m, hi_n = (72, 88) if tall_empty else (64, 80)
        big_sz = (max(20, min(hi_m, avail / 2.9)) if show_money
                  else max(20, min(hi_n, avail / 2.2)))
    else:
        big_sz = (max(20, min(52, avail / 2.2)) if show_money
                  else max(20, min(58, avail / 1.7)))
    if not use_L:
        if has_cjk(big):
            big_sz *= 0.72    # 中文方块字比窄体数字宽得多，压一点才放得下
        mon_sz = big_sz * (0.52 if single else 0.58)
        cap_sz = max(9, min(15, big_sz * 0.30))

        f_big = (F_TXT if has_cjk(big) else F_NUM)(int(big_sz * scale))
        f_sec = F_NUM(int(big_sz * 0.45 * scale))
        f_cap = F_TXT(int(cap_sz * scale))
        f_mon = F_MON(int(mon_sz * scale))

        # 主字太宽就缩，窄卡片下很容易顶出去。秒数也要算进去。
        def _line_w2():
            w = text_w(probe, big, f_big)
            if sec:
                w += int(6 * scale) + text_w(probe, sec, f_sec)
            return w
        while _line_w2() > tw and big_sz > 14:
            big_sz -= 1
            f_big = (F_TXT if has_cjk(big) else F_NUM)(int(big_sz * scale))
            f_sec = F_NUM(int(big_sz * 0.45 * scale))
            # 金额和说明是按 big_sz 的比例定的，主字缩了它们必须跟着缩。
            # 之前只改 f_big / f_sec，f_mon 一直停在收缩前那一档，
            # 窄文字列里就会直接画出卡片边框（"¥257." 被截断）。
            mon_sz = big_sz * (0.52 if single else 0.58)
            cap_sz = max(9, min(15, big_sz * 0.30))
            f_mon = F_MON(int(mon_sz * scale))
            f_cap = F_TXT(int(cap_sz * scale))

        # 金额本身也要按宽度校验一次：它的字号是从主字高度推出来的，
        # 从来没量过自己有多宽，纯数字位数一多照样出框。
        if show_money:
            room_m = (W - pad * 2) if use_L else tw
            while money_w(probe, f_mon) > room_m and f_mon.size > 9:
                f_mon = F_MON(f_mon.size - 1)

    # ---------- 画图片 ----------
    if has_img and bw > 0:
        try:
            frames, durs, total = load_frames(path, bw, bh, cfg,
                                              vertical=stack, flip=(bx == 0))
            if not frames:
                raise ValueError("no frames")
            if len(frames) > 1 and total:            # 动图：按累计时长挑当前帧
                t, idx = clock_ms % total, 0
                for i, dms in enumerate(durs):
                    if t < dms:
                        idx = i
                        break
                    t -= dms
                band = frames[idx]
            else:
                band = frames[0]
            layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            layer.paste(band, (bx, by), band)
            layer.putalpha(ImageChops.multiply(layer.split()[3], shape))
            im = Image.alpha_composite(im, layer)
        except Exception as e:
            print("image failed:", e)

    im = Image.alpha_composite(im, noise_layer(W, H, shape))
    d = ImageDraw.Draw(im)

    # ---------- 倒计时折行 ----------
    def shrink(text, size, maxw, floor=8):
        sz = size
        while sz > floor:
            f = F_TXT(int(sz))
            if text_w(d, text, f) <= maxw:
                return f
            sz -= 1
        return F_TXT(int(floor))

    def wrap(items, font, maxw):
        lines, cur = [], ""
        for it in items:
            cand = (cur + " · " + it) if cur else it
            if cur and text_w(d, cand, font) > maxw:
                lines.append(cur)
                cur = it
            else:
                cur = cand
        if cur:
            lines.append(cur)
        return lines

    room_r = aw if use_L else (tw if single else max(60, min(int(tw * 0.62), tw)))
    if use_L:
        room_r = tw

    # 倒计时最多占几行。没有这个上限的话，加满 6 项就会折成四五行，
    # 后面的收缩循环为了把它们全塞进去会把主时间一起压到最小档，
    # 结果整张卡片全是小字——宁可少显示两条，也不能让主显示区失真。
    slot_cap = int(clamp(cfg.get("slot_rows", 2), 1, 4, 2))
    CAPR_MIN = 10          # 倒计时字号下限，再小就不是给人看的了

    def rewrap(f):
        return wrap(parts, f, room_r)[:slot_cap] if parts else []

    f_capr = f_cap
    slot_lines = rewrap(f_capr)
    while slot_lines and any(text_w(d, l, f_capr) > room_r for l in slot_lines) \
            and f_capr.size > CAPR_MIN:
        f_capr = F_TXT(f_capr.size - 1)
        slot_lines = rewrap(f_capr)

    g_big = int(f_big.size * 0.34)
    g_mid = int(f_mon.size * 0.42)
    slot_gap = max(2, int(f_capr.size * 0.5))

    def measure(fc, lines):
        """返回 (总高, 可撑开的间距合计)。用返回值传，别用模块全局——
        那样两次渲染之间会串值。"""
        if use_L:
            slot_h = (len(lines) * fc.size + slot_gap * max(0, len(lines) - 1)) if lines else 0
            h = (f_mon.size if show_money else 0) \
                + (int(fc.size * 0.6) if show_money and lines else 0) + slot_h
            return h, 0
        if single:
            rows = f_big.size + f_cap.size + fc.size * len(lines) \
                   + (f_mon.size if show_money else 0)
            gaps = g_big + (g_mid if show_money else 0) \
                   + (int(fc.size * 0.9) if lines else 0) \
                   + slot_gap * max(0, len(lines) - 1)
        else:
            slot_h = (len(lines) * fc.size + slot_gap * max(0, len(lines) - 1)) if lines else 0
            rows = max(f_big.size, f_mon.size if show_money else 0) \
                   + max(f_cap.size, slot_h)
            gaps = g_big
        return rows + gaps, gaps

    guard = int(8 * scale) * 2
    block, _gaps_sum = measure(f_capr, slot_lines)
    while block > th - guard and f_capr.size > CAPR_MIN:
        f_capr = F_TXT(f_capr.size - 1)
        slot_lines = rewrap(f_capr)
        slot_gap = max(2, int(f_capr.size * 0.5))
        block, _gaps_sum = measure(f_capr, slot_lines)   # measure 返回二元组，必须解包

    # 放不下时按优先级依次让步，而不是四个字号一起降。
    # 一起降的结果是加两条倒计时就把主时间也拖成小字——主显示区是这张卡片
    # 存在的理由，它必须最后一个动。顺序：砍倒计时行 → 缩倒计时 →
    # 缩说明和金额 → 才轮到主时间。
    for _ in range(60):
        if block <= th - guard:
            break
        shrunk = False
        if len(slot_lines) > 1:
            slot_lines.pop()
            shrunk = True
        elif f_capr.size > CAPR_MIN:
            f_capr = F_TXT(f_capr.size - 1)
            slot_lines = rewrap(f_capr)
            slot_gap = max(2, int(f_capr.size * 0.5))
            shrunk = True
        elif f_cap.size > 10 or (show_money and f_mon.size > 11):
            if f_cap.size > 10:
                f_cap = F_TXT(f_cap.size - 1)
            if show_money and f_mon.size > 11:
                f_mon = F_MON(f_mon.size - 1)
            shrunk = True
        elif f_big.size > 14 and (not use_L):
            big_sz = max(14, f_big.size / scale - 1)
            f_big = (F_TXT if has_cjk(big) else F_NUM)(int(big_sz * scale))
            f_sec = F_NUM(max(8, int(big_sz * 0.45 * scale)))
            shrunk = True
        g_big = int(f_big.size * 0.34)
        g_mid = int(f_mon.size * 0.42)
        block, _gaps_sum = measure(f_capr, slot_lines)
        if not shrunk:
            break
    while slot_lines and block > th - guard:   # 缩到底还放不下就少显示几行
        slot_lines.pop()
        block, _gaps_sum = measure(f_capr, slot_lines)

    # 行都砍光了还塞不下，说明文字区实在太小，
    # 这时候宁可不显示收入，也不能让文字叠在一起
    if block > th - guard and show_money and single:
        show_money = False
        block, _gaps_sum = measure(f_capr, slot_lines)
    if block > th - guard:                     # 最后兜底：说明行也让位
        cap = ""
        block, _gaps_sum = measure(f_capr, slot_lines)

    y = ty + max(int(6 * scale), (th - block) // 2)

    # ---------- 调试输出 ----------
    # set OFFWORK_DEBUG=1 后用 python widget.py 跑（不要用 pythonw，看不到输出）
    if os.environ.get("OFFWORK_DEBUG"):
        print("[render] W=%d H=%d scale=%.2f | tx=%d ty=%d tw=%d th=%d "
              "| single=%s stack=%s use_L=%s | block=%d guard=%d y=%d"
              % (W, H, scale, tx, ty, tw, th, single, stack, use_L, block, guard, y))
        print("[render] sizes big=%d sec=%d cap=%d mon=%d capr=%d | lines=%r"
              % (f_big.size, f_sec.size, f_cap.size, f_mon.size, f_capr.size, slot_lines))
        print("[render] widths big=%.1f sec=%.1f cap=%.1f mon=%.1f"
              % (text_w(d, big, f_big), text_w(d, sec, f_sec) if sec else 0,
                 text_w(d, cap, f_cap),
                 money_w(d, f_mon) if show_money else 0))

    # ---------- 画文字 ----------
    if use_L:
        # 主时间和说明放进图片旁边的空白，收入和倒计时放下方那条
        ah_block = f_big.size + int(f_big.size * 0.34) + f_cap.size
        ya = ay + max(int(6 * scale), (ah - ah_block) // 2)
        draw_text(d, (ax, ya), big, f_big, fg)
        bwid = text_w(d, big, f_big)
        if sec:
            sx = min(ax + bwid + int(5 * scale),
                     ax + aw - int(text_w(d, sec, f_sec)))     # 兜底，不越界
            draw_text(d, (sx, ya + int((f_big.size - f_sec.size) * .75)),
                      sec, f_sec, fg2)
        ya += f_big.size + int(f_big.size * 0.34)
        f_capl = f_cap if text_w(d, cap, f_cap) <= aw else shrink(cap, f_cap.size, aw)
        draw_text(d, (ax, ya), cap, f_capl, fg2)

        slot_h = (len(slot_lines) * f_capr.size
                  + slot_gap * max(0, len(slot_lines) - 1)) if slot_lines else 0
        bblock = (f_mon.size if show_money else 0) \
                 + (int(f_capr.size * 0.6) if show_money and slot_lines else 0) + slot_h
        yb = ty + max(int(4 * scale), (th - bblock) // 2)
        yb = min(yb, H - int(4 * scale) - bblock)      # 兜底，绝不越过下沿
        if show_money:
            draw_money(d, (pad, yb), f_mon, accent + (255,))
            yb += f_mon.size + (int(f_capr.size * 0.6) if slot_lines else 0)
        for i, line in enumerate(slot_lines):
            draw_text(d, (pad, yb + i * (f_capr.size + slot_gap)), line, f_capr, fg2)
    elif single:
        # 卡片比内容高很多时（方形无图），把行距撑开铺满上下，
        # 比一味放大字号自然——内容还是成组的，只是呼吸感更足。
        # 只要还有余量就把行距撑开铺满，不限于方形无图那一种情况。
        # 撑开是在字号收缩之后做的，撑完必须再验一次没超框，
        # 否则某些占比区间会撑过头、行与行叠在一起。
        rows_only = block - _gaps_sum
        stretch = 1.0
        if _gaps_sum > 0:
            room_for_gaps = max(0, (th - guard) - rows_only)
            stretch = max(1.0, min(3.5 if tall_empty else 2.4,
                                   min(th * 0.86 - rows_only, room_for_gaps) / _gaps_sum))
        while stretch > 1.0:
            g_big_s = int(g_big * stretch)
            g_mid_s = int(g_mid * stretch)
            g_slot_s = int(int(f_capr.size * 0.9) * stretch)
            block_s = rows_only + g_big_s + (g_mid_s if show_money else 0) \
                      + (g_slot_s if slot_lines else 0) \
                      + slot_gap * max(0, len(slot_lines) - 1)
            if block_s <= th - guard:
                break
            stretch -= 0.1
        else:
            g_big_s, g_mid_s = g_big, g_mid
            g_slot_s = int(f_capr.size * 0.9)
            block_s = block
        y = ty + max(int(6 * scale), (th - block_s) // 2)

        draw_text(d, (tx, y), big, f_big, fg)
        bwid = text_w(d, big, f_big)
        if sec:
            draw_text(d, (tx + bwid + int(6 * scale),
                          y + int((f_big.size - f_sec.size) * .75)), sec, f_sec, fg2)
        y += f_big.size + g_big_s

        f_capl = f_cap if text_w(d, cap, f_cap) <= tw else shrink(cap, f_cap.size, tw)
        draw_text(d, (tx, y), cap, f_capl, fg2)

        # 倒计时紧跟在说明行下面（都是小字灰字，成一组），收入放最后一行。
        # 收入是彩色重点，压在底部比夹在中间更稳。
        y += f_cap.size
        if slot_lines:
            y += g_slot_s
            for i, line in enumerate(slot_lines):
                draw_text(d, (tx, y + i * (f_capr.size + slot_gap)), line, f_capr, fg2)
            y += len(slot_lines) * f_capr.size + slot_gap * (len(slot_lines) - 1)
        if show_money:
            y += g_mid_s
            draw_money(d, (tx, y), f_mon, accent + (255,))
    else:
        # 两行两列：第一行主字与金额底部对齐，第二行说明与倒计时顶部对齐。
        # 文字区比内容高很多时把行距撑开，否则两行贴在一起浮在中间。
        #
        # ---- 先按宽度收一遍 ----
        # 这两行都是左右并排的，每一半能用的宽度不是整条 tw。
        # 上面所有的收缩循环只按高度和 tw 判断，压根没有"同一行里两块字
        # 会互相撞"这个约束——秒数会钻到金额底下、说明会钻到倒计时底下。
        # 高度算得再准也挡不住，这才是文字重叠的真正来源。
        gapx = max(6, int(8 * scale))

        def _row1_w():
            w = text_w(d, big, f_big)
            if sec:
                w += int(6 * scale) + text_w(d, sec, f_sec)
            return w

        for _ in range(80):
            mw_ = money_w(d, f_mon) if show_money else 0
            room1 = tw - (mw_ + gapx if show_money else 0)
            if _row1_w() <= room1:
                break
            # 金额先让，主时间是主角，能不缩就不缩
            if show_money and f_mon.size > 11:
                f_mon = F_MON(f_mon.size - 1)
            elif f_big.size > 12:
                nb = f_big.size - 1
                f_big = (F_TXT if has_cjk(big) else F_NUM)(nb)
                f_sec = F_NUM(max(8, int(nb * 0.45)))
            elif show_money:
                show_money = False          # 实在挤不下，宁可不显示收入
            else:
                sec = ""                    # 最后连秒数也砍掉
                break

        # 第二行：说明 与 倒计时 抢宽度
        for _ in range(40):
            slot_w = max([text_w(d, l, f_capr) for l in slot_lines], default=0)
            lim = int(tw - slot_w - gapx)
            if lim >= text_w(d, cap, F_TXT(9)) or not slot_lines:
                break
            if f_capr.size > CAPR_MIN:
                f_capr = F_TXT(f_capr.size - 1)
                slot_lines = rewrap(f_capr)
                slot_gap = max(2, int(f_capr.size * 0.5))
            else:
                slot_lines.pop()            # 缩到底还撞，就少显示一条倒计时
        slot_w = max([text_w(d, l, f_capr) for l in slot_lines], default=0)
        lim = max(int(tw * 0.25), int(tw - slot_w - gapx))

        slot_h = (len(slot_lines) * f_capr.size
                  + slot_gap * max(0, len(slot_lines) - 1)) if slot_lines else 0
        r1 = max(f_big.size, f_mon.size if show_money else 0)
        r2 = max(f_cap.size, slot_h)
        g2 = g_big
        if th - guard > r1 + r2 + g_big:
            g2 = int(min(th * 0.86 - r1 - r2, g_big * 9))
            g2 = max(g_big, g2)
        g2 = min(g2, max(g_big, (th - guard) - r1 - r2))    # 撑开后再夹一次
        y = ty + max(int(6 * scale), (th - (r1 + g2 + r2)) // 2)
        base_y = y + r1
        draw_text(d, (tx, base_y - f_big.size), big, f_big, fg)
        bwid = text_w(d, big, f_big)
        if sec:
            draw_text(d, (tx + bwid + int(6 * scale),
                          base_y - f_sec.size - int(f_big.size * .08)), sec, f_sec, fg2)
        if show_money:
            mw = money_w(d, f_mon)
            draw_money(d, (tx + tw - mw, base_y - f_mon.size),
                       f_mon, accent + (255,))

        y2 = base_y + g2
        # lim 已在上面按倒计时的实际宽度算好，不再用固定的 45%
        f_capl = f_cap if text_w(d, cap, f_cap) <= lim else shrink(cap, f_cap.size, lim)
        draw_text(d, (tx, y2), cap, f_capl, fg2)
        # 整块贴右边，但块内各行左对齐——逐行右对齐会让「周五 0 天」和
        # 「发薪 13 天」左边参差不齐，读起来像没对上。
        blk_x = tx + tw - slot_w
        for i, line in enumerate(slot_lines):
            draw_text(d, (blk_x, y2 + i * (f_capr.size + slot_gap)),
                      line, f_capr, fg2)

    # ---- 设置入口：鼠标悬停时才浮现 ----
    if cfg.get("_hover"):
        cx, cy = W - int(16 * scale), int(14 * scale)
        rr = max(1, int(1.6 * scale))
        for k in (-1, 0, 1):
            ox = cx + k * int(6 * scale)
            d.ellipse((ox - rr, cy - rr, ox + rr, cy + rr), fill=fg2)

    return im


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
WM_EXITSIZEMOVE = 0x232
CS_DBLCLKS = 8
TME_LEAVE = 2


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


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
_sig(user32, "MonitorFromWindow", wt.HANDLE, wt.HWND, wt.DWORD)
_sig(user32, "GetMonitorInfoW", wt.BOOL, wt.HANDLE, ctypes.c_void_p)
_sig(user32, "SetTimer", ULONG_PTR, wt.HWND, ULONG_PTR, wt.UINT, wt.LPVOID)
_sig(user32, "KillTimer", wt.BOOL, wt.HWND, ULONG_PTR)
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
_sig(gdi32, "CreateRoundRectRgn", wt.HANDLE, ctypes.c_int, ctypes.c_int,
     ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)
_sig(user32, "SetWindowRgn", ctypes.c_int, wt.HWND, wt.HANDLE, wt.BOOL)

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
        self.interval = 0
        self.t0 = time.time()
        self.pool = image_pool()
        self.idx = 0
        self.cur = None            # 当前显示的图，轮换只改内存不落盘
        self._topmost = None       # 记住上次的置顶状态，值没变就别重新申请
        self.last_switch = time.time()
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
        self.set_topmost(bool(self.cfg.get("top", True)), force=True)
        self.sync_timer()
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
        if msg == WM_EXITSIZEMOVE:
            self.detect_dock()          # 松手时判断有没有靠到边缘
            # DWM 的模糊会缓存一份采样，窗口移走后不重新取，
            # 卡片里就显示着旧位置的内容。拖动结束重新申请一次逼它刷新。
            self.apply_blur()
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
            # SendMessageW 会一直阻塞到拖动结束，所以这里已经是松手之后了。
            # 先吸附再存位置，否则存下来的是吸附前那个坐标。
            self.detect_dock()
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
        self.maybe_rotate()
        if self.cfg.get("bg") == "glass":
            now = int(time.time())
            if now != getattr(self, "_blur_tick", 0):
                self._blur_tick = now      # 每秒补刷一次采样
                self.apply_blur()
        cfg = dict(self.cfg)
        cfg["_hover"] = self.hover
        im = render(cfg, self.theme, int((time.time() - self.t0) * 1000))
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
        changed = self.size != (w, h)
        self.size = (w, h)
        if changed:                  # 尺寸没变就别动窗口，否则拖动时会被拽回去
            self.nudge_onscreen()
            if self.cfg.get("bg") == "glass":
                self.clip_region(True)

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
        self.set_topmost(bool(self.cfg.get("top", True)), force=True)

    def center(self):
        """窗口拖丢了用这个找回来"""
        self.cfg["dock_x"] = self.cfg["dock_y"] = ""
        write_cfg({"dock_x": "", "dock_y": ""})   # 居中就不该再被吸回边上
        wa = self.work_area()
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
        self.clip_region(glass)

    def clip_region(self, glass):
        """DWM 的模糊作用于整个矩形窗口，不看我们位图的 alpha。
        卡片是圆角的，四角外面那圈会露出一个矩形的模糊区域。
        只能再用 SetWindowRgn 把窗口本身裁成圆角，代价是硬边裁剪有锯齿。
        纯色模式不申请模糊，就不需要裁，圆角保持抗锯齿。"""
        if not glass:
            user32.SetWindowRgn(self.hwnd, None, True)
            return
        w, h = self.size
        if w <= 0 or h <= 0:
            return
        d = max(2, int(RADIUS * 2 * self.uiscale()))
        rgn = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, d, d)
        user32.SetWindowRgn(self.hwnd, rgn, True)

    def set_topmost(self, on, force=False):
        """重新申请 HWND_TOPMOST 会把窗口顶到置顶层最前面，压过设置窗口。
        所以状态没变就不动它。"""
        on = bool(on)
        if not force and self._topmost == on:
            return
        self._topmost = on
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST,
                            0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)

    def save_pos(self):
        r = wt.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        write_cfg({"wx": r.left, "wy": r.top})

    def reload(self):
        self.cfg = read_cfg()
        self.theme = system_theme()
        self.pool = image_pool()
        if self.pool:
            # 优先保持当前正在显示的那张。轮换只改内存不落盘，
            # 直接读配置会让每次 reload 都跳回第一张。
            cur = self.cur if self.cur in self.pool else self.cfg.get("img_file")
            self.idx = self.pool.index(cur) if cur in self.pool else 0
            self.cur = self.pool[self.idx]
            self.cfg["img_file"] = self.cur
        else:
            self.cur = None
        self.sync_timer()
        self.apply_blur()
        self.set_topmost(bool(self.cfg.get("top", True)))
        self.paint()

    def maybe_rotate(self):
        """到点就换下一张。只换图不改卡片高度——占宽本来就按图片比例算，
        方图自然窄、横图自然宽，卡片跟着跳高跳矮会很闹。"""
        every = int(self.cfg.get("rotate_min", 0) or 0)
        if every <= 0 or len(self.pool) < 2:
            return False
        if time.time() - self.last_switch < every * 60:
            return False
        self.last_switch = time.time()
        if self.cfg.get("shuffle", True):
            import random
            nxt = self.idx
            while nxt == self.idx:                 # 别连着抽到同一张
                nxt = random.randrange(len(self.pool))
            self.idx = nxt
        else:
            self.idx = (self.idx + 1) % len(self.pool)
        self.cur = self.pool[self.idx]
        self.cfg["img_file"] = self.cur
        self.sync_timer()                          # 静态图和动图刷新率不同
        return True

    def sync_timer(self):
        """静态图每秒一帧够了，动图要提到 20fps 左右"""
        want = 1000
        f = self.cfg.get("img_file")
        if f:
            p = os.path.join(IMGDIR, f)
            try:
                with Image.open(p) as im:
                    if getattr(im, "n_frames", 1) > 1:
                        want = 50
            except Exception:
                pass
        if want != self.interval:
            user32.KillTimer(self.hwnd, 1)
            user32.SetTimer(self.hwnd, 1, want, None)
            self.interval = want

    def work_area(self):
        """当前窗口所在显示器的工作区。
        不能用 SystemParametersInfoW(SPI_GETWORKAREA)，那个只认主显示器，
        窗口拖到副屏后会被判定为"戳出屏幕"然后每秒拽回主屏。"""
        mon = user32.MonitorFromWindow(self.hwnd, 2)      # NEAREST
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(mon, ctypes.cast(ctypes.byref(mi), ctypes.c_void_p)):
            return mi.rcWork
        wa = wt.RECT()
        user32.SystemParametersInfoW(0x0030, 0,
                                     ctypes.cast(ctypes.byref(wa), ctypes.c_void_p), 0)
        return wa

    def nudge_onscreen(self):
        """只在卡片尺寸变化后校正一次，不能每帧都做，否则会跟拖动循环打架。

        换图会改卡片尺寸（占宽是按图片比例算的），如果之前吸在右边或下边，
        尺寸一变就得重新贴——不然横图换成竖图时卡片变宽，右边那截会顶出
        屏幕；贴底的会压到任务栏上。所以这里先按记下的边重贴，再夹一次边界。
        """
        r = wt.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        wa = self.work_area()
        w, h = self.size
        pad = int(self.cfg.get("dock_pad", 12))
        dx, dy = self.cfg.get("dock_x", ""), self.cfg.get("dock_y", "")
        nx, ny = r.left, r.top
        if dx == "left":
            nx = wa.left + pad
        elif dx == "right":
            nx = wa.right - w - pad
        if dy == "top":
            ny = wa.top + pad
        elif dy == "bottom":
            ny = wa.bottom - h - pad
        # 没吸附的那个轴仍然要保证不出屏
        nx = max(wa.left, min(nx, wa.right - w))
        ny = max(wa.top, min(ny, wa.bottom - h))
        if (nx, ny) != (r.left, r.top):
            user32.SetWindowPos(self.hwnd, None, nx, ny, 0, 0,
                                SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOZORDER)

    def detect_dock(self):
        """拖动结束时判断有没有靠边。两个轴分开记，所以四个角也能吸住。

        判定用的是"卡片边缘到工作区边缘的距离"，阈值里加上 dock_pad——
        否则吸附完成后卡片离边缘正好是 pad，下次再拖一点点就会被判定成
        没吸附，表现为吸附状态自己丢掉。
        """
        if not self.cfg.get("dock", True):
            return
        r = wt.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        wa = self.work_area()
        pad = int(self.cfg.get("dock_pad", 12))
        snap = int(DOCK_SNAP * self.uiscale()) + pad
        dx = dy = ""
        if abs(r.left - wa.left) <= snap:
            dx = "left"
        elif abs(wa.right - r.right) <= snap:
            dx = "right"
        if abs(r.top - wa.top) <= snap:
            dy = "top"
        elif abs(wa.bottom - r.bottom) <= snap:
            dy = "bottom"
        self.cfg["dock_x"], self.cfg["dock_y"] = dx, dy
        write_cfg({"dock_x": dx, "dock_y": dy})
        self.nudge_onscreen()

    def show(self, v):
        user32.ShowWindow(self.hwnd, SW_SHOW if v else SW_HIDE)

    def quit(self):
        self.save_pos()
        # 托盘图标必须显式删掉。进程直接退出的话 Windows 不会自动清，
        # 图标会一直留在托盘里（"幽灵图标"），要等鼠标划过去探测才消失。
        # 之前只有托盘菜单的「退出」调了 icon.stop()，设置窗口的
        # 「退出程序」没调，每退一次就攒一个。
        try:
            if _TRAY[0] is not None:
                _TRAY[0].stop()
                _TRAY[0] = None
        except Exception:
            pass
        user32.PostMessageW(self.hwnd, WM_DESTROY, 0, 0)


# ============================ 设置窗口 ============================
_settings_open = threading.Event()
_settings_root = [None]          # 存一份 Tk root，好把已开的窗口提到前面


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
    W = clamp(cfg.get("cardw", 320), W_MIN, W_MAX, 320)
    IW_MIN, IW_MAX = 20, 70
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


TAB_KEYS = {
    "工作": ["start", "end", "salary", "cur_sym", "sym_gap", "work_text", "show_money"],
    "外观": ["bg", "ui", "cardw", "cardh", "glass", "top", "alttab", "auto_size", "dock", "dock_pad"],
    "图片": ["img_fill", "img_side", "text_pct", "iw", "iw_auto", "ix", "iy",
             "fade", "rotate_min", "shuffle", "auto_resize"],
    "倒计时": ["slots", "slot_rows"],
}


def place_settings(root, widget):
    """把设置窗口摆到组件旁边的空白处。

    组件是置顶的，设置窗口压在它上面就看不见改动效果，只能一边改一边挪窗口。
    尺寸要等控件都建完才准，所以这一步放在 mainloop 之前做，不能在 Tk() 之后
    就算——那时候 winfo 拿到的还是 1x1。
    """
    root.update_idletasks()
    w = max(root.winfo_reqwidth(), root.winfo_width())
    h = max(root.winfo_reqheight(), root.winfo_height())
    try:
        r = wt.RECT()
        user32.GetWindowRect(widget.hwnd, ctypes.byref(r))
        wa = widget.work_area()
    except Exception:
        return
    gap = 12
    for x, y in ((r.right + gap, r.top),          # 右
                 (r.left - w - gap, r.top),       # 左
                 (r.left, r.bottom + gap),        # 下
                 (r.left, r.top - h - gap)):      # 上
        x = max(wa.left, min(x, wa.right - w))
        y = max(wa.top, min(y, wa.bottom - h))
        # 夹回工作区之后可能又压到组件上了，得重新判一次相交
        if not (x < r.right and x + w > r.left and y < r.bottom and y + h > r.top):
            root.geometry("+%d+%d" % (x, y))
            return
    root.geometry("+%d+%d" % (wa.left, wa.top))   # 四边都塞不下，贴左上角


def open_settings(widget):
    if _settings_open.is_set():
        # 已经开着：可能被最小化或压在下面了，提到最前而不是什么都不做
        root = _settings_root[0]
        if root is not None:
            def raise_it():
                try:
                    root.deiconify()
                    root.lift()
                    root.focus_force()
                except Exception:
                    pass
            try:
                root.after(0, raise_it)
            except Exception:
                pass
        return
    _settings_open.set()

    def run():
        import traceback
        try:
            _build(widget)
        except Exception:
            traceback.print_exc()
        _settings_root[0] = None
        _settings_open.clear()

    threading.Thread(target=run, daemon=True).start()


def _build(widget):
    import tkinter as tk
    from tkinter import ttk, filedialog

    cfg = read_cfg()
    ns_defaults = dict(DEFAULTS)
    root = tk.Tk()
    _settings_root[0] = root
    root.title("下班倒计时 · 设置")
    root.resizable(False, False)
    try:            # 先粗放一下，保证开在组件所在的那块屏上，多屏时别跑到主屏去
        r0 = wt.RECT()
        user32.GetWindowRect(widget.hwnd, ctypes.byref(r0))
        root.geometry("+%d+%d" % (r0.left, r0.top))
    except Exception:
        pass
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass

    dirty = {}
    VARS = {}          # 配置键 -> 更新对应控件的函数，重置时原地刷新用

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
        try:
            root.lift()          # 组件重绘后把设置窗口拉回前面
        except Exception:
            pass

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
            val = fix_astral(v.get())
            if cast is float:
                try:
                    val = float(val)
                except ValueError:
                    return
            dirty[key] = val
            commit(500)
        v.trace_add("write", on)
        VARS[key] = lambda val, v=v: v.set(str(val))
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
        VARS[key] = lambda val, v=v: v.set(float(val))
        return v

    def add_check(parent, r, label, key, default, cb=None):
        v = tk.BooleanVar(value=bool(cfg.get(key, default)))
        ttk.Checkbutton(parent, text=label, variable=v,
                        command=lambda: (dirty.__setitem__(key, v.get()),
                                         cb() if cb else None, commit(0))
                        ).grid(row=r, column=0, columnspan=3, sticky="w", pady=3)
        VARS[key] = lambda val, v=v: v.set(bool(val))
        return v

    # ================= 工作 =================
    t1 = ttk.Frame(nb, padding=12)
    nb.add(t1, text="工作")
    add_entry(t1, 0, "上班时间", "start", maxlen=5)
    add_entry(t1, 1, "下班时间", "end", maxlen=5)
    add_entry(t1, 2, "月薪", "salary", cast=float, maxlen=9)
    # 只放 BMP 内的符号。emoji 在 BMP 以外，Tcl/Tk 8.6 存不住也画不出，
    # 别再往这个列表里加。这个下拉框是可编辑的，列表里没有的自己敲。
    CUR = {"¥ 人民币 / 日元": "¥", "$ 美元": "$", "€ 欧元": "€", "£ 英镑": "£",
           "HK$ 港币": "HK$", "NT$ 新台币": "NT$", "S$ 新加坡元": "S$",
           "RM 马来西亚林吉特": "RM", "฿ 泰铢": "฿", "₫ 越南盾": "₫",
           "₱ 菲律宾比索": "₱", "Rp 印尼盾": "Rp", "₩ 韩元": "₩",
           "₹ 印度卢比": "₹", "₽ 卢布": "₽", "A$ 澳元": "A$", "C$ 加元": "C$",
           "CHF 瑞士法郎": "CHF", "¢ 分": "¢", "元": "元", "不显示符号": ""}
    crev = {v: k for k, v in CUR.items()}
    ttk.Label(t1, text="货币符号").grid(row=3, column=0, sticky="w", pady=4)
    curv = tk.StringVar(value=crev.get(cfg.get("cur_sym", "¥"),
                                       str(cfg.get("cur_sym", "¥"))))
    ttk.Combobox(t1, textvariable=curv, width=18,
                 values=list(CUR.keys())).grid(row=3, column=1, columnspan=2,
                                               sticky="we", pady=4)
    VARS["cur_sym"] = lambda val, v=curv, rv=crev: v.set(rv.get(val, str(val)))

    def on_cur(*_):
        t = curv.get()
        # 选的是预设就取它对应的符号；自己敲的就原样用，最多 4 个字符
        # 不 strip：有人就是想靠尾随空格拉开距离。全空白当成不显示。
        dirty["cur_sym"] = CUR[t] if t in CUR else (t[:4] if t.strip() else "")
        commit(500)
    curv.trace_add("write", on_cur)

    add_check(t1, 4, "符号与数字之间空一格", "sym_gap", False)
    add_entry(t1, 5, "上班文案", "work_text", maxlen=12)
    money_v = tk.BooleanVar(value=bool(cfg.get("show_money", True)))
    VARS["show_money"] = lambda val, v=money_v: v.set(bool(val))
    ttk.Checkbutton(t1, text="显示今日收入", variable=money_v,
                    command=lambda: (write_cfg({"show_money": money_v.get()}),
                                     widget.reload())
                    ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 0))
    ttk.Label(t1, text="货币符号可以直接在框里改，最多 4 个字符；汉字量词会自动放到\n"
                       "数字后面（288.91 元）。上班文案留空用「距下班」",
              foreground="#888", justify="left").grid(row=7, column=0, columnspan=3,
                                                      sticky="w")

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
    VARS["bg"] = lambda val, v=bgv, rv=bg_rev: v.set(rv.get(val, list(rv.values())[0]))
    bgv.trace_add("write", on_bg)

    add_scale(t2, 1, "整体缩放", "ui", 75, 150, 100)
    add_scale(t2, 2, "卡片宽度", "cardw", 180, 720, 320)
    add_scale(t2, 3, "卡片高度", "cardh", 90, 640, 104)
    ttk.Button(t2, text="正方形", width=8,
               command=lambda: (write_cfg({"cardw": 300, "cardh": 300}),
                                widget.reload())
               ).grid(row=4, column=2, sticky="e", pady=(2, 0))
    gl = add_scale(t2, 5, "玻璃浓度", "glass", 14, 230, 90)

    def glass_row(show):
        for w in t2.grid_slaves(row=5):
            w.grid_remove() if not show else w.grid()
    glass_row(cfg.get("bg") == "glass")

    add_check(t2, 6, "窗口置顶", "top", True)
    as_v = tk.BooleanVar(value=bool(cfg.get("auto_size", False)))
    VARS["auto_size"] = lambda val, v=as_v: v.set(bool(val))
    ttk.Checkbutton(t2, text="按图片比例自动调整卡片尺寸（贴三边不裁切）",
                    variable=as_v,
                    command=lambda: (write_cfg({"auto_size": as_v.get()}),
                                     widget.reload())
                    ).grid(row=10, column=0, columnspan=3, sticky="w", pady=3)

    alt_v = tk.BooleanVar(value=bool(cfg.get("alttab", False)))
    VARS["alttab"] = lambda val, v=alt_v: v.set(bool(val))
    ttk.Checkbutton(t2, text="在 Alt+Tab 中显示", variable=alt_v,
                    command=lambda: (write_cfg({"alttab": alt_v.get()}),
                                     widget.apply_alttab())
                    ).grid(row=7, column=0, columnspan=3, sticky="w", pady=3)
    ttk.Button(t2, text="回到屏幕中央", command=widget.center
               ).grid(row=8, column=0, columnspan=3, sticky="we", pady=(8, 0))

    auto_v = tk.BooleanVar(value=os.path.exists(STARTUP_VBS))
    ttk.Checkbutton(t2, text="开机自启", variable=auto_v,
                    command=lambda: set_autostart(auto_v.get())
                    ).grid(row=9, column=0, columnspan=3, sticky="w", pady=3)

    dock_v = tk.BooleanVar(value=bool(cfg.get("dock", True)))
    VARS["dock"] = lambda val, v=dock_v: v.set(bool(val))
    ttk.Checkbutton(t2, text="拖到屏幕边缘时自动吸附（换图变尺寸后重新贴边）",
                    variable=dock_v,
                    command=lambda: (write_cfg({"dock": dock_v.get()}),
                                     widget.reload())
                    ).grid(row=11, column=0, columnspan=3, sticky="w", pady=3)
    add_scale(t2, 12, "贴边留白", "dock_pad", 0, 40, 12)

    # ================= 图片 =================
    t3 = ttk.Frame(nb, padding=12)
    nb.add(t3, text="图片")
    lbl = ttk.Label(t3, text="", foreground="#888")
    lbl.place(x=0, y=0)

    def refresh_img_label():
        pool = image_pool()
        if not pool:
            lbl.config(text="未导入图片")
        else:
            cur = widget.cfg.get("img_file") or pool[0]
            lbl.config(text="图片池 %d 张，当前 %s" % (len(pool), cur))

    def pick():
        ps = filedialog.askopenfilenames(
            parent=root,
            filetypes=[("图片 / 动图", "*.png *.jpg *.jpeg *.gif *.webp *.bmp *.apng")])
        if not ps:
            return
        exist = set(image_pool())
        n = 0
        for p in ps:
            ext = os.path.splitext(p)[1].lower()
            while True:                          # 找个没被占用的文件名
                n += 1
                name = "bg%03d%s" % (n, ext)
                if name not in exist:
                    break
            try:
                shutil.copyfile(p, os.path.join(IMGDIR, name))
                exist.add(name)
            except Exception as e:
                print("copy failed:", e)
        pool = image_pool()
        c = read_cfg()
        if pool and c.get("img_file") not in pool:
            c["img_file"] = pool[0]
            if c.get("auto_resize"):             # 默认关闭，免得把手调好的尺寸冲掉
                c = auto_fit(c)
        write_cfg(c)
        refresh_img_label()
        widget.reload()

    def clear():
        for old in os.listdir(IMGDIR):
            try:
                os.remove(os.path.join(IMGDIR, old))
            except OSError:
                pass
        write_cfg({"img_file": ""})      # 不动卡片尺寸，正方形不该被打回长条
        refresh_img_label()
        widget.reload()

    def step_img(delta):
        pool = image_pool()
        if len(pool) < 2:
            return
        # 以"当前正在显示的那张"为基准，而不是 widget.idx——
        # 池子被增删过之后 idx 可能已经指到别的图上了
        cur = widget.cfg.get("img_file") or widget.cur
        base = pool.index(cur) if cur in pool else 0
        widget.idx = (base + delta) % len(pool)
        widget.cur = pool[widget.idx]
        widget.cfg["img_file"] = widget.cur
        widget.last_switch = time.time()      # 手动翻页后重新计轮换间隔
        widget.sync_timer()
        widget.paint()
        refresh_img_label()

    def refit():
        write_cfg(auto_fit(read_cfg()))
        widget.reload()

    bar = ttk.Frame(t3)
    bar.grid(row=0, column=0, columnspan=3, sticky="we", pady=(18, 0))
    ttk.Button(bar, text="导入(可多选)", command=pick, width=11).grid(row=0, column=0, padx=2)
    ttk.Button(bar, text="上一张", command=lambda: step_img(-1),
               width=7).grid(row=0, column=1, padx=2)
    ttk.Button(bar, text="下一张", command=lambda: step_img(1),
               width=7).grid(row=0, column=2, padx=2)
    ttk.Button(bar, text="清空", command=clear, width=6).grid(row=0, column=3, padx=2)
    ttk.Button(bar, text="按比例调整", command=refit, width=11).grid(row=0, column=4, padx=2)
    FILL = {"贴三边（会裁切）": "cover", "完整显示（留白）": "fit"}
    frev = {v: k for k, v in FILL.items()}
    ttk.Label(t3, text="填充").grid(row=4, column=0, sticky="w", pady=4)
    fillv = tk.StringVar(value=frev.get(cfg.get("img_fill", "cover"),
                                        "贴三边（会裁切）"))
    ttk.Combobox(t3, textvariable=fillv, width=20, state="readonly",
                 values=list(FILL.keys())).grid(row=4, column=1, columnspan=2,
                                                sticky="we", pady=4)
    VARS["img_fill"] = lambda val, v=fillv, rv=frev: v.set(rv.get(val, list(rv.values())[0]))
    fillv.trace_add("write", lambda *_: (write_cfg({"img_fill": FILL[fillv.get()]}),
                                         widget.reload()))

    add_scale(t3, 13, "文字区占比 %", "text_pct", 25, 65, 40)

    SIDE = {"靠左": "left", "居中": "center", "靠右": "right"}
    srev = {v: k for k, v in SIDE.items()}
    ttk.Label(t3, text="靠边").grid(row=5, column=0, sticky="w", pady=4)
    sidev = tk.StringVar(value=srev.get(cfg.get("img_side", "left"), "靠左"))
    ttk.Combobox(t3, textvariable=sidev, width=20, state="readonly",
                 values=list(SIDE.keys())).grid(row=5, column=1, columnspan=2,
                                                sticky="we", pady=4)
    VARS["img_side"] = lambda val, v=sidev, rv=srev: v.set(rv.get(val, list(rv.values())[0]))
    sidev.trace_add("write", lambda *_: (write_cfg({"img_side": SIDE[sidev.get()]}),
                                         widget.reload()))

    ar = tk.BooleanVar(value=bool(cfg.get("auto_resize", False)))
    VARS["auto_resize"] = lambda val, v=ar: v.set(bool(val))
    ttk.Checkbutton(t3, text="导入图片时按比例改卡片尺寸", variable=ar,
                    command=lambda: write_cfg({"auto_resize": ar.get()})
                    ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

    ROT = {"不轮换": 0, "每分钟": 1, "每 5 分钟": 5, "每 15 分钟": 15,
           "每小时": 60, "每 6 小时": 360, "每天": 1440}
    rrev = {v: k for k, v in ROT.items()}
    ttk.Label(t3, text="轮换").grid(row=2, column=0, sticky="w", pady=4)
    rotv = tk.StringVar(value=rrev.get(int(cfg.get("rotate_min", 0)), "不轮换"))
    ttk.Combobox(t3, textvariable=rotv, width=14, state="readonly",
                 values=list(ROT.keys())).grid(row=2, column=1, columnspan=2,
                                               sticky="we", pady=4)
    VARS["rotate_min"] = lambda val, v=rotv, rv=rrev: v.set(rv.get(val, list(rv.values())[0]))
    rotv.trace_add("write", lambda *_: (write_cfg({"rotate_min": ROT[rotv.get()]}),
                                        widget.reload()))
    shufv = tk.BooleanVar(value=bool(cfg.get("shuffle", True)))
    VARS["shuffle"] = lambda val, v=shufv: v.set(bool(val))
    ttk.Checkbutton(t3, text="随机顺序（取消则按文件名依次播放）", variable=shufv,
                    command=lambda: (write_cfg({"shuffle": shufv.get()}),
                                     widget.reload())
                    ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 4))

    iwa = tk.BooleanVar(value=bool(cfg.get("iw_auto", True)))
    VARS["iw_auto"] = lambda val, v=iwa: v.set(bool(val))
    ttk.Checkbutton(t3, text="自动占宽（文字用多少留多少，其余给图片）",
                    variable=iwa,
                    command=lambda: (write_cfg({"iw_auto": iwa.get()}), widget.reload())
                    ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 2))
    add_scale(t3, 7, "手动占宽 %", "iw", 20, 70, 45)
    add_scale(t3, 8, "水平偏移", "ix", -300, 300, 0)
    add_scale(t3, 9, "垂直偏移", "iy", -300, 300, 0)
    ttk.Button(t3, text="偏移归零", width=10,
               command=lambda: (write_cfg({"ix": 0, "iy": 0}), widget.reload())
               ).grid(row=10, column=2, sticky="e", pady=(2, 0))
    add_scale(t3, 11, "淡出", "fade", 0, 100, 22)
    ttk.Label(t3, text="横图贴左右上三边，竖图贴上下和一侧。文字区占比控制另一边留多少",
              foreground="#888").grid(row=12, column=0, columnspan=3,
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
                    save_slots(s)      # write_cfg 里统一做 astral 清洗
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
    rowbox = ttk.Frame(t4)
    rowbox.grid(row=2, column=0, sticky="we", pady=(10, 0))
    add_scale(rowbox, 0, "最多占几行", "slot_rows", 1, 4, 2)
    ttk.Label(t4, text="最多 6 项，放不下自动换行；超过上面设的行数就不显示了。\n"
                       "过期的不显示，勾「每年」可年年重复",
              foreground="#888").grid(row=3, column=0, sticky="w", pady=(6, 0))
    draw_slots()

    def reset_tab(name):
        """只重置当前页签涉及的键，别的设置不动。
        原地更新控件，不销毁重建窗口——那样一闪一跳，像是出了故障。"""
        from tkinter import messagebox
        if not messagebox.askokcancel(
                "恢复默认", "把「%s」页的设置恢复成默认值？\n其他页签不受影响。" % name,
                parent=root):
            return
        patch = {k: ns_defaults[k] for k in TAB_KEYS[name] if k in ns_defaults}
        write_cfg(patch)
        for k, val in patch.items():
            fn = VARS.get(k)
            if fn:
                try:
                    fn(val)
                except Exception:
                    pass
        if "slots" in patch:
            draw_slots()
        if "bg" in patch:
            glass_row(patch["bg"] == "glass")
        if "img_fill" in patch or "iw_auto" in patch:
            pass
        dirty.clear()              # 控件回填会触发 trace，别让旧值又写回去
        widget.reload()

    for tab_name, frame in (("工作", t1), ("外观", t2), ("图片", t3), ("倒计时", t4)):
        row = frame.grid_size()[1]
        ttk.Button(frame, text="恢复本页默认", width=14,
                   command=lambda n=tab_name: reset_tab(n)
                   ).grid(row=row + 1, column=0, columnspan=3,
                          sticky="e", pady=(12, 0))

    # ---------- 底部 ----------
    # 保存失败以前只往 stdout 打一行，pythonw 下完全看不见，
    # 表现就是"改了没反应"。这里摆到界面上。
    err = ttk.Label(root, text="", foreground="#c33")
    err.pack(fill="x", padx=12)

    def poll_err():
        err.config(text=("保存失败：" + _LAST_SAVE_ERR[0]) if _LAST_SAVE_ERR[0] else "")
        try:
            root.after(800, poll_err)
        except Exception:
            pass
    poll_err()

    foot = ttk.Frame(root)
    foot.pack(fill="x", padx=10, pady=(0, 10))
    ttk.Button(foot, text="隐藏到托盘",
               command=lambda: widget.show(False)).pack(side="left")
    ttk.Button(foot, text="退出程序",
               command=lambda: (root.destroy(), widget.quit())).pack(side="left", padx=6)
    ttk.Button(foot, text="关闭", command=root.destroy).pack(side="right")

    root.protocol("WM_DELETE_WINDOW", root.destroy)
    place_settings(root, widget)
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
_TRAY = [None]           # 托盘图标的引用，退出时要用它删图标


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
        pystray.MenuItem("退出", lambda i, *_: widget.quit()),
    ))
    _TRAY[0] = icon
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
