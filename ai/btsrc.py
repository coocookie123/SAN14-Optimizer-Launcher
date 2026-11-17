"""Behaviour trees as text, both ways: render nodes in the same notation as ai/out/*.txt, and parse that
notation back into nodes. Used for writing new logic in patches and modules.

  python -P ai/btsrc.py --selftest      # render every tree of bt_exce.bin, parse it back, compare

Notation (one node per line, two spaces of indent per level; an optional "[n] " prefix is ignored):

  SEQ(0, 1, 1)                                  built-in node by short name, args in brackets
  VariableGetArmyInfoNode($06dc5c3d, 11:士气, $mod.morale)
                                                game node; "11:士气" means 11 (text after ':' is a note)
  IF $mod.morale >= 50                          condition, see below
  MiscLogNode(0, 1, $c553a000)  # 攻略対象勢力リスト   name stored on the node (Shift-JIS)
  # comment line                                lines starting with '#' are ignored

Values: decimal (negative allowed), $xxxxxxxx (8 hex digits) for a blackboard key or any u32, and
$name.with.dots for a new variable: its key is crc32 of the name (see var_key; btpatch checks that it does
not collide with a key the game already uses).

Conditions (CBehaviorTreeConditionDecoratorNode):
  IF term                                       one term, all-flag set (the usual case)
  IF [NOT ]ALL{term, term, ...}                 top-level list; ANY{...} = any-flag clear; NOT = negate all
  term:  [NOT ]ALL(term, ...) | [NOT ]ANY(term, ...)          group of the following terms
         [NOT ]a > b | a >= b                    functions 18/19 when b is $..., 20/21 when b is a number
         [NOT ]C<id>[:note](op, op, ...)         any other condition function
"""
import os
import re
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import btfile  # noqa: E402
import btdump  # noqa: E402
import conditions  # noqa: E402

GROUP = 0xFFFFFFFF
CMP = {18: ('>', True), 19: ('>=', True), 20: ('>', False), 21: ('>=', False)}  # id: (op, rhs is var)


# ---------------------------------------------------------------- names

def _name_tables():
    game, builtin = {}, {}
    for h, n in btdump.TYPES['game'].items():
        short = n[1:] if n.startswith('C') else n
        game.setdefault(short, []).append(int(h, 16))
    for h, n in btdump.TYPES['builtin'].items():
        builtin[btdump.BUILTIN.get(n, n)] = int(h, 16)
    game = {k: v[0] for k, v in game.items() if len(v) == 1}
    return game, builtin


GAME_TYPES, BUILTIN_TYPES = _name_tables()


def var_key(name):
    """Blackboard key for a new variable written as $name in source."""
    return zlib.crc32(name.encode('utf-8')) & 0xFFFFFFFF


# ---------------------------------------------------------------- render

def val(v, as_var=None):
    """Render a u32. as_var=True forces $hex, False forces a number, None guesses like btdump."""
    if as_var is True:
        return f'${v:08x}'
    if as_var is False:
        return str(v - 0x100000000 if v >= 0x80000000 else v)
    return btdump.val(v)


def term_text(t):
    neg = 'NOT ' if t['not'] else ''
    if t['kind'] == 2:
        fid, ops = t['id'], t['ops']
        if fid in CMP and len(ops) == 2:
            op, rhs_var = CMP[fid]
            return f'{neg}{val(ops[0], True)} {op} {val(ops[1], rhs_var)}'
        note = btdump.CONDITION_NAMES.get(fid)
        return f'{neg}C{fid}{":" + note if note else ""}(' + ', '.join(val(o) for o in ops) + ')'
    return None  # groups are handled by the caller


def condition_text(args):
    c = conditions.parse(args)
    if not c:
        raise ValueError(f'condition args do not parse: {args}')
    terms = c['terms']
    i = 0

    def take():
        nonlocal i
        t = terms[i]
        i += 1
        if t['kind'] == 2:
            return term_text(t)
        word = {0: 'ALL', 1: 'ANY'}[t['kind']]
        sub = [take() for _ in range(t['x'])]
        return ('NOT ' if t['not'] else '') + f'{word}(' + ', '.join(sub) + ')'

    top = []
    while i < len(terms):
        top.append(take())
    if not c['a'] and c['b'] == 1 and len(top) == 1 and not c['c']:
        s = top[0]
    else:
        s = ('NOT ' if c['a'] else '') + ('ALL' if c['b'] else 'ANY') + '{' + ', '.join(top) + '}'
        if c['c']:
            s += f' c={c["c"]}'
    return 'IF ' + s


def node_text(n, info_names=True):
    name = btdump.type_name(n)
    if name == 'IF' and not (n.flags & 1):
        s = condition_text(n.args)
    else:
        s = name
        if n.args:
            s += '(' + (btdump.args_text(name, n.args) if info_names else ', '.join(val(a) for a in n.args)) + ')'
    if n.extra is not None:
        s += f' extra={val(n.extra)}'
    if n.args2:
        s += ' list=[' + ', '.join(val(a) for a in n.args2) + ']'
    if n.name:
        s += '  # ' + n.name.decode('cp932')
    return s


def render(root, indent=0, numbered=False):
    lines = []
    stack = [(root, indent)]
    while stack:
        n, d = stack.pop()
        lines.append('  ' * d + (f'[{n.index}] ' if numbered else '') + node_text(n))
        for c in reversed(n.children):
            stack.append((c, d + 1))
    return '\n'.join(lines)


# ---------------------------------------------------------------- parse

TOKEN = re.compile(r'\s*(?:(\$[0-9a-fA-F]{8})(?![\w.])|(\$[A-Za-z_][\w.]*)|(-?\d+)(?::[^,)\]]*)?|(>=|>)|(.))')


def parse_value(tok):
    tok = tok.strip()
    m = re.fullmatch(r'\$([0-9a-fA-F]{8})', tok)
    if m:
        return int(m.group(1), 16)
    if tok.startswith('$'):
        return var_key(tok[1:])
    m = re.fullmatch(r'(-?\d+)(?::.*)?', tok)
    if m:
        return int(m.group(1)) & 0xFFFFFFFF
    raise ValueError(f'bad value {tok!r}')


def split_top(s, sep=','):
    """Split on separators that are not inside () or {}."""
    out, depth, cur = [], 0, ''
    for ch in s:
        if ch in '({[':
            depth += 1
        elif ch in ')}]':
            depth -= 1
        if ch == sep and depth == 0:
            out.append(cur)
            cur = ''
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [x.strip() for x in out]


def parse_term(s, out):
    """Append the encoded term(s) for text s to out (list of term dicts)."""
    s = s.strip()
    neg = 0
    if s.startswith('NOT '):
        neg, s = 1, s[4:].strip()
    m = re.fullmatch(r'(ALL|ANY)\((.*)\)', s, re.S)
    if m:
        subs = split_top(m.group(2))
        out.append({'kind': 0 if m.group(1) == 'ALL' else 1, 'not': neg, 'id': GROUP, 'x': len(subs), 'ops': []})
        for sub in subs:
            parse_term(sub, out)
        return
    m = re.fullmatch(r'C(\d+)(?::[^(]*)?\((.*)\)', s, re.S)
    if m:
        ops = [parse_value(x) for x in split_top(m.group(2))] if m.group(2).strip() else []
        out.append({'kind': 2, 'not': neg, 'id': int(m.group(1)), 'x': 0, 'ops': ops})
        return
    m = re.fullmatch(r'(\S+)\s*(>=|>)\s*(\S+)', s)
    if m:
        lhs, op, rhs = m.groups()
        rhs_var = rhs.startswith('$')
        fid = {('>', True): 18, ('>=', True): 19, ('>', False): 20, ('>=', False): 21}[(op, rhs_var)]
        out.append({'kind': 2, 'not': neg, 'id': fid, 'x': 0, 'ops': [parse_value(lhs), parse_value(rhs)]})
        return
    raise ValueError(f'bad condition term {s!r}')


def parse_condition(s):
    """Text after 'IF ' -> ConditionDecorator args."""
    s = s.strip()
    a, b, c = 0, 1, 0
    m = re.fullmatch(r'(NOT\s+)?(ALL|ANY)\{(.*)\}(?:\s+c=(\d+))?', s, re.S)
    terms = []
    if m:
        a = 1 if m.group(1) else 0
        b = 1 if m.group(2) == 'ALL' else 0
        c = int(m.group(4) or 0)
        for t in split_top(m.group(3)):
            parse_term(t, terms)
    else:
        parse_term(s, terms)
    args = [a, b, c, len(terms)]
    for t in terms:
        args += [t['kind'], t['not'], t['id'], t['x'], len(t['ops'])] + t['ops']
    return args


def parse_node_line(text):
    text = re.sub(r'^\[\d+\]\s*', '', text.strip())
    name_bytes = None
    if '  # ' in text:
        text, label = text.split('  # ', 1)
        name_bytes = label.encode('cp932')
    text = text.strip()
    n = btfile.Node()
    if text.startswith('IF '):
        n.flags = 0
        n.type = BUILTIN_TYPES['IF']
        n.args = parse_condition(text[3:])
    else:
        m = re.fullmatch(r'([^\s(]+)(?:\((.*)\))?((?:\s+\w+=\S+)*)', text, re.S)
        if not m:
            raise ValueError(f'bad node line {text!r}')
        name, argtext = m.group(1), m.group(2)
        if name in BUILTIN_TYPES:
            n.flags, n.type = 0, BUILTIN_TYPES[name]
        elif name in GAME_TYPES:
            n.flags, n.type = 1, GAME_TYPES[name]
        elif re.fullmatch(r'game_[0-9a-f]{8}', name):
            n.flags, n.type = 1, int(name[5:], 16)
        else:
            raise ValueError(f'unknown node type {name!r}')
        n.args = [parse_value(x) for x in split_top(argtext)] if argtext and argtext.strip() else []
        for kv in (m.group(3) or '').split():
            k, v = kv.split('=', 1)
            if k == 'extra':
                n.extra = parse_value(v)
                n.flags |= 4
            elif k == 'list':
                n.args2 = [parse_value(x) for x in split_top(v.strip('[]'))]
                n.flags |= 8
    if name_bytes is not None:
        n.name = name_bytes
        n.flags |= 2
    n.children = []
    return n


def parse(text):
    """Parse source text into a list of root nodes (each with .children)."""
    roots, stack = [], []
    for raw in text.splitlines():
        if not raw.strip() or raw.strip().startswith('#'):
            continue
        indent = len(raw) - len(raw.lstrip(' '))
        if indent % 2:
            raise ValueError(f'odd indent: {raw!r}')
        depth = indent // 2
        node = parse_node_line(raw)
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if stack:
            if depth != stack[-1][0] + 1:
                raise ValueError(f'indent jumps more than one level: {raw!r}')
            stack[-1][1].children.append(node)
        else:
            if roots and depth != roots_depth:
                raise ValueError(f'top-level nodes must share one indent: {raw!r}')
            roots_depth = depth
            roots.append(node)
        stack.append((depth, node))
    return roots


# ---------------------------------------------------------------- self test

def same(a, b, path=''):
    for f in ('flags', 'type', 'extra', 'args', 'name', 'args2'):
        x, y = getattr(a, f), getattr(b, f)
        if (list(x) if isinstance(x, (list, tuple)) else x) != (list(y) if isinstance(y, (list, tuple)) else y):
            return f'{path}: {f} {x!r} != {y!r}'
    if len(a.children) != len(b.children):
        return f'{path}: {len(a.children)} children != {len(b.children)}'
    for k, (x, y) in enumerate(zip(a.children, b.children)):
        r = same(x, y, f'{path}/{k}')
        if r:
            return r
    return None


def selftest(path=None):
    bt = btfile.load(path or btdump.GAME_BT)
    total = 0
    for i, t in enumerate(bt.trees):
        k = t.ktbt
        text = render(k.root, numbered=True)
        roots = parse(text)
        if len(roots) != 1:
            return f'tree {i}: {len(roots)} roots'
        r = same(k.root, roots[0], f'tree {i}')
        if r:
            return r
        total += len(k.nodes)
    return f'ok: {total} nodes in {len(bt.trees)} trees render and parse back identically'


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        print(selftest(sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith('-') else None))
    else:
        sys.exit(__doc__)
