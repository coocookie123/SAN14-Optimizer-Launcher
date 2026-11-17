"""How far a city (or gate) hits: armies besieging it lose men at which distances when no enemy army
is near. Needs a run with footholds.csv (recorder from 2026-11-11 evening on).

  python -P ai/research/city_range.py ai/runs/<run>
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402

SIEGE = {'2', '8', '9', '10', '22', '11'}


def main():
    run = sys.argv[1]
    fh = o.read_csv(run, 'footholds')
    if not fh:
        sys.exit('this run has no footholds.csv')
    pos = {('城市' if r['类型'] == '城市' else '关隘', r['编号']): int(r['格']) for r in fh}
    rows = o.read_csv(run, 'armies')
    by = collections.defaultdict(dict)
    for r in rows:
        by[r['日期']][r['部队']] = r
    days = sorted(by, key=lambda d: o.day_number(o.parse_date(d)))
    stat = collections.defaultdict(lambda: [0, 0])          # distance -> [army-days, losing]
    for i, d in enumerate(days[:-1]):
        today, tomorrow = by[d], by[days[i + 1]]
        for k, r in today.items():
            t = r.get('部队目标', '')
            kind = '城市' if t.startswith('城市') else '关隘' if t.startswith('关隘') else None
            if not kind or r['军势任务'] not in SIEGE or k not in tomorrow:
                continue
            if tomorrow[k]['主将'] != r['主将']:
                continue
            h = pos.get((kind, t[2:]))
            if h is None:
                continue
            here = int(r['格'])
            if any(b['势力'] != r['势力'] and o.hex_distance(here, int(b['格'])) <= 3 for b in today.values()):
                continue                                    # an enemy army could be the one hitting
            dist = o.hex_distance(here, h)
            if dist > 8:
                continue
            stat[dist][0] += 1
            stat[dist][1] += int(tomorrow[k]['兵数']) < int(r['兵数']) - 30
    print('离目标据点的距离：部队日，第二天掉兵的部队日（附近 3 格没有敌军部队）')
    for dist in sorted(stat):
        n, lose = stat[dist]
        print(f'  {dist} 格：{n:5d}，掉兵 {lose:5d}（{lose * 100 // max(1, n)}%）')


if __name__ == '__main__':
    main()
