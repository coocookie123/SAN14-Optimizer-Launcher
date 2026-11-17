# usage: python -P ai/research/stage_check.py [--ab] ai/runs/A [ai/runs/B ...]
"""Staging check (modules/experiments/stage.json): transports (军势任务 12) into the staging city (the force's
own city nearest to a city it besieges within the next 90 days), and siege troops (军势) that set out from the
staging city. --ab: split by odd / even force ids (A/B runs: odd ones have the modules)."""
import sys, collections, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import check_modules as C, observe as o

args = [a for a in sys.argv[1:] if not a.startswith('--')]
ab = '--ab' in sys.argv


def med(v):
    v = sorted(v)
    return v[len(v) // 2] if v else 0


for path in args:
    r = C.Run(path, 12)
    ids = {f['君主']: int(f['势力']) for f in o.read_csv(r.path, 'forces')}
    chex = {f['名称']: int(f['格']) for f in o.read_csv(r.path, 'footholds') if f['类型'] == '城市'}
    gates = {51 + int(f['编号']): int(f['格']) for f in o.read_csv(r.path, 'footholds') if f['类型'] == '关隘'}
    names = {}
    for f in o.read_csv(r.path, 'footholds'):
        names.setdefault(f['编号'], f['名称'])
    cities = collections.defaultdict(dict)
    for c in o.read_csv(r.path, 'cities'):
        cities[c['日期']][c['城市']] = c['势力']
    cdays = sorted(cities, key=r.dn)

    def owner_at(day):
        best = None
        for d in cdays:
            if r.dn(d) <= r.dn(day):
                best = d
        return cities[best] if best else {}

    def goal_hex(goal):
        return r.fh.get(goal) if int(goal) < 52 else gates.get(int(goal))

    def group(force):
        if not ab:
            return 'all'
        return 'odd (modules)' if ids.get(force, 0) % 2 else 'even (official)'

    first = {}
    for x in r.rows:
        k = (x['势力'], x['军势'], x['军势目标'], x['军势任务'])
        if k not in first or r.dn(x['日期']) < r.dn(first[k][0]['日期']):
            first[k] = [x]
        elif x['日期'] == first[k][0]['日期']:
            first[k].append(x)
    sieges = []        # (force, day, goal, origin city, staging city, soldiers)
    transports = []    # (force, day, dest name, soldiers)
    for (force, t, goal, task), xs in first.items():
        day = xs[0]['日期']
        own = [n for n, f in owner_at(day).items() if f == force and n in chex]
        if not own:
            continue
        sold = sum(int(x['兵数']) for x in xs)
        pos = int(xs[0]['格'])
        if task in C.SIEGE:
            g = goal_hex(goal)
            if g is None:
                continue
            origin = min(own, key=lambda n: o.hex_distance(pos, chex[n]))
            stage = min(own, key=lambda n: o.hex_distance(g, chex[n]))
            sieges.append((force, r.dn(day), goal, origin, stage, sold))
        elif task == '12':
            transports.append((force, r.dn(day), names.get(goal, goal), sold))
    out = collections.defaultdict(collections.Counter)
    vals = collections.defaultdict(lambda: collections.defaultdict(list))
    for force, day, dest, sold in transports:
        k = group(force)
        out[k]['transports'] += 1
        out[k]['transports with 3000+ soldiers'] += sold >= 3000
        later = [s for s in sieges if s[0] == force and day <= s[1] <= day + 90]
        if any(s[4] == dest for s in later):
            out[k]['into a later staging city'] += 1
            out[k]['  of them 3000+ soldiers'] += sold >= 3000
            vals[k]['soldiers into staging'].append(sold)
    for force, day, goal, origin, stage, sold in sieges:
        k = group(force)
        out[k]['siege troops'] += 1
        out[k]['  from the staging city'] += origin == stage
        vals[k]['siege troops soldiers'].append(sold)
        if origin == stage:
            vals[k]['soldiers from staging'].append(sold)
        else:
            vals[k]['soldiers from elsewhere'].append(sold)
    print(path)
    for k in sorted(out):
        c = out[k]
        print('  %s: %s' % (k, ', '.join('%s %d' % (n, v) for n, v in c.items())))
        print('    median soldiers: %s' % ', '.join('%s %d (sum %d)' % (n, med(v), sum(v))
                                                    for n, v in vals[k].items()))
