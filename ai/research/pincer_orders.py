# usage: python -P ai/research/pincer_orders.py ai/runs/A [ai/runs/B ...]
"""Pincers (two armies of one force next to an enemy army and two hexes apart, as in check_modules): what orders the two had,
how the day's exchange depends on it, and what happened to a pincer the next day (kept, broken because a
member left / got a new order, the enemy died). Shows whether our modules break the pincers the original
AI forms by itself."""
import sys, collections, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import check_modules as C, observe as o

W = 220


def xy(h):
    x = h % W
    return x, 2 * (h // W) + (x % 2 == 0)


def opposite(a, b, e):
    """both next to e and two hexes apart (opposite or 120 degrees): the pincer of check_modules"""
    return o.hex_distance(a, e) == 1 and o.hex_distance(b, e) == 1 and o.hex_distance(a, b) == 2


def kind(x, eid):
    a, t = x['部队行动'], x['部队目标']
    if a == '7':
        return 'attack it' if t == eid else 'attack other'
    return {'13': 'wait', '1': 'move', '14': 'move'}.get(a, 'other %s' % a)


for path in sys.argv[1:]:
    r = C.Run(path, 12)
    nxt = {r.days[i]: r.days[i + 1] for i in range(len(r.days) - 1)}
    by = {(x['日期'], x['部队']): x for x in r.rows}
    pairs = collections.Counter()
    ex = collections.defaultdict(lambda: [0, 0, 0.0])
    fate = collections.Counter()
    for d, rs in r.byday.items():
        nd = nxt.get(d)
        for e in rs:
            he = int(e['格'])
            for f in {z['势力'] for z in rs if z['势力'] != e['势力']}:
                adj = [z for z in rs if z['势力'] == f and o.hex_distance(he, int(z['格'])) == 1]
                pin = [(a, b) for i, a in enumerate(adj) for b in adj[i + 1:] if opposite(int(a['格']), int(b['格']), he)]
                if not pin:
                    continue
                a, b = pin[0]
                eid = '部队' + e['部队']
                ka, kb = sorted((kind(a, eid), kind(b, eid)))
                key = 'both attack it' if ka == kb == 'attack it' else '%s + %s' % (ka, kb)
                pairs[key] += 1
                if not nd:
                    continue
                na, nb, ne = by.get((nd, a['部队'])), by.get((nd, b['部队'])), by.get((nd, e['部队']))
                lo = sum(max(0, int(x['兵数']) - int(n['兵数'])) for x, n in ((a, na), (b, nb)) if n and n['主将'] == x['主将'])
                # what the pair dealt: every enemy touching either member, shared among the armies of ours
                # touching it (a member attacking another enemy is not counted as doing nothing)
                le = 0.0
                for y in rs:
                    if y['势力'] == f or not any(o.hex_distance(int(y['格']), int(x['格'])) == 1 for x in (a, b)):
                        continue
                    m = by.get((nd, y['部队']))
                    if not m or m['主将'] != y['主将']:
                        continue
                    k = sum(1 for z in rs if z['势力'] == f and o.hex_distance(int(z['格']), int(y['格'])) == 1)
                    mine = sum(1 for x in (a, b) if o.hex_distance(int(y['格']), int(x['格'])) == 1)
                    le += max(0, int(y['兵数']) - int(m['兵数'])) * mine / max(k, 1)
                g = key
                ex[g][0] += 1
                ex[g][1] += lo
                ex[g][2] += le
                if not ne or ne['主将'] != e['主将']:
                    fate['enemy gone'] += 1
                elif any(not n or n['主将'] != x['主将'] for x, n in ((a, na), (b, nb))):
                    fate['a member gone'] += 1
                elif opposite(int(na['格']), int(nb['格']), int(ne['格'])):
                    fate['kept'] += 1
                else:
                    moved = [x for x, n in ((a, na), (b, nb)) if n['格'] != x['格']]
                    neword = [x for x, n in ((a, na), (b, nb)) if (n['部队行动'], n['部队目标']) != (x['部队行动'], x['部队目标'])]
                    fate['broken: %s moved%s' % (len(moved), ', order changed' if neword else '')] += 1
    n = sum(pairs.values())
    print(path, 'pincer days', n)
    for k, v in pairs.most_common(8):
        print('   %-28s %4d (%2d%%)' % (k, v, 100 * v // n))
    for g, (k, lo, le) in sorted(ex.items(), key=lambda kv: -kv[1][0]):
        if k >= 3:
            print('   exchange %-28s days %4d  our loss %6d  %.2f' % (g, k, lo, le / lo if lo else 0))
    print('   next day:', dict(fate.most_common()))
