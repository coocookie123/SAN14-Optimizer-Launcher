"""攻城时目标城周边的府：能踩的有几个、全踩掉守方府分能压到多少（2026-11-14，用户：「先占周边府」）。

  python -P ai/research/siege_fu.py ai/runs/<run> [more runs] [--months 12] [--list] [--side odd|even]

每次攻城（siege_force.py 的分法），到城下那天：
  站位格 = 目标城 3 格内的格子（不含城）；守方府分 = 站位格所在区域和相邻区域里府属于守方的、按涂色占比加起来，取平均；
  算进去的守方府里，府格是普通府（地形 15）或港（18）的算「能踩」（城 16、关 17 踩不了）；
  全踩掉：把这些府的那一份去掉后的守方府分；
  离城多远：能踩的府离目标城的格数；
  到城下前 30 天里攻方踩下（府易主给攻方）的、在这片里的府。
地图表 research/area_exce.bin（每格的区域，u16 × 220 × 220）、terrain_exce.bin（每格地形），从游戏数据导出，不进仓库。
"""
import collections
import os
import struct
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import observe as o  # noqa: E402
import siege_force as F  # noqa: E402

RES = os.path.dirname(os.path.abspath(__file__))
AREA = struct.unpack('<48400H', open(os.path.join(RES, 'area_exce.bin'), 'rb').read())
TERR = open(os.path.join(RES, 'terrain_exce.bin'), 'rb').read()
STAND = 3
TAKEABLE = {15, 18}


def ring(c, r):
    y, x = divmod(c, 220)
    out = []
    for dy in range(-r - 1, r + 2):
        for dx in range(-r - 1, r + 2):
            yy, xx = y + dy, x + dx
            if 0 <= yy < 220 and 0 <= xx < 220:
                h = yy * 220 + xx
                if h != c and o.hex_distance(c, h) <= r:
                    out.append(h)
    return out


def analyse(p, months):
    ops = F.analyse(p, months)
    if not os.path.exists(os.path.join(p, 'fu.csv')):
        return []
    fu = F.Fu(p)
    fuhex = {a['区域']: int(a['府格']) for a in o.read_csv(p, 'areas')}
    city = {f['名称']: int(f['格']) for f in o.read_csv(p, 'footholds') if f['类型'] == '城市'}
    ev = [(F.dn(e['日期']), e['对象'].split(':')[0], e['到']) for e in o.read_csv(p, 'events') if e['事件'] == '府易主']
    out = []
    for x in ops:
        if not x['arrive']:
            continue
        c = city[x['city']]
        day = x['arrive']
        hexes = ring(c, STAND)
        zone = collections.Counter()      # area -> how many stand hexes count it
        for h in hexes:
            a = str(AREA[h])
            for b in [a] + fu.adj.get(a, []):
                zone[b] += 1
        cur = after = 0.0
        take = {}
        for b, k in zone.items():
            w, share = fu.owner(b, day)
            if w != x['owner']:
                continue
            cur += k * share
            fh = fuhex.get(b)
            if fh is not None and TERR[fh] in TAKEABLE:
                take[b] = (o.hex_distance(c, fh), k * share / len(hexes))
            else:
                after += k * share
        cur /= len(hexes)
        after /= len(hexes)
        took_before = [b for d, b, to in ev if to == x['force'] and day - 30 <= d <= day and b in zone]
        out.append(dict(x, cur=cur, after=after, take=take, took_before=took_before))
    return out


def main():
    args = sys.argv[1:]
    months = 12
    if '--months' in args:
        i = args.index('--months')
        months = int(args[i + 1])
        del args[i:i + 2]
    listing = '--list' in args
    side = args[args.index('--side') + 1] if '--side' in args else None
    if side:
        del args[args.index('--side'):args.index('--side') + 2]
    args = [a for a in args if a != '--list']
    allx = []
    for p in args:
        xs = analyse(p, months)
        if side:
            xs = [x for x in xs if x['fid'] % 2 == (1 if side == 'odd' else 0)]
        allx += xs
        if listing:
            print('== %s' % os.path.basename(p.rstrip('/\\')))
            for x in xs:
                print('   %s→%s %s：守方府分 %.1f → 全踩 %.1f；能踩 %d 个（离城 %s）；到城下前 30 天攻方已踩 %d；%s' % (
                    x['force'], x['city'], x['date'], x['cur'], x['after'], len(x['take']),
                    ','.join(str(d) for d, _ in sorted(x['take'].values())), len(x['took_before']),
                    '拿下' if x['took'] else '没拿下'))
    if not allx:
        return
    n = len(allx)
    print('\n到城下的攻城 %d 次（有府数据）：' % n)
    print('  站位格上守方府分平均 %.2f；能踩的守方府全踩掉后 %.2f（城、关那份去不掉）' % (
        sum(x['cur'] for x in allx) / n, sum(x['after'] for x in allx) / n))
    ks = collections.Counter(min(len(x['take']), 6) for x in allx)
    print('  每次能踩的守方府个数：' + '，'.join('%s 个 %d 次' % ('6+' if k == 6 else k, ks[k]) for k in sorted(ks)))
    for R in (4, 6, 8, 10):
        gain = [sum(v for d, v in x['take'].values() if d <= R) for x in allx]
        cnt = [sum(1 for d, v in x['take'].values() if d <= R) for x in allx]
        print('  只踩离城 %2d 格内的：平均 %.1f 个，守方府分平均降 %.2f' % (R, sum(cnt) / n, sum(gain) / n))
    tb = sum(len(x['took_before']) for x in allx)
    print('  到城下前 30 天攻方已经在这片踩下的府：%d 个（%d 次攻城里有）' % (tb, sum(1 for x in allx if x['took_before'])))
    for lab, grp in (('拿下', [x for x in allx if x['took']]), ('没拿下', [x for x in allx if not x['took']])):
        if grp:
            print('  %s %d 次：守方府分 %.2f，全踩后 %.2f，能踩 %.1f 个' % (
                lab, len(grp), sum(x['cur'] for x in grp) / len(grp), sum(x['after'] for x in grp) / len(grp),
                sum(len(x['take']) for x in grp) / len(grp)))


if __name__ == '__main__':
    main()
