# -*- coding: utf-8 -*-
"""墨阁素材浮窗 · 桌面宿主（Windows）

这个文件只解决一件事：让「素材浮窗」从"只能在墨阁网页里浮着"，
变成"盖在任意应用上面的独立窗口"。

--------------------------------------------------------------------------
【为什么需要它：网页画不出自己的窗口】
    原来的浮窗是 index.html 里的一个 position:fixed 层，z-index 42。
    它只能浮在**墨阁这个页面**上面 —— 浏览器不允许网页画到自己的
    窗口外面。这是浏览器的沙箱，不是代码写得不够好。
    所以"盖在 Word / 微信 / 桌面上"必须换成一个真正的桌面窗口。

【它是什么】
    一个很小的常驻进程，开一个无边框、始终置顶的窗口，
    窗口里装的就是墨阁自己的那个页面（http://127.0.0.1:8000/float）。
    界面、筛选、搜索、备忘录一个字都没重写 —— 换掉的只是外面那层壳。

--------------------------------------------------------------------------
【四条要求各自是怎么落地的】

  1) 盖在任意应用上面
     pywebview 的 on_top=True（落到 Win32 的 WS_EX_TOPMOST）。
     注意"置顶"对**管理员权限运行的程序**无效：非提升权限的窗口
     压不过提升权限的窗口，这是 Windows 的规则，改不了。
     同理，独占全屏的游戏也会盖住它。

  2) 自由拖动 / 缩放 + 记住位置尺寸
     拖动：先试"原生拖动"（给窗口发 WM_NCLBUTTONDOWN + HTCAPTION，
           由系统自己拖，跟拖任何窗口的标题栏是同一条路 —— 平滑、
           会吸附屏幕边缘、多显示器也对）。原生这条拿不到窗口句柄时，
           退回页内那套 JS 拖（见 index.html 的 chFloatBindDrag）。
     缩放：右下角手柄走 JS 增量 → api.resize_to()。
     记住：一个后台线程每 0.6 秒读一次 GetWindowRect，变了就写
           data/float_win.json。**不用 pywebview 的 moved/resized 事件** ——
           它每来一个事件就新起一个线程，拖动过程中会瞬间起几百个。

  3) 全局快捷键 + 托盘
     快捷键：Win32 RegisterHotKey（默认 Ctrl+Alt+M，写在配置里可改）。
             注册不上（被输入法/QQ/微信占了）会有托盘气泡提示，
             并且退回"不带防重复"的方式再试一次。
     托盘  ：pystray，菜单里有 显示/隐藏、打开主页面、退出。

  4) 切应用不丢内容、不打扰被盖住的软件
     内容不丢：隐藏走 ShowWindow(SW_HIDE) —— **窗口留着、页面不动**，
               不是销毁再重建，所以输入框里的字、列表滚到哪都在。
     不打扰  ：显示走 ShowWindow(SW_SHOWNOACTIVATE)，**不抢焦点**。
               你在 Word 里打字，浮窗冒出来不会把光标抢走。
               只有你自己去点输入框时它才激活（那是正常的）。

--------------------------------------------------------------------------
【关于后端】双击一次就全齐
    启动时先探一下 http://127.0.0.1:8000/api/health：
      · 已经在跑（比如你自己开过「启动墨阁.bat」）→ 直接用，退出时也不关它
      · 没在跑且 8000 端口空着 → 自己把它拉起来（无控制台窗口，日志写
        data/float_host.log），退出时顺手关掉
      · 端口被别的程序占着 → 不硬来，在窗口里说清是哪个端口被占了

【为什么这个文件在 floatwin/ 而不是 backend/】
    backend/ 是 FastAPI 应用（uvicorn backend.main:app）；这个是**带界面
    的客户端进程**，两者是并列的两个入口，混在一起以后没法分辨。
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser

# ----------------------------------------------------------------------
# 路径与常量
# ----------------------------------------------------------------------
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 跟后端一个规矩：允许用 MOGE_DATA_DIR 改写数据目录（测试用）
DATA_DIR = os.environ.get("MOGE_DATA_DIR") or os.path.join(REPO, "data")

CONF_PATH = os.path.join(DATA_DIR, "float_win.json")
LOG_PATH = os.path.join(DATA_DIR, "float_host.log")
# 【后端自己一个日志文件，不跟宿主共用一个】
# 实测踩过：原来两头都往 float_host.log 追加，结果
#   ① 宿主写的行是 UTF-8，uvicorn 子进程按系统 GBK 写 —— 同一个文件里
#      混着两种编码，谁打开都可能一半乱码；
#   ② 两个写入者共用一个追加句柄，写入位置会互相打断：宿主丢了 4 行日志，
#      还有一行被拦腰截断（"托盘就绪"只剩半句）。日志少行最坑 ——
#      排查时看到的是"这一步没打日志"，会去查一个根本不存在的问题。
# 分开写，各自的编码也各自保证（下面给子进程强塞 PYTHONUTF8=1）。
BACKEND_LOG_PATH = os.path.join(DATA_DIR, "float_backend.log")
PROFILE_DIR = os.path.join(DATA_DIR, "webview_profile")
ICON_PNG = os.path.join(DATA_DIR, "float_icon.png")
ICON_ICO = os.path.join(DATA_DIR, "float_icon.ico")

HOST = "127.0.0.1"
PORT = 8000
BASE = "http://%s:%d" % (HOST, PORT)
FLOAT_URL = BASE + "/float"
WIN_TITLE = "墨阁素材浮窗"

W_DEF, H_DEF = 780, 580
W_MIN, H_MIN = 380, 300
HOTKEY_DEF = "ctrl+alt+m"
# 默认那个被别的软件占了就往后退（实测这台机器上 Ctrl+Alt+M 已被占用）。
# 顺序是有讲究的：先给"离手近、又不容易撞"的，Win 键相关的放最后。
HOTKEY_FALLBACKS = ["ctrl+alt+m", "ctrl+shift+m", "ctrl+alt+f",
                    "ctrl+shift+f", "ctrl+alt+n", "alt+shift+m"]

# Win32 常量
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
WM_NCLBUTTONDOWN = 0x00A1
HTCAPTION, HTBOTTOMRIGHT = 2, 17
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 1, 2, 4, 8, 0x4000
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
HWND_TOPMOST = -1
SPI_GETWORKAREA = 0x0030
MONITOR_DEFAULTTONULL = 0
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
CREATE_NO_WINDOW = 0x08000000
# 所有出网点都带一个身份串（本地回环也带上，保持一条规矩）
USER_AGENT = "Moge/1.0 (+local writing tool)"

LOG_LOCK = threading.Lock()


def log(*parts):
    """写一行日志。日志文件是排查问题的唯一现场 —— 这个进程没有黑窗口。"""
    line = time.strftime("[%m-%d %H:%M:%S] ") + " ".join(str(p) for p in parts)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with LOG_LOCK:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


# ----------------------------------------------------------------------
# 本机探测（一律绕开系统代理）
#
# 为什么显式禁用代理：她的机器上挂着本地代理（给 github 用的）。
# urllib 默认会读系统代理设置，万一 127.0.0.1 也被塞进代理，
# 这个健康检查就会莫名其妙地失败 —— 而失败会让我们以为"后端没起来"，
# 于是去起第二个 uvicorn，然后端口冲突。这条路上每一步都在骗人。
# ----------------------------------------------------------------------
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def backend_alive(timeout=1.5):
    try:
        req = urllib.request.Request(BASE + "/api/health",
                                     headers={"User-Agent": USER_AGENT})
        with _OPENER.open(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def port_busy(port=PORT):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex((HOST, port)) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


# ----------------------------------------------------------------------
# 位置 / 尺寸：逻辑像素（跟 pywebview 的一套坐标对齐）
# ----------------------------------------------------------------------
def dpi_scale(hwnd=0):
    """屏幕缩放倍数（1.25 = 125%）。

    【为什么优先问"窗口"而不是"系统"】GetDpiForSystem 只告诉你**主屏**
    是多少；她要是哪天接了第二块 100% 的屏、把浮窗拖过去，窗口所在屏
    是 1.0 而系统回答 1.25 —— 那位置尺寸就会差 1.25 倍（存下去的位置
    下次启动就跑到别的地方去了）。GetDpiForWindow 给的是**这个窗口
    当前待的那块屏**，正是我们要的那个数。拿不到句柄时才退回系统值。

    注意：pywebview 内部（winforms 的 resize/move）用的也是
    GetDpiForWindow —— 两边得用同一把尺子，否则我们换算出来的"逻辑值"
    跟它设进去的"逻辑值"不是一回事。
    """
    u = ctypes.windll.user32
    if hwnd:
        try:
            v = int(u.GetDpiForWindow(wt.HWND(hwnd)))
            if v > 0:
                return max(1.0, v / 96.0)
        except Exception:
            pass
    try:
        return max(1.0, float(u.GetDpiForSystem()) / 96.0)
    except Exception:
        return 1.0


def _rect_logical(hwnd):
    r = wt.RECT()
    if not ctypes.windll.user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r)):
        return None
    # 用**这个窗口所在屏**的缩放来换算（别用系统值，见 dpi_scale）
    s = dpi_scale(hwnd)
    return r.left / s, r.top / s, (r.right - r.left) / s, (r.bottom - r.top) / s


def _on_any_monitor(x, y):
    """这个点还在不在某块屏幕上？拔掉外接屏之后，存的位置会掉到屏幕外。"""
    try:
        u = ctypes.windll.user32
        u.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
        u.MonitorFromPoint.restype = wt.HANDLE
        h = u.MonitorFromPoint(wt.POINT(int(x) + 30, int(y) + 15),
                               MONITOR_DEFAULTTONULL)
        return bool(h)
    except Exception:
        return True


def _default_geom():
    """默认位置：贴在屏幕右边偏上（跟页内版一样的起手位置）。"""
    try:
        u = ctypes.windll.user32
        r = wt.RECT()
        u.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0)
        s = dpi_scale()
        return (r.right / s - W_DEF - 36, r.top / s + 24, W_DEF, H_DEF)
    except Exception:
        return (200, 80, W_DEF, H_DEF)


def clamp_geom(x, y, w, h):
    """把窗口夹回"能拿得回来"的范围。

    只保证两件事：标题栏露在屏幕里（标题栏是唯一的拖动把手），
    以及宽高不小于能用的大小。夹太狠反而难受 —— 她想放哪就放哪。
    """
    w = max(W_MIN, int(w or W_DEF))
    h = max(H_MIN, int(h or H_DEF))
    s = dpi_scale()
    try:
        u = ctypes.windll.user32
        vx = u.GetSystemMetrics(SM_XVIRTUALSCREEN) / s
        vy = u.GetSystemMetrics(SM_YVIRTUALSCREEN) / s
        vw = u.GetSystemMetrics(SM_CXVIRTUALSCREEN) / s
        vh = u.GetSystemMetrics(SM_CYVIRTUALSCREEN) / s
    except Exception:
        return int(x), int(y), w, h
    w = min(w, max(W_MIN, vw - 40))
    h = min(h, max(H_MIN, vh - 40))
    x = max(vx + 4, min(int(x), vx + vw - 220))
    y = max(vy + 4, min(int(y), vy + vh - 64))
    return int(x), int(y), w, h


# ----------------------------------------------------------------------
# 托盘图标：用 Pillow 现画一个（不依赖任何外部图片文件）
# ----------------------------------------------------------------------
def make_icons():
    """画一个"一叠素材卡"的小图标，顺便存成 .png（托盘）和 .ico（任务栏）。"""
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        log("没有 Pillow，跳过图标生成：", e)
        return
    if not os.path.exists(ICON_PNG):
        try:
            S = 256
            im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
            d = ImageDraw.Draw(im)
            # 底：墨褐圆角方块（跟界面里的 --accent 同一个颜色）
            d.rounded_rectangle([10, 10, S - 10, S - 10], radius=58,
                                fill=(140, 90, 60, 255))
            # 三张"卡片"：米白两条 + 一条浅一档的
            d.rounded_rectangle([62, 70, 194, 96], radius=13, fill=(246, 245, 242, 255))
            d.rounded_rectangle([62, 118, 194, 144], radius=13, fill=(240, 231, 224, 255))
            d.rounded_rectangle([62, 166, 152, 192], radius=13, fill=(214, 178, 152, 255))
            im.save(ICON_PNG)
        except Exception as e:
            log("生成 png 图标失败：", e)
    if not os.path.exists(ICON_ICO):
        try:
            from PIL import Image
            Image.open(ICON_PNG).save(ICON_ICO,
                                      sizes=[(16, 16), (24, 24), (32, 32),
                                             (48, 48), (64, 64), (128, 128), (256, 256)])
        except Exception as e:
            log("生成 ico 图标失败：", e)


# ----------------------------------------------------------------------
# 快捷键：把一个 "ctrl+alt+m" 这样的字符串翻成 Win32 的两个数
# ----------------------------------------------------------------------
_VK_F = {("f%d" % i): 0x70 + i - 1 for i in range(1, 25)}


def parse_hotkey(text):
    """'ctrl+alt+m' → (修饰键, 虚拟键码)。认不出来就返回 None。"""
    if not text:
        return None
    mods, key = 0, None
    for part in str(text).lower().replace(" ", "").split("+"):
        if part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part in ("alt",):
            mods |= MOD_ALT
        elif part in ("shift",):
            mods |= MOD_SHIFT
        elif part in ("win", "super", "meta"):
            mods |= MOD_WIN
        elif len(part) == 1 and part.isalnum():
            key = ord(part.upper())
        elif part in _VK_F:
            key = _VK_F[part]
        elif part.isdigit():
            key = ord(part)
        else:
            return None
    if key is None or mods == 0:
        return None
    return mods, key


def hotkey_text(mods, vk):
    """反着来一遍，给界面显示用（页面上要写"Ctrl+Alt+M 收起"）。"""
    names = []
    if mods & MOD_CONTROL:
        names.append("Ctrl")
    if mods & MOD_ALT:
        names.append("Alt")
    if mods & MOD_SHIFT:
        names.append("Shift")
    if mods & MOD_WIN:
        names.append("Win")
    if 0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39:
        names.append(chr(vk))
    else:
        for k, v in _VK_F.items():
            if v == vk:
                names.append(k.upper())
                break
        else:
            names.append("0x%02X" % vk)
    return "+".join(names)


# ----------------------------------------------------------------------
# 显示"正在启动"的那一页
#
# 为什么要它：双击之后后端可能还没起来（第一次要十几秒）。
# 那时候直接 load 页面会是一片空白，她会以为"双击了没反应"。
# ----------------------------------------------------------------------
SPLASH = """<!doctype html><meta charset="utf-8">
<style>
  html,body{height:100%;margin:0}
  body{background:#f6f5f2;color:#26231f;
       font:14px/1.7 "Microsoft YaHei",system-ui,sans-serif;
       display:flex;align-items:center;justify-content:center;
       -webkit-user-select:none;user-select:none}
  .box{text-align:center;padding:24px 30px;max-width:440px}
  .logo{width:52px;height:52px;margin:0 auto 14px;border-radius:13px;
        background:#8c5a3c;color:#f6f5f2;font-size:26px;line-height:52px;
        font-weight:600}
  .t{font-size:15px;font-weight:600}
  .s{margin-top:6px;font-size:12.5px;color:#9c968c}
  .bad{color:#a8443a}
  .sug{margin-top:12px;font-size:12.5px;color:#6b665e;text-align:left;
       background:#fff;border:1px solid #e5e2dc;border-radius:10px;padding:10px 12px}
</style>
<div class="box">
  <div class="logo">墨</div>
  <div class="t" id="t">正在启动墨阁服务…</div>
  <div class="s" id="s">第一次启动要多等十几秒，之后就快了。</div>
  <div class="sug" id="sug" hidden></div>
</div>
<script>
  var t = document.getElementById("t");
  var s = document.getElementById("s");
  var sug = document.getElementById("sug");
  var hk = "";        // 宿主注册成功的那个真实键名（可能是退让后的候选）
  function fail(msg) {
    t.textContent = "墨阁服务没能起来";
    t.className = "t bad";
    s.textContent = msg || "原因不明。";
    sug.hidden = false;
    /* 【这里绝对不能写死 Ctrl+Alt+M】
       她这台机器上 Ctrl+Alt+M 已经被别的软件占了，宿主会退到候选里
       抢得到的那个（实测是 Ctrl+Shift+M）。写死一个键名，等于在最需要
       的时候教她按一个按不动的键 —— 比什么都不说还伤人。
       拿得到宿主下发的键名就用它，拿不到就指托盘（托盘一定起得来）。 */
    var how = hk ? ("先按 " + hk + " 把这个窗口收起来")
                 : "先把窗口收起来（右下角托盘图标右键里有）";
    sug.textContent = "可以这样排查：" + how
      + "，再双击 G 盘里的「启动墨阁.bat」看看那个黑窗口报了什么错；"
      + "详细日志在 data\\float_backend.log。";
  }
  (function poll() {
    var api = window.pywebview && window.pywebview.api;
    if (!api) { setTimeout(poll, 300); return; }
    api.state().then(function (st) {
      if (!st) { setTimeout(poll, 800); return; }
      if (st.hotkey_label) { hk = st.hotkey_label; }
      if (st.backend === "failed") { fail(st.msg); return; }
      if (st.backend === "ready") {
        t.textContent = "好了，正在打开素材浮窗…";
        s.textContent = "";
        setTimeout(poll, 1200);        // 宿主马上会把这个页面换掉
        return;
      }
      setTimeout(poll, 600);
    }).catch(function () { setTimeout(poll, 800); });
  })();
</script>
"""


# ----------------------------------------------------------------------
# 宿主主体
# ----------------------------------------------------------------------
class FloatHost:
    def __init__(self):
        self.window = None
        self.tray = None
        self.proc = None            # 我们自己拉起来的后端进程
        self.owned_backend = False  # 是自己拉的吗？（决定退出时关不关它）
        self.backend_state = "starting"
        self.backend_msg = ""
        self.hotkey_text = ""
        self.hotkey_tid = 0
        self.hotkey_ok = False
        self._conf = {}
        # 【这把锁是防"两份配置互相覆盖"的】
        # 写这份配置的有两个线程：几何轮询（watch_geometry，每 0.6 秒）
        # 和主线程（补默认值 / 记实际生效的快捷键）。save_conf 走的是
        # "先写同名的 .tmp 再替换"—— 两个线程同时写同一个 .tmp，
        # 内容会交错成半截 JSON，下次启动读到坏配置就静默回默认位置
        # （她看到的是"位置又没记住"，而且不报错）。
        self._conf_lock = threading.Lock()
        self._last_saved = None
        self._stop = threading.Event()
        self.want_w, self.want_h = W_DEF, H_DEF   # 这次要建多大的窗（见 fix_size）

    # ---------------- 配置（位置 / 尺寸 / 快捷键 都在这一个文件里）-----
    def load_conf(self):
        try:
            with open(CONF_PATH, encoding="utf-8") as f:
                c = json.load(f)
            self._conf = c if isinstance(c, dict) else {}
        except Exception:
            self._conf = {}
        # 【配置里必须真的有 hotkey 这个键】
        # 快捷键被占用的时候，我们告诉她"想换组合就改 float_win.json 里的
        # hotkey"。可她打开文件**根本找不到这个字段** —— 提示让她去改一个
        # 不存在的东西，只能靠猜。（提示本身在误导，跟缺功能一样伤人。）
        # 所以第一次跑就把默认值落进文件，她打开就有得改。
        if not str(self._conf.get("hotkey") or "").strip():
            self._conf["hotkey"] = HOTKEY_DEF
            self.save_conf()
        return self._conf

    def save_conf(self):
        # 两个线程都会写（几何轮询 / 主线程），必须串起来写，见 _conf_lock
        with self._conf_lock:
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                tmp = CONF_PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._conf, f, ensure_ascii=False, indent=2)
                os.replace(tmp, CONF_PATH)
            except Exception as e:
                log("存配置失败：", e)

    def geom(self):
        c = self._conf
        try:
            x, y, w, h = int(c["x"]), int(c["y"]), int(c["w"]), int(c["h"])
        except Exception:
            x, y, w, h = _default_geom()
        # 存的位置已经不在任何一块屏幕上了（拔过外接屏）→ 回默认位置
        if not _on_any_monitor(x, y):
            log("上次的位置不在任何屏幕里了，回到默认位置")
            x, y, w, h = _default_geom()
        return clamp_geom(x, y, w, h)

    # ---------------- 窗口句柄 ----------------
    def hwnd(self):
        try:
            n = getattr(self.window, "native", None)
            h = getattr(n, "Handle", None) if n is not None else None
            if h is not None:
                return int(h.ToInt64())
        except Exception:
            pass
        try:
            h = ctypes.windll.user32.FindWindowW(None, WIN_TITLE)
            return int(h) or None
        except Exception:
            return None

    # ---------------- 显示 / 隐藏（都不抢焦点）----------------
    def is_visible(self):
        h = self.hwnd()
        if h:
            return bool(ctypes.windll.user32.IsWindowVisible(wt.HWND(h)))
        return not bool(self.window.hidden) if self.window else False

    def is_minimized(self):
        h = self.hwnd()
        if not h:
            return False
        # IsIconic = 最小化了
        return bool(ctypes.windll.user32.IsIconic(wt.HWND(h)))

    def show(self, activate=False):
        h = self.hwnd()
        if h:
            u = ctypes.windll.user32
            if self.is_minimized():
                u.ShowWindow(wt.HWND(h), 9)      # SW_RESTORE
            u.ShowWindow(wt.HWND(h),
                         SW_SHOWNOACTIVATE if not activate else 5)  # 5 = SW_SHOW
            # 再顶一次，免得它被别的置顶窗口压住
            u.SetWindowPos(wt.HWND(h), wt.HWND(HWND_TOPMOST), 0, 0, 0, 0,
                           SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        else:
            self.window.show()

    def hide(self):
        h = self.hwnd()
        if h:
            ctypes.windll.user32.ShowWindow(wt.HWND(h), SW_HIDE)
        else:
            self.window.hide()
        self.save_geom_now()

    def toggle(self):
        # 用真实的可见 / 最小化状态判断，而不是我们自己记的变量 ——
        # 她完全可能在任务栏上点一下把它最小化，那种情况该"还原"不是"藏起来"
        if self.is_visible() and not self.is_minimized():
            self.hide()
        else:
            self.show(activate=False)

    # ---------------- 位置尺寸的看护线程 ----------------
    def watch_geometry(self):
        """每 0.6 秒看一次窗口在哪、多大，变了就写进配置文件。

        为什么不订阅 pywebview 的 moved / resized 事件：它每来一个事件就
        新起一个线程执行回调，而拖动过程中 WM_MOVE 是成百上千个 ——
        那会瞬间起几百个线程。轮询虽然笨，但这里 0.6 秒一次的开销可以忽略。
        """
        while not self._stop.is_set():
            try:
                h = self.hwnd()
                g = _rect_logical(h) if h else None
                if g:
                    x, y, w, hh = (int(round(v)) for v in g)
                    if (x, y, w, hh) != self._last_saved:
                        # 拖动过程中会连写几次，文件很小，无所谓
                        self._last_saved = (x, y, w, hh)
                        self._conf.update({"x": x, "y": y, "w": w, "h": hh})
                        self.save_conf()
            except Exception:
                pass
            self._stop.wait(0.6)

    def save_geom_now(self):
        try:
            h = self.hwnd()
            g = _rect_logical(h) if h else None
            if g:
                x, y, w, hh = (int(round(v)) for v in g)
                self._conf.update({"x": x, "y": y, "w": w, "h": hh})
                self._last_saved = (x, y, w, hh)
                self.save_conf()
        except Exception as e:
            log("存位置失败：", e)

    # ---------------- 原生拖动 / 原生缩放 ----------------
    def native_drag(self, kind="move"):
        """给窗口发一条"标题栏被按下"的消息，让系统自己拖。

        这是所有无边框窗口的标准做法（Electron 的 -webkit-app-region: drag
        底下干的就是这件事）。成功的话：平滑、会吸附屏幕边缘、多显示器也对。
        拿不到句柄或调用失败就返回 False，页面上会退回 JS 增量拖动。
        """
        h = self.hwnd()
        if not h:
            return False
        try:
            u = ctypes.windll.user32
            hit = HTBOTTOMRIGHT if kind == "bottomright" else HTCAPTION
            u.ReleaseCapture()
            u.SendMessageW(wt.HWND(h), WM_NCLBUTTONDOWN, wt.WPARAM(hit), 0)
            return True
        except Exception as e:
            log("原生拖动失败，改用 JS 拖动：", e)
            return False

    def move_to(self, x, y):
        """JS 增量拖动的落点（这条路一定通，是原生那条的兜底）。"""
        try:
            self.window.move(int(x), int(y))
            return True
        except Exception as e:
            log("移动窗口失败：", e)
            return False

    def resize_to(self, w, h):
        try:
            w = max(W_MIN, int(w))
            h = max(H_MIN, int(h))
            self.window.resize(w, h)
            # 往右下角拉大之后可能顶出屏幕，夹回来（代价是跳一下，
            # 比"拉完跑到屏幕外拿不回来"好得多）
            x, y = self.window.x, self.window.y
            if x is not None and y is not None:
                nx, ny, _, _ = clamp_geom(x, y, w, h)
                if (nx, ny) != (int(x), int(y)):
                    self.window.move(nx, ny)
            return True
        except Exception as e:
            log("缩放窗口失败：", e)
            return False

    # ---------------- 后端：看一眼、没有就拉起来 ----------------
    def prepare_backend(self):
        log("启动检查：后端在跑吗？", backend_alive())
        if backend_alive():
            self.backend_state = "ready"
            self.owned_backend = False
            return
        if port_busy():
            self.backend_state = "failed"
            self.backend_msg = ("%d 端口被别的程序占着，墨阁服务起不来。"
                                "先关掉占用它的程序，再双击一次这个图标。"
                                % PORT)
            log(self.backend_msg)
            return
        self.backend_state = "starting"
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            lf = open(BACKEND_LOG_PATH, "a", encoding="utf-8", buffering=1)
            lf.write("\n===== %s 由浮窗宿主拉起后端 =====\n"
                     % time.strftime("%m-%d %H:%M:%S"))
            # 【必须把子进程的输出编码也钉死成 UTF-8】
            # 不给的话，uvicorn 这个子进程会按系统 ANSI 代码页（中文机是
            # cp936/GBK）编码往这个文件里写 —— 我们这边却按 UTF-8 追加，
            # 一个文件两种编码，乱码就是这么来的。
            env = dict(os.environ)
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            # 【无缓冲】后端的 stdout 是个文件。不给这个变量的话，Python 到
            # 文件时是**块缓冲** —— 那段"墨阁已启动"的提示和每一步 print 全
            # 攒在缓冲区里，她排"服务卡在哪一步"的时候打开 float_backend.log
            # 只会看到一片空白（或者半截），跟"什么都没发生"长得一模一样。
            env["PYTHONUNBUFFERED"] = "1"
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "backend.main:app",
                 "--host", HOST, "--port", str(PORT)],
                cwd=REPO, stdin=subprocess.DEVNULL, stdout=lf, stderr=lf,
                env=env, creationflags=CREATE_NO_WINDOW)
            self.owned_backend = True
            log("后端已拉起，pid =", self.proc.pid)
        except Exception as e:
            self.backend_state = "failed"
            self.backend_msg = "启动墨阁服务失败：" + str(e)
            log(traceback.format_exc())

    def stop_backend(self):
        if not (self.owned_backend and self.proc):
            return
        log("关掉我们自己拉起来的后端 pid =", self.proc.pid)
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=6)
            except Exception:
                self.proc.kill()
        except Exception as e:
            log("关后端失败（可能它自己已经退了）：", e)

    def restart_backend(self):
        self.stop_backend()
        self.proc = None
        self.owned_backend = False
        time.sleep(0.6)
        self.backend_state = "starting"
        self.prepare_backend()
        if self.backend_state == "failed":
            tray_notify(self, self.backend_msg or "重启失败")
            return
        self.wait_backend(45)
        try:
            self.window.load_url(FLOAT_URL)
        except Exception as e:
            log("重载页面失败：", e)

    # ---------------- 全局快捷键 ----------------
    def _note_hotkey_actual(self, text):
        """把"这次真正生效的键"记进配置文件，仅供她核对。

        【为什么不拿它当 want 用】配置里的 hotkey 是"**她想要的**组合"。
        不能因为这次抢不到就把它改写成实际值 —— 那等于把"Ctrl+Alt+M 被占了"
        这件事永久掩盖：下次启动不再试它，看起来一切正常，而占用它的那个
        软件哪天卸了，她也永远回不到自己挑的那个键。
        所以这里只记、不改。字段名带上 "actual"，免得跟 hotkey 看混。
        """
        val = text or "（没有可用的，只能从托盘开）"
        if self._conf.get("hotkey_actual") == val:
            return
        self._conf["hotkey_actual"] = val
        self.save_conf()

    def start_hotkey(self):
        """注册全局快捷键。

        【为什么要一串候选、而不是"注册失败就让她自己改配置"】
        实测：Ctrl+Alt+M 在我们这台机器上**已经被别的软件占了**
        （很可能就是输入法或者某个常驻工具）。如果只试一个然后报错，
        她的第一次体验就是"快捷键根本不好使，还让我去改一个 JSON 文件"——
        这要求太高了。所以按顺序往后试，谁抢到就用谁，
        并把**真正生效的那个键**告诉她（托盘气泡 + 标题栏上那句提示）。
        """
        want = str(self._conf.get("hotkey") or HOTKEY_DEF).strip() or HOTKEY_DEF

        def loop():
            u = ctypes.windll.user32
            self.hotkey_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            cands, seen = [], set()
            for name in [want] + HOTKEY_FALLBACKS:
                if name and name not in seen:
                    seen.add(name)
                    cands.append(name)
            picked = None
            for name in cands:
                hk = parse_hotkey(name)
                if not hk:
                    log("这个快捷键写法看不懂，跳过：", name)
                    continue
                mods, vk = hk
                # 先带"防重复"（按住不狂触发），不行再不带 ——
                # 有些软件占的恰好是那个标志位。
                if u.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk) \
                        or u.RegisterHotKey(None, 1, mods, vk):
                    picked = (mods, vk, name)
                    break

            if not picked:
                self.hotkey_ok = False
                self.hotkey_text = hotkey_text(*parse_hotkey(want))
                log("候选快捷键全被占了，只剩托盘可用")
                self._note_hotkey_actual("")
                tray_notify(self, "所有候选快捷键都被别的软件占用了。"
                                  "先右键托盘图标显示 / 隐藏；"
                                  "想固定一个组合，改 %s 里的 hotkey 再重启浮窗。"
                                  % CONF_PATH)
            else:
                mods, vk, name = picked
                self.hotkey_ok = True
                self.hotkey_text = hotkey_text(mods, vk)
                log("全局快捷键已注册：", self.hotkey_text)
                self._note_hotkey_actual(self.hotkey_text)
                if name.lower() != want.lower():
                    tray_notify(self, "%s 被别的软件占用了，快捷键先给你换成 %s。"
                                      "想用别的组合：改 %s 里的 hotkey 再重启浮窗。"
                                      % (want.upper(), self.hotkey_text, CONF_PATH))

            msg = wt.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    try:
                        self.toggle()
                    except Exception:
                        log(traceback.format_exc())

        self.hotkey_thread = threading.Thread(target=loop, daemon=True,
                                              name="moge-hotkey")
        self.hotkey_thread.start()

    def stop_hotkey(self):
        try:
            if self.hotkey_tid:
                ctypes.windll.user32.PostThreadMessageW(self.hotkey_tid,
                                                        WM_QUIT, 0, 0)
            ctypes.windll.user32.UnregisterHotKey(None, 1)
        except Exception:
            pass

    # ---------------- 托盘 ----------------
    def start_tray(self):
        try:
            import pystray
            from PIL import Image
        except Exception as e:
            log("没有 pystray / Pillow，跳过托盘：", e)
            return
        make_icons()
        try:
            img = Image.open(ICON_PNG)
        except Exception:
            img = None
        items = [
            pystray.MenuItem("显示 / 隐藏　(%s)" % (self.hotkey_text or HOTKEY_DEF),
                             lambda *a: self.toggle(), default=True),
            pystray.MenuItem("打开墨阁主页面（浏览器）",
                             lambda *a: webbrowser.open(BASE)),
        ]
        if self.owned_backend:
            items.append(pystray.MenuItem("重启墨阁服务",
                                          lambda *a: self.restart_backend()))
        items += [pystray.Menu.SEPARATOR,
                  pystray.MenuItem("退出（并停止墨阁服务）"
                                   if self.owned_backend else "退出浮窗",
                                   lambda *a: self.quit())]
        try:
            self.tray = pystray.Icon("moge-float", img, WIN_TITLE,
                                     pystray.Menu(*items))
            threading.Thread(target=self.tray.run, daemon=True,
                             name="moge-tray").start()
            log("托盘图标已就绪")
        except Exception as e:
            log("托盘起不来（不影响用快捷键）：", e)

    # ---------------- 等后端起来，再把页面换过去 ----------------
    def wait_backend(self, seconds=60):
        t0 = time.time()
        while time.time() - t0 < seconds:
            if backend_alive():
                self.backend_state = "ready"
                return True
            if self.backend_state == "failed":
                return False
            if self.proc and self.proc.poll() is not None:
                self.backend_state = "failed"
                self.backend_msg = ("墨阁服务刚起来就退出了。"
                                    "多半是端口被占或者代码报错 ——"
                                    "原因写在 data\\float_backend.log 里。")
                return False
            time.sleep(0.5)
        self.backend_state = "failed"
        self.backend_msg = ("等了 %d 秒墨阁服务还没起来 —— 它一个字都还没回，"
                            "多半不是配置问题。"
                            "详情看 data\\float_backend.log。" % seconds)
        return False

    # ---------------- 收尾 ----------------
    def quit(self):
        log("退出浮窗宿主")
        self.save_geom_now()
        self._stop.set()
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        self.stop_hotkey()
        self.stop_backend()
        try:
            if self.window:
                self.window.destroy()
        except Exception:
            pass
        # 【兜底】quit 可能是从托盘那个线程调进来的，那里 sys.exit 只结束
        # 那个线程、结束不了进程。给正常退出 2 秒，然后硬退 ——
        # 绝不留下一个"点了退出但进程还在"的幽灵。
        threading.Timer(2.0, lambda: os._exit(0)).start()

    # ---------------- 窗口起来之后要做的事 ----------------
    def worker(self):
        try:
            log("后台初始化开始")
            self.set_window_icon()
            log("  · 图标就绪")
            self.fix_size()
            log("  · 尺寸对齐完成")
            # 先起托盘：快捷键那条"被占用、给你换成 X"的提示要靠它弹气泡
            self.start_tray()
            log("  · 托盘就绪")
            self.start_hotkey()
            log("  · 快捷键线程已起")
            threading.Thread(target=self.watch_geometry, daemon=True,
                             name="moge-geom").start()
            if self.wait_backend(60):
                log("后端就绪，加载浮窗页面")
                self.window.load_url(FLOAT_URL)
            # 失败时不用额外做什么：那一页自己会轮询 state() 并把原因写在屏幕上
        except Exception:
            log("后台初始化出错：", traceback.format_exc())

    def fix_size(self):
        """把刚建出来的窗口尺寸**对齐到我们要的那个数**。

        【为什么需要这一下】pywebview 是按"带边框窗口"建窗的（它给的
        width/height 是含边框的外框尺寸），而我们建完立刻把边框去掉了
        （frameless），WinForms 于是把外框按边框厚度缩了一圈 ——
        实测在 125% 缩放的屏幕上，要 780x580，量出来只有 766x542。

        不纠的后果不是"差十几个像素"这么简单：存进配置的是**量到的**
        那个数（766x542），下次启动就按 766x542 建窗，又被缩一圈……
        于是她每启动一次窗口就小一点，位置尺寸"记住了"却一直在漂，
        几十次之后会小到没法用。这种 bug 最阴的地方是"看起来是记住了"。

        resize() 底下走的是 SetWindowPos，那时边框已经没了，
        要多大就是多大，一次就对齐。
        """
        try:
            # 【为什么丢到一个子线程里做】resize() 底下会先等窗口的 shown 事件
            # （pywebview 的 @_shown_call，最多等 20 秒）。实测某些情况下这个
            # 等待会一直挂着 —— 那会把整个初始化拖死：托盘、快捷键全都不出来，
            # 而"双击之后什么都不发生"是最难查的一种故障。
            # 它只是个锦上添花的补正，绝不能让主线等它。
            box = {}

            def do_resize():
                try:
                    self.window.resize(self.want_w, self.want_h)
                    box["ok"] = True
                except Exception as e:
                    box["err"] = e

            t = threading.Thread(target=do_resize, daemon=True, name="moge-fixsize")
            t.start()
            t.join(8)
            if t.is_alive():
                log("尺寸对齐没在 8 秒内回来，跳过（只是可能差十几个像素）")
                return
            if box.get("err"):
                log("对齐初始尺寸失败（不影响用）：", box["err"])
                return
            time.sleep(0.15)
            h = self.hwnd()
            g = _rect_logical(h)
            log("尺寸对齐：要 %dx%d，量到 %.0fx%.0f，屏幕缩放 %.2f"
                % (self.want_w, self.want_h,
                   g[2] if g else -1, g[3] if g else -1, dpi_scale(h)))
        except Exception as e:
            log("对齐初始尺寸失败（不影响用，只是可能差十几像素）：", e)

    def set_window_icon(self):
        """给窗口换上自己的图标（任务栏和 Alt-Tab 上那个）。

        【为什么用 Win32 的 LoadImageW，而不是 System.Drawing.Icon】
        实测踩到的：走 pythonnet 那条路，
            from System.Drawing import Icon
            window.native.Icon = Icon("…/float_icon.ico")
        会在最后一步**卡死** —— 不抛异常、就是不返回，日志停在
        "准备套到窗口上"这一行，整个后台初始化（托盘、快捷键、页面）
        全都不出来了。现象是"双击之后窗口是空的、什么都不发生"。

        换个图标而已，不值得为它冒"双击没反应"的风险，所以改成
        直接用 Win32 从同一个 .ico 里读图标。这条路完全不经过 .NET。

        外层那个"8 秒上限"是保命的：这一区已经证明会出怪事，
        而它出问题的方式是**静默卡住**，代价是整个程序不可用。
        """
        def do():
            try:
                make_icons()
                hwnd = self.hwnd()
                if not hwnd or not os.path.exists(ICON_ICO):
                    log("没有图标文件，跳过（不影响使用）")
                    return
                u = ctypes.windll.user32
                u.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT,
                                         ctypes.c_int, ctypes.c_int, wt.UINT]
                u.LoadImageW.restype = wt.HANDLE
                IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x0010, 0x0080
                for size, which in ((16, 0), (32, 1)):   # 0=小图标 1=大图标
                    hic = u.LoadImageW(None, ICON_ICO, IMAGE_ICON,
                                       size, size, LR_LOADFROMFILE)
                    if hic:
                        # 窗口接过这个图标的所有权，**不要**在这里 DestroyIcon
                        u.SendMessageW(wt.HWND(hwnd), WM_SETICON, which, hic)
                log("    窗口图标已设置")
            except Exception as e:
                log("设置窗口图标失败（不影响使用）：", e)

        t = threading.Thread(target=do, daemon=True, name="moge-icon")
        t.start()
        t.join(8)
        if t.is_alive():
            log("设置窗口图标没在 8 秒内回来，跳过（只是任务栏图标不好看）")


def tray_notify(host, msg):
    """托盘气泡。

    会**等一小会儿**托盘就绪 —— 快捷键那条"被占用、给你换成 X"的提示
    是注册完立刻发的，而那时候托盘线程可能还没跑起来。
    气球弹不出来不算错，但"她永远不知道该按哪个键"是实打实的麻烦。
    """
    log("提示：", msg)
    for _ in range(25):            # 最多等 4 秒左右
        try:
            if host.tray:
                host.tray.notify(msg, WIN_TITLE)
                return
        except Exception:
            pass
        time.sleep(0.16)


class Bridge:
    """暴露给页面用的几个方法（页面里是 window.pywebview.api.xxx()）。

    【两条硬规矩，踩过坑的】
    1) 方法名**不要以下划线开头** —— pywebview 只暴露公开方法。
    2) 域（属性）名**必须以下划线开头** —— 这条更容易忘，代价也更大：
       pywebview 建 JS 桥时会 `dir(js_api对象)` 把**所有公开属性递归走一遍**
       （见 webview/util.py 的 get_functions），碰到带 __module__ 的对象就
       继续往下钻。原先这里写的是 `self.host = host`，于是它顺着
       host.window.native 一路爬进 .NET 的 WinForms 对象，
       打印出天文数字长度的 `AccessibilityObject.Bounds.Empty.Empty.Empty…`
       然后 maximum recursion depth exceeded —— 表现是**窗口刚加载完页面，
       整个进程直接没了**，而且 pythonw 没有控制台，什么线索都看不到。
       所以这里存成 _host。
    """

    def __init__(self, host):
        self._host = host

    def state(self):
        return {
            "backend": self._host.backend_state,
            "msg": self._host.backend_msg,
            "hotkey_label": self._host.hotkey_text,
            "hotkey_ok": self._host.hotkey_ok,
        }

    def diag(self, msg=""):
        """页面报一声"我连上桥了"。

        这一行是判断"页面 ↔ 宿主"通不通的**唯一判据**：
        日志里有它，才谈得上后面那些拖动、缩放、隐藏的调用；
        没有它，说明桥根本没搭上，界面那边只能当普通网页用。
        """
        log("页面说：", msg)
        return True

    def drag_start(self, kind="move"):
        return self._host.native_drag(kind)

    def move_to(self, x, y):
        return self._host.move_to(x, y)

    def resize_to(self, w, h):
        return self._host.resize_to(w, h)

    def save_geom(self):
        self._host.save_geom_now()
        return True

    def hide_win(self):
        self._host.hide()
        return True

    def open_main(self):
        webbrowser.open(BASE)
        return True

    def quit_app(self):
        self._host.quit()
        return True


# ----------------------------------------------------------------------
def fatal(msg, err=None):
    """出事了 —— 弹一个系统消息框。

    这个进程是用 pythonw 跑的，**没有黑窗口**：不弹框的话她只会看到
    "双击了没反应"，那是最难查的一种错。
    """
    detail = ""
    if err:
        detail = "\n\n" + str(err)
        log("致命错误：", msg, "\n", traceback.format_exc())
    try:
        ctypes.windll.user32.MessageBoxW(
            None, "%s%s\n\n详细日志：\n  %s（浮窗自己）\n  %s（墨阁服务）"
                  % (msg, detail, LOG_PATH, BACKEND_LOG_PATH),
            "墨阁素材浮窗 · 启动失败", 0x10)
    except Exception:
        pass


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    log("=" * 46)
    log("启动浮窗宿主，python =", sys.executable)

    try:
        import webview
    except Exception as e:
        fatal("缺少 pywebview，浮窗起不来。\n"
              "在项目目录里双击「启动墨阁.bat」所在的文件夹，"
              "用命令行跑一次：\n.venv\\Scripts\\python.exe -m pip install "
              "pywebview pystray", e)
        return 1

    host = FloatHost()
    host.load_conf()

    # 【单实例】她双击第二次的时候，不该冒出第二个一模一样的浮窗。
    # 窗口是"藏起来"而不是销毁的，所以哪怕它现在收着，这句也能找到它。
    try:
        u = ctypes.windll.user32
        other = u.FindWindowW(None, WIN_TITLE)
        if other:
            log("已经有一个浮窗在跑了，把它叫出来")
            u.ShowWindow(wt.HWND(other), SW_SHOWNOACTIVATE)
            u.SetForegroundWindow(wt.HWND(other))   # 这一下要抢焦点：
            return 0                                # 她刚双击，本来就是想看它
    except Exception:
        pass

    g = host.geom()
    host.want_w, host.want_h = g[2], g[3]
    log("窗口位置尺寸 =", g)

    host.prepare_backend()
    if host.backend_state == "failed":
        # 后端没戏了也照样把窗口开出来 —— 那一页会把原因写在屏幕上，
        # 比"双击了什么都不出现"有用得多
        log("后端不可用，仍然开窗口把原因显示出来：", host.backend_msg)

    try:
        host.window = webview.create_window(
            WIN_TITLE,
            html=SPLASH,
            js_api=Bridge(host),
            x=g[0], y=g[1], width=g[2], height=g[3],
            min_size=(W_MIN, H_MIN),
            frameless=True,       # 不要系统标题栏：标题栏是我们自己画的那条
            easy_drag=False,      # 不要"按住哪都能拖"，否则点列表也会拖窗口
            on_top=True,          # 始终置顶
            resizable=True,
            shadow=True,
            background_color="#f6f5f2",
            text_select=True,     # 素材要能选中复制
        )
    except Exception as e:
        fatal("创建窗口失败。", e)
        host.stop_backend()
        return 1

    try:
        webview.start(host.worker, gui="edgechromium",
                      private_mode=False,      # 关掉"隐身"：登录状态要留住
                      storage_path=PROFILE_DIR,
                      debug=False)
    except Exception as e:
        fatal("窗口启动失败。", e)
        host.stop_backend()
        return 1

    host.stop_backend()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
