"""Index: function RVA -> named classes (RTTI) whose virtual functions call it.
Writes evindex.json {func_hex: [class::slot, ...]} for all classes in the given namespaces."""
import json
import os
import re
import struct
import sys

import capstone

D = __import__('os').path.join(__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__)))), 'analysis', 'dump_1793841123')
IMG = open(os.path.join(D, 'image.bin'), 'rb').read()
META = json.load(open(os.path.join(D, 'meta.json'), encoding='utf-8'))
RB = int(META['runtime_base'], 16)
SECS = {s['name']: (int(s['rva'], 16), int(s['vsize'], 16)) for s in META['sections']}
T0, TN = SECS['.text']
R0, RN = SECS['.rdata']
CS = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
PLUMB = {0x113a3a0, 0xb2ae60, 0xb2acf0, 0xb2b380, 0xb2b590, 0x332410, 0x331820}

# all COLs: sig 1, off, cdoff, td, cd, self == col
cols = {}
for p in range(R0, R0 + RN - 24, 4):
    if IMG[p:p + 4] != b'\x01\0\0\0':
        continue
    sig, off, cdo, td, cd, me = struct.unpack_from('<IIIIII', IMG, p)
    if me != p or not (0 < td < len(IMG)):
        continue
    name = IMG[td + 16:td + 200].split(b'\0')[0]
    if name.startswith(b'.?A'):
        cols[p] = name.decode('ascii', 'replace')
print('cols', len(cols), file=sys.stderr)
want = re.compile(sys.argv[1] if len(sys.argv) > 1 else r'@S14Event@@$')
vt = {}
for p in range(R0, R0 + RN - 8, 8):
    q = struct.unpack_from('<Q', IMG, p)[0] - RB
    if q in cols and want.search(cols[q]):
        vt.setdefault(cols[q], []).append(p + 8)
print('vtables', sum(len(v) for v in vt.values()), file=sys.stderr)


FIELDS = {}


def fields(fn, short, n=300):
    for i in CS.disasm(IMG[fn:fn + n * 6], fn):
        if i.mnemonic == 'int3':
            break
        m = re.search(r'(byte|word|dword|qword) ptr \[r(?:[a-d]x|[sd]i|bx|8|9|1[0-5])(?:d)? \+ (0x[0-9a-f]+)\]', i.op_str)
        if m and i.mnemonic.startswith(('mov', 'cmp', 'add', 'sub', 'lea', 'call')):
            kind = 'v' if i.mnemonic == 'call' else m.group(1)[0]
            key = f'{kind}{int(m.group(2), 16):x}'
            FIELDS.setdefault(key, set()).add(short)


def calls(fn, depth=2, seen=None, n=400):
    seen = seen if seen is not None else set()
    if fn in seen or depth == 0:
        return set()
    seen.add(fn)
    out = set()
    for i in CS.disasm(IMG[fn:fn + n * 6], fn):
        if i.mnemonic == 'int3':
            break
        if i.mnemonic in ('call', 'jmp') and i.op_str.startswith('0x'):
            t = int(i.op_str, 16)
            if T0 <= t < T0 + TN and t not in PLUMB:
                if i.mnemonic == 'call' or not (fn <= t < fn + n * 6):
                    out.add(t)
                    if depth > 1:
                        out |= calls(t, depth - 1, seen, 60)
    return out


index = {}
for cname, vts in vt.items():
    short = re.sub(r'^\.\?A[UV]|@.*$', '', cname)
    for v in vts:
        for k in range(40):
            q = struct.unpack_from('<Q', IMG, v + 8 * k)[0] - RB
            if not T0 <= q < T0 + TN:
                break
            fields(q, short)
            for t in calls(q):
                index.setdefault(f'{t:x}', set()).add(short)
                fields(t, short, 80)
json.dump({k: sorted(v) for k, v in index.items()}, open('evindex.json', 'w'), indent=0)
print('funcs', len(index), file=sys.stderr)
json.dump({k: sorted(v) for k, v in FIELDS.items()}, open('evfields.json', 'w'), indent=0)
