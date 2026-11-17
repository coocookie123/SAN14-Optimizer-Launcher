"""出兵的部队数量：同时在外多少支、每个军势带几支、各占武将和城内兵力的多少。按任务分（迎击、攻城、区域攻击、运输）。

  python -P ai/research/army_counts.py ai/runs/<run> [more runs] [--months 12]

军势按（势力，军势编号，军势任务，第一次出现那天）分开：同一编号以后再用算新的军势。军势的部队数取它出现以后
10 天内同时在外的最多支数（陆续出城的也算进去）。
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402

KIND = {'1': '迎击', '2': '攻城', '8': '攻城', '9': '攻城', '10': '攻城', '22': '攻城', '3': '区域攻击', '13': '区域攻击',
        '14': '区域攻击', '15': '区域攻击', '16': '区域攻击', '21': '区域攻击', '12': '运输', '5': '建设'}


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
    forces = [f for f in o.read_csv(path, 'forces') if f['异民族'] != '是' and int(f['城市'] or 0) > 0]
    fdays = sorted({f['日期'] for f in forces}, key=dn)
    gen = sum(int(f['武将']) for f in forces) / max(1, len(fdays))
    city = sum(int(f['城内兵力']) for f in forces) / max(1, len(fdays))
    field = [len(byday[d]) for d in days]
    fsold = [sum(int(x['兵数']) for x in byday[d]) for d in days]
    nontr = [sum(1 for x in byday[d] if x['军势任务'] != '12') for d in days]
    out = ['同时在外的部队：平均 %.0f 支（不算运输 %.0f 支），兵 %.0f 万；同期武将平均 %.0f 名、城内兵 %.0f 万；在外部队占武将 %.0f%%' % (
        sum(field) / len(field), sum(nontr) / len(nontr), sum(fsold) / len(fsold) / 1e4, gen, city / 1e4,
        100 * sum(field) / len(field) / max(gen, 1))]
    # troops (军势)
    first = {}
    members = collections.defaultdict(lambda: collections.defaultdict(set))
    for d in days:
        for x in byday[d]:
            key0 = (x['势力'], x['军势'], x['军势任务'])
            k = first.get(key0)
            if k is None or dn(d) - dn(k[3]) > 60 and not members[k][d]:
                k = key0 + (d,)
                first[key0] = k
            if dn(d) - dn(k[3]) <= 10:
                members[k][d].add((x['部队'], x['主将']))
    per = collections.defaultdict(list)
    sold = collections.defaultdict(list)
    for k, dd in members.items():
        n = max(len(v) for v in dd.values())
        per[KIND.get(k[2], '其他')].append(n)
    for kind in ('迎击', '攻城', '区域攻击', '运输', '建设', '其他'):
        v = per.get(kind)
        if not v:
            continue
        c = collections.Counter(min(n, 5) for n in v)
        out.append('%s军势 %d 个，平均 %.2f 支部队；1 支 %d%%，2 支 %d%%，3 支 %d%%，4 支 %d%%，5 支以上 %d%%' % (
            kind, len(v), sum(v) / len(v), *(100 * c[i] // len(v) for i in (1, 2, 3, 4, 5))))
    # big forces: per force, average armies out (non-transport) vs generals
    pf = collections.defaultdict(list)
    for d in days:
        c = collections.Counter(x['势力'] for x in byday[d] if x['军势任务'] != '12')
        for f, n in c.items():
            pf[f].append(n)
    gens = collections.defaultdict(list)
    for f in forces:
        gens[f['君主']].append(int(f['武将']))
    top = sorted(gens, key=lambda f: -sum(gens[f]) / len(gens[f]))[:5]
    out.append('大势力（不算运输）同时在外：' + '；'.join('%s %.1f 支 / %.0f 将' % (f, sum(pf[f]) / len(days), sum(gens[f]) / len(gens[f]))
                                            for f in top))
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
