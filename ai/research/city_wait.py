"""攻城部队在城的射程里（离目标城 3 格内）站着不动、没在打城（行动不是 4）的部队日：按阵型、命令、旁边有没有敌军分，
掉兵多少（2026-11-15，BEHAVIOR_PLAN 第四十六节第 1.3 版）。

  python -P ai/research/city_wait.py ai/runs/<run> [more runs]
"""
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import observe as o  # noqa: E402
SIEGE = ('2', '8', '9', '10', '22')
dn = lambda d: o.day_number(o.parse_date(d))
by = collections.defaultdict(lambda: [0, 0])
tot = [0, 0]
allin = [0, 0]
for run in sys.argv[1:]:
    city = {f['编号']: int(f['格']) for f in o.read_csv(run, 'footholds') if f['类型'] == '城市'}
    byday = collections.defaultdict(list)
    for x in o.read_csv(run, 'armies'):
        byday[dn(x['日期'])].append(x)
    days = sorted(byday)
    key = lambda x: (x['部队'], x['主将'])
    prev = {}
    for i, d in enumerate(days[:-1]):
        today = byday[d]
        tom = {key(x): x for x in byday[days[i + 1]]}
        for x in today:
            if x['军势任务'] not in SIEGE or x['军势目标'] not in city:
                continue
            h = int(x['格']); dist = o.hex_distance(h, city[x['军势目标']])
            if dist > 3:
                continue
            y = tom.get(key(x)); p = prev.get(key(x))
            loss = max(0, int(x['兵数']) - int(y['兵数'])) if y else 0
            allin[0] += 1; allin[1] += loss
            if x['部队行动'] == '4' or not p or int(p['格']) != h:
                continue
            foe = min((o.hex_distance(int(z['格']), h) for z in today if z['势力'] != x['势力']), default=99)
            k = (x['阵型'], x['部队行动'], '贴敌' if foe <= 1 else '敌3格内' if foe <= 3 else '无敌')
            by[k][0] += 1; by[k][1] += loss
            tot[0] += 1; tot[1] += loss
        prev = {key(x): x for x in today}
print('攻城部队在城 3 格内的部队日 %d、掉兵 %d；其中站着不动、没在打城的 %d 日、掉兵 %d' % (allin[0], allin[1], tot[0], tot[1]))
print('阵型 行动 旁边敌军  部队日  掉兵  每天')
for k, (n, l) in sorted(by.items(), key=lambda kv: -kv[1][0])[:20]:
    print('%s %s %s %6d %7d %6.0f' % (k + (n, l, l / max(n, 1))))
