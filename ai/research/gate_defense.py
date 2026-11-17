"""守关：攻关时守方部队派了多少、什么阵型、站在关前还是关后，关丢没丢、守关部队溃灭多少。

  python -P ai/research/gate_defense.py ai/runs/<run> [more runs] [--months 12]

一次攻关＝攻城任务（2 8 9 10 22）、军势目标是关隘（编号 52 起）的军势在关 6 格内的日子，同一关隘断开不超过
10 天算一次。守方＝关隘当时的主人（按攻关军势以外、站在关 4 格内的部队的势力判断，取最多的那个）。
关前 / 关后：守方部队离攻方本队比关离攻方本队近，算关前（站到了关外迎敌），否则关后。
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402

SIEGE = {'2', '8', '9', '10', '22'}


def dn(d):
    return o.day_number(o.parse_date(d))


def analyse(p, months):
    rows = o.read_csv(p, 'armies')
    days = sorted({r['日期'] for r in rows}, key=dn)
    lo = dn(days[0])
    rows = [r for r in rows if not months or dn(r['日期']) < lo + months * 31]
    byday = collections.defaultdict(list)
    for r in rows:
        byday[r['日期']].append(r)
    days = sorted(byday, key=dn)
    gates = {f['编号']: (f['名称'], int(f['格'])) for f in o.read_csv(p, 'footholds') if f['类型'] == '关隘'}
    # gate ids in 军势目标 are 51 + gate index
    gid = {str(51 + int(k)): v for k, v in gates.items()}
    sor = collections.defaultdict(list)
    for s in o.read_csv(p, 'sorties'):
        sor[(s['部队'], s['主将'])].append(s)
    ep = collections.defaultdict(list)
    for d in days:
        for x in byday[d]:
            if x['军势任务'] in SIEGE and x['军势目标'] in gid and o.hex_distance(int(x['格']), gid[x['军势目标']][1]) <= 6:
                ep[x['军势目标']].append(d)
    out = []
    tot = collections.Counter()
    forms = collections.Counter()
    for g, ds in ep.items():
        ds = sorted(set(ds), key=dn)
        parts, cur = [], [ds[0]]
        for a, b in zip(ds, ds[1:]):
            if dn(b) - dn(a) > 10:
                parts.append(cur)
                cur = []
            cur.append(b)
        parts.append(cur)
        name, gh = gid[g]
        for part in parts:
            attackers = collections.Counter()
            defenders = {}
            front = back = 0
            for d in part:
                atk = [x for x in byday[d] if x['军势任务'] in SIEGE and x['军势目标'] == g]
                if not atk:
                    continue
                af = atk[0]['势力']
                attackers[af] += 1
                lead = next((x for x in atk if x['本队'] == '是'), atk[0])
                lh = int(lead['格'])
                for x in byday[d]:
                    if x['势力'] == af or x['军势任务'] in SIEGE or o.hex_distance(int(x['格']), gh) > 4:
                        continue
                    defenders.setdefault((x['部队'], x['主将']), x)
                    if o.hex_distance(int(x['格']), lh) < o.hex_distance(gh, lh):
                        front += 1
                    else:
                        back += 1
            if not attackers:
                continue
            tot['攻关'] += 1
            tot['守关部队'] += len(defenders)
            tot['关前部队日'] += front
            tot['关后部队日'] += back
            for x in defenders.values():
                forms[x['阵型']] += 1
                ss = [s for s in sor.get((x['部队'], x['主将']), []) if dn(s['出发']) <= dn(part[-1])]
                if ss and ss[-1]['结局'].startswith('溃灭'):
                    tot['守关部队溃灭'] += 1
            tot['守方出兵 0 支'] += not defenders
    if not tot['攻关']:
        return ['（一年里没有攻关）']
    out.append('攻关 %d 次；守方派出的部队合计 %d 支（平均 %.1f 支，一支都没有的 %d 次）；溃灭 %d 支；关前部队日 %d、关后 %d' % (
        tot['攻关'], tot['守关部队'], tot['守关部队'] / tot['攻关'], tot['守方出兵 0 支'], tot['守关部队溃灭'],
        tot['关前部队日'], tot['关后部队日']))
    out.append('守关部队阵型：%s' % '、'.join('%s %d' % kv for kv in forms.most_common(8)))
    falls = [e for e in o.read_csv(p, 'events') if e['事件'] == '城市易主']
    return out


def main():
    args = sys.argv[1:]
    months = 12
    if '--months' in args:
        i = args.index('--months')
        months = int(args[i + 1])
        del args[i:i + 2]
    for p in args:
        print('== %s' % os.path.basename(p.rstrip('/\\')))
        for ln in analyse(p, months):
            print('  ' + ln)


if __name__ == '__main__':
    main()
