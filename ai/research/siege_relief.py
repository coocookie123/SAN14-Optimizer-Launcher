"""攻城时守方周边城的援军：每个攻城军势（攻城任务、本队到过目标城 8 格内），看守方从别的城派来的部队（出发时离
目标城最近的城不是目标城、在攻城期间到过目标城 8 格内），和攻城结果。

  python -P ai/research/siege_relief.py ai/runs/<run> [more runs] [--months 12]

按「有没有援军」分组：攻城军势个数、拿下目标城的比例、攻城军势转撤退 / 回城的比例、出发时攻方兵 ÷ 目标城兵、
援军兵 ÷ 攻方兵。援军来自的城离目标城多远（城到城的格数）。
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402

SIEGE = {'2', '8', '9', '10', '22'}


def dn(d):
    return o.day_number(o.parse_date(d))


def analyse(p, months):
    rows = o.read_csv(p, 'armies')
    days = sorted({r['日期'] for r in rows}, key=dn)
    lo = dn(days[0])
    rows = [r for r in rows if not months or dn(r['日期']) < lo + months * 31]
    cities = {f['编号']: (f['名称'], int(f['格'])) for f in o.read_csv(p, 'footholds') if f['类型'] == '城市'}
    chex = {n: h for n, h in cities.values()}
    hist = collections.defaultdict(list)
    for c in o.read_csv(p, 'cities'):
        hist[c['城市']].append((dn(c['日期']), c['势力'], int(c['兵力'])))
    falls = collections.defaultdict(list)
    for e in o.read_csv(p, 'events'):
        if e['事件'] == '城市易主':
            falls[e['对象']].append((dn(e['日期']), e['到']))

    def garrison(name, d):
        best = None
        for dd, f, s in hist.get(name, []):
            if dd <= d:
                best = (f, s)
        return best or (hist[name][0][1:] if hist.get(name) else (None, 0))
    # sorties: (slot, general) -> first row, all rows
    trips = collections.defaultdict(list)
    for r in rows:
        trips[(r['部队'], r['主将'], r['势力'])].append(r)
    nearest = lambda h: min(chex, key=lambda n: o.hex_distance(chex[n], h))  # noqa: E731
    # siege troops: (force, troops id, target, first day)
    sieges = {}
    for r in rows:
        if r['军势任务'] in SIEGE and r['部队目标'].startswith('城市'):
            c = cities.get(r['部队目标'][2:])
            if not c:
                continue
            k = (r['势力'], r['军势'], c[0])
            s = sieges.setdefault(k, dict(first=dn(r['日期']), last=dn(r['日期']), near=False, sold=0, armies=set(),
                                          retreat=False))
            s['last'] = max(s['last'], dn(r['日期']))
            if o.hex_distance(int(r['格']), c[1]) <= 8:
                s['near'] = True
            if (r['部队'], r['主将']) not in s['armies']:
                s['armies'].add((r['部队'], r['主将']))
                s['sold'] += int(r['兵数'])
    for r in rows:                                # did the troops turn to retreat / go home
        for (f, t, c), s in sieges.items():
            pass
    groups = collections.defaultdict(lambda: [0, 0, [], [], []])
    for (f, t, cname), s in sieges.items():
        if not s['near']:
            continue
        defender, gs = garrison(cname, s['first'])
        if not defender or defender == f:
            continue
        ch = chex[cname]
        relief = 0
        rcities = []
        for (slot, gen, force), tr in trips.items():
            if force != defender:
                continue
            days_ = [dn(x['日期']) for x in tr]
            if days_[-1] < s['first'] or days_[0] > s['last'] + 3:
                continue
            if not any(o.hex_distance(int(x['格']), ch) <= 8 and s['first'] <= dn(x['日期']) <= s['last'] + 3 for x in tr):
                continue
            home = nearest(int(tr[0]['格']))
            if home == cname or tr[0]['军势任务'] in SIEGE or tr[0]['军势任务'] == '12':
                continue
            relief += max(int(x['兵数']) for x in tr)
            rcities.append(o.hex_distance(chex[home], ch))
        took = any(s['first'] <= dd <= s['last'] + 5 and to == f for dd, to in falls.get(cname, []))
        g = groups['有援军' if relief else '没援军']
        g[0] += 1
        g[1] += took
        g[2].append(s['sold'] / max(gs, 1))
        if relief:
            g[3].append(relief / max(s['sold'], 1))
            g[4] += rcities
    out = []
    for k in ('没援军', '有援军'):
        n, took, r1, r2, rc = groups[k]
        if not n:
            continue
        med = lambda v: sorted(v)[len(v) // 2] if v else 0  # noqa: E731
        out.append('%s：攻城军势 %d 个，拿下 %d（%d%%）；攻方兵 ÷ 目标城兵 中位 %.2f%s' % (
            k, n, took, 100 * took // n, med(r1),
            '；援军兵 ÷ 攻方兵 中位 %.2f，援军出发城离目标城中位 %d 格' % (med(r2), med(rc)) if r2 else ''))
    return out


def main():
    args = sys.argv[1:]
    months = 12
    if '--months' in args:
        i = args.index('--months')
        months = int(args[i + 1])
        del args[i:i + 2]
    for p in args:
        print('== %s' % os.path.basename(p.rstrip('/\\')))
        for ln in analyse(p, months):
            print('  ' + ln)


if __name__ == '__main__':
    main()
