"""援军（自家城被围时附近的迎击部队）：转撤退的次数、被压着时的结局、城丢没丢。

  python -P ai/research/relief.py ai/runs/<run> [more runs] [--months 12]

1. 迎击转撤退、本队 10 格内有敌方攻城军势在打我方的城：次数、撤时围城敌兵 ÷ 我方军势兵、60 天内丢城。
2. 被压着的援军：迎击部队 10 格内有敌军在攻我方城、围城敌兵 ≥ 我方军势兵 2 倍，这支部队这次出征的结局。
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
    byday = collections.defaultdict(list)
    for r in rows:
        byday[r['日期']].append(r)
    days = sorted(byday, key=dn)
    fh = {f['编号']: (f['名称'], int(f['格'])) for f in o.read_csv(p, 'footholds') if f['类型'] == '城市'}
    falls = [e for e in o.read_csv(p, 'events') if e['事件'] == '城市易主']
    sor = collections.defaultdict(list)
    for s in o.read_csv(p, 'sorties'):
        sor[(s['部队'], s['主将'])].append(s)
    seq = collections.defaultdict(list)
    for x in rows:
        seq[(x['势力'], x['军势'])].append(x)
    res = []
    for k, rs in seq.items():
        bd = collections.OrderedDict()
        for x in rs:
            bd.setdefault(x['日期'], []).append(x)
        ds = list(bd)
        for a, b in zip(ds, ds[1:]):
            if bd[a][0]['军势任务'] != '1' or bd[b][0]['军势任务'] != '0':
                continue
            lead = next((y for y in bd[b] if y['本队'] == '是'), bd[b][0])
            h = int(lead['格'])
            sieg = [y for y in byday[b] if y['势力'] != lead['势力'] and y['军势任务'] in SIEGE and y['军势目标'] in fh
                    and o.hex_distance(fh[y['军势目标']][1], h) <= 10]
            if not sieg:
                continue
            city = collections.Counter(y['军势目标'] for y in sieg).most_common(1)[0][0]
            foes = sum(int(y['兵数']) for y in sieg if y['军势目标'] == city)
            ours = sum(int(y['兵数']) for y in bd[b])
            cname = fh[city][0]
            fell = any(e['对象'] == cname and e['从'] == lead['势力'] and 0 <= dn(e['日期']) - dn(b) <= 60 for e in falls)
            res.append((foes / max(ours, 1), fell))
    out = []
    n = len(res)
    out.append('迎击转撤退、附近有敌军在攻我方城：%d 次；围城敌兵 ÷ 我兵 中位 %s；之后 60 天内丢城 %d 次' % (
        n, ('%.1f' % sorted(r[0] for r in res)[n // 2]) if n else '-', sum(r[1] for r in res)))
    seen = set()
    stat = collections.Counter()
    for d in days:
        for x in byday[d]:
            if x['军势任务'] != '1':
                continue
            k = (x['部队'], x['主将'])
            if k in seen:
                continue
            h = int(x['格'])
            sieg = [y for y in byday[d] if y['势力'] != x['势力'] and y['军势任务'] in SIEGE and y['军势目标'] in fh
                    and o.hex_distance(fh[y['军势目标']][1], h) <= 10]
            if not sieg:
                continue
            city = collections.Counter(y['军势目标'] for y in sieg).most_common(1)[0][0]
            foes = sum(int(y['兵数']) for y in sieg if y['军势目标'] == city)
            ours = sum(int(y['兵数']) for y in byday[d] if y['势力'] == x['势力'] and y['军势'] == x['军势'])
            if foes < 2 * ours:
                continue
            seen.add(k)
            ss = [s for s in sor.get(k, []) if dn(s['出发']) <= dn(d) <= dn(s['结束'])]
            stat[ss[0]['结局'][:4] if ss else '?'] += 1
    out.append('被压着的援军（围城敌兵 ≥ 我方军势 2 倍）的结局：%s' % dict(stat.most_common()))
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
