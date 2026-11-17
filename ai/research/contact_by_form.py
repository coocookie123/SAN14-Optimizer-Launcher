# usage: python -P ai/research/contact_by_form.py ai/runs/A [ai/runs/B ...]
"""Army-days in contact (an enemy adjacent), split by formation group, troops size and the order the army
had: our loss that day, the adjacent enemies' loss shared among the armies of ours touching them, and the
exchange (enemy loss / our loss). Shows which kind of army a change made fight better or worse."""
import sys, collections, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import check_modules as C, observe as o

ENG = {'井阑', '冲车', '投石'}


def group(x):
    f = x['阵型']
    return '雁行' if f == '雁行' else '锋矢' if f == '锋矢' else 'engine' if f in ENG else 'transport' if f == '输送' else 'melee'


def kind(x, ids):
    a, t = x['部队行动'], x['部队目标']
    if a == '7':
        return 'attack adjacent' if t in ids else 'attack other'
    if a == '13':
        return 'wait'
    if a in ('1', '14'):
        return 'move'
    return 'other'


for path in sys.argv[1:]:
    r = C.Run(path, 12)
    nxt = {r.days[i]: r.days[i + 1] for i in range(len(r.days) - 1)}
    by = {(x['日期'], x['部队']): x for x in r.rows}
    acc = collections.defaultdict(lambda: [0, 0, 0.0])
    for d, rs in r.byday.items():
        nd = nxt.get(d)
        if not nd:
            continue
        size = collections.Counter((x['势力'], x['军势']) for x in rs)
        for x in rs:
            hx = int(x['格'])
            foes = [y for y in rs if y['势力'] != x['势力'] and o.hex_distance(hx, int(y['格'])) <= 1]
            n = by.get((nd, x['部队']))
            if not foes or not n or n['主将'] != x['主将']:
                continue
            ids = {'部队' + y['部队'] for y in foes}
            ours = max(0, int(x['兵数']) - int(n['兵数']))
            theirs = 0.0
            for y in foes:
                m = by.get((nd, y['部队']))
                if m and m['主将'] == y['主将']:
                    c = sum(1 for z in rs if z['势力'] == x['势力'] and o.hex_distance(int(z['格']), int(y['格'])) <= 1)
                    theirs += max(0, int(y['兵数']) - int(m['兵数'])) / max(c, 1)
            multi = 'multi' if size[(x['势力'], x['军势'])] >= 2 else 'solo'
            for key in ((group(x), multi, 'all'), (group(x), multi, kind(x, ids))):
                acc[key][0] += 1
                acc[key][1] += ours
                acc[key][2] += theirs
    print(path)
    for k in sorted(acc, key=lambda k: (k[0], k[1], k[2] != 'all', -acc[k][0])):
        n, ours, theirs = acc[k]
        if n < 5:
            continue
        print('  %-9s %-5s %-16s days %4d  our loss %7d  enemy loss %7.0f  exchange %.2f' % (
            k + (n, ours, theirs, theirs / ours if ours else 0)))
