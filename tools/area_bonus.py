"""府加成 (the base value of the 府 effect bonus) for the player and the computer, set in game memory.

How the game does it (SAN14CE, analysed 2026-11-11 on Steam build 25953386, function RVA 0x2b2b30):
  xmm7 = [player value]                                   (exe default 4)
  for a computer force: difficulty 超级 (3) -> [超级 value] (default 16)
                        difficulty 顶级 (4) -> [顶级 value] (default 24)
  (初级 / 中级 / 高级: the computer uses the player value too.)
The three values are static .data, read only by this function, and nothing writes them, so writing
them once changes the bonus until the game exits. We write data only, no code: 「三国志14 优化伴侣」
hashes this function (and the difficulty getter) and would refuse to start if they were patched.
So the computer value applies on 超级 / 顶级; on lower difficulties the computer gets the player value.
(The companion instead forces the difficulty to 顶级 for this one call, so its computer value applies
on every difficulty.)

Usage: python -P tools/area_bonus.py status | set <player> <computer> | restore
"""
import ctypes
import ctypes.wintypes as wt
import struct
import sys

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patch as P  # noqa: E402
import sigs as S  # noqa: E402

# movd xmm7,[player]; ... cmp edi,3; jne; movd xmm7,[超级]; jmp; cmp edi,4; jl; movd xmm7,[顶级]:
# sig "area_bonus" in sigs/sigs.json, one rel field per value
SLOTS = ('player', 'super', 'ultimate')
LIMIT = 100                                                          # same range as the companion's UI

P.psapi.GetModuleFileNameExW.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.LPWSTR, wt.DWORD]


def locate(hp):
    """{'player': addr, 'super': addr, 'ultimate': addr} in the running game; RuntimeError if not found."""
    base = P.main_module(hp)
    ta, tn = P.text_section(hp, base)
    text = P.rpm(hp, ta, tn)
    if not text:
        raise RuntimeError('cannot read game code')
    h = S.find('area_bonus', text, ta)
    out = {name: h.target(name) for name in SLOTS}
    out['base'] = base
    return out


def read(hp, addrs):
    return {k: struct.unpack('<i', P.rpm(hp, addrs[k], 4))[0] for k in SLOTS}


def originals(hp, addrs):
    """The values in the exe file on disk (what the game starts with)."""
    buf = ctypes.create_unicode_buffer(1024)
    P.psapi.GetModuleFileNameExW(hp, None, buf, 1024)
    data = open(buf.value, 'rb').read()
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    n = struct.unpack_from('<H', data, pe + 6)[0]
    so = pe + 24 + struct.unpack_from('<H', data, pe + 20)[0]
    out = {}
    for k in SLOTS:
        rva = addrs[k] - addrs['base']
        for i in range(n):
            _, vs, va, rs, ra = struct.unpack_from('<8sIIII', data, so + 40 * i)
            if va <= rva < va + min(vs, rs):
                out[k] = struct.unpack_from('<i', data, ra + rva - va)[0]
    if len(out) != len(SLOTS):
        raise RuntimeError('府加成 initial values not found in the exe file')
    return out


def write(hp, addrs, values):
    for k, v in values.items():
        got = ctypes.c_size_t()
        if not P.k32.WriteProcessMemory(hp, addrs[k], struct.pack('<i', int(v)), 4, ctypes.byref(got)) or got.value != 4:
            raise OSError('WriteProcessMemory failed %d' % ctypes.get_last_error())
    return read(hp, addrs) == {k: int(v) for k, v in values.items()}


def clamp(v):
    return max(0, min(LIMIT, int(v)))


def apply(hp, player, computer):
    """(ok, message). Player value for the player; computer value for computers on 超级 / 顶级."""
    addrs = locate(hp)
    before = read(hp, addrs)
    if any(not 0 <= v <= 1000 for v in before.values()):
        return False, 'unexpected values %s, not writing' % before
    want = {'player': clamp(player), 'super': clamp(computer), 'ultimate': clamp(computer)}
    ok = write(hp, addrs, want)
    return ok, '府加成 %s -> %s' % (before, read(hp, addrs))


def restore(hp):
    addrs = locate(hp)
    ok = write(hp, addrs, originals(hp, addrs))
    return ok, '府加成 restored to %s' % read(hp, addrs)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    pid, hp = P.open_game()
    if not hp:
        print('game process not found')
        return 1
    try:
        if cmd == 'status':
            addrs = locate(hp)
            print('pid %d: now %s, exe defaults %s' % (pid, read(hp, addrs), originals(hp, addrs)))
        elif cmd == 'set':
            print('pid %d: ok=%s %s' % ((pid,) + apply(hp, int(sys.argv[2]), int(sys.argv[3]))))
        elif cmd == 'restore':
            print('pid %d: ok=%s %s' % ((pid,) + restore(hp)))
    except (RuntimeError, OSError) as e:
        print('pid %d: %s' % (pid, e))
        return 1
    finally:
        P.k32.CloseHandle(hp)
    return 0


if __name__ == '__main__':
    sys.exit(main())
