"""Tests for btpatch/btsrc on the real game tree (read only). python -P ai/test_btpatch.py"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import btfile  # noqa: E402
import btpatch  # noqa: E402
import btsrc  # noqa: E402

TMP = tempfile.mkdtemp(prefix='btpatch_test_')


def module(mid, ops, priority=0):
    p = os.path.join(TMP, mid + '.json')
    with open(p, 'w', encoding='utf-8') as f:
        json.dump({'id': mid, 'priority': priority, 'ops': ops}, f, ensure_ascii=False)
    return p


def build(*paths):
    bt, report = btpatch.build_tree(list(paths))
    for t in bt.trees:
        t.ktbt.renumber()
    data = bt.to_bytes()
    again = btfile.BTFile(data)          # what the game would read
    assert again.to_bytes() == data
    return again


def kids(bt, tree, index):
    n = bt.trees[tree].ktbt.nodes[index]
    return [btsrc.node_text(c) for c in n.children]


def expect_error(paths, words):
    try:
        btpatch.build_tree(paths)
    except btpatch.PatchError as e:
        assert words in str(e), f'error {e!r} lacks {words!r}'
        return
    raise AssertionError(f'expected an error with {words!r}')


ARMY_PRIORITY = {'tree': 3, 'index': 7, 'type': 'SEQ'}           # SEQ(0, 0, 1): first that succeeds
RECOVER = {'tree': 3, 'find': 'RecoverLogisticsNode'}           # [102]


def test_insert_order():
    a = module('a', [{'op': 'insert', 'at': ARMY_PRIORITY, 'pos': 'first',
                      'src': 'MiscDummyNode(1)'}], priority=50)
    b = module('b', [{'op': 'insert', 'at': ARMY_PRIORITY, 'pos': 'first',
                      'src': ['SEQ(0, 1, 1)', '  MiscDummyNode(2)', '  MiscDummyNode(3)']}], priority=10)
    bt = build(b, a)
    k = kids(bt, 3, 7)
    assert k[0] == 'MiscDummyNode(1)' and k[1] == 'SEQ(0, 1, 1)', k[:3]
    assert k[2].startswith('SetRelayHexEvadeFlameHexNode'), k[2]
    assert len(bt.trees[3].ktbt.nodes) == 111 + 4


def test_wrap_before_after():
    w = module('w', [{'op': 'wrap', 'at': RECOVER, 'src': 'IF $m.w.flag >= 1'},
                     {'op': 'before', 'at': RECOVER, 'src': 'VariableCreateNode($m.w.flag, 1)'},
                     {'op': 'after', 'at': RECOVER, 'src': 'MiscDummyNode(7)'}])
    bt = build(w)
    k = bt.trees[3].ktbt
    texts = [btsrc.node_text(n) for n in k.nodes]
    flag = btsrc.var_key('m.w.flag')
    i = next(j for j, n in enumerate(k.nodes) if texts[j].startswith('IF ') and flag in n.args)
    assert texts[i - 1].startswith('VariableCreateNode($'), texts[i - 1]
    assert texts[i + 1] == 'RecoverLogisticsNode', texts[i + 1]
    assert texts[i + 2] == 'MiscDummyNode(7)', texts[i + 2]
    assert k.nodes[i].args[-2] == flag and k.nodes[i - 1].args[0] == flag
    # the wrapper sits where the node was: same parent, and the node is its only child
    assert k.nodes[i].parent == k.nodes[i - 1].parent and k.nodes[i + 1].parent == i


def test_find_within_count():
    bt = btfile.load(btpatch.btdump.GAME_BT)
    t, hits = btpatch.resolve(bt, {'tree': 1, 'find': 'SelectPersonTacticsNode(', 'within': 'LINK($db6b16c3)',
                                   'count': 'any'}, 'test')
    t, every = btpatch.resolve(bt, {'tree': 1, 'find': 'SelectPersonTacticsNode(', 'count': 'any'}, 'test')
    assert 3 <= len(hits) <= len(every) == 14, (len(hits), len(every))
    t, one = btpatch.resolve(bt, {'tree': 1, 'find': 're:^DepartureTroopsCreateNode\\(.*, 1, ', 'count': 'any'},
                             'test')
    assert len(one) == 3, len(one)       # one interception sortie per district branch
    expect_error([module('c', [{'op': 'remove', 'at': {'tree': 1, 'find': 'SelectPersonTacticsNode(',
                                                       'within': 'LINK($db6b16c3)'}}])], 'expected 1')


def test_conflicts():
    r1 = module('r1', [{'op': 'replace', 'at': RECOVER, 'src': 'MiscDummyNode(1)'}])
    r2 = module('r2', [{'op': 'remove', 'at': RECOVER}])
    expect_error([r1, r2], 'conflicting')
    s1 = module('s1', [{'op': 'set_arg', 'at': {'tree': 0, 'index': 4}, 'arg': 1, 'expect': 1, 'value': 2}])
    s2 = module('s2', [{'op': 'set_arg', 'at': {'tree': 0, 'index': 4}, 'arg': 1, 'expect': 1, 'value': 3}])
    expect_error([s1, s2], 'conflicting')
    # replacing a parent while another module changes a child
    p = module('p', [{'op': 'replace', 'at': {'tree': 0, 'index': 2, 'type': 'SEQ'}, 'src': 'MiscDummyNode(1)'}])
    expect_error([p, s1], 'subtree')
    # two wraps of the same node are fine
    w1 = module('w1', [{'op': 'wrap', 'at': RECOVER, 'src': 'IF $m.w1.x >= 1'}], priority=5)
    w2 = module('w2', [{'op': 'wrap', 'at': RECOVER, 'src': 'IF $m.w2.x >= 1'}], priority=9)
    bt = build(w1, w2)
    texts = [btsrc.node_text(n) for n in bt.trees[3].ktbt.nodes]
    i = texts.index('RecoverLogisticsNode')
    assert texts[i - 1].startswith('IF ') and texts[i - 2].startswith('IF '), texts[i - 2:i + 1]


def test_variable_rules():
    bad = module('bad', [{'op': 'before', 'at': RECOVER, 'src': 'VariableCreateNode($flag, 1)'}])
    expect_error([bad], 'must be named')
    x = module('x', [{'op': 'before', 'at': RECOVER, 'src': 'VariableCreateNode($shared.n, 1)'}])
    y = module('y', [{'op': 'after', 'at': RECOVER, 'src': 'VariableCreateNode($shared.n, 2)'}])
    build(x, y)


def test_expect_mismatch():
    m = module('mm', [{'op': 'set_arg', 'at': {'tree': 0, 'index': 4}, 'arg': 1, 'expect': 5, 'value': 2}])
    expect_error([m], 'expected 5')


def test_condition_roundtrip():
    for s in ['$a.b >= 50', 'NOT C65($d6a5eaae)', 'ANY{$x.y > 3, ALL(C1(0, $00000010), NOT $x.y >= -2)}',
              'NOT ALL{C115(4)}', 'ALL{C26(1), ANY(C115(0), C115(1))}']:
        args = btsrc.parse_condition(s)
        n = btfile.Node()
        n.flags, n.type, n.args, n.children = 0, btsrc.BUILTIN_TYPES['IF'], args, []
        n.extra = n.name = n.args2 = None
        again = btsrc.parse_condition(btsrc.condition_text(args)[3:])
        assert again == args, (s, args, again)


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_'):
            fn()
            print('ok', name)
