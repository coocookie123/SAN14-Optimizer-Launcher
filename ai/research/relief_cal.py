"""攻城援军预估对不对（2026-11-14，用户：「会考虑敌方城市的支援部队，也放入战力评估了吗」）。

  python -P ai/research/relief_cal.py ai/runs/<run> [more runs]

攻城评估模块（siegeplan）在出兵门槛和凑兵目标里加了「目标城相邻守方城的（兵 − 5000）× 距离权重」：12 格内 0.35、
18 格内 0.2、30 格内 0.15，合计封顶 45000。这里拿实际赶来的援军对一对：
  每次攻城（siege_force.analyse 的口径）在出发那天，取守方每座别的城最近一条月记录的兵，算（兵 − 5000）；
  攻城期间到过城 8 格内、出发处离那座城最近的守方部队算「从那座城来的援军」，取各支最多兵数相加；
  按到目标城的格数分档：每档「实际来的援军 ÷ 能出的兵（兵 − 5000）」就是实测权重，和 siegeplan 的权重比。
注意：只看相邻关系拿不到（记录里没有据点相邻表），这里按格数算所有守方城；出城的部队（离目标城最近）不算援军。
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import observe as o  # noqa: E402
import siege_force as F  # noqa: E402

BANDS = [(12, '≤12 格', 0.35), (18, '13–18 格', 0.2), (30, '19–30 格', 0.15), (45, '31–45 格', 0.0), (10 ** 9, '>45 格', 0.0)]


def main():
    tot = collections.defaultdict(lambda: [0, 0, 0])     # band -> spare, came, sieges with a city in the band
    per = []                                             # (predicted, actual) per siege
    for p in sys.argv[1:]:
        city = {}
        for f in o.read_csv(p, 'footholds'):
            if f['类型'] == '城市':
                city[f['名称']] = int(f['格'])
        snaps = collections.defaultdict(list)
        for c in o.read_csv(p, 'cities'):
            snaps[c['城市']].append((F.dn(c['日期']), c['势力'], int(c['兵力'])))
        rows = o.read_csv(p, 'armies')
        chexes = [(h, n) for n, h in city.items()]
        for x in F.analyse(p, 12):
            ch, owner, start, end = city[x['city']], x['owner'], x['start'], x['start'] + 120
            spare = {}
            for name, rec in snaps.items():
                if name == x['city']:
                    continue
                r = [s for s in rec if s[0] <= start] or rec[:1]
                if r and r[-1][1] == owner:
                    spare[name] = max(0, r[-1][2] - 5000)
            # relief units: defender units within 8 of the city during the siege, from the nearest city at their first row
            near = {}
            for r in rows:
                d = F.dn(r['日期'])
                if r['势力'] == owner and start <= d <= end and o.hex_distance(int(r['格']), ch) <= F.NEAR:
                    k = (r['部队'], r['主将'])
                    near[k] = max(near.get(k, 0), int(r['兵数']))
            first = {}
            for r in rows:
                k = (r['部队'], r['主将'])
                if k in near and r['势力'] == owner and start - 20 <= F.dn(r['日期']) <= end:
                    if k not in first or F.dn(r['日期']) < F.dn(first[k]['日期']):
                        first[k] = r
            came = collections.Counter()
            for k, mx in near.items():
                if k in first:
                    src = min(chexes, key=lambda c: o.hex_distance(int(first[k]['格']), c[0]))[1]
                    if src != x['city']:
                        came[src] += mx
            pred = act = 0
            seen = set()
            for name, s in spare.items():
                dist = o.hex_distance(city[name], ch)
                band = next(b for b in BANDS if dist <= b[0])
                tot[band[1]][0] += s
                tot[band[1]][1] += came.get(name, 0)
                if band[1] not in seen:
                    tot[band[1]][2] += 1
                    seen.add(band[1])
                pred += s * band[2]
                act += came.get(name, 0)
            per.append((min(pred, 45000), act, x['att']))
    print('按到目标城的格数（守方别的城）：能出的兵（兵 − 5000）合计、实际赶来的援军合计、实测权重、siegeplan 权重')
    for lim, lab, w in BANDS:
        s, c, n = tot[lab]
        print('  %-8s 攻城 %3d 次有这档的城  能出 %8d  来了 %7d  实测 %.3f  现用 %.2f' % (lab, n, s, c, c / max(s, 1), w))
    n = len(per)
    if n:
        pa = sorted(a - b for b, a, _ in per)
        print('每次攻城（%d 次）：预估援军平均 %d，实际 %d；实际 − 预估 中位 %d；实际比预估多 5000 以上 %d 次；'
              '实际援军 > 攻方兵一半 %d 次' % (n, sum(b for b, _, _ in per) // n, sum(a for _, a, _ in per) // n,
                                     pa[n // 2], sum(1 for v in pa if v > 5000), sum(1 for _, a, t in per if a > t / 2)))


if __name__ == '__main__':
    main()
