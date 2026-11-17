"""What each argument of a game node is for, read from its code (1.0.2.58226 memory image).

  python -P ai/research/node_args.py CommandExecutionMoveNode [more node names ...]

SetParam (vtable slot 17) copies the file's u32 args into fields of the node object. Execute (slot 16)
then uses each field: as a blackboard key it reads an object (b2ae60), reads an int (b2acf0), writes an
int (b2b380) or writes an object (b2b590); otherwise the field is a plain number (enum or constant).
"""
import os
import re
import sys

import capstone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.argv, _args = sys.argv[:1], sys.argv[1:]
import make_node_types as M  # noqa: E402
import rtti  # noqa: E402

CS = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
IMG = rtti.IMG
BB = {0xb2ae60: 'obj in', 0xb2acf0: 'int in', 0xb2b380: 'int OUT', 0xb2b590: 'obj OUT',
      0xb2adb0: 'by name'}


def setparam_fields(fn):
    """[(arg index, field offset)] from `mov eax, [rdx+4k]; mov [rcx+off], eax` pairs (also via r8 etc.)."""
    out = []
    last = {}
    for i in CS.disasm(IMG[fn:fn + 0x400], fn):
        m = re.match(r'(e\w+|r\w+d), dword ptr \[rdx(?: \+ (0x[0-9a-f]+|\d+))?\]$', i.op_str)
        if i.mnemonic == 'mov' and m:
            last[m.group(1)] = int(m.group(2) or '0', 0) // 4
            continue
        m = re.match(r'dword ptr \[rcx \+ (0x[0-9a-f]+)\], (e\w+|r\w+d)$', i.op_str)
        if i.mnemonic == 'mov' and m and m.group(2) in last:
            out.append((last.pop(m.group(2)), int(m.group(1), 16)))
            continue
        if i.mnemonic in ('ret', 'int3') or (i.mnemonic == 'jmp' and not i.op_str.startswith('0x')):
            break
    return out


def field_uses(fn, end, fields):
    """For each field offset: how Execute uses it."""
    uses = {off: set() for off in fields}
    ins = list(CS.disasm(IMG[fn:end], fn))
    for k, i in enumerate(ins):
        m = re.search(r'dword ptr \[(r\w+) \+ (0x[0-9a-f]+)\]', i.op_str)
        if not m:
            continue
        off = int(m.group(2), 16)
        if off not in uses:
            continue
        dst = i.op_str.split(',')[0].strip()
        # follow to the next call that takes it in edx (blackboard key)
        kind = None
        if i.mnemonic == 'mov' and dst in ('edx', 'r8d', 'ecx', 'r9d'):
            for j in ins[k + 1:k + 12]:
                if j.mnemonic == 'call' and j.op_str.startswith('0x'):
                    t = int(j.op_str, 16)
                    kind = BB.get(t, f'arg to {t:x}') if dst == 'edx' or t not in BB else None
                    break
        if kind is None:
            kind = 'cmp' if i.mnemonic == 'cmp' else 'number'
        uses[off].add(kind)
    return uses


def describe(name):
    vts = rtti.vtables('C' + name)
    if not vts:
        return f'{name}: no vtable'
    sl = rtti.slots(vts[0][0])
    sp, ex = sl[17], sl[16]
    b = M.func_bounds(ex)
    fields = setparam_fields(sp)
    uses = field_uses(ex, b[1] if b else ex + 0x400, [o for _, o in fields])
    calls = sorted({int(i.op_str, 16) for i in CS.disasm(IMG[ex:b[1] if b else ex + 0x400], ex)
                    if i.mnemonic == 'call' and i.op_str.startswith('0x')} - set(BB) - {0x113a3a0, 0x332410})
    lines = [f'{name}: execute {ex:x} ({(b[1] - b[0]) if b else "?"} bytes), setparam {sp:x}, {len(fields)} args']
    for k, off in fields:
        lines.append(f'   arg {k}: +{off:#x} {", ".join(sorted(uses.get(off, []))) or "unused?"}')
    lines.append('   calls ' + ' '.join(f'{c:x}' for c in calls[:16]))
    return '\n'.join(lines)


if __name__ == '__main__':
    for n in _args:
        print(describe(n))
