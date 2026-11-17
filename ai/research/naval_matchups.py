"""水战对位：按「一片交战」看两边掉兵，分水对水（按船型差几级）、岸打水、陆对陆（2026-11-15，BEHAVIOR_PLAN 第四十七节）。

  python -P ai/research/naval_matchups.py ai/runs/<run> [more runs] [--months 12] [--parity]
  A/B：写成 ai/runs/<run>@odd 或 @even（这一局开模块的是奇数还是偶数势力），--parity 就按「开模块方 / 没开方」合计。

要 armies.csv 有「船型」「水战个性」两列（2026-11-15 起的记录器）。

一片交战：某天互相贴着（相邻格）的两个势力的部队，按「贴着就连起来」连成一片（只算正好两个势力的片）。
每片算两边当天到第二天的掉兵：第二天还在的按兵数差；不在了的，那天出征结局是溃灭就算全掉，否则不算（回城、撤走）。
楼船有范围攻击（用户 2026-11-15：一打多时效果比面板大，其他船没有），按片统计就把这一点算进去了。

分类（每片两边各看一次，从「那一边」看）：
- 地形：这一边的部队全在水上（地形 14、24）/ 全在陆上 / 混着。
- 水对水：两边都全在水上；按两边主力船型（兵最多的那种）差几级分，楼船对非楼船单列；有没有水战个性（68）。
- 岸打水：这一边全在陆上、对面全在水上。
- 陆对陆：两边都全在陆上，作对照。
交换 = 对面掉 ÷ 这一边掉。
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402

RES = os.path.join(HERE, 'research')
TER = open(os.path.join(RES, 'terrain_exce.bin'), 'rb').read()
WATER = {14, 24}
TIER = {'走舸': 1, '蒙冲': 2, '楼船': 3}


def dn(d):
    return o.day_number(o.parse_date(d))


def analyse(run, months):
    rows = o.read_csv(run, 'armies')
    if rows and '船型' not in rows[0]:
        print('%s：没有船型列，跳过' % run)
        return []
    lo = min(dn(x['日期']) for x in rows)
    rows = [x for x in rows if not months or dn(x['日期']) < lo + months * 31]
    byday = collections.defaultdict(list)
    for x in rows:
        byday[dn(x['日期'])].append(x)
    days = sorted(byday)
    nxt = {days[i]: days[i + 1] for i in range(len(days) - 1)}
    key = lambda x: (x['部队'], x['主将'])  # noqa: E731
    wiped = {(s['部队'], s['主将'], dn(s['结束'])) for s in o.read_csv(run, 'sorties') if s['结局'].startswith('溃灭')}
    fid = {f['君主']: int(f['势力']) for f in o.read_csv(run, 'forces')}
    out = []
    for d in days:
        nd = nxt.get(d)
        if nd is None:
            continue
        tomorrow = {key(x): x for x in byday[nd]}
        units = [x for x in byday[d] if x['势力']]
        # contact graph between different forces, components by union-find
        parent = list(range(len(units)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        hexes = [int(x['格']) for x in units]
        touched = set()
        for i in range(len(units)):
            for j in range(i + 1, len(units)):
                if units[i]['势力'] != units[j]['势力'] and o.hex_distance(hexes[i], hexes[j]) == 1:
                    parent[find(i)] = find(j)
                    touched.update((i, j))
        comps = collections.defaultdict(list)
        for i in touched:
            comps[find(i)].append(i)
        for idx in comps.values():
            forces = {units[i]['势力'] for i in idx}
            if len(forces) != 2:
                continue
            sides = {}
            for f in forces:
                us = [units[i] for i in idx if units[i]['势力'] == f]
                loss = 0
                for x in us:
                    y = tomorrow.get(key(x))
                    if y:
                        loss += max(0, int(x['兵数']) - int(y['兵数']))
                    elif (x['部队'], x['主将'], d) in wiped or (x['部队'], x['主将'], nd) in wiped:
                        loss += int(x['兵数'])
                wat = [x for x in us if TER[int(x['格'])] in WATER]
                ship = collections.Counter()
                for x in wat:
                    ship[x['船型']] += int(x['兵数'])
                sides[f] = {'fid': fid.get(f, 0), 'n': len(us), 'sol': sum(int(x['兵数']) for x in us), 'loss': loss,
                            'terr': 'water' if len(wat) == len(us) else 'land' if not wat else 'mixed',
                            'ship': ship.most_common(1)[0][0] if ship else '',
                            'lou': any(x['船型'] == '楼船' for x in wat),
                            'trait': any('68' in (x.get('水战个性') or '').split('+') for x in us)}
            out.append((d, sides))
    return out


def classify(me, foe):
    if me['terr'] == 'water' and foe['terr'] == 'water':
        diff = TIER.get(me['ship'], 0) - TIER.get(foe['ship'], 0)
        return '水对水 船型%+d级' % diff
    if me['terr'] == 'land' and foe['terr'] == 'water':
        return '岸打水（我在岸上）'
    if me['terr'] == 'water' and foe['terr'] == 'land':
        return '水打岸（我在水上）'
    if me['terr'] == 'land' and foe['terr'] == 'land':
        return '陆对陆'
    return '混合'


def main():
    a = sys.argv[1:]
    months = int(a[a.index('--months') + 1]) if '--months' in a else 12
    runs = [x for i, x in enumerate(a) if not x.startswith('--') and (i == 0 or a[i - 1] != '--months')]
    engs = []
    for arg in runs:
        r, _, on = arg.partition('@')      # run@odd / run@even: which side had the module on in this run
        engs += [(d, sides, on) for d, sides in analyse(r, months)]
    stat = collections.defaultdict(lambda: [0, 0, 0, 0])   # n, my loss, foe loss, my soldiers
    extra = collections.defaultdict(lambda: [0, 0, 0, 0])
    for d, sides, on in engs:
        (f1, s1), (f2, s2) = sides.items()
        for me, foe in ((s1, s2), (s2, s1)):
            c = classify(me, foe)
            if '--parity' in a and on:     # A/B with run@odd / run@even
                c = ('开模块方 ' if (me['fid'] % 2 == 1) == (on == 'odd') else '没开方 ') + c
            elif '--parity' in a:          # A/B: from the odd / even side
                c = ('奇数方 ' if me['fid'] % 2 else '偶数方 ') + c
            t = stat[c]
            t[0] += 1; t[1] += me['loss']; t[2] += foe['loss']; t[3] += me['sol'] / max(foe['sol'], 1)
            if c.startswith('水对水'):
                for lab, ok in (('水对水 我有楼船、对面没有', me['lou'] and not foe['lou']),
                                ('水对水 两边都有楼船', me['lou'] and foe['lou']),
                                ('水对水 我有水战个性、对面没有', me['trait'] and not foe['trait']),
                                ('水对水 两边船型相同、都没有水战个性', me['ship'] == foe['ship'] and not me['trait'] and not foe['trait'])):
                    if ok and '--parity' not in a:
                        e = extra[lab]
                        e[0] += 1; e[1] += me['loss']; e[2] += foe['loss']; e[3] += me['sol'] / max(foe['sol'], 1)
    print('交战片·天 %d（每片两边各算一次）' % len(engs))
    print('%-28s %6s %9s %9s %7s %9s' % ('类别', '片·天', '我掉', '对面掉', '交换', '兵力比均值'))
    order = sorted(stat, key=lambda c: ('水对水' not in c, c))
    for c in order:
        n, ml, fl, r = stat[c]
        print('%-28s %6d %9d %9d %7.2f %9.2f' % (c, n, ml, fl, fl / max(ml, 1), r / max(n, 1)))
    for c, (n, ml, fl, r) in extra.items():
        print('%-28s %6d %9d %9d %7.2f %9.2f' % (c, n, ml, fl, fl / max(ml, 1), r / max(n, 1)))


if __name__ == '__main__':
    main()
