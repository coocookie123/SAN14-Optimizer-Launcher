"""攻城时港口被守方占走：港口格（地形 18）都是所在区域的府格，占港口就是占这个区域的府（区域易主、攻方补给可能断）。

  python -P ai/research/port_cut.py ai/runs/<run> [more runs]

每天按 fu.csv 看港口区域的府归属；某港口从势力甲变成乙，而当天（或前一天）甲有攻城军势的部队在乙的一座城 12 格内、
这港口离那座城 10 格内，算一次「攻城时港口被占」。另列：攻方当时有没有部队在港口 2 格内（有没有人留守），被占后
10 天内那支攻城军势的士气变化、之后 30 天内拿没拿下那座城。对照：同样的港口在攻城期间没被占的天数。
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402

TER = open(os.path.join(HERE, 'research', 'terrain_exce.bin'), 'rb').read()
SIEGE = {'2', '8', '9', '10', '22'}


def dn(d):
    return o.day_number(o.parse_date(d))


def analyse(p):
    rows = o.read_csv(p, 'armies')
    byday = collections.defaultdict(list)
    for r in rows:
        byday[r['日期']].append(r)
    days = sorted(byday, key=dn)
    idx = {d: i for i, d in enumerate(days)}
    fu = collections.defaultdict(dict)                 # day -> area -> owner
    port = {}                                          # area -> hex
    for r in o.read_csv(p, 'fu'):
        h = int(r['府格'])
        if TER[h] == 18:
            port[r['区域']] = h
            fu[r['日期']][r['区域']] = r['府势力']
    cities = {f['编号']: (f['名称'], int(f['格'])) for f in o.read_csv(p, 'footholds') if f['类型'] == '城市'}
    owner = collections.defaultdict(dict)
    for c in o.read_csv(p, 'cities'):
        owner[dn(c['日期'])][c['城市']] = c['势力']
    falls = {}
    for e in o.read_csv(p, 'events'):
        if e['事件'] == '城市易主':
            falls.setdefault(e['对象'], []).append((dn(e['日期']), e['到']))
    out = []
    events = []
    guarded_days = unguarded_days = 0
    prev = None
    for d in days:
        if d not in fu:
            continue
        cur = fu[d]
        sieges = collections.defaultdict(list)          # attacker -> [(city name, city hex, troops)]
        for x in byday[d]:
            if x['军势任务'] in SIEGE and x['部队目标'].startswith('城市'):
                c = cities.get(x['部队目标'][2:])
                if c and o.hex_distance(int(x['格']), c[1]) <= 12:
                    sieges[x['势力']].append((c[0], c[1], x['军势']))
        for ar, h in port.items():
            own = cur.get(ar)
            for att, lst in sieges.items():
                near = [s for s in lst if o.hex_distance(h, s[1]) <= 10]
                if not near or own != att:
                    continue
                guard = any(y['势力'] == att and o.hex_distance(int(y['格']), h) <= 2 for y in byday[d])
                guarded_days += guard
                unguarded_days += not guard
        if prev:
            for ar, h in port.items():
                a, b = fu[prev].get(ar), cur.get(ar)
                if not a or not b or a == b:
                    continue
                lst = [s for s in sieges.get(a, []) if o.hex_distance(h, s[1]) <= 10]
                if not lst:
                    continue
                cname, ch, troops = lst[0]
                guard = any(y['势力'] == a and o.hex_distance(int(y['格']), h) <= 2 for y in byday[prev])
                taker = [y for y in byday[d] if y['势力'] == b and int(y['格']) == h]
                i = idx[d]

                def mor(k):
                    if i + k >= len(days):
                        return None
                    xs = [int(y['士气']) for y in byday[days[i + k]] if y['势力'] == a and y['军势'] == troops]
                    return sum(xs) / len(xs) if xs else None
                m0, m10 = mor(0), mor(10)
                took = any(dd >= dn(d) - 5 and dd <= dn(d) + 30 and to == a for dd, to in falls.get(cname, []))
                events.append((d, a, b, h, cname, o.hex_distance(h, ch), guard,
                               taker[0]['主将'] + '/' + taker[0]['军势任务'] if taker else '-', m0, m10, took))
        prev = d
    # daily port changes (observe.py records 府易主 since 2026-11-13); they replace the monthly guess above
    daily = [e for e in o.read_csv(p, 'events') if e['事件'] == '府易主' and e['对象'].endswith(':港')]
    if daily:
        events = []
        for e in daily:
            d = e['日期']
            if d not in idx or idx[d] == 0:
                continue
            pd = days[idx[d] - 1]
            h = int(e['对象'].split(':')[1])
            a, b = e['从'], e['到']
            sg = collections.defaultdict(list)
            for x in byday[pd]:
                if x['军势任务'] in SIEGE and x['部队目标'].startswith('城市'):
                    c = cities.get(x['部队目标'][2:])
                    if c and o.hex_distance(int(x['格']), c[1]) <= 12 and o.hex_distance(h, c[1]) <= 10:
                        sg[x['势力']].append((c[0], c[1], x['军势']))
            if not sg.get(a):
                continue
            cname, ch, troops = sg[a][0]
            guard = any(y['势力'] == a and o.hex_distance(int(y['格']), h) <= 2 for y in byday[pd])
            taker = [y for y in byday[d] if y['势力'] == b and o.hex_distance(int(y['格']), h) <= 1]
            i = idx[d]

            def mor(k):
                if i + k >= len(days):
                    return None
                xs = [int(y['士气']) for y in byday[days[i + k]] if y['势力'] == a and y['军势'] == troops]
                return sum(xs) / len(xs) if xs else None
            took = any(dn(d) - 5 <= dd <= dn(d) + 30 and to == a for dd, to in falls.get(cname, []))
            events.append((d, a, b, h, cname, o.hex_distance(h, ch), guard,
                           taker[0]['主将'] + '/' + taker[0]['军势任务'] if taker else '-', mor(0), mor(10), took))
    out.append('攻城期间（攻方部队在敌城 12 格内、港口离那城 10 格内）攻方自己的港口：有人在 2 格内留守 %d 天，没人 %d 天' % (
        guarded_days, unguarded_days))
    out.append('攻城时港口被对方占走 %d 次：' % len(events))
    for e in events:
        out.append('  %s %s 的港口（格 %d，离 %s %d 格）被 %s 占（%s）；被占前一天 2 格内有人留守：%s；攻方士气 %s → 10 天后 %s；30 天内拿下 %s：%s' % (
            e[0], e[1], e[3], e[4], e[5], e[2], e[7], '有' if e[6] else '无', '%.0f' % e[8] if e[8] else '-',
            '%.0f' % e[9] if e[9] else '-', e[4], '是' if e[10] else '否'))
    return out


def main():
    for p in sys.argv[1:]:
        print('== %s' % os.path.basename(p.rstrip('/\\')))
        for ln in analyse(p):
            print('  ' + ln)


if __name__ == '__main__':
    main()
