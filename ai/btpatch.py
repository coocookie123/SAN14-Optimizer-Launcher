"""Build a modified 0000/bt_exce.bin from behaviour modules (ai/modules/*.json). The game file is only read;
the result goes to ai/build/.

  python -P ai/btpatch.py ai/modules/a.json [ai/modules/b.json ...]    # -> ai/build/a+b.bin
  python -P ai/btpatch.py --check ai/modules/a.json ...                # only check that they apply
  python -P ai/btpatch.py --show ai/modules/a.json ...                 # print every changed spot

Module format (JSON):
  {"id": "formation_mix", "name": "...", "description": "...", "priority": 50,
   "ops": [ op, ... ]}

Ops (all positions are resolved on the official tree before anything changes):
  {"op": "set_arg", "at": ANCHOR, "arg": 1, "expect": 1, "value": 2}
  {"op": "insert",  "at": ANCHOR (the parent), "pos": "first" | "last" | n, "src": TEXT}
  {"op": "before" | "after", "at": ANCHOR (a sibling), "src": TEXT}
  {"op": "wrap",    "at": ANCHOR, "src": "IF ..."}     the node becomes the only child of this decorator
  {"op": "replace", "at": ANCHOR, "src": TEXT}
  {"op": "remove",  "at": ANCHOR}
TEXT is tree source in the notation of ai/out/*.txt (see btsrc.py); several top-level lines insert several
nodes. "src" may also be a list of lines.

ANCHOR picks nodes:
  {"tree": 1, "index": 1715, "type": "SelectPersonTacticsNode"}     one node by pre-order index (type checked)
  {"tree": 1, "find": "SelectPersonTacticsNode(", "within": "LINK($db6b16c3)", "count": 3}
      every node whose text (as in ai/out, without the [n]) contains `find`, below a node whose text
      contains `within` (optional). "re:" before either makes it a regular expression. `count` is the
      number of matches expected (default 1; "any" = at least one); a different number stops the build,
      so a game update that moved things is noticed instead of patched blindly.

Rules that keep modules independent:
  * New variables are written $m.<module id>.<name>; their keys must not collide with any key or value the
    official tree uses, nor with another module's variable. ($shared.<name> may be used by several.)
  * Two modules may not replace, remove or set the same argument of the same node, nor change a node that
    another module replaces or removes. Wrapping the same node and inserting next to the same node are
    allowed; the order then follows priority (higher first, ties by module id).
  * Inserts by several modules at one place keep that order, so in a "first that succeeds" SEQ(0, 0, 1)
    the higher-priority module is tried first.

The old format {"edits": [{"tree", "node", "type", "arg", "expect", "value"}]} still works.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import btdump  # noqa: E402
import btfile  # noqa: E402
import btsrc  # noqa: E402

BUILD = os.path.join(HERE, 'build')


class PatchError(Exception):
    pass


def u32(v):
    return v & 0xFFFFFFFF


def load_module(path):
    with open(path, encoding='utf-8') as f:
        m = json.load(f)
    m.setdefault('id', os.path.splitext(os.path.basename(path))[0])
    m.setdefault('name', m['id'])
    m.setdefault('priority', 0)
    ops = list(m.get('ops', []))
    for e in m.get('edits', []):        # old format
        ops.append({'op': 'set_arg', 'at': {'tree': e['tree'], 'index': e['node'], 'type': e.get('type')},
                    'arg': e['arg'], 'expect': e['expect'], 'value': e['value']})
    m['ops'] = ops
    m['path'] = path
    return m



def dll_features(module_paths):
    """san14_ai.dll features the modules switch on (module key "dll"), in order, no repeats.
    The launcher passes them in SAN14_AI_FEATURES; a module may have only these and no tree ops."""
    out = []
    for p in module_paths:
        for f in load_module(p).get('dll', []):
            if f not in out:
                out.append(f)
    return out

# ------------------------------------------------------------------ anchors

def parents_of(k):
    par = {}
    for n in k.nodes:
        for c in n.children:
            par[id(c)] = n
    return par


def text_match(pattern, text):
    if pattern.startswith('re:'):
        return re.search(pattern[3:], text) is not None
    return pattern in text


def resolve(bt, anchor, where):
    t = anchor.get('tree')
    try:
        k = bt.trees[t].ktbt
    except (IndexError, TypeError):
        raise PatchError(f'{where}: no tree {t}')
    if 'index' in anchor:
        i = anchor['index']
        if not 0 <= i < len(k.nodes):
            raise PatchError(f'{where}: tree {t} has no node {i}')
        n = k.nodes[i]
        if anchor.get('type') and btdump.type_name(n) != anchor['type']:
            raise PatchError(f'{where}: node {i} is {btdump.type_name(n)}, expected {anchor["type"]}')
        return t, [n]
    find, within = anchor.get('find'), anchor.get('within')
    if not find:
        raise PatchError(f'{where}: anchor needs "index" or "find"')
    par = parents_of(k)
    hits = []
    for n in k.nodes:
        if not text_match(find, btsrc.node_text(n)):
            continue
        if within:
            p = par.get(id(n))
            while p is not None and not text_match(within, btsrc.node_text(p)):
                p = par.get(id(p))
            if p is None:
                continue
        hits.append(n)
    want = anchor.get('count', 1)
    if want == 'any':
        if not hits:
            raise PatchError(f'{where}: nothing matches {find!r}' + (f' within {within!r}' if within else ''))
    elif len(hits) != want:
        raise PatchError(f'{where}: {len(hits)} nodes match {find!r}' + (f' within {within!r}' if within else '')
                         + f', expected {want}')
    return t, hits


# ------------------------------------------------------------------ build

def src_text(op):
    s = op.get('src', '')
    return '\n'.join(s) if isinstance(s, list) else s


# A/B mode: inserted logic only runs for forces with an odd (or even) id, so one game compares
# forces with and without the modules. The getter gives "my force" in each tree's context.
AB_FORCE = {0: 'GetMyselfForceNode($m.{id}.abf)', 1: 'GetMyselfForceNode($m.{id}.abf)',
            2: 'VariableGetTroopsInfoNode($88dbc001, 15, $m.{id}.abf)',
            3: 'VariableGetArmyInfoNode($06dc5c3d, 5, $m.{id}.abf)'}


def ab_split(ab):
    """'odd' / 'even' -> (side, None): every module; 'odd:defend+rally' -> (side, {'defend', 'rally'}):
    only those module ids (an empty list after the colon: none). None -> (None, None)."""
    if not ab:
        return None, None
    side, sep, ids = ab.partition(':')
    return side, ({i for i in re.split(r'[+,]', ids) if i} if sep else None)


def ab_dll(ab, module_paths):
    """SAN14_AI_AB for san14_ai.dll: the side, plus the DLL features of the limited modules when only some are."""
    side, ids = ab_split(ab)
    if not side or ids is None:
        return side
    feats = dll_features([p for p in module_paths if load_module(p)['id'] in ids])
    return side + ':' + '+'.join(feats)


def ab_gate(text, tree, mid, ab):
    """Wrap inserted tree source so it only runs for odd/even force ids; the wrapper always succeeds."""
    def v(n):
        return '$m.%s.%s' % (mid, n)
    lines = [ln for ln in text.split('\n') if ln.strip()]
    roots = [ln for ln in lines if not ln.startswith(' ')]
    body = lines if len(roots) == 1 else ['SEQ(0, 2, 1)'] + ['  ' + ln for ln in lines]
    cond = ('IF %s >= 1' if ab == 'odd' else 'IF NOT %s >= 1') % v('abid')
    head = ['SEQ(0, 0, 1)', '  SEQ(0, 1, 1)', '    ' + AB_FORCE[tree].format(id=mid),
            '    VariableGetForceInfoNode(%s, 0, %s)' % (v('abf'), v('abid')),
            '    VariableSetVariableSpecifiedTypeNode(14, %s, %s)' % (v('abh'), v('abid')),
            '    VariableDivideValueNode(%s, 2)' % v('abh'), '    VariableMultiValueNode(%s, 2)' % v('abh'),
            '    VariableMinusVariableNode(%s, %s)' % (v('abid'), v('abh')), '    ' + cond]
    return '\n'.join(head + ['      ' + ln for ln in body] + ['  MiscDummyNode(1)'])


def module_vars(m, text):
    # $xxxxxxxx (exactly 8 hex digits) is an existing key, not a new name
    names = set(re.findall(r'\$(?![0-9a-fA-F]{8}(?![\w.]))([A-Za-z_][\w.]*)', text))
    for name in names:
        if not (name.startswith(f'm.{m["id"]}.') or name.startswith('shared.')):
            raise PatchError(f'{m["id"]}: variable ${name} must be named $m.{m["id"]}.<name> or $shared.<name>')
    return names


def official_values(bt):
    vals = set()
    for t in bt.trees:
        for n in t.ktbt.nodes:
            vals.update(n.args)
            if n.args2:
                vals.update(n.args2)
    return vals


def build_tree(module_paths, source=None, ab=None):
    """Apply modules to the official tree (source: its path, default the Steam install). Returns (BTFile, report)."""
    bt = btfile.load(source or btdump.GAME_BT)
    mods = sorted((load_module(p) for p in module_paths), key=lambda m: (-m['priority'], m['id']))
    known = official_values(bt)
    keys = {}                         # var key -> name
    claims = {}                       # id(node) -> [(module id, kind, arg)]
    plans = []                        # (module, op, tree, [nodes], parsed src)
    report = []
    # 1. resolve everything on the untouched tree
    for m in mods:
        for k, op in enumerate(m['ops']):
            where = f'{m["id"]} op {k} ({op.get("op")})'
            kind = op.get('op')
            if kind not in ('set_arg', 'insert', 'before', 'after', 'wrap', 'replace', 'remove'):
                raise PatchError(f'{where}: unknown op')
            t, nodes = resolve(bt, op.get('at', {}), where)
            text = src_text(op)
            side, only = ab_split(ab)
            if side and (only is None or m['id'] in only) and kind in ('insert', 'before', 'after') and text.strip():
                text = ab_gate(text, op.get('at', {}).get('tree', 1), m['id'], side)
            roots = []
            if kind in ('insert', 'before', 'after', 'wrap', 'replace'):
                if not text.strip():
                    raise PatchError(f'{where}: needs "src"')
                for name in module_vars(m, text):
                    key = btsrc.var_key(name)
                    if key in known:
                        raise PatchError(f'{where}: key of ${name} ({key:08x}) is already used by the game tree')
                    if keys.get(key, name) != name:
                        raise PatchError(f'{where}: ${name} and ${keys[key]} have the same key {key:08x}')
                    keys[key] = name
                try:
                    btsrc.parse(text)            # syntax check now, fresh copies are made per use
                except ValueError as e:
                    raise PatchError(f'{where}: {e}')
                if kind == 'wrap':
                    roots = btsrc.parse(text)
                    if len(roots) != 1 or roots[0].children:
                        raise PatchError(f'{where}: wrap needs exactly one node line')
            for n in nodes:
                if kind == 'set_arg':
                    a = op['arg']
                    if not 0 <= a < len(n.args):
                        raise PatchError(f'{where}: node has {len(n.args)} args')
                    if n.args[a] != u32(op['expect']):
                        raise PatchError(f'{where}: arg {a} is {btsrc.val(n.args[a])}, expected {op["expect"]}')
                claims.setdefault(id(n), []).append((m['id'], kind, op.get('arg')))
            plans.append((m, op, t, nodes, text))
    # 2. conflicts
    exclusive = ('replace', 'remove')
    for nid, cl in claims.items():
        owners = {c[0] for c in cl}
        if len(owners) < 2:
            continue
        kinds = [c[1] for c in cl]
        args = [(c[0], c[2]) for c in cl if c[1] == 'set_arg']
        same_arg = {}
        for mid, a in args:
            same_arg.setdefault(a, set()).add(mid)
        if any(k in exclusive for k in kinds) or any(len(v) > 1 for v in same_arg.values()):
            raise PatchError(f'modules {sorted(owners)} change the same node in conflicting ways ({kinds})')
    # removed / replaced subtrees must not contain nodes other modules touch
    for m, op, t, nodes, text in plans:
        if op['op'] not in exclusive:
            continue
        for n in nodes:
            stack = list(n.children)
            while stack:
                c = stack.pop()
                for mid, kind, _ in claims.get(id(c), []):
                    if mid != m['id']:
                        raise PatchError(f'{m["id"]} {op["op"]}s a subtree that {mid} changes ({kind})')
                stack.extend(c.children)
    # 3. apply (plans are already in priority order)
    par_cache = {}
    first_cursor = {}
    after_of = {}
    top = {}                          # id(node) -> outermost wrapper put around it
    for m, op, t, nodes, text in plans:
        k = bt.trees[t].ktbt
        if t not in par_cache:
            par_cache[t] = parents_of(k)
        par = par_cache[t]
        for n in nodes:
            kind = op['op']
            if kind == 'set_arg':
                n.args[op['arg']] = u32(op['value'])
                report.append(f'{m["id"]}: tree {t} [{n.index}] arg {op["arg"]}: {op["expect"]} -> {op["value"]}')
                continue
            new = btsrc.parse(text) if text.strip() else []
            if kind == 'insert':
                pos = op.get('pos', 'last')
                if pos == 'first':
                    c = first_cursor.get(id(n), 0)
                    n.children[c:c] = new
                    first_cursor[id(n)] = c + len(new)
                elif pos == 'last':
                    n.children.extend(new)
                else:
                    n.children[int(pos):int(pos)] = new
                for x in new:
                    par[id(x)] = n
            elif kind in ('before', 'after', 'replace', 'remove', 'wrap'):
                # once a node is wrapped, its place in the parent belongs to the outermost wrapper
                target = top.get(id(n), n)
                p = par.get(id(target))
                if p is None:
                    raise PatchError(f'{m["id"]}: cannot {kind} the root')
                i = next(j for j, c in enumerate(p.children) if c is target)
                if kind == 'before':
                    p.children[i:i] = new
                elif kind == 'after':
                    # after earlier (higher-priority) inserts at the same spot
                    j = i + 1
                    while j < len(p.children) and after_of.get(id(p.children[j])) is n:
                        j += 1
                    for x in new:
                        after_of[id(x)] = n
                    p.children[j:j] = new
                elif kind == 'replace':
                    p.children[i:i + 1] = new
                elif kind == 'remove':
                    del p.children[i]
                elif kind == 'wrap':
                    w = new[0]
                    w.children = [target]
                    p.children[i] = w
                    par[id(target)] = w
                    top[id(n)] = w
                    new = [w]
                for x in new:
                    par[id(x)] = p
            report.append(f'{m["id"]}: {kind} at tree {t} [{n.index}] {btdump.type_name(n)}'
                          + (f' (+{sum(1 for _ in walk(new))} nodes)' if new else ''))
    return bt, report


def walk(roots):
    stack = list(roots)
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


def build(module_paths, out=None, check_only=False, source=None, ab=None):
    """Returns (output path or None, problems). source: the official 0000/bt_exce.bin to start from."""
    try:
        bt, report = build_tree(module_paths, source, ab)
    except PatchError as e:
        return None, [str(e)]
    for t in bt.trees:
        t.ktbt.renumber()
    data = bt.to_bytes()
    again = btfile.BTFile(data)
    if again.to_bytes() != data:
        return None, ['patched file does not round-trip']
    if check_only:
        return None, []
    os.makedirs(BUILD, exist_ok=True)
    ids = [load_module(p)['id'] for p in module_paths]
    out = out or os.path.join(BUILD, '+'.join(sorted(ids)) + ('.ab_%s' % ab.replace(':', '_').replace('+', '-') if ab else '') + '.bin')
    with open(out, 'wb') as f:
        f.write(data)
    return out, []


def show(module_paths):
    bt, report = build_tree(module_paths)
    for line in report:
        print(line)


def main():
    args = sys.argv[1:]
    check = '--check' in args
    do_show = '--show' in args
    args = [a for a in args if a not in ('--check', '--show')]
    if not args:
        sys.exit(__doc__)
    if do_show:
        try:
            show(args)
        except PatchError as e:
            sys.exit(f'not applied: {e}')
        return
    out, problems = build(args, check_only=check)
    if problems:
        print('not applied:')
        for p in problems:
            print('  ' + p)
        sys.exit(1)
    print('ok' if check else f'wrote {out}')


if __name__ == '__main__':
    main()
