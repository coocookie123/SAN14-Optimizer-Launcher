"""任务完成后的行为连续性：回城后很快又出城的次数，和打退进攻后顺势攻城的机会。

  python -P ai/research/continuity.py ai/runs/<run> [more runs] [--months 12]

1. 回城再出：同一武将一次出征结束（回到城里），30 天内又出征；看新出征和上次结束地点（上一次最后一天的位置）
   的距离、新任务、回城路上和再出城各花了多少天（按上一次最后 3 天离城的距离估）。
2. 顺势攻城的机会：一个敌方军势在我方城 10 格内溃灭或撤走（第二天不在了或任务变成撤退、回城），当天我方
   8 格内的部队兵力还剩出征时的 60% 以上、士气 70 以上；15 格内有敌方的城，城里兵不到这些部队合计的 2 倍。
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402

KIND = {'1': '迎击', '2': '攻城', '8': '攻城', '9': '攻城', '10': '攻城', '22': '攻城', '3': '区域攻击', '13': '区域攻击',
        '14': '区域攻击', '15': '区域攻击', '16': '区域攻击', '21': '区域攻击', '12': '运输', '5': '建设', '17': '回城',
        '18': '回城', '0': '撤退', '7': '集合', '23': '涂地防守'}


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
    didx = {d: i for i, d in enumerate(days)}
    cities = {f['名称']: int(f['格']) for f in o.read_csv(p, 'footholds') if f['类型'] == '城市'}
    hist = collections.defaultdict(list)
    for c in o.read_csv(p, 'cities'):
        hist[c['城市']].append((dn(c['日期']), c['势力'], int(c['兵力'])))
    for e in o.read_csv(p, 'events'):
        if e['事件'] == '城市易主':
            hist[e['对象']].append((dn(e['日期']), e['到'], None))
    for v in hist.values():
        v.sort(key=lambda t: t[0])

    def city_state(name, d):
        w, s = None, None
        for dd, f, sol in hist.get(name, []):
            if dd <= d or w is None:
                w = f
                if sol is not None:
                    s = sol
        return w, s
    # trips per general
    trips = collections.defaultdict(list)
    for d in days:
        for x in byday[d]:
            trips[x['主将']].append(x)
    re_out = []
    for gen, seq in trips.items():
        parts, cur = [], [seq[0]]
        for a, b in zip(seq, seq[1:]):
            if didx[b['日期']] - didx[a['日期']] > 1:     # consecutive records (months have 30 days, dn counts 31)
                parts.append(cur)
                cur = []
            cur.append(b)
        parts.append(cur)
        for t1, t2 in zip(parts, parts[1:]):
            gap = didx[t2[0]['日期']] - didx[t1[-1]['日期']]
            if gap > 30 or t1[0]['军势任务'] == '12' or t2[0]['军势任务'] == '12':
                continue
            end_task = KIND.get(t1[-1]['军势任务'], t1[-1]['军势任务'])
            # where the previous trip did its job: the farthest point from where it came home
            home = int(t1[-1]['格'])
            far = max(t1, key=lambda x: o.hex_distance(int(x['格']), home))
            farh = int(far['格'])
            # how far the new trip goes back toward that spot: closest it gets within 20 days
            close = min(o.hex_distance(int(x['格']), farh) for x in t2[:20])
            re_out.append((gap, o.hex_distance(farh, home), close, KIND.get(t1[0]['军势任务'], t1[0]['军势任务']),
                           KIND.get(t2[0]['军势任务'], t2[0]['军势任务']), end_task))
    out = []
    n = len(re_out)
    back = [r for r in re_out if r[1] >= 6 and r[2] <= 4]
    out.append('回城后 30 天内又出征：%d 次；其中上次去过离城 6 格以上、这次又回到那里 4 格内的 %d 次（白走路）；'
               '这些的回城到再出城中位 %d 天、上次离城中位 %d 格' % (
                   n, len(back), sorted(r[0] for r in back)[len(back) // 2] if back else 0,
                   sorted(r[1] for r in back)[len(back) // 2] if back else 0))
    c = collections.Counter((r[3], r[4]) for r in back)
    out.append('    按「上次任务 → 这次任务」：%s' % '、'.join('%s→%s %d' % (a, b, v) for (a, b), v in c.most_common(6)))
    # follow-up opportunities
    seen = set()
    peak = {}
    for x in rows:
        k = (x['部队'], x['主将'])
        peak[k] = max(peak.get(k, 0), int(x['兵数']))
    opp = 0
    taken = 0
    nxt = {days[i]: days[i + 1] for i in range(len(days) - 1)}
    for d in days:
        nd = nxt.get(d)
        if not nd:
            continue
        cur = collections.defaultdict(list)
        for x in byday[d]:
            cur[(x['势力'], x['军势'])].append(x)
        nxt_keys = {(x['势力'], x['军势']): x for x in byday[nd]}
        for (f, t), rs in cur.items():
            if rs[0]['军势任务'] in ('12', '0', '17', '18'):
                continue
            gone = (f, t) not in nxt_keys or nxt_keys[(f, t)]['军势任务'] in ('0', '17', '18')
            if not gone or sum(int(x['兵数']) for x in rs) < 10000:
                continue
            h = int(rs[0]['格'])
            near_c = [n_ for n_, ch in cities.items() if o.hex_distance(ch, h) <= 10 and city_state(n_, dn(d))[0] not in (None, f)]
            if not near_c:
                continue
            defender = city_state(near_c[0], dn(d))[0]
            ours = [x for x in byday[d] if x['势力'] == defender and o.hex_distance(int(x['格']), h) <= 8
                    and int(x['兵数']) >= 0.6 * peak.get((x['部队'], x['主将']), 1) and int(x['士气']) >= 70]
            if not ours:
                continue
            key = (defender, f, dn(d) // 10)
            if key in seen:
                continue
            seen.add(key)
            power = sum(int(x['兵数']) for x in ours)
            targets = [n_ for n_, ch in cities.items() if city_state(n_, dn(d))[0] == f and
                       any(o.hex_distance(int(x['格']), ch) <= 15 for x in ours) and (city_state(n_, dn(d))[1] or 1e9) < 2 * power]
            if targets:
                opp += 1
                # did the defender attack one of those cities within 30 days?
                later = [y for dd in days if 0 < dn(dd) - dn(d) <= 30 for y in byday[dd]
                         if y['势力'] == defender and y['军势任务'] in ('2', '8', '9', '10', '22')]
                tnames = {f_['编号'] for f_ in o.read_csv(p, 'footholds') if f_['名称'] in targets}
                taken += any(y['军势目标'] in tnames for y in later)
    out.append('打退一个 1 万兵以上的敌方军势后、附近我方部队状态好、15 格内有守兵不到我方 2 倍的敌城：%d 次；'
               '30 天内原版真的去打了这些城：%d 次' % (opp, taken))
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
