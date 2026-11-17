# usage: python -P ai/research/siege_groups.py [--odd] ai/runs/A [ai/runs/B ...]
"""Sieges of one force on one city by several troops (军势) at overlapping times: when each troops
arrived (its first day within ARRIVE hexes of the city), how far apart the arrivals were, how many soldiers
each brought, and whether the city fell. Shows whether native sieges arrive piecemeal and whether that
matters. --odd: only attackers of odd force ids (A/B runs)."""
import sys, collections, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import check_modules as C, observe as o

ARRIVE = 6
args = [a for a in sys.argv[1:] if not a.startswith('--')]
odd_only = '--odd' in sys.argv
for path in args:
    r = C.Run(path, 12)
    ids = {f['君主']: int(f['势力']) for f in o.read_csv(r.path, 'forces')}
    names = {}
    for f in o.read_csv(r.path, 'footholds'):
        names.setdefault(f['编号'], f['名称'])
    events = [e for e in o.read_csv(r.path, 'events') if e['事件'] == '城市易主' and e['日期'] in r.byday]
    troops = collections.defaultdict(list)
    for x in r.rows:
        if x['军势任务'] in C.SIEGE:
            troops[(x['势力'], x['军势'], x['军势目标'])].append(x)
    info = []        # (force, goal, start, end, arrival day or None, soldiers at start, first distance)
    for (force, t, goal), rs in troops.items():
        g = r.fh.get(goal)
        if g is None or (odd_only and ids.get(force, 0) % 2 == 0):
            continue
        ds = sorted({x['日期'] for x in rs}, key=r.dn)
        arr = next((d for d in ds if any(o.hex_distance(int(x['格']), g) <= ARRIVE for x in rs if x['日期'] == d)), None)
        sold = sum(int(x['兵数']) for x in rs if x['日期'] == ds[0])
        d0 = min(o.hex_distance(int(x['格']), g) for x in rs if x['日期'] == ds[0])
        info.append((force, goal, r.dn(ds[0]), r.dn(ds[-1]), r.dn(arr) if arr else None, sold, d0))
    # group troops of one force on one goal whose times overlap
    groups = []
    for force, goal in {(i[0], i[1]) for i in info}:
        its = sorted([i for i in info if i[0] == force and i[1] == goal], key=lambda i: i[2])
        cur = []
        for i in its:
            if cur and i[2] > max(c[3] for c in cur) + 10:
                groups.append(cur)
                cur = []
            cur.append(i)
        if cur:
            groups.append(cur)
    stat = collections.defaultdict(lambda: [0, 0])
    spreads = []
    for gr in groups:
        force, goal = gr[0][0], gr[0][1]
        lo, hi = min(i[2] for i in gr), max(i[3] for i in gr) + 10
        took = any(e['对象'] == names.get(goal) and e['到'] == force and lo <= r.dn(e['日期']) <= hi for e in events)
        arr = sorted(i[4] for i in gr if i[4] is not None)
        if len(gr) == 1:
            k = 'one troops'
        elif len(arr) < 2:
            k = '2+ troops, <2 arrived'
        else:
            sp = arr[-1] - arr[0]
            spreads.append(sp)
            k = '2+ troops, arrivals %s' % ('within 5 days' if sp <= 5 else '6-15 days apart' if sp <= 15 else '16+ days apart')
        stat[k][0] += 1
        stat[k][1] += took
    print(path, 'siege groups', len(groups))
    for k in sorted(stat):
        n, t = stat[k]
        print('   %-34s %3d  taken %d (%d%%)' % (k, n, t, 100 * t // n))
    if spreads:
        spreads.sort()
        print('   arrival spread of multi-troops groups: median %d days, max %d' % (spreads[len(spreads) // 2], spreads[-1]))
