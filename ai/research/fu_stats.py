"""府（各区域的府）在交战和行军里起多大作用、原版怎么踩府。要 2026-11-12 以后的记录（armies.csv 有「区域」
「格子势力」「府势力」三列，另有 areas.csv、fu.csv）。

  python -P ai/research/fu_stats.py ai/runs/<run> [more runs] [--months 12]

1. 贴身交换比按位置分（另按府分差分，府分见 observe.py）：我方部队贴着敌军那天，脚下这一格涂的是我方 / 对方 / 别家，所在区域的府是我方 / 对方
   / 别家。交换比＝敌方掉兵 ÷ 我方掉兵（第二天和当天比；一支敌军被几支我方贴着时平分，同 check_modules）。
2. 踩府：部队目标是某个府的格（areas.csv 的府格）且这个府不是自己的。按军势任务分，看是不是本队、离本队几格、
   之后府有没有变成它的。
3. 每月府易主多少（fu.csv）。
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402

TASKS = {'1': '迎击', '2': '攻城', '8': '攻城', '9': '攻城', '10': '攻城', '22': '攻城', '3': '区域攻击',
         '13': '区域攻击', '14': '区域攻击', '15': '区域攻击', '16': '区域攻击', '21': '区域攻击', '11': '占领',
         '12': '运输', '17': '回城', '18': '回城', '7': '集合', '23': '涂地防守', '0': '撤退', '4': '攻设施',
         '5': '建设', '6': '随本队'}


def dn(d):
    return o.day_number(o.parse_date(d))


def rel(who, me, foe):
    if not who:
        return '无主'
    return '我方' if who == me else '对方' if who == foe else '别家'


def analyse(path, months):
    rows = o.read_csv(path, 'armies')
    if not rows or '府势力' not in rows[0]:
        return ['（这份记录没有区域和府的列）']
    days = sorted({r['日期'] for r in rows}, key=dn)
    lo = dn(days[0])
    rows = [r for r in rows if not months or dn(r['日期']) < lo + months * 31]
    byday = collections.defaultdict(list)
    for r in rows:
        byday[r['日期']].append(r)
    days = sorted(byday, key=dn)
    nxt = {days[i]: days[i + 1] for i in range(len(days) - 1)}
    by = {(x['日期'], x['部队']): x for x in rows}
    fuhex = {}
    for a in o.read_csv(path, 'areas'):
        fuhex[int(a['府格'])] = a['区域']
    out = []
    fh = [int(f['格']) for f in o.read_csv(path, 'footholds')]
    near_fh = {}

    def by_city(h):
        if h not in near_fh:
            near_fh[h] = any(o.hex_distance(h, c) <= 4 for c in fh)
        return near_fh[h]
    # 1. contact exchange by place
    ex = collections.defaultdict(lambda: [0.0, 0.0, 0])     # key -> our loss, their loss, army-days
    for d, rs in byday.items():
        nd = nxt.get(d)
        if not nd:
            continue
        for x in rs:
            hx = int(x['格'])
            foes = [y for y in rs if y['势力'] != x['势力'] and o.hex_distance(hx, int(y['格'])) <= 1]
            if not foes:
                continue
            foe = collections.Counter(y['势力'] for y in foes).most_common(1)[0][0]
            n = by.get((nd, x['部队']))
            ours = max(0, int(x['兵数']) - int(n['兵数'])) if n and n['主将'] == x['主将'] else None
            if ours is None:
                continue
            theirs = 0.0
            for y in foes:
                m = by.get((nd, y['部队']))
                if m and m['主将'] == y['主将']:
                    k = sum(1 for z in rs if z['势力'] == x['势力'] and o.hex_distance(int(z['格']), int(y['格'])) <= 1)
                    theirs += max(0, int(y['兵数']) - int(m['兵数'])) / max(k, 1)
            field = not by_city(hx) and x['军势任务'] not in ('2', '8', '9', '10', '22')
            fukey = rel(x['府势力'], x['势力'], foe)
            for key in (('格', rel(x['格子势力'], x['势力'], foe)), ('府', fukey),
                        ('野战府', fukey) if field else ('城边府', fukey)):
                e = ex[key]
                e[0] += ours
                e[1] += theirs
                e[2] += 1
    label = {'格': '按脚下这一格涂成谁的（部队站上去就涂成自己的，只作核对）', '府': '按所在区域的府是谁的',
             '野战府': '同上，只算野战（离城、关隘 5 格以上，不在攻城）', '城边府': '同上，城、关隘 4 格内或在攻城'}
    for kind in ('格', '府', '野战府', '城边府'):
        parts = []
        for who in ('我方', '对方', '别家', '无主'):
            e = ex.get((kind, who))
            if e and e[2]:
                parts.append('%s %s（%d 部队日）' % (who, ('%.2f' % (e[1] / e[0])) if e[0] else '-', e[2]))
        out.append('贴身交换比，%s：%s' % (label[kind], '；'.join(parts)))
    # 1b. by the 府分 gap (府加成's input: own + neighbouring areas held, by painted share), field fights only
    if '府分' in rows[0]:
        gap = collections.defaultdict(lambda: [0.0, 0.0, 0])
        for d, rs in byday.items():
            nd = nxt.get(d)
            if not nd:
                continue
            for x in rs:
                hx = int(x['格'])
                if by_city(hx):
                    continue
                foes = [y for y in rs if y['势力'] != x['势力'] and o.hex_distance(hx, int(y['格'])) <= 1]
                n = by.get((nd, x['部队']))
                if not foes or not n or n['主将'] != x['主将'] or not x['府分']:
                    continue
                dg = float(x['府分']) - sum(float(y['府分'] or 0) for y in foes) / len(foes)
                b = '≤-3' if dg <= -3 else '-3~-1' if dg < -1 else '-1~1' if dg <= 1 else '1~3' if dg < 3 else '≥3'
                task = '攻城' if x['军势任务'] in ('2', '8', '9', '10', '22') else '迎击' if x['军势任务'] == '1' else '其他'
                for key in ((task, b), ('全部', b)):
                    e = gap[key]
                    e[0] += max(0, int(x['兵数']) - int(n['兵数']))
                    for y in foes:
                        m = by.get((nd, y['部队']))
                        if m and m['主将'] == y['主将']:
                            k = sum(1 for z in rs if z['势力'] == x['势力'] and o.hex_distance(int(z['格']), int(y['格'])) <= 1)
                            e[1] += max(0, int(y['兵数']) - int(m['兵数'])) / max(k, 1)
                    e[2] += 1
        for task in ('全部', '迎击', '攻城', '其他'):
            parts = ['%s %s（%d）' % (b, ('%.2f' % (gap[(task, b)][1] / gap[(task, b)][0])) if gap[(task, b)][0] else '-',
                                     gap[(task, b)][2]) for b in ('≤-3', '-3~-1', '-1~1', '1~3', '≥3') if gap[(task, b)][2]]
            out.append('野战贴身交换比按府分差（我方减对方，%s）：%s' % (task, '；'.join(parts)))
    # 1c. A/B: by the id parity of our force (A/B runs: the module only for odd ids)
    if '府分' in rows[0]:
        fid = {}
        for f in o.read_csv(path, 'forces'):
            fid[f['君主']] = int(f['势力'])
        grp = lambda name: None if name not in fid else ('奇数' if fid[name] % 2 else '偶数')  # noqa: E731
        ab = collections.defaultdict(lambda: [0.0, 0, 0.0, 0.0])    # group: gap sum, n, our loss, their loss
        for d, rs in byday.items():
            nd = nxt.get(d)
            if not nd:
                continue
            for x in rs:
                hx = int(x['格'])
                if by_city(hx) or not x['府分']:
                    continue
                foes = [y for y in rs if y['势力'] != x['势力'] and o.hex_distance(hx, int(y['格'])) <= 1]
                n = by.get((nd, x['部队']))
                g = grp(x['势力'])
                if not foes or not n or n['主将'] != x['主将'] or not g:
                    continue
                e = ab[g]
                e[0] += float(x['府分']) - sum(float(y['府分'] or 0) for y in foes) / len(foes)
                e[1] += 1
                e[2] += max(0, int(x['兵数']) - int(n['兵数']))
                for y in foes:
                    m = by.get((nd, y['部队']))
                    if m and m['主将'] == y['主将']:
                        k = sum(1 for z in rs if z['势力'] == x['势力'] and o.hex_distance(int(z['格']), int(y['格'])) <= 1)
                        e[3] += max(0, int(y['兵数']) - int(m['兵数'])) / max(k, 1)
        fu_rows = o.read_csv(path, 'fu')
        ms = sorted({r['日期'] for r in fu_rows}, key=dn)
        held = {}
        for m_ in (ms[0], ms[-1]) if ms else ():
            c = collections.Counter(grp(r['府势力']) for r in fu_rows if r['日期'] == m_ and r['府势力'])
            held[m_] = c
        parts = []
        for g in ('奇数', '偶数'):
            e = ab[g]
            if e[1]:
                parts.append('%s：野战贴身时府分差平均 %+.2f（%d 部队日），交换比 %s，府 %s' % (
                    g, e[0] / e[1], e[1], ('%.2f' % (e[3] / e[2])) if e[2] else '-',
                    ' -> '.join(str(held[m_].get(g, 0)) for m_ in held)))
        out.append('A/B（按势力编号奇偶）：' + '；'.join(parts))
    # 2. stepping on 府
    steps = {}
    for x in rows:
        t = x['部队目标']
        if not t.startswith('格'):
            continue
        h = int(t[1:])
        if h not in fuhex:
            continue
        key = (x['部队'], x['主将'], h)
        if key not in steps:
            steps[key] = x
    cnt = collections.Counter()
    lead_far = []
    got = collections.Counter()
    owner_kind = collections.Counter()
    for (slot, gen, h), x in steps.items():
        task = TASKS.get(x['军势任务'], x['军势任务'])
        cnt[task] += 1
        # whose 府 was it: the 府 owner of its area on that day, seen from any army standing in the area
        area = fuhex[h]
        holders = [y['府势力'] for y in byday[x['日期']] if y['区域'] == area]
        holder = holders[0] if holders else '?'
        owner_kind[rel(holder, x['势力'], None) if holder != '?' else '?'] += 1
        mates = [y for y in byday[x['日期']] if y['军势'] == x['军势'] and y['势力'] == x['势力']]
        lead = next((y for y in mates if y['本队'] == '是'), None)
        if lead and lead['部队'] != slot and len(mates) >= 2:
            lead_far.append(o.hex_distance(int(lead['格']), int(x['格'])))
        # did it become theirs within 20 days (any army of that force later standing in the area sees it)
        i = days.index(x['日期'])
        later = [y for dd in days[i:i + 21] for y in byday[dd] if y['区域'] == area]
        got['拿到' if any(y['府势力'] == x['势力'] for y in later) else '没拿到' if later else '看不出'] += 1
    out.append('踩府（部队目标是不属于自己的府）：%d 次；按军势任务 %s；当时府是 %s' % (
        len(steps), dict(cnt.most_common()), dict(owner_kind)))
    if lead_far:
        lead_far.sort()
        out.append('    其中多支部队的军势里不是本队的 %d 次，离本队中位 %d 格，超过 6 格 %d 次' % (
            len(lead_far), lead_far[len(lead_far) // 2], sum(d > 6 for d in lead_far)))
    out.append('    20 天内府变成自己的：%s' % dict(got))
    # 3. 府 changing hands
    fu = o.read_csv(path, 'fu')
    bym = collections.defaultdict(dict)
    for r in fu:
        bym[r['日期']][r['区域']] = r['府势力']
    ms = sorted(bym, key=dn)
    ch = [sum(1 for a, w in bym[m2].items() if bym[m1].get(a) not in (None, w)) for m1, m2 in zip(ms, ms[1:])]
    if ch:
        out.append('府易主：每月 %s（共 %d 个府）' % (ch, len(bym[ms[0]])))
    # time spent: field army-days by the 府 of where they stand
    place = collections.Counter()
    for x in rows:
        if x['军势任务'] in ('12', '17', '18'):
            continue
        place['自己的府' if x['府势力'] == x['势力'] else '别人的府' if x['府势力'] else '无主'] += 1
    out.append('野战部队日（不含运输、回城）站在哪里：%s' % dict(place))
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
