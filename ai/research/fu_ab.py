"""占府 A/B（只给奇数编号势力开 fu）对照默认局：攻城时府分、这一片府易主、拿下率、溃灭率（2026-11-14）。

  python -P ai/research/fu_ab.py --ab ai/runs/A1 ai/runs/A2 ... --base ai/runs/B1 ai/runs/B2 ...

按攻方势力编号奇偶分开（A/B 局里奇数方开了占府，默认局两边都没开），每组看到过城下 3 格内的攻城：
  到城下那天站位格上守方府分（siege_fu.py）；攻城期间城 6 格内双方府分和府分差（siege_force.py）；
  这一片（到城下时能踩的、或之前 30 天攻方踩下的区域）易主给攻方：到城下前 20 天、到城下后 40 天，每次平均几个；
  拿下率、攻城部队溃灭率、兵力。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import observe as o  # noqa: E402
import siege_force as F  # noqa: E402
import siege_fu as SF  # noqa: E402


def group(runs, side):
    xs = []
    for p in runs:
        ev = [(F.dn(e['日期']), e['对象'].split(':')[0], e['到']) for e in o.read_csv(p, 'events') if e['事件'] == '府易主']
        for x in SF.analyse(p, 12):
            if x['fid'] % 2 != (1 if side == 'odd' else 0):
                continue
            zone = set(x['take']) | set(x['took_before'])
            x['before'] = sum(1 for d, a, to in ev if to == x['force'] and a in zone and x['arrive'] - 20 <= d < x['arrive'])
            x['during'] = sum(1 for d, a, to in ev if to == x['force'] and a in zone and x['arrive'] <= d <= x['arrive'] + 40)
            xs.append(x)
    return xs


def line(label, xs):
    n = len(xs)
    if not n:
        print('%-16s 无' % label)
        return
    fx = [x for x in xs if x['fa'] is not None]
    so = sum(x['n_so'] for x in xs)
    w = sum(x['wiped'] for x in xs)
    print('%-16s %3d 次 | 到城下守方府分 %.2f | 攻城期间府分 攻 %.2f 守 %.2f 差 %+.2f | 这一片易主给攻方 前 %.2f 后 %.2f | '
          '拿下 %d%% | 攻城部队溃灭 %d%% | 攻方 %d 兵、守方合计 %d' % (
              label, n, sum(x['cur'] for x in xs) / n,
              sum(x['fa'] for x in fx) / max(len(fx), 1), sum(x['fd'] for x in fx) / max(len(fx), 1),
              sum(x['fa'] - x['fd'] for x in fx) / max(len(fx), 1),
              sum(x['before'] for x in xs) / n, sum(x['during'] for x in xs) / n,
              100 * sum(x['took'] for x in xs) // n, 100 * w // max(so, 1),
              sum(x['att'] for x in xs) // n, sum(x['garrison'] + x['sortie'] + x['relief'] for x in xs) // n))


def main():
    a = sys.argv[1:]
    ab = a[a.index('--ab') + 1:a.index('--base')] if '--base' in a else a[a.index('--ab') + 1:]
    base = a[a.index('--base') + 1:] if '--base' in a else []
    line('A/B 奇数（开）', group(ab, 'odd'))
    line('A/B 偶数', group(ab, 'even'))
    if base:
        line('默认局 奇数', group(base, 'odd'))
        line('默认局 偶数', group(base, 'even'))


if __name__ == '__main__':
    main()
