"""攻城军势的编成：每个军势几支部队、每支多少兵、兵最多的一支占多少、阵型，按势力编号奇偶分组（A/B 局里奇数一方开着
攻城出兵编成）。

  python -P ai/research/siege_comp.py ai/runs/<run> [more runs] [--months 12]

军势按（势力，军势编号）第一次以攻城任务出现那天算一次出兵，取那天起 8 天内见到的部队。
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


def analyse(path, months):
    rows = o.read_csv(path, 'armies')
    days = sorted({r['日期'] for r in rows}, key=dn)
    lo = dn(days[0])
    rows = [r for r in rows if not months or dn(r['日期']) < lo + months * 31]
    byday = collections.defaultdict(list)
    for r in rows:
        byday[r['日期']].append(r)
    days = sorted(byday, key=dn)
    idx = {d: i for i, d in enumerate(days)}
    fid = {}
    for f in o.read_csv(path, 'forces'):
        fid[f['君主']] = int(f['势力'])
    first = {}
    for d in days:
        for x in byday[d]:
            if x['军势任务'] in SIEGE:
                first.setdefault((x['势力'], x['军势'], x['军势目标']), d)
    g = collections.defaultdict(lambda: dict(n=[], sold=[], top=[], forms=collections.Counter(), small=0, total=[]))
    for (f, t, goal), d0 in first.items():
        if f not in fid:
            continue
        seen = {}
        for d in days[idx[d0]:idx[d0] + 8]:
            for x in byday[d]:
                if x['势力'] == f and x['军势'] == t:
                    seen.setdefault((x['部队'], x['主将']), x)
        if not seen:
            continue
        v = list(seen.values())
        s = [int(x['兵数']) for x in v]
        e = g['奇数' if fid[f] % 2 else '偶数']
        e['n'].append(len(v))
        e['sold'] += s
        e['total'].append(sum(s))
        if len(v) >= 2:
            e['top'].append(max(s) / sum(s))
        e['small'] += sum(1 for x in s if x <= 3000)
        for x in v:
            e['forms'][x['阵型']] += 1
    # one departure: same force, same target, troops first seen within 3 days of each other
    dep = collections.defaultdict(list)
    for (f, t, goal), d0 in sorted(first.items(), key=lambda kv: dn(kv[1])):
        lst = dep[(f, goal)]
        if lst and dn(d0) - dn(lst[-1][0]) <= 3:
            lst[-1][1].append(t)
        else:
            lst.append((d0, [t]))
    dg = collections.defaultdict(lambda: [0, 0, 0, 0])     # group: departures, armies, soldiers, troops
    for (f, goal), lst in dep.items():
        if f not in fid:
            continue
        for d0, ts in lst:
            seen = {}
            for d in days[idx[d0]:idx[d0] + 8]:
                for x in byday[d]:
                    if x['势力'] == f and x['军势'] in ts:
                        seen.setdefault((x['部队'], x['主将']), int(x['兵数']))
            e = dg['奇数' if fid[f] % 2 else '偶数']
            e[0] += 1
            e[1] += len(seen)
            e[2] += sum(seen.values())
            e[3] += len(ts)
    out = []
    for k in ('奇数', '偶数'):
        e = dg[k]
        if e[0]:
            out.append('%s：按「同一目标、3 天内出发」合起来算 %d 次出兵，每次 %.2f 个军势、%.2f 支部队、%d 兵' % (
                k, e[0], e[3] / e[0], e[1] / e[0], e[2] / e[0]))
    for k in ('奇数', '偶数'):
        e = g[k]
        if not e['n']:
            continue
        n = len(e['n'])
        c = collections.Counter(min(x, 5) for x in e['n'])
        out.append('%s：攻城军势 %d 个，平均 %.2f 支（1 支 %d%%，2 支 %d%%，3 支以上 %d%%），每个军势合计 %d 兵，每支平均 %d 兵；'
                   '兵最多的一支占 %d%%；3000 兵以下 %d 支；阵型 %s' % (
                       k, n, sum(e['n']) / n, 100 * c[1] // n, 100 * c[2] // n, 100 * (c[3] + c[4] + c[5]) // n,
                       sum(e['total']) / n, sum(e['sold']) / max(1, len(e['sold'])),
                       100 * sum(e['top']) / max(1, len(e['top'])), e['small'],
                       '、'.join('%s %d' % kv for kv in e['forms'].most_common(8))))
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
