"""攻城任务的部队贴着敌军、4 格内没有友军的日子：离目标城多远、同目标的友军离多远、敌我兵力（2026-11-16，
BEHAVIOR_PLAN 第四十九、五十节）。也用来验「攻城落单不单打」（DLL 功能 siegelone）。

  python -P ai/research/siege_lone.py ai/runs/<run>[@odd|@even] [more runs]

@odd / @even：这一局开模块的是奇数还是偶数编号势力（A/B），结果按「开模块方 / 没开方」分开；不写就合在一起。
每边另外给：攻城任务部队贴敌的全部部队日和掉兵（落单占多少）、攻城出征溃灭（sorties.csv）、日志里 siegelone 的汇总。
"""
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import observe as o  # noqa: E402

SIEGE = ('2', '8', '9', '10', '22')
DAY = re.compile(r'siegelone: alone ahead of their group (\d+) \(new (\d+)\); held (\d+), of them stepping back (\d+); let go: '
                 r'company (\d+), none behind or out of the band (\d+), contact (\d+), timeout (\d+)')
DAY_KEYS = ['落单在前的部队日', '新按住', '按住的部队日', '其中往后队靠', '放手：友军到了', '放手：后面没人了或出了范围', '放手：贴上敌军', '放手：等满']


def dn(d):
    return o.day_number(o.parse_date(d))


def main():
    tot = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0]))
    allc = collections.defaultdict(lambda: [0, 0, 0])      # side -> siege contact army-days, my loss, foe loss
    units = collections.defaultdict(set)
    wiped = collections.defaultdict(set)
    sort = collections.defaultdict(lambda: [0, 0])          # side -> siege sorties, wiped
    logt = collections.Counter()
    for arg in [a for a in sys.argv[1:] if not a.startswith('--')]:
        run, _, on = arg.partition('@')
        rows = o.read_csv(run, 'armies')
        fid = {f['君主']: int(f['势力']) for f in o.read_csv(run, 'forces')}

        def side(force):
            return '全部' if not on else ('开模块方' if (fid.get(force, 0) % 2 == 1) == (on == 'odd') else '没开方')
        cpos = {f['编号']: int(f['格']) for f in o.read_csv(run, 'footholds') if f['类型'] == '城市'}
        byday = collections.defaultdict(list)
        for r in rows:
            byday[dn(r['日期'])].append(r)
        days = sorted(byday)
        key = lambda x: (x['部队'], x['主将'])  # noqa: E731
        for i, d in enumerate(days[:-1]):
            nxt = {key(x): int(x['兵数']) for x in byday[days[i + 1]]}
            cnt = collections.Counter((x['势力'], x['军势']) for x in byday[d])
            for x in byday[d]:
                if x['军势任务'] not in SIEGE or x['军势目标'] not in cpos:
                    continue
                h = int(x['格'])
                foes = [y for y in byday[d] if y['势力'] != x['势力'] and o.hex_distance(int(y['格']), h) == 1]
                if not foes:
                    continue
                sd = side(x['势力'])
                me = max(int(x['兵数']) - nxt.get(key(x), 0), 0)
                fo = sum(max(int(y['兵数']) - nxt.get(key(y), 0), 0) / max(1, sum(
                    1 for z in byday[d] if z['势力'] != y['势力'] and o.hex_distance(int(z['格']), int(y['格'])) == 1)) for y in foes)
                t = allc[sd]
                t[0] += 1
                t[1] += me
                t[2] += fo
                if any(y['势力'] == x['势力'] and y is not x and o.hex_distance(int(y['格']), h) <= 4 for y in byday[d]):
                    continue
                dist = o.hex_distance(h, cpos[x['军势目标']])
                mates = [o.hex_distance(int(y['格']), h) for y in byday[d] if y['势力'] == x['势力'] and y is not x
                         and y['军势任务'] in SIEGE and y['军势目标'] == x['军势目标']]
                md = min(mates) if mates else 99
                foe = sum(int(y['兵数']) for y in byday[d] if y['势力'] != x['势力'] and o.hex_distance(int(y['格']), h) <= 3)
                for k in ((0, '落单贴敌合计'), (1, '离目标城 ' + ('≤3 格' if dist <= 3 else '4–8 格' if dist <= 8 else '>8 格')),
                          (2, '同目标的友军 ' + ('5–8 格内' if md <= 8 else '9–15 格内' if md <= 15 else '更远或没有')),
                          (3, '军势 ' + ('单支' if cnt[(x['势力'], x['军势'])] == 1 else '多支里落单的')),
                          (4, '3 格内敌兵 ÷ 自己 ' + ('<1' if foe < int(x['兵数']) else '1–2' if foe < 2 * int(x['兵数']) else '≥2'))):
                    v = tot[sd][k]
                    v[0] += 1
                    v[1] += me
                units[sd].add((run, key(x)))
                if nxt.get(key(x), 0) == 0 and int(x['兵数']) < 2500:
                    wiped[sd].add((run, key(x)))
        for s in o.read_csv(run, 'sorties'):
            if s['军势任务'] in SIEGE:
                v = sort[side(s['势力'])]
                v[0] += 1
                v[1] += s['结局'].startswith('溃灭')
        log = os.path.join(run, 'ai_dll.log')
        if os.path.exists(log):
            for m in DAY.finditer(open(log, encoding='utf-8-sig', errors='replace').read()):
                for k, v in zip(DAY_KEYS, m.groups()):
                    logt[k] += int(v)
    for sd in sorted(tot):
        a = allc[sd]
        lone = tot[sd][(0, '落单贴敌合计')]
        print('== %s：攻城部队贴敌 %d 部队日，掉 %d、敌掉 %d（交换 %.2f）；其中落单 %d 部队日、掉 %d（占 %d%%）；落单的部队 %d 支，贴着时没了 %d 支' % (
            sd, a[0], a[1], a[2], a[2] / max(a[1], 1), lone[0], lone[1], 100 * lone[1] // max(a[1], 1), len(units[sd]), len(wiped[sd])))
        if sort[sd][0]:
            print('  攻城出征 %d 次，溃灭 %d（%d%%）' % (sort[sd][0], sort[sd][1], 100 * sort[sd][1] // sort[sd][0]))
        for k in sorted(tot[sd]):
            if k[0]:
                print('  %-24s 部队日 %4d 掉兵 %6d' % (k[1], tot[sd][k][0], tot[sd][k][1]))
    if logt:
        print('日志 siegelone 合计：' + '，'.join('%s %d' % (k, logt[k]) for k in DAY_KEYS))


if __name__ == '__main__':
    main()
