# usage: python -P ai/research/target_stats.py ai/runs/A [ai/runs/B ...]
"""How attack targets are spread inside a troop (军势) of 2+ land armies, one year.
Per troop-day with 2+ armies attacking armies (action 7): how many distinct targets vs how many enemy armies
are within 8 of the lead; per army: is its target the enemy nearest to itself / to the lead, how far away;
and the next-day exchange of armies in contact by whether their troop splits targets or not."""
import sys, collections
sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__))))
import check_modules as C, observe as o

SKIP = {'输送', '投石', '井阑', '冲车', '楼船', '斗舰', '走舸'}
for path in sys.argv[1:]:
    r = C.Run(path, 12)
    nxt = {r.days[i]: r.days[i + 1] for i in range(len(r.days) - 1)}
    by = {(x['日期'], x['部队']): x for x in r.rows}
    dist = collections.Counter()          # (n attackers, n distinct targets, enemies near)
    near = collections.Counter()
    tdist = collections.Counter()
    exch = collections.defaultdict(lambda: [0, 0.0, 0.0])
    for d, rs in r.byday.items():
        troops = collections.defaultdict(list)
        for x in rs:
            troops[(x['势力'], x['军势'])].append(x)
        for (f, t), xs in troops.items():
            land = [x for x in xs if x['阵型'] not in SKIP]
            if len(land) < 2:
                continue
            lead = next((x for x in xs if x['本队'] == '是'), land[0])
            lh = int(lead['格'])
            foes = [y for y in rs if y['势力'] != f and y['阵型'] not in ('输送',)]
            foes8 = [y for y in foes if o.hex_distance(lh, int(y['格'])) <= 8]
            att = [x for x in land if x['部队行动'] == '7' and x['部队目标'].startswith('部队')]
            if len(att) < 2 or not foes8:
                continue
            tg = {x['部队目标'] for x in att}
            kind = '%d attackers' % min(len(att), 4)
            dist[(kind, 'targets ' + ('1' if len(tg) == 1 else '2+'), 'foes8 ' + ('1' if len(foes8) == 1 else '2+'))] += 1
            for x in att:
                hx = int(x['格'])
                ys = [y for y in foes if '部队' + y['部队'] == x['部队目标']]
                if not ys:
                    continue
                y = ys[0]
                dself = o.hex_distance(hx, int(y['格']))
                mself = min(o.hex_distance(hx, int(z['格'])) for z in foes)
                mlead = min(o.hex_distance(lh, int(z['格'])) for z in foes)
                near['nearest to itself' if dself == mself else 'not nearest to itself'] += 1
                near['nearest to lead' if o.hex_distance(lh, int(y['格'])) == mlead else 'not nearest to lead'] += 1
                tdist['target %s' % ('adjacent' if dself <= 1 else '2-3' if dself <= 3 else '4-8' if dself <= 8 else '>8')] += 1
            # exchange of this troop's armies in contact, next day, split vs focused
            nd = nxt.get(d)
            if not nd:
                continue
            k = 'focused' if len(tg) == 1 else 'split'
            for x in land:
                hx = int(x['格'])
                adj = [y for y in foes if o.hex_distance(hx, int(y['格'])) <= 1]
                n = by.get((nd, x['部队']))
                if not adj or not n or n['主将'] != x['主将']:
                    continue
                exch[k][0] += 1
                exch[k][1] += max(0, int(x['兵数']) - int(n['兵数']))
                for y in adj:
                    m = by.get((nd, y['部队']))
                    if m and m['主将'] == y['主将']:
                        c = sum(1 for z in rs if z['势力'] == f and o.hex_distance(int(z['格']), int(y['格'])) <= 1)
                        exch[k][2] += max(0, int(y['兵数']) - int(m['兵数'])) / max(c, 1)
    print(path)
    tot = sum(dist.values())
    for k, v in sorted(dist.items()):
        print('  %-12s %-10s %-9s %4d' % (k + (v,)))
    print('  troop-days', tot, ' all on one target %.0f%%' % (100 * sum(v for k, v in dist.items() if k[1] == 'targets 1') / max(tot, 1)),
          ' when 2+ foes near: %.0f%%' % (100 * sum(v for k, v in dist.items() if k[1] == 'targets 1' and k[2] == 'foes8 2+') /
                                         max(1, sum(v for k, v in dist.items() if k[2] == 'foes8 2+'))))
    print('  ', dict(near))
    print('  ', dict(tdist))
    for k, (n, ours, theirs) in exch.items():
        print('   contact exchange %-8s army-days %4d  %.2f' % (k, n, theirs / ours if ours else 0))
