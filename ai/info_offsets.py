"""Which structure field every behaviour-tree info node index reads, taken from the game code (prototype).

  python -P ai/info_offsets.py                         # newest dump: extract, compare with info_names.json
  set SAN14_DUMP=analysis\\dump_<...>  (then the same)  # another dump
  python -P ai/info_offsets.py --diff old.json new.json   # what moved between two versions

The info nodes (VariableGetPersonInfoNode(object, index, out) and friends) switch on the index in their
execute function (vtable slot 16): every case reads one field or calls one getter. The execute function is
found without addresses: node factory (sig bt_factory in sigs/sigs.json) -> constructor -> vtable -> slot
16, as ai/make_node_types.py does. For every case the field reads are listed as keys: b/w/d + offset
(byte / word / dword read, e.g. w11a = word at +0x11a), v<off> for a virtual call, call:<rva> for a getter.

Output: ai/out/info_offsets_<version>.json, and how many of the offsets written in info_names.json
("依据": "word +0x11a ...") the code confirms. After a game update, run it on the old and the new dump and
--diff the two files: the indexes whose field offset changed are listed (old -> new), which is what the
offsets in info_names.json and in ai/gamedata.py / ai/observe.py would need.
"""
import collections
import json
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'research'))
import make_node_types as M  # noqa: E402  (loads the image)
import bt_vtables as BV  # noqa: E402

CS = M.CS
FIELD_RX = re.compile(r'(?:movzx|movsx|movsxd|mov) e?[a-z0-9]+d?, (byte|word|dword) ptr \[r(?:ax|cx|dx|bx|si|di|8|9|1[0-2]|15)\w* \+ (0x[0-9a-f]+)\]')


def find_switch(fn, limit=0x900):
    """(jump table RVA, case count) of the first `jmp reg` switch in fn, or None."""
    ins = list(CS.disasm(M.IMG[fn:fn + limit], fn))
    for k, i in enumerate(ins):
        if i.mnemonic == 'int3' and k and ins[k - 1].mnemonic == 'int3':
            break
        if i.mnemonic == 'jmp' and re.fullmatch(r'r\w+', i.op_str):
            cnt = tbl = None
            for p in ins[max(0, k - 10):k][::-1]:
                m = re.search(r'\*4 \+ 0x([0-9a-f]+)\]', p.op_str)
                if p.mnemonic == 'mov' and m and tbl is None:
                    tbl = int(m.group(1), 16)
                if p.mnemonic == 'cmp' and cnt is None:
                    mm = re.search(r', (0x[0-9a-f]+|\d+)$', p.op_str)
                    if mm:
                        cnt = int(mm.group(1), 0) + 1
            if tbl and cnt:
                return tbl, cnt
    return None


def getter_field(fn):
    """'w2e' if fn is a small getter that returns one field of its argument (mov(zx) eax,[rcx+2Eh]; ret)."""
    reads = []
    for i in CS.disasm(M.IMG[fn:fn + 48], fn):
        m = FIELD_RX.match(i.mnemonic + ' ' + i.op_str)
        if m and '[rcx' in i.op_str:
            reads.append('%s%x' % (m.group(1)[0], int(m.group(2), 16)))
        if i.mnemonic == 'ret':
            return reads[0] if len(reads) == 1 else None
        if i.mnemonic in ('call', 'jmp', 'int3') or i.address - fn > 40:
            return None
    return None


def case_keys(a, maxn=40):
    """Keys of one case: field reads (b/w/d<off>), virtual calls (v<off>), getters (call:<rva>)."""
    keys, seen, n = [], set(), 0
    while n < maxn and a not in seen:
        seen.add(a)
        ins = next(CS.disasm(M.IMG[a:a + 16], a), None)
        if ins is None:
            break
        n += 1
        mn, op = ins.mnemonic, ins.op_str
        if mn == 'jmp' and op.startswith('0x'):
            if keys:
                break                       # the common tail that stores the result
            a = int(op, 16)
            continue
        if mn in ('ret', 'int3'):
            break
        m = FIELD_RX.match(mn + ' ' + op)
        if m:
            keys.append('%s%x' % (m.group(1)[0], int(m.group(2), 16)))
        elif mn == 'call' and op.startswith('0x'):
            keys.append('call:' + op[2:])
            g = getter_field(int(op, 16))
            if g:
                keys.append(g)
        elif mn == 'call' and re.match(r'qword ptr \[r\w+ \+ (0x[0-9a-f]+)\]', op):
            keys.append('v' + format(int(re.search(r'0x[0-9a-f]+', op).group(0), 16), 'x'))
        a = ins.address + ins.size
    return keys


def execute_functions():
    """{node class name without the leading C: execute function RVA} for every *InfoNode / GameDataNode."""
    out = {}
    for (_key, _h), (vt, name, _t, _calls) in BV.cases().items():
        if vt and name and (name.endswith('InfoNode') or name.endswith('GameDataNode')):
            out[name[1:] if name.startswith('C') else name] = struct.unpack_from('<Q', M.IMG, vt + 8 * 16)[0] - M.RB
    return out


def extract():
    res = {}
    for node, fn in sorted(execute_functions().items()):
        sw = find_switch(fn)
        if not sw:
            continue
        tbl, cnt = sw
        cases = [struct.unpack_from('<i', M.IMG, tbl + 4 * k)[0] for k in range(cnt)]
        dflt = collections.Counter(cases).most_common(1)[0][0]
        res[node] = {'execute': '%x' % fn, 'cases': {str(k): case_keys(c) for k, c in enumerate(cases) if c != dflt}}
    return res


def fields(keys):
    return [k for k in keys if re.fullmatch(r'[bwd][0-9a-f]+', k)]


def check(res):
    """Offsets written in info_names.json ("byte/word/dword +0x...") that the code confirms."""
    names = json.load(open(os.path.join(HERE, 'info_names.json'), encoding='utf-8'))
    ok, bad = [], []
    for node, entries in names.items():
        if node.startswith('_') or node not in res:
            continue
        for idx, (label, why) in entries.items():
            m = re.search(r'(byte|word|dword) \+0x([0-9a-f]+)', why)
            if not m:
                continue
            want = '%s%x' % (m.group(1)[0], int(m.group(2), 16))
            got = fields(res[node]['cases'].get(idx, []))
            (ok if want in got else bad).append((node, idx, label, want, got))
    return ok, bad


def diff(a, b):
    """Changed field reads per node / index between two extraction files."""
    out = []
    for node in sorted(set(a['nodes']) | set(b['nodes'])):
        ca = a['nodes'].get(node, {}).get('cases', {})
        cb = b['nodes'].get(node, {}).get('cases', {})
        for idx in sorted(set(ca) | set(cb), key=int):
            fa, fb = fields(ca.get(idx, [])), fields(cb.get(idx, []))
            if fa != fb:
                out.append((node, idx, fa, fb))
    return out


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if sys.argv[1:2] == ['--diff']:
        a, b = (json.load(open(p, encoding='utf-8')) for p in sys.argv[2:4])
        changes = diff(a, b)
        print('%s -> %s: %d 个编号读的字段变了' % (a.get('version'), b.get('version'), len(changes)))
        for node, idx, fa, fb in changes:
            print('  %-34s [%s] %s -> %s' % (node, idx, ','.join(fa) or '-', ','.join(fb) or '-'))
        return
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'tools'))
    import sigs as S
    img = S.Image(M.IMG, M.RB)
    ver = img.file_version() or '?'
    res = extract()
    out = os.path.join(HERE, 'out', 'info_offsets_%s.json' % ver)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump({'version': ver, 'nodes': res}, f, indent=1, ensure_ascii=False)
    n_cases = sum(len(v['cases']) for v in res.values())
    n_fields = sum(1 for v in res.values() for k in v['cases'].values() if fields(k))
    print('版本 %s：%d 种读属性节点，%d 个编号，其中 %d 个直接读字段；写到 %s' % (
        ver, len(res), n_cases, n_fields, os.path.relpath(out, os.path.dirname(HERE))))
    ok, bad = check(res)
    print('info_names.json 里写了字段偏移的 %d 条，代码里对得上 %d 条' % (len(ok) + len(bad), len(ok)))
    for node, idx, label, want, got in bad:
        print('  对不上：%s [%s] %s 写的是 %s，代码读 %s' % (node, idx, label, want, ','.join(got) or '（不直接读字段）'))


if __name__ == '__main__':
    main()
