"""List the game's threads: id, CPU time, start address (as module+RVA), and which thread owns the main window.

Usage: python -P tools/threads.py
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import attach, find_game  # noqa: E402

k32 = ctypes.WinDLL('kernel32', use_last_error=True)
ntdll = ctypes.WinDLL('ntdll')
u32 = ctypes.WinDLL('user32')
THREAD_QUERY_INFORMATION = 0x0040

JS = r"""
rpc.exports = {
  threads() { return Process.enumerateThreads().map(t => ({id: t.id, state: t.state, name: t.name || null})); },
  modules() { return Process.enumerateModules().map(m => ({name: m.name, base: m.base.toString(), size: m.size})); },
};
"""


def thread_info(tid):
    h = k32.OpenThread(THREAD_QUERY_INFORMATION, False, tid)
    if not h:
        return None, None
    try:
        c, e, kt, ut = (wt.FILETIME() for _ in range(4))
        cpu = None
        if k32.GetThreadTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
            def ft(x):
                return (x.dwHighDateTime << 32 | x.dwLowDateTime) / 1e7
            cpu = ft(kt) + ft(ut)
        start = ctypes.c_void_p()
        # ThreadQuerySetWin32StartAddress = 9
        ntdll.NtQueryInformationThread(h, 9, ctypes.byref(start), ctypes.sizeof(start), None)
        return cpu, start.value
    finally:
        k32.CloseHandle(h)


def window_threads(pid):
    res = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        p = wt.DWORD()
        tid = u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and u32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            u32.GetWindowTextW(hwnd, buf, 256)
            res.append((tid, hwnd, buf.value))
        return True
    u32.EnumWindows(WNDENUMPROC(cb), 0)
    return res


def main():
    p = find_game()
    if not p:
        sys.exit('game process not found')
    sess = attach(p.pid)
    sc = sess.create_script(JS)
    sc.load()
    mods = [(m['name'], int(m['base'], 16), m['size']) for m in sc.exports_sync.modules()]
    ths = sc.exports_sync.threads()
    sess.detach()

    def where(a):
        if not a:
            return '-'
        for n, b, s in mods:
            if b <= a < b + s:
                return '%s+0x%x' % (n, a - b)
        return hex(a)

    wins = window_threads(p.pid)
    print('pid', p.pid, p.name)
    for tid, hwnd, title in wins:
        print('window thread', tid, hex(hwnd), repr(title))
    rows = []
    for t in ths:
        cpu, start = thread_info(t['id'])
        rows.append((cpu or 0, t['id'], t['state'], t['name'], where(start)))
    rows.sort(reverse=True)
    for cpu, tid, state, name, start in rows:
        print('%7.1fs  tid=%-6d %-8s %-24s %s' % (cpu, tid, state, name or '', start))


if __name__ == '__main__':
    main()
