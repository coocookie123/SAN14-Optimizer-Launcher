"""半渡而击（DLL shore）的效果：派去岸边的部队有没有贴上还在水上的敌军，贴上后双方每天掉多少兵。

  python -P ai/research/shore_check.py <ai_dll.log> ai/runs/<run>

每条「shore: army 甲 ... against army 乙」：之后 12 天里，甲在陆上、乙在水面、两者相邻的日子算「岸边贴上」，
记下这些天两边的掉兵（第二天兵数差）；另记乙之后有没有上岸、甲有没有下水、甲这次出征的结局。
地形表见 naval_stats.py。
"""
import collections
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402

WATER = {14, 24}
TER = open(os.path.join(HERE, 'research', 'terrain_exce.bin'), 'rb').read()
PAT = re.compile(r'(\d+)-(\d+)-(\d+) shore: army (\d+) \(force (\d+), formation (\d+), (\d+) soldiers\) -> hex (\d+) '
                 r'\((\d+) from it\) against army (\d+) \(force (\d+), formation (\d+), (\d+) soldiers, troops task (\d+), '
                 r'on terrain (\d+)\), (\d+) away')
REL = re.compile(r'(\d+)-(\d+)-(\d+) shore: (\d+) new, (\d+) holding \((\d+) moved on\), released: landed (\d+), gone (\d+), '
                 r'timeout (\d+), on water (\d+), land contact (\d+), new order from the game (\d+)'
                 r'(?:; kept off the water: (\d+) orders, (\d+) back to the bank)?')


def dn(d):
    return o.day_number(o.parse_date(d))


def main():
    log, run = sys.argv[1], sys.argv[2]
    orders = []
    rel = collections.Counter()
    for ln in open(log, encoding='utf-8', errors='replace'):
        m = PAT.search(ln)
        if m:
            g = m.groups()
            orders.append(dict(day='%d-%02d-%02d' % tuple(map(int, g[:3])), army=g[3], force=int(g[4]),
                               soldiers=int(g[6]), hex=int(g[7]), enemy=g[9], eforce=int(g[10]), esold=int(g[12]),
                               task=g[13], away=int(g[15])))
            continue
        m = REL.search(ln)
        if m:
            g = [int(v or 0) for v in m.groups()[3:]]
            for k, v in zip(('new', 'holding', 'moved', 'landed', 'gone', 'timeout', 'wet', 'contact', 'replaced',
                             'overruled', 'ashore'), g):
                rel[k] += v
    rows = o.read_csv(run, 'armies')
    byday = collections.defaultdict(dict)
    for r in rows:
        byday[r['日期']][r['部队']] = r
    days = sorted(byday, key=dn)
    idx = {d: i for i, d in enumerate(days)}
    print('命令 %d 条；按兵力：派去 %d 兵对水上 %d 兵（合计）；离岸边格中位 %d 格' % (
        len(orders), sum(x['soldiers'] for x in orders), sum(x['esold'] for x in orders),
        sorted(x['away'] for x in orders)[len(orders) // 2] if orders else 0))
    print('放手原因（军势日累计）：%s' % dict(rel))
    touched = 0
    tdays = 0
    lossA = lossE = 0
    landed = wet = 0
    seen = set()                       # (army, enemy, day): the same pair ordered again counts once a day
    tasks = collections.Counter(x['task'] for x in orders)
    for od in orders:
        i0 = idx.get(od['day'])
        if i0 is None:
            continue
        got = False
        a0 = byday[days[i0]].get(od['army'])
        gen = a0['主将'] if a0 else None
        for i in range(i0, min(i0 + 13, len(days) - 1)):
            a, e = byday[days[i]].get(od['army']), byday[days[i]].get(od['enemy'])
            a2, e2 = byday[days[i + 1]].get(od['army']), byday[days[i + 1]].get(od['enemy'])
            if not a or not e or a['主将'] != gen:
                break
            ah, eh = int(a['格']), int(e['格'])
            if TER[ah] in WATER:
                wet += 1
                break
            if TER[eh] not in WATER:
                landed += 1
                break
            if o.hex_distance(ah, eh) == 1 and a2 and e2:
                got = True
                if (od['army'], od['enemy'], days[i]) in seen:
                    continue
                seen.add((od['army'], od['enemy'], days[i]))
                tdays += 1
                lossA += max(0, int(a['兵数']) - int(a2['兵数']))
                lossE += max(0, int(e['兵数']) - int(e2['兵数']))
        touched += got
    print('敌军任务：%s' % dict(tasks.most_common()))
    print('岸边贴上过的命令 %d/%d；贴上的天数（部队对·天，去重）%d，这些天我方掉兵 %d、水上敌军掉兵 %d（交换比 %.2f）' % (
        touched, len(orders), tdays, lossA, lossE, lossE / max(lossA, 1)))
    print('之后敌军上岸 %d 次，我方下水 %d 次（12 天内，第一次发生就停）' % (landed, wet))


if __name__ == '__main__':
    main()
