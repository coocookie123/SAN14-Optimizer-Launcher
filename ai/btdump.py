"""Dump 0000/bt_exce.bin as readable outlines, one text file per tree.

  python -P ai/btdump.py [bt_exce.bin] [out_dir]

Defaults: the Steam install's 0000/bt_exce.bin (read only) and ai/out/. Node names come from
ai/node_types.json (make_node_types.py). Notation:

  [12] SEQ                      built-in node, short name (table BUILTIN below)
  [13] GetMyselfArmyNode($06dc5c3d)
                                game node; $xxxxxxxx is a blackboard variable (by hash),
                                plain numbers are constants
  [14] IF ALL{$9cad27ea >= 50, NOT C146($06dc5c3d)}
                                condition decorator: one term, or a top-level list in braces
                                (ALL = every term, ANY = any term, NOT before it negates the whole);
                                inside, ALL(...) / ANY(...) group terms; Cnn = condition function nn.
                                This is the notation ai/btsrc.py parses: lines can be copied into patches.
  # 攻略対象勢力リスト          debug name stored on the node (MiscLogNode text, Shift-JIS)
"""
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import btfile  # noqa: E402
import conditions  # noqa: E402

GAME_BT = r'E:\Steam\steamapps\common\Romance_of_the_Three_Kingdoms_14\0000\bt_exce.bin'

BUILTIN = {
    'CBehaviorTreeRootNode': 'ROOT',
    'CBehaviorTreeSequentialNode': 'SEQ',
    'CBehaviorTreeSelectorNode': 'SEL',
    'CBehaviorTreeOneSelectorNode': 'ONESEL',
    'CBehaviorTreePriorityNode': 'PRI',
    'CBehaviorTreeParallelNode': 'PAR',
    'CBehaviorTreeProbabilityNode': 'PROB',
    'CBehaviorTreeConditionDecoratorNode': 'IF',
    'CBehaviorTreeLoopDecoratorNode': 'LOOP',
    'CBehaviorTreeReverseDecoratorNode': 'NOT',
    'CBehaviorTreeJointNode': 'JOINT',
    'CBehaviorTreeStaticLinkNode': 'LINK',
    'CBehaviorTreeDynamicLinkNode': 'DLINK',
}

# what each tree does, judged from the nodes it uses (see README)
TREE_LABELS = {
    0x57c83e03: 'force',     # 势力
    0xcca2ce60: 'district',  # 军团
    0x6b6c9fad: 'troops',    # 出征部队的任务
    0xeee443b1: 'army',      # 单支部队的战术
}


def load_types():
    with open(os.path.join(HERE, 'node_types.json'), encoding='utf-8') as f:
        return json.load(f)


TYPES = load_types()


def type_name(n):
    h = f'{n.type:08x}'
    if n.flags & 1:
        name = TYPES['game'].get(h)
        return name[1:] if name and name.startswith('C') else (name or f'game_{h}')
    name = TYPES['builtin'].get(h)
    return BUILTIN.get(name, name or f'builtin_{h}')


def val(a):
    if a >= 0xFFFF0000:
        return str(a - 0x100000000)
    if a >= 0x10000:
        return f'${a:08x}'
    return str(a)


# Condition functions read by hand from the callback's jump table (RVA c3160, 1.0.2.58226).
# A trailing ? means the meaning is inferred from the field it reads plus how the trees use it.
CONDITION_NAMES = {
    22: '有效',        # object exists and its vfunc 0x18 is nonzero
    23: '相等',        # two blackboard ints are equal
    24: '在列表中',    # C24(type, obj, list): obj's pointer is in the list (abfb0); false if either is missing
    84: '附近无敌军?', # C84(type, obj, force, n): gathers the armies within n hexes of obj (2b0500) and
                       # checks each one's force against `force` (af880); the original uses it as "safe to send from"
    26: '军团方针?',   # current agent (district) byte +0x15 == n; values 0/1/2
    28: '部队任务',    # troops word +0x14 == n (same enum as SpecifiedTroopsSetPurposeNode)
    29: '部队行动',    # army byte +0x37 == n (same enum as ArmySetArmyActionNode)
    63: '玩家势力',    # force id == player's force id
    129: '部队阵型',   # army's formation == n (formation n via bdb00, army's via 22ca00; army byte +0x1b):
                       # 1 鱼鳞 2 锋矢 3 雁行 4 方圆 5 鹤翼 6 长蛇 7 锥行 8 井阑 9 冲车 10 投石
                       # 11 走舸 12 蒙冲 13 楼船 14 四夷 17/18/20 输船 19 输送
    65: '玩家军团',    # district +0x10 (force index) == player's force
    115: '难度',       # config difficulty == n; 0 初级 1 中级 2 高级 3 超级 4 顶级 (by UI order)
    31: '等于',        # blackboard int == constant (same family as 18-21: a32a0)
    37: '等于?',       # these four compare something with a constant (sete / setne by shape);
    120: '不等于?',    # which field is not read yet
    123: '等于?',
    147: '等于?',
    116: '玩家坐大?',  # true for the player's own objects, or player's share of a total >= n%,
                       # or a difficulty-dependent rotation; not fully read
}


def term_text(t):
    c = TYPES['conditions'].get(str(t['id']), {})
    ops = [val(o) for o in t['ops']]
    if 'op' in c and len(ops) == 2:
        s = f'{ops[0]} {c["op"]} {ops[1]}'
    elif t['id'] in CONDITION_NAMES:
        s = f'C{t["id"]}:{CONDITION_NAMES[t["id"]]}({", ".join(ops)})'
    else:
        s = f'C{t["id"]}({", ".join(ops)})'
    return ('NOT ' if t['not'] else '') + s


def condition_text(args):
    c = conditions.parse(args)
    if not c:
        return 'IF?(' + ', '.join(val(a) for a in args) + ')'
    terms = c['terms']
    out = []
    i = 0

    def take():
        nonlocal i
        t = terms[i]
        i += 1
        if t['kind'] == 2:
            return term_text(t)
        # kind 0: all of the next x terms, kind 1: any of them [kind 1 read from code shape only]
        word = 'ALL' if t['kind'] == 0 else 'ANY' if t['kind'] == 1 else f'G{t["kind"]}'
        sub = [take() for _ in range(t['x']) if i < len(terms)]
        return ('NOT ' if t['not'] else '') + f'{word}(' + ', '.join(sub) + ')'

    while i < len(terms):
        out.append(take())
    word = 'ALL' if c['b'] else 'ANY'
    s = f'{word}(' + ', '.join(out) + ')' if len(out) > 1 else (out[0] if out else 'TRUE')
    if c['a']:
        s = 'NOT ' + s
    if c['c']:
        s += f'  [c={c["c"]}]'
    return 'IF ' + s


def load_info_names():
    """ai/info_names.json: attribute index -> name for the Variable*InfoNode family."""
    try:
        with open(os.path.join(HERE, 'info_names.json'), encoding='utf-8') as f:
            j = json.load(f)
    except FileNotFoundError:
        return {}
    alias = j.get('_同义', {})
    pos = j.get('_编号位置', {})
    names = {k: v for k, v in j.items() if not k.startswith('_')}
    table = {}
    for node in set(names) | set(alias):
        src = names.get(alias.get(node, node), {})
        table[node] = (pos.get(node, 1), {int(i): v[0] for i, v in src.items()})
    return table


INFO_NAMES = load_info_names()


def args_text(name, args):
    parts = [val(a) for a in args]
    pos, names = INFO_NAMES.get(name, (None, {}))
    if pos is not None and pos < len(args) and args[pos] in names:
        parts[pos] = f'{args[pos]}:{names[args[pos]]}'
    return ', '.join(parts)


def node_text(n):
    name = type_name(n)
    if name == 'IF':
        s = condition_text(n.args)
    else:
        s = name
        if n.args:
            s += '(' + args_text(name, n.args) + ')'
    if n.extra is not None:
        s += f' extra={val(n.extra)}'
    if n.args2:
        s += ' list=[' + ', '.join(val(a) for a in n.args2) + ']'
    if n.name:
        try:
            s += '  # ' + n.name.decode('cp932')
        except UnicodeDecodeError:
            s += '  # ' + n.name.hex()
    return f'[{n.index}] {s}'


def dump_tree(k, f):
    import btsrc  # same notation the patch tools parse, so any line here can be copied into a module
    f.write(btsrc.render(k.root, numbered=True) + '\n')


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else GAME_BT
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, 'out')
    os.makedirs(out, exist_ok=True)
    bt = btfile.load(src)
    summary = []
    for i, t in enumerate(bt.trees):
        k = t.ktbt
        label = TREE_LABELS.get(k.name_hash, f'{k.name_hash:08x}')
        path = os.path.join(out, f'tree{i}_{label}.txt')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f'# tree {i} ({label}), name hash {k.name_hash:08x}, {len(k.nodes)} nodes, from {src}\n')
            dump_tree(k, f)
        cnt = collections.Counter(type_name(n) for n in k.nodes)
        summary.append(f'== tree {i} ({label}): {len(k.nodes)} nodes, {len(cnt)} node types')
        summary += [f'  {v:5d} {name}' for name, v in cnt.most_common()]
        print(f'{path}: {len(k.nodes)} nodes')
    with open(os.path.join(out, 'summary.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(summary) + '\n')


if __name__ == '__main__':
    main()
