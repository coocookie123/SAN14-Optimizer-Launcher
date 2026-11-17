"""Find vtables by MSVC RTTI class name in the dumped image (analysis/dump_*/image.bin).

  python -P ai/rtti.py SCompareForceCityCount@S14Event   # prints vtable RVA and its functions
"""
import glob
import json
import os
import re
import struct
import sys

import capstone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_d = sorted(glob.glob(os.path.join(ROOT, 'analysis', 'dump_*')))[-1]
IMG = open(os.path.join(_d, 'image.bin'), 'rb').read()
META = json.load(open(os.path.join(_d, 'meta.json'), encoding='utf-8'))
RB = int(META['runtime_base'], 16)
SECS = {s['name']: (int(s['rva'], 16), int(s['vsize'], 16)) for s in META['sections']}
CS = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)


def vtables(name):
    """All vtables (RVA, COL offset) of the class whose decorated name contains `name@@`."""
    out = []
    for kind in (b'.?AV', b'.?AU'):
        pat = kind + name.encode() + b'@@'
        for m in re.finditer(re.escape(pat) + b'\x00', IMG):
            td = m.start() - 0x10
            rd0, rdn = SECS['.rdata']
            # COL: sig 1, offset, cdOffset, pTypeDescriptor (RVA), pClassDescriptor, pSelf
            for c in re.finditer(re.escape(struct.pack('<I', td)), IMG[rd0:rd0 + rdn]):
                col = rd0 + c.start() - 12
                sig, off = struct.unpack_from('<II', IMG, col)
                if sig != 1:
                    continue
                for v in re.finditer(re.escape(struct.pack('<Q', RB + col)), IMG[rd0:rd0 + rdn]):
                    out.append((rd0 + v.start() + 8, off))
    return out


def slots(vt, n=40):
    t0, tn = SECS['.text']
    res = []
    for i in range(n):
        q = struct.unpack_from('<Q', IMG, vt + 8 * i)[0] - RB
        if not t0 <= q < t0 + tn:
            break
        res.append(q)
    return res


def brief(fn, k=4):
    ins = list(CS.disasm(IMG[fn:fn + 48], fn))[:k]
    return ' ; '.join(f'{i.mnemonic} {i.op_str}' for i in ins)


if __name__ == '__main__':
    for vt, off in vtables(sys.argv[1]):
        print(f'vtable {vt:#x} (offset {off})')
        for i, fn in enumerate(slots(vt)):
            print(f'  [{i}] {fn:x}: {brief(fn)}')
