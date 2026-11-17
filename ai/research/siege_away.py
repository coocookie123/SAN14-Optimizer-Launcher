# usage: python -P ai/research/siege_away.py ai/runs/A [ai/runs/B ...]
"""Why a siege troops (军势) gets farther from its target city (the check's 「攻城军势离城变远的天数」).
For each day the troops' smallest distance to the goal grows: was the closest army gone the next day
(wiped, back in a city: the measure jumps without anyone walking away), or did it walk away, and under
what order (action, target kind), with enemy armies how close."""
import sys, collections, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import check_modules as C, observe as o

for path in sys.argv[1:]:
    r = C.Run(path, 12)
    nxt = {r.days[i]: r.days[i + 1] for i in range(len(r.days) - 1)}
    by = {(x['日期'], x['部队']): x for x in r.rows}
    ep = collections.defaultdict(list)
    for x in r.rows:
        if x['军势任务'] in C.SIEGE:
            ep[(x['势力'], x['军势'], x['军势目标'])].append(x)
    why = collections.Counter()
    for (force, t, goal), rs in ep.items():
        g = r.fh.get(goal)
        if g is None:
            continue
        per = collections.defaultdict(list)
        for x in rs:
            per[x['日期']].append(x)
        days = sorted(per, key=r.dn)
        for d, d2 in zip(days, days[1:]):
            if nxt.get(d) != d2:
                continue
            m1 = min(o.hex_distance(int(x['格']), g) for x in per[d])
            m2 = min(o.hex_distance(int(x['格']), g) for x in per[d2])
            if m2 <= m1:
                continue
            c = min(per[d], key=lambda x: o.hex_distance(int(x['格']), g))
            n = by.get((d2, c['部队']))
            if not n or n['主将'] != c['主将'] or n['军势'] != c['军势']:
                why[('closest army gone (wiped, in a city, other troops)', '')] += 1
                continue
            if o.hex_distance(int(n['格']), g) <= m1:
                why[('closest army stayed, the measure moved to another', '')] += 1
                continue
            foes = [o.hex_distance(int(c['格']), int(y['格'])) for y in r.byday[d] if y['势力'] != c['势力']]
            fd = min(foes) if foes else 99
            tk = c['部队目标'][:2]
            if c['部队目标'].startswith('格'):
                th = int(c['部队目标'][2:]) if c['部队目标'][2:].isdigit() else None
                th = int(c['部队目标'][1:]) if th is None else th
                lead = next((x for x in per[d] if x['本队'] == '是'), None)
                tk = 'hex: lead' if lead and lead is not c and th == int(lead['格']) else (
                    'hex: own' if th == int(c['格']) else 'hex: farther from city' if o.hex_distance(th, g) > m1 else 'hex: nearer')
            why[('walked away: action %s -> %s' % (c['部队行动'], tk),
                 'enemy %s' % ('adjacent' if fd <= 1 else '2-3' if fd <= 3 else '4-8' if fd <= 8 else 'none near'))] += 1
    print(path, sum(why.values()))
    for k, v in why.most_common(14):
        print('   %-52s %-14s %d' % (k + (v,)))
