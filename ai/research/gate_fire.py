"""守关时守方站位和双方损失：每支攻关部队在关 2 格内的日子，守方部队在哪、雁行有没有站在射击位、两边掉多少兵。

  python -P ai/research/gate_fire.py ai/runs/<run> [more runs] [--from 203-08-21] [--until 203-11-01]

攻方＝部队目标是关隘（「关隘N」）、离那个关 3 格内的部队；守方＝离关 8 格内、和攻方不同势力、和攻方不在关的同一侧
（离攻方本身比离关远，近似为关后）的部队。射击位＝守方雁行离某支攻方部队正好 2 格；排队＝守方部队离所有攻方
都在 3 格以上（够不着）。掉兵＝第二天同一部队兵数少了多少。
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402


def dn(d):
    return o.day_number(o.parse_date(d))


def analyse(p, lo, hi):
    rows = o.read_csv(p, 'armies')
    byday = collections.defaultdict(list)
    for r in rows:
        if (lo and dn(r['日期']) < lo) or (hi and dn(r['日期']) > hi):
            continue
        byday[r['日期']].append(r)
    days = sorted(byday, key=dn)
    nxt = {days[i]: days[i + 1] for i in range(len(days) - 1)}
    gates = {f['编号']: (f['名称'], int(f['格'])) for f in o.read_csv(p, 'footholds') if f['类型'] == '关隘'}
    key = lambda x: (x['部队'], x['主将'])  # noqa: E731
    stat = collections.defaultdict(lambda: collections.Counter())
    for d in days:
        today = byday[d]
        tom = {key(x): x for x in byday.get(nxt.get(d), [])}
        for gid, (gname, gh) in gates.items():
            atk = [x for x in today if x['部队目标'] == '关隘' + gid and o.hex_distance(int(x['格']), gh) <= 3]
            if not atk:
                continue
            af = {x['势力'] for x in atk}
            s = stat[gname]
            s['攻关日'] += 1
            for x in atk:
                y = tom.get(key(x))
                if y:
                    s['攻方掉兵'] += max(0, int(x['兵数']) - int(y['兵数']))
            for x in today:
                h = int(x['格'])
                if x['势力'] in af or o.hex_distance(h, gh) > 8:
                    continue
                da = min(o.hex_distance(h, int(a['格'])) for a in atk)
                if da < o.hex_distance(h, gh) and da <= 2:      # outside, next to the attackers
                    s['守方关前部队日'] += 1
                s['守方部队日'] += 1
                if x['阵型'] == '雁行':
                    s['守方雁行日'] += 1
                    s['雁行在射击位'] += da == 2
                if da >= 3:
                    s['够不着（排队）'] += 1
                y = tom.get(key(x))
                if y:
                    s['守方掉兵'] += max(0, int(x['兵数']) - int(y['兵数']))
    out = []
    for g, s in stat.items():
        out.append('%s：攻关 %d 天，攻方掉兵 %d；守方部队日 %d（雁行 %d，其中在射击位 %d；够不着的 %d；站到关前 %d），守方掉兵 %d' % (
            g, s['攻关日'], s['攻方掉兵'], s['守方部队日'], s['守方雁行日'], s['雁行在射击位'], s['够不着（排队）'],
            s['守方关前部队日'], s['守方掉兵']))
    return out or ['（没有攻关）']


def main():
    args = sys.argv[1:]
    lo = hi = None
    if '--from' in args:
        i = args.index('--from')
        lo = dn(args[i + 1])
        del args[i:i + 2]
    if '--until' in args:
        i = args.index('--until')
        hi = dn(args[i + 1])
        del args[i:i + 2]
    for p in args:
        print('== %s' % os.path.basename(p.rstrip('/\\')))
        for ln in analyse(p, lo, hi):
            print('  ' + ln)


if __name__ == '__main__':
    main()
