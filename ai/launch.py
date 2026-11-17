"""Start the game with a modified AI behaviour tree. Nothing is written to the game directory.

  python -P ai/launch.py ai/modules/experiments/test_day2.json   # build the tree from these modules and start the game with it
  python -P ai/launch.py --bt E:/some/bt.bin             # use a tree file you built yourself
  python -P ai/launch.py --original                      # same way, official tree (baseline for comparisons)
  python -P ai/launch.py --game-dir D:/SAN14 ...           # game not in the default Steam folder
  python -P ai/launch.py --original --trap-near --trap-log  # also 陷阱近身触发 and its log of every trap that goes off

How: builds the tree with btpatch.py into ai/build/, starts SAN14CE_SC.exe suspended (Steam must be
running; SteamAppId is set so the game does not restart itself through Steam), queues
LoadLibraryW(bin/san14_ai.dll) as an APC on its main thread, then resumes it. The DLL loads before any
game code runs and redirects the game's CreateFileW of 0000/bt_exce.bin to the patched file (path passed
in the environment variable SAN14_AI_BT). The game reads the tree once at start, so a patch only takes
effect for a game started this way. The official launcher is skipped; its settings are kept in the
registry, so the game uses whatever was set there last.

The DLL logs to analysis/ai_dll.log (previous run: ai_dll.prev.log).
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import btpatch  # noqa: E402
import patch as P  # noqa: E402

DLL = os.path.join(ROOT, 'bin', 'san14_ai.dll')
# 人力资源管理器 (the user's own copy, loaded after ours): its files under <game dir>\SAN14HR go here instead
HR_DIR = os.path.join(ROOT, 'logs', 'san14HR')
LOG = os.path.join(ROOT, 'analysis', 'ai_dll.log')
DEFAULT_DIR = os.path.dirname(os.path.dirname(btpatch.btdump.GAME_BT))   # ...\Romance_of_the_Three_Kingdoms_14
EXES = ('SAN14CE_SC.exe', 'SAN14CE_TC.exe', 'SAN14CE.exe', 'SAN14CE_KO.exe', 'SAN14CE_EN.exe')
APPID = '872410'


class LaunchError(Exception):
    pass


def steam_game_dir():
    """The game's folder in any Steam library (registry SteamPath + libraryfolders.vdf), else DEFAULT_DIR."""
    import re
    import winreg
    roots = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as k:
            roots.append(winreg.QueryValueEx(k, 'SteamPath')[0])
    except OSError:
        pass
    for r in list(roots):
        vdf = os.path.join(r, 'steamapps', 'libraryfolders.vdf')
        if os.path.exists(vdf):
            roots += [m.replace('\\\\', '\\') for m in re.findall(r'"path"\s+"([^"]+)"', open(vdf, encoding='utf-8', errors='replace').read())]
    for r in roots + [os.path.dirname(os.path.dirname(os.path.dirname(DEFAULT_DIR)))]:
        d = os.path.join(r, 'steamapps', 'common', 'Romance_of_the_Three_Kingdoms_14')
        if any(os.path.exists(os.path.join(d, e)) for e in EXES):
            return os.path.normpath(d)
    return DEFAULT_DIR


def game_exe(game_dir):
    for e in EXES:
        p = os.path.join(game_dir, e)
        if os.path.isfile(p):
            return p
    raise LaunchError(f'{game_dir} 里找不到传承版的游戏程序（SAN14CE_SC.exe 等）')


k32 =ctypes.WinDLL('kernel32', use_last_error=True)


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [('cb', wt.DWORD), ('lpReserved', wt.LPWSTR), ('lpDesktop', wt.LPWSTR), ('lpTitle', wt.LPWSTR),
                ('dwX', wt.DWORD), ('dwY', wt.DWORD), ('dwXSize', wt.DWORD), ('dwYSize', wt.DWORD),
                ('dwXCountChars', wt.DWORD), ('dwYCountChars', wt.DWORD), ('dwFillAttribute', wt.DWORD),
                ('dwFlags', wt.DWORD), ('wShowWindow', wt.WORD), ('cbReserved2', wt.WORD),
                ('lpReserved2', ctypes.c_void_p), ('hStdInput', wt.HANDLE), ('hStdOutput', wt.HANDLE),
                ('hStdError', wt.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [('hProcess', wt.HANDLE), ('hThread', wt.HANDLE), ('dwProcessId', wt.DWORD),
                ('dwThreadId', wt.DWORD)]


k32.CreateProcessW.argtypes = [wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL, wt.DWORD,
                               ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
                               ctypes.POINTER(PROCESS_INFORMATION)]
k32.VirtualAllocEx.restype = ctypes.c_void_p
k32.VirtualAllocEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD]
k32.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_size_t)]
k32.QueueUserAPC.argtypes = [ctypes.c_void_p, wt.HANDLE, ctypes.c_void_p]
k32.GetModuleHandleW.restype = wt.HMODULE
k32.GetProcAddress.restype = ctypes.c_void_p
k32.GetProcAddress.argtypes = [wt.HMODULE, ctypes.c_char_p]
k32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]


def process_running(name):
    import subprocess
    out = subprocess.run(['tasklist', '/FI', f'IMAGENAME eq {name}', '/NH'], capture_output=True, text=True,
                         creationflags=0x08000000).stdout
    return name.lower() in out.lower()


def start(game_dir, bt_path, exe=None, features=(), ab=None, extra_dlls=(), hr_block='', trap_near=False,
          trap_log=False):
    """Start the game (exe, default: the CE exe in game_dir) suspended with san14_ai.dll loaded first.
    features: DLL features to switch on (btpatch.dll_features), ab: 'odd'/'even' = only those force ids
    ('odd:defend+siege': only those DLL features, see aihook/san14_ai.cpp).
    trap_near: 陷阱近身触发 (a trap goes off only with an enemy within 1 hex); trap_log: log every trap that goes off.
    Raises LaunchError. Returns the pid."""
    exe = exe or game_exe(game_dir)
    if not os.path.isfile(exe):
        raise LaunchError(f'找不到 {exe}')
    if P.find_pid():
        raise LaunchError('游戏已经开着，先关掉再用这个方式启动（树只在游戏启动时读一次）')
    if not process_running('steam.exe'):
        raise LaunchError('Steam 没在运行，先打开 Steam')
    if not os.path.isfile(DLL):
        raise LaunchError(f'缺少 {DLL}（python -P aihook/build.py）')
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    if os.path.exists(LOG):
        os.replace(LOG, LOG.replace('.log', '.prev.log'))
    env = dict(os.environ)
    for k in ('SAN14_AI_BT', 'SAN14_AI_FEATURES', 'SAN14_AI_AB', 'SAN14_HR_REDIRECT', 'SAN14_HR_DLL', 'SAN14_HR_BLOCK',
              'SAN14_TRAP_NEAR', 'SAN14_TRAP_LOG'):
        env.pop(k, None)
    extra_dlls = [os.path.abspath(d) for d in extra_dlls if d]
    for d in extra_dlls:
        if not os.path.isfile(d):
            raise LaunchError(f'找不到 {d}')
    if extra_dlls:                       # the third-party DLL writes under <game dir>\SAN14HR: send that to HR_DIR
        os.makedirs(HR_DIR, exist_ok=True)
        env['SAN14_HR_REDIRECT'] = HR_DIR
        env['SAN14_HR_DLL'] = extra_dlls[0]       # it is the dinput8 proxy: san14_ai.dll routes the game's
                                                  # DirectInput8Create through it, which is what starts it
        if hr_block:                     # its features to keep out, e.g. 'commandbook' (see aihook HrOccupy)
            env['SAN14_HR_BLOCK'] = hr_block
    env.update({'SAN14_AI_LOG': LOG, 'SteamAppId': APPID, 'SteamGameId': APPID})
    if bt_path:                          # no tree file: the DLL only logs, the game reads the official tree
        env['SAN14_AI_BT'] = os.path.abspath(bt_path)
    if features:                         # DLL-side AI (see aihook/san14_ai.cpp)
        env['SAN14_AI_FEATURES'] = ','.join(features)
    if ab:
        env['SAN14_AI_AB'] = ab
    if trap_near:                        # 陷阱近身触发 (aihook InstallTrapNear)
        env['SAN14_TRAP_NEAR'] = '1'
    if trap_log:
        env['SAN14_TRAP_LOG'] = '1'
    block = ''.join(f'{k}={v}\0' for k, v in sorted(env.items(), key=lambda kv: kv[0].upper())) + '\0'
    envbuf = ctypes.create_unicode_buffer(block, len(block))
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    pi = PROCESS_INFORMATION()
    cmd = ctypes.create_unicode_buffer(f'"{exe}"')
    CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT = 0x4, 0x400
    if not k32.CreateProcessW(exe, cmd, None, None, False, CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT,
                              envbuf, game_dir, ctypes.byref(si), ctypes.byref(pi)):
        raise LaunchError(f'启动失败 (error {ctypes.get_last_error()})')
    try:
        load = k32.GetProcAddress(k32.GetModuleHandleW('kernel32.dll'), b'LoadLibraryW')
        for dll in [os.path.abspath(DLL)] + extra_dlls:      # APCs run in order: ours (and its hooks) first
            path = (dll + '\0').encode('utf-16le')
            mem = k32.VirtualAllocEx(pi.hProcess, None, len(path), 0x3000, 0x04)
            if not mem or not k32.WriteProcessMemory(pi.hProcess, mem, path, len(path), None):
                raise OSError(f'write DLL path failed ({ctypes.get_last_error()})')
            if not k32.QueueUserAPC(load, pi.hThread, mem):
                raise OSError(f'QueueUserAPC failed ({ctypes.get_last_error()})')
    except OSError as e:
        k32.TerminateProcess(pi.hProcess, 1)
        raise LaunchError(f'注入失败，已结束游戏进程：{e}')
    k32.ResumeThread(pi.hThread)
    k32.CloseHandle(pi.hThread)
    k32.CloseHandle(pi.hProcess)
    return pi.dwProcessId


def wait_log(timeout=60):
    """Wait for the DLL to report the redirect. Returns the log text."""
    t0 = time.time()
    text = ''
    while time.time() - t0 < timeout:
        if os.path.exists(LOG):
            with open(LOG, encoding='utf-8-sig', errors='replace') as f:
                text = f.read()
            if 'redirect #' in text:
                break
        time.sleep(0.5)
    return text


def wait_trap(timeout=60):
    """Wait for the DLL to report 陷阱近身触发 (installed when the game reads the tree). (ok, log line or reason)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.exists(LOG):
            with open(LOG, encoding='utf-8-sig', errors='replace') as f:
                for line in f:
                    if 'trapnear installed' in line or 'trapnear NOT installed' in line:
                        return 'trapnear installed' in line, line.strip()
        time.sleep(0.5)
    return False, 'no trapnear line in %s after %d s' % (LOG, timeout)


def main():
    args = sys.argv[1:]
    game_dir = steam_game_dir()
    bt = None
    trap = {k: k in args for k in ('--trap-near', '--trap-log')}   # 陷阱近身触发 / its test log
    args = [a for a in args if a not in trap]
    trap = {'trap_near': trap['--trap-near'], 'trap_log': trap['--trap-log']}
    if '--game-dir' in args:
        i = args.index('--game-dir')
        game_dir = args[i + 1]
        del args[i:i + 2]
    if '--bt' in args:
        i = args.index('--bt')
        bt = args[i + 1]
        del args[i:i + 2]
    if '--original' in args:             # official tree, same launch path (for a baseline)
        try:
            pid = start(game_dir, None, **trap)
        except LaunchError as e:
            sys.exit(str(e))
        print(f'游戏已启动（pid {pid}），用的是原版行为树')
        if trap['trap_near'] or trap['trap_log']:
            print('陷阱近身触发：%s' % wait_trap()[1])
        return
    if not bt:
        if not args:
            sys.exit(__doc__)
        bt, problems = btpatch.build(args, source=os.path.join(game_dir, '0000', 'bt_exce.bin'))
        if problems:
            print('补丁对不上当前游戏版本，没有启动：')
            for p in problems:
                print('  ' + p)
            sys.exit(1)
        print(f'改版行为树：{bt}')
    try:
        pid = start(game_dir, bt, features=btpatch.dll_features(args), **trap)
    except LaunchError as e:
        sys.exit(str(e))
    print(f'游戏已启动（pid {pid}），等待游戏读取行为树……')
    text = wait_log()
    ok = [l for l in text.splitlines() if 'redirect #' in l]
    if ok and ok[-1].split(' : ')[-1].startswith('ok'):
        print('成功：游戏读的是改版行为树')
    elif 'san14_ai.dll loaded' in text:
        print('DLL 已加载，但 60 秒内没看到游戏读行为树，详见 ' + LOG)
    else:
        print('没看到 DLL 的日志，注入可能失败，详见 ' + LOG)
    for l in text.splitlines()[:6]:
        print('  ' + l)


if __name__ == '__main__':
    main()
