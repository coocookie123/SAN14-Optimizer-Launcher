"""每次攻城出兵带了多少：按势力编号奇偶分组（A/B 局里奇数一方开着攻城出兵编成）。

  python -P ai/research/siege_dep.py ai/runs/<run> [more runs]

一次出兵＝同一势力、同一目标、4 天内第一次以攻城任务出现、出现时在自家城 3 格内的部队。看每次几座城出发、
几支部队、多少兵、3000 兵以下几支、雁行几支、多支时兵最多的一支占多少。
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


def analyse(p):
    rows = o.read_csv(p, 'armies')
    byday = collections.defaultdict(list)
    for r in rows:
        byday[r['日期']].append(r)
    days = sorted(byday, key=dn)
    cities = {f['名称']: int(f['格']) for f in o.read_csv(p, 'footholds') if f['类型'] == '城市'}
    fid = {}
    for f in o.read_csv(p, 'forces'):
        fid[f['君主']] = int(f['势力'])
    first = {}
    for d in days:
        for x in byday[d]:
            if x['军势任务'] in SIEGE:
                first.setdefault((x['部队'], x['主将']), x)
    out = []
    for grp in (1, 0):
        dep = collections.defaultdict(lambda: [set(), 0, 0, 0, 0, []])
        for x in first.values():
            if x['势力'] not in fid or fid[x['势力']] % 2 != grp:
                continue
            h = int(x['格'])
            c = min(cities, key=lambda n: o.hex_distance(h, cities[n]))
            if o.hex_distance(h, cities[c]) > 3:
                continue
            e = dep[(x['势力'], x['军势目标'], dn(x['日期']) // 4)]
            s = int(x['兵数'])
            e[0].add(c)
            e[1] += 1
            e[2] += s
            e[3] += s <= 3000
            e[4] += x['阵型'] == '雁行'
            e[5].append(s)
        n = len(dep)
        if not n:
            continue
        top = [max(v[5]) / sum(v[5]) for v in dep.values() if len(v[5]) >= 2]
        out.append('%s：攻城出兵 %d 次，每次 %.2f 座城、%.2f 支、%d 兵；3000 兵以下 %.2f 支、雁行 %.2f 支；多支时最大一支占 %d%%' % (
            '奇数' if grp else '偶数', n, sum(len(v[0]) for v in dep.values()) / n, sum(v[1] for v in dep.values()) / n,
            sum(v[2] for v in dep.values()) / n, sum(v[3] for v in dep.values()) / n, sum(v[4] for v in dep.values()) / n,
            100 * sum(top) / max(1, len(top))))
    return out


def main():
    for p in sys.argv[1:]:
        print('== %s' % os.path.basename(p.rstrip('/\\')))
        for ln in analyse(p):
            print('  ' + ln)


if __name__ == '__main__':
    main()
