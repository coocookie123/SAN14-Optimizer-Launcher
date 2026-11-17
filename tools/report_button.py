"""SAN14 report helper: skip the turn-start report and offer a "回看报告" button on the map.

Run with pythonw (no console):
  pythonw -P tools/report_button.py                 # game already running or started separately
  pythonw -P tools/report_button.py --steam         # start the game through Steam (desktop shortcut)
  pythonw -P tools/report_button.py <game command>  # also start the game (Steam launch option:
                                                    #   "<pythonw>" -P "<this file>" %command%)
Options before those (from the launcher):
  --no-skip                      do not skip the report (no button); only the 府加成 below, then exit
  --area-bonus <player>,<cpu>    set the 府加成 base values (tools/area_bonus.py), 0-100 each
  --takeover                     in auto mode (看海), Esc gives the starting force back to the player (tools/takeover.py)
  --ai <id>,<id>...              start the game with the computer-AI modules ai/modules/<id>.json (ai/launch.py:
                                 patched behaviour tree, read once at game start). If that fails, the game starts
                                 normally (original AI) and a notice says so
  --trap-near                    陷阱近身触发: a trap goes off only when an enemy army is within 1 hex of it (bin/
                                 san14_ai.dll, so the game is started by ai/launch.py as with --ai); --trap-log also
                                 logs every trap that goes off (tests, logs/ai_dll.log)
What it does:
  * waits for SAN14CE_*.exe, applies the in-memory skip patch v5 (tools/patch.py) once code is decrypted;
  * injects bin/san14_overlay.dll, which draws the button inside the game and skips the report on the
    game's own state thread before it starts (no 2 s pause); once that is active, v5 is removed again.
    Without the DLL, v5 keeps skipping (with the game's 2 s wait) and the button is a layered window;
  * shows a button styled like the game's own buttons, right of the advisor bar's last button
    ("执行建言"), only while the game is in front and the player is on the strategy map;
  * clicking it shows the game's own report of this turn again (DLL: request through shared memory;
    v5: remove the patch, set CUserStrategyState sub-phase 0, re-apply once the report is closed);
  * exits when the game exits.
Logs: logs/helper.log (+ logs/overlay_dll.log written by the DLL)
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import threading
import time
import traceback

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import functools  # noqa: E402

import area_bonus as AB  # noqa: E402
import gamestate as G  # noqa: E402
import ingame  # noqa: E402
import patch as P  # noqa: E402

VERSION = '0.8.0'
STEAM_URL = 'steam://rungameid/872410'
GAME_LAUNCHER = 'san14_launcher.exe'
LOG_DIR = os.path.join(os.path.dirname(HERE), 'logs')
LOG = os.path.join(LOG_DIR, 'helper.log')
AI_DIR = os.path.join(os.path.dirname(HERE), 'ai')
AI_LOG = os.path.join(LOG_DIR, 'ai_dll.log')     # written by bin/san14_ai.dll
FONT_FILE = r'C:\Windows\Fonts\Source Han Serif SC Heavy (TrueType).ttf'
FONT_FALLBACK = r'C:\Windows\Fonts\msyhbd.ttc'
LABEL = '回看报告'

# Layout measured on a 3840x2160 client (2026-11-10): the advisor bar's last button ("执行建言")
# spans x 605..698, y 2060..2123. Ours sits a little to the right with the same height.
REF_W, REF_H = 3840, 2160
BTN_X, BTN_Y_FROM_BOTTOM, BTN_W, BTN_H = 722, 100, 236, 64
MARGIN = 10          # room for the drop shadow (reference pixels)

user32 = ctypes.WinDLL('user32', use_last_error=True)
gdi32 = ctypes.WinDLL('gdi32')
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
try:
    ctypes.WinDLL('shcore').SetProcessDpiAwareness(2)
except Exception:
    user32.SetProcessDPIAware()

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.FindWindowW.restype = wt.HWND
user32.GetForegroundWindow.restype = wt.HWND
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE), wt.HDC,
                                       ctypes.POINTER(wt.POINT), wt.COLORREF, ctypes.c_void_p, wt.DWORD]
user32.LoadCursorW.restype = wt.HANDLE
user32.LoadCursorW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]
user32.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_size_t]
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]

WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x8, 0x80, 0x08000000
WM_DESTROY, WM_TIMER, WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x2, 0x113, 0x200, 0x201, 0x202
WM_MOUSELEAVE, WM_MOUSEACTIVATE, WM_HOTKEY = 0x2A3, 0x21, 0x312
MA_NOACTIVATE = 3
VK_F8, SC_F8, KEYEVENTF_KEYUP = 0x77, 0x42, 0x2
# CUserStrategyState::Update + 0x16 in the original code: mov eax, [rsi+470h] (read the sub-phase).
# 「三国志14 优化伴侣」 (Frida) hooks exactly here to skip the report itself; F8 opens its report again.
STRATEGY_STAGE_READ = bytes.fromhex('8b8670040000')
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
HWND_TOPMOST = wt.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
TME_LEAVE = 0x2
MOD_ALT, MOD_CONTROL = 0x1, 0x2


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [('cbSize', wt.UINT), ('style', wt.UINT), ('lpfnWndProc', WNDPROC), ('cbClsExtra', ctypes.c_int),
                ('cbWndExtra', ctypes.c_int), ('hInstance', wt.HINSTANCE), ('hIcon', wt.HICON), ('hCursor', wt.HANDLE),
                ('hbrBackground', wt.HBRUSH), ('lpszMenuName', wt.LPCWSTR), ('lpszClassName', wt.LPCWSTR), ('hIconSm', wt.HICON)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [('cbSize', wt.DWORD), ('dwFlags', wt.DWORD), ('hwndTrack', wt.HWND), ('dwHoverTime', wt.DWORD)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wt.DWORD), ('biWidth', ctypes.c_long), ('biHeight', ctypes.c_long), ('biPlanes', wt.WORD),
                ('biBitCount', wt.WORD), ('biCompression', wt.DWORD), ('biSizeImage', wt.DWORD),
                ('biXPelsPerMeter', ctypes.c_long), ('biYPelsPerMeter', ctypes.c_long), ('biClrUsed', wt.DWORD),
                ('biClrImportant', wt.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [('BlendOp', ctypes.c_ubyte), ('BlendFlags', ctypes.c_ubyte), ('SourceConstantAlpha', ctypes.c_ubyte),
                ('AlphaFormat', ctypes.c_ubyte)]


LOG_LIMIT = 1 << 20          # per run; the previous run is kept as *.prev.log
DLL_LOG = os.path.join(os.path.dirname(LOG), 'overlay_dll.log')
_log = {'bytes': 0, 'stopped': False, 'seen': {}}


def rotate_logs(include_dll):
    """Keep only the previous run: x.log -> x.prev.log, then start empty."""
    os.makedirs(LOG_DIR, exist_ok=True)
    for path in (LOG, DLL_LOG) if include_dll else (LOG,):
        try:
            if os.path.exists(path):
                os.replace(path, path[:-4] + '.prev.log')
        except OSError:
            pass                 # e.g. the DLL still has its log open; keep appending


def log(msg, dedupe=False):
    """Append a line. dedupe=True: the same message is written at most once a minute."""
    now = time.time()
    if dedupe:
        key = msg.split('\n', 1)[0]
        seen = _log['seen'].get(key)
        if seen and now - seen[0] < 60:
            seen[1] += 1
            return
        if seen and seen[1]:
            msg += '\n  (same message suppressed %d times in the previous minute)' % seen[1]
        _log['seen'][key] = [now, 0]
    if _log['stopped']:
        return
    line = time.strftime('%Y-%m-%d %H:%M:%S ') + msg + '\n'
    if _log['bytes'] + len(line) > LOG_LIMIT:
        _log['stopped'] = True
        line = time.strftime('%Y-%m-%d %H:%M:%S ') + 'log size limit reached; nothing more is logged this run\n'
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(line)
        _log['bytes'] += len(line.encode('utf-8'))
    except OSError:
        pass


# ----------------------------------------------------------------------------------------------
# Button rendering (colours sampled from the game's own advisor-bar buttons)
# ----------------------------------------------------------------------------------------------
BODY_STOPS = [(0.00, (222, 227, 222)), (0.25, (213, 216, 211)), (0.45, (207, 209, 194)), (0.60, (211, 206, 176)),
              (0.80, (227, 223, 188)), (0.94, (239, 234, 194)), (1.00, (230, 221, 176))]
RIM = (187, 176, 112)
RIM_HOVER = (236, 190, 64)
TEXT = (68, 34, 30)
TEXT_BUSY = (140, 120, 100)


def _grad(stops, t):
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            k = (t - t0) / (t1 - t0) if t1 > t0 else 0
            return tuple(int(c0[i] + (c1[i] - c0[i]) * k) for i in range(3))
    return stops[-1][1]


_font_cache = {}
# (file, variation): Noto Serif SC Black matches the game's heavy serif and has every glyph we need;
# the Source Han Serif Heavy file on this machine lacks CJK punctuation; Microsoft YaHei Bold ships
# with Windows 10/11 everywhere.
FONT_CHAIN = [(r'C:\Windows\Fonts\NotoSerifSC-VF.ttf', b'Black'), (FONT_FILE, None), (FONT_FALLBACK, None)]


def _font(px):
    if px not in _font_cache:
        for path, var in FONT_CHAIN:
            try:
                f = ImageFont.truetype(path, px)
                if var:
                    f.set_variation_by_name(var)
                _font_cache[px] = f
                break
            except (OSError, ValueError):
                continue
    return _font_cache[px]


def render(scale, label, hover=False, pressed=False, busy=False, w_ref=BTN_W, font_ref=32):
    """Return an RGBA image (shadow margin included) at the given scale. w_ref: width in reference px."""
    ss = 4                                   # supersampling for smooth corners
    w, h, m = round(w_ref * scale), round(BTN_H * scale), round(MARGIN * scale)
    W, H = (w + 2 * m) * ss, (h + 2 * m) * ss
    r = max(2, round(7 * scale)) * ss
    box = (m * ss, m * ss, (m + w) * ss - 1, (m + h) * ss - 1)

    # drop shadow
    shadow = Image.new('L', (W, H), 0)
    off = round(3 * scale) * ss
    ImageDraw.Draw(shadow).rounded_rectangle((box[0] + off, box[1] + off, box[2] + off, box[3] + off), r, fill=170)
    shadow = shadow.filter(ImageFilter.GaussianBlur(3 * scale * ss))
    img = Image.new('RGBA', (W, H), (28, 18, 10, 0))
    img.putalpha(shadow)

    # rim
    mask = Image.new('L', (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, r, fill=255)
    rim = Image.new('RGBA', (W, H), RIM_HOVER if hover else RIM)
    img = Image.composite(rim, img, mask)

    # body gradient
    rimw = round((3 if hover else 2) * scale) * ss
    inner = (box[0] + rimw, box[1] + rimw, box[2] - rimw, box[3] - rimw)
    body = Image.new('RGB', (1, inner[3] - inner[1] + 1))
    for y in range(body.height):
        c = _grad(BODY_STOPS, y / max(1, body.height - 1))
        if pressed:
            c = tuple(int(v * 0.88) for v in c)
        elif hover:
            c = tuple(min(255, int(v * 1.03)) for v in c)
        body.putpixel((0, y), c)
    body = body.resize((inner[2] - inner[0] + 1, body.height))
    bmask = Image.new('L', (W, H), 0)
    ImageDraw.Draw(bmask).rounded_rectangle(inner, max(1, r - rimw), fill=255)
    layer = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    layer.paste(body, (inner[0], inner[1]))
    img = Image.composite(layer, img, bmask)

    img = img.resize((W // ss, H // ss), Image.LANCZOS)

    # label
    d = ImageDraw.Draw(img)
    f = _font(max(10, round(font_ref * scale)))
    tb = d.textbbox((0, 0), label, font=f)
    tx = m + (w - (tb[2] - tb[0])) // 2 - tb[0]
    ty = m + (h - (tb[3] - tb[1])) // 2 - tb[1] + (1 if pressed else 0)
    d.text((tx, ty), label, font=f, fill=TEXT_BUSY if busy else TEXT)
    return img


def to_bgra(img):
    """RGBA image -> premultiplied BGRA bytes (what both the DLL and UpdateLayeredWindow expect)."""
    r, g, b, a = img.split()
    r, g, b = (ImageChops.multiply(ch, a) for ch in (r, g, b))
    return Image.merge('RGBA', (b, g, r, a)).tobytes(), img.size[0], img.size[1]


@functools.lru_cache(maxsize=32)
def button_bgra(scale, hover, pressed, busy):
    """Premultiplied BGRA bytes of the rendered button: (data, w, h)."""
    return to_bgra(render(scale, LABEL, hover, pressed, busy))


def notice_bgra(scale, text):
    """A notice bar in the same style as the button, wide enough for the text (max 1024 px)."""
    font_ref = 26
    f = _font(max(10, round(font_ref * scale)))
    tw = ImageDraw.Draw(Image.new('L', (1, 1))).textbbox((0, 0), text, font=f)[2]
    w_max = (1024 - 2 * MARGIN * scale - 4) / scale
    if tw / scale + 56 > w_max:              # too long for the 1024 px texture (4K: scale 2): smaller font
        font_ref = max(10 / scale, font_ref * (w_max - 56) / (tw / scale))
        tw = ImageDraw.Draw(Image.new('L', (1, 1))).textbbox(
            (0, 0), text, font=_font(max(10, round(font_ref * scale))))[2]
    w_ref = min(tw / scale + 56, w_max)
    return to_bgra(render(scale, text, w_ref=w_ref, font_ref=font_ref))


def layered_put(hwnd, data, w, h, x, y):
    """Show premultiplied BGRA pixels in a layered window with its top-left at (x, y)."""
    hdc_screen = user32.GetDC(None)
    hdc = gdi32.CreateCompatibleDC(hdc_screen)
    bmi = BITMAPINFOHEADER()
    bmi.biSize, bmi.biWidth, bmi.biHeight, bmi.biPlanes, bmi.biBitCount = ctypes.sizeof(bmi), w, -h, 1, 32
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    ctypes.memmove(bits, data, len(data))
    old = gdi32.SelectObject(hdc, hbmp)
    pt_dst, size, pt_src = wt.POINT(x, y), wt.SIZE(w, h), wt.POINT(0, 0)
    blend = BLENDFUNCTION(0, 0, 255, 1)
    user32.UpdateLayeredWindow(hwnd, hdc_screen, ctypes.byref(pt_dst), ctypes.byref(size), hdc,
                               ctypes.byref(pt_src), 0, ctypes.byref(blend), 2)
    gdi32.SelectObject(hdc, old)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc)
    user32.ReleaseDC(None, hdc_screen)


# ----------------------------------------------------------------------------------------------
# Layered window (fallback display) + timer host
# ----------------------------------------------------------------------------------------------
class Overlay:
    def __init__(self, helper):
        self.helper = helper
        self.hover = self.pressed = False
        self.tracking = False
        self.shown = False
        self.ingame = None          # ingame.Shared when drawing inside the game
        self.blank = False          # shown but fully transparent
        self.geom = None            # (x, y, scale)
        self.drawn_key = None
        hinst = kernel32.GetModuleHandleW(None)
        self._proc = WNDPROC(self.wndproc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(wc)
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32649))   # IDC_HAND
        wc.lpszClassName = 'SAN14ReportButton'
        user32.RegisterClassExW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                                           'SAN14ReportButton', '回看报告', WS_POPUP, 0, 0, 10, 10,
                                           None, None, hinst, None)
        user32.SetTimer(self.hwnd, 1, 120, None)

    def attach_ingame(self, sh):
        """Draw inside the game (bin/san14_overlay.dll) instead of with this window."""
        self.ingame = sh
        self.ig_key = None
        self.last_clicks = sh.get('clicks')
        user32.KillTimer(self.hwnd, 1)
        user32.SetTimer(self.hwnd, 1, 50, None)

    def tick_ingame(self):
        sh, helper = self.ingame, self.helper
        sh.heartbeat()
        if not helper.alive():
            helper.game_gone = True
            user32.PostQuitMessage(0)
            return
        bbw, bbh = sh.get('bb_w'), sh.get('bb_h')
        show = bool(bbw) and not helper.busy.locked() and helper.on_map() is not None
        if show:
            scale = round(min(bbw / REF_W, bbh / REF_H), 4)
            hover, pressed = bool(sh.get('hover')), bool(sh.get('pressed'))
            key = (scale, bbw, bbh, hover, pressed)
            if key != self.ig_key:
                data, w, h = button_bgra(scale, hover, pressed, False)
                m = round(MARGIN * scale)
                x = round(BTN_X * scale)
                y = bbh - round(BTN_Y_FROM_BOTTOM * scale)
                sh.put_image(data, w, h)
                sh.set('img_x', x - m)
                sh.set('img_y', y - m)
                sh.set('hit_x0', x)
                sh.set('hit_y0', y)
                sh.set('hit_x1', x + round(BTN_W * scale))
                sh.set('hit_y1', y + round(BTN_H * scale))
                self.ig_key = key
        sh.set('visible', 1 if show else 0)
        clicks = sh.get('clicks')
        if clicks != self.last_clicks:
            self.last_clicks = clicks
            if show:
                helper.request_report()

    def draw(self):
        x, y, scale = self.geom
        busy = self.helper.busy.locked()
        key = (x, y, scale, self.hover, self.pressed, busy, self.blank)
        if key == self.drawn_key:
            return
        self.drawn_key = key
        if self.blank:
            # fully transparent: invisible and click-through, but the window stays on screen
            m = round(MARGIN * scale)
            w, h = round(BTN_W * scale) + 2 * m, round(BTN_H * scale) + 2 * m
            data = bytes(w * h * 4)
        else:
            data, w, h = button_bgra(scale, self.hover, self.pressed, busy)
        m = round(MARGIN * scale)
        layered_put(self.hwnd, data, w, h, x - m, y - m)

    def set_visible(self, on):
        if on and not self.shown:
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
        elif not on and self.shown:
            user32.ShowWindow(self.hwnd, SW_HIDE)
            self.hover = self.pressed = False
        self.shown = on

    def tick(self):
        if self.ingame:
            try:
                self.tick_ingame()
            except Exception:
                log('ingame tick error:\n' + traceback.format_exc(), dedupe=True)
            return
        try:
            mode, geom = self.helper.where()
        except Exception as e:
            log('tick error: %s' % e, dedupe=True)
            mode, geom = 'hidden', None
        if self.helper.game_gone:
            user32.PostQuitMessage(0)
            return
        if mode != 'hidden':
            if mode == 'blank' and not self.blank:
                self.hover = self.pressed = False
            self.blank = mode == 'blank'
            self.geom = geom
            self.draw()
        self.set_visible(mode != 'hidden')

    def wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_TIMER:
                self.tick()
                return 0
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
            if msg == WM_MOUSEMOVE:
                if not self.tracking:
                    tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), TME_LEAVE, hwnd, 0)
                    user32.TrackMouseEvent(ctypes.byref(tme))
                    self.tracking = True
                if not self.hover:
                    self.hover = True
                    self.draw()
                return 0
            if msg == WM_MOUSELEAVE:
                self.tracking = False
                self.hover = self.pressed = False
                if self.shown:
                    self.draw()
                return 0
            if msg == WM_LBUTTONDOWN:
                self.pressed = True
                self.draw()
                return 0
            if msg == WM_LBUTTONUP:
                was = self.pressed and not self.blank
                self.pressed = False
                self.draw()
                if was:
                    self.helper.request_report()
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            log('wndproc error:\n' + traceback.format_exc(), dedupe=True)
        return user32.DefWindowProcW(hwnd, msg, wp, lp)


# ----------------------------------------------------------------------------------------------
# Game side
# ----------------------------------------------------------------------------------------------
class Helper:
    def __init__(self, game):
        self.g = game
        self.busy = threading.Lock()
        self.game_gone = False
        self.hproc = kernel32.OpenProcess(0x00100000, False, game.pid)   # SYNCHRONIZE
        self.sh = None              # ingame.Shared once the DLL is in the game
        self.dll_skip = False       # the DLL skips the report itself (else patch v5 does)

    def use_dll_skip(self, sh, timeout=10):
        """Wait for the DLL's own report skip; once active, remove patch v5 (it would only slow things)."""
        self.sh = sh
        t0 = time.time()
        while time.time() - t0 < timeout and sh.get('skip_state') not in (1, 3) and self.alive():
            sh.heartbeat()
            time.sleep(0.1)
        state = sh.get('skip_state')
        if state != 1:
            log('dll report skip not active (%s); keeping patch v5' % ingame.SKIP_STATES.get(state, state))
            return
        try:
            ok, msg = P.restore(self.g.pid, self.g.hp)
        except Exception as e:
            ok, msg = False, str(e)
        if ok:
            self.dll_skip = True
            log('dll report skip active (%.1f s); patch v5 removed (%s)' % (time.time() - t0, msg))
        else:
            # both at once would break "回看报告" (v5 empties the report it shows): stay with v5 only
            sh.set('skip_req', 0)
            log('dll report skip turned off again: could not remove patch v5 (%s)' % msg)

    def alive(self):
        return kernel32.WaitForSingleObject(self.hproc, 0) != 0

    def on_map(self):
        st = self.g.stack()
        if not st:
            return None
        obj, name = st[-1]
        if name != 'CUserStrategyState' or self.g.subphase(obj) not in (1, 2):
            return None
        return obj

    def where(self):
        """(mode, (x, y, scale)) in screen pixels; mode is 'hidden', 'blank' or 'button'.

        While the game is in front the window is never hidden, only drawn fully transparent
        ('blank'): showing/hiding a window over a borderless game makes Windows switch between
        direct scan-out and composition, which can flash the screen for a frame.
        """
        if not self.alive():
            self.game_gone = True
            return 'hidden', None
        hwnd = user32.FindWindowW('KT_SANGOKUSHI14_WINDOW', None)
        if not hwnd or user32.IsIconic(hwnd):
            return 'hidden', None
        fg = user32.GetForegroundWindow()
        if fg != hwnd:
            return 'hidden', None
        mode = 'button' if (not self.busy.locked() and self.on_map() is not None) else 'blank'
        cr = wt.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(cr))
        pt = wt.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        cw, ch = cr.right, cr.bottom
        if cw < 200 or ch < 200:
            return 'hidden', None
        scale = min(cw / REF_W, ch / REF_H)
        x = pt.x + round(BTN_X * scale)
        y = pt.y + ch - round(BTN_Y_FROM_BOTTOM * scale)
        return mode, (x, y, round(scale, 4))

    def request_report(self):
        threading.Thread(target=self.show_report, daemon=True).start()

    def show_report(self):
        if not self.busy.acquire(blocking=False):
            return
        try:
            obj = self.on_map()
            if obj is None:
                log('report: not on the strategy map, ignored')
                return
            if self.foreign_hook(obj):
                self.show_report_companion(obj)
                return
            if self.dll_skip:
                self.show_report_dll(obj)
                return
            ok, msg = P.restore(self.g.pid, self.g.hp)
            if not ok:
                log('report: could not remove patch: %s' % msg)
                return
            # the game's own CReportWaitState would hold the report back for up to 2 s; let it end
            # on its next frame (only for this button-triggered report; put back as soon as it is gone)
            fast = P.set_wait(self.g.pid, self.g.hp, 0)
            self.g.set_subphase(obj, 0)
            log('report: shown again (fast open %s)' % fast)
            seen, t0 = False, time.time()
            while time.time() - t0 < 900 and self.alive():
                time.sleep(0.05 if fast else 0.25)
                names = [n for _, n in self.g.stack()]
                if fast and (('CReportDisplayState' in names) or (time.time() - t0 > 5)):
                    fast = not P.set_wait(self.g.pid, self.g.hp, P.WAIT_ORIG)
                if 'CReportDisplayState' in names:
                    seen = True
                elif names and names[-1] == 'CUserStrategyState' and self.g.subphase(obj) in (1, 2):
                    if seen or time.time() - t0 > 20:
                        break
            if self.alive():
                ok, msg = P.apply(self.g.pid, self.g.hp)
                log('report: closed, patch %s (%s)' % ('back' if ok else 'NOT back', msg))
        except Exception:
            log('report error:\n' + traceback.format_exc(), dedupe=True)
        finally:
            self.busy.release()

    def show_report_dll(self, obj):
        """The DLL sets sub-phase 0 on the game's thread and lets the game run its own report."""
        sh = self.sh
        req = (sh.get('reopen_ack') + 1) & 0xFFFFFFFF
        sh.set('reopen_req', req)
        t0 = time.time()
        while sh.get('reopen_ack') != req and time.time() - t0 < 2 and self.alive():
            time.sleep(0.02)
        if sh.get('reopen_ack') != req:
            sh.set('reopen_req', sh.get('reopen_ack'))      # withdraw: must not fire later by surprise
            log('report: the game did not take the request (not on the map any more?)')
            return
        log('report: shown again (in game, %.2f s)' % (time.time() - t0))
        seen = self.wait_report(obj, 5)
        log('report: closed' if seen else 'report: not seen on the state stack')

    def wait_report(self, obj, open_timeout):
        """Wait while the report is up (the button stays hidden: busy). False if it never appeared."""
        seen, t0 = False, time.time()
        while time.time() - t0 < 900 and self.alive():
            time.sleep(0.05 if not seen else 0.1)
            names = [n for _, n in self.g.stack()]
            if 'CReportDisplayState' in names or 'CReportWaitState' in names:
                seen = True
            elif names and names[-1] == 'CUserStrategyState' and self.g.subphase(obj) in (1, 2):
                if seen or time.time() - t0 > open_timeout:
                    break
        return seen

    def foreign_hook(self, obj):
        """True while 「三国志14 优化伴侣」 has patched CUserStrategyState::Update to skip the report itself."""
        if self.sh is not None and self.sh.get('skip_state') == 1:     # our DLL watches it (slot 5 is ours)
            return bool(self.sh.get('foreign_hook'))
        update = self.g.q(self.g.q(obj) + 5 * 8)
        return P.rpm(self.g.hp, update + 0x16, len(STRATEGY_STAGE_READ)) != STRATEGY_STAGE_READ

    def show_report_companion(self, obj):
        """优化伴侣 skips the report; its own F8 (read on the game thread while the map is up) shows it again."""
        hwnd = game_window_of(self.g.pid)
        if not hwnd or user32.GetForegroundWindow() != hwnd:
            log('report: game not in front, F8 not sent')
            return
        v5 = not self.dll_skip
        if v5:                       # patch v5 would empty the report the companion opens
            ok, msg = P.restore(self.g.pid, self.g.hp)
            if not ok:
                log('report: could not remove patch v5 for the companion: %s' % msg)
                return
        try:
            user32.keybd_event(VK_F8, SC_F8, 0, 0)
            time.sleep(0.15)
            user32.keybd_event(VK_F8, SC_F8, KEYEVENTF_KEYUP, 0)
            if self.wait_report(obj, 2):
                log('report: opened by the companion (F8) and closed')
            elif self.dll_skip:
                log('report: the companion did not open it (its 略过旬末报告 off?); opening it ourselves')
                self.show_report_dll(obj)
            else:
                log('report: the companion did not open it (its 略过旬末报告 off?)')
        finally:
            if v5 and self.alive():
                ok, msg = P.apply(self.g.pid, self.g.hp)
                log('report: patch v5 %s (%s)' % ('back' if ok else 'NOT back', msg))


def process_names():
    snap = P.k32.CreateToolhelp32Snapshot(0x2, 0)
    pe = P.PE32W()
    pe.dwSize = ctypes.sizeof(pe)
    names = set()
    ok = P.k32.Process32FirstW(snap, ctypes.byref(pe))
    while ok:
        names.add(pe.szExeFile.lower())
        ok = P.k32.Process32NextW(snap, ctypes.byref(pe))
    P.k32.CloseHandle(snap)
    return names


TESTED_VERSIONS = {'1.0.2.58226'}      # SAN14CE_SC.exe, Steam build 25953386
try:                                   # and every version marked "tested" in sigs/sigs.json
    import sigs as _sigs
    TESTED_VERSIONS |= _sigs.load().tested_versions()
except Exception:
    pass
UNSUPPORTED_AFTER = 15                 # s after the game window appeared (code is decrypted by then)

version_dll = ctypes.WinDLL('version')
version_dll.GetFileVersionInfoSizeW.argtypes = [wt.LPCWSTR, ctypes.POINTER(wt.DWORD)]
version_dll.GetFileVersionInfoW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p]
version_dll.VerQueryValueW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.UINT)]
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]


def exe_info(pid):
    """(exe path, file version 'a.b.c.d' or '?') of the running game."""
    path, ver = '?', '?'
    h = kernel32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        buf = ctypes.create_unicode_buffer(1024)
        n = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            path = buf.value
        kernel32.CloseHandle(h)
    try:
        size = version_dll.GetFileVersionInfoSizeW(path, None)
        data = ctypes.create_string_buffer(size)
        p, ln = ctypes.c_void_p(), wt.UINT()
        if size and version_dll.GetFileVersionInfoW(path, 0, size, data) and \
                version_dll.VerQueryValueW(data, '\\', ctypes.byref(p), ctypes.byref(ln)):
            ffi = ctypes.cast(p, ctypes.POINTER(ctypes.c_uint32 * 13)).contents   # VS_FIXEDFILEINFO
            ms, ls = ffi[2], ffi[3]
            ver = '%d.%d.%d.%d' % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except Exception:
        pass
    return path, ver


def game_window_of(pid):
    hwnd = user32.FindWindowW('KT_SANGOKUSHI14_WINDOW', None)
    if not hwnd:
        return None
    owner = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
    return hwnd if owner.value == pid else None


def wait_and_patch(skip=True, timeout=3600):
    """('ok', Game) | ('unsupported', pid, reason) | None (no game / gave up).

    skip: apply patch v5 (also the version check); else only check that the 府加成 site is there."""
    t0 = time.time()
    last = ''
    launcher_seen = launcher_gone_at = None
    window_since = None
    logged_pid = None
    while time.time() - t0 < timeout:
        # the game's own launcher closed without starting the game -> stop waiting
        names = process_names()
        if GAME_LAUNCHER in names:
            launcher_seen, launcher_gone_at = True, None
        elif launcher_seen and not (names & set(P.GAME_EXES)):
            launcher_gone_at = launcher_gone_at or time.time()
            if time.time() - launcher_gone_at > 30:
                log('game launcher closed without starting the game; helper stops')
                return None
        pid, hp = P.open_game()
        if hp:
            if pid != logged_pid:
                path, ver = exe_info(pid)
                log('game: %s, version %s (%s)' % (path, ver, 'tested' if ver in TESTED_VERSIONS else 'NOT tested'))
                logged_pid, window_since = pid, None
            if window_since is None and game_window_of(pid):
                window_since = time.time()
            try:
                if skip:
                    ok, msg = P.apply(pid, hp)
                else:
                    AB.locate(hp)
                    ok, msg = True, 'report skip off; game code checked'
                if ok:
                    log('pid %d: %s' % (pid, msg))
                    return 'ok', G.Game(pid, hp)
                last = msg
            except Exception as e:
                last = str(e)
            P.k32.CloseHandle(hp)
            if window_since and time.time() - window_since > UNSUPPORTED_AFTER:
                return 'unsupported', pid, last
        time.sleep(2)
    log('gave up waiting for the game: %s' % last)
    return None


def setup_ingame(pid, skip=False):
    """Inject the DLL and wait for its hooks. Returns ingame.Shared or None.

    skip: also ask the DLL to skip the turn-start report itself (see Helper.use_dll_skip)."""
    if not os.path.exists(ingame.DLL):
        return None
    try:
        sh = ingame.Shared()
        sh.heartbeat()
        sh.set('cmd', 0)
        sh.set('visible', 0)
        if skip:
            sh.set('reopen_req', sh.get('reopen_ack'))
        sh.set('skip_req', 1 if skip else 0)
        if not ingame.inject(pid):
            log('dll injection failed; using window overlay')
            return None
        t0 = time.time()
        while time.time() - t0 < 15 and sh.get('dll_state') != 1:
            sh.heartbeat()
            time.sleep(0.2)
        if sh.get('dll_state') != 1:
            log('dll loaded but not hooked (state %d); using window overlay' % sh.get('dll_state'))
            return None
        return sh
    except Exception:
        log('in-game overlay setup failed; using window overlay\n' + traceback.format_exc())
        return None


def show_notice(pid, text, seconds=12):
    """Show a one-line notice where the button would be: inside the game, else as a layered window."""
    sh = setup_ingame(pid)
    if sh:
        t0 = time.time()
        while not sh.get('bb_w') and time.time() - t0 < 5:     # first frame after hooking
            sh.heartbeat()
            time.sleep(0.1)
        bbw, bbh = sh.get('bb_w') or REF_W, sh.get('bb_h') or REF_H
        scale = round(min(bbw / REF_W, bbh / REF_H), 4)
        data, w, h = notice_bgra(scale, text)
        m = round(MARGIN * scale)
        sh.put_image(data, w, h)
        sh.set('img_x', round(BTN_X * scale) - m)
        sh.set('img_y', bbh - round(BTN_Y_FROM_BOTTOM * scale) - m)
        for k in ('hit_x0', 'hit_y0', 'hit_x1', 'hit_y1'):      # no click area: the notice never eats clicks
            sh.set(k, 0)
        sh.set('visible', 1)
        t0 = time.time()
        while time.time() - t0 < seconds and P.find_pid() == pid:
            sh.heartbeat()
            time.sleep(0.2)
        sh.set('visible', 0)
        log('notice shown in game: %s' % text)
        return
    hwnd_game = None
    t0 = time.time()
    while not hwnd_game and time.time() - t0 < 30:
        hwnd_game = game_window_of(pid)
        time.sleep(0.5)
    if not hwnd_game:
        log('notice not shown (no game window): %s' % text)
        return
    cr, pt = wt.RECT(), wt.POINT(0, 0)
    user32.GetClientRect(hwnd_game, ctypes.byref(cr))
    user32.ClientToScreen(hwnd_game, ctypes.byref(pt))
    scale = round(min(cr.right / REF_W, cr.bottom / REF_H), 4) or 1.0
    data, w, h = notice_bgra(scale, text)
    m = round(MARGIN * scale)
    hinst = kernel32.GetModuleHandleW(None)
    proc = WNDPROC(lambda hw, msg, wp, lp: user32.DefWindowProcW(hw, msg, wp, lp))
    wc = WNDCLASSEXW()
    wc.cbSize, wc.lpfnWndProc, wc.hInstance, wc.lpszClassName = ctypes.sizeof(wc), proc, hinst, 'SAN14ReportNotice'
    user32.RegisterClassExW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | 0x20,  # TRANSPARENT
                                  'SAN14ReportNotice', '', WS_POPUP, 0, 0, w, h, None, None, hinst, None)
    layered_put(hwnd, data, w, h, pt.x + round(BTN_X * scale) - m, pt.y + cr.bottom - round(BTN_Y_FROM_BOTTOM * scale) - m)
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    msg = wt.MSG()
    t0 = time.time()
    while time.time() - t0 < seconds and P.find_pid() == pid:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.05)
    user32.DestroyWindow(hwnd)
    log('notice shown as window: %s' % text)


def parse_options(args):
    """Leading options from the launcher: --no-skip, --area-bonus <player>,<computer>, --takeover, --ai <ids>.
    Returns (opts, rest)."""
    opts = {'skip': True, 'area': None, 'takeover': False, 'ai': [], 'ai_ab': None, 'hr_dll': None, 'hr_block': '',
            'trap': False, 'trap_log': False}
    args = list(args)
    while args and args[0] in ('--no-skip', '--area-bonus', '--takeover', '--ai', '--ai-ab', '--hr-dll', '--hr-block',
                               '--trap-near', '--trap-log'):
        a = args.pop(0)
        if a == '--trap-near':         # 陷阱近身触发 (in san14_ai.dll: the game is then started by us)
            opts['trap'] = True
        elif a == '--trap-log':        # tests: log every trap that goes off
            opts['trap_log'] = True
        elif a == '--hr-dll':          # the user's 人力资源管理器 DLL, loaded with ours (the game is then started by us)
            if args:
                opts['hr_dll'] = args.pop(0)
        elif a == '--hr-block':        # its features to keep out, comma list (commandbook)
            if args:
                opts['hr_block'] = args.pop(0)
        elif a == '--no-skip':
            opts['skip'] = False
        elif a == '--takeover':
            opts['takeover'] = True
        elif a == '--ai-ab':           # tests: modules only for odd/even force ids; odd:<id>+<id> only those modules
            if args and args[0].partition(':')[0] in ('odd', 'even'):
                opts['ai_ab'] = args.pop(0)
        elif a == '--ai':
            if args:
                opts['ai'] = [i.strip() for i in args.pop(0).split(',') if i.strip()]
        elif args:
            try:
                p, c = args.pop(0).split(',')
                opts['area'] = (AB.clamp(p), AB.clamp(c))
            except ValueError:
                pass
    return opts, args


def apply_area_bonus(game, area):
    try:
        ok, msg = AB.apply(game.hp, *area)
    except Exception as e:
        ok, msg = False, str(e)
    log('area bonus (player %d, computer %d): %s %s' % (area + ('ok' if ok else 'FAILED', msg)))


def start_takeover(game):
    try:
        import takeover
        takeover.Takeover(game.pid, game.hp, log=log).start()
        log('takeover: Esc in auto mode returns the starting force to the player')
    except Exception:
        log('takeover setup failed:\n' + traceback.format_exc())


def ai_launch():
    """ai/launch.py, logging to logs/ai_dll.log."""
    if AI_DIR not in sys.path:
        sys.path.insert(0, AI_DIR)
    import launch
    launch.LOG = AI_LOG
    return launch


def game_to_start(launch, args):
    """(game_dir, exe) for --steam or an exe path from the launcher."""
    if args == ['--steam']:
        return launch.steam_game_dir(), None
    exe = os.path.abspath(args[0])
    if not os.path.basename(exe).lower().startswith('san14ce'):
        exe = None      # e.g. the official launcher (Steam launch option): start the CE exe next to it
    return os.path.dirname(os.path.abspath(args[0])), exe


def start_with_hr(args, hr_dll, hr_block='', trap=False, trap_log=False):
    """No AI modules, but the 人力资源管理器 DLL and / or 陷阱近身触发: start the game ourselves with the official tree
    (san14_ai.dll only routes the HR DLL's DirectInput8Create and its files, and hooks the traps' 发动范围).
    (pid, None) | (None, reason)."""
    try:
        launch = ai_launch()
        game_dir, exe = game_to_start(launch, args)
        return launch.start(game_dir, None, exe, extra_dlls=[hr_dll] if hr_dll else (), hr_block=hr_block,
                            trap_near=trap, trap_log=trap_log), None
    except Exception as e:
        return None, '%s: %s' % (type(e).__name__, e)


def start_with_ai(ids, args, ab=None, hr_dll=None, hr_block='', trap=False, trap_log=False):
    """Build the tree from ai/modules/<id>.json and start the game with it. (pid, None) | (None, reason);
    on failure nothing was started and the caller starts the game the normal way."""
    try:
        launch = ai_launch()
        if args == ['--steam']:
            game_dir, exe = launch.steam_game_dir(), None
        else:
            exe = os.path.abspath(args[0])
            game_dir = os.path.dirname(exe)
            if not os.path.basename(exe).lower().startswith('san14ce'):
                exe = None      # e.g. the official launcher (Steam launch option): start the CE exe next to it
        paths = [os.path.join(AI_DIR, 'modules', i + '.json') for i in ids]
        missing = [os.path.basename(p) for p in paths if not os.path.isfile(p)]
        if missing:
            return None, 'module file missing: ' + ', '.join(missing)
        bt, problems = launch.btpatch.build(paths, source=os.path.join(game_dir, '0000', 'bt_exce.bin'), ab=ab)
        if problems:
            return None, 'modules do not fit this game version: ' + '; '.join(problems)
        return launch.start(game_dir, bt, exe, features=launch.btpatch.dll_features(paths),
                            ab=launch.btpatch.ab_dll(ab, paths), extra_dlls=[hr_dll] if hr_dll else (),
                            hr_block=hr_block, trap_near=trap, trap_log=trap_log), None
    except Exception as e:
        return None, '%s: %s' % (type(e).__name__, e)


def trap_failure(pid, why):
    """None if 陷阱近身触发 is on in the game we started, else the notice to show (the reason goes to the log)."""
    if pid:
        ok, line = ai_launch().wait_trap(timeout=60)
        if ok:
            log('trapnear active: %s' % line)
            return None
        why = line
    log('trapnear NOT active: %s' % why)
    return '三国志14优化：陷阱近身触发没有生效，本局陷阱照原版（详见 logs 里的 helper.log）'


def ai_failure(ai_pid, why):
    """None if the game read the patched tree, else the notice to show (the reason goes to the log)."""
    if ai_pid:
        text = ai_launch().wait_log(timeout=60)
        hits = [l.strip() for l in text.splitlines() if 'redirect #' in l]
        if hits and hits[-1].split(' : ')[-1].startswith('ok'):
            log('ai modules active: %s' % hits[-1])
            return None
        why = 'the game did not read the patched tree (%s)' % (hits[-1] if hits else 'no redirect in ' + AI_LOG)
    log('ai modules NOT active: %s' % why)
    return '三国志14优化：电脑 AI 模块没有生效，本局是原版 AI（详见 logs 里的 helper.log）'


def run_partial(pid, ver, opts, ai_pid, ai_why, trap_pid=None, trap_why=None):
    """The report skip's code did not match this game version (with --no-skip: the 府加成 code did not).
    Check every feature on its own against sigs/sigs.json (tools/sigcheck.py) and still run the requested
    ones whose code matches; the notice names what is off. False if the check failed or nothing else runs
    (the caller then shows the usual all-off notice)."""
    try:
        import types
        import sigcheck
        hp = P.k32.OpenProcess(P.PROCESS_ACCESS, False, pid)
        if not hp:
            return False
        feats = sigcheck.check_features(hp)
    except Exception:
        log('per-feature check failed:\n' + traceback.format_exc())
        return False
    names = sigcheck.S.load().features
    for fid, (ok, why) in feats.items():
        log('feature %s: %s%s' % (fid, 'ok' if ok else 'OFF', ('; ' + '; '.join(why)) if why else ''))
    area = bool(opts['area']) and feats['area_bonus'][0]
    takeover = opts['takeover'] and feats['takeover'][0]
    trap_note = trap_failure(trap_pid, trap_why) if opts['trap'] else None     # the DLL checked its own sigs
    trap = opts['trap'] and not trap_note
    if not area and not takeover and not trap:
        return False
    off = [names[f]['name'] for f, wanted in (('skip_report', opts['skip']), ('area_bonus', opts['area']),
                                              ('takeover', opts['takeover']), ('trap_near', opts['trap']))
           if wanted and not (trap if f == 'trap_near' else feats[f][0])]
    game = types.SimpleNamespace(pid=pid, hp=hp)
    if area:
        apply_area_bonus(game, opts['area'])
    if takeover:
        start_takeover(game)
    show_notice(pid, '三国志14优化：游戏版本 %s 部分未适配，已停用%s，其余照常' % (ver, '、'.join(off)))
    note = ai_failure(ai_pid, ai_why) if opts['ai'] else None
    if note:
        show_notice(pid, note)
    if takeover:
        while P.find_pid() == pid:
            time.sleep(1)
        log('game exited; helper stops')
    return True


def main():
    opts, args = parse_options(sys.argv[1:])
    # start the game first, even if another helper is already waiting (it will patch it)
    running = P.find_pid()
    started = 'attach'
    ai_pid = ai_why = None
    if opts['ai'] and args[:1] != ['--notice-test']:
        if running:
            ai_why = 'the game was already running (the tree is only read at game start)'
        elif args:
            ai_pid, ai_why = start_with_ai(opts['ai'], args, opts['ai_ab'], opts['hr_dll'], opts['hr_block'],
                                           opts['trap'], opts['trap_log'])
        else:
            ai_why = 'no game to start (attach mode)'
    hr_pid = hr_why = None
    if (opts['hr_dll'] or opts['trap'] or opts['trap_log']) and not opts['ai'] and args[:1] != ['--notice-test']:
        if running:
            hr_why = 'the game was already running (our DLL is only loaded at game start)'
        elif args:
            hr_pid, hr_why = start_with_hr(args, opts['hr_dll'], opts['hr_block'], opts['trap'], opts['trap_log'])
        else:
            hr_why = 'no game to start (attach mode)'
    trap_pid, trap_why = (ai_pid, ai_why) if opts['ai'] else (hr_pid, hr_why)     # 陷阱近身触发 rides on either
    if ai_pid:
        started = 'started with ai modules %s, pid %d' % (','.join(opts['ai']), ai_pid)
        if opts['hr_dll']:
            started += ' and 人力资源管理器 %s (keep out: %s)' % (opts['hr_dll'], opts['hr_block'] or 'none')
    elif hr_pid:
        started = 'started with %s, pid %d' % (
            ' and '.join((['人力资源管理器 %s (keep out: %s)' % (opts['hr_dll'], opts['hr_block'] or 'none')]
                          if opts['hr_dll'] else []) + (['陷阱近身触发'] if opts['trap'] else []) +
                         (['trap log'] if opts['trap_log'] else [])), hr_pid)
    elif args == ['--steam']:
        started = 'game already running' if running else 'started the game through Steam'
        if not running:
            os.startfile(STEAM_URL)
    elif args and args[0] != '--notice-test':
        started = 'started: %s' % ' '.join(args)
        if not running:
            exe_dir = os.path.dirname(os.path.abspath(args[0]))
            subprocess.Popen(args, cwd=exe_dir if os.path.isdir(exe_dir) else None)
    mutex = kernel32.CreateMutexW(None, False, 'Local\\SAN14ReportSkipHelper')
    if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
        log('another helper is already running; leaving the game to it (%s)' % started)
        return
    # new run: keep only the previous run's logs (the DLL log stays if the DLL is still loaded)
    rotate_logs(include_dll=not (running and ingame.module_loaded(running)))
    log('helper %s started (%s); report skip %s, area bonus %s, takeover %s, ai %s, trap near %s' % (
        VERSION, started, 'on' if opts['skip'] else 'off', opts['area'] or 'unchanged',
        'on' if opts['takeover'] else 'off', ','.join(opts['ai']) or 'off',
        ('on' + (' (with log)' if opts['trap_log'] else '')) if opts['trap'] else 'off'))
    if args[:1] == ['--notice-test']:       # show the "unsupported version" notice on the running game
        if running:
            show_notice(running, '三国志14优化（测试）：游戏版本 %s 未适配，已停用，游戏照常运行' % exe_info(running)[1])
        return
    if not opts['skip'] and not opts['area'] and not opts['takeover']:
        # ai / 陷阱近身触发 only: say whether they took effect, then stop
        notes = ([ai_failure(ai_pid, ai_why)] if opts['ai'] else []) + \
                ([trap_failure(trap_pid, trap_why)] if opts['trap'] else [])
        notes = [n for n in notes if n]
        pid = ai_pid or hr_pid or running
        t0 = time.time()
        while notes and not pid and time.time() - t0 < 120:     # fallback start: wait for the game
            time.sleep(1)
            pid = P.find_pid()
        for note in notes:
            if pid:
                show_notice(pid, note)
        log('nothing more to do')
        return
    res = wait_and_patch(opts['skip'])
    if not res:
        return
    if res[0] == 'unsupported':
        _, pid, why = res
        ver = exe_info(pid)[1]
        log('unsupported game version %s, mod disabled: %s' % (ver, why))
        if not run_partial(pid, ver, opts, ai_pid, ai_why, trap_pid, trap_why):   # per feature: the rest keeps working
            show_notice(pid, '三国志14优化：游戏版本 %s 未适配，已停用，游戏照常运行' % ver)
        return
    game = res[1]
    if opts['area']:
        apply_area_bonus(game, opts['area'])
    if opts['ai']:
        note = ai_failure(ai_pid, ai_why)
        if note:
            show_notice(game.pid, note)
    if opts['trap']:
        note = trap_failure(trap_pid, trap_why)
        if note:
            show_notice(game.pid, note)
    if opts['takeover']:
        start_takeover(game)
    if not opts['skip']:
        if opts['takeover']:
            while P.find_pid() == game.pid:
                time.sleep(1)
            log('game exited; helper stops')
            return
        log('report skip off: nothing more to do; helper stops')
        return
    helper = Helper(game)
    overlay = Overlay(helper)
    sh = setup_ingame(game.pid, skip=True)
    if sh:
        overlay.attach_ingame(sh)
        log('drawing inside the game (dll hooked)')
        helper.use_dll_skip(sh)
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    log('game exited; helper stops')
    del mutex


if __name__ == '__main__':
    try:
        main()
    except Exception:
        log('fatal:\n' + traceback.format_exc())
