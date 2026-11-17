"""Skip the turn-start report screens (行动报告 / 收支报告) by patching the running game in memory.

How the game does it (SAN14CE, analysed 2026-11-10 on Steam build 25953386):
  CUserStrategyState sub-phase 0 runs the report coroutine (RVA 0x459d30): unless spectator mode,
  it pushes CReportWaitState (<=2 s) and then CReportDisplayState and waits for both.
  CReportDisplayState::Update (vtable slot 5, RVA 0x67fe80) switches on its sub-state [this+470h]:
    0 = 行动报告 page, 1 = 收支报告 page, 2 = start closing (close animation),
    3 = animation done -> pop itself; its std::function callback then resumes the coroutine,
        refreshes the map UI and sets CUserStrategyState sub-phase 1 (command input).
  The player's two confirmations normally walk 0 -> 1 -> 2 -> 3.

Patch v4 (20 bytes, current): in CReportDisplayState::Enter (vtable slot 1, RVA 0x67fca0) the
"sub-state = 4; open page 0" sequence is replaced by "sub-state = 3; anim-state = 3". The window's
root UI is still created but never shown, no page is built, no animation plays; on its first
Update the state takes the normal "animation done -> pop" path, so Exit (re-shows the HUD, frees
UI, null-checks the pages) and the std::function callback are the game's own code.

Patch v5 (current, = v4 + 5 bytes): the allocation of the window's root UI at the start of Enter
is replaced by a jmp past the root-UI setup, so not even the window background is created
(Exit null-checks the root, the destructor never touches it). User-confirmed 2026-11-10: no flash.

Earlier attempts, restored to original if found in memory:
  v1 jump over the CReportDisplayState push -> sub-phase never left 0, report coroutine looped.
  v2 v1 + run the callback body -> sub-phase 1 but the strategy UI never came back.
  v3 (1 byte) Update "sub-state 0" -> "sub-state 2" close handler: works, but the window flashes.

Nothing on disk is touched; restarting the game (or `restore`) brings back the original code.
Sites are found by the byte patterns in sigs/sigs.json; if anything does not match, nothing is written.

Usage: python -P tools/patch.py status|apply|restore|wait|waitnew [timeout_seconds]
  wait: wait for the game to start and its code to be decrypted, then apply (for a launcher).
  waitnew: same, but ignore a game process that is already running.
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sigs as S  # noqa: E402

k32 = ctypes.WinDLL('kernel32', use_last_error=True)
psapi = ctypes.WinDLL('psapi')

HERE = os.path.dirname(os.path.abspath(__file__))
BACKUP = os.path.join(os.path.dirname(HERE), 'logs', 'patch_original_bytes.json')
GAME_EXES = ['san14ce_sc.exe', 'san14ce.exe', 'san14ce_tc.exe', 'san14ce_ko.exe']

# Every site is found through sigs/sigs.json (tools/sigs.py); the patterns, offsets and original bytes
# live there, one variant per game version that needs its own pattern.
#   report_enter        CReportDisplayState::Enter: v4 site ("sub-state = 4; open page 0" -> "sub-state = 3;
#                       anim-state = 3", the 20 bytes are the manifest's "patched" value) and the v5 site
#                       ("mov ecx,188h", the allocation of the window's root UI, replaced by a jmp to the
#                       "call" at root_target, so the root UI is never created)
#   report_wait_update  CReportWaitState::Update "cmp eax, 2000" (ms to wait before the report window).
#                       Only shortened temporarily while the report button re-opens the report (see
#                       report_button.py); apply() always puts it back to 2000. Optional.
#   report_update       v3 site (old experiment, only restored). Optional.
#   report_coroutine    v1/v2 site in the report coroutine (old experiments, only restored). Optional.
WAIT_ORIG = 2000
# the 1.0.2.58226 bytes of the old v1/v2 site, to recognise those experiments of 2026-11-10
CO_REGION = 26
CO_ORIG_HEAD = bytes.fromhex('48 8d 44 24 28 48 89 84 24 98 00 00 00 48 8d 05')
CO_ORIG_TAIL = bytes.fromhex('48 89 44 24 28 48')

PROCESS_ACCESS = 0x0008 | 0x0010 | 0x0020 | 0x0400
THREAD_ACCESS = 0x0002 | 0x0008
PAGE_EXECUTE_READWRITE = 0x40

k32.OpenProcess.restype = wt.HANDLE
k32.OpenThread.restype = wt.HANDLE
k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.VirtualProtectEx.argtypes = [wt.HANDLE, ctypes.c_uint64, ctypes.c_size_t, wt.DWORD, ctypes.POINTER(wt.DWORD)]
k32.FlushInstructionCache.argtypes = [wt.HANDLE, ctypes.c_uint64, ctypes.c_size_t]
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.SuspendThread.argtypes = [wt.HANDLE]
k32.ResumeThread.argtypes = [wt.HANDLE]
k32.GetThreadContext.argtypes = [wt.HANDLE, ctypes.c_void_p]
psapi.EnumProcessModulesEx.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p), wt.DWORD, ctypes.POINTER(wt.DWORD), wt.DWORD]


class PE32W(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ProcessID', wt.DWORD),
                ('th32DefaultHeapID', ctypes.c_void_p), ('th32ModuleID', wt.DWORD), ('cntThreads', wt.DWORD),
                ('th32ParentProcessID', wt.DWORD), ('pcPriClassBase', ctypes.c_long), ('dwFlags', wt.DWORD),
                ('szExeFile', ctypes.c_wchar * 260)]


class TE32(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ThreadID', wt.DWORD),
                ('th32OwnerProcessID', wt.DWORD), ('tpBasePri', ctypes.c_long),
                ('tpDeltaPri', ctypes.c_long), ('dwFlags', wt.DWORD)]


def find_pid():
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    pe = PE32W()
    pe.dwSize = ctypes.sizeof(pe)
    ok = k32.Process32FirstW(snap, ctypes.byref(pe))
    pid = None
    while ok:
        if pe.szExeFile.lower() in GAME_EXES:
            pid = pe.th32ProcessID
            break
        ok = k32.Process32NextW(snap, ctypes.byref(pe))
    k32.CloseHandle(snap)
    return pid


def thread_ids(pid):
    snap = k32.CreateToolhelp32Snapshot(0x4, 0)
    te = TE32()
    te.dwSize = ctypes.sizeof(te)
    out = []
    ok = k32.Thread32First(snap, ctypes.byref(te))
    while ok:
        if te.th32OwnerProcessID == pid:
            out.append(te.th32ThreadID)
        ok = k32.Thread32Next(snap, ctypes.byref(te))
    k32.CloseHandle(snap)
    return out


def rpm(hp, addr, n):
    buf = ctypes.create_string_buffer(n)
    got = ctypes.c_size_t()
    if not k32.ReadProcessMemory(hp, addr, buf, n, ctypes.byref(got)):
        return None
    return buf.raw[:got.value]


def thread_rip(ht):
    raw = ctypes.create_string_buffer(1232 + 16)
    a = (ctypes.addressof(raw) + 15) & ~15
    ctypes.c_uint32.from_address(a + 0x30).value = 0x100001  # CONTEXT_CONTROL
    if not k32.GetThreadContext(ht, a):
        return None
    return ctypes.c_uint64.from_address(a + 0xF8).value


def write_code(pid, hp, addr, data):
    """Suspend every thread, make sure none is executing inside [addr, addr+len), write, resume."""
    handles = []
    try:
        for tid in thread_ids(pid):
            ht = k32.OpenThread(THREAD_ACCESS, False, tid)
            if ht and k32.SuspendThread(ht) != 0xFFFFFFFF:
                handles.append(ht)
            elif ht:
                k32.CloseHandle(ht)
        for ht in handles:
            rip = thread_rip(ht)
            if rip is not None and addr <= rip < addr + len(data):
                raise RuntimeError('a game thread is executing the patch region right now; try again')
        old = wt.DWORD()
        if not k32.VirtualProtectEx(hp, addr, len(data), PAGE_EXECUTE_READWRITE, ctypes.byref(old)):
            raise OSError('VirtualProtectEx failed %d' % ctypes.get_last_error())
        try:
            got = ctypes.c_size_t()
            if not k32.WriteProcessMemory(hp, addr, data, len(data), ctypes.byref(got)) or got.value != len(data):
                raise OSError('WriteProcessMemory failed %d' % ctypes.get_last_error())
        finally:
            tmp = wt.DWORD()
            k32.VirtualProtectEx(hp, addr, len(data), old.value, ctypes.byref(tmp))
        k32.FlushInstructionCache(hp, addr, len(data))
    finally:
        for ht in handles:
            k32.ResumeThread(ht)
            k32.CloseHandle(ht)


def main_module(hp):
    arr = (ctypes.c_void_p * 1)()
    need = wt.DWORD()
    psapi.EnumProcessModulesEx(hp, arr, ctypes.sizeof(arr), ctypes.byref(need), 3)
    return arr[0]


def text_section(hp, base):
    hdr = rpm(hp, base, 0x1000)
    pe = struct.unpack_from('<I', hdr, 0x3c)[0]
    nsec = struct.unpack_from('<H', hdr, pe + 6)[0]
    so = pe + 24 + struct.unpack_from('<H', hdr, pe + 20)[0]
    for i in range(nsec):
        name, vs, va = struct.unpack_from('<8sII', hdr, so + 40 * i)
        if name.rstrip(b'\0') == b'.text':
            return base + va, vs
    raise RuntimeError('.text not found')


compile_pattern = S.compile_pattern      # kept for other tools


def exe_key(hp, base):
    hdr = rpm(hp, base, 0x200)
    pe = struct.unpack_from('<I', hdr, 0x3c)[0]
    return '%08x-%x' % (struct.unpack_from('<I', hdr, pe + 8)[0], struct.unpack_from('<I', hdr, pe + 24 + 56)[0])


def load_backup():
    try:
        with open(BACKUP, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def find_one(rx, text, ta, what):
    hits = [m.start() for m in rx.finditer(text)]
    if len(hits) != 1:
        raise RuntimeError('%s pattern matched %d times (expected 1): game version changed or code not decrypted yet' % (what, len(hits)))
    return ta + hits[0]


def locate(hp):
    base = main_module(hp)
    ta, tn = text_section(hp, base)
    text = rpm(hp, ta, tn)
    if not text:
        raise RuntimeError('cannot read game code')
    info = {'key': exe_key(hp, base)}
    # v4 / v5 sites (required): the hit's checks make sure the site, the root-UI allocation before it and the
    # call it jumps to are the original bytes or our own patch
    h = S.find('report_enter', text, ta)
    e = h.at('site')
    ecur = rpm(hp, e, 20)
    info['ent'] = e
    info['ent_cur'] = ecur
    info['ent_orig_head'] = h.expect('site')
    info['ent_new'] = h.expect('site', 'patched')
    if h.state('site') == 'original':
        info['ent_orig'] = ecur
    else:
        bk = load_backup().get(info['key'] + ':enter')
        info['ent_orig'] = bytes.fromhex(bk) if bk else None
    r, target = h.at('root'), h.at('root_target')
    info['root'] = r
    info['root_cur'] = rpm(hp, r, 5)
    info['root_orig'] = h.expect('root')
    info['root_new'] = b'\xe9' + struct.pack('<i', target - (r + 5))
    # report-wait threshold. Optional: 「三国志14 优化伴侣」 (Frida) hooks the entry of this function, which
    # breaks the pattern while it runs; only the button's "fast open" in v5 mode needs it.
    try:
        w = S.find('report_wait_update', text, ta)
        info['wait'] = w.at('wait_ms')
        info['wait_cur'] = struct.unpack('<I', rpm(hp, info['wait'], 4))[0]
        info['wait_orig'] = w.expect('wait_ms')
    except RuntimeError:
        info['wait'] = info['wait_cur'] = None
        info['wait_orig'] = WAIT_ORIG
    # old v3 site (optional: only to undo that experiment)
    info['je0'] = None
    try:
        u = S.find('report_update', text, ta)
        je0, je2 = u.at('je0'), u.at('je2')
        b0, b2 = rpm(hp, je0, 6), rpm(hp, je2, 6)
        if b0[:2] == b'\x0f\x84' and b2[:2] == b'\x0f\x84':
            case2 = je2 + 6 + struct.unpack('<i', b2[2:])[0]
            info['je0'] = je0
            info['je0_cur'] = b0
            info['je0_new'] = b'\x0f\x84' + struct.pack('<i', case2 - (je0 + 6))
            orig_case0 = load_backup().get(info['key'] + ':je0')
            info['je0_orig'] = bytes.fromhex(orig_case0) if orig_case0 else (None if b0 == info['je0_new'] else b0)
    except RuntimeError:
        pass
    # old coroutine site (v1/v2; optional: only to undo those experiments)
    info['co'] = None
    info['co_state'] = 'absent'
    try:
        c = S.scan('report_coroutine', text, ta).at('site')
    except RuntimeError:
        return info
    cur = rpm(hp, c, CO_REGION)
    info['co'] = c
    info['co_cur'] = cur
    if cur[:16] == CO_ORIG_HEAD and cur[20:] == CO_ORIG_TAIL:
        info['co_state'] = 'original'
        info['co_orig'] = cur
    else:
        bk = load_backup().get(info['key'])
        info['co_orig'] = bytes.fromhex(bk) if bk else None
        if cur[:5] == bytes.fromhex('e9 4c 00 00 00') and cur[5:16] == CO_ORIG_HEAD[5:] and cur[20:] == CO_ORIG_TAIL:
            info['co_state'] = 'old-v1'
            info['co_orig'] = CO_ORIG_HEAD[:5] + cur[5:]
        else:
            info['co_state'] = 'old-v2' if cur[:3] == b'\x48\x8b\xcb' else 'unknown'
    return info


def save_backup(key, val):
    d = load_backup()
    d[key] = val.hex()
    os.makedirs(os.path.dirname(BACKUP), exist_ok=True)
    with open(BACKUP, 'w', encoding='utf-8') as f:
        json.dump(d, f, indent=1)


def v3_state(info):
    if info['je0'] is None:
        return 'absent'
    if info['je0_cur'] == info['je0_new']:
        return 'patched'
    if info['je0_orig'] and info['je0_cur'] == info['je0_orig']:
        return 'original'
    return 'unknown %s' % info['je0_cur'].hex(' ')


def v4_state(info):
    if info['ent_cur'] == info['ent_new']:
        return 'patched'
    if info['ent_cur'][:len(info['ent_orig_head'])] == info['ent_orig_head']:
        return 'original'
    return 'unknown %s' % info['ent_cur'].hex(' ')


def root_state(info):
    if info['root_cur'] == info['root_new']:
        return 'patched'
    if info['root_cur'] == info['root_orig']:
        return 'original'
    return 'unknown %s' % info['root_cur'].hex(' ')


def fix_old(pid, hp, info):
    """Put earlier experimental patches (v1/v2 coroutine, v3 update) back to original. Those sites are
    optional: when they are missing or look different (another game version) there is nothing to undo."""
    note = ''
    if info['co_state'] in ('old-v1', 'old-v2'):
        if not info['co_orig']:
            raise RuntimeError('old patch found but original bytes unknown; restart the game')
        write_code(pid, hp, info['co'], info['co_orig'])
        note += 'old %s patch restored; ' % info['co_state']
    elif info['co_state'] == 'unknown':
        note += 'report coroutine has other bytes (%s), left alone; ' % info['co_cur'].hex(' ')
    st3 = v3_state(info)
    if st3 == 'patched':
        if not info['je0_orig']:
            raise RuntimeError('v3 patch found but original bytes unknown; restart the game')
        write_code(pid, hp, info['je0'], info['je0_orig'])
        note += 'v3 patch restored; '
    elif st3 not in ('original', 'absent'):
        note += 'report update has other bytes (%s), left alone; ' % st3
    return note


def set_wait(pid, hp, ms, info=None):
    """Set the CReportWaitState threshold (ms). 0 = the wait ends on its next frame."""
    info = info or locate(hp)
    if info['wait'] is None:
        return False
    if info['wait_cur'] != ms:
        write_code(pid, hp, info['wait'], struct.pack('<I', ms))
    return struct.unpack('<I', rpm(hp, info['wait'], 4))[0] == ms


def apply(pid, hp):
    info = locate(hp)
    note = fix_old(pid, hp, info)
    if info['wait'] is not None and info['wait_cur'] != info['wait_orig']:
        set_wait(pid, hp, info['wait_orig'], info)
        note += 'report wait reset to %d ms; ' % info['wait_orig']
    st, rs = v4_state(info), root_state(info)
    if st == 'patched' and rs == 'patched':
        return True, note + 'already patched at %x / %x' % (info['ent'], info['root'])
    if st not in ('original', 'patched') or rs not in ('original', 'patched'):
        return False, note + 'not writing: enter %s, root %s' % (st, rs)
    if st == 'original':
        save_backup(info['key'] + ':enter', info['ent_orig'])
        write_code(pid, hp, info['ent'], info['ent_new'])
    if rs == 'original':
        write_code(pid, hp, info['root'], info['root_new'])
    ok = rpm(hp, info['ent'], 20) == info['ent_new'] and rpm(hp, info['root'], 5) == info['root_new']
    return ok, note + 'patched at %x / %x' % (info['ent'], info['root'])


def restore(pid, hp):
    info = locate(hp)
    note = fix_old(pid, hp, info)
    if root_state(info) == 'patched':
        write_code(pid, hp, info['root'], info['root_orig'])
        note += 'root-skip restored; '
    st = v4_state(info)
    if st == 'original':
        return True, note + 'already original at %x' % info['ent']
    if not info['ent_orig']:
        return False, note + 'original bytes unknown; restart the game instead'
    write_code(pid, hp, info['ent'], info['ent_orig'])
    return rpm(hp, info['ent'], 20) == info['ent_orig'], note + 'restored at %x' % info['ent']


def open_game(pid=None):
    pid = pid or find_pid()
    if not pid:
        return None, None
    return pid, k32.OpenProcess(PROCESS_ACCESS, False, pid)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if cmd in ('wait', 'waitnew'):
        timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 600
        skip = find_pid() if cmd == 'waitnew' else None
        t0, msg = time.time(), 'game not found'
        while time.time() - t0 < timeout:
            pid = find_pid()
            if not pid or pid == skip:
                time.sleep(2)
                continue
            pid, hp = open_game(pid)
            if hp:
                try:
                    ok, msg = apply(pid, hp)
                except Exception as e:  # still starting / still encrypted
                    ok, msg = False, str(e)
                k32.CloseHandle(hp)
                if ok:
                    print('pid %d: %s' % (pid, msg))
                    return 0
            time.sleep(2)
        print('timed out: %s' % msg)
        return 1
    pid, hp = open_game()
    if not hp:
        print('game process not found')
        return 1
    try:
        if cmd == 'status':
            info = locate(hp)
            print('pid %d: enter %s at %x; root-skip %s at %x; report wait %s ms; v3 site %s; report coroutine %s' % (
                pid, v4_state(info), info['ent'], root_state(info), info['root'], info['wait_cur'],
                v3_state(info), info['co_state']))
        elif cmd == 'apply':
            print('pid %d: ok=%s %s' % ((pid,) + apply(pid, hp)))
        elif cmd == 'restore':
            print('pid %d: ok=%s %s' % ((pid,) + restore(pid, hp)))
    except RuntimeError as e:
        print('pid %d: %s' % (pid, e))
        return 1
    finally:
        k32.CloseHandle(hp)
    return 0


if __name__ == '__main__':
    sys.exit(main())
