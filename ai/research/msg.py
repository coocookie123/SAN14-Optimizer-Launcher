"""Decode zp1 msgdata and grep. usage: python -P msg.py <file> <regex> [context]"""
import re
import struct
import sys
import zlib

raw = open(sys.argv[1], 'rb').read()
size, bsz, n = struct.unpack_from('<III', raw, 4)
sizes = struct.unpack_from(f'<{n}I', raw, 16)
pos = 0x800
out = b''
for s in sizes:
    ln = struct.unpack_from('<I', raw, pos)[0]
    out += zlib.decompress(raw[pos + 4:pos + 4 + ln])
    pos += s
    pos = (pos + 0x7f) & ~0x7f
strs = out.decode('utf-16le', 'replace').split('\0')
rx = re.compile(sys.argv[2])
ctx = int(sys.argv[3]) if len(sys.argv) > 3 else 0
o = open(sys.argv[4] if len(sys.argv) > 4 else 'msg_out.txt', 'w', encoding='utf-8')
for i, s in enumerate(strs):
    if rx.search(s):
        for j in range(max(0, i - ctx), min(len(strs), i + ctx + 1)):
            o.write(f'{j}{"*" if j == i else " "} {strs[j]!r}\n')
        o.write('--\n')
print(len(strs), 'strings')
