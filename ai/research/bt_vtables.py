"""Vtables of the built-in ktgl behaviour-tree nodes and of chosen game nodes, with their slots.

  python -P ai/research/bt_vtables.py                     # built-in nodes
  python -P ai/research/bt_vtables.py ExecuteInterceptNode # also this game node (class name part)

Uses the tree loader switch and the game factory the same way make_node_types.py does.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import make_node_types as M  # noqa: E402


def cases():
    fac = M.sig_addr('bt_factory')          # sigs/sigs.json
    ld = M.sig_addr('bt_loader_switch')
    out = {}
    for key, (lo, hi) in (('game', M.func_bounds(fac)), ('builtin', (ld, M.func_bounds(ld)[1]))):
        cs = M.switch_cases(lo, hi)
        freq = {}
        for t in cs.values():
            t = M.case_start(t)
            for i in M.CS.disasm(M.IMG[t:t + 0x60], t):
                if i.mnemonic == 'call' and i.op_str.startswith('0x'):
                    c = int(i.op_str, 16)
                    freq[c] = freq.get(c, 0) + 1
                if i.mnemonic == 'jmp':
                    break
        plumbing = {c for c, k in freq.items() if k > len(cs) // 3}
        for h, t in cs.items():
            t2 = M.case_start(t)
            calls = []
            for ins in M.CS.disasm(M.IMG[t2:t2 + 0x80], t2):
                if ins.mnemonic == 'call' and ins.op_str.startswith('0x'):
                    calls.append(int(ins.op_str, 16))
                if ins.mnemonic == 'jmp':
                    break
            vt = None
            for c in [c for c in calls if c not in plumbing]:
                vt = M.last_vtable(c)
                if vt:
                    break
            vt = vt or M.last_vtable(t2, 0x80)
            out[(key, h)] = (vt, M.vtable_name(vt) if vt else None, t2, calls)
    return out


def slots(vt, n=24):
    res = []
    for i in range(n):
        q = M.struct.unpack_from('<Q', M.IMG, vt + 8 * i)[0] - M.RB
        if not M.TEXT0 <= q < M.TEXT0 + M.TEXTN:
            break
        ins = list(M.CS.disasm(M.IMG[q:q + 40], q))[:3]
        res.append((i, q, ' ; '.join(f'{x.mnemonic} {x.op_str}' for x in ins)))
    return res


if __name__ == '__main__':
    want = sys.argv[1:]
    for (key, h), (vt, name, start, calls) in sorted(cases().items(), key=lambda kv: str(kv[1][1])):
        if key == 'builtin' or any(w in str(name) for w in want):
            print(f'== {key} {h:08x} {name} vtable {vt:#x} case {start:#x} ctor-calls {[hex(c) for c in calls]}' if vt
                  else f'== {key} {h:08x} {name} (no vtable) case {start:#x}')
            if vt:
                for i, q, b in slots(vt):
                    print(f'   [{i:2}] {q:x}: {b}')
