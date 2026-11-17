# usage: python -P ai/research/siege_origins.py [--odd] ai/runs/A [ai/runs/B ...]
"""Where siege troops (军势) set out from, against the force's own city nearest to the target that day:
how far each is from the target, how many soldiers the troops brought, and how many the nearest city and
the departure city had. Shows whether staging in the nearest city (moving soldiers there first) could help.
--odd: only odd force ids (A/B runs)."""
import sys, collections, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import check_modules as C, observe as o

args = [a for a in sys.argv[1:] if not a.startswith('--')]
odd_only = '--odd' in sys.argv
for path in args:
    r = C.Run(path, 12)
    ids = {f['君主']: int(f['势力']) for f in o.read_csv(r.path, 'forces')}
    chex = {f['名称']: int(f['格']) for f in o.read_csv(r.path, 'footholds') if f['类型'] == '城市'}
    gates = {51 + int(f['编号']): int(f['格']) for f in o.read_csv(r.path, 'footholds') if f['类型'] == '关隘'}
    cities = collections.defaultdict(dict)       # day -> city -> (owner, soldiers)
    for c in o.read_csv(r.path, 'cities'):
        cities[c['日期']][c['城市']] = (c['势力'], int(c['兵力'] or 0))
    cdays = sorted(cities, key=r.dn)

    def city_state(day):
        best = None
        for d in cdays:
            if r.dn(d) <= r.dn(day):
                best = d
        return cities[best] if best else {}

    first = {}
    for x in r.rows:
        if x['军势任务'] not in C.SIEGE or (odd_only and ids.get(x['势力'], 0) % 2 == 0):
            continue
        k = (x['势力'], x['军势'], x['军势目标'])
        if k not in first or r.dn(x['日期']) < r.dn(first[k][0]['日期']):
            first[k] = [x]
        elif x['日期'] == first[k][0]['日期']:
            first[k].append(x)
    stat = collections.Counter()
    gaps, rows = [], []
    for (force, t, goal), xs in first.items():
        g = r.fh.get(goal) if int(goal) < 52 else gates.get(int(goal))
        if g is None:
            continue
        st = city_state(xs[0]['日期'])
        own = {n: v for n, v in st.items() if v[0] == force and n in chex}
        if not own:
            continue
        pos = int(xs[0]['格'])
        dep = min(own, key=lambda n: o.hex_distance(pos, chex[n]))
        near = min(own, key=lambda n: o.hex_distance(g, chex[n]))
        dd, dn = o.hex_distance(chex[dep], g), o.hex_distance(chex[near], g)
        sold = sum(int(x['兵数']) for x in xs)
        k = 'from the nearest city' if dep == near or dd - dn <= 3 else 'from a farther city'
        stat[k] += 1
        if k == 'from a farther city':
            gaps.append(dd - dn)
            rows.append((dd, dn, sold, own[dep][1], own[near][1]))
    print(path, dict(stat))
    if gaps:
        gaps.sort()
        print('   farther departure: extra distance median %d (max %d)' % (gaps[len(gaps) // 2], gaps[-1]))
        print('   (dep->target, nearest->target, troops soldiers, dep city soldiers left, nearest city soldiers):')
        for row in sorted(rows)[:12]:
            print('     %3d %3d %6d %6d %6d' % row)
