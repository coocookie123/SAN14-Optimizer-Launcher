"""Per info-node, per index: what the case reads, event classes that read the same thing, and tree usage.
usage: python -P summ.py > summ.txt"""
import collections
import glob
import json
import re
import struct

import capstone

IMG = open(__import__('os').path.join(__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__)))), 'analysis', 'dump_1793841123', 'image.bin'), 'rb').read()
CS = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
EV = json.load(open('evindex.json'))
EF = json.load(open('evfields.json'))
PLUMB = {0x113a3a0, 0xb2ae60, 0xb2acf0, 0xb2b380, 0xb2b590, 0x332410, 0x331820, 0x1a270}

NODES = [  # label, execute rva, tree node name, index arg position, object regex for hints
    ('person', 0x8f350, 'VariableGetPersonInfoNode', 1, 'Person'),
    ('foothold', 0x8acd0, 'VariableGetFootholdInfoNode', 1, 'Foothold|City'),
    ('district', 0x882c0, 'VariableGetDistrictInfoNode', 1, 'District|Force'),
    ('mydistrict', 0x3c030, 'GetMyselfDistrictInfoNode', 0, 'District|Force'),
    ('force', 0x8c620, 'VariableGetForceInfoNode', 1, 'Force'),
    ('myforce', 0x56690, 'GetMyselfForceInfoNode', 0, 'Force'),
    ('army', 0x86d10, 'VariableGetArmyInfoNode', 1, 'Army|Unit'),
    ('troops', 0x8fc50, 'VariableGetTroopsInfoNode', 1, 'Army|Unit|Troop'),
    ('area', 0x86260, 'VariableGetAreaInfoNode', 1, 'Area|Hex'),
    ('gamedata', 0x8e6b0, 'VariableGetGameDataNode', 0, ''),
    ('caution_troops', 0x87e20, 'VariableGetCautionTroopsInfoNode', 1, ''),
    ('hex', 0x8e880, 'VariableGetHexInfoNode', 1, 'Hex|Area'),
    ('formation', 0x8e580, 'VariableGetFormationInfoNode', 1, ''),
]


def find_switches(fn, limit=0x900):
    ins = list(CS.disasm(IMG[fn:fn + limit], fn))
    out = []
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
                out.append((tbl, cnt))
    return out


def case_keys(a, maxn=40):
    """Follow a case; return list of keys: f/b/w/d<off>, call:<rva>, v<off>, list:<n>, const:<n>."""
    keys = []
    seen = set()
    n = 0
    last_edx = None
    while n < maxn and a not in seen:
        seen.add(a)
        ins = next(CS.disasm(IMG[a:a + 16], a), None)
        if ins is None:
            break
        n += 1
        mn, op = ins.mnemonic, ins.op_str
        if mn == 'jmp' and op.startswith('0x'):
            t = int(op, 16)
            # stop at common tails: a jump backwards into the store block
            if any(k.startswith(('b', 'w', 'd', 'call', 'v', 'const')) for k in keys) and not any(k.startswith('list') for k in keys):
                break
            if t < a and any(k.startswith('list') for k in keys):
                break
            a = t
            continue
        if mn in ('ret', 'int3'):
            break
        m = re.match(r'(?:movzx|movsx|movsxd|mov) e?[a-z0-9]+d?, (byte|word|dword) ptr \[r(?:ax|cx|dx|bx|si|di|8|9|1[0-5])\w* \+ (0x[0-9a-f]+)\]', mn + ' ' + op)
        if m and 'rbp' not in op and 'rsp' not in op and 'rip' not in op and 'r13 +' not in op and 'r14 +' not in op:
            keys.append(f'{m.group(1)[0]}{int(m.group(2), 16):x}')
        elif mn == 'call' and op.startswith('0x'):
            t = int(op, 16)
            if t == 0x1a270 and last_edx is not None:
                keys.append(f'list:{last_edx}')
                maxn += 60
            if t not in PLUMB:
                keys.append(f'call:{t:x}')
        elif mn == 'call' and re.match(r'qword ptr \[r\w+ \+ (0x[0-9a-f]+)\]', op):
            keys.append('v' + format(int(re.search(r'0x[0-9a-f]+', op).group(0), 16), 'x'))
        mm = re.match(r'mov edx, (0x[0-9a-f]+|\d+)$', mn + ' ' + op) if mn == 'mov' else None
        if mm:
            last_edx = int(mm.group(1), 0)
        if mn == 'mov' and re.match(r'ebx, (0x[0-9a-f]+|\d+)$', op):
            keys.append('const:' + op.split(', ')[1])
        a = ins.address + ins.size
    return keys


def hints(keys, rx):
    out = []
    for k in keys:
        if k.startswith('call:'):
            v = EV.get(k[5:], [])
        else:
            v = EF.get(k, [])
        if rx:
            v2 = [x for x in v if re.search(rx, x)]
            v = v2 or v
        if v and len(v) <= 40:
            out.append(f'{k}->' + ','.join(x.replace('SCompare', 'C.').replace('SExec', 'E.').replace('SAct', 'A.') for x in v[:6]) + (f'..({len(v)})' if len(v) > 6 else ''))
    return out


def usage():
    """(node name, idx) -> (count, sample lines with the output var used in IFs)"""
    use = collections.defaultdict(list)
    for f in glob.glob(__import__('os').path.join(__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__))), 'out', 'tree*_*.txt')):
        lines = open(f, encoding='utf-8').read().splitlines()
        for i, l in enumerate(lines):
            m = re.search(r'\] (\w+InfoNode|VariableGetGameDataNode)\(([^)]*)\)', l)
            if not m:
                continue
            args = [x.strip() for x in m.group(2).split(',')]
            use[(m.group(1), args)].append(None) if False else None
            for name, _, _, pos, _ in NODES:
                pass
            use[m.group(1)].append((f, i, args, lines))
    return use


def main():
    use = usage()
    for label, fn, node, pos, rx in NODES:
        sw = find_switches(fn)
        if not sw:
            print(f'##### {label}: no switch found')
            continue
        tbl, cnt = sw[0]
        cases = [struct.unpack_from('<i', IMG, tbl + 4 * k)[0] for k in range(cnt)]
        dflt = collections.Counter(cases).most_common(1)[0]
        print(f'##### {label} ({node}) {cnt} cases')
        per = collections.defaultdict(list)
        for f, i, args, lines in use.get(node, []):
            try:
                idx = int(args[pos])
            except (ValueError, IndexError):
                continue
            out = args[-1]
            ifs = []
            for l in lines[i + 1:i + 25]:
                if out in l and (' IF ' in l or 'Node(' in l) and 'InfoNode' not in l:
                    ifs.append(re.sub(r'^\s*', '', l)[:110])
                if len(ifs) >= 2:
                    break
            per[idx].append((f.split('\\')[-1][:5] + ':' + str(i), ifs))
        for k, c in enumerate(cases):
            if c == dflt[0] and dflt[1] > 2:
                continue
            if not per.get(k):
                continue
            keys = case_keys(c)
            u = per.get(k, [])
            samp = []
            for where, ifs in u:
                for s in ifs:
                    if s not in samp:
                        samp.append(s)
                if len(samp) >= 3:
                    break
            print(f'  [{k}] used {len(u)}x  keys {keys[:9]}')
            for h in hints([x for x in keys[:9] if x not in ('d40',)], rx):
                print(f'        hint {h}')
            for s in samp[:3]:
                print(f'        use  {s}')


main()
