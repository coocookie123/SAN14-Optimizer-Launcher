"""Find code that stores a constant into word [reg+off]: python -P ai/research/find_store.py 0x14 0xc
Also catches `mov eXx, imm` followed within 6 instructions by `mov word ptr [r..+off], Xx`."""
import glob, json, os, re, struct, sys
import capstone
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
d = sorted(glob.glob(os.path.join(ROOT, 'analysis', 'dump_*')))[-1]
IMG = open(os.path.join(d, 'image.bin'), 'rb').read()
meta = json.load(open(os.path.join(d, 'meta.json'), encoding='utf-8'))
secs = {s['name']: (int(s['rva'], 16), int(s['vsize'], 16)) for s in meta['sections']}
p0, pn = secs['.pdata']
CS = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
off, val = int(sys.argv[1], 16), int(sys.argv[2], 16)
dst = re.compile(r'^word ptr \[r\w+ \+ %s\], (\w+)$' % hex(off))
seen = set()
for i in range(p0, p0 + pn, 12):
    b, e, u = struct.unpack_from('<III', IMG, i)
    if b == 0: break
    if (b, e) in seen or e - b > 0x8000: continue
    seen.add((b, e))
    regs = {}
    for ins in CS.disasm(IMG[b:e], b):
        m = re.match(r'^(e\w\w|r\d+d), (0x[0-9a-f]+|\d+)$', ins.op_str) if ins.mnemonic == 'mov' else None
        if m:
            regs[m.group(1)] = (int(m.group(2), 0), ins.address)
        if ins.mnemonic == 'mov':
            m2 = dst.match(ins.op_str)
            if m2:
                src = m2.group(1)
                v = None
                if src.startswith('0x') or src.isdigit():
                    v = int(src, 0)
                else:
                    full = {'ax': 'eax', 'bx': 'ebx', 'cx': 'ecx', 'dx': 'edx', 'si': 'esi', 'di': 'edi'}.get(src, src[:-1] + 'd' if src.endswith('w') else src)
                    if full in regs and ins.address - regs[full][1] < 0x40:
                        v = regs[full][0]
                if v == val:
                    print(f'{ins.address:x}  {ins.mnemonic} {ins.op_str}   (func {b:x})')
