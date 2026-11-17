"""府加成实测：读 DLL 调试功能 bonus 的日志（每支部队的府加成和所在区域的状态，变了才记一行）。

  python -P ai/research/bonus_log.py logs/ai_dll.log [--from HH:MM:SS]

看同一支部队前后两条记录之间：
  只有「区域里的敌军」变了（同一区域、府主人和涂色格数都没变）：加成变没变；
  敌军踩上府 / 离开府；
  府主人变了、涂色格数变了：加成怎么变。
另列出每种情况的几个例子，以及「区域里有敌军」时的加成和没有时的对比（按府是不是自己的分）。
"""
import collections
import re
import sys

PAT = re.compile(r'(\d\d:\d\d:\d\d)\.\d+ (\d+-\d+-\d+) bonus: army (\d+) force (\d+) hex (\d+) area (\d+) owner (\d+) '
                 r'painted (\d+)/(\d+) foes in area (\d+) on fu (\d+) sum (\d+) x([\d.]+)')


def main():
    a = sys.argv[1:]
    since = a[a.index('--from') + 1] if '--from' in a else None
    log = a[0]
    recs = collections.defaultdict(list)
    with open(log, encoding='utf-8', errors='replace') as f:
        for ln in f:
            m = PAT.match(ln.strip())
            if not m or (since and m.group(1) < since):
                continue
            g = m.groups()
            r = dict(date=g[1], army=int(g[2]), force=int(g[3]), hex=int(g[4]), area=int(g[5]), owner=int(g[6]),
                     painted=int(g[7]), total=int(g[8]), foes=int(g[9]), onfu=int(g[10]), sum=int(g[11]),
                     mult=float(g[12]))
            recs[r['army']].append(r)
    kinds = collections.defaultdict(list)
    for army, rs in recs.items():
        for p, q in zip(rs, rs[1:]):
            if p['force'] != q['force']:
                continue
            same_place = p['area'] == q['area'] and p['hex'] == q['hex']
            same_area = p['area'] == q['area']
            if same_area and p['owner'] == q['owner'] and p['painted'] == q['painted']:
                if p['onfu'] != q['onfu']:
                    k = '敌军踩上府' if q['onfu'] else '敌军离开府'
                elif (p['foes'] > 0) != (q['foes'] > 0):
                    k = '敌军进入区域' if q['foes'] else '敌军离开区域'
                elif p['foes'] != q['foes']:
                    k = '区域里敌军数变了'
                else:
                    k = '同区域其他变化（相邻区域）'
            elif same_area and p['owner'] != q['owner']:
                k = '府易主'
            elif same_area:
                k = '涂色格数变了'
            else:
                k = '换了区域'
            kinds[k].append((p, q, same_place))
    print('记录 %d 条，部队 %d 支' % (sum(len(v) for v in recs.values()), len(recs)))
    for k in ('敌军进入区域', '敌军离开区域', '区域里敌军数变了', '敌军踩上府', '敌军离开府', '府易主', '涂色格数变了',
              '同区域其他变化（相邻区域）'):
        v = kinds.get(k, [])
        if not v:
            print('%s：0 次' % k)
            continue
        ch = sum(1 for p, q, _ in v if p['sum'] != q['sum'] or p['mult'] != q['mult'])
        print('%s：%d 次，其中加成变了 %d 次' % (k, len(v), ch))
        for p, q, _ in v[:4]:
            print('    %s→%s 部队 %d 势力 %d 区域 %d 府主 %d→%d 涂 %d/%d→%d/%d 敌军 %d→%d 踩府 %d→%d 和 %d→%d 倍数 %.3f→%.3f' % (
                p['date'], q['date'], p['army'], p['force'], p['area'], p['owner'], q['owner'], p['painted'],
                p['total'], q['painted'], q['total'], p['foes'], q['foes'], p['onfu'], q['onfu'], p['sum'], q['sum'],
                p['mult'], q['mult']))
    # level comparison: in own-府 areas, with and without enemies in the area
    lv = collections.defaultdict(list)
    for rs in recs.values():
        for r in rs:
            own = '自己的府' if r['owner'] == r['force'] else '别人的府'
            lv[(own, '有敌军' if r['foes'] else '无敌军')].append(r['mult'])
    for key in sorted(lv):
        v = lv[key]
        print('%s、区域里%s：%d 条，倍数平均 %.3f' % (key[0], key[1], len(v), sum(v) / len(v)))


if __name__ == '__main__':
    main()
