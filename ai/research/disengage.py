# usage: python -P ai/research/disengage.py ai/runs/A [ai/runs/B ...]
"""Disengaging takes days: follow each army that touches an enemy while under an order to leave (go home:
action 2/11/12/3; move: 1/14; 雁行 separately) from the first such day until it is free (no enemy adjacent),
wiped, or its order changes. Per order kind and force-id group: how the episodes end and how long getting
free takes, and the soldiers lost on the way."""
import sys, collections, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import check_modules as C, observe as o


def kind(x):
    a = x['部队行动']
    k = 'go home' if a in ('2', '11', '12', '3') else 'move' if a in ('1', '14') else None
    if k and x['阵型'] == '雁行':
        k += ' (雁行)'
    return k


for path in sys.argv[1:]:
    r = C.Run(path, 12)
    ids = {f['君主']: int(f['势力']) for f in o.read_csv(r.path, 'forces')}
    grp = lambda n: '奇' if ids.get(n, 0) % 2 else '偶'
    days = r.days
    by = {(x['日期'], x['部队']): x for x in r.rows}

    def touching(d, x):
        return any(y['势力'] != x['势力'] and o.hex_distance(int(x['格']), int(y['格'])) <= 1 for y in r.byday[d])

    fate = {}                        # (army slot, general) -> how its last sortie ended
    for so in o.read_csv(r.path, 'sorties'):
        fate[(so['部队'], so['主将'])] = so['结局']
    seen = set()
    res = collections.defaultdict(lambda: collections.Counter())
    took = collections.defaultdict(list)
    lost = collections.defaultdict(int)
    for i, d in enumerate(days):
        for x in r.byday[d]:
            k = kind(x)
            key = (x['部队'], x['主将'])
            if not k or key in seen or not touching(d, x):
                continue
            seen.add(key)            # one episode per army (its first)
            g = (k, grp(x['势力']))
            start = int(x['兵数'])
            end = 'still in contact at the end'
            for j in range(i + 1, len(days)):
                n = by.get((days[j], x['部队']))
                if not n or n['主将'] != x['主将']:
                    end = 'wiped' if fate.get(key, '').startswith('溃灭') else 'got home'
                    break
                lost[g] += max(0, start - int(n['兵数']))
                start = int(n['兵数'])
                if kind(n) != k:
                    end = 'order changed'
                    break
                if not touching(days[j], n):
                    end = 'got free'
                    took[g].append(j - i)
                    break
            res[g][end] += 1
    print(path)
    for g in sorted(res):
        c = res[g]
        n = sum(c.values())
        t = sorted(took[g])
        med = t[len(t) // 2] if t else 0
        print('   %-14s %s  episodes %3d  got free %3d%% (median %d days)  got home %3d%%  wiped %3d%%  order changed %3d%%  lost/episode %d' % (
            g[0], g[1], n, 100 * c['got free'] // n, med, 100 * c['got home'] // n, 100 * c['wiped'] // n,
            100 * c['order changed'] // n, lost[g] // n))
