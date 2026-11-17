"""Fast external memory search (ReadProcessMemory, nothing injected).

Usage:
  python -P tools/memscan.py text <utf16 text> [...]     # find UTF-16LE text
  python -P tools/memscan.py hex <hex bytes> [...]       # find raw bytes, e.g. "e8 03 00 00"
Prints address, region protection/type and a short UTF-16 preview.
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import GAME_EXES  # noqa: E402
from stack import find_pid  # noqa: E402

k32 = ctypes.WinDLL('kernel32', use_last_error=True)


class MBI(ctypes.Structure):
    _fields_ = [('BaseAddress', ctypes.c_uint64), ('AllocationBase', ctypes.c_uint64),
                ('AllocationProtect', wt.DWORD), ('PartitionId', wt.WORD), ('RegionSize', ctypes.c_uint64),
                ('State', wt.DWORD), ('Protect', wt.DWORD), ('Type', wt.DWORD)]


k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_uint64, ctypes.POINTER(MBI), ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.OpenProcess.restype = wt.HANDLE

MEM_COMMIT = 0x1000
READABLE = {0x02: 'r', 0x04: 'rw', 0x08: 'wc', 0x20: 'rx', 0x40: 'rwx', 0x80: 'rwxc'}
TYPES = {0x20000: 'private', 0x40000: 'mapped', 0x1000000: 'image'}


def regions(hp, writable_only=False):
    addr = 0
    mbi = MBI()
    while k32.VirtualQueryEx(hp, addr, ctypes.byref(mbi), ctypes.sizeof(mbi)):
        prot = mbi.Protect & 0xFF
        if mbi.State == MEM_COMMIT and prot in READABLE and not (mbi.Protect & 0x100):
            if not writable_only or prot in (0x04, 0x08, 0x40, 0x80):
                yield mbi.BaseAddress, mbi.RegionSize, READABLE[prot], TYPES.get(mbi.Type, hex(mbi.Type))
        addr = mbi.BaseAddress + mbi.RegionSize
        if addr >= 0x7FFFFFFFFFFF:
            break


def read(hp, addr, size):
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t()
    if not k32.ReadProcessMemory(hp, addr, buf, size, ctypes.byref(got)):
        return None
    return buf.raw[:got.value]


def open_game():
    pid = find_pid(GAME_EXES)
    if not pid:
        sys.exit('game process not found')
    hp = k32.OpenProcess(0x0400 | 0x0010, False, pid)
    return pid, hp


def search(hp, needles, chunk=64 << 20):
    found = {n: [] for n in needles}
    total = 0
    for base, size, prot, typ in regions(hp):
        for off in range(0, size, chunk):
            n = min(chunk + 256, size - off)
            data = read(hp, base + off, n)
            if not data:
                continue
            total += len(data)
            for nd in needles:
                i = data.find(nd)
                while i >= 0:
                    if i < chunk:  # avoid double counting the overlap
                        found[nd].append((base + off + i, prot, typ))
                    i = data.find(nd, i + 1)
    return found, total


def main():
    kind = sys.argv[1]
    if kind == 'text':
        needles = [t.encode('utf-16le') for t in sys.argv[2:]]
    else:
        needles = [bytes.fromhex(h) for h in sys.argv[2:]]
    pid, hp = open_game()
    found, total = search(hp, needles)
    print('scanned %.1f GB in pid %d' % (total / 1e9, pid))
    for nd, lst in found.items():
        label = nd.decode('utf-16le', 'replace') if kind == 'text' else nd.hex(' ')
        print('%s: %d hits' % (label, len(lst)))
        for a, prot, typ in lst[:30]:
            pre = read(hp, a - 48, 160) or b''
            s = pre.decode('utf-16le', 'replace').replace('\x00', '|').replace('\n', ' ')
            print('   0x%x %-4s %-8s %r' % (a, prot, typ, s[:70]))


if __name__ == '__main__':
    main()
