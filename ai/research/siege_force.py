"""攻城兵力够不够、府分差多少（2026-11-14，用户：「看看是不是攻城的总兵力不够……还有可能没有考虑府加成」）。

  python -P ai/research/siege_force.py ai/runs/<run> [more runs] [--months 12] [--list] [--side odd|even]   # --side: 只看攻方势力编号为奇数 / 偶数的（A/B）

一次攻城 = 同一势力打同一座城、时间上接得上（前一个结束 10 天内后一个开始）的攻城军势合起来。每次攻城记：
  攻方：出发时的部队数、兵数（每支部队第一天的兵）、攻城兵器数；
  守方：目标城的主人和城里的兵（cities.csv 每月一条，取到城下那天之前最近的一条）；攻城期间到过城 8 格内的守方部队，
        按它第一次出现时离哪座城最近分成「从目标城出来的」和「别的城来的援军」，各取最多兵数；
  府分：攻方部队在城 6 格内的日子里，攻方府分（记录里的「府分」列）和守方在同一格的府分（按 areas.csv、fu.csv、
        府易主事件算：所在区域和相邻区域里府属于守方的，各按涂色占比加起来），取平均；
  结果：城市易主给攻方（结束后 10 天内也算）；攻方部队溃灭几支、掉了多少兵（sorties.csv）。
然后按「攻方兵 ÷ 守方总兵（城里 + 出城 + 援军）」和「府分差」分组看拿下率和溃灭率。
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402

SIEGE = {'2', '8', '9', '10', '22'}
ENGINES = {'井阑', '冲车', '投石'}
CHECK = []       # |logged 府分 - computed| for the attackers, to check Fu
CITYR = 3          # 到城下
NEAR = 8           # 守方部队算参战
FUR = 6            # 府分取样范围


def dn(d):
    return o.day_number(o.parse_date(d))


class Fu:
    """Owner of each area's 府 by day: monthly snapshots (fu.csv) moved forward by 府易主 events."""

    def __init__(self, p):
        self.adj = {}
        for a in o.read_csv(p, 'areas'):
            self.adj[a['区域']] = a['相邻区域'].split()
        snap = collections.defaultdict(dict)
        for f in o.read_csv(p, 'fu'):
            snap[dn(f['日期'])][f['区域']] = (f['府势力'], float(f['涂色占比'] or 0))
        self.snaps = sorted(snap.items())
        self.ev = collections.defaultdict(list)
        for e in o.read_csv(p, 'events'):
            if e['事件'] == '府易主':
                self.ev[e['对象'].split(':')[0]].append((dn(e['日期']), e['到']))
        self.cache = {}

    def owner(self, area, day):
        k = (area, day)
        if k in self.cache:
            return self.cache[k]
        base, share = None, 0.0
        for d, s in self.snaps:
            if d > day:
                break
            if area in s:
                base, share = s[area]
                t0 = d
        if base is None and self.snaps and area in self.snaps[0][1]:
            base, share = self.snaps[0][1][area]
            t0 = -1
        if base is not None:
            for d, to in self.ev.get(area, []):
                if t0 < d <= day:
                    base = to
        self.cache[k] = (base, share)
        return base, share

    def score(self, area, force, day):
        s = 0.0
        for a in [area] + self.adj.get(area, []):
            w, share = self.owner(a, day)
            if w == force:
                s += share
        return s


def analyse(p, months):
    rows = o.read_csv(p, 'armies')
    days = sorted({r['日期'] for r in rows}, key=dn)
    lo = dn(days[0])
    rows = [r for r in rows if not months or dn(r['日期']) < lo + months * 31]
    city = {}
    for f in o.read_csv(p, 'footholds'):
        if f['类型'] == '城市':
            city[f['编号']] = (f['名称'], int(f['格']))
    chexes = [(h, n) for n, h in city.values()]
    cities = collections.defaultdict(list)
    for c in o.read_csv(p, 'cities'):
        cities[c['城市']].append((dn(c['日期']), c['势力'], int(c['兵力'])))
    falls = [e for e in o.read_csv(p, 'events') if e['事件'] == '城市易主']
    fid = {f['君主']: int(f['势力']) for f in o.read_csv(p, 'forces')}
    sorties = o.read_csv(p, 'sorties')
    has_fu = os.path.exists(os.path.join(p, 'fu.csv')) and '府分' in rows[0]
    fu = Fu(p) if has_fu else None
    byday = collections.defaultdict(list)
    for r in rows:
        byday[r['日期']].append(r)

    # episodes: (force, troops slot, goal), split at gaps over 5 days
    ep = collections.defaultdict(list)
    for r in rows:
        if r['军势任务'] in SIEGE and r['军势目标'] in city:
            ep[(r['势力'], r['军势'], r['军势目标'])].append(r)
    episodes = []
    for (force, t, goal), rs in ep.items():
        rs.sort(key=lambda x: dn(x['日期']))
        cur = [rs[0]]
        for x in rs[1:]:
            if dn(x['日期']) - dn(cur[-1]['日期']) > 5:
                episodes.append((force, goal, cur))
                cur = []
            cur.append(x)
        episodes.append((force, goal, cur))
    # operations: same force and goal, overlapping or within 10 days
    ops = []
    for force, goal, rs in sorted(episodes, key=lambda e: dn(e[2][0]['日期'])):
        s, e = dn(rs[0]['日期']), dn(rs[-1]['日期'])
        for op in ops:
            if op['force'] == force and op['goal'] == goal and s <= op['end'] + 10:
                op['rows'] += rs
                op['end'] = max(op['end'], e)
                break
        else:
            ops.append({'force': force, 'goal': goal, 'start': s, 'end': e, 'rows': list(rs), 'date': rs[0]['日期']})

    out = []
    for op in ops:
        name, ch = city[op['goal']]
        rs = op['rows']
        units = {}
        for x in sorted(rs, key=lambda x: dn(x['日期'])):
            units.setdefault((x['部队'], x['主将']), x)
        att = sum(int(x['兵数']) for x in units.values())
        eng = sum(x['阵型'] in ENGINES for x in units.values())
        arr = [dn(x['日期']) for x in rs if o.hex_distance(int(x['格']), ch) <= CITYR]
        arrive = min(arr) if arr else None
        ref = arrive or op['start']
        rec = [c for c in cities.get(name, []) if c[0] <= ref] or cities.get(name, [])[:1]
        if not rec:
            continue
        _, owner, garrison = rec[-1]
        if owner == op['force']:
            continue
        # defender units near the city during the siege
        dunits = {}
        for x in rows:
            d = dn(x['日期'])
            if x['势力'] != owner or not (op['start'] <= d <= op['end']):
                continue
            if o.hex_distance(int(x['格']), ch) <= NEAR:
                k = (x['部队'], x['主将'])
                dunits.setdefault(k, [x, 0])
                dunits[k][1] = max(dunits[k][1], int(x['兵数']))
        # where each defender unit came from: its first row of that trip (look back up to 20 days)
        first = {}
        for x in rows:
            k = (x['部队'], x['主将'])
            if k in dunits and x['势力'] == owner and op['start'] - 20 <= dn(x['日期']) <= op['end']:
                if k not in first or dn(x['日期']) < dn(first[k]['日期']):
                    first[k] = x
        sortie = relief = 0
        for k, (x, mx) in dunits.items():
            h = int(first.get(k, x)['格'])
            near_city = min(chexes, key=lambda c: o.hex_distance(h, c[0]))[1]
            if near_city == name:
                sortie += mx
            else:
                relief += mx
        took = any(e['对象'] == name and e['到'] == op['force'] and op['start'] <= dn(e['日期']) <= op['end'] + 10
                   for e in falls)
        # sorties of the attacking units
        wiped = lost = n_so = 0
        for s in sorties:
            if s['势力'] != op['force'] or (s['部队'], s['主将']) not in units:
                continue
            if not (op['start'] - 3 <= dn(s['出发']) <= op['end']):
                continue
            n_so += 1
            wiped += s['结局'].startswith('溃灭')
            lost += max(0, int(s['最多兵数']) - int(s['最后兵数']))
        fa = fd = None
        if fu:
            sa, sd, n = 0.0, 0.0, 0
            for x in rs:
                if o.hex_distance(int(x['格']), ch) <= FUR and x.get('区域'):
                    sa += float(x['府分'] or 0)
                    sd += fu.score(x['区域'], owner, dn(x['日期']))
                    n += 1
            if n:
                fa, fd = sa / n, sd / n
            for x in rs:
                if x.get('区域'):
                    CHECK.append(abs(float(x['府分'] or 0) - fu.score(x['区域'], op['force'], dn(x['日期']))))
        out.append({'fid': fid.get(op['force'], 0), 'force': op['force'], 'owner': owner, 'city': name, 'start': op['start'], 'end': op['end'], 'date': op['date'], 'arrive': arrive,
                    'units': len(units), 'eng': eng, 'att': att, 'garrison': garrison, 'sortie': sortie,
                    'relief': relief, 'took': took, 'wiped': wiped, 'n_so': n_so, 'lost': lost, 'fa': fa, 'fd': fd})
    return out


def bucket(v, edges):
    for i, e in enumerate(edges):
        if v < e:
            return i
    return len(edges)


def main():
    args = sys.argv[1:]
    months = 12
    if '--months' in args:
        i = args.index('--months')
        months = int(args[i + 1])
        del args[i:i + 2]
    listing = '--list' in args
    side = args[args.index('--side') + 1] if '--side' in args else None
    if side:
        del args[args.index('--side'):args.index('--side') + 2]
    args = [a for a in args if a != '--list']
    allops = []
    for p in args:
        ops = analyse(p, months)
        if side:
            ops = [x for x in ops if x['fid'] % 2 == (1 if side == 'odd' else 0)]
        allops += ops
        reached = [x for x in ops if x['arrive']]
        print('== %s：攻城 %d 次（到过城下 %d），拿下 %d；平均出发 %.1f 支、%d 兵；城里 %d 兵、出城 %d、援军 %d' % (
            os.path.basename(p.rstrip('/\\')), len(ops), len(reached), sum(x['took'] for x in ops),
            sum(x['units'] for x in ops) / max(len(ops), 1), sum(x['att'] for x in ops) / max(len(ops), 1),
            sum(x['garrison'] for x in ops) / max(len(ops), 1), sum(x['sortie'] for x in ops) / max(len(ops), 1),
            sum(x['relief'] for x in ops) / max(len(ops), 1)))
        if listing:
            for x in sorted(ops, key=lambda x: x['start']):
                print('   %s→%s(%s) %s %d支 %d兵(器%d) | 城%d 出%d 援%d | 府 %s/%s | %s 溃%d/%d 掉%d' % (
                    x['force'], x['city'], x['owner'], x['date'], x['units'], x['att'], x['eng'], x['garrison'],
                    x['sortie'], x['relief'], '%.1f' % x['fa'] if x['fa'] is not None else '-',
                    '%.1f' % x['fd'] if x['fd'] is not None else '-', '拿下' if x['took'] else ('到城下' if x['arrive'] else '没到'),
                    x['wiped'], x['n_so'], x['lost']))
    ops = [x for x in allops if x['arrive']]
    if CHECK:
        print('府分核对：攻方 %d 条记录，自己算的和记录列相差 ≤0.05 的 %d%%，平均差 %.2f' % (
            len(CHECK), 100 * sum(c <= 0.05 for c in CHECK) // len(CHECK), sum(CHECK) / len(CHECK)))
    print('\n合计（只算到过城下 %d 格内的 %d 次）：' % (CITYR, len(ops)))

    def show(title, key, edges, labels):
        g = collections.defaultdict(list)
        for x in ops:
            v = key(x)
            if v is not None:
                g[bucket(v, edges)].append(x)
        print(title)
        for i, lab in enumerate(labels):
            xs = g.get(i, [])
            if not xs:
                continue
            so = sum(x['n_so'] for x in xs)
            print('  %-10s %3d 次  拿下 %3d%%  攻城部队溃灭 %3d%%  平均攻方 %5d 兵、守方合计 %5d' % (
                lab, len(xs), 100 * sum(x['took'] for x in xs) // len(xs),
                100 * sum(x['wiped'] for x in xs) // max(so, 1), sum(x['att'] for x in xs) // len(xs),
                sum(x['garrison'] + x['sortie'] + x['relief'] for x in xs) // len(xs)))

    show('按 攻方兵 ÷ 城里的兵：', lambda x: x['att'] / max(x['garrison'], 1), [1, 1.5, 2, 3],
         ['<1', '1–1.5', '1.5–2', '2–3', '≥3'])
    show('按 攻方兵 ÷ 守方合计（城里 + 出城 + 援军）：',
         lambda x: x['att'] / max(x['garrison'] + x['sortie'] + x['relief'], 1), [0.7, 1, 1.5, 2],
         ['<0.7', '0.7–1', '1–1.5', '1.5–2', '≥2'])
    show('按 有没有援军：', lambda x: 1 if x['relief'] > 0 else 0, [1], ['没援军', '有援军'])
    show('按 折算府加成后的兵力比（攻方兵 ×(1+0.1×攻方府分) ÷ 守方合计 ×(1+0.1×守方府分)；府加成 10 时实测倍数约 1+0.1×府分）：',
         lambda x: None if x['fa'] is None else x['att'] * (1 + 0.1 * x['fa']) / max(
             (x['garrison'] + x['sortie'] + x['relief']) * (1 + 0.1 * x['fd']), 1),
         [0.7, 1, 1.5, 2], ['<0.7', '0.7–1', '1–1.5', '1.5–2', '≥2'])
    print('兵力比（不算府）× 府分差 两向（拿下 / 次数）：')
    cells = collections.defaultdict(lambda: [0, 0])
    for x in ops:
        if x['fa'] is None:
            continue
        r = bucket(x['att'] / max(x['garrison'] + x['sortie'] + x['relief'], 1), [1, 1.5])
        f = bucket(x['fa'] - x['fd'], [-3, -1.5])
        cells[(r, f)][0] += x['took']
        cells[(r, f)][1] += 1
    print('  %-10s %-12s %-12s %-12s' % ('', '府分差≤−3', '−3~−1.5', '>−1.5'))
    for r, lab in enumerate(['兵力比<1', '1–1.5', '≥1.5']):
        print('  %-10s ' % lab + ' '.join('%-12s' % ('%d/%d' % tuple(cells[(r, f)]) if cells[(r, f)][1] else '-') for f in range(3)))
    show('按 府分差（攻方 − 守方，城 %d 格内平均）：' % FUR,
         lambda x: None if x['fa'] is None else x['fa'] - x['fd'], [-3, -1, 1, 3], ['≤−3', '−3~−1', '−1~1', '1~3', '≥3'])


if __name__ == '__main__':
    main()
