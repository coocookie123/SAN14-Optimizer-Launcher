"""Snapshot call stacks of a process's threads without injecting anything.

Each sample: SuspendThread -> GetThreadContext -> StackWalk64 (dbghelp, unwinds with the
target's own .pdata) -> ResumeThread. No game code is modified.

Usage:
  python -P tools/stack.py <label> [--samples N] [--interval S] [--pid PID] [--all]
Writes analysis/stacks_<label>.json and prints frames that fall inside the main exe.
By default only threads whose start address is inside the main exe are sampled.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ANALYSIS  # noqa: E402

k32 = ctypes.WinDLL('kernel32', use_last_error=True)
ntdll = ctypes.WinDLL('ntdll')
psapi = ctypes.WinDLL('psapi')
dbghelp = ctypes.WinDLL(os.path.join(os.environ['SystemRoot'], 'System32', 'dbghelp.dll'))

PROCESS_ALL = 0x0400 | 0x0010 | 0x0008 | 0x00100000  # QUERY_INFORMATION | VM_READ | VM_OPERATION | SYNCHRONIZE
THREAD_ACCESS = 0x0002 | 0x0008 | 0x0040  # SUSPEND_RESUME | GET_CONTEXT | QUERY_INFORMATION
TH32CS_SNAPTHREAD = 0x4
CONTEXT_FULL = 0x10000B
MACHINE_AMD64 = 0x8664
CTX_SIZE = 1232


class THREADENTRY32(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ThreadID', wt.DWORD),
                ('th32OwnerProcessID', wt.DWORD), ('tpBasePri', ctypes.c_long),
                ('tpDeltaPri', ctypes.c_long), ('dwFlags', wt.DWORD)]


class ADDRESS64(ctypes.Structure):
    _fields_ = [('Offset', ctypes.c_uint64), ('Segment', ctypes.c_uint16), ('Mode', ctypes.c_int)]


class STACKFRAME64(ctypes.Structure):
    _fields_ = [('AddrPC', ADDRESS64), ('AddrReturn', ADDRESS64), ('AddrFrame', ADDRESS64),
                ('AddrStack', ADDRESS64), ('AddrBStore', ADDRESS64), ('FuncTableEntry', ctypes.c_void_p),
                ('Params', ctypes.c_uint64 * 4), ('Far', wt.BOOL), ('Virtual', wt.BOOL),
                ('Reserved', ctypes.c_uint64 * 3), ('KdHelp', ctypes.c_byte * 0x80)]


class MODULEINFO(ctypes.Structure):
    _fields_ = [('lpBaseOfDll', ctypes.c_void_p), ('SizeOfImage', wt.DWORD), ('EntryPoint', ctypes.c_void_p)]


dbghelp.SymInitializeW.argtypes = [wt.HANDLE, wt.LPCWSTR, wt.BOOL]
dbghelp.SymCleanup.argtypes = [wt.HANDLE]
dbghelp.SymFunctionTableAccess64.restype = ctypes.c_void_p
dbghelp.SymFunctionTableAccess64.argtypes = [wt.HANDLE, ctypes.c_uint64]
dbghelp.SymGetModuleBase64.restype = ctypes.c_uint64
dbghelp.SymGetModuleBase64.argtypes = [wt.HANDLE, ctypes.c_uint64]
FTA = ctypes.WINFUNCTYPE(ctypes.c_void_p, wt.HANDLE, ctypes.c_uint64)
GMB = ctypes.WINFUNCTYPE(ctypes.c_uint64, wt.HANDLE, ctypes.c_uint64)
dbghelp.StackWalk64.argtypes = [wt.DWORD, wt.HANDLE, wt.HANDLE, ctypes.POINTER(STACKFRAME64), ctypes.c_void_p,
                                ctypes.c_void_p, FTA, GMB, ctypes.c_void_p]
k32.OpenProcess.restype = wt.HANDLE
k32.OpenThread.restype = wt.HANDLE
k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
k32.GetThreadContext.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.SuspendThread.argtypes = [wt.HANDLE]
k32.ResumeThread.argtypes = [wt.HANDLE]
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
psapi.EnumProcessModulesEx.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p), wt.DWORD, ctypes.POINTER(wt.DWORD), wt.DWORD]
psapi.GetModuleBaseNameW.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.LPWSTR, wt.DWORD]
psapi.GetModuleInformation.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MODULEINFO), wt.DWORD]

_fta = FTA(lambda h, a: dbghelp.SymFunctionTableAccess64(h, a))
_gmb = GMB(lambda h, a: dbghelp.SymGetModuleBase64(h, a))


def modules(hp):
    arr = (ctypes.c_void_p * 1024)()
    need = wt.DWORD()
    psapi.EnumProcessModulesEx(hp, arr, ctypes.sizeof(arr), ctypes.byref(need), 3)
    out = []
    for i in range(need.value // ctypes.sizeof(ctypes.c_void_p)):
        name = ctypes.create_unicode_buffer(260)
        psapi.GetModuleBaseNameW(hp, arr[i], name, 260)
        mi = MODULEINFO()
        psapi.GetModuleInformation(hp, arr[i], ctypes.byref(mi), ctypes.sizeof(mi))
        out.append((name.value, mi.lpBaseOfDll or 0, mi.SizeOfImage))
    return out


def thread_ids(pid):
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    te = THREADENTRY32()
    te.dwSize = ctypes.sizeof(te)
    tids = []
    ok = k32.Thread32First(snap, ctypes.byref(te))
    while ok:
        if te.th32OwnerProcessID == pid:
            tids.append(te.th32ThreadID)
        ok = k32.Thread32Next(snap, ctypes.byref(te))
    k32.CloseHandle(snap)
    return tids


def thread_start(ht):
    start = ctypes.c_void_p()
    ntdll.NtQueryInformationThread(ht, 9, ctypes.byref(start), ctypes.sizeof(start), None)
    return start.value or 0


def walk(hp, ht, max_frames=64):
    raw = ctypes.create_string_buffer(CTX_SIZE + 16)
    addr = (ctypes.addressof(raw) + 15) & ~15
    ctypes.c_uint32.from_address(addr + 0x30).value = CONTEXT_FULL
    if k32.SuspendThread(ht) == 0xFFFFFFFF:
        return None
    try:
        if not k32.GetThreadContext(ht, addr):
            return None
        rip = ctypes.c_uint64.from_address(addr + 0xF8).value
        rsp = ctypes.c_uint64.from_address(addr + 0x98).value
        rbp = ctypes.c_uint64.from_address(addr + 0xA0).value
        sf = STACKFRAME64()
        sf.AddrPC.Offset, sf.AddrPC.Mode = rip, 3
        sf.AddrStack.Offset, sf.AddrStack.Mode = rsp, 3
        sf.AddrFrame.Offset, sf.AddrFrame.Mode = rbp, 3
        frames = []
        for _ in range(max_frames):
            if not dbghelp.StackWalk64(MACHINE_AMD64, hp, ht, ctypes.byref(sf), addr, None, _fta, _gmb, None):
                break
            if sf.AddrPC.Offset == 0:
                break
            frames.append(sf.AddrPC.Offset)
        return frames
    finally:
        k32.ResumeThread(ht)


def find_pid(names):
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)

    class PE32(ctypes.Structure):
        _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ProcessID', wt.DWORD),
                    ('th32DefaultHeapID', ctypes.c_void_p), ('th32ModuleID', wt.DWORD), ('cntThreads', wt.DWORD),
                    ('th32ParentProcessID', wt.DWORD), ('pcPriClassBase', ctypes.c_long), ('dwFlags', wt.DWORD),
                    ('szExeFile', ctypes.c_wchar * 260)]
    pe = PE32()
    pe.dwSize = ctypes.sizeof(pe)
    ok = k32.Process32FirstW(snap, ctypes.byref(pe))
    pid = None
    while ok:
        if pe.szExeFile.lower() in names:
            pid = pe.th32ProcessID
            break
        ok = k32.Process32NextW(snap, ctypes.byref(pe))
    k32.CloseHandle(snap)
    return pid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('label')
    ap.add_argument('--samples', type=int, default=10)
    ap.add_argument('--interval', type=float, default=0.2)
    ap.add_argument('--pid', type=int)
    ap.add_argument('--all', action='store_true', help='sample every thread, not only those started in the main exe')
    ap.add_argument('--until-flag', action='store_true',
                    help='keep sampling (up to --samples rounds) until analysis/stop.flag appears')
    ap.add_argument('--wait', type=float, default=0, help='seconds to wait for the game process to appear')
    ap.add_argument('--delay', type=float, default=0, help='seconds to wait after the game appears')
    a = ap.parse_args()
    flag = os.path.join(ANALYSIS, 'stop.flag')
    if os.path.exists(flag):
        os.remove(flag)
    from common import GAME_EXES
    pid = a.pid or find_pid(GAME_EXES)
    if not pid and a.wait:
        print('waiting for the game to start...', flush=True)
        t_wait = time.time()
        while not pid and time.time() - t_wait < a.wait:
            time.sleep(1)
            pid = find_pid(GAME_EXES)
        if pid and a.delay:
            time.sleep(a.delay)
        print('game pid', pid, flush=True)
    if not pid:
        sys.exit('game process not found')
    hp = k32.OpenProcess(PROCESS_ALL, False, pid)
    if not hp:
        sys.exit('OpenProcess failed %d' % ctypes.get_last_error())
    dbghelp.SymInitializeW(hp, None, True)
    mods = modules(hp)
    main_name, main_base, main_size = mods[0]

    def where(x):
        for n, b, s in mods:
            if b <= x < b + s:
                return n, x - b
        return None, x

    samples = {}
    t0 = time.time()
    for i in range(a.samples):
        if a.until_flag and os.path.exists(flag):
            os.remove(flag)
            break
        if k32.WaitForSingleObject(hp, 0) == 0:  # process exited
            print('process exited', flush=True)
            break
        ts = round(time.time() - t0, 2)
        for tid in thread_ids(pid):
            ht = k32.OpenThread(THREAD_ACCESS, False, tid)
            if not ht:
                continue
            try:
                st = thread_start(ht)
                if not a.all and not (main_base <= st < main_base + main_size):
                    continue
                fr = walk(hp, ht)
            finally:
                k32.CloseHandle(ht)
            if fr:
                samples.setdefault(str(tid), []).append((ts, fr))
        time.sleep(a.interval)
    dbghelp.SymCleanup(hp)
    k32.CloseHandle(hp)

    # summarise: per thread, frames inside the main exe as RVAs, with how many samples contain them
    out = {'label': a.label, 'pid': pid, 'main': main_name, 'main_base': hex(main_base),
           'samples': a.samples, 'threads': {}}
    for tid, lst in samples.items():
        stacks = []
        for ts, fr in lst:
            stacks.append({'t': ts, 'f': [('%s+0x%x' % where(x)) if where(x)[0] else hex(x) for x in fr]})
        if any(any(x.startswith(main_name) for x in s['f']) for s in stacks):
            out['threads'][tid] = stacks
    path = os.path.join(ANALYSIS, 'stacks_%s.json' % a.label)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f)
    print('saved', path, 'seconds', round(time.time() - t0, 1), flush=True)
    for tid, stacks in out['threads'].items():
        inner = [s for s in stacks if any(x.startswith(main_name) for x in s['f'])]
        print('tid %s: %d samples, %d touch %s, depth %d' % (tid, len(stacks), len(inner), main_name,
                                                              max(len(s['f']) for s in stacks)))


if __name__ == '__main__':
    main()
