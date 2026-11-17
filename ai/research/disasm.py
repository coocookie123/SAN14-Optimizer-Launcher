"""Linear disassembly from an RVA in the dumped image: python -P ai/research/disasm.py <rva> [bytes]"""
import glob, json, os, sys
import capstone
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
d = sorted(glob.glob(os.path.join(ROOT, 'analysis', 'dump_*')))[-1]
IMG = open(os.path.join(d, 'image.bin'), 'rb').read()
CS = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
a = int(sys.argv[1], 16); n = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0x200
for i in CS.disasm(IMG[a:a + n], a):
    print(f'{i.address:8x}  {i.mnemonic:6} {i.op_str}')
