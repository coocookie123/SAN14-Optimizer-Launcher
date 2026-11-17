"""攻城落单贴敌之前 12 天里，「攻城落单不单打」（DLL 功能 siegelone）的条件满足过没有；没满足是卡在哪一条
（2026-11-16，BEHAVIOR_PLAN 第五十节）。另外跟日志里被按住的部队：之后 20 天有没有落单贴敌、掉了多少兵。

  python -P ai/research/siege_lone_why.py ai/runs/<run>[@odd|@even] [more runs]
"""
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import observe as o  # noqa: E402

SIEGE = ('2', '8', '9', '10', '22')
SL_MIN, SL_MAX, SL_NEAR, SL_JOIN, SL_AHEAD = 5, 8, 4, 12, 2
BACK = 12
NEW = re.compile(r'(\d+)-(\d+)-(\d+) siegelone: army (\d+) \(force')
ORDER = ['条件满足过、日志里按住过', '条件满足过、日志里没按住', '在 5–8 格时后面没有同目标友军', '在 5–8 格时身边 4 格内有友军',
         '进 5–8 格时已经贴着敌军', '8 格外就贴上了', '出现时已在 5 格内', '其他']


def dn(d):
    return o.day_number(o.parse_date(d))


def main():
    why = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0]))
    after = collections.Counter()
    elig = collections.defaultdict(collections.Counter)
    hl = [0, 0]
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
        at = {(d, key(x)): x for d in days for x in byday[d]}
        holds = collections.defaultdict(list)          # slot -> day numbers of new holds
        log = os.path.join(run, 'ai_dll.log')
        if os.path.exists(log):
            for m in NEW.finditer(open(log, encoding='utf-8-sig', errors='replace').read()):
                holds[m.group(4)].append(o.day_number((int(m.group(1)), int(m.group(2)), int(m.group(3)))))

        def touch(x, d):
            h = int(x['格'])
            return any(y['势力'] != x['势力'] and o.hex_distance(int(y['格']), h) == 1 for y in byday[d])

        def company(x, d):
            h = int(x['格'])
            return any(y['势力'] == x['势力'] and y is not x and o.hex_distance(int(y['格']), h) <= SL_NEAR for y in byday[d])

        def behind(x, d, dist):
            h = int(x['格'])
            g = cpos[x['军势目标']]
            return any(y['势力'] == x['势力'] and y is not x and y['军势任务'] in SIEGE and y['军势目标'] == x['军势目标']
                       and o.hex_distance(int(y['格']), h) <= SL_JOIN and o.hex_distance(int(y['格']), g) >= dist + SL_AHEAD
                       for y in byday[d])

        def streak_loss(k, i):
            loss = 0
            while i < len(days) - 1:
                cur, nx = at.get((days[i], k)), at.get((days[i + 1], k))
                if not cur or not touch(cur, days[i]):
                    break
                loss += max(int(cur['兵数']) - (int(nx['兵数']) if nx else 0), 0)
                i += 1
            return loss

        inc = set()
        for i, d in enumerate(days):
            for x in byday[d]:
                k = key(x)
                if x['军势任务'] not in SIEGE or x['军势目标'] not in cpos:
                    continue
                if not touch(x, d):
                    inc.discard(k)
                    continue
                if company(x, d) or k in inc:
                    continue
                inc.add(k)
                seen = []
                for j in range(max(0, i - BACK), i):
                    p = at.get((days[j], k))
                    if p and p['军势任务'] in SIEGE and p['军势目标'] == x['军势目标']:
                        dist = o.hex_distance(int(p['格']), cpos[p['军势目标']])
                        seen.append((dist, company(p, days[j]), behind(p, days[j], dist), touch(p, days[j]), days[j]))
                band = [s for s in seen if SL_MIN <= s[0] <= SL_MAX]
                ok = [s for s in band if not s[1] and s[2] and not s[3]]
                if ok:
                    r = ORDER[0] if any(ok[0][4] - 2 <= h <= d for h in holds.get(x['部队'], [])) else ORDER[1]
                elif any(not s[1] and not s[3] for s in band):
                    r = ORDER[2]
                elif any(s[1] for s in band):
                    r = ORDER[3]
                elif band:
                    r = ORDER[4]
                elif seen and min(s[0] for s in seen) > SL_MAX or not seen and o.hex_distance(int(x['格']), cpos[x['军势目标']]) > SL_MAX:
                    r = ORDER[5]
                elif not seen or seen[0][0] < SL_MIN:
                    r = ORDER[6]
                else:
                    r = ORDER[7]
                v = why[side(x['势力'])][r]
                v[0] += 1
                v[1] += streak_loss(k, i)
        # every army whose conditions hold (by the csv) for the first time on a target, both sides: the next 20 days
        first = set()
        for i, d in enumerate(days):
            for x in byday[d]:
                k = key(x)
                if x['军势任务'] not in SIEGE or x['军势目标'] not in cpos or (k, x['军势目标']) in first:
                    continue
                dist = o.hex_distance(int(x['格']), cpos[x['军势目标']])
                if not (SL_MIN <= dist <= SL_MAX) or company(x, d) or touch(x, d) or not behind(x, d, dist):
                    continue
                first.add((k, x['军势目标']))
                e = elig[side(x['势力'])]
                e['满足条件的部队'] += 1
                e['其中日志里按住了'] += any(d - 1 <= h <= d + 3 for h in holds.get(x['部队'], []))
                lone = withc = False
                last = x
                loss = 0
                for j in range(i, len(days)):
                    if days[j] > d + 20:
                        break
                    q = at.get((days[j], k))
                    if not q:
                        break
                    if touch(q, days[j]):
                        nx = at.get((days[j + 1], k)) if j + 1 < len(days) else q
                        loss += max(int(q['兵数']) - (int(nx['兵数']) if nx else 0), 0)
                        if company(q, days[j]):
                            withc = True
                        else:
                            lone = True
                    last = q
                e['之后 20 天落单贴过敌' if lone else '之后 20 天只在有友军时贴敌' if withc else '之后 20 天没贴过敌'] += 1
                e['没了'] += at.get((min(d + 21, days[-1]), k)) is None and int(last['兵数']) < 2500
                e['起始兵'] += int(x['兵数'])
                e['贴敌掉兵'] += loss
        # the held armies: the 20 days after the hold began
        for slot, hd in holds.items():
            for h0 in hd:
                x = next((y for y in byday.get(h0, []) if y['部队'] == slot), None)
                if not x:
                    after['找不到这支部队'] += 1
                    continue
                k = key(x)
                lone = withc = False
                last = x
                for d in [d for d in days if h0 <= d <= h0 + 20]:
                    p = at.get((d, k))
                    if not p:
                        break
                    last = p
                    if touch(p, d):
                        if company(p, d):
                            withc = True
                        else:
                            lone = True
                gone = at.get((min(h0 + 21, days[-1]), k)) is None and int(last['兵数']) < 2500
                after['之后 20 天落单贴敌' if lone else '之后 20 天只在有友军时贴敌' if withc else '之后 20 天没贴过敌'] += 1
                after['其中没了'] += gone
                hl[0] += int(x['兵数'])
                hl[1] += 0 if gone else int(last['兵数'])
    for sd in sorted(why):
        n = sum(v[0] for v in why[sd].values())
        print('== %s：落单贴敌 %d 段，掉 %d' % (sd, n, sum(v[1] for v in why[sd].values())))
        for r in ORDER:
            if r in why[sd]:
                print('  %-28s %3d 段 掉 %6d' % (r, why[sd][r][0], why[sd][r][1]))
    for sd in sorted(elig):
        e = elig[sd]
        print('== %s：%s；贴敌掉兵占起始兵 %d%%' % (sd, '，'.join('%s %d' % (k, e[k]) for k in (
            '满足条件的部队', '其中日志里按住了', '之后 20 天落单贴过敌', '之后 20 天只在有友军时贴敌', '之后 20 天没贴过敌', '没了', '起始兵', '贴敌掉兵')),
            100 * e['贴敌掉兵'] // max(e['起始兵'], 1)))
    if after:
        print('按住过的部队：' + '，'.join('%s %d' % kv for kv in after.items()) + '；兵 %d → %d' % (hl[0], hl[1]))


if __name__ == '__main__':
    main()
