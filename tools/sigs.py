"""The signature manifest sigs/sigs.json: every byte pattern and RTTI name the mod relies on, in one place.

Each entry ("sig") says which features use it, how to find it (scan .text, at a vtable slot, at the target
of another sig's call ...), how many matches are expected, which values to take from the match ("fields":
call/jump targets, rip-relative addresses, immediates, patch sites), what those values must be ("expect"),
the RVAs in every known game version, and "anchors" that help tools/sigfind.py re-find the code after a
game update. A sig has one or more "variants" (pattern + fields); a new game version that needs a new
pattern gets a new variant, the old one stays for the old version.

Runtime use (patch.py, area_bonus.py, gamestate.py, takeover.py, ai/gamedata.py):
    hit = sigs.find('report_enter', text, text_addr)      # raises SigError when not found
    hit.addr, hit.at('site'), hit.target('je2'), hit.value('status_off'), hit.expect('site')
Whole-image use (tools/sigcheck.py, tools/sigfind.py, report_button's per-feature check):
    img = Image(data, base)            # laid out by RVA: image.bin of tools/dump.py, or read from the game
    res = resolve(img)                 # {name: Result}
    features(res)                      # {feature: (ok, [reasons])}
The DLL (overlay/san14_overlay.cpp) gets the same patterns through overlay/sigs_gen.h, generated from the
manifest by overlay/build.py.
"""
import json
import os
import re
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MANIFEST = os.path.join(ROOT, 'sigs', 'sigs.json')
WHERE_KINDS = ('scan', 'vslot', 'target', 'scan_from', 'rtti')
FIELD_KINDS = ('rel', 'site', 'u8', 'u16', 'u32', 'i32', 'bytes')


class SigError(RuntimeError):
    pass


def tokens(p):
    """'48 8B ?? C3' -> [0x48, 0x8b, None, 0xc3]"""
    return [None if t == '??' else int(t, 16) for t in p.split()]


def compile_pattern(p):
    return re.compile(b''.join(b'.' if t == '??' else re.escape(bytes([int(t, 16)])) for t in p.split()), re.S)


def fmt_pattern(toks):
    return ' '.join('??' if b is None else '%02X' % b for b in toks)


NUMERIC = ('u8', 'u16', 'u32', 'i32')


def _num(x):
    return int(x, 0) if isinstance(x, str) else x


def _norm_field(spec):
    """Field spec with numeric expect/patched values as ints ("0x470" in the JSON -> 0x470)."""
    s = dict(spec)
    if s.get('kind') in NUMERIC:
        for k in ('expect', 'patched'):
            if k in s:
                s[k] = [_num(v) for v in s[k]] if isinstance(s[k], list) else _num(s[k])
    return s


def bytes_match(got, want_hex):
    """got starts with want_hex ('48 8d ?? 05': ?? matches any byte)."""
    want = tokens(want_hex)
    return len(got) >= len(want) and all(w is None or got[i] == w for i, w in enumerate(want))


class Variant:
    def __init__(self, sig, index, d):
        self.sig, self.index = sig, index
        self.d = d
        self.pattern = d.get('pattern')
        self.fields = {k: _norm_field(v) for k, v in d.get('fields', {}).items()}
        self.versions = d.get('versions', [])
        self.toks = tokens(self.pattern) if self.pattern else []
        self._rx = None

    @property
    def rx(self):
        if self._rx is None:
            self._rx = compile_pattern(self.pattern)
        return self._rx

    def match_at(self, buf, off):
        if off < 0 or off + len(self.toks) > len(buf):
            return False
        return all(b is None or buf[off + i] == b for i, b in enumerate(self.toks))


class Sig:
    def __init__(self, name, d):
        self.name = name
        self.d = d
        self.desc = d.get('desc', '')
        self.features = d.get('features', [])
        self.where = d.get('where', {'scan': 'text'})
        self.kind = next((k for k in WHERE_KINDS if k in self.where), 'scan')
        self.count = d.get('count', 1)
        self.optional = d.get('optional', False)       # True, or the features for which it is optional
        self.anchors = d.get('anchors', [])
        self.checks = d.get('checks', [])
        self.rva = d.get('rva', {})
        self.variants = [Variant(self, i, v) for i, v in enumerate(d.get('variants', []))]

    def optional_for(self, feature):
        return self.optional is True or (isinstance(self.optional, list) and feature in self.optional)

    def deps(self):
        """Names of the sigs this one needs first (its location or its checks refer to them)."""
        out = []
        w = self.where.get('target') or self.where.get('scan_from')
        if w:
            out.append(w.split('.')[0])
        if 'vslot' in self.where:
            out.append(self.where['vslot'][0])
        for c in self.checks:
            if 'vslot_is' in c:
                out.append(c['vslot_is'][0])
        for v in self.variants:
            for f in v.fields.values():
                if isinstance(f.get('same_as'), str):
                    out.append(f['same_as'].split('.')[0])
        return [n for n in out if n != self.name]


class Manifest:
    def __init__(self, path=MANIFEST, data=None):
        """From the file, or from an already loaded dict (data; path is then only where save() writes)."""
        self.path = path
        if data is None:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        self.d = data
        self.features = self.d.get('features', {})
        self.versions = self.d.get('versions', {})
        self.sigs = {n: Sig(n, s) for n, s in self.d['sigs'].items()}

    def sig(self, name):
        try:
            return self.sigs[name]
        except KeyError:
            raise SigError('no sig named %r in %s' % (name, self.path))

    def tested_versions(self):
        return {v for v, m in self.versions.items() if m.get('tested')}

    def save(self, path=None):
        """Write back (keeps key order; one sig per block, patterns on one line)."""
        with open(path or self.path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(dumps(self.d))
            f.write('\n')


def dumps(d):
    """JSON with short lists and field specs on one line, so the file stays readable and diffable."""
    def inline(x):
        return json.dumps(x, ensure_ascii=False)

    def flat(x):
        return all(not isinstance(v, (dict, list)) or (isinstance(v, list) and flat(v)) for v in
                   (x.values() if isinstance(x, dict) else x))

    def enc(x, ind, key=None):
        pad = ' ' * ind
        if isinstance(x, dict):
            if not x:
                return '{}'
            if key == 'fields':
                items = ['%s  %s: %s' % (pad, inline(k), inline(v)) for k, v in x.items()]
                return '{\n' + ',\n'.join(items) + '\n' + pad + '}'
            if key not in ('sigs', 'features', 'versions', 'rva') and flat(x) and len(inline(x)) < 170:
                return inline(x)
            items = ['%s  %s: %s' % (pad, inline(k), enc(v, ind + 2, k)) for k, v in x.items()]
            return '{\n' + ',\n'.join(items) + '\n' + pad + '}'
        if isinstance(x, list):
            if flat(x) and (len(inline(x)) < 120 or key == 'versions'):
                return inline(x)
            items = ['%s  %s' % (pad, enc(v, ind + 2)) for v in x]
            return '[\n' + ',\n'.join(items) + '\n' + pad + ']'
        return inline(x)
    return enc(d, 0)


_cache = {}


def load(path=None):
    path = path or MANIFEST
    m = _cache.get(path)
    if m is None:
        m = _cache[path] = Manifest(path)
    return m


def sig(name):
    return load().sig(name)


# ---- matches --------------------------------------------------------------------------------------

class Hit:
    """A match of one variant. buf holds the code, buf_addr is the address of buf[0] (an absolute address
    for a live .text buffer, 0 for a whole image laid out by RVA), so every address here is in the same
    space as buf_addr."""

    def __init__(self, sig, variant, buf, buf_addr, off):
        self.sig, self.variant = sig, variant
        self.buf, self.buf_addr, self.off = buf, buf_addr, off
        self.addr = buf_addr + off

    def spec(self, f):
        try:
            return self.variant.fields[f]
        except KeyError:
            raise SigError('%s has no field %r' % (self.sig.name, f))

    def at(self, f):
        """Address of the field (start of its instruction for 'rel')."""
        return self.addr + self.spec(f)['at']

    def raw(self, f, n=None):
        s = self.spec(f)
        o = self.off + s['at']
        n = n or s.get('len') or {'u8': 1, 'u16': 2, 'u32': 4, 'i32': 4}.get(s['kind'], 4)
        if o < 0 or o + n > len(self.buf):
            raise SigError('%s.%s is outside the code buffer' % (self.sig.name, f))
        return bytes(self.buf[o:o + n])

    def target(self, f):
        """Target of a call / jump / rip-relative operand: the instruction is `len` bytes long and ends
        with its 32-bit displacement."""
        s = self.spec(f)
        end = self.off + s['at'] + s['len']
        return self.buf_addr + end + struct.unpack_from('<i', self.buf, end - 4)[0]

    def value(self, f):
        s = self.spec(f)
        k = s['kind']
        if k == 'rel':
            return self.target(f)
        if k == 'site':
            return self.at(f)
        if k == 'bytes':
            return self.raw(f)
        fmt = {'u8': '<B', 'u16': '<H', 'u32': '<I', 'i32': '<i'}[k]
        return struct.unpack(fmt, self.raw(f))[0]

    def expect(self, f, key='expect'):
        """The expected original value of a field (key='patched': the mod's own value); bytes for
        'site'/'bytes' fields (only when it has no ?? in it)."""
        e = self.spec(f).get(key)
        if isinstance(e, list):
            e = e[0]
        if isinstance(e, str):
            return bytes.fromhex(e)
        return e

    def state(self, f):
        """'original', 'patched' (holds the mod's own bytes / value), 'unchecked' (no expect) or 'unknown'."""
        s = self.spec(f)
        if 'expect' not in s:
            return 'unchecked'
        for key in ('expect', 'patched'):
            if key not in s:
                continue
            vals = s[key] if isinstance(s[key], list) else [s[key]]
            if s['kind'] in ('site', 'bytes'):
                got = self.raw(f, max(len(tokens(v)) for v in vals))
                if any(bytes_match(got, v) for v in vals):
                    return 'original' if key == 'expect' else 'patched'
            elif self.value(f) in vals:
                return 'original' if key == 'expect' else 'patched'
        return 'unknown'

    def check(self, f):
        """None, or why the field value is neither the expected original nor the mod's patched one
        (only checks what the buffer holds; string targets etc. are checked by resolve())."""
        if self.state(f) != 'unknown':
            return None
        s = self.spec(f)
        exp = s['expect'] if isinstance(s['expect'], list) else [s['expect']]
        if s['kind'] in ('site', 'bytes'):
            got = self.raw(f, max(len(tokens(e)) for e in exp))
            return '%s.%s is %s, expected %s' % (self.sig.name, f, got.hex(' '), ' or '.join(exp))
        return '%s.%s is %#x, expected %s' % (self.sig.name, f, self.value(f), ' or '.join('%#x' % e for e in exp))

    def checked(self, *names):
        """Raise SigError if a field (default: all with "expect") does not hold the expected value."""
        for f in names or [k for k, s in self.variant.fields.items() if 'expect' in s and not s.get('soft')]:
            why = self.check(f)
            if why:
                raise SigError(why)
        return self


def scan(sig, buf, buf_addr=0, lo=0, hi=None):
    """Hit of the first variant that matches exactly sig.count times in buf[lo:hi]; SigError otherwise."""
    if isinstance(sig, str):
        sig = load().sig(sig)
    hi = len(buf) if hi is None else min(hi, len(buf))
    counts = []
    for v in sig.variants:
        hits = [m.start() for m in v.rx.finditer(buf, lo, hi)]
        if len(hits) == sig.count:
            return Hit(sig, v, buf, buf_addr, hits[0])
        counts.append(len(hits))
    raise SigError('%s pattern matched %s times (expected %d): game version changed or code not decrypted yet' % (
        sig.name, '/'.join(map(str, counts)) or 'no pattern', sig.count))


def match_at(sig, buf, buf_addr, addr):
    """Hit of the first variant that matches at addr (vtable slot / call target sigs)."""
    if isinstance(sig, str):
        sig = load().sig(sig)
    off = addr - buf_addr
    for v in sig.variants:
        if v.match_at(buf, off):
            return Hit(sig, v, buf, buf_addr, off)
    raise SigError('%s not recognised at %#x' % (sig.name, addr))


def find(name, text, text_addr):
    """Runtime helper: the unique match of a 'scan' sig in the game's .text (read with ReadProcessMemory).
    Fields with "expect" are checked too."""
    return scan(name, text, text_addr).checked()


# ---- whole images -----------------------------------------------------------------------------------

class Image:
    """A module image laid out by RVA. data: bytes; base: the runtime base the pointers in it use."""

    def __init__(self, data, base, label=''):
        self.data, self.base, self.label = data, base, label
        pe = struct.unpack_from('<I', data, 0x3c)[0]
        self.timestamp = struct.unpack_from('<I', data, pe + 8)[0]
        nsec = struct.unpack_from('<H', data, pe + 6)[0]
        opt = pe + 24
        self.size_of_image = struct.unpack_from('<I', data, opt + 56)[0]
        so = opt + struct.unpack_from('<H', data, pe + 20)[0]
        self.secs = {}
        for i in range(nsec):
            n, vs, va = struct.unpack_from('<8sII', data, so + 40 * i)
            self.secs.setdefault(n.rstrip(b'\0').decode('ascii', 'replace'), (va, vs))
        self._vt = {}

    @classmethod
    def from_dump(cls, d):
        with open(os.path.join(d, 'meta.json'), encoding='utf-8') as f:
            meta = json.load(f)
        with open(os.path.join(d, 'image.bin'), 'rb') as f:
            data = f.read()
        img = cls(data, int(meta['runtime_base'], 16), d)
        img.meta = meta
        return img

    def sec(self, name):
        return self.secs[name]

    def in_sec(self, rva, name):
        a, n = self.secs.get(name, (0, 0))
        return a <= rva < a + n

    def q(self, rva):
        return struct.unpack_from('<Q', self.data, rva)[0]

    def d32(self, rva):
        return struct.unpack_from('<I', self.data, rva)[0]

    def cstr(self, rva, n=256):
        if not 0 <= rva < len(self.data):
            return None
        e = self.data.find(b'\0', rva, rva + n)
        return self.data[rva:e].decode('latin-1') if e >= 0 else None

    def file_version(self):
        """'a.b.c.d' from VS_FIXEDFILEINFO in .rsrc, or None."""
        a, n = self.secs.get('.rsrc', (0, 0))
        i = self.data.find(b'\xbd\x04\xef\xfe', a, a + n)
        if i < 0:
            return None
        ms, ls = struct.unpack_from('<II', self.data, i + 8)
        return '%d.%d.%d.%d' % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)

    def vtable(self, mangled):
        """RVA of the one vtable of an MSVC class (complete object locator with offset 0), or None.
        Same rule as VtableByRtti in the DLL."""
        if mangled in self._vt:
            return self._vt[mangled]
        out = None
        da, dn = self.secs['.data']
        ra, rn = self.secs['.rdata']
        name = mangled.encode('ascii') + b'\0'
        i = self.data.find(name, da, da + dn)
        if i >= 0:
            td = struct.pack('<I', i - 0x10)
            found = []
            j = self.data.find(td, ra, ra + rn)
            while j >= 0:
                col = j - 12
                if col % 4 == 0 and col >= ra:
                    sig_, off, _cd, _td, _cls, me = struct.unpack_from('<IIIIII', self.data, col)
                    if sig_ == 1 and off == 0 and me == col:
                        ptr = struct.pack('<Q', self.base + col)
                        k = self.data.find(ptr, ra, ra + rn)
                        while k >= 0:
                            if k % 8 == 0:
                                found.append(k + 8)
                            k = self.data.find(ptr, k + 1, ra + rn)
                j = self.data.find(td, j + 1, ra + rn)
            out = found[0] if len(found) == 1 else None
        self._vt[mangled] = out
        return out

    def slot(self, vt, i):
        """RVA of the function in vtable slot i."""
        return self.q(vt + 8 * i) - self.base


class Result:
    def __init__(self, sig):
        self.sig = sig
        self.ok = False
        self.why = None          # reason when not ok
        self.hit = None          # Hit (pattern sigs)
        self.addr = None         # RVA of the match (or of the vtable for rtti sigs)
        self.values = {}         # field -> value (RVAs for rel/site fields)
        self.warnings = []

    def get(self, f=None):
        return self.addr if f is None else self.values.get(f)


def _ref(results, ref):
    """'sig' -> its address, 'sig.field' -> that field's value; None if unresolved."""
    n, _, f = ref.partition('.')
    r = results.get(n)
    if not r or not r.ok:
        return None
    return r.get(f or None)


def resolve(img, manifest=None, only=None):
    """Find every sig in img. Returns {name: Result}; addresses are RVAs."""
    m = manifest or load()
    out = {}

    def go(name, stack=()):
        if name in out:
            return out[name]
        s = m.sig(name)
        if name in stack:
            raise SigError('dependency loop at %s' % name)
        for dep in s.deps():
            go(dep, stack + (name,))
        out[name] = r = Result(s)
        try:
            _resolve_one(img, s, r, out)
            r.ok = True
        except SigError as e:
            r.why = str(e)
        return r

    for name in (only or m.sigs):
        go(name)
    return out


def _resolve_one(img, s, r, results):
    k = s.kind
    w = s.where
    if k == 'rtti':
        vt = img.vtable(w['rtti'])
        if vt is None:
            raise SigError('%s: vtable of %s not found (or not unique)' % (s.name, w['rtti']))
        r.addr = vt
        for f, i in s.d.get('slots', {}).items():
            v = img.slot(vt, i)
            if img.in_sec(v, '.text'):
                r.values[f] = v
            elif hooked(img, vt, i):
                r.warnings.append('slot %d (%s) is hooked (points outside the game code: our DLL while the mod runs?)'
                                  % (i, f))
            else:
                raise SigError('%s: slot %d is not code' % (s.name, i))
        return
    if k == 'vslot':
        vname, idx = w['vslot']
        vt = _ref(results, vname)
        if vt is None:
            raise SigError('%s: needs %s, which was not found' % (s.name, vname))
        if hooked(img, vt, idx):           # replaced by a hook: find the original function by its pattern instead
            a, n = img.sec('.text')
            hit = scan(s, img.data, 0, a, a + n)
            r.warnings.append('vtable slot %d of %s is hooked; found by scanning the code instead' % (idx, vname))
        else:
            hit = match_at(s, img.data, 0, img.slot(vt, idx))
    elif k == 'target':
        t = _ref(results, w['target'])
        if t is None:
            raise SigError('%s: needs %s, which was not found' % (s.name, w['target']))
        hit = match_at(s, img.data, 0, t)
    elif k == 'scan_from':
        t = _ref(results, w['scan_from'])
        if t is None:
            raise SigError('%s: needs %s, which was not found' % (s.name, w['scan_from']))
        hit = scan(s, img.data, 0, t, t + w.get('range', 0x400))
    else:
        a, n = img.sec(w.get('section', '.text'))
        hit = scan(s, img.data, 0, a, a + n)
    r.hit, r.addr = hit, hit.addr
    for f, spec in hit.variant.fields.items():
        r.values[f] = v = hit.value(f)
        why = hit.check(f)
        if why:
            if spec.get('soft'):
                r.warnings.append(why + ' (the code reads it from the game, so this is only a note)')
            else:
                raise SigError(why)
        if spec['kind'] == 'rel':
            want = spec.get('to', 'text')
            sec = {'text': '.text', 'rdata': '.rdata', 'data': '.data'}.get(want)
            if sec and not img.in_sec(v, sec) and not (want == 'data' and img.in_sec(v, '.rdata')):
                raise SigError('%s.%s points to %#x, not into %s' % (s.name, f, v, sec))
        if 'expect_str' in spec:
            got = img.cstr(v)
            if got != spec['expect_str']:
                raise SigError('%s.%s points to %r, expected %r' % (s.name, f, got, spec['expect_str']))
        if 'same_as' in spec:
            n, _, of = spec['same_as'].partition('.')
            o = r.values.get(of) if n == s.name else _ref(results, spec['same_as'])
            if o is not None and o != v:
                raise SigError('%s.%s is %#x but %s is %#x' % (s.name, f, v, spec['same_as'], o))
    for c in s.checks:
        if 'vslot_is' in c:        # the match is the function in a vtable slot (vtable from an rtti sig)
            vname, idx = c['vslot_is']
            vt = _ref(results, vname)
            if vt is not None and not hooked(img, vt, idx) and img.slot(vt, idx) != r.addr:
                raise SigError('%s is not slot %d of %s' % (s.name, idx, vname))


def hooked(img, vt, i):
    """The vtable slot points outside this module (a hook, e.g. bin/san14_overlay.dll in a running game)."""
    q = img.q(vt + 8 * i)
    return not img.base <= q < img.base + img.size_of_image


def features(results, manifest=None):
    """{feature: (ok, [reasons])}. A feature is off when one of its required sigs is not found."""
    m = manifest or load()
    out = {}
    for fid in m.features:
        bad, notes = [], []
        for name, s in m.sigs.items():
            if fid not in s.features:
                continue
            r = results.get(name)
            if r is None:
                continue
            if not r.ok:
                (notes if s.optional_for(fid) else bad).append('%s: %s' % (name, r.why))
            notes += ['%s: %s' % (name, w) for w in r.warnings]
        out[fid] = (not bad, bad + notes)
    return out
