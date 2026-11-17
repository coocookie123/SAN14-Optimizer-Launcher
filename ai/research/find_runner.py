"""Find functions that touch a given byte offset and also call into the BT library (0xb29000-0xb36000).
python -P ai/research/find_runner.py 0x2a"""
import bisect, glob, json, os, re, struct, sys
import capstone
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
d = sorted(glob.glob(os.path.join(ROOT, 'analysis', 'dump_*')))[-1]
IMG = open(os.path.join(d, 'image.bin'), 'rb').read()
meta = json.load(open(os.path.join(d, 'meta.json'), encoding='utf-8'))
secs = {s['name']: (int(s['rva'], 16), int(s['vsize'], 16)) for s in meta['sections']}
p0, pn = secs['.pdata']
funcs = []
for i in range(p0, p0 + pn, 12):
    b, e, u = struct.unpack_from('<III', IMG, i)
    if b == 0: break
    funcs.append((b, e))
CS = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
off = int(sys.argv[1], 16)
lo, hi = 0xb29000, 0xb36000
pat = re.compile(r'byte ptr \[r\w+ \+ %s\]' % hex(off))
hits = []
for b, e in funcs:
    if e - b > 0x3000: continue
    touch = False; calls = set()
    for ins in CS.disasm(IMG[b:e], b):
        if pat.search(ins.op_str): touch = True
        if ins.mnemonic == 'call' and ins.op_str.startswith('0x'):
            t = int(ins.op_str, 16)
            if lo <= t < hi: calls.add(t)
    if touch and calls:
        hits.append((b, e, sorted(calls)))
for b, e, c in hits:
    print(f'{b:x}-{e:x}', ' '.join(hex(x) for x in c))
