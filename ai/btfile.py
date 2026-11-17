"""Read and write the behavior-tree file 0000/bt_exce.bin (三国志14 传承版).

Layout (all little-endian u32):

  JBTP file
    'JBTP', version (2), tree count, 0
    count x (offset, size, hash)          -- hash: CRC32 of the tree section, byte-swapped
    trees, back to back

  tree section
    'TB1G0000', size, 0x20, chunk count, 0, 0, 0      (0x20 bytes)
    'IZSG0000', 0x18, 0, 1, 1                         (0x18 bytes)
    chunk count - 1 chunks: 'ITBG0000' / 'IBBG0000', size (incl. 16-byte header), 0, body

  ITBG body = KTBT tree (loader: SAN14CE_SC.exe 1.0.2.58226 RVA b2cae0)
    'KTBT', version (1), tree name hash, 0
    node count
    nodes, each:
      flags, type hash
      [flags & 4] extra
      parent index (-1 for the root), child count
      n, n x arg
      [flags & 2] len, len bytes (name)
      [flags & 8] n2, n2 x arg2

  IBBG body = blackboard definition, kept as raw bytes for now.

The game reads the file once while the program starts (RVA 153080 -> c0ca0), not when a save
is loaded.
"""
import struct
import zlib

U32 = struct.Struct('<I')


def section_hash(sec):
    """The per-tree hash in the JBTP header is the CRC32 of the section, stored byte-swapped."""
    return int.from_bytes(zlib.crc32(sec).to_bytes(4, 'little'), 'big')


class Reader:
    def __init__(self, data, pos=0, end=None):
        self.d = data
        self.p = pos
        self.end = len(data) if end is None else end

    def u32(self):
        if self.p + 4 > self.end:
            raise ValueError(f'read past end at {self.p:#x}')
        v = U32.unpack_from(self.d, self.p)[0]
        self.p += 4
        return v

    def u32s(self, n):
        if self.p + 4 * n > self.end:
            raise ValueError(f'read past end at {self.p:#x}')
        v = list(struct.unpack_from(f'<{n}I', self.d, self.p))
        self.p += 4 * n
        return v

    def raw(self, n):
        if self.p + n > self.end:
            raise ValueError(f'read past end at {self.p:#x}')
        v = self.d[self.p:self.p + n]
        self.p += n
        return v


class Node:
    __slots__ = ('index', 'flags', 'type', 'extra', 'parent', 'nchild', 'args', 'name', 'args2', 'children')

    def __init__(self):
        self.extra = None
        self.name = None
        self.args2 = None
        self.children = []

    def to_bytes(self):
        out = [U32.pack(self.flags), U32.pack(self.type)]
        if self.flags & 4:
            out.append(U32.pack(self.extra))
        out += [U32.pack(self.parent), U32.pack(len(self.children)), U32.pack(len(self.args))]
        out += [U32.pack(a) for a in self.args]
        if self.flags & 2:
            out += [U32.pack(len(self.name)), self.name]
        if self.flags & 8:
            out.append(U32.pack(len(self.args2)))
            out += [U32.pack(a) for a in self.args2]
        return b''.join(out)


class KTBT:
    def __init__(self, body):
        r = Reader(body)
        magic = r.raw(4)
        if magic != b'KTBT':
            raise ValueError(f'not KTBT: {magic!r}')
        self.version, self.name_hash, self.pad = r.u32s(3)
        count = r.u32()
        self.nodes = []
        for i in range(count):
            n = Node()
            n.index = i
            n.flags = r.u32()
            n.type = r.u32()
            if n.flags & 4:
                n.extra = r.u32()
            n.parent = r.u32()
            n.nchild = r.u32()
            n.args = r.u32s(r.u32())
            if n.flags & 2:
                n.name = r.raw(r.u32())
            if n.flags & 8:
                n.args2 = r.u32s(r.u32())
            self.nodes.append(n)
        self.tail = body[r.p:]
        roots = []
        for n in self.nodes:
            if n.parent == 0xFFFFFFFF:
                roots.append(n)
            else:
                self.nodes[n.parent].children.append(n)
        if len(roots) != 1:
            raise ValueError(f'{len(roots)} root nodes')
        self.root = roots[0]

    def renumber(self):
        """Lay the nodes out in pre-order from the root (the game's own order) and fix indices,
        parent links and child counts. Call after adding, removing or moving nodes."""
        order = []
        stack = [(self.root, 0xFFFFFFFF)]
        while stack:
            n, parent = stack.pop()
            n.index = len(order)
            n.parent = parent
            order.append(n)
            for c in reversed(n.children):
                stack.append((c, n.index))
        self.nodes = order

    def to_bytes(self):
        self.renumber()
        head = b'KTBT' + struct.pack('<4I', self.version, self.name_hash, self.pad, len(self.nodes))
        return head + b''.join(n.to_bytes() for n in self.nodes) + self.tail


class Tree:
    """One tree section: TB1G header, IZSG block, then ITBG (tree) and IBBG (blackboard) chunks."""

    def __init__(self, sec):
        r = Reader(sec)
        if r.raw(8) != b'TB1G0000':
            raise ValueError('not TB1G')
        size, hsize, count, *self.tb1g_rest = r.u32s(6)
        if size != len(sec) or hsize != 0x20:
            raise ValueError(f'TB1G size {size:#x}/{len(sec):#x} header {hsize:#x}')
        self.izsg = r.raw(0x18)
        if not self.izsg.startswith(b'IZSG0000'):
            raise ValueError('no IZSG')
        self.chunks = []  # (magic, pad, body or KTBT)
        for _ in range(count - 1):
            magic = r.raw(8)
            csize, pad = r.u32s(2)
            body = r.raw(csize - 16)
            if magic == b'ITBG0000':
                body = KTBT(body)
            self.chunks.append((magic, pad, body))
        if r.p != len(sec):
            raise ValueError(f'{len(sec) - r.p} bytes after the last chunk')

    @property
    def ktbt(self):
        for magic, _, body in self.chunks:
            if magic == b'ITBG0000':
                return body
        return None

    def to_bytes(self):
        parts = []
        for magic, pad, body in self.chunks:
            b = body.to_bytes() if isinstance(body, KTBT) else body
            parts.append(magic + struct.pack('<2I', len(b) + 16, pad) + b)
        body = self.izsg + b''.join(parts)
        head = b'TB1G0000' + struct.pack('<6I', 0x20 + len(body), 0x20, len(self.chunks) + 1, *self.tb1g_rest)
        return head + body


class BTFile:
    def __init__(self, data):
        r = Reader(data)
        if r.raw(4) != b'JBTP':
            raise ValueError('not JBTP')
        self.version, count, self.pad = r.u32s(3)
        self.entries = [tuple(r.u32s(3)) for _ in range(count)]
        self.trees = []
        pos = r.p
        for off, size, h in self.entries:
            if off != pos:
                raise ValueError(f'gap before tree at {off:#x}')
            self.trees.append(Tree(data[off:off + size]))
            pos = off + size
        if pos != len(data):
            raise ValueError(f'{len(data) - pos} bytes after the last tree')

    def to_bytes(self):
        secs = [t.to_bytes() for t in self.trees]
        head_len = 0x10 + 12 * len(secs)
        out = [b'JBTP', struct.pack('<3I', self.version, len(secs), self.pad)]
        off = head_len
        for sec in secs:
            out.append(struct.pack('<3I', off, len(sec), section_hash(sec)))
            off += len(sec)
        return b''.join(out) + b''.join(secs)


def load(path):
    with open(path, 'rb') as f:
        return BTFile(f.read())


if __name__ == '__main__':
    import sys
    data = open(sys.argv[1], 'rb').read()
    bt = BTFile(data)
    for i, t in enumerate(bt.trees):
        k = t.ktbt
        print(f'tree {i}: name {k.name_hash:08x}, {len(k.nodes)} nodes, tail {len(k.tail)} bytes, '
              f'chunks {[c[0][:4].decode() for c in t.chunks]}')
    print('round trip identical:', bt.to_bytes() == data)
