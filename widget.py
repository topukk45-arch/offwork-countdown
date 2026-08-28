# -*- coding: utf-8 -*-
"""
下班倒计时 · 自绘版
分层窗口 (UpdateLayeredWindow) + Pillow 渲染，不使用 WebView2。
依赖: pip install pillow pystray
运行: pythonw widget.py
"""
import colorsys
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
BIG_MAX = 0.33      # 主时间最多占文字区高度的比例。没有这条线的话，"撑满"就是
                    # 一路放到高度装不下为止：文字区 300 高，主时间能长到 130px，
                    # 再反推需要多宽的文字列，卡片会被撑成一块广告牌
FIT_GROW_H = 1.6    # 竖版自动加高最多到设定高度的几倍。比横版收得紧：加高是
                    # 为了在图片下面腾出一条放文字，不是把方卡拉成长条。2.5 倍
                    # 时 300x300 会变成 300x592，早就不是"近似正方形"了
FIT_GROW = 2.5      # 横版自动加宽最多加到设定宽度的几倍。加宽是为了省得手拖滑块，
                    # 不是让滑块作废：不封顶的话，某些图片比例下它会一路顶到
                    # W_MAX，你设的宽高就完全不起作用了
IMG_MAX = 0.60      # 图片最多占卡片的几成（宽或高，看排法）。以前是个滑块，
                    # 可它只在图片按比例算得过大时才起作用，平时纹丝不动，
                    # 摆在设置里徒增困惑
SLOT_MAX = 3        # 倒计时最多几项。这是个桌面小组件，不是日程表——
                    # 六项在小卡片上本来也塞不下，砍行之后照样只显示三两条
DOCK_SNAP = 28      # 拖动结束时离边缘小于这个距离就算吸附
RADIUS = 16
TEXT_CR = 4.5       # 次要文字和金额相对卡片底色的最低对比度（WCAG AA 正文档）。
                    # 这两个颜色是一路混合出来的：次要文字先掺 45% 底色，开了
                    # 取色再掺 38% 主色，叠完离底色只剩不到三成，而全程没有下限。
                    # 浅色卡配暖色图时实测掉到 3.2，金额在默认浅蓝强调色下更是
                    # 只有 2.0——那一档跟取色无关，白底浅蓝一直就看不清。

DEFAULTS = {
    "start": "09:30", "end": "18:30", "salary": 12000,
    "work_text": "距下班",
    "cur_sym": "¥",            # 货币符号，从设置里的下拉框选
    "sym_gap": False,          # 符号和数字之间空一格
    "show_money": True,
    "show_cap": True,          # 是否显示「距下班 · 18:30」那一行
    "bg": "auto",              # auto | light | dark | glass
    "glass": 90,
    "ui": 100,                 # 缩放百分比
    "cardw": 320, "cardh": 104,
    # 图片在文字的哪一侧。含义随排法变：竖版是图在上 / 图在下，横版是图在左 /
    # 图在右。图片没占满的那一维一律居中，不给选——靠一边不好看。
    "img_side": "left",        # left = 图在上 / 图在左，right = 图在下 / 图在右
    "img_file": "", "fade": 22,
    "tint": False,             # 卡片配色跟随当前图片
    "tint_amt": 14,            # 主色掺进底色的比例 %
    "slot_rows": 2,            # 倒计时最多占几行，多出来的不显示
    "slot_each": False,        # 每条倒计时单独一行，不挤在一起
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


def ink_box(d, text, font):
    """文字实际墨迹的包围盒 (左, 上, 右, 下)，相对绘制原点。

    Pillow 的 text() 是按 em 框的原点画的，而不同字体的左边距和上下留白
    差很多：Bahnschrift 的数字左边几乎没有空隙，微软雅黑的汉字自带一圈。
    同一个 x 画出来，"0" 和 "距" 的左边缘就是对不齐的；同一套行距，
    数字行和汉字行之间看着也忽大忽小。按墨迹框排就没这问题。
    """
    try:
        b = font.getbbox(str(text))
        if b and b[2] > b[0] and b[3] > b[1]:
            return b[0], b[1], b[2], b[3]
    except Exception:
        pass
    w = int(d.textlength(str(text), font=font))
    return 0, 0, w, font.size


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
_FIT_WHY = [""]      # 自动加宽这一帧的结论，设置窗口把它显示出来
_STACK = [False]     # 这一帧是不是竖版（图在上）。设置窗口拿它切「靠边」的措辞
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
    key = (path, mt, bw, bh, int(cfg.get("fade", 45)), vertical, flip)
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
    """把图片放进目标框。

    目标框的宽高比是照着图片算出来的，所以这里等比缩放就正好填满，不裁。
    取整会差一两像素，居中放，两边各分半个像素的误差。
    淡出方向永远朝着文字那一侧，由 vertical / flip 两个参数定。
    """
    k = min(bw / src.width, bh / src.height)
    src = src.resize((max(1, int(round(src.width * k))),
                      max(1, int(round(src.height * k)))), Image.LANCZOS)
    out = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    out.paste(src, ((bw - src.width) // 2, (bh - src.height) // 2), src)

    fade = max(0, min(100, int(cfg.get("fade", 45)))) / 100
    if fade > 0:
        if vertical:                      # 纵向：往文字那一侧淡出
            n = bh
            grad = Image.new("L", (1, n))
            span = max(1, n * fade)
            for i in range(n):
                # flip=True 表示图片在下方，文字在上，要往上淡出
                t = i if flip else (n - 1 - i)
                grad.putpixel((0, i), int(255 * min(1.0, t / span)))
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


_TINT_CACHE = {}


def dominant_color(path):
    """取图片主色。

    降到 64px 再统计，按 24 一档粗量化把同色系归成一堆。跳过透明像素，
    也跳过接近灰白和接近纯黑的颜色——表情包大多是白底加黑描边，不排除的话
    取出来永远是白色或黑色，配色等于没变。
    """
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return None
    if key in _TINT_CACHE:
        return _TINT_CACHE[key]
    try:
        with Image.open(path) as src:
            src.seek(0)                       # 动图只看第一帧
            im = src.convert("RGBA")
            im.thumbnail((64, 64))
            px = [q for q in im.getdata() if q[3] > 128]
    except Exception:
        return None
    if not px:
        return None
    buckets = {}
    for cr, cg, cb, _ in px:
        k = (cr // 24, cg // 24, cb // 24)
        acc = buckets.setdefault(k, [0, 0, 0, 0])
        acc[0] += cr
        acc[1] += cg
        acc[2] += cb
        acc[3] += 1
    best, best_score = None, -1.0
    fallback, fallback_n = None, -1
    for acc in buckets.values():
        n = acc[3]
        cr, cg, cb = acc[0] // n, acc[1] // n, acc[2] // n
        if n > fallback_n:
            fallback, fallback_n = (cr, cg, cb), n
        _, li, sa = colorsys.rgb_to_hls(cr / 255, cg / 255, cb / 255)
        if sa < 0.18 or li < 0.12 or li > 0.92:
            continue
        score = n * (0.5 + sa)                # 面积为主，鲜艳度加权
        if score > best_score:
            best, best_score = (cr, cg, cb), score
    out = best or fallback
    if len(_TINT_CACHE) > 64:
        _TINT_CACHE.clear()
    _TINT_CACHE[key] = out
    return out


def fit_accent(rgb, dark):
    """把主色调到在当前深浅底色上读得清的亮度和饱和度。
    直接用原色的话，浅色卡片配上浅黄的图会得到一行看不见的字。"""
    hu, li, sa = colorsys.rgb_to_hls(*[c / 255 for c in rgb])
    sa = max(sa, 0.45)
    li = max(li, 0.62) if dark else min(li, 0.42)
    return tuple(int(c * 255) for c in colorsys.hls_to_rgb(hu, li, sa))


def _lum(c):
    """WCAG 相对亮度"""
    out = []
    for v in c[:3]:
        v /= 255.0
        out.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def contrast(a, b):
    la, lb = _lum(a), _lum(b)
    if la < lb:
        la, lb = lb, la
    return (la + 0.05) / (lb + 0.05)


def _hls(h, li, sa):
    li = max(0.0, min(1.0, li))
    return tuple(int(round(c * 255)) for c in colorsys.hls_to_rgb(h, li, sa))


def ensure_cr(rgb, bg, need=TEXT_CR):
    """把颜色推到相对底色至少 need 的对比度，保住色相和饱和度。

    只动亮度，不往黑白里掺。掺白掺黑等于把配色洗掉一半——取色开着的意义
    就是让卡片带上图片的颜色，为了看清先把颜色兑没了，那不如不取。
    两个方向都试，先要变暗那一档（改动量小的优先），推到头仍然不够就取
    对比度最高的那个端点——底色本身落在中间调时会走到这一步。
    """
    if contrast(rgb, bg) >= need:
        return rgb
    hu, li, sa = colorsys.rgb_to_hls(*[c / 255.0 for c in rgb])
    best, best_cr = rgb, contrast(rgb, bg)
    for target in (0.0, 1.0):
        lo, hi, hit = 0.0, 1.0, None
        for _ in range(14):
            mid = (lo + hi) / 2
            cand = _hls(hu, li + (target - li) * mid, sa)
            if contrast(cand, bg) >= need:
                hit, hi = cand, mid
            else:
                lo = mid
        if hit is not None:
            return hit
        end = _hls(hu, target, sa)
        if contrast(end, bg) > best_cr:
            best, best_cr = end, contrast(end, bg)
    return best


ROW_KEYS = ("big", "cap", "mon", "slot")
ALIGNS = ("left", "center", "right")


def row_plan(cfg):
    """返回 [(行键, 对齐)]。缺的补齐、重复的丢掉、非法值退回默认——
    配置是用户可以手改的文件，渲染里不能假设它一定合法。"""
    out, seen = [], set()
    for it in cfg.get("rows") or []:
        k = (it or {}).get("k")
        if k in ROW_KEYS and k not in seen:
            a = (it or {}).get("align", "left")
            out.append((k, a if a in ALIGNS else "left"))
            seen.add(k)
    for k in ROW_KEYS:
        if k not in seen:
            out.append((k, "left"))
    return out


def render(cfg, theme, clock_ms=0, rects=None):
    scale = max(0.75, min(1.5, float(cfg.get("ui", 100)) / 100))
    W = int(clamp(cfg.get("cardw", 320), W_MIN, W_MAX, 320) * scale)
    H = int(clamp(cfg.get("cardh", 104), H_MIN, H_MAX, 104) * scale)
    r = int(RADIUS * scale)
    pad = int(18 * scale)

    # 手动微调倍率。字号仍然先按文字区高度自动推一遍，再乘这些系数，
    # 之后照样走收缩流程——所以调过头也只是被压回来，不会溢出卡片。
    # 字号微调和行距以前是五个滑块。排版是算出来的，算得好就不需要手调，
    # 算得不好该改算法。留成常量，公式里那些乘法原样保留，改起来只动这一行。
    fs_big = fs_cap = fs_mon = fs_slot = gp = 1.0

    dark = theme["dark"] if cfg["bg"] in ("auto", "glass") else (cfg["bg"] == "dark")
    glass = cfg["bg"] == "glass"
    accent = theme["accent"]

    # 配色跟随当前图片：强调色直接换成图片主色，底色掺一点点进去。
    # 底色只掺一小撮就够——掺多了卡片会变成一块彩色板，文字对比度掉得厉害。
    tint = None
    if cfg.get("tint") and cfg.get("img_file"):
        _tp = os.path.join(IMGDIR, cfg["img_file"])
        if os.path.exists(_tp):
            tint = dominant_color(_tp)
    if tint:
        accent = fit_accent(tint, dark)
    amt = (clamp(cfg.get("tint_amt", 14), 0, 40, 14) / 100) if tint else 0

    def _tinted(c):
        if not amt:
            return c
        return tuple(int(x * (1 - amt) + y * amt) for x, y in zip(c, tint))

    if glass:
        # 下限不能是 0：分层窗口里 alpha=0 的像素鼠标是穿透的，
        # 全透明就等于整张卡点不中，既拖不动也打不开设置。1 就够了，
        # 底色几乎看不见但像素仍然可命中；文字本来就是不透明画的，照样清楚。
        a = max(1, min(255, int(cfg.get("glass", 90))))
        base = _tinted((26, 26, 26) if dark else (242, 242, 242)) + (a,)
    else:
        base = _tinted((25, 28, 33) if dark else (255, 255, 255)) + (255,)

    # 文字必须完全不透明。"变灰"靠往底色方向混合，不能靠降 alpha——
    # 在半透明卡片上降 alpha 等于让桌面直接透过文字。
    fg_rgb = (255, 255, 255) if dark else (16, 18, 22)
    if tint:
        # 主文字也掺一点主色。只给次要文字上色的话，时分是纯黑、秒和说明
        # 却带着颜色，看着像只做了一半。主文字掺得轻，18% 够了——
        # 掺重了时钟会变成一坨彩色，可读性掉得厉害。
        fg_rgb = tuple(int(x * 0.82 + y * 0.18) for x, y in zip(fg_rgb, accent))
    fg = fg_rgb + (255,)
    fg2 = tuple(int(x * 0.55 + y * 0.45) for x, y in zip(fg_rgb, base[:3])) + (255,)
    if tint:
        # 说明和倒计时那两行也往主色偏一点。文字是完全不透明画的，
        # 所以这条路径不受玻璃浓度影响，透明卡片上照样看得出配色。
        fg2 = tuple(int(x * 0.62 + y * 0.38)
                    for x, y in zip(fg2[:3], accent)) + (255,)
    # 上面那几个混合比例是按"好看"调的，没有一处保证读得清。这里统一兜一次底：
    # 达标就原样放行（绝大多数情况），不达标才推亮度。放在最后做，所以不管前面
    # 怎么混、混几层，出口只有这一个。
    # 毛玻璃下底色是半透明的，真正的背景是桌面，量不到；仍然按 base 算——
    # 卡片本来就是照着 base 配色的，这是能拿到的最好参照。
    fg2 = ensure_cr(fg2[:3], base[:3]) + (255,)
    accent = ensure_cr(accent, base[:3])     # 金额用的就是它

    im, shape = card_shape(W, H, r, base)
    probe = ImageDraw.Draw(im)

    plan = row_plan(cfg)
    big, sec, cap, money, parts = compute(cfg)
    show_money = bool(cfg.get("show_money", True))
    if not cfg.get("show_cap", True):
        cap = ""
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

    if rects is not None:
        # 卡片自身的尺寸，播种自由排版时拿它换算比例
        rects.append(("_card", 0, 0, W, H, 0))

    # ---------- 布局 ----------
    # 图片带的宽高比 == 图片本身的宽高比，所以永远等比放得下，不裁。
    # 两种摆法各算一遍，谁能把图片放得更大就用谁：
    #   两列（图在侧）图片高 = 卡片高，宽 = 卡片高 x 比例
    #   堆叠（图在上）图片宽 = 卡片宽，高 = 卡片宽 / 比例
    # 竖图自然落到两列，横图自然落到堆叠，不需要"卡片宽高比小于 1.55"这种魔数。
    #
    # 「文字区最少占比」是保险：图片按比例算出来太大时把它压回去，
    # 保证文字永远有地方待。平时不起作用。
    #
    # 剩下的宽度（或高度）全部归文字，不再按占比对半切——图片只拿它需要的。
    stack = False
    _STACK[0] = False  # 设置窗口拿它切措辞，无图时也得更新，不能留上一次的值
    img_fix = 0        # 两列布局下由图片比例定死的带宽，跟卡片宽度无关。加宽时要用
    bw = bh = bx = by = 0
    tx, ty, tw, th = pad, 0, W - pad * 2, H          # 文字区
    gap = int(9 * scale)                             # 图片和文字之间的小缝

    if has_img:
        room = IMG_MAX                                     # 图片最多占
        # 小图照样撑满，糊就糊。之前封在原始分辨率的 2 倍，结果一张 60px 的
        # 表情包在 340 高的卡片里只占 150，旁边空一大块——为了图片清楚牺牲版面，
        # 换来的是一张丑卡。取舍是：字要看清，图能看出是什么就行。
        img_fix = max(1, int(H * ratio))                   # 只由图片和卡片高决定
        cw = max(1, min(img_fix, int(W * room)))           # 两列：贴上下，宽受限
        ch = max(1, min(H, int(cw / max(0.05, ratio))))
        cw = max(1, min(cw, int(ch * ratio)))

        # 堆叠 + 自动加高时，高度已经是照着"图片铺满整宽 + 文字够用"算出来的，
        # 「文字区最少占比」那道保险再压一次就会把图片从 300 压回 288，白白
        # 差一口气铺不满。这时候只留一条底线：文字至少还有四分之一。
        room_s = max(room, 0.75) if True else room
        sh = max(1, min(int(W / max(0.05, ratio)), int(H * room_s)))  # 堆叠：贴左右
        sw = max(1, min(W, int(sh * ratio)))
        sh = max(1, min(sh, int(sw / max(0.05, ratio))))

        # 判据就是卡片形状：你把卡片设成什么样，就用哪种排法。
        #   宽 > 高  -> 图在左、字在右，不够宽就把卡片加宽
        #   高 >= 宽 -> 图在上、字在下，不够高就把卡片加高
        # 之前试过三版自动判据（谁让图片大 / 按图片朝向 / 谁让主时间大），
        # 每一版都在某类图上翻车，而且用户看不出它凭什么这么选。形状是用户
        # 自己定的，两种排法现在都能靠加宽或加高把文字撑开，那就交回给用户。
        # 用**你设的**尺寸判，不能用加宽加高之后算出来的：横版加宽会让 W 变大、
        # 竖版加高会让 H 变大，拿新值再判一次就会把自己判进另一种排法，然后
        # 越长越长。360x300 设的是横版，加高后变成 300x592 的长条就是这么来的。
        base_w = clamp(cfg.get("_fit_base", cfg.get("cardw", 320)),
                       W_MIN, W_MAX, 320)
        base_h0 = clamp(cfg.get("_fit_base_h", cfg.get("cardh", 104)),
                        H_MIN, H_MAX, 104)
        stack = base_h0 >= base_w
        _STACK[0] = stack
        # 「图片位置」管的是图片在文字的哪一侧：竖版图在上 / 图在下，
        # 横版图在左 / 图在右。图片没占满的那一维一律居中。
        to_end = cfg.get("img_side", "left") == "right"
        if stack:
            bw, bh = sw, sh
            bx = (W - bw) // 2                       # 横着没占满就居中
            if to_end:                               # 图在下，文字在上
                by = H - bh
                ty, th = 0, H - bh - gap
            else:                                    # 图在上，文字在下
                by = 0
                ty, th = bh + gap, H - bh - gap
            tw = W - pad * 2
        else:
            bw, bh = cw, ch
            by = (H - bh) // 2                       # 竖着没占满就居中
            if to_end:                               # 图在右，文字在左
                bx, tx = W - bw, pad
            else:                                    # 图在左，文字在右
                bx, tx = 0, bw + gap
            tw = W - bw - gap - pad

    if rects is not None:
        rects.append(("_text", tx, ty, tw, th, 0))   # 文字区，自由排版的坐标基准

    # ---------- 字号：按文字区高度和行数反推 ----------
    # 无图 + 接近正方形时也走竖排。两列布局会把内容压成中间一小块，
    # 正方形卡片上下各空一大片。
    tall_empty = (not has_img) and (W / max(1, H)) < 1.55
    # 堆叠布局下文字区一高，两行两列就不合适了——把行距拉开只是中间空一块，
    # 本质还是两行。够高就改成竖排四行，每行各占一档，才算真正铺开。
    stack_tall = stack and th >= int(112 * scale)
    single = (has_img and not stack) or tall_empty or stack_tall
    # 竖版一律走左右分栏（主时间+说明在左上，倒计时+金额在右下）；
    # 横版保持原样，由文字区高度在竖排单列和两行两列之间自动选。
    # 布局以前是个四选一的下拉，可"排得好不好看"该由算法负责，不该甩给用户。
    split = stack and tw > int(150 * scale)

    # ---------- 按文字反推卡片宽度 ----------
    # 字号是被文字列的宽度卡住的：列一窄，主时间放大到顶宽就停了，高度上剩多少
    # 都用不掉，只能摊成行距和上下留白。这里反过来算——先二分出高度能容下多大
    # 的字，再看那个字号下最宽的一行要多宽，不够就把卡片加宽，然后整个重画一遍。
    #
    # 放在这里是有讲究的：再往下就该合成图片了，那一步对动图是逐帧解码加缩放，
    # 重画一遍等于把整张 GIF 再处理一次。放在合成之前，重来的只是这段几何计算。
    #
    # 只在竖排单列下生效。两行两列那种同一行里左右两块字互相抢宽度，
    # 高度模型完全是另一套，套过来算出来的宽度没有意义。
    #
    # 图片在上（堆叠）时不做：那种布局下文字列本来就是整张卡宽，加宽只会把
    # 图片一起放大，越加越糟。
    if not cfg.get("_fit_pass"):
        if not True:
            _FIT_WHY[0] = ""
        elif not single and not stack:
            _FIT_WHY[0] = ("没加宽：当前是两行两列布局，"
                           "同一行里左右两块字互抢宽度，不适用")
    if os.environ.get("OFFWORK_DEBUG") and True:
        print("[fit] 开关=on single=%s stack=%s pass=%s"
              % (single, stack, cfg.get("_fit_pass", 0)))
    # ---------- 两条加宽/加高共用的估算器 ----------
    vis_k = [k for k, _a in plan
             if k == "big" or (k == "cap" and cap)
             or (k == "mon" and show_money) or (k == "slot" and parts)]
    guard0 = int(8 * scale) * 2
    n_slot = min(len(parts), int(clamp(cfg.get("slot_rows", 2), 1, SLOT_MAX, 2))) \
        if cfg.get("slot_each") else 1

    def _wide(t):
        """测宽用的替身：数字一律换成 8。

        秒数每秒变一次，字形宽度差一两像素，算出来的卡片宽度就跟着抖；
        宽度一抖，动图逐帧缓存的键跟着变，整张 GIF 每秒重新解码一遍——
        表现就是尺寸一卡一卡地跳。按最宽的数字量，结果就恒定了。
        """
        return "".join("8" if ch.isdigit() else ch for ch in str(t))

    def _need(pb):
        """给定主字号，返回 (块高, 最宽的一行要多宽)。

        倒计时不勾「每条单独一行」时按一行算：加宽之后本来就该收成一行，
        这个假设跟结果是自洽的。宽度取单条的最大值——再宽的列也不会把
        「发薪 13 天」这样一条拆开，它就是宽度的下限。
        """
        bsz = pb / (scale * max(0.01, fs_big))
        csz = max(9, min(24, bsz * 0.30))
        c_ = max(1, int(csz * scale * fs_cap))
        m_ = max(1, int(bsz * 0.52 * scale * fs_mon))
        r_ = max(8, int(csz * scale * fs_slot))
        fb = (F_TXT if has_cjk(big) else F_NUM)(pb)
        fsc = F_NUM(max(8, int(pb * 0.45)))
        hs, ws = [], []
        for k in vis_k:
            if k == "big":
                hs.append(pb)
                ws.append(text_w(probe, _wide(big), fb) + (
                    (int(6 * scale) + text_w(probe, _wide(sec), fsc))
                    if sec else 0))
            elif k == "cap":
                hs.append(c_)
                ws.append(text_w(probe, _wide(cap), F_TXT(c_)))
            elif k == "mon":
                hs.append(m_)
                fm2 = F_MON(m_)
                ws.append(text_w(probe, sym, _f_sym(fm2)) + _sym_gap(fm2)
                          + text_w(probe, _wide(money), fm2))
            else:
                fr = F_TXT(r_)
                hs.append(r_ * n_slot
                          + max(2, int(r_ * 0.5 * gp)) * (n_slot - 1))
                ws.append(max([text_w(probe, _wide(t), fr) for t in parts], default=0))
        g = max(2, int(c_ * 1.15 * gp))
        return sum(hs) + g * max(0, len(hs) - 1), max(ws or [0])

    def _wr_big():
        """主时间每 1px 字号占多少宽。反过来由宽度推字号时要用。"""
        ref = 100
        f = (F_TXT if has_cjk(big) else F_NUM)(ref)
        w = text_w(probe, _wide(big), f)
        if sec:
            w += int(6 * scale) + text_w(probe, _wide(sec), F_NUM(int(ref * 0.45)))
        return max(1.0, w) / ref

    # 堆叠这一半：图片铺满整个卡片宽度（顶满上左右三边），底部把卡片拉长放文字。
    # 加高不会改变图片大小——堆叠下图片宽 = 卡片宽、高 = 宽 / 比例，跟卡片高度
    # 无关，所以这条路没有"越长越要长"的回环。
    #
    # 目标跟横版那条路对称：横版是"加宽到文字不再嫌挤"，竖版就该是"加高到文字
    # 不再嫌矮"。之前写成"长到文字区正好等于最少占比"——那是下限不是舒服的尺寸，
    # 表现就是自动出来的字比手动拉一下还小。
    if (stack and has_img and True
            and int(cfg.get("_fit_pass", 0)) < 3):
        sh_full = max(1, int(W / max(0.05, ratio)))     # 铺满整宽时图片该多高
        base_h = clamp(cfg.get("_fit_base_h", cfg.get("cardh", 104)),
                       H_MIN, H_MAX, 104) * scale
        # 宽度能容下多大的主时间——堆叠下文字列就是整张卡宽，这个值不会变
        pb_w = int(max(12, (W - pad * 2) / _wr_big()))
        blk = _need(pb_w)[0]
        # 两条约束取严的：装得下所有行，且主时间不超过文字区高度的 BIG_MAX
        need_th = max(blk + guard0, int(pb_w / BIG_MAX))
        want_h = (int(sh_full + gap + need_th) + 7) // 8 * 8
        want_h = min(want_h, int(H_MAX * scale), int(base_h * FIT_GROW_H))
        if not cfg.get("_fit_pass"):
            _FIT_WHY[0] = ("已加高到 %dx%d（设定高度 %d）" % (W, want_h, int(base_h))
                           if want_h > H + 2 else
                           "没加高：图片已经铺满整宽，文字也放得下")
        if want_h > H + 2:
            c2 = dict(cfg)
            c2["cardh"] = int(want_h / scale)
            c2["_fit_base_h"] = cfg.get("_fit_base_h", cfg.get("cardh", 104))
            c2["_fit_pass"] = int(cfg.get("_fit_pass", 0)) + 1
            if rects is not None:
                del rects[:]
            return render(c2, theme, clock_ms, rects)

    if (single and not stack and True
            and int(cfg.get("_fit_pass", 0)) < 3 and tw > 0 and th > 0):
        lo3, hi3 = 12, max(13, int(th * BIG_MAX))
        while hi3 - lo3 > 1:
            mid = (lo3 + hi3) // 2
            if _need(mid)[0] <= th - guard0:
                lo3 = mid
            else:
                hi3 = mid
        need_w = _need(lo3)[1]
        if need_w <= tw + 2 and not cfg.get("_fit_pass"):
            _FIT_WHY[0] = ("没加宽：文字没被宽度卡住（文字列 %d，只需要 %d，"
                           "字号是被高度决定的）" % (tw, need_w))
        if os.environ.get("OFFWORK_DEBUG"):
            print("[fit] 文字列宽 tw=%d 需要 %d（高度能容下的主字号 %d）-> %s"
                  % (tw, need_w, lo3,
                     "加宽" if need_w > tw + 2 else "已经够宽，不动"))
        if need_w > tw + 2:
            # 直接解出需要多宽，不要拿斜率去推。
            #   文字列 = 卡片宽 - 图片带 - 固定开销(缝隙+内边距)
            #   图片带 = min(卡片宽 x (1-文字区占比), 封顶值)
            # 是分段线性的：图片带封顶之后，加出来的宽度全归文字列，这时还按
            # 占比折算就会多加一倍还多——卡片一路撞到上限，右边空一大条。
            # 文字列 = W - min(图片带定值, W x 图片最多占) - 固定开销，分段线性。
            # 两段各解一次，再验证解出来的宽度**自己**落在哪一段——不能拿当前
            # 宽度去判断走哪一段：眼下图片带还顶着占比线，等加宽完它早就由比例
            # 定死了，按占比那一段解出来会多加两百像素，右边空一大条。
            c_pad = max(0, W - bw - tw)
            tpct_ = 1 - IMG_MAX
            room_ = 1 - tpct_
            if not has_img:
                want = W + (need_w - tw)
            else:
                w_fix = need_w + img_fix + c_pad          # 图片带定死那一段
                w_pct = (need_w + c_pad) / max(0.15, tpct_)   # 顶着占比线那一段
                if img_fix and img_fix <= w_fix * room_:
                    want = w_fix
                elif img_fix > w_pct * room_:
                    want = w_pct
                else:
                    want = min(w_fix, w_pct)
            # 上限按"用户自己设的那个宽度"算，不是按上一轮加宽后的宽度——
            # 后者是自己乘自己，几轮下来还是没有边界。
            base = clamp(cfg.get("_fit_base", cfg.get("cardw", 320)),
                         W_MIN, W_MAX, 320) * scale
            newW = int(want)
            newW = (newW + 7) // 8 * 8          # 按 8 像素对齐，零头也别让它抖
            newW = min(newW, int(W_MAX * scale), int(base * FIT_GROW))
            if newW <= W + 2:
                if not cfg.get("_fit_pass"):
                    _FIT_WHY[0] = ("没加宽：已经到上限了（设定 %d，最多 %d）"
                                   % (int(base), min(int(W_MAX * scale),
                                                     int(base * FIT_GROW))))
            else:
                c2 = dict(cfg)
                c2["cardw"] = int(newW / scale)
                c2["_fit_base"] = cfg.get("_fit_base", cfg.get("cardw", 320))
                c2["_fit_pass"] = int(cfg.get("_fit_pass", 0)) + 1
                if rects is not None:
                    del rects[:]           # 这一遍记的坐标作废，重画的那遍会重记
                out = render(c2, theme, clock_ms, rects)
                if not cfg.get("_fit_pass"):
                    # 报最终尺寸，不是这一轮想要的尺寸——重画那一遍还可能再调一次。
                    _FIT_WHY[0] = ("已加宽到 %dx%d（设定宽度 %d）"
                                   % (out.size[0], out.size[1], int(base)))
                return out

    avail = th / scale - (14 if stack else 24)
    if tall_empty:
        avail = th / scale - 20
    if single:
        # 上限跟着可用高度走。写死 44 的话，250px 高的卡片主时间才 34px，
        # 跟卡片完全不成比例。
        hi_m, hi_n = (72, 88) if tall_empty else (64, 80)
        big_sz = (max(20, min(hi_m, avail / 2.9)) if show_money
                  else max(20, min(hi_n, avail / 2.2)))
    else:
        big_sz = (max(20, min(52, avail / 2.2)) if show_money
                  else max(20, min(58, avail / 1.7)))
    # 上下都要夹。只夹放大那一头不够——隐藏了金额和说明时公式本身就会算出
    # 超过这条线的初值，卡片跟着被撑成广告牌。
    big_sz = max(14, min(big_sz, th / scale * BIG_MAX))
    if has_cjk(big):
        big_sz *= 0.72        # 中文方块字比窄体数字宽得多，压一点才放得下
    mon_sz = big_sz * (0.52 if single else 0.58)
    cap_sz = max(9, min(24, big_sz * 0.30))

    f_big = (F_TXT if has_cjk(big) else F_NUM)(int(big_sz * scale * fs_big))
    f_sec = F_NUM(int(big_sz * 0.45 * scale * fs_big))
    f_cap = F_TXT(int(cap_sz * scale * fs_cap))
    f_mon = F_MON(int(mon_sz * scale * fs_mon))

    # 主字太宽就缩，窄卡片下很容易顶出去。秒数也要算进去。
    def _line_w2():
        w = text_w(probe, big, f_big)
        if sec:
            w += int(6 * scale) + text_w(probe, sec, f_sec)
        return w
    while _line_w2() > tw and big_sz > 14:
        big_sz -= 1
        f_big = (F_TXT if has_cjk(big) else F_NUM)(int(big_sz * scale * fs_big))
        f_sec = F_NUM(int(big_sz * 0.45 * scale * fs_big))
        # 金额和说明是按 big_sz 的比例定的，主字缩了它们必须跟着缩。
        # 之前只改 f_big / f_sec，f_mon 一直停在收缩前那一档，
        # 窄文字列里就会直接画出卡片边框（"¥257." 被截断）。
        mon_sz = big_sz * (0.52 if single else 0.58)
        cap_sz = max(9, min(24, big_sz * 0.30))
        f_mon = F_MON(int(mon_sz * scale * fs_mon))
        f_cap = F_TXT(int(cap_sz * scale * fs_cap))

    # 金额本身也要按宽度校验一次：它的字号是从主字高度推出来的，
    # 从来没量过自己有多宽，纯数字位数一多照样出框。
    if show_money:
        while money_w(probe, f_mon) > tw and f_mon.size > 9:
            f_mon = F_MON(f_mon.size - 1)

    # ---------- 画图片 ----------
    if has_img and bw > 0:
        try:
            # 淡出朝文字那一侧：竖版图在下时往上淡，横版图在左时往右淡
            frames, durs, total = load_frames(
                path, bw, bh, cfg, vertical=stack,
                flip=(to_end if stack else (bx == 0)))
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

    # ---------- 自由排版 ----------
    # 一切相对文字区（tx/ty/tw/th），不是相对整张卡片。锚点定位置、
    # 轴心定"用自己的哪条边去贴"，所以换图后文字区怎么变形都不会跑出框。
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
        if cfg.get("slot_each"):
            return list(items)          # 每条一行，不拼在一起
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

    # ---------- 左右分栏 ----------
    # 左栏放主时间和说明（顶对齐），右栏放倒计时和金额（右对齐、底对齐）。
    # 竖排单列是四行全左对齐，两行两列是主时间配金额、说明配倒计时；这一种
    # 是左上大字、右下次要信息，纵向错开，比前两种紧凑也更像"设计过"。
    # 自己一套字号协商，不走下面那套四行的收缩流程。
    if split:
        gapx = max(8, int(10 * scale))
        cap_txt = cap

        def _fit_split(bsz):
            """给定主字号，返回左右两栏的字体、行和尺寸"""
            fb = (F_TXT if has_cjk(big) else F_NUM)(max(8, int(bsz * scale * fs_big)))
            fs = F_NUM(max(6, int(bsz * 0.45 * scale * fs_big)))
            csz = max(9, min(24, bsz * 0.34))
            fc = F_TXT(max(8, int(csz * scale * fs_cap)))
            if cap_txt and text_w(d, cap_txt, fc) > tw:
                # 说明行自己太长就单独缩它，别让整栏的字号陪着一起降。
                # 文案是用户随手填的，可能比主时间长好几倍，凭它一个人
                # 把主时间拖到最小档不合理。
                fc = shrink(cap_txt, fc.size, tw)
            fm = F_MON(max(9, int(bsz * 0.42 * scale * fs_mon)))
            fr = F_TXT(max(8, int(csz * scale * fs_slot)))
            cap_n = int(clamp(cfg.get("slot_rows", 2), 1, SLOT_MAX, 2))
            lines = wrap(parts, fr, int(tw * 0.55))[:cap_n] if parts else []
            lw = max(text_w(d, big, fb)
                     + ((int(6 * scale) + text_w(d, sec, fs)) if sec else 0),
                     text_w(d, cap_txt, fc) if cap_txt else 0)
            rw = max(max([text_w(d, l, fr) for l in lines], default=0),
                     money_w(d, fm) if show_money else 0)
            gapl = max(2, int(fr.size * 0.45 * gp))
            # 高度一律按墨迹框算，不按字号。字号带着字体上下的留白，两栏用的
            # 字号又不一样，拿它对齐的话小字那栏总要低一截——就是"底不齐"。
            def _ih(t, f):
                b = ink_box(d, t, f)
                return b[3] - b[1], b[1]
            bh_, bo_ = _ih(big, fb)
            if sec:
                # 秒数是另一个字体、按 em 顶单独定位画的，落点比主时间低，墨迹
                # 还能再往下探一截。行高只按主时间算的话，这一截就是白捡的重叠——
                # 隐藏说明行时左栏只剩这一行，右栏正好顶上来撞。
                bh_ = max(bh_, int((fb.size - fs.size) * .75)
                          + ink_box(d, sec, fs)[3])
            lh = bh_
            ch_ = co_ = 0
            if cap_txt:
                ch_, co_ = _ih(cap_txt, fc)
                lh += int(fb.size * 0.30 * gp) + ch_
            rh = 0
            lo_ = 0
            if lines:
                l0h, lo_ = _ih(lines[0], fr)
                rh = (len(lines) - 1) * (fr.size + gapl) + l0h
            mh_ = mo_ = 0
            if show_money:
                mh_, mo_ = _ih(sym + money, fm)
                rh += (int(fm.size * 0.45 * gp) if lines else 0) + mh_
            return (fb, fs, fc, fm, fr, lines, gapl, lw, rw, lh, rh,
                    bo_, co_, lo_, mo_, bh_)

        # 两栏一个贴文字区顶、一个贴底。纵向错得开的时候它们压根不在同一行，
        # 各自都能用满整条宽度；只有纵向撞上了，才需要「左栏宽 + 缝 + 右栏宽」。
        # 以前不分情况一律按并排要求，等于替一个根本不存在的冲突买单——主时间
        # 被右下角那三行小字挤掉一大截，中间还空着一大片。
        mg = max(int(6 * scale), int(th * 0.06))
        avail_h = th - mg * 2          # 真正能画的那一段，top 到 bottom
        vgap = max(4, int(7 * scale))  # 纵向错开时两栏之间至少留这么点

        def _split_fits(st):
            lw, rw, lh, rh = st[7], st[8], st[9], st[10]
            if lh + rh + vgap <= avail_h:
                return lw <= tw and rw <= tw
            return lw + gapx + rw <= tw and max(lh, rh) <= avail_h

        # 上限按墨迹高算，不按字号。汉字的墨迹几乎等于字号，窄体数字只有七成，
        # 同一条 BIG_MAX 拿字号一刀切，「已下班」就会比走秒的时钟粗一大圈——
        # 原来那个 0.78 的 CJK 系数是在手工补这个差，换算一次就不用猜了。
        _pb = ink_box(d, big, (F_TXT if has_cjk(big) else F_NUM)(100))
        ink_k = max(0.1, (_pb[3] - _pb[1]) / 100.0)

        def _search():
            # 二分，不要一档一档往下减。以前是 for _ in range(80) 线性扫，高卡片上
            # 起点能到 120 以上，80 步用完还没扫到放得下的档位，循环就这么退出去了，
            # 带着一个从没验证过的字号往下画——主时间直接画到卡片外面。
            lo = 14
            hi = max(lo, int(min(th * BIG_MAX / ink_k, th - 20) / scale))
            if hi > lo and not _split_fits(_fit_split(hi)):
                while hi - lo > 1:
                    mid = (lo + hi) // 2
                    if _split_fits(_fit_split(mid)):
                        lo = mid
                    else:
                        hi = mid
                hi = lo
            return hi

        bsz = _search()
        if cap_txt and not _split_fits(_fit_split(bsz)):
            # 缩到字号下限还塞不下，说明行让位重来一次。跟竖排单列最后那条
            # 兜底一个路子：宁可少显示一行，也不能让两栏叠在一起。
            cap_txt = ""
            bsz = _search()
        (fb, fs, fc, fm, fr, lines, gapl, lw, rw, lh, rh,
         bo_, co_, lo_, mo_, bh_) = _fit_split(bsz)

        # 左栏贴文字区顶，右栏贴文字区底，中间空开——就是"左上右下"。
        # 之前给它加过前提（说明行、倒计时、收入三样齐全才错开，少一样就退回
        # 共用底线），结果隐藏了任何一样就永远看不到这个排法。去掉前提，一律错开。
        top = ty + mg
        bottom = ty + th - mg
        # 下面所有 y 都是"墨迹顶"，画的时候各自减掉自己的墨迹偏移
        ib = ink_box(d, big, fb)
        draw_text(d, (tx - ib[0], top - bo_), big, fb, fg)
        if sec:
            draw_text(d, (tx + text_w(d, big, fb) + int(6 * scale),
                          top + int((fb.size - fs.size) * .75)), sec, fs, fg2)
        y_cap = top + lh
        if cap_txt:
            ic = ink_box(d, cap_txt, fc)
            y_cap = top + lh - (ic[3] - ic[1])       # 左栏最后一行贴着左栏的底
            draw_text(d, (tx - ic[0], y_cap - ic[1]), cap_txt, fc, fg2)
        # 右栏贴同一条底线；整块贴右边，块内各行左对齐——逐行右对齐会让
        # 「周五 0 天」「发薪 13 天」「¥551.72」三行的左边参差不齐。
        ry = bottom - rh
        blk_x = tx + tw - rw
        yy = ry
        for i, line in enumerate(lines):
            draw_text(d, (blk_x, yy - lo_), line, fr, fg2)
            yy += fr.size + (gapl if i < len(lines) - 1 else 0)
        # 金额钉在右栏底线上，不从上面累加推下来。块高 rh 里最后一行倒计时记的是
        # 墨迹高，绘制却按字号步进，两者差着字体上下那圈留白；累加到金额时已经
        # 顶到 rh 之外，字号一大就直接压到卡片边上。
        _mb = ink_box(d, sym + money, fm) if show_money else None
        if show_money:
            draw_money(d, (blk_x, bottom - _mb[3]), fm, accent + (255,))

        if rects is not None:
            # 高度用行的实际墨迹高（含秒数探出的那一截），不是 em 框——
            # 播种自由排版时拿 em 框会比看得见的字大出一圈
            rects.append(("big", tx, top, int(lw), bh_, fb.size))
            if cap_txt:
                rects.append(("cap", tx, y_cap, int(text_w(d, cap_txt, fc)),
                              fc.size, fc.size))
            if lines:
                rects.append(("slot", int(blk_x), ry, int(rw),
                              int(len(lines) * fr.size + gapl * (len(lines) - 1)),
                              fr.size))
            if show_money:
                rects.append(("mon", int(blk_x), bottom - (_mb[3] - _mb[1]),
                              int(money_w(d, fm)), _mb[3] - _mb[1], fm.size))
        if cfg.get("_hover"):
            cx, cy = W - int(16 * scale), int(14 * scale)
            rr2 = max(1, int(1.6 * scale))
            for kk in (-1, 0, 1):
                ox = cx + kk * int(6 * scale)
                d.ellipse((ox - rr2, cy - rr2, ox + rr2, cy + rr2), fill=fg2)
        return im

    room_r = tw if single else max(60, min(int(tw * 0.62), tw))

    # 倒计时最多占几行。没有这个上限的话，加满 6 项就会折成四五行，
    # 后面的收缩循环为了把它们全塞进去会把主时间一起压到最小档，
    # 结果整张卡片全是小字——宁可少显示两条，也不能让主显示区失真。
    slot_cap = int(clamp(cfg.get("slot_rows", 2), 1, SLOT_MAX, 2))
    CAPR_MIN = 10          # 倒计时字号下限，再小就不是给人看的了

    def rewrap(f):
        return wrap(parts, f, room_r)[:slot_cap] if parts else []

    f_capr = F_TXT(max(8, int(cap_sz * scale * fs_slot)))
    slot_lines = rewrap(f_capr)
    while slot_lines and any(text_w(d, l, f_capr) > room_r for l in slot_lines) \
            and f_capr.size > CAPR_MIN:
        f_capr = F_TXT(f_capr.size - 1)
        slot_lines = rewrap(f_capr)

    g_big = int(f_big.size * 0.34 * gp)
    g_mid = int(f_mon.size * 0.42 * gp)
    slot_gap = max(2, int(f_capr.size * 0.5 * gp))

    def _row_heights(fc, lines):
        """竖排单列下每个可见行的高度，顺序按配置。measure 和实际绘制
        共用这一份，两边就不会像以前金额那样各算各的、算着算着对不上。"""
        out = []
        for k, _a in plan:
            if k == "big":
                out.append(f_big.size)
            elif k == "cap" and cap:
                out.append(f_cap.size)
            elif k == "mon" and show_money:
                out.append(f_mon.size)
            elif k == "slot" and lines:
                out.append(len(lines) * fc.size
                           + max(2, int(fc.size * 0.5 * gp)) * (len(lines) - 1))
        return out

    def measure(fc, lines):
        """返回 (总高, 可撑开的间距合计)。用返回值传，别用模块全局——
        那样两次渲染之间会串值。"""
        if single:
            # 行序可调，所以高度也按"可见行列表"算，不再写死四行的加法。
            # 行距按边界序号取：默认顺序下取到的正好是原来那三个值。
            hs = _row_heights(fc, lines)
            gl = [g_big, int(fc.size * 0.9), g_mid]
            rows = sum(hs)
            gaps = sum(gl[:max(0, len(hs) - 1)])
        else:
            slot_h = (len(lines) * fc.size + slot_gap * max(0, len(lines) - 1)) if lines else 0
            rows = max(f_big.size, f_mon.size if show_money else 0) \
                   + max(f_cap.size if cap else 0, slot_h)
            gaps = g_big
        return rows + gaps, gaps

    guard = int(8 * scale) * 2
    block, _gaps_sum = measure(f_capr, slot_lines)
    while block > th - guard and f_capr.size > CAPR_MIN:
        f_capr = F_TXT(f_capr.size - 1)
        slot_lines = rewrap(f_capr)
        slot_gap = max(2, int(f_capr.size * 0.5 * gp))
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
            slot_gap = max(2, int(f_capr.size * 0.5 * gp))
            shrunk = True
        elif f_cap.size > 10 or (show_money and f_mon.size > 11):
            if f_cap.size > 10:
                f_cap = F_TXT(f_cap.size - 1)
            if show_money and f_mon.size > 11:
                f_mon = F_MON(f_mon.size - 1)
            shrunk = True
        elif f_big.size > 14:
            big_sz = max(14, f_big.size / scale - 1)
            f_big = (F_TXT if has_cjk(big) else F_NUM)(int(big_sz * scale))
            f_sec = F_NUM(max(8, int(big_sz * 0.45 * scale)))
            shrunk = True
        g_big = int(f_big.size * 0.34 * gp)
        g_mid = int(f_mon.size * 0.42 * gp)
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

    # ---------- 反向撑满 ----------
    # 上面那几个字号上限（64 / 72 / 52）是按扁卡片定的。卡片一高，算出来的
    # 字号仍然停在上限，多出来的高度全摊给行距，看着就是几行小字浮在中间。
    # 这里反过来再推一次：宽高都还有余量就整体加一档，直到某一维顶住。
    # 只放大不缩小，所以跟前面整套收缩逻辑不会打架。
    if single:
        def _plan_h(fb, fc, fm, fr, lines, gap):
            """这套字体在竖排单列下的最小占高。跟下面真正排版用的是同一套算法：
            行高取墨迹框，行距取默认那一档。measure() 是给两列和 L 形用的，
            行高按字号、三段行距各取各的，拿它判断还能不能放大会偏乐观。"""
            hs = []
            for k, _a in plan:
                if k == "big":
                    b = ink_box(d, big, fb)
                    hs.append(b[3] - b[1])
                elif k == "cap" and cap:
                    b = ink_box(d, cap, fc)
                    hs.append(b[3] - b[1])
                elif k == "mon" and show_money:
                    b = ink_box(d, sym + money, fm)
                    hs.append(b[3] - b[1])
                elif k == "slot" and lines:
                    b = ink_box(d, lines[0], fr)
                    hs.append((b[3] - b[1]) + (len(lines) - 1) * (fr.size + gap))
            g_min = max(2, int(fc.size * 1.15 * gp))
            return sum(hs) + g_min * max(0, len(hs) - 1)

        def _set_for(pb):
            """给定主字号（像素），推出整套字体和折行结果。
            参数用的是最终像素而不是 big_sz —— 前面的收缩循环改的是 f_big.size
            本身，big_sz 那个变量已经把 fs_big 折进去了，再乘一次会翻倍。"""
            bsz = pb / (scale * max(0.01, fs_big))
            fb = (F_TXT if has_cjk(big) else F_NUM)(int(pb))
            fsc = F_NUM(max(8, int(pb * 0.45)))
            csz = max(9, min(24, bsz * 0.30))
            fc = F_TXT(int(csz * scale * fs_cap))
            fm = F_MON(int(bsz * 0.52 * scale * fs_mon))
            fr = F_TXT(max(8, int(csz * scale * fs_slot)))
            return (fb, fsc, fc, fm, fr, rewrap(fr),
                    max(2, int(fr.size * 0.5 * gp)))

        def _fits(s):
            fb, fsc, fc, fm, fr, lines, gapn = s
            if text_w(d, big, fb) + (
                    (int(6 * scale) + text_w(d, sec, fsc)) if sec else 0) > tw:
                return False
            if cap and text_w(d, cap, fc) > tw:
                return False
            if show_money and money_w(d, fm) > tw:
                return False
            if lines and max(text_w(d, l, fr) for l in lines) > room_r:
                return False
            return _plan_h(fb, fc, fm, fr, lines, gapn) <= th - guard

        # 二分，不要一档一档往上试：大卡片能长四五十档，每档都要量一遍文字宽度，
        # 动图逐帧渲染的时候这点开销会直接吃掉帧率。字号越大只会越宽越高，
        # 放得下这件事是单调的，二分安全。
        top_big = max(int(f_big.size), int(th * BIG_MAX))
        lo, hi = int(f_big.size), min(int(f_big.size) + 160, top_big + 1)
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if _fits(_set_for(mid)):
                lo = mid
            else:
                hi = mid
        best = _set_for(lo)
        if lo > f_big.size and _fits(best):
            (f_big, f_sec, f_cap, f_mon, f_capr,
             slot_lines, slot_gap) = best

        # 主时间经常先被宽度顶住（窄卡片上几乎必然），而说明和倒计时的字号是
        # 从它的比例推出来的，还带着 min(24, ...) 那个上限——卡片放到多大，
        # 这两行也就 24px。于是一行大字配三行小字，中间全是空白。
        # 所以再单独放大一次次要行，按各自与主字的比例封顶，不喧宾夺主。
        b_c, b_m, b_r = f_cap.size, f_mon.size, f_capr.size
        top_c = max(b_c, int(f_big.size * 0.42))
        top_m = max(b_m, int(f_big.size * 0.72))
        top_r = max(b_r, int(f_big.size * 0.42))

        def _sec_for(mul):
            fc = F_TXT(min(top_c, max(8, int(b_c * mul / 100.0))))
            fm = F_MON(min(top_m, max(9, int(b_m * mul / 100.0))))
            fr = F_TXT(min(top_r, max(8, int(b_r * mul / 100.0))))
            return fc, fm, fr, rewrap(fr), max(2, int(fr.size * 0.5 * gp))

        def _sec_fits(s):
            fc, fm, fr, lines, gapn = s
            if cap and text_w(d, cap, fc) > tw:
                return False
            if show_money and money_w(d, fm) > tw:
                return False
            if lines and max(text_w(d, l, fr) for l in lines) > room_r:
                return False
            return _plan_h(f_big, fc, fm, fr, lines, gapn) <= th - guard

        lo2, hi2 = 100, 300                    # 放大倍率，按百分比二分
        while hi2 - lo2 > 4:
            mid = (lo2 + hi2) // 2
            if _sec_fits(_sec_for(mid)):
                lo2 = mid
            else:
                hi2 = mid
        sec_best = _sec_for(lo2)
        if lo2 > 100 and _sec_fits(sec_best):
            f_cap, f_mon, f_capr, slot_lines, slot_gap = sec_best
        g_big = int(f_big.size * 0.34 * gp)
        g_mid = int(f_mon.size * 0.42 * gp)
        block, _gaps_sum = measure(f_capr, slot_lines)

    y = ty + max(int(6 * scale), (th - block) // 2)

    # ---------- 调试输出 ----------
    # set OFFWORK_DEBUG=1 后用 python widget.py 跑（不要用 pythonw，看不到输出）
    if os.environ.get("OFFWORK_DEBUG"):
        print("[render] 图片带 %dx%d @(%d,%d) 带比例 %.3f 图片比例 %.3f %s"
              % (bw, bh, bx, by, (bw / bh) if bh else 0, ratio,
                 "堆叠" if stack else "两列"))
        print("[render] W=%d H=%d scale=%.2f | tx=%d ty=%d tw=%d th=%d "
              "| single=%s stack=%s | block=%d guard=%d y=%d"
              % (W, H, scale, tx, ty, tw, th, single, stack, block, guard, y))
        print("[render] sizes big=%d sec=%d cap=%d mon=%d capr=%d | lines=%r"
              % (f_big.size, f_sec.size, f_cap.size, f_mon.size, f_capr.size, slot_lines))
        print("[render] widths big=%.1f sec=%.1f cap=%.1f mon=%.1f"
              % (text_w(d, big, f_big), text_w(d, sec, f_sec) if sec else 0,
                 text_w(d, cap, f_cap),
                 money_w(d, f_mon) if show_money else 0))

    # ---------- 画文字 ----------
    if single:
        # 卡片比内容高很多时（方形无图），把行距撑开铺满上下，
        # 比一味放大字号自然——内容还是成组的，只是呼吸感更足。
        # 只要还有余量就把行距撑开铺满，不限于方形无图那一种情况。
        # 撑开是在字号收缩之后做的，撑完必须再验一次没超框，
        # 否则某些占比区间会撑过头、行与行叠在一起。
        # 每行的字体和文本先定下来，排版按墨迹框走
        f_capl = f_cap
        if cap and text_w(d, cap, f_cap) > tw:
            f_capl = shrink(cap, f_cap.size, tw)
        bwid = text_w(d, big, f_big)
        w_big = bwid + ((int(6 * scale) + text_w(d, sec, f_sec)) if sec else 0)
        w_slot = max([text_w(d, l, f_capr) for l in slot_lines], default=0)

        vis = []            # (键, 对齐, 宽, 墨迹左偏, 墨迹上偏, 墨迹高, 字号)
        for k, al in plan:
            if k == "big":
                bx, byy, _bx2, by2 = ink_box(d, big, f_big)
                vis.append((k, al, w_big, bx, byy, by2 - byy, f_big.size))
            elif k == "cap" and cap:
                bx, byy, _bx2, by2 = ink_box(d, cap, f_capl)
                vis.append((k, al, text_w(d, cap, f_capl), bx, byy,
                            by2 - byy, f_capl.size))
            elif k == "mon" and show_money:
                bx, byy, _bx2, by2 = ink_box(d, sym + money, f_mon)
                vis.append((k, al, money_w(d, f_mon), bx, byy,
                            by2 - byy, f_mon.size))
            elif k == "slot" and slot_lines:
                bx, byy, _bx2, by2 = ink_box(d, slot_lines[0], f_capr)
                hblk = (by2 - byy) + (len(slot_lines) - 1) * (f_capr.size + slot_gap)
                vis.append((k, al, w_slot, bx, byy, hblk, f_capr.size))

        # 行距统一：原来三个间隙分别取自三个不同字体的比例（0.34×主字、
        # 0.9×倒计时、0.42×金额），数值差好几倍，看着就是忽宽忽窄。
        # 统一成一个值，再按剩余空间等比撑开。
        rows_only = sum(v[5] for v in vis)
        n_gap = max(0, len(vis) - 1)
        g_row = max(2, int(f_cap.size * 1.15 * gp))
        if n_gap:
            # 剩下的高度在行距和上下留白之间平均分，留白正好等于一个行距。
            # 以前行距封顶在 0.8 倍主字号，多出来的全落到上下两头——文字列一窄，
            # 字号被宽度顶住长不上去，就成了中间挤成一块、上下各空一大片。
            # 行距滑块乘在这里：拉大是行距吃掉留白，不是把整块推出去。
            g_row = max(g_row, int((th - rows_only) / (n_gap + 2) * gp))
            while rows_only + g_row * n_gap > th - guard and g_row > 2:
                g_row -= 1
        block_s = rows_only + g_row * n_gap
        y = ty + max(int(6 * scale), (th - block_s) // 2)

        def _x(align, ww):
            if align == "center":
                return tx + max(0, (tw - int(ww)) // 2)
            if align == "right":
                return tx + max(0, tw - int(ww))
            return tx

        for idx, (k, al, ww, inkx, inky, inkh, fsz) in enumerate(vis):
            x = _x(al, ww)
            # 减掉墨迹的左偏和上偏，对齐的是看得见的边缘，不是 em 框
            dx, dy = x - inkx, y - inky
            if k == "big":
                draw_text(d, (dx, dy), big, f_big, fg)
                if sec:
                    draw_text(d, (dx + bwid + int(6 * scale),
                                  dy + int((f_big.size - f_sec.size) * .75)),
                              sec, f_sec, fg2)
            elif k == "cap":
                draw_text(d, (dx, dy), cap, f_capl, fg2)
            elif k == "mon":
                draw_money(d, (dx, dy), f_mon, accent + (255,))
            else:
                for j2, line in enumerate(slot_lines):
                    draw_text(d, (dx, dy + j2 * (f_capr.size + slot_gap)),
                              line, f_capr, fg2)
            if rects is not None:
                rects.append((k, x, y, int(ww), int(inkh), fsz))
            y += inkh + g_row
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
                slot_gap = max(2, int(f_capr.size * 0.5 * gp))
            else:
                slot_lines.pop()            # 缩到底还撞，就少显示一条倒计时
        slot_w = max([text_w(d, l, f_capr) for l in slot_lines], default=0)
        lim = max(int(tw * 0.25), int(tw - slot_w - gapx))

        slot_h = (len(slot_lines) * f_capr.size
                  + slot_gap * max(0, len(slot_lines) - 1)) if slot_lines else 0
        r1 = max(f_big.size, f_mon.size if show_money else 0)
        r2 = max(f_cap.size if cap else 0, slot_h)
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
        if cap:
            f_capl = f_cap if text_w(d, cap, f_cap) <= lim else shrink(cap, f_cap.size, lim)
            draw_text(d, (tx, y2), cap, f_capl, fg2)
        # 整块贴右边，但块内各行左对齐——逐行右对齐会让「周五 0 天」和
        # 「发薪 13 天」左边参差不齐，读起来像没对上。
        blk_x = tx + tw - slot_w
        for i, line in enumerate(slot_lines):
            draw_text(d, (blk_x, y2 + i * (f_capr.size + slot_gap)),
                      line, f_capr, fg2)

        if rects is not None:
            # 播种自由排版要用。四条分支都得记，否则在没记的那种布局下
            # 切到自由排版会拿不到初值，文字直接跑没影。
            rects.append(("big", tx, base_y - f_big.size,
                          int(_row1_w()), f_big.size, f_big.size))
            if show_money:
                rects.append(("mon", tx + tw - mw, base_y - f_mon.size,
                              int(mw), f_mon.size, f_mon.size))
            if cap:
                rects.append(("cap", tx, y2, int(text_w(d, cap, f_capl)),
                              f_capl.size, f_capl.size))
            if slot_lines:
                rects.append(("slot", blk_x, y2, int(slot_w), slot_h, f_capr.size))

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
        self._first_run = not os.path.exists(CFG)
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
        if self._first_run:
            # 头一次跑，配置文件还不存在：与其给一个跟屏幕无关的 320x104，
            # 不如按这块屏推一个。用户改过之后就再也不动了。
            w, h = suggest_size(self.work_area(), self.cfg.get("ui", 100))
            write_cfg({"cardw": w, "cardh": h})
            self.cfg["cardw"], self.cfg["cardh"] = w, h
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


def suggest_size(wa, ui=100, portrait=False):
    """按屏幕可用区域推一个卡片尺寸。

    桌面小组件占屏宽 15~20% 是常见档位，再大就从"角落里的小工具"变成"面板"了。
    横版取屏高的 19% 当高度、1.8 倍当宽度；竖版取屏高的 28% 做成方卡，下面那条
    文字区由自动加高补出来。返回逻辑尺寸（没乘缩放），跟配置里存的一致。
    """
    sh = max(1, wa.bottom - wa.top)
    k = max(0.75, min(1.5, float(ui) / 100))
    if portrait:
        w = h = int(round(sh * 0.28 / k / 10) * 10)
    else:
        h = int(round(sh * 0.19 / k / 10) * 10)
        w = int(round(h * 1.8 / 10) * 10)
    return (max(W_MIN, min(W_MAX, w)), max(H_MIN, min(H_MAX, h)))


TAB_KEYS = {
    "工作": ["start", "end", "salary", "cur_sym", "sym_gap", "work_text", "show_money", "show_cap"],
    "外观": ["bg", "ui", "cardw", "cardh", "glass", "top", "alttab",
             "dock", "dock_pad", "tint", "tint_amt"],
    "图片": ["img_side", "fade", "rotate_min", "shuffle"],
    "倒计时": ["slots", "slot_rows", "slot_each"],
}


def place_settings(root, widget, only_if_overlap=False):
    """把设置窗口摆到组件旁边的空白处。

    组件是置顶的，设置窗口压在它上面就看不见改动效果，只能一边改一边挪窗口。

    两个坑：
    - 尺寸要等窗口真正映射出来才准。Tk() 之后马上量拿到的是 1x1，
      update_idletasks 之后拿到的是客户区，不含标题栏和边框，
      按客户区算会以为放得下、实际盖住一角。所以补上一圈装饰的余量。
    - 切页签时 Notebook 会改变窗口大小，原来不重叠的位置可能就压上去了，
      所以切页签也重判一次，但只在真的压住时才挪，免得窗口乱跳。
    """
    root.update_idletasks()
    w = max(root.winfo_reqwidth(), root.winfo_width()) + 16      # 左右边框
    h = max(root.winfo_reqheight(), root.winfo_height()) + 40    # 标题栏
    try:
        r = wt.RECT()
        user32.GetWindowRect(widget.hwnd, ctypes.byref(r))
        wa = widget.work_area()
    except Exception:
        return

    def hits(x, y):
        return (x < r.right and x + w > r.left
                and y < r.bottom and y + h > r.top)

    if only_if_overlap and not hits(root.winfo_rootx(), root.winfo_rooty()):
        return
    gap = 12
    for x, y in ((r.right + gap, r.top),          # 右
                 (r.left - w - gap, r.top),       # 左
                 (r.left, r.bottom + gap),        # 下
                 (r.left, r.top - h - gap)):      # 上
        x = max(wa.left, min(x, wa.right - w))
        y = max(wa.top, min(y, wa.bottom - h))
        # 夹回工作区之后可能又压到组件上了，得重新判一次相交
        if not hits(x, y):
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
    def on_show(key, var, row):
        write_cfg({key: var.get()})
        widget.reload()

    money_v = tk.BooleanVar(value=bool(cfg.get("show_money", True)))
    VARS["show_money"] = lambda val, v=money_v: v.set(bool(val))
    ttk.Checkbutton(t1, text="显示今日收入", variable=money_v,
                    command=lambda: on_show("show_money", money_v, "mon")
                    ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 0))
    cap_v = tk.BooleanVar(value=bool(cfg.get("show_cap", True)))
    VARS["show_cap"] = lambda val, v=cap_v: v.set(bool(val))
    ttk.Checkbutton(t1, text="显示说明行（距下班 · 18:30）", variable=cap_v,
                    command=lambda: on_show("show_cap", cap_v, "cap")
                    ).grid(row=7, column=0, columnspan=3, sticky="w", pady=3)
    ttk.Label(t1, text="货币符号可以直接在框里改，最多 4 个字符；汉字量词会自动放到\n"
                       "数字后面（288.91 元）。上班文案留空用「距下班」",
              foreground="#888", justify="left").grid(row=8, column=0, columnspan=3,
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
    # 横版加宽、竖版加高，都会让实际尺寸跟滑块对不上。滑块本身看不出这件事，
    # 所以把实际尺寸摆在下面一行，一眼就知道自己拖的那个管不管用。
    size_lbl = ttk.Label(t2, text="", foreground="#888")
    size_lbl.grid(row=5, column=0, columnspan=3, sticky="w", pady=(2, 0))

    def size_hint():
        c = read_cfg()
        k = max(0.75, min(1.5, float(c.get("ui", 100)) / 100))
        bw_ = int(clamp(c.get("cardw", 320), W_MIN, W_MAX, 320) * k)
        bh_ = int(clamp(c.get("cardh", 104), H_MIN, H_MAX, 104) * k)
        w, h = widget.size
        if w <= 0 or (abs(w - bw_) <= 2 and abs(h - bh_) <= 2):
            return ""
        why = "自动算的"
        which = []
        if abs(w - bw_) > 2:
            which.append("宽度")
        if abs(h - bh_) > 2:
            which.append("高度")
        return "实际 %dx%d，%s是%s，滑块不起作用" % (
            w, h, "、".join(which), why)

    def use_suggested(portrait):
        w, h = suggest_size(widget.work_area(), read_cfg().get("ui", 100), portrait)
        write_cfg({"cardw": w, "cardh": h})
        widget.reload()
        for k, v in (("cardw", w), ("cardh", h)):
            fn = VARS.get(k)
            if fn:
                fn(v)

    szbar = ttk.Frame(t2)
    szbar.grid(row=4, column=0, columnspan=3, sticky="e", pady=(2, 0))
    ttk.Label(szbar, text="按屏幕大小：").grid(row=0, column=0)
    ttk.Button(szbar, text="横版", width=8,
               command=lambda: use_suggested(False)).grid(row=0, column=1, padx=(0, 4))
    ttk.Button(szbar, text="竖版", width=8,
               command=lambda: use_suggested(True)).grid(row=0, column=2)
    gl = add_scale(t2, 6, "玻璃浓度", "glass", 1, 255, 90)
    # 这三个控件的引用要在这里就抓住。grid_remove() 之后控件不再受 grid 管理，
    # grid_slaves(row=5) 会返回空列表 —— 藏起来就再也找不回来了，
    # 表现就是切回毛玻璃时滑块不出现，得重开设置窗口。
    _glass_ws = list(t2.grid_slaves(row=6))

    def glass_row(show):
        for w in _glass_ws:
            w.grid() if show else w.grid_remove()
    glass_row(cfg.get("bg") == "glass")

    add_check(t2, 7, "窗口置顶", "top", True)
    alt_v = tk.BooleanVar(value=bool(cfg.get("alttab", False)))
    VARS["alttab"] = lambda val, v=alt_v: v.set(bool(val))
    ttk.Checkbutton(t2, text="在 Alt+Tab 中显示", variable=alt_v,
                    command=lambda: (write_cfg({"alttab": alt_v.get()}),
                                     widget.apply_alttab())
                    ).grid(row=8, column=0, columnspan=3, sticky="w", pady=3)
    ttk.Button(t2, text="回到屏幕中央", command=widget.center
               ).grid(row=9, column=0, columnspan=3, sticky="we", pady=(8, 0))

    auto_v = tk.BooleanVar(value=os.path.exists(STARTUP_VBS))
    ttk.Checkbutton(t2, text="开机自启", variable=auto_v,
                    command=lambda: set_autostart(auto_v.get())
                    ).grid(row=10, column=0, columnspan=3, sticky="w", pady=3)

    dock_v = tk.BooleanVar(value=bool(cfg.get("dock", True)))
    VARS["dock"] = lambda val, v=dock_v: v.set(bool(val))
    ttk.Checkbutton(t2, text="拖到屏幕边缘时自动吸附（换图变尺寸后重新贴边）",
                    variable=dock_v,
                    command=lambda: (write_cfg({"dock": dock_v.get()}),
                                     widget.reload())
                    ).grid(row=11, column=0, columnspan=3, sticky="w", pady=3)
    add_scale(t2, 12, "贴边留白", "dock_pad", 0, 40, 12)

    tint_v = tk.BooleanVar(value=bool(cfg.get("tint", False)))
    VARS["tint"] = lambda val, v=tint_v: v.set(bool(val))
    ttk.Checkbutton(t2, text="卡片配色跟随当前图片（强调色取主色，底色掺一点）",
                    variable=tint_v,
                    command=lambda: (write_cfg({"tint": tint_v.get()}),
                                     widget.reload())
                    ).grid(row=13, column=0, columnspan=3, sticky="w", pady=3)
    add_scale(t2, 14, "取色浓度", "tint_amt", 0, 40, 14)

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

    bar = ttk.Frame(t3)
    bar.grid(row=0, column=0, columnspan=3, sticky="we", pady=(18, 0))
    ttk.Button(bar, text="导入(可多选)", command=pick, width=11).grid(row=0, column=0, padx=2)
    ttk.Button(bar, text="上一张", command=lambda: step_img(-1),
               width=7).grid(row=0, column=1, padx=2)
    ttk.Button(bar, text="下一张", command=lambda: step_img(1),
               width=7).grid(row=0, column=2, padx=2)
    ttk.Button(bar, text="清空", command=clear, width=6).grid(row=0, column=3, padx=2)

    # 图片在文字的哪一侧。竖版和横版不是同一个方向，所以措辞跟着当前排法走：
    # 竖版 -> 图在上 / 图在下；横版 -> 图在左 / 图在右。存的还是同一个键。
    SIDE_TXT = {True: ("图在上", "图在下"), False: ("图在左", "图在右")}
    ttk.Label(t3, text="图片位置").grid(row=1, column=0, sticky="w", pady=4)
    sidev = tk.StringVar()
    side_cb = ttk.Combobox(t3, textvariable=sidev, width=20, state="readonly")
    side_cb.grid(row=1, column=1, columnspan=2, sticky="we", pady=4)
    _side_raw = ["right" if cfg.get("img_side") == "right" else "left"]
    _side_fill = [False]          # 回填措辞时别让它反过来触发保存

    def refresh_side():
        """措辞跟着当前排法换。改 values 和回填都要挡住 trace，
        否则 Tk 在旧文字不在新列表里时会把变量清掉，反过来触发一次保存。"""
        a, b = SIDE_TXT[bool(_STACK[0])]
        want = b if _side_raw[0] == "right" else a
        if list(side_cb["values"]) == [a, b] and sidev.get() == want:
            return
        _side_fill[0] = True
        try:
            side_cb.config(values=[a, b])
            sidev.set(want)
        finally:
            _side_fill[0] = False

    def on_side(*_):
        """按选中项在列表里的**位置**判断，不拿文字去比对。

        原来是用 _STACK[0] 反推标签再比对文字：竖版比「图在下」、横版比「图在右」。
        可 _STACK[0] 是每次渲染都在写的全局值，预览、播种、组件各渲各的，只要在
        点下拉和回调之间翻一下，比对就对不上，位置被悄悄复位成默认——这就是
        "自动重排会恢复图片位置"。位置索引跟谁在渲染无关。
        """
        if _side_fill[0]:
            return
        vals = list(side_cb["values"])
        cur = sidev.get()
        _side_raw[0] = "right" if (cur in vals and vals.index(cur) == 1) else "left"
        write_cfg({"img_side": _side_raw[0]})
        widget.reload()
    sidev.trace_add("write", on_side)

    def _set_side(val):
        _side_raw[0] = "right" if val == "right" else "left"
        refresh_side()
    VARS["img_side"] = _set_side
    refresh_side()

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

    add_scale(t3, 4, "淡出", "fade", 0, 100, 22)
    ttk.Label(t3, text="图片按自己的宽高比完整显示，绝不裁切。排法由卡片形状决定：\n"
                       "宽 > 高 时图在左、字在右；高 ≥ 宽 时图在上、字在下。\n"
                       "图片没占满的那一维会居中。",
              foreground="#888", justify="left").grid(row=6, column=0, columnspan=3,
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
        sync_add_btn(len(slots))

    def sync_add_btn(n):
        """到上限就置灰。留着能点但点了没反应，看着像坏了。
        存量配置里超过上限的项照样列出来，可以删，只是不能再加。"""
        try:
            add_btn.state(["disabled"] if n >= SLOT_MAX else ["!disabled"])
        except Exception:
            pass

    def add_slot():
        s = read_cfg().get("slots", [])
        if len(s) >= SLOT_MAX:
            return
        s.append({"label": "节日", "type": "date", "value": date.today().isoformat()})
        save_slots(s)
        draw_slots()

    add_btn = ttk.Button(t4, text="+ 添加一项", command=lambda: add_slot())
    add_btn.grid(row=1, column=0, sticky="we", pady=(8, 0))
    rowbox = ttk.Frame(t4)
    rowbox.grid(row=2, column=0, sticky="we", pady=(10, 0))
    add_scale(rowbox, 0, "最多占几行", "slot_rows", 1, SLOT_MAX, 2)
    add_check(rowbox, 1, "每条单独一行（不挤在同一行里）", "slot_each", False)
    ttk.Label(t4, text="最多 %d 项，放不下自动换行；超过上面设的行数就不显示了。\n"
                       "过期的不显示，勾「每年」可年年重复" % SLOT_MAX,
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
        dirty.clear()              # 控件回填会触发 trace，别让旧值又写回去
        widget.reload()

    for tab_name, frame in (("工作", t1), ("外观", t2), ("图片", t3),
                            ("倒计时", t4)):
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
            size_lbl.config(text=size_hint())
            refresh_side()        # 排法变了，「图片位置」的措辞要跟着换
        except Exception:
            pass
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
    # 映射出来之后再摆一次，这时候拿到的才是真实尺寸
    root.after(150, lambda: place_settings(root, widget, True))
    nb.bind("<<NotebookTabChanged>>",
            lambda *_: root.after(60, lambda: place_settings(root, widget, True)))
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
