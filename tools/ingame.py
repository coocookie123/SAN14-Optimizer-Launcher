"""In-game drawing: shared memory with bin/san14_overlay.dll and DLL injection.

Shared-memory layout must match overlay/san14_overlay.cpp (struct Shared).
CLI (development):
  python -P tools/ingame.py status
  python -P tools/ingame.py inject
  python -P tools/ingame.py unload     # before rebuilding the DLL while the game keeps running
"""
import ctypes
import ctypes.wintypes as wt
import mmap
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import patch as P  # noqa: E402

DLL = os.path.join(os.path.dirname(HERE), 'bin', 'san14_overlay.dll')
TAG = 'Local\\SAN14ReportSkipOverlay'
MAGIC = 0x52343153
HEADER = 128
TEX_W, TEX_H = 1024, 256
SIZE = HEADER + TEX_W * TEX_H * 4

# field offsets (u32 / i32)
F = {name: 4 * i for i, name in enumerate(
    'magic version py_heartbeat visible img_serial img_w img_h img_x img_y hit_x0 hit_y0 hit_x1 hit_y1 '
    'bb_w bb_h client_w client_h hover pressed clicks dll_frame cmd dll_state bb_format '
    'skip_req skip_state skip_count reopen_req reopen_ack foreign_hook'.split())}
SKIP_STATES = {0: 'not tried', 1: 'active', 2: 'removed', 3: 'game code not recognised'}
SIGNED = {'img_x', 'img_y', 'hit_x0', 'hit_y0', 'hit_x1', 'hit_y1'}

k32 = ctypes.WinDLL('kernel32', use_last_error=True)
psapi = ctypes.WinDLL('psapi')
k32.OpenProcess.restype = wt.HANDLE
k32.VirtualAllocEx.restype = ctypes.c_void_p
k32.VirtualAllocEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD]
k32.VirtualFreeEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD]
k32.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.GetModuleHandleW.restype = wt.HMODULE
k32.GetProcAddress.restype = ctypes.c_void_p
k32.GetProcAddress.argtypes = [wt.HMODULE, ctypes.c_char_p]
k32.CreateRemoteThread.restype = wt.HANDLE
k32.CreateRemoteThread.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p]
k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
k32.GetExitCodeThread.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
k32.CloseHandle.argtypes = [wt.HANDLE]
psapi.EnumProcessModulesEx.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p), wt.DWORD, ctypes.POINTER(wt.DWORD), wt.DWORD]
psapi.GetModuleBaseNameW.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.LPWSTR, wt.DWORD]


class Shared:
    def __init__(self):
        self.mm = mmap.mmap(-1, SIZE, tagname=TAG)
        if self.get('magic') != MAGIC:
            self.mm[:HEADER] = bytes(HEADER)
            self.set('version', 1)
            self.set('magic', MAGIC)
        self.serial = self.get('img_serial')

    def get(self, name):
        return struct.unpack_from('<i' if name in SIGNED else '<I', self.mm, F[name])[0]

    def set(self, name, value):
        if name in SIGNED:
            struct.pack_into('<i', self.mm, F[name], int(value))
        else:
            struct.pack_into('<I', self.mm, F[name], int(value) & 0xFFFFFFFF)

    def heartbeat(self):
        self.set('py_heartbeat', k32.GetTickCount() & 0xFFFFFFFF)

    def put_image(self, bgra, w, h):
        if w > TEX_W or h > TEX_H:
            raise ValueError('image too large: %dx%d' % (w, h))
        self.mm[HEADER:HEADER + len(bgra)] = bgra
        self.set('img_w', w)
        self.set('img_h', h)
        self.serial = (self.serial + 1) & 0xFFFFFFFF
        self.set('img_serial', self.serial)


def module_loaded(pid, name='san14_overlay.dll'):
    hp = k32.OpenProcess(0x0400 | 0x0010, False, pid)
    if not hp:
        return False
    try:
        arr = (ctypes.c_void_p * 1024)()
        need = wt.DWORD()
        psapi.EnumProcessModulesEx(hp, arr, ctypes.sizeof(arr), ctypes.byref(need), 3)
        for i in range(need.value // ctypes.sizeof(ctypes.c_void_p)):
            buf = ctypes.create_unicode_buffer(260)
            psapi.GetModuleBaseNameW(hp, arr[i], buf, 260)
            if buf.value.lower() == name:
                return True
        return False
    finally:
        k32.CloseHandle(hp)


def inject(pid, dll=DLL):
    """LoadLibraryW(dll) inside the game. Returns True if the DLL is (now) loaded."""
    if module_loaded(pid):
        return True
    access = 0x0002 | 0x0008 | 0x0010 | 0x0020 | 0x0400  # CREATE_THREAD|VM_OPERATION|VM_READ|VM_WRITE|QUERY_INFO
    hp = k32.OpenProcess(access, False, pid)
    if not hp:
        return False
    try:
        data = (os.path.abspath(dll) + '\0').encode('utf-16le')
        mem = k32.VirtualAllocEx(hp, None, len(data), 0x3000, 0x04)
        if not mem:
            return False
        got = ctypes.c_size_t()
        k32.WriteProcessMemory(hp, mem, data, len(data), ctypes.byref(got))
        load = k32.GetProcAddress(k32.GetModuleHandleW('kernel32.dll'), b'LoadLibraryW')
        th = k32.CreateRemoteThread(hp, None, 0, load, mem, 0, None)
        if not th:
            k32.VirtualFreeEx(hp, mem, 0, 0x8000)
            return False
        k32.WaitForSingleObject(th, 10000)
        code = wt.DWORD()
        k32.GetExitCodeThread(th, ctypes.byref(code))
        k32.CloseHandle(th)
        k32.VirtualFreeEx(hp, mem, 0, 0x8000)
        return code.value != 0
    finally:
        k32.CloseHandle(hp)


def unload(sh, pid, timeout=8):
    sh.heartbeat()
    sh.set('cmd', 1)
    t0 = time.time()
    while time.time() - t0 < timeout:
        sh.heartbeat()
        if not module_loaded(pid):
            sh.set('cmd', 0)
            return True
        time.sleep(0.2)
    return False


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    pid = P.find_pid()
    if not pid:
        sys.exit('game not running')
    sh = Shared()
    if cmd == 'inject':
        sh.set('cmd', 0)
        print('inject:', inject(pid))
        time.sleep(1.5)
    elif cmd == 'unload':
        print('unload:', unload(sh, pid))
    print('loaded:', module_loaded(pid), {k: sh.get(k) for k in
          ('dll_state', 'dll_frame', 'bb_w', 'bb_h', 'bb_format', 'client_w', 'client_h', 'hover', 'pressed', 'clicks',
           'skip_req', 'skip_state', 'skip_count', 'reopen_req', 'reopen_ack', 'foreign_hook')})


if __name__ == '__main__':
    main()
