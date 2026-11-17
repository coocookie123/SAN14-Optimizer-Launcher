"""占府（DLL fu）每条命令之后怎样：把 DLL 日志里的「fu: army 甲 -> 府 hex H」和「took / gave up」对上看海记录。

  python -P ai/research/fu_orders.py logs/ai_dll.log ai/runs/<run> [--from HH:MM:SS]

每条命令看：踩到没有、用了几天；派去的部队 15 天内有没有溃灭；30 天后那个区域的府还是不是它的势力
（从站在那个区域的部队的「府势力」列看，没人站着就看不出）。按顺路 / 先行分开。
"""
import collections
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402

ORDER = re.compile(r'(\d\d:\d\d:\d\d)\.\d+ (\d+)-(\d+)-(\d+) fu: army (\d+) \(force (\d+), formation (\d+), task (\d+)\) '
                   r'-> 府 hex (\d+) \(area (\d+), owner (\d+)\), distance (\d+), (on the way|ahead)')
END = re.compile(r'(\d\d:\d\d:\d\d)\.\d+ (\d+)-(\d+)-(\d+) fu: army (\d+) \(force (\d+)\) (took|stepped on|gave up) 府 hex (\d+) '
                 r'\(area (\d+)\) after (\d+) days')


def dn(d):
    return o.day_number(o.parse_date(d))


def ds(y, m, d):
    return '%d-%02d-%02d' % (int(y), int(m), int(d))


def main():
    a = sys.argv[1:]
    since = a[a.index('--from') + 1] if '--from' in a else None
    a = [x for i, x in enumerate(a) if x != '--from' and (i == 0 or a[i - 1] != '--from')]
    log, run = a[0], a[1]
    orders, ends = [], []
    with open(log, encoding='utf-8', errors='replace') as f:
        for ln in f:
            ln = ln.strip()
            m = ORDER.match(ln)
            if m and not (since and m.group(1) < since):
                g = m.groups()
                orders.append(dict(day=ds(*g[1:4]), army=g[4], force=int(g[5]), form=int(g[6]), task=int(g[7]),
                                   hex=int(g[8]), area=g[9], dist=int(g[11]), kind='顺路' if g[12] == 'on the way' else '先行'))
            m = END.match(ln)
            if m and not (since and m.group(1) < since):
                g = m.groups()
                ends.append(dict(day=ds(*g[1:4]), army=g[4], took=g[6] in ("took", "stepped on"), hex=int(g[7]), days=int(g[9])))
    rows = o.read_csv(run, 'armies')
    by = {(x['日期'], x['部队']): x for x in rows}
    byday = collections.defaultdict(list)
    for x in rows:
        byday[x['日期']].append(x)
    days = sorted(byday, key=dn)
    sorties = collections.defaultdict(list)
    for s in o.read_csv(run, 'sorties'):
        sorties[(s['部队'], s['主将'])].append(s)
    lords = {}
    for f in o.read_csv(run, 'forces'):
        lords[int(f['势力'])] = f['君主']
    stat = collections.defaultdict(lambda: collections.Counter())
    took_days = collections.defaultdict(list)
    for od in orders:
        k = od['kind']
        st = stat[k]
        st['命令'] += 1
        end = next((e for e in ends if e['army'] == od['army'] and e['hex'] == od['hex'] and dn(e['day']) >= dn(od['day'])),
                   None)
        if end is None:
            st['没结束（记录到头或被别的命令换掉）'] += 1
        elif end['took']:
            st['踩到'] += 1
            took_days[k].append(end['days'])
        else:
            st['放弃'] += 1
        x = by.get((od['day'], od['army']))
        if x:
            wiped = any(s['结局'].startswith('溃灭') and dn(s['出发']) <= dn(od['day']) <= dn(s['结束']) and
                        dn(s['结束']) - dn(od['day']) <= 15 for s in sorties.get((x['部队'], x['主将']), []))
            st['15 天内溃灭'] += wiped
        lord = lords.get(od['force'])
        later = [y for dd in days if 25 <= dn(dd) - dn(od['day']) <= 35 for y in byday[dd] if y['区域'] == od['area']]
        if later and lord:
            st['30 天后府还是自己的'] += any(y['府势力'] == lord for y in later)
            st['30 天后看得出'] += 1
    for k in ('顺路', '先行'):
        if not stat[k]:
            continue
        st = stat[k]
        td = sorted(took_days[k])
        print('%s：%s；踩到用时中位 %s 天' % (k, '，'.join('%s %d' % kv for kv in st.items()), td[len(td) // 2] if td else '-'))


if __name__ == '__main__':
    main()
