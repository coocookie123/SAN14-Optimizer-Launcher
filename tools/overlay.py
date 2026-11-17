"""Report button (test version): a small always-on-top window plus a global hotkey (Ctrl+Alt+R).

Pressing it during the player's strategy phase shows the original turn report again:
  1. temporarily remove the skip patch,
  2. set CUserStrategyState sub-phase back to 0 so the game runs its own report sequence,
  3. after the report window has closed (sub-phase back to 1/2), put the skip patch back.
Income/expense numbers are not wired up yet.

Run: pythonw -P tools/overlay.py   (or python -P to see log output)
"""
import ctypes
import ctypes.wintypes as wt
import os
import queue
import sys
import threading
import time
import traceback
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gamestate as G  # noqa: E402
import patch as P  # noqa: E402

u32 = ctypes.WinDLL('user32')
try:
    ctypes.WinDLL('shcore').SetProcessDpiAwareness(2)
except Exception:
    u32.SetProcessDPIAware()

GWL_EXSTYLE = -20
WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x8, 0x80, 0x08000000
MOD_ALT, MOD_CONTROL, WM_HOTKEY = 0x1, 0x2, 0x0312
u32.GetWindowLongPtrW.restype = ctypes.c_longlong
u32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_longlong]
u32.GetParent.restype = wt.HWND

events = queue.Queue()
busy = threading.Lock()


def log(msg):
    events.put(('status', msg))
    print(time.strftime('%H:%M:%S'), msg, flush=True)


def trigger():
    if not busy.acquire(blocking=False):
        log('正在处理上一次请求')
        return
    try:
        g = G.open_game()
        if not g:
            log('没找到游戏')
            return
        stack = g.stack()
        top_obj, top = stack[-1]
        if top != 'CUserStrategyState':
            log('现在不能弹出：请先关掉菜单（当前 %s）' % top)
            return
        if g.subphase(top_obj) not in (1, 2):
            log('现在不能弹出：不在下命令阶段')
            return
        ok, msg = P.restore(g.pid, g.hp)
        if not ok:
            log('撤补丁失败：%s' % msg)
            return
        g.set_subphase(top_obj, 0)
        log('正在弹出报告…')
        seen, t0 = False, time.time()
        while time.time() - t0 < 900:
            time.sleep(0.25)
            names = [n for _, n in g.stack()]
            if not names:
                break
            if 'CReportDisplayState' in names:
                seen = True
            elif seen and names[-1] == 'CUserStrategyState' and g.subphase(top_obj) in (1, 2):
                break
            elif not seen and time.time() - t0 > 20 and g.subphase(top_obj) in (1, 2):
                log('报告没有出现')
                break
        ok, msg = P.apply(g.pid, g.hp)
        log('报告已关闭，跳过补丁已恢复' if ok else '恢复补丁失败：%s' % msg)
    except Exception as e:
        log('出错：%s' % e)
        traceback.print_exc()
    finally:
        busy.release()


def hotkey_thread():
    if not u32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT, ord('R')):
        events.put(('status', '快捷键 Ctrl+Alt+R 注册失败'))
        return
    msg = wt.MSG()
    while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY:
            threading.Thread(target=trigger, daemon=True).start()


def main():
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes('-topmost', True)
    root.attributes('-alpha', 0.88)
    root.configure(bg='#1f1b16')
    w, h = 460, 150
    sw = root.winfo_screenwidth()
    root.geometry('%dx%d+%d+%d' % (w, h, sw - w - 40, 220))
    font = ('Microsoft YaHei UI', 12)
    tk.Label(root, text='报告按钮（测试版）', fg='#e8d9b5', bg='#1f1b16', font=('Microsoft YaHei UI', 12, 'bold')).pack(anchor='w', padx=12, pady=(8, 0))
    status = tk.Label(root, text='快捷键 Ctrl+Alt+R', fg='#cfc6b3', bg='#1f1b16', font=font, anchor='w', justify='left', wraplength=w - 24)
    status.pack(anchor='w', padx=12)
    btn = tk.Button(root, text='弹出报告', font=font, command=lambda: threading.Thread(target=trigger, daemon=True).start(),
                    bg='#5a4632', fg='white', activebackground='#7a5f42', relief='flat')
    btn.pack(anchor='w', padx=12, pady=6)

    root.update_idletasks()
    hwnd = u32.GetParent(root.winfo_id()) or root.winfo_id()
    ex = u32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    u32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)

    threading.Thread(target=hotkey_thread, daemon=True).start()

    def pump():
        try:
            while True:
                kind, val = events.get_nowait()
                if kind == 'status':
                    status.config(text=val)
        except queue.Empty:
            pass
        root.attributes('-topmost', True)
        root.after(200, pump)
    pump()
    root.mainloop()


if __name__ == '__main__':
    main()
