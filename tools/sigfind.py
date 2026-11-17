"""Re-find the signatures of sigs/sigs.json after a game update and propose new patterns.

  python -P tools/sigfind.py --old 1.0.2.58226 --new analysis/dump_<...>   # versions or dump dirs ('live' = running game)
  python -P tools/sigfind.py --old ... --new ... --only report_enter,area_bonus
  python -P tools/sigfind.py --old ... --new ... --write [--yes]   # after review: add them to sigs.json
  python -P tools/sigfind.py --selftest [--dump DIR]               # demo on one image (see selftest())

For every sig that does not match in the new image:
  1. the old image tells where it was: its function (.pdata, or int3 padding for leaf functions) and the
     instructions around it;
  2. candidate functions in the new image come from the sig's "where" (vtable slot, call target, scan
     range) and its "anchors": RTTI vtable slot, a referenced string, calls to / from a function another sig
     gives, a constant, a referenced global; with none of those, the functions near the old address
     (shifted by how far the sigs that still match have moved);
  3. each candidate is compared with the old function instruction by instruction, ignoring addresses (call
     and jump targets, rip-relative operands); the best one is aligned with the old function (difflib);
  4. the old pattern window and every field are mapped through that alignment. The new pattern is the new
     bytes there, call / jump displacements and rip offsets as ??, and the old pattern's own ?? (patch
     sites and the like) carried over. It must match as often as the manifest says (unique), else it is
     made longer;
  5. the new variant is checked like any other (expect / same_as / strings / vtable slot). A value that the
     code relies on and that changed (an "expect") is reported for a human; it is never written silently.
Proposals are printed and saved to analysis/sigfind_<new version>.json. --write asks per sig (or --yes)
and appends them to sigs.json as new variants for the new version (the old variants stay), then records
the new version's RVAs like `sigcheck.py --record`. Sigs that still match only get the version recorded.
"""
import argparse
import bisect
import copy
import difflib
import json
import os
import re
import struct
import sys

import capstone
from capstone import x86 as X
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import sigs as S  # noqa: E402
import sigcheck as C  # noqa: E402

CS = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
CS.detail = True
RIP_RX = re.compile(r'\[rip [+-] 0x[0-9a-f]+\]')
WEAK = 4000               # an anchor that fits more functions than this tells nothing
MAX_EXTEND = 24           # instructions added to make a pattern unique


# ---- code view of an image ---------------------------------------------------------------------------

class Code:
    """Functions (.pdata), disassembly and cross references of an Image."""

    def __init__(self, img):
        self.img, self.D = img, img.data
        self.ta, self.tn = img.sec('.text')
        pa, pn = img.sec('.pdata')
        a = np.frombuffer(img.data, dtype='<u4', count=pn // 12 * 3, offset=pa).reshape(-1, 3)
        a = a[(a[:, 0] != 0) & (a[:, 1] > a[:, 0])]
        a = a[np.argsort(a[:, 0], kind='stable')]
        self.pb, self.pe, self.pu = a[:, 0].tolist(), a[:, 1].tolist(), a[:, 2].tolist()
        self._prim = {}
        self._pieces = None
        self._tgt = None
        self._insns = {}

    def in_text(self, a):
        return self.ta <= a < self.ta + self.tn

    def _primary(self, i):
        if i in self._prim:
            return self._prim[i]
        b, u, D = self.pb[i], self.pu[i], self.D
        for _ in range(32):
            u &= ~1
            if not (D[u] >> 3) & 4:          # no UNW_FLAG_CHAININFO
                break
            c = D[u + 2]
            b, _e, u = struct.unpack_from('<III', D, u + 4 + 2 * ((c + 1) & ~1))
        self._prim[i] = b
        return b

    def pieces(self, start):
        if self._pieces is None:
            pc = {}
            for i in range(len(self.pb)):
                pc.setdefault(self._primary(i), []).append((self.pb[i], self.pe[i]))
            self._pieces = pc
        return sorted(self._pieces.get(start, []))

    def fstart(self, rva):
        """Start of the function containing rva (primary .pdata entry; else just after int3 padding)."""
        i = bisect.bisect_right(self.pb, rva) - 1
        if i >= 0 and self.pb[i] <= rva < self.pe[i]:
            return self._primary(i)
        s, lo = rva, max(self.ta, rva - 0x400)
        prev_end = self.pe[i] if i >= 0 else 0
        while s > lo and s > prev_end and self.D[s - 1] != 0xCC:
            s -= 1
        return s

    def func(self, rva):
        """(start, [(begin, end), ...])."""
        s = self.fstart(rva)
        p = self.pieces(s)
        if p:
            return s, p
        i = bisect.bisect_right(self.pb, s)
        limit = min(s + 0x400, self.pb[i] if i < len(self.pb) else s + 0x400)
        e = s
        for ins in CS.disasm(self.D[s:limit], s):
            if ins.mnemonic == 'int3':
                break
            e = ins.address + ins.size
        return s, [(s, e)]

    def insns(self, start):
        if start not in self._insns:
            out = []
            for b, e in self.func(start)[1]:
                out += list(CS.disasm(self.D[b:e], b))
            self._insns[start] = out
        return self._insns[start]

    def _targets(self):
        if self._tgt is None:
            n = self.tn - 3
            v = np.ndarray((n,), dtype='<i4', buffer=self.D, offset=self.ta, strides=(1,))
            self._tgt = v + np.arange(n, dtype=np.int32)          # target - ta - 4, for a disp32 at ta + i
        return self._tgt

    def refs_to(self, target):
        """Positions in .text of 32-bit displacements that point at target."""
        return [self.ta + int(h) for h in np.flatnonzero(self._targets() == np.int32(target - self.ta - 4))]

    def callers(self, target):
        return [p - 1 for p in self.refs_to(target) if self.D[p - 1] in (0xE8, 0xE9)]

    def anchor(self, a, ref):
        """Function starts that satisfy anchor a, or None when it cannot be evaluated here.
        ref('sig.field') gives the address of another sig's value in this image."""
        k, v = next(iter(a.items()))
        if k == 'vslot':
            vt = self.img.vtable(v[0])
            return None if vt is None else {self.fstart(self.img.slot(vt, v[1]))}
        if k in ('func_of', 'calls', 'called_by', 'refs'):
            t = None
            for r in (v if isinstance(v, list) else [v]):     # alternatives: the first one found here
                t = ref(r)
                if t is not None:
                    break
            if t is None:
                return None
            if k == 'func_of':
                return {self.fstart(t)}
            if k == 'calls':
                return {self.fstart(p) for p in self.callers(t)}
            if k == 'refs':
                return {self.fstart(p) for p in self.refs_to(t)}
            return {self.fstart(int(i.op_str, 16)) for i in self.insns(self.fstart(t))
                    if i.mnemonic in ('call', 'jmp') and i.op_str.startswith('0x') and self.in_text(int(i.op_str, 16))}
        if k == 'string':
            s = v.encode('ascii') + b'\0'
            out = set()
            for sec in ('.rdata', '.data'):
                a0, n = self.img.sec(sec)
                i = self.D.find(s, a0, a0 + n)
                while i >= 0:
                    if self.D[i - 1] == 0:
                        out |= {self.fstart(p) for p in self.refs_to(i)}
                    i = self.D.find(s, i + 1, a0 + n)
            return out
        if k == 'const':
            pat = struct.pack('<I', int(v, 0) & 0xffffffff if isinstance(v, str) else v & 0xffffffff)
            out, i, n = set(), self.D.find(pat, self.ta, self.ta + self.tn), 0
            while i >= 0:
                n += 1
                if n > WEAK * 4:
                    return None
                out.add(self.fstart(i))
                i = self.D.find(pat, i + 1, self.ta + self.tn)
            return out
        return None


def norm(ins):
    """Instruction text without addresses: call / jump targets and rip offsets dropped."""
    m, op = ins.mnemonic, ins.op_str
    if (m == 'call' or m.startswith('j') or m.startswith('loop')) and op.startswith('0x'):
        return m + ' T'
    return m + ' ' + RIP_RX.sub('[rip]', op)


def addr_bytes(ins):
    """Offsets inside ins of a rel32 call / jump displacement and of a rip-relative offset."""
    out = set()
    for op in ins.operands:
        if op.type == X.X86_OP_MEM and op.mem.base == X.X86_REG_RIP and ins.disp_offset:
            out.update(range(ins.disp_offset, ins.disp_offset + 4))
        elif op.type == X.X86_OP_IMM and ins.imm_size == 4 and (
                ins.group(capstone.CS_GRP_JUMP) or ins.group(capstone.CS_GRP_CALL)):
            out.update(range(ins.imm_offset, ins.imm_offset + 4))
    return out


class Side:
    """One function's instructions in one image."""

    def __init__(self, code, start):
        self.code, self.start = code, start
        self.insns = code.insns(start)
        self.addrs = [i.address for i in self.insns]
        self.toks = [norm(i) for i in self.insns]

    def index(self, a):
        i = bisect.bisect_right(self.addrs, a) - 1
        if i >= 0 and a < self.addrs[i] + self.insns[i].size:
            return i, a - self.addrs[i]
        return None, None


def align(a, b):
    """({old index: new index}, similarity) of two token lists."""
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    m = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal' or (tag == 'replace' and i2 - i1 == j2 - j1):
            for k in range(i2 - i1):
                m[i1 + k] = j1 + k
        elif tag == 'replace':
            am = [t.split(' ')[0] for t in a[i1:i2]]
            bm = [t.split(' ')[0] for t in b[j1:j2]]
            for t2, x1, x2, y1, _y2 in difflib.SequenceMatcher(None, am, bm, autojunk=False).get_opcodes():
                if t2 == 'equal':
                    for k in range(x2 - x1):
                        m[i1 + x1 + k] = j1 + y1 + k
    return m, sm.ratio()


def similarity(a, b):
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if sm.real_quick_ratio() < 0.3 or sm.quick_ratio() < 0.3:
        return 0.0
    return sm.ratio()


def map_addr(old, new, m, a):
    """Address in the new function that corresponds to old address a, or None."""
    i, intra = old.index(a)
    if i is None or i not in m:
        return None
    oi, ni = old.insns[i], new.insns[m[i]]
    if intra == 0 or oi.size == ni.size:
        return ni.address + intra
    if oi.imm_offset and intra == oi.imm_offset and ni.imm_offset:
        return ni.address + ni.imm_offset
    if oi.disp_offset and intra == oi.disp_offset and ni.disp_offset:
        return ni.address + ni.disp_offset
    return None


# ---- one sig -----------------------------------------------------------------------------------------

class Finder:
    def __init__(self, old_img, new_img, manifest, new_manifest=None, log=print):
        self.old_img, self.new_img = old_img, new_img
        self.old = Code(old_img)
        self.new = self.old if new_img is old_img else Code(new_img)
        self.m = manifest
        self.work = S.Manifest(manifest.path, data=copy.deepcopy((new_manifest or manifest).d))
        self.log = log
        self.old_res = S.resolve(old_img, manifest)
        self.new_res = S.resolve(new_img, self.work)

    def old_ref(self, ref):
        return S._ref(self.old_res, ref)

    def new_ref(self, ref):
        return S._ref(self.new_res, ref)

    def shift(self):
        """Median move of the sigs found in both images (new RVA - old RVA)."""
        d = sorted(self.new_res[n].addr - r.addr for n, r in self.old_res.items()
                   if r.ok and self.new_res[n].ok and r.hit is not None)
        return d[len(d) // 2] if d else 0

    def candidates(self, s, ofs, use_anchors=True):
        """({function start: anchors matched}, anchors used, notes)."""
        cands, used, notes = {}, 0, []
        for a in (s.anchors if use_anchors else []):
            old_set = self.old.anchor(a, self.old_ref)
            if old_set is not None and ofs not in old_set:
                notes.append('anchor %s does not hold in the old image (ignored)' % json.dumps(a, ensure_ascii=False))
                continue
            new_set = self.new.anchor(a, self.new_ref)
            if new_set is None or len(new_set) > WEAK:
                notes.append('anchor %s %s' % (json.dumps(a, ensure_ascii=False),
                                               'cannot be evaluated' if new_set is None else 'is too common'))
                continue
            used += 1
            for f in new_set:
                cands[f] = cands.get(f, 0) + 1
        return cands, used, notes

    def nearby(self, ofs, osize):
        """Functions near the old one, shifted by the median move."""
        c = ofs + self.shift()
        lo = bisect.bisect_left(self.new.pb, c - 0x20000)
        hi = bisect.bisect_right(self.new.pb, c + 0x20000)
        out = {}
        for i in range(lo, hi):
            f = self.new._primary(i)
            n = sum(e - b for b, e in self.new.pieces(f))
            if 0.6 * osize <= n <= 1.6 * osize:
                out[f] = 0
        return out

    def find(self, name, use_old=True):
        """Proposal dict for one sig."""
        s, r_old = self.work.sig(name), self.old_res[name]
        p = {'name': name, 'status': 'not_found', 'notes': []}
        if not r_old.ok:
            p['notes'].append('not found in the old image either: %s' % r_old.why)
            return p
        p['old'] = '%x' % r_old.addr
        if s.kind == 'rtti':
            p['notes'].append('RTTI class %s not found: look for the renamed class by hand' % s.where['rtti'])
            return p
        old_hit, ov = r_old.addr, r_old.hit.variant
        ofs = self.old.fstart(old_hit)
        old = Side(self.old, ofs)
        p['old_func'] = '%x' % ofs
        # where the sig must be, if its location comes from another sig
        fixed = region = None
        w = s.where
        if s.kind == 'vslot':
            vt = self.new_ref(w['vslot'][0])
            fixed = self.new_img.slot(vt, w['vslot'][1]) if vt is not None else None
        elif s.kind == 'target':
            fixed = self.new_ref(w['target'])
        elif s.kind == 'scan_from':
            t = self.new_ref(w['scan_from'])
            region = (t, t + w.get('range', 0x400)) if t is not None else None
        if s.kind in ('vslot', 'target') and fixed is None or s.kind == 'scan_from' and region is None:
            p['notes'].append('needs %s, which was not found in the new image' % (w.get('vslot') or w.get('target') or
                                                                                  w.get('scan_from')))
            return p
        anchors_used = 0
        if fixed is not None:
            cands = {self.new.fstart(fixed): 1}
        elif region is not None:
            cands = {self.new.fstart(region[0]): 1}
        else:
            cands, anchors_used, notes = self.candidates(s, ofs)
            p['notes'] += notes
            if not cands and use_old:
                cands = self.nearby(ofs, sum(e - b for b, e in self.old.func(ofs)[1]))
                p['notes'].append('no anchor found it: compared %d functions near the old address' % len(cands))
        p['candidates'] = len(cands)
        p['anchors'] = '%d' % anchors_used
        if not cands:
            p['notes'].append('no candidate function')
            return p
        # rank: anchors first, then similarity with the old function (only functions of a similar size)
        most = max(cands.values())
        top = [f for f, k in cands.items() if k == most]
        if not use_old:
            p['ranked'] = ['%x' % f for f in sorted(top)[:5]]
            if len(top) != 1:
                p['notes'].append('anchors alone leave %d functions' % len(top))
                return p
            scored = [(1.0, 0, top[0])]
        else:
            if len(top) > 1:
                osize = self.size(self.old, ofs)
                top = [f for f in top if 0.5 * osize - 16 <= self.size(self.new, f) <= 2 * osize + 16] or top
            near = ofs + self.shift()
            scored = sorted(((similarity(old.toks, Side(self.new, f).toks), -abs(f - near), f) for f in top), reverse=True)
        sim, _, nfs = scored[0]
        new = Side(self.new, nfs)
        m, sim = align(old.toks, new.toks)
        p['new_func'] = '%x' % nfs
        p['similarity'] = round(sim, 3)
        if len(scored) > 1:
            p['runner_up'] = round(scored[1][0], 3)
        if sim < 0.6:
            p['notes'].append('the best function is only %.0f%% like the old one' % (100 * sim))
        new_hit = map_addr(old, new, m, old_hit)
        if fixed is not None and old_hit == ofs:
            new_hit = fixed                         # a pattern at the start of a vtable function / call target
        if new_hit is None:
            p['notes'].append('the start of the old pattern has no counterpart in the new function')
            return p
        old_end = old_hit + len(ov.toks)
        e = map_addr(old, new, m, old_end - 1)
        if e is None:                               # last old instruction without counterpart: end at the last one that has
            i, _ = old.index(old_end - 1)
            while i is not None and i >= 0 and i not in m:
                i -= 1
            e = new.insns[m[i]].address + new.insns[m[i]].size - 1 if i is not None and i >= 0 else None
        if e is None or e < new_hit:
            p['notes'].append('the old pattern has no counterpart in the new function')
            return p
        toks = self.pattern(old, new, m, old_hit, ov, new_hit, e + 1)
        fields, fnotes = self.fields(old, new, m, old_hit, ov, new_hit)
        p['notes'] += fnotes
        toks = self.unique(s, new, toks, new_hit, region, p)
        if toks is None:
            return p
        var = {'versions': [C.version_of(self.new_img)], 'pattern': S.fmt_pattern(toks), 'fields': fields}
        p['new'] = '%x' % new_hit
        p['variant'] = var
        # check it like any other variant
        d = self.work.d['sigs'][name]
        d['variants'] = d['variants'] + [var]
        self.work = S.Manifest(self.work.path, data=self.work.d)
        self.new_res = S.resolve(self.new_img, self.work)
        r = self.new_res[name]
        if r.ok and r.addr == new_hit and sim >= 0.6:
            p['status'] = 'found'
            p['values'] = {k: ('%x' % v if isinstance(v, int) else v.hex(' ')) for k, v in r.values.items()}
            p['notes'] += r.warnings
        else:
            p['status'] = 'review'
            if not r.ok or r.addr != new_hit:
                p['notes'].append('the new pattern is not accepted as it is: %s' % (r.why or 'found elsewhere (%x)' % r.addr))
        return p

    def size(self, code, f):
        return sum(e - b for b, e in code.func(f)[1])

    def pattern(self, old, new, m, old_hit, ov, new_hit, new_end):
        """New pattern tokens for [new_hit, new_end)."""
        old_wild = {}                              # old instruction -> its ?? that are not addresses (patch sites ...)
        for k, b in enumerate(ov.toks):
            if b is None:
                i, intra = old.index(old_hit + k)
                if i is not None and intra not in addr_bytes(old.insns[i]):
                    old_wild.setdefault(i, set()).add(intra)
        inv = {j: i for i, j in m.items()}
        out, a, D = [], new_hit, self.new.D
        while a < new_end:
            j, intra = new.index(a)
            if j is None:
                out.append(D[a])
                a += 1
                continue
            ins = new.insns[j]
            wild = addr_bytes(ins)
            i = inv.get(j)
            if i is not None and i in old_wild:
                wild |= old_wild[i] if old.insns[i].size == ins.size else set(range(1, ins.size))
            stop = min(ins.size, intra + new_end - a)
            out += [None if k in wild else D[ins.address + k] for k in range(intra, stop)]
            a = ins.address + stop
        if out and out[0] is None:
            out[0] = D[new_hit]
        return out

    def fields(self, old, new, m, old_hit, ov, new_hit):
        fields, notes = {}, []
        for f, spec in ov.d.get('fields', {}).items():
            na = map_addr(old, new, m, old_hit + spec['at'])
            s2 = dict(spec)
            if na is None:
                notes.append('field %s: no counterpart in the new function, kept at +%d (check by hand)' % (f, spec['at']))
                fields[f] = s2
                continue
            s2['at'] = na - new_hit
            if spec['kind'] == 'rel':
                j, intra = new.index(na)
                if intra == 0 and new.insns[j].size != spec['len']:
                    notes.append('field %s: instruction is now %d bytes (was %d)' % (f, new.insns[j].size, spec['len']))
                    s2['len'] = new.insns[j].size
            fields[f] = s2
        return fields, notes

    def unique(self, s, new, toks, new_hit, region, p):
        """toks, made longer until the match count is what the manifest wants (scan sigs); None if impossible."""
        if s.kind in ('vslot', 'target'):
            return toks
        lo, hi = region if region else (self.new.ta, self.new.ta + self.new.tn)
        j, _ = new.index(new_hit + len(toks) - 1)
        for _ in range(MAX_EXTEND + 1):
            n = len(list(S.compile_pattern(S.fmt_pattern(toks)).finditer(self.new.D, lo, hi)))
            if n == s.count:
                return toks
            if j is None or j + 1 >= len(new.insns) or n < s.count:
                break
            j += 1
            ins = new.insns[j]
            if ins.address != new_hit + len(toks):
                break                               # next instruction is in another piece
            wild = addr_bytes(ins)
            toks = toks + [None if k in wild else self.new.D[ins.address + k] for k in range(ins.size)]
        p['notes'].append('the new pattern matches %d times, wanted %d' % (n, s.count))
        p['status'] = 'review'
        return None


# ---- driver -------------------------------------------------------------------------------------------

def run(finder, only=None, all_sigs=False, use_old=True):
    props = []
    for name in list(finder.old_res):
        if only and name not in only:
            continue
        r_new = finder.new_res[name]
        if r_new.ok and not all_sigs:
            props.append({'name': name, 'status': 'ok', 'old': '%x' % finder.old_res[name].addr
                          if finder.old_res[name].ok else None, 'new': '%x' % r_new.addr, 'notes': r_new.warnings})
            continue
        props.append(finder.find(name, use_old))
    return props


STATUS = {'ok': '原特征码仍可用', 'found': '已找回', 'review': '找到了，需要人工确认', 'not_found': '没找到'}


def show(props):
    for p in props:
        line = '%-20s %-12s' % (p['name'], STATUS[p['status']])
        if p.get('old'):
            line += ' 旧 %s' % p['old']
        if p.get('new'):
            line += ' -> 新 %s' % p['new']
        if 'similarity' in p:
            line += '  函数 %s→%s 相似度 %.2f%s 候选 %s 锚点 %s' % (
                p.get('old_func'), p.get('new_func'), p['similarity'],
                ('（次优 %.2f）' % p['runner_up']) if 'runner_up' in p else '', p.get('candidates'), p.get('anchors'))
        print(line)
        if p.get('variant'):
            print('      新特征码: %s' % p['variant']['pattern'])
        for n in p.get('notes', []):
            print('      - %s' % n)


def open_any(spec):
    if spec == 'live':
        import patch as P
        pid, hp = P.open_game()
        if not hp:
            sys.exit('the game is not running')
        return C.live_image(hp)
    if os.path.isdir(spec):
        return S.Image.from_dump(spec)
    d = C.dump_for_version(spec)
    if not d:
        sys.exit('no image for %s (a dump directory or a version with an archived dump in analysis/)' % spec)
    return S.Image.from_dump(d)


def write(props, new_img, yes, path=S.MANIFEST):
    m = S.Manifest(path)
    n = 0
    for p in props:
        if p['status'] not in ('found', 'review') or not p.get('variant'):
            continue
        if p['status'] == 'review':
            print('%s 需要人工确认：%s' % (p['name'], '；'.join(p['notes'])))
            if yes:
                print('  （--yes 不写需要人工确认的；看过后不带 --yes 再运行，逐条确认）')
                continue
        if not yes:
            ans = input('把 %s 的新特征码写进清单？[y/N] ' % p['name']).strip().lower()
            if ans != 'y':
                continue
        variants = m.d['sigs'][p['name']]['variants']
        variants[:] = [v for v in variants if v.get('pattern') != p['variant']['pattern']] + [p['variant']]
        n += 1
    print('写入 %d 个新变体' % n)
    m = S.Manifest(m.path, data=m.d)       # rebuild the sig objects with the new variants
    res = S.resolve(new_img, m)
    C.record(new_img, res, m)          # adds the version to every variant that matches, records RVAs, saves


# ---- self test ------------------------------------------------------------------------------------------

def corrupt(toks):
    """Change two fixed bytes so the pattern matches nowhere."""
    out = list(toks)
    fixed = [i for i, b in enumerate(out) if b is not None and i > 0]
    for i in (fixed[len(fixed) // 3], fixed[2 * len(fixed) // 3]):
        out[i] ^= 0x5A
    return out


def mutate_one(img, res, name):
    """A copy of the image where one struct offset / constant inside the sig's pattern is 8 larger, as a
    compiler would emit after the structure grew. Returns (image, description) or (None, why)."""
    r = res[name]
    hit, ov = r.addr, r.hit.variant
    code = list(CS.disasm(img.data[hit:hit + len(ov.toks) + 16], hit))
    spots = []                              # (preference, address, instruction): structure offsets first
    for ins in code:
        o = ins.address - hit
        if o + ins.size > len(ov.toks):
            break
        if ins.group(capstone.CS_GRP_JUMP) or ins.group(capstone.CS_GRP_CALL):
            continue
        mems = [op for op in ins.operands if op.type == X.X86_OP_MEM]
        if any(op.mem.base == X.X86_REG_RIP for op in mems):
            continue
        for off, size, kind in ((ins.disp_offset, ins.disp_size, 'disp'), (ins.imm_offset, ins.imm_size, 'imm')):
            if not off or size not in (1, 4) or ov.toks[o + off] is None:
                continue
            v = img.data[ins.address + off]
            if v > 0xf0 or (size == 1 and v >= 0x70):
                continue
            stack = any(op.mem.base in (X.X86_REG_RSP, X.X86_REG_RBP) for op in mems) or ins.mnemonic in ('sub', 'add')
            spots.append((0 if kind == 'disp' and not stack else 1 if kind == 'imm' else 2, ins.address + off, ins))
    if not spots:
        return None, 'no constant in the pattern to change'
    _, a, ins = min(spots, key=lambda s: (s[0], s[1]))
    v = img.data[a]
    data = bytearray(img.data)
    data[a] = v + 8
    new = S.Image(bytes(data), img.base, img.label + ' (+8 at %x)' % a)
    return new, '%x: %s %s，字节 %02x 改成 %02x' % (ins.address, ins.mnemonic, ins.op_str, v, v + 8)


def selftest(img):
    """Three checks on one image:
    A. every pattern broken (two fixed bytes changed) -> must come back at the same place with the same fields;
    B. anchors only (no old function to compare with) -> how many functions the anchors leave;
    C. a simulated update: one structure offset / constant in the pattern 8 larger -> found again, new
       pattern matches the changed code, a changed expected value is reported."""
    m = S.load()
    ok_all = True
    base = Finder(img, img, m, log=lambda *a: None)
    names = [n for n, s in m.sigs.items() if s.kind != 'rtti']
    print('A. 特征码改坏后找回（同一镜像）')
    for name in names:
        work = copy.deepcopy(m.d)
        for v in work['sigs'][name]['variants']:
            v['pattern'] = S.fmt_pattern(corrupt(S.tokens(v['pattern'])))
        f = Finder(img, img, m, S.Manifest(m.path, data=work))
        f.old, f.new = base.old, base.old
        if f.new_res[name].ok:
            print('  ?    %-20s the broken pattern still matches' % name)
            continue
        p = f.find(name)
        want = base.old_res[name]
        got = f.new_res[name]
        same = p['status'] == 'found' and got.addr == want.addr and got.values == want.values
        ok_all &= same
        print('  %s %-20s %s%s' % ('ok  ' if same else 'FAIL', name, '找回 %s，字段一致' % p.get('new') if same else
                                    STATUS[p['status']], ('（相似度 %.2f，候选 %s）' % (p['similarity'], p['candidates']))
                                    if 'similarity' in p else ''))
        if not same:
            for n in p['notes']:
                print('         - %s' % n)
    print()
    print('B. 只用锚点（不和旧函数比相似度）')
    for name in names:
        s = m.sig(name)
        if s.kind != 'scan':
            print('  -    %-20s 位置由 where 决定（%s），不需要锚点' % (name, next(iter(s.where))))
            continue
        ofs = base.old.fstart(base.old_res[name].addr)
        cands, used, notes = base.candidates(s, ofs)
        for n in notes:
            if 'does not hold' in n:
                ok_all = False
                print('  FAIL %-20s %s' % (name, n))
        best = max(cands.values()) if cands else 0
        top = sorted(f for f, k in cands.items() if k == best)
        verdict = ('唯一，正确' if top == [ofs] else ('正确函数在 %d 个并列里' % len(top) if ofs in top else '没包含正确函数')) \
            if top else '锚点找不到候选'
        print('  %s %-20s 锚点 %d 条，候选 %d 个，最高分 %d 个：%s' % ('ok  ' if top == [ofs] else 'weak', name, used, len(cands),
                                                          len(top), verdict))
    print()
    print('C. 模拟游戏更新：特征码里一个结构偏移或常数 +8')
    for name in names:
        new_img, what = mutate_one(img, base.old_res, name)
        if new_img is None:
            print('  -    %-20s %s' % (name, what))
            continue
        f = Finder(img, new_img, m)
        if f.new_res[name].ok:
            print('  ?    %-20s still matches after %s' % (name, what))
            continue
        p = f.find(name)
        hit_ok = p.get('new') == '%x' % base.old_res[name].addr
        good = hit_ok and p['status'] in ('found', 'review')
        ok_all &= good
        print('  %s %-20s %s：%s %s' % ('ok  ' if good else 'FAIL', name, what, STATUS[p['status']],
                                      '@%s' % p.get('new') if p.get('new') else ''))
        for n in p['notes']:
            if 'expected' in n or 'not accepted' in n or 'note' in n:
                print('         - %s' % n)
    print()
    print('自检%s' % ('全部通过' if ok_all else '有失败项'))
    return 0 if ok_all else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--old')
    ap.add_argument('--new')
    ap.add_argument('--only', help='comma-separated sig names')
    ap.add_argument('--all', action='store_true', help='also re-find sigs that still match (for testing)')
    ap.add_argument('--write', action='store_true')
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--dump', help='image for --selftest (default: the archived image of the newest tested version)')
    ap.add_argument('--sigs', help='another manifest file to read and --write (default sigs/sigs.json)')
    args = ap.parse_args()
    if args.selftest:
        d = args.dump or C.dump_for_version(sorted(S.load().tested_versions())[-1])
        return selftest(S.Image.from_dump(d))
    if not args.old or not args.new:
        ap.error('--old and --new are needed (or --selftest)')
    manifest = S.Manifest(args.sigs) if args.sigs else S.load()
    old_img, new_img = open_any(args.old), open_any(args.new)
    print('旧：%s（%s）  新：%s（%s）' % (old_img.label, C.version_of(old_img), new_img.label, C.version_of(new_img)))
    f = Finder(old_img, new_img, manifest)
    props = run(f, set(args.only.split(',')) if args.only else None, args.all)
    show(props)
    out = os.path.join(ROOT, 'analysis', 'sigfind_%s.json' % C.version_of(new_img))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w', encoding='utf-8') as fh:
        json.dump({'old': C.version_of(old_img), 'new': C.version_of(new_img), 'proposals': props}, fh,
                  ensure_ascii=False, indent=1)
    print('建议已存到 %s' % os.path.relpath(out, ROOT))
    if args.write:
        write(props, new_img, args.yes, manifest.path)
    bad = [p['name'] for p in props if p['status'] in ('not_found', 'review')]
    return 1 if bad else 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
