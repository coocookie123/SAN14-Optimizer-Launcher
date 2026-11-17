"""Drive the game for automated AI tests: start it like the W launcher does, look at it, press keys and click,
read its state, switch auto mode (看海) on, stop it.

  python -P ai/gamectl.py start [--ai a,b,c] [--no-area]   # same command line as the launcher
  python -P ai/gamectl.py shot [out.png]                   # bring the game to front, save a screenshot
  python -P ai/gamectl.py click X Y                        # X, Y in a 1920x1080 frame of the client area
  python -P ai/gamectl.py key NAME [n]                     # e.g. key RETURN, key DOWN 3, key ESCAPE
  python -P ai/gamectl.py state                            # date, auto mode, state stack
  python -P ai/gamectl.py auto                             # switch auto mode on (player force 0, flag 0x100)
  python -P ai/gamectl.py kill

Input goes through SendInput, so the game window is brought to the front first and the user should not
touch mouse or keyboard meanwhile. Coordinates are scaled from 1920x1080 to the real client size.
"""
import ctypes
import ctypes.wintypes as wt
import os
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

DIST = os.path.join(ROOT, 'dist', '[W]三国志14优化启动器')
SHOTS = os.path.join(ROOT, 'analysis', 'shots')
WINDOW_CLASS = 'KT_SANGOKUSHI14_WINDOW'
AUTO_BIT, FLAGS_OFF, PLAYER_OFF = 0x100, 0x16a8, 0x3a    # status object (see tools/takeover.py)
REF_W, REF_H = 1920, 1080

u32 = ctypes.WinDLL('user32', use_last_error=True)
k32 = ctypes.WinDLL('kernel32', use_last_error=True)
u32.SetProcessDPIAware()
u32.FindWindowW.restype = wt.HWND
u32.GetForegroundWindow.restype = wt.HWND


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', wt.LONG), ('dy', wt.LONG), ('mouseData', wt.DWORD), ('dwFlags', wt.DWORD),
                ('time', wt.DWORD), ('dwExtraInfo', ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', wt.WORD), ('wScan', wt.WORD), ('dwFlags', wt.DWORD), ('time', wt.DWORD),
                ('dwExtraInfo', ctypes.c_size_t)]


class _U(ctypes.Union):
    _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT), ('pad', ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [('type', wt.DWORD), ('u', _U)]


VK = {'RETURN': 0x0D, 'ESCAPE': 0x1B, 'SPACE': 0x20, 'LEFT': 0x25, 'UP': 0x26, 'RIGHT': 0x27, 'DOWN': 0x28,
      'TAB': 0x09, 'BACK': 0x08, 'F1': 0x70, 'Z': 0x5A, 'X': 0x58, 'C': 0x43}
EXTENDED = {0x25, 0x26, 0x27, 0x28}


def window():
    return u32.FindWindowW(WINDOW_CLASS, None)


def client_rect(h):
    r = wt.RECT()
    u32.GetClientRect(h, ctypes.byref(r))
    pt = wt.POINT(0, 0)
    u32.ClientToScreen(h, ctypes.byref(pt))
    return pt.x, pt.y, r.right, r.bottom


def front(h=None, wait=1.0):
    """Restore the game window and bring it to the foreground (Alt trick against the focus lock)."""
    h = h or window()
    if not h:
        return False
    if u32.IsIconic(h):
        u32.ShowWindow(h, 9)            # SW_RESTORE
    _key(0x12, True)                    # Alt down/up lets SetForegroundWindow through
    _key(0x12, False)
    u32.SetForegroundWindow(h)
    time.sleep(wait)
    return u32.GetForegroundWindow() == h


def _send(inp):
    u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _key(vk, down):
    i = INPUT(type=1)
    i.u.ki = KEYBDINPUT(vk, u32.MapVirtualKeyW(vk, 0), (0 if down else 2) | (1 if vk in EXTENDED else 0), 0, 0)
    _send(i)


def key(name, n=1, gap=0.35):
    vk = VK.get(name.upper()) or ord(name.upper()[0])
    for _ in range(n):
        _key(vk, True)
        time.sleep(0.06)
        _key(vk, False)
        time.sleep(gap)


def click(x, y, h=None, right=False):
    """Click at (x, y) of a 1920x1080 frame of the client area."""
    h = h or window()
    cx, cy, cw, ch = client_rect(h)
    sx, sy = cx + round(x * cw / REF_W), cy + round(y * ch / REF_H)
    u32.SetCursorPos(sx, sy)
    time.sleep(0.12)
    down, up = (8, 16) if right else (2, 4)
    for f in (down, up):
        i = INPUT(type=0)
        i.u.mi = MOUSEINPUT(0, 0, 0, f, 0, 0)
        _send(i)
        time.sleep(0.08)
    return sx, sy


def shot(path=None, h=None, size=(1280, 720)):
    from PIL import ImageGrab
    h = h or window()
    cx, cy, cw, ch = client_rect(h)
    img = ImageGrab.grab((cx, cy, cx + cw, cy + ch), all_screens=True)
    if size:
        img = img.resize(size)
    os.makedirs(SHOTS, exist_ok=True)
    path = path or os.path.join(SHOTS, time.strftime('%H%M%S') + '.png')
    img.save(path)
    return path, img


def game():
    import gamedata as G
    return G.GameData()


def status(g):
    import gamedata as G
    return g.q(g.root + G.STATUS)


def date(g):
    st = status(g)
    if not st:
        return None
    y, m, d = struct.unpack_from('<HBB', g.read(st + 0x34, 4))
    return y, m, d


def states(g):
    """Class names on the game's state stack (gamestate.Game), top last."""
    import gamestate
    gs = gamestate.Game(g.pid, g.hp)
    return [n for _, n in gs.stack()]


def auto_on(g):
    st = status(g)
    flags = g.d(st + FLAGS_OFF)
    import patch as P
    P.wpm(g.hp, st + PLAYER_OFF, b'\0') if hasattr(P, 'wpm') else _wpm(g.hp, st + PLAYER_OFF, b'\0')
    _wpm(g.hp, st + FLAGS_OFF, struct.pack('<I', (flags | AUTO_BIT) & 0xFFFFFFFF))
    return g.d(st + FLAGS_OFF) & AUTO_BIT != 0


def _wpm(hp, addr, data):
    n = ctypes.c_size_t()
    k32.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t,
                                       ctypes.POINTER(ctypes.c_size_t)]
    return k32.WriteProcessMemory(hp, ctypes.c_void_p(addr), data, len(data), ctypes.byref(n))


def start(ai=('deploy', 'rally', 'engage', 'retreat'), area=(10, 10), skip=True, takeover=False, ab=None, dev=False,
          hr_dll=None, hr_block='', trap=False, trap_log=False):
    """Same command line as the launcher builds (launcher/launcher.cpp StartGame), through Steam.
    dev: run the helper from this checkout (tools/, ai/modules/, bin/; logs in logs/) instead of the
    release package, with the package's Python runtime. trap: 陷阱近身触发 (--trap-near); trap_log: log every
    trap that goes off in the DLL log (--trap-log, tests)."""
    py = os.path.join(DIST, 'runtime', 'pythonw.exe')
    app = ROOT if dev else os.path.join(DIST, 'app')
    script = os.path.join(app, 'tools', 'report_button.py')
    cmd = [py, '-P', script]
    if not skip:
        cmd.append('--no-skip')
    if area:
        cmd += ['--area-bonus', '%d,%d' % area]
    if takeover:
        cmd.append('--takeover')
    if trap:
        cmd.append('--trap-near')
    if trap_log:
        cmd.append('--trap-log')
    if hr_dll:
        cmd += ['--hr-dll', hr_dll] + (['--hr-block', hr_block] if hr_block else [])
    if ai:
        if ab:
            cmd += ['--ai-ab', ab]
        cmd += ['--ai', ','.join(ai)]
        cmd.append('--steam')
    else:
        # official tree: start the exe directly (no tree file, so nothing is redirected) and let the helper
        # attach; going through Steam would open the game's own launcher and wait for a click
        import launch
        if not hr_dll and not trap and not trap_log:   # with the HR DLL or the trap options the helper starts
                                                       # the game itself (--steam below)
            launch.start(launch.steam_game_dir(), None)
        else:
            cmd.append('--steam')
    subprocess.Popen(cmd, cwd=app, creationflags=0x08000000)
    return cmd


def kill():
    import patch as P
    pid = P.find_pid()
    if pid:
        hp = k32.OpenProcess(1, False, pid)      # PROCESS_TERMINATE
        k32.TerminateProcess(hp, 0)
        k32.CloseHandle(hp)
    return pid


def main():
    a = sys.argv[1:]
    cmd = a[0] if a else 'state'
    if cmd == 'start':
        ai = ('intercept_more', 'rally', 'engage', 'retreat')
        if '--ai' in a:
            v = a[a.index('--ai') + 1]
            ai = tuple(x for x in v.split(',') if x) if v != 'none' else ()
        print(start(ai, None if '--no-area' in a else (10, 10)))
    elif cmd == 'shot':
        print('front', front())
        p, _ = shot(a[1] if len(a) > 1 else None)
        print(p)
    elif cmd == 'click':
        front(wait=0.3)
        print(click(float(a[1]), float(a[2])))
    elif cmd == 'key':
        front(wait=0.3)
        key(a[1], int(a[2]) if len(a) > 2 else 1)
    elif cmd == 'state':
        g = game()
        st = status(g)
        print('date', date(g), 'auto', bool(g.d(st + FLAGS_OFF) & AUTO_BIT) if st else None,
              'player', g.b(st + PLAYER_OFF) if st else None)
        try:
            print('states', states(g))
        except Exception as e:
            print('states ?', e)
    elif cmd == 'auto':
        print('auto on', auto_on(game()))
    elif cmd == 'kill':
        print('killed', kill())


if __name__ == '__main__':
    main()
