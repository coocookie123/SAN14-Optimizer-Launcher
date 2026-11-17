"""防断粮（DLL supplylog / supplyguard，BEHAVIOR_PLAN 第四十六节第二层）的日志汇总（2026-11-14）。

  python -P ai/research/supply_check.py <run>/ai_dll.log [ai/runs/<run>]

- 每天的汇总行：攻城军势在城下 14 格内的军势日、粮道长度、最窄一步只有 1 格的、断了的、威胁、派遣。
- 威胁：按敌军的军势任务（援军路过还是别的）、有没有贴着我方、它附近的敌兵占集团几成分组。
- 断粮：每次「CUT OFF」到「has its road back」隔了几天；给了局目录时，看被断军势的部队之后 3、6 天的平均士气变化，
  和同局其余在城下的攻城军势比。
- 派遣（supplyguard）：派了几次、野战几次、派不出几次、回原命令的原因；给了局目录时，看派出那天到之后 5 天，
  护粮部队（第一支）和被打的敌军各掉多少兵。
"""
import collections
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import observe as o  # noqa: E402
from fu_stats import TASKS  # noqa: E402

D = r'(\d+)-(\d+)-(\d+) '
SUM = re.compile(D + r'supply: siege troops near their city (\d+): road (\d+) \(avg (\d+) steps, (\d+) with a 1-hex step\), '
                 r'cut (\d+), farther than \d+ (\d+); threats (\d+) \(new (\d+)\), game cut flags near (\d+); guards sent '
                 r'(\d+) \(field battles (\d+)\), holding (\d+), back (\d+), none to send (\d+)'
                 r'(?:, taken over (\d+), road 府 sent (\d+) \(odd/even forces: near (\d+)/(\d+), cut (\d+)/(\d+), '
                 r'threats (\d+)/(\d+)\))?')
THREAT = re.compile(D + r'supply: threat to the group of force (\d+) on city (\d+) \((\d+) soldiers(, cut off)?\): army (\d+) '
                    r'\(force (\d+), formation (\d+), (\d+) soldiers, (\d+) within \d+, troops task (-?\d+), action (\d+) -> '
                    r'(\d+):(\d+), ai flags (\w+)\) at hex (\d+), (\d+) from the road behind, (\d+) from the city, touching '
                    r'(\d+) of ours')
CUT = re.compile(D + r'supply: troops (\d+) \(force (\d+), city (\d+), lead army (\d+) (\d+) hexes out, (\d+) soldiers, '
                 r'morale (\d+)\) (is CUT OFF|has its road back) \(road (-?\d+), narrowest (\d+)')
GUARD = re.compile(D + r'supplyguard: (guard|FIELD BATTLE), (\d+) armies \((\d+) soldiers, first army (\d+), (\d+) hexes away\) '
                   r'at army (\d+) \(force (\d+), (\d+) soldiers with those near it, (\d+) from the road behind\) for the group '
                   r'of force (\d+) on city (\d+) \((\d+) soldiers')
BACK = re.compile(D + r'supplyguard: army (\d+) \(force (\d+), (\d+) soldiers\) back to its order after (\d+) days \(([^)]*)\)')
NONE = re.compile(D + r'supplyguard: nobody to send')


def day(g):
    return '%d-%02d-%02d' % tuple(map(int, g[:3]))


def dn(d):
    return o.day_number(o.parse_date(d))


def main():
    log = sys.argv[1]
    run = sys.argv[2] if len(sys.argv) > 2 else None
    tot = collections.Counter()
    ndays = 0
    threats, cuts, guards, backs = [], [], [], []
    nnone = 0
    for ln in open(log, encoding='utf-8', errors='replace'):
        m = SUM.search(ln)
        if m:
            g = [int(x) if x is not None else 0 for x in m.groups()]
            ndays += 1
            for k, v in zip(['军势日', '有粮道', '均长', '窄1格', '断', '太远', '威胁', '新威胁', '游戏断粮标志', '派出',
                             '野战', '在护', '回', '派不出', '接管', '踩路府', '奇近', '偶近', '奇断', '偶断',
                             '奇威胁', '偶威胁'], g[3:]):
                tot[k] += v
            continue
        m = THREAT.search(ln)
        if m:
            g = m.groups()
            threats.append(dict(day=day(g), force=int(g[3]), city=int(g[4]), gsol=int(g[5]), cut=bool(g[6]),
                                army=g[7], eforce=int(g[8]), sol=int(g[10]), clus=int(g[11]), task=g[12],
                                act=int(g[13]), road=int(g[18]), city_d=int(g[19]), touch=int(g[20])))
            continue
        m = CUT.search(ln)
        if m:
            g = m.groups()
            cuts.append(dict(day=day(g), troops=g[3], force=int(g[4]), city=int(g[5]), lead=g[6], out=int(g[7]),
                             sol=int(g[8]), morale=int(g[9]), cut=g[10] == 'is CUT OFF'))
            continue
        m = GUARD.search(ln)
        if m:
            g = m.groups()
            guards.append(dict(day=day(g), big=g[3] == 'FIELD BATTLE', n=int(g[4]), sol=int(g[5]), army=g[6],
                               away=int(g[7]), enemy=g[8], eforce=int(g[9]), clus=int(g[10]), road=int(g[11]),
                               force=int(g[12]), city=int(g[13]), gsol=int(g[14])))
            continue
        m = BACK.search(ln)
        if m:
            g = m.groups()
            backs.append(dict(day=day(g), army=g[3], days=int(g[6]), why=g[7]))
            continue
        if NONE.search(ln):
            nnone += 1
    print('汇总行 %d 天：攻城军势日 %d，有粮道 %d（平均长 %.1f 步，最窄一步只有 1 格 %d），断 %d，太远 %d；威胁 %d（新 %d），'
          '游戏断粮标志 %d；派出 %d（野战 %d），回 %d，派不出 %d' % (
              ndays, tot['军势日'], tot['有粮道'], tot['均长'] / max(ndays, 1), tot['窄1格'], tot['断'], tot['太远'],
              tot['威胁'], tot['新威胁'], tot['游戏断粮标志'], tot['派出'], tot['野战'], tot['回'], tot['派不出']))
    if tot['奇近'] or tot['偶近']:
        print('分边（奇数 / 偶数势力）：城下军势日 %d / %d，断 %d / %d（%.1f%% / %.1f%%），威胁 %d / %d；护粮被树或游戏接管 %d；'
              '派去踩粮道上的府 %d' % (tot['奇近'], tot['偶近'], tot['奇断'], tot['偶断'], 100 * tot['奇断'] / max(tot['奇近'], 1),
                                100 * tot['偶断'] / max(tot['偶近'], 1), tot['奇威胁'], tot['偶威胁'], tot['接管'], tot['踩路府']))
    if threats:
        print('\n新威胁 %d 条（日志里记下的）：' % len(threats))
        by = collections.Counter(TASKS.get(t['task'], '任务' + t['task']) for t in threats)
        print('  敌军军势任务：' + '，'.join('%s %d' % kv for kv in by.most_common()))
        print('  贴着我方：%d；粮道已断时：%d' % (sum(1 for t in threats if t['touch']), sum(1 for t in threats if t['cut'])))
        r = collections.Counter('≥一半' if 2 * t['clus'] >= t['gsol'] else '2–5 成' if 5 * t['clus'] >= t['gsol'] else '<2 成'
                                for t in threats)
        print('  附近敌兵 / 集团：' + '，'.join('%s %d' % kv for kv in r.most_common()))
        rd = collections.Counter(t['road'] for t in threats)
        print('  离粮道（身后）：' + '，'.join('%d 格 %d' % (k, rd[k]) for k in sorted(rd)))
    if cuts:
        print('\n断粮 %d 次（奇数方 %d），连回 %d 次' % (sum(c['cut'] for c in cuts),
                                                 sum(c['cut'] and c['force'] % 2 for c in cuts), sum(not c['cut'] for c in cuts)))
        open_ = {}
        durs = []
        for c in cuts:
            if c['cut']:
                open_[c['troops']] = c
            elif c['troops'] in open_:
                durs.append(dn(c['day']) - dn(open_.pop(c['troops'])['day']))
        if durs:
            durs.sort()
            print('  断了多少天连回：中位 %d，最长 %d（%d 次）；到局末没连回 %d' % (durs[len(durs) // 2], durs[-1], len(durs),
                                                                           len(open_)))
    if guards:
        print('\n派遣 %d 次（野战 %d），共 %d 支；派不出 %d 次' % (len(guards), sum(g['big'] for g in guards),
                                                    sum(g['n'] for g in guards), nnone))
        print('  派出兵 / 敌兵：中位 %.2f；离敌军：中位 %d 格' % (
            sorted(g['sol'] / max(g['clus'], 1) for g in guards)[len(guards) // 2],
            sorted(g['away'] for g in guards)[len(guards) // 2]))
    if backs:
        w = collections.Counter(b['why'] for b in backs)
        print('  回原命令 %d：' % len(backs) + '，'.join('%s %d' % kv for kv in w.most_common()) +
              '；在外中位 %d 天' % sorted(b['days'] for b in backs)[len(backs) // 2])
    if not run:
        return
    rows = o.read_csv(run, 'armies')
    by = collections.defaultdict(dict)
    days = set()
    for r in rows:
        by[r['日期']][r['部队']] = r
        days.add(r['日期'])
    days = sorted(days, key=dn)
    idx = {d: i for i, d in enumerate(days)}

    def later(d, k):
        i = idx.get(d)
        return days[i + k] if i is not None and i + k < len(days) else None

    if cuts:
        ch = collections.defaultdict(list)
        for c in cuts:
            if not c['cut']:
                continue
            for k in (3, 6):
                d2 = later(c['day'], k)
                if not d2:
                    continue
                a = [int(r['士气']) for r in by[c['day']].values() if r['军势'] == c['troops']]
                b = [int(r['士气']) for r in by[d2].values() if r['军势'] == c['troops']]
                if a and b:
                    ch[k].append(sum(b) / len(b) - sum(a) / len(a))
        for k in (3, 6):
            if ch[k]:
                print('断粮后 %d 天士气变化：平均 %+.1f（%d 次）' % (k, sum(ch[k]) / len(ch[k]), len(ch[k])))
    if guards:
        own = opp = 0
        n = 0
        for g in guards:
            d2 = later(g['day'], 5)
            if not d2:
                continue
            a0, e0 = by[g['day']].get(g['army']), by[g['day']].get(g['enemy'])
            if not a0 or not e0:
                continue
            a1, e1 = by[d2].get(g['army']), by[d2].get(g['enemy'])
            own += int(a0['兵数']) - (int(a1['兵数']) if a1 and a1['主将'] == a0['主将'] else 0)
            opp += int(e0['兵数']) - (int(e1['兵数']) if e1 and e1['主将'] == e0['主将'] else 0)
            n += 1
        print('派出后 5 天（%d 次，护粮第一支对被打的敌军）：我方掉 %d，敌军掉 %d，交换 %.2f' % (n, own, opp, opp / max(own, 1)))


if __name__ == '__main__':
    main()
