import sys,ctypes,struct
sys.path.insert(0,__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__))))
import gamedata as G
g=G.GameData(); st=g.q(g.root+G.STATUS)
k=ctypes.windll.kernel32
def w(a,b):
    n=ctypes.c_size_t(); return k.WriteProcessMemory(ctypes.c_void_p(g.hp),ctypes.c_void_p(a),b,len(b),ctypes.byref(n))
fid=int(sys.argv[1])
flags=g.d(st+0x16a8)
w(st+0x3a,bytes([fid]))
nf = flags & ~0x100 if fid else flags | 0x100
w(st+0x16a8,struct.pack('<I',nf & 0xffffffff))
print('3a',g.b(st+0x3a),'flags',hex(g.d(st+0x16a8)))
