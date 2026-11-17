# usage: python -P ai/research/transport_trips.py [--odd] ai/runs/A [ai/runs/B ...]
"""Native transports (军势任务 12): distance from where the troops was first seen to its target foothold,
days on the road, hexes per day, soldiers carried, and whether the target was the own city nearest to a
siege target of the same force at that time (staging city). --odd: only odd force ids (A/B runs)."""
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
    gates = {51 + int(f['编号']): int(f['格']) for f in o.read_csv(r.path, 'footholds') if f['类型'] == '关隘'}
    trips = collections.defaultdict(list)
    for x in r.rows:
        if x['军势任务'] != '12' or (odd_only and ids.get(x['势力'], 0) % 2 == 0):
            continue
        trips[(x['势力'], x['军势'], x['军势目标'])].append(x)
    dist, days, speed, sold = [], [], [], []
    for (force, t, goal), xs in trips.items():
        g = r.fh.get(goal) if int(goal) < 52 else gates.get(int(goal))
        if g is None:
            continue
        xs.sort(key=lambda x: r.dn(x['日期']))
        d0 = o.hex_distance(int(xs[0]['格']), g)
        n = r.dn(xs[-1]['日期']) - r.dn(xs[0]['日期']) + 1
        dist.append(d0)
        days.append(n)
        if n >= 2 and d0 >= 4:
            speed.append(d0 / n)
        sold.append(int(xs[0]['兵数']))
    if not dist:
        print(path, 'no transports')
        continue
    for v in (dist, days, speed, sold):
        v.sort()
    q = lambda v: '%s/%s/%s' % (round(v[len(v) // 4], 1), round(v[len(v) // 2], 1), round(v[3 * len(v) // 4], 1))
    print('%s transports %d: start distance q1/med/q3 %s; days %s; hexes per day %s; soldiers %s' % (
        path, len(dist), q(dist), q(days), q(speed), q(sold)))
