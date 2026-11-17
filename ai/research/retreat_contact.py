# usage: python -P ai/research/retreat_contact.py ai/runs/A [ai/runs/B ...]
"""Armies on the way home (troops task 0 retreat, 17/18 return) that touch an enemy: per force-id group
(odd / even, for A/B runs) and per order kind, the days, the exchange that day and how many such armies
were wiped out in the end. Shows what 「先打开再撤」 (fight back while in contact) changes."""
import sys, collections, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import check_modules as C, observe as o

HOME = {'0', '17', '18'}
for path in sys.argv[1:]:
    r = C.Run(path, 12)
    ids = {f['君主']: int(f['势力']) for f in o.read_csv(r.path, 'forces')}
    grp = lambda n: '奇数' if ids.get(n, 0) % 2 else '偶数'
    nxt = {r.days[i]: r.days[i + 1] for i in range(len(r.days) - 1)}
    by = {(x['日期'], x['部队']): x for x in r.rows}
    acc = collections.defaultdict(lambda: [0, 0, 0.0])
    armies = collections.defaultdict(set)
    for d, rs in r.byday.items():
        nd = nxt.get(d)
        if not nd:
            continue
        for x in rs:
            if x['军势任务'] not in HOME or x['阵型'] in ('输送', '井阑', '冲车', '投石'):
                continue
            hx = int(x['格'])
            foes = [y for y in rs if y['势力'] != x['势力'] and o.hex_distance(hx, int(y['格'])) <= 1]
            n = by.get((nd, x['部队']))
            if not foes or not n or n['主将'] != x['主将']:
                continue
            k = (grp(x['势力']), 'attack' if x['部队行动'] == '7' else 'go home' if x['部队行动'] in ('2', '11', '12', '3') else 'other')
            acc[k][0] += 1
            acc[k][1] += max(0, int(x['兵数']) - int(n['兵数']))
            for y in foes:
                m = by.get((nd, y['部队']))
                if m and m['主将'] == y['主将']:
                    c = sum(1 for z in rs if z['势力'] == x['势力'] and o.hex_distance(int(z['格']), int(y['格'])) <= 1)
                    acc[k][2] += max(0, int(y['兵数']) - int(m['兵数'])) / max(c, 1)
            armies[grp(x['势力'])].add((x['部队'], x['主将']))
    wiped = collections.Counter()
    for s in o.read_csv(r.path, 'sorties'):
        if s['出发'] in r.byday and (s['部队'], s['主将']) in armies[grp(s['势力'])] and s['结局'].startswith('溃灭'):
            wiped[grp(s['势力'])] += 1
    print(path)
    for k in sorted(acc):
        n, ours, theirs = acc[k]
        print('   %-4s %-8s days %4d  our loss %6d  exchange %.2f' % (k + (n, ours, theirs / ours if ours else 0)))
    for g in ('奇数', '偶数'):
        print('   %s armies on the way home that touched an enemy: %d, wiped later %d' % (g, len(armies[g]), wiped[g]))
