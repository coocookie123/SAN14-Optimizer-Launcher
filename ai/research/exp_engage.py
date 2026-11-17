"""Statistics for the engagement mechanics experiment (modules/experiments/exp_engage.json), from a recorder run.

  python -P ai/research/exp_engage.py ai/runs/<run> [--months 12]

A. Hold: armies with action 13 (wait) while the nearest enemy army is 3-6 hexes away: did they stay on
   the same hex the next day?
B. Targeting: armies whose own target is an army. Is it an enemy? Did the gap shrink and did contact
   follow? For 锋矢: how often the target was an enemy 雁行 or siege engine.
C. Ranged follow: 雁行 in a troops that has a non-雁行 army: distance to the nearest friendly non-雁行
   army each day, and allies within 1 / 2 hexes at their first contact.
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402

BACK = {'雁行', '井阑', '冲车', '投石'}


def load(run, months):
    rows = o.read_csv(run, 'armies')
    if not rows:
        sys.exit('no armies.csv')
    days = sorted({r['日期'] for r in rows}, key=lambda d: o.day_number(o.parse_date(d)))
    lo = o.day_number(o.parse_date(days[0]))
    if months:
        days = [d for d in days if o.day_number(o.parse_date(d)) < lo + months * 31]
    by = collections.OrderedDict((d, {}) for d in days)
    for r in rows:
        if r['日期'] in by:
            by[r['日期']][r['部队']] = r
    return by


def nearest(hexv, force, today, skip=None, want=None):
    best = None
    for k, b in today.items():
        if k == skip:
            continue
        if want == 'enemy' and b['势力'] == force:
            continue
        if want == 'friend' and b['势力'] != force:
            continue
        d = o.hex_distance(hexv, int(b['格']))
        if best is None or d < best[0]:
            best = (d, k)
    return best


def main():
    args = sys.argv[1:]
    months = None
    if '--months' in args:
        i = args.index('--months')
        months = int(args[i + 1])
        del args[i:i + 2]
    by = load(args[0], months)
    days = list(by)
    has_target = any('部队目标' in r for t in by.values() for r in t.values())
    # A
    hold = stay = 0
    for i, d in enumerate(days[:-1]):
        today, tomorrow = by[d], by[days[i + 1]]
        for k, r in today.items():
            if r['部队行动'] != '13' or k not in tomorrow or tomorrow[k]['主将'] != r['主将']:
                continue
            ne = nearest(int(r['格']), r['势力'], today, k, 'enemy')
            if ne and 3 <= ne[0] <= 6:
                hold += 1
                stay += tomorrow[k]['格'] == r['格']
    print(f'A 待命：敌军在 3-6 格时行动为 13 的部队日 {hold}，第二天原地未动 {stay}'
          f'（{stay * 100 // max(1, hold)}%）')
    # B
    if has_target:
        tot = enemy = closer = contact = 0
        fs_tot = fs_weak = 0
        for i, d in enumerate(days[:-1]):
            today, tomorrow = by[d], by[days[i + 1]]
            for k, r in today.items():
                t = r.get('部队目标', '')
                if not t.startswith('部队'):
                    continue
                tk = t[2:]
                tgt = today.get(tk)
                if not tgt:
                    continue
                tot += 1
                if tgt['势力'] == r['势力']:
                    continue
                enemy += 1
                if r['阵型'] == '锋矢':
                    fs_tot += 1
                    fs_weak += tgt['阵型'] in BACK
                d0 = o.hex_distance(int(r['格']), int(tgt['格']))
                if k in tomorrow and tk in tomorrow:
                    d1 = o.hex_distance(int(tomorrow[k]['格']), int(tomorrow[tk]['格']))
                    closer += d1 < d0
                    contact += int(tomorrow[tk]['兵数']) < int(tgt['兵数'])
        print(f'B 指定目标：目标是部队的部队日 {tot}，其中敌军 {enemy}；第二天距离缩短 {closer}，'
              f'目标第二天掉兵 {contact}')
        print(f'  锋矢以敌军为目标 {fs_tot} 次，其中目标是雁行或攻城兵器 {fs_weak} 次')
    else:
        print('B 指定目标：这次记录没有「部队目标」列（旧记录）')
    # C
    dist = collections.Counter()
    for d, today in by.items():
        for k, r in today.items():
            if r['阵型'] != '雁行':
                continue
            mates = [b for j, b in today.items() if j != k and b['军势'] == r['军势'] and b['阵型'] not in BACK]
            if not mates:
                continue
            m = min(o.hex_distance(int(r['格']), int(b['格'])) for b in mates)
            dist[min(m, 6)] += 1
    n = sum(dist.values())
    print('C 雁行离最近己方前排的距离（部队日）：' + '，'.join(f'{k}{"+" if k == 6 else ""}格 {v}'
                                                  for k, v in sorted(dist.items())) + f'（共 {n}）')
    bat = o.read_csv(args[0], 'battles')
    first = {(b['日期'], b['部队']): b for b in bat}
    near1 = near2 = cnt = 0
    for (d, k), b in first.items():
        r = by.get(d, {}).get(k)
        if not r or r['阵型'] != '雁行':
            continue
        cnt += 1
        fr = [x for j, x in by[d].items() if j != k and x['势力'] == r['势力']]
        ds = [o.hex_distance(int(r['格']), int(x['格'])) for x in fr]
        near1 += any(v <= 1 for v in ds)
        near2 += any(v <= 2 for v in ds)
    print(f'  雁行首次接战 {cnt} 次：1 格内有友军 {near1} 次，2 格内有友军 {near2} 次')


if __name__ == '__main__':
    main()
