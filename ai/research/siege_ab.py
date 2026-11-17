"""攻城评估 A/B（--ab odd:siegeplan2 配 siegeplane，或换边 --ab even:siegeplan2 配 siegeplano）：按攻方势力编号奇偶比攻城次数、兵力比、拿下率、溃灭率（2026-11-14）。

  python -P ai/research/siege_ab.py --ab ai/runs/A1 A2 ... --base ai/runs/B1 B2 ...

口径同 siege_force.py（同一势力打同一座城、时间接得上的攻城军势合成一次）。每组：攻城次数、到过城下 3 格内的次数、拿下
（全部攻城里），出发兵数平均；到过城下的：守方合计（城里 + 出城 + 援军）、攻方兵 ÷ 守方合计 < 0.7 / ≥ 1.5 的占比，
折算府加成后的兵力比中位，攻城部队溃灭率（出征里溃灭的支数）、掉兵合计。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import siege_force as F  # noqa: E402


def group(runs, odd):
    xs = []
    for p in runs:
        xs += [x for x in F.analyse(p, 12) if x['fid'] % 2 == (1 if odd else 0)]
    return xs


def line(label, xs, nruns):
    n = len(xs)
    if not n:
        print('%-14s 无' % label)
        return
    ar = [x for x in xs if x['arrive'] is not None]
    m = len(ar)
    tot = lambda x: x['garrison'] + x['sortie'] + x['relief']  # noqa: E731
    r = sorted(x['att'] / max(tot(x), 1) for x in ar)
    fr = sorted(x['att'] * (1 + 0.1 * x['fa']) / max(tot(x) * (1 + 0.1 * x['fd']), 1) for x in ar if x['fa'] is not None)
    so = sum(x['n_so'] for x in ar)
    print('%-14s 攻城 %3d 次（每局 %.1f），到城下 %3d，拿下 %2d（%2d%%）；出发平均 %5d 兵 | 到城下的：守方合计 %5d，'
          '兵力比 <0.7 %2d%%、≥1.5 %2d%%，折府后中位 %.2f；攻城部队溃灭 %d/%d（%d%%），掉兵 %d' % (
              label, n, n / max(nruns, 1), m, sum(x['took'] for x in xs), 100 * sum(x['took'] for x in xs) // n,
              sum(x['att'] for x in xs) // n, sum(tot(x) for x in ar) // max(m, 1),
              100 * sum(v < 0.7 for v in r) // max(m, 1), 100 * sum(v >= 1.5 for v in r) // max(m, 1),
              fr[len(fr) // 2] if fr else 0, sum(x['wiped'] for x in ar), so, 100 * sum(x['wiped'] for x in ar) // max(so, 1),
              sum(x['lost'] for x in ar)))


def main():
    a = sys.argv[1:]
    ab = a[a.index('--ab') + 1:a.index('--base')] if '--base' in a else a[a.index('--ab') + 1:]
    base = a[a.index('--base') + 1:] if '--base' in a else []
    line('A/B 奇数', group(ab, True), len(ab))
    line('A/B 偶数', group(ab, False), len(ab))
    if base:
        line('对照 奇数', group(base, True), len(base))
        line('对照 偶数', group(base, False), len(base))


if __name__ == '__main__':
    main()
