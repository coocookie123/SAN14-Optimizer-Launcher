"""占府第四版「围城先占府」的命令怎样了（2026-11-14）：读 DLL 日志里 (siege zone) 的派出、踩上、放弃，对上看海记录。

  python -P ai/research/fu_zone.py ai/runs/<run> [more runs]      # 每局目录里要有 ai_dll.log

每条命令：踩上没有、用了几天、派出时离府几格；踩上后 15 天内府有没有易主给它（events.csv 的府易主）；
派出去的部队 15 天内有没有溃灭（sorties.csv，按部队编号和势力对上、出征期间包含派出那天）。
"""
import collections
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402

ORDER = re.compile(r'(\d+)-(\d+)-(\d+) fu: army (\d+) \(force (\d+), formation (\d+)\) of siege troops (\d+) \(target city (\d+), '
                   r'lead (\d+) hexes away\) -> 府 hex (\d+) \(area (\d+), owner (\d+), worth ([\d.]+)\), distance (\d+) \(siege zone\)')
END = re.compile(r'(\d+)-(\d+)-(\d+) fu: army (\d+) \(force (\d+)\) (stepped on|gave up) 府 hex (\d+) \(area (\d+)\) after (\d+) days '
                 r'\(siege zone\)')


def dn(y, m, d):
    return o.day_number((int(y), int(m), int(d)))


def main():
    tot = collections.Counter()
    for p in sys.argv[1:]:
        names = {int(f['势力']): f['君主'] for f in o.read_csv(p, 'forces')}
        fu_ev = collections.defaultdict(list)
        for e in o.read_csv(p, 'events'):
            if e['事件'] == '府易主':
                fu_ev[e['对象'].split(':')[0]].append((o.day_number(o.parse_date(e['日期'])), e['到']))
        sorties = o.read_csv(p, 'sorties')
        orders, ends = [], {}
        for ln in open(os.path.join(p, 'ai_dll.log'), encoding='utf-8', errors='replace'):
            m = ORDER.search(ln)
            if m:
                g = m.groups()
                orders.append(dict(day=dn(*g[:3]), army=int(g[3]), force=int(g[4]), hex=int(g[9]), area=g[10],
                                   lead=int(g[8]), worth=float(g[12]), dist=int(g[13])))
                continue
            m = END.search(ln)
            if m:
                g = m.groups()
                ends.setdefault((int(g[3]), int(g[6])), []).append((dn(*g[:3]), g[5], int(g[8])))
        c = collections.Counter()
        days, dists = [], []
        for x in orders:
            c['派出'] += 1
            c['奇数势力' if x['force'] % 2 else '偶数势力'] += 1
            dists.append(x['dist'])
            end = next((e for e in ends.get((x['army'], x['hex']), []) if e[0] >= x['day']), None)
            if end is None:
                c['没结束'] += 1
            elif end[1] == 'stepped on':
                c['踩上'] += 1
                days.append(end[2])
                name = names.get(x['force'])
                if any(end[0] <= d <= end[0] + 15 and to == name for d, to in fu_ev.get(x['area'], [])):
                    c['之后府易主给它'] += 1
            else:
                c['放弃'] += 1
            name = names.get(x['force'])
            for s in sorties:
                if s['势力'] != name or int(s['部队']) != x['army']:
                    continue
                a, b = o.day_number(o.parse_date(s['出发'])), o.day_number(o.parse_date(s['结束']))
                if a <= x['day'] <= b:
                    if s['结局'].startswith('溃灭') and b - x['day'] <= 15:
                        c['派出后 15 天内溃灭'] += 1
                    break
        tot.update(c)
        print('== %s：%s；踩上用时中位 %s 天；派出时离府中位 %s 格' % (
            os.path.basename(p.rstrip('/\\')), '，'.join('%s %d' % kv for kv in c.items()),
            sorted(days)[len(days) // 2] if days else '-', sorted(dists)[len(dists) // 2] if dists else '-'))
    print('合计：' + '，'.join('%s %d' % kv for kv in tot.items()))


if __name__ == '__main__':
    main()
