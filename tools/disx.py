"""Disassemble functions from the dumped image (analysis/dump_*/image.bin, laid out by RVA).

Function bounds come from the exe's own .pdata (chained unwind entries are followed back to the
primary function).

Usage:
  python -P tools/disx.py func <rva>            # whole function containing rva, call targets as RVAs
  python -P tools/disx.py around <rva> [n]      # n instructions before/after rva (default 30)
  python -P tools/disx.py callers <rva> [max]   # direct E8 calls to the function at rva
"""
import bisect
import glob
import json
import os
import struct
import sys

import capstone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load():
    dumps = sorted(glob.glob(os.path.join(ROOT, 'analysis', 'dump_*')))
    d = dumps[-1]
    img = open(os.path.join(d, 'image.bin'), 'rb').read()
    meta = json.load(open(os.path.join(d, 'meta.json'), encoding='utf-8'))
    secs = {s['name']: (int(s['rva'], 16), int(s['vsize'], 16)) for s in meta['sections']}
    return img, secs


IMG, SECS = load()
TEXT = SECS['.text']
PD_RVA, PD_SIZE = SECS['.pdata']
_pd = [struct.unpack_from('<III', IMG, PD_RVA + 12 * i) for i in range(PD_SIZE // 12)]
_pd = [e for e in _pd if e[0]]
_pd.sort()
_starts = [e[0] for e in _pd]
CS = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
CS.detail = False


def primary(entry):
    """Follow UNW_FLAG_CHAININFO back to the primary RUNTIME_FUNCTION."""
    seen = 0
    while seen < 32:
        begin, end, unw = entry
        unw &= ~1
        ver_flags = IMG[unw]
        flags = ver_flags >> 3
        if not flags & 0x4:
            return entry
        count = IMG[unw + 2]
        off = unw + 4 + 2 * ((count + 1) & ~1)
        entry = struct.unpack_from('<III', IMG, off)
        seen += 1
    return entry


def func_of(rva):
    i = bisect.bisect_right(_starts, rva) - 1
    if i < 0:
        return None
    b, e, u = _pd[i]
    if not b <= rva < e:
        return None
    pb, pe, _ = primary((b, e, u))
    # the function body may be split into several pdata pieces; gather the ones chaining to pb
    pieces = [(b2, e2) for (b2, e2, u2) in _pd if (b2, e2) == (pb, pe) or primary((b2, e2, u2))[0] == pb] \
        if (b, e) != (pb, pe) else [(pb, pe)]
    return pb, pe, pieces


def fmt(ins):
    s = '%s %s' % (ins.mnemonic, ins.op_str)
    return s


def dis_range(a, b, mark=None):
    for ins in CS.disasm(IMG[a:b], a):
        m = '>>' if mark is not None and ins.address <= mark < ins.address + ins.size else '  '
        print('%s %7x  %-24s %s' % (m, ins.address, IMG[ins.address:ins.address + ins.size].hex()[:24], fmt(ins)))


def main():
    cmd, rva = sys.argv[1], int(sys.argv[2], 16)
    if cmd == 'func':
        f = func_of(rva)
        if not f:
            sys.exit('no pdata entry for %x' % rva)
        pb, pe, pieces = f
        print('function %x..%x pieces %s' % (pb, pe, ' '.join('%x-%x' % p for p in pieces)))
        for a, b in sorted(pieces):
            print('--- piece %x..%x' % (a, b))
            dis_range(a, b, mark=rva - 1)
    elif cmd == 'around':
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 30
        start = max(TEXT[0], rva - n * 5)
        # resync: start disassembly at the containing function start when possible
        f = func_of(rva)
        if f and f[0] < rva and rva - f[0] < 4000:
            start = f[0]
        lines = list(CS.disasm(IMG[start:rva + n * 8], start))
        idx = next((i for i, x in enumerate(lines) if x.address <= rva < x.address + x.size or x.address >= rva), len(lines) - 1)
        for ins in lines[max(0, idx - n):idx + n]:
            m = '>>' if ins.address <= rva - 1 < ins.address + ins.size else '  '
            print('%s %7x  %s' % (m, ins.address, fmt(ins)))
    elif cmd == 'callers':
        mx = int(sys.argv[3]) if len(sys.argv) > 3 else 50
        a, n = TEXT
        hits = []
        i = IMG.find(b'\xe8', a, a + n)
        while 0 <= i < a + n:
            tgt = (i + 5 + struct.unpack_from('<i', IMG, i + 1)[0]) & 0xFFFFFFFF
            if tgt == rva:
                hits.append(i)
            i = IMG.find(b'\xe8', i + 1, a + n)
        print('%d direct calls to %x' % (len(hits), rva))
        for h in hits[:mx]:
            f = func_of(h)
            print('  call at %x  in func %s' % (h, ('%x' % f[0]) if f else '?'))


if __name__ == '__main__':
    main()
