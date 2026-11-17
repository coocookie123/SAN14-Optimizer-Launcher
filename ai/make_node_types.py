"""Build ai/node_types.json: behavior-tree node type hash -> class name.

Reads the decrypted game image made by tools/dump.py (analysis/dump_*/image.bin). Run it again
after a game update:  python -P ai/make_node_types.py

Two places create nodes (RVAs for SAN14CE_SC.exe 1.0.2.58226; found by pattern below):
  * built-in ktgl nodes: a switch on the type hash inside the tree loader (flags bit 0 clear);
  * game nodes (flags bit 0 set): the factory, a switch `cmp edx, hash` with 200+ cases.
For every case we follow the allocation to the constructor and take the last vtable it stores,
then read the class name from the MSVC RTTI behind that vtable.
"""
import glob
import json
import os
import re
import struct
import sys

import capstone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CS = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import sigs as S  # noqa: E402  (patterns: bt_factory, bt_loader_switch, bt_condition_cb in sigs/sigs.json)


def load_image():
    # the newest dump, or the one named by the environment variable SAN14_DUMP (a dump directory)
    d = os.environ.get('SAN14_DUMP') or sorted(glob.glob(os.path.join(ROOT, 'analysis', 'dump_*')))[-1]
    img = open(os.path.join(d, 'image.bin'), 'rb').read()
    meta = json.load(open(os.path.join(d, 'meta.json'), encoding='utf-8'))
    secs = {s['name']: (int(s['rva'], 16), int(s['vsize'], 16)) for s in meta['sections']}
    return img, secs, int(meta['runtime_base'], 16), meta


IMG, SECS, RB, META = load_image()
TEXT0, TEXTN = SECS['.text']
RD0, RDN = SECS['.rdata']


def rtti_name(vt):
    """Class name of the vtable at RVA vt, or None."""
    try:
        col = struct.unpack_from('<Q', IMG, vt - 8)[0] - RB
        if not RD0 <= col < RD0 + RDN:
            return None
        sig, = struct.unpack_from('<I', IMG, col)
        if sig != 1:
            return None
        td = struct.unpack_from('<I', IMG, col + 12)[0]
        e = IMG.find(b'\x00', td + 0x10)
        n = IMG[td + 0x10:e].decode()
        return n if n.startswith('.?AV') else None
    except (struct.error, UnicodeDecodeError):
        return None


def getter_string(fn):
    """String returned by a `lea rax, [rip+X]; ret` getter, or None."""
    ins = list(CS.disasm(IMG[fn:fn + 16], fn))[:2]
    if len(ins) == 2 and ins[0].mnemonic == 'lea' and ins[0].op_str.startswith('rax') and ins[1].mnemonic == 'ret':
        t = rip_target(ins[0])
        if t:
            e = IMG.find(b'\x00', t)
            try:
                v = IMG[t:e].decode('ascii')
            except UnicodeDecodeError:
                return None
            return v if re.fullmatch(r'C[A-Za-z0-9_]+', v) else None
    return None


def vtable_name(vt):
    """Node class name: vtable slot 13 (game nodes) or 12 (built-in ktgl nodes), else RTTI."""
    names = []
    for slot in (13, 12):
        q = struct.unpack_from('<Q', IMG, vt + 8 * slot)[0] - RB
        if TEXT0 <= q < TEXT0 + TEXTN:
            names.append(getter_string(q))
    for n in names:
        if n and n != 'CBehaviorTreeLeafNode':
            return n
    r = rtti_name(vt)
    return short(r) if r else None


def is_vtable(t):
    q = struct.unpack_from('<Q', IMG, t)[0] - RB
    return TEXT0 <= q < TEXT0 + TEXTN and vtable_name(t) is not None


def short(n):
    m = re.match(r'\.\?AV(.+?)@@$', n)
    return m.group(1) if m else n


def rip_target(ins):
    m = re.search(r'\[rip ([+-]) 0x([0-9a-f]+)\]', ins.op_str)
    if not m:
        return None
    d = int(m.group(2), 16)
    return ins.address + ins.size + (d if m.group(1) == '+' else -d)


def node_vtable(t):
    """True if the vtable names itself through slot 13/12 (node classes do; member containers such as
    tlib::ptr_list, whose vtables constructors also write, do not)."""
    for slot in (13, 12):
        q = struct.unpack_from('<Q', IMG, t + 8 * slot)[0] - RB
        if TEXT0 <= q < TEXT0 + TEXTN and getter_string(q) not in (None, 'CBehaviorTreeLeafNode'):
            return True
    return False


def last_vtable(start, limit=0x400, depth=0):
    """Last vtable whose address is loaded in code from start up to ret/jmp; follows calls 2 deep.
    A vtable that names itself (see node_vtable) wins over a later one that does not."""
    found = None
    for ins in CS.disasm(IMG[start:start + limit], start):
        if ins.mnemonic == 'lea':
            t = rip_target(ins)
            if t and RD0 <= t < RD0 + RDN and is_vtable(t):
                if found is None or node_vtable(t) or not node_vtable(found):
                    found = t
        elif ins.mnemonic == 'call' and depth < 2 and ins.op_str.startswith('0x'):
            # base-class constructors run first; the derived vtable is written after them
            pass
        elif ins.mnemonic in ('ret', 'int3'):
            break
        elif ins.mnemonic == 'jmp':
            if ins.op_str.startswith('0x') and depth < 2 and found is None:
                return last_vtable(int(ins.op_str, 16), limit, depth + 1)
            break
    return found


def case_start(target):
    """Built-in cases begin with `test rdi, rdi; je create` (reuse an existing node); skip to create."""
    ins = list(CS.disasm(IMG[target:target + 16], target))[:2]
    if len(ins) == 2 and ins[0].mnemonic == 'test' and ins[1].mnemonic == 'je':
        return int(ins[1].op_str, 16)
    return target


def case_class(target, alloc_fns):
    """Class created by one switch case: allocation, then constructor call (or inline vtable)."""
    target = case_start(target)
    calls = []
    for ins in CS.disasm(IMG[target:target + 0x80], target):
        if ins.mnemonic == 'call' and ins.op_str.startswith('0x'):
            calls.append(int(ins.op_str, 16))
        if ins.mnemonic == 'jmp':
            break
    ctors = [c for c in calls if c not in alloc_fns]
    for c in ctors:
        vt = last_vtable(c)
        if vt:
            return vtable_name(vt)
    vt = last_vtable(target, 0x80)
    return vtable_name(vt) if vt else None


def switch_cases(start, end):
    """{hash: case address} from a `cmp edx, imm32` binary-search switch."""
    ins = list(CS.disasm(IMG[start:end], start))
    cases = {}
    for i, x in enumerate(ins[:-2]):
        if x.mnemonic == 'cmp' and x.op_str.startswith('edx, 0x'):
            h = int(x.op_str.split(', ')[1], 16)
            for k in range(i + 1, min(i + 4, len(ins))):
                n = ins[k]
                if n.mnemonic == 'je':
                    cases[h] = int(n.op_str, 16)
                    break
                if n.mnemonic == 'jne':
                    cases[h] = ins[k + 1].address
                    break
                if n.mnemonic not in ('ja', 'jb', 'jg', 'jl', 'jae', 'jbe'):
                    break
    return cases


SETCC = {'setg': '>', 'setge': '>=', 'setl': '<', 'setle': '<=', 'sete': '==', 'setne': '!='}


def flow(fn, limit=0x400):
    """Instructions of fn reachable through branches (enough for small leaf functions)."""
    seen, work, out = set(), [fn], []
    while work and len(out) < 600:
        a = work.pop()
        if a in seen or not fn <= a < fn + limit:
            continue
        for i in CS.disasm(IMG[a:a + 0x100], a):
            if i.address in seen:
                break
            seen.add(i.address)
            out.append(i)
            if i.mnemonic.startswith('j') and i.op_str.startswith('0x'):
                work.append(int(i.op_str, 16))
                if i.mnemonic == 'jmp':
                    break
            if i.mnemonic == 'ret':
                break
    return sorted(out, key=lambda x: x.address)


def condition_table():
    """Condition function id -> {rva, op?, operands?} from the game's condition callback.

    The callback (`dec ecx; ...; cmp ecx, N; ja; ... jmp rcx`) switches on id - 1. Plain
    comparisons are recognised by shape: context call, one or two blackboard reads, one setcc.
    Example (1.0.2.58226): 18 var > var, 19 var >= var, 20 var > const, 21 var >= const.
    """
    cb = sig_addr('bt_condition_cb')
    if not cb:
        return {}
    ins = list(CS.disasm(IMG[cb:cb + 0x40], cb))
    count = next(int(i.op_str.split(', ')[1], 16) for i in ins if i.mnemonic == 'cmp') + 1
    tbl = next(int(re.search(r'\+ 0x([0-9a-f]+)\]', i.op_str).group(1), 16)
               for i in ins if i.mnemonic == 'mov' and '*4' in i.op_str)
    base = 0  # lea r8, [rip - X] points at the image base
    res = {}
    for fid in range(1, count + 1):
        case = base + struct.unpack_from('<i', IMG, tbl + 4 * (fid - 1))[0]
        fn = None
        for i in CS.disasm(IMG[case:case + 0x30], case):
            if i.mnemonic == 'call' and i.op_str.startswith('0x'):
                fn = int(i.op_str, 16)
                break
            if i.mnemonic in ('ret', 'jmp'):
                break
        entry = {'rva': f'{fn:x}' if fn else f'{case:x}'}
        if fn:
            body = flow(fn)
            calls = [i.op_str for i in body if i.mnemonic == 'call']
            sets = [SETCC[i.mnemonic] for i in body if i.mnemonic in SETCC]
            uniq = sorted(set(calls))
            if len(sets) == 1 and len(uniq) == 2 and len(calls) in (2, 4):
                entry['op'] = sets[0]
                entry['operands'] = 'var,var' if len(calls) == 4 else 'var,const'
        res[str(fid)] = entry
    return res


def find(pattern):
    m = re.search(pattern, IMG[TEXT0:TEXT0 + TEXTN], re.S)
    return TEXT0 + m.start() if m else None


def sig_addr(name):
    """RVA of a sig from sigs/sigs.json in the dumped image, or None (the reason is printed)."""
    try:
        return S.scan(name, IMG, 0, TEXT0, TEXT0 + TEXTN).checked().addr
    except S.SigError as e:
        print(e)
        return None


def func_bounds(rva):
    pd0, pdn = SECS['.pdata']
    best = None
    for i in range(pdn // 12):
        b, e, u = struct.unpack_from('<III', IMG, pd0 + 12 * i)
        if b <= rva < e:
            best = (b, e)
            break
    return best


def main():
    # game factory: first compare is `cmp edx, imm32` right after `mov edi, r9d; mov rsi, r8; xor ebx, ebx`
    fac = sig_addr('bt_factory')
    # tree loader node switch: `mov edx, [rbp-0x6c]; cmp edx, imm32` after the KTBT check
    ld = sig_addr('bt_loader_switch')
    if not fac or not ld:
        sys.exit('pattern not found; the game exe changed (see tools/sigfind.py)')
    fb = func_bounds(fac)
    lb = func_bounds(ld)
    out = {'exe': META.get('exe'), 'exe_mtime': META.get('exe_mtime'), 'game': {}, 'builtin': {}}
    for key, (lo, hi) in (('game', fb), ('builtin', (ld, lb[1]))):
        cases = switch_cases(lo, hi)
        # calls that show up in most cases are allocator plumbing, not constructors
        freq = {}
        for t in cases.values():
            t = case_start(t)
            for i in CS.disasm(IMG[t:t + 0x60], t):
                if i.mnemonic == 'call' and i.op_str.startswith('0x'):
                    c = int(i.op_str, 16)
                    freq[c] = freq.get(c, 0) + 1
                if i.mnemonic == 'jmp':
                    break
        plumbing = {c for c, k in freq.items() if k > len(cases) // 3}
        for h, t in sorted(cases.items()):
            out[key][f'{h:08x}'] = case_class(t, plumbing)
    out['conditions'] = condition_table()
    # two built-in cases do not match the case pattern; their constructors were read by hand
    # (b2bb60 -> CBehaviorTreeRootNode, b314a0 -> CBehaviorTreeParallelNode in 1.0.2.58226)
    for h, n in (('f3836309', 'CBehaviorTreeRootNode'), ('9fb4c384', 'CBehaviorTreeParallelNode')):
        if h in out['builtin'] and not out['builtin'][h]:
            out['builtin'][h] = n
    path = os.path.join(ROOT, 'ai', 'node_types.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    g = out['game']
    print(f'game factory {fb[0]:#x}: {len(g)} types, {sum(1 for v in g.values() if v)} named')
    print(f'loader switch {ld:#x}: {len(out["builtin"])} built-in types')
    for h, n in out['builtin'].items():
        print('  ', h, n)
    print('unnamed game types:', [h for h, n in g.items() if not n])
    print('unnamed built-in types:', [h for h, n in out['builtin'].items() if not n])


if __name__ == '__main__':
    main()
