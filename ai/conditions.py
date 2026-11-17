"""Condition expressions of CBehaviorTreeConditionDecoratorNode.

Node args: [any_flag, all_flag, c, term count, terms...]
  any_flag -> b34aa0, all_flag -> b34900, c -> b34a90 (built-in node setters, 1.0.2.58226)
Each term: kind, negate, function id, x, operand count, operands...
  (setter b34910: bytes of the first two and the last two words are kept, the id is a full u32)

kind 0 calls the game's condition callback with the function id: one function (RVA c3160 in
1.0.2.58226) that switches on id - 1 through a jump table of 0xa7 entries.
"""


def parse(args):
    """Split ConditionDecorator args into (flags, terms). Returns None if they do not fit."""
    if len(args) < 4:
        return None
    a, b, c, count = args[:4]
    terms = []
    p = 4
    for _ in range(count):
        if p + 5 > len(args):
            return None
        kind, neg, fid, x, n = args[p:p + 5]
        ops = args[p + 5:p + 5 + n]
        if len(ops) != n:
            return None
        terms.append({'kind': kind, 'not': neg, 'id': fid, 'x': x, 'ops': ops})
        p += 5 + n
    if p != len(args):
        return None
    return {'a': a, 'b': b, 'c': c, 'terms': terms}
