"""水战机会：敌军坐船靠近别家港口时，守方有没有出来、在哪交手、谁吃亏、港口丢没丢（2026-11-15，BEHAVIOR_PLAN 第四十七节）。

  python -P ai/research/naval_ports.py ai/runs/<run> [more runs] [--months 12] [--r 12] [--parity]
  A/B：写成 ai/runs/<run>@odd 或 @even（这一局开模块的是奇数还是偶数势力），--parity 就按「开的一方 / 不开的一方」合计。

港 = 地形 18 的 40 格（research/terrain_exce.bin），每个港是所在区域（research/area_exce.bin）的府格，港的主人 =
那个区域的府势力（fu.csv 每月一次，加 events.csv 每天的「府易主」）。

一段「靠港」：一支部队在水面（地形 14、24），离一个别家的港 r 格内（默认 12），按天连起来（隔 2 天以内算同一段）。
每段记：
- 敌军：任务（fu_stats.TASKS）、兵数、它的军势目标城是不是港主人的；最先进入 24 格是在进入 r 格前几天（提前量）。
- 守方反应：港主人的部队有没有到它 6 格内（几天后到、任务）；有没有贴上（相邻格）；贴上的那些部队日里两边掉兵
  （第二天同一部队少了多少；贴着几支就平分），分「敌在水上、守方在陆上」「两边都在水上」等。
- 结果：之后 10 天内敌军有没有在港所在区域上岸（站到陆地格）；30 天内港（府）有没有归到敌军势力；港所在城有没有易主。
"""
import collections
import os
import struct
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import observe as o  # noqa: E402
from fu_stats import TASKS  # noqa: E402

RES = os.path.join(HERE, 'research')
TER = open(os.path.join(RES, 'terrain_exce.bin'), 'rb').read()
AREA = struct.unpack('<48400H', open(os.path.join(RES, 'area_exce.bin'), 'rb').read())
WATER = {14, 24}
PORTS = [h for h in range(len(TER)) if TER[h] == 18]
NEAR = 6        # 守方部队离敌军几格内算「出来了」
FAR = 24        # 优化伴侣的临水防御范围


def dn(d):
    return o.day_number(o.parse_date(d))


def analyse(run, months, r):
    rows = o.read_csv(run, 'armies')
    lo = min(dn(x['日期']) for x in rows)
    rows = [x for x in rows if not months or dn(x['日期']) < lo + months * 31]
    byday = collections.defaultdict(list)
    for x in rows:
        byday[dn(x['日期'])].append(x)
    days = sorted(byday)
    key = lambda x: (x['部队'], x['主将'])  # noqa: E731
    at = {(d, key(x)): x for d in days for x in byday[d]}
    nxt = {days[i]: days[i + 1] for i in range(len(days) - 1)}
    # owner of every area's 府 by day: monthly snapshot, then the daily flips
    areas = {int(a['区域']): a for a in o.read_csv(run, 'areas')}
    snap = collections.defaultdict(dict)
    for f in o.read_csv(run, 'fu'):
        snap[dn(f['日期'])][int(f['区域'])] = f['府势力']
    flips = collections.defaultdict(list)
    for e in o.read_csv(run, 'events'):
        if e['事件'] == '府易主':
            flips[dn(e['日期'])].append((int(e['对象'].split(':')[0]), e['到']))
    owner, cur = {}, {}
    sdays = sorted(snap)
    for d in days:
        last = max([s for s in sdays if s <= d], default=None)
        if last is not None and cur.get('_snap') != last:
            cur = dict(snap[last]); cur['_snap'] = last
        for a, to in flips.get(d, []):
            cur[a] = to
        owner[d] = dict(cur)
    city_area = {int(a['城市']): int(a['区域']) for a in areas.values() if a['府格'] and int(a['府格']) in
                 {int(f['格']) for f in o.read_csv(run, 'footholds') if f['类型'] == '城市'}}
    city_of_area = {int(a['区域']): int(a['城市']) for a in areas.values() if a['城市']}

    # 1. episodes: (army, port) with the army on water within r of a port someone else owns
    ep = {}
    episodes = []
    for d in days:
        own = owner[d]
        for x in byday[d]:
            h = int(x['格'])
            if TER[h] not in WATER:
                continue
            for p in PORTS:
                po = own.get(AREA[p])
                if not po or po == x['势力']:
                    continue
                dist = o.hex_distance(h, p)
                if dist > r:
                    continue
                k = (key(x), p)
                e = ep.get(k)
                if e and d - e['last'] <= 2:
                    e['last'] = d
                    e['days'].add(d)
                    e['mind'] = min(e['mind'], dist)
                else:
                    e = {'army': key(x), 'port': p, 'pown': po, 'force': x['势力'], 'first': d, 'last': d, 'days': {d},
                         'mind': dist, 'task': TASKS.get(x['军势任务'], x['军势任务']), 'sol': int(x['兵数']),
                         'goal': x['军势目标']}
                    ep[k] = e
                    episodes.append(e)
    # one army near two ports of the same owner on the same days: keep the nearer port only
    seen = {}
    for e in sorted(episodes, key=lambda e: e['mind']):
        k = (e['army'], e['pown'], e['first'] // 5)
        e['dup'] = k in seen
        seen[k] = 1
    episodes = [e for e in episodes if not e['dup']]

    city_hex = {int(f['编号']): int(f['格']) for f in o.read_csv(run, 'footholds') if f['类型'] == '城市'}
    name_id = {f['名称']: int(f['编号']) for f in o.read_csv(run, 'footholds') if f['类型'] == '城市'}
    garr = collections.defaultdict(dict)
    for c in o.read_csv(run, 'cities'):
        garr[dn(c['日期'])][name_id.get(c['城市'])] = (c['势力'], int(c['兵力']))
    gdays = sorted(garr)
    # 2. each episode: lead time, defender response, contact losses, outcome
    for e in episodes:
        a = e['army']
        # lead: first day on water within FAR of the port, going back from the episode start
        lead = 0
        d = e['first']
        i = days.index(d)
        while i > 0:
            i -= 1
            x = at.get((days[i], a))
            if not x or TER[int(x['格'])] not in WATER or o.hex_distance(int(x['格']), e['port']) > FAR:
                break
            lead = e['first'] - days[i]
        e['lead'] = lead
        resp_day = None
        resp_task = None
        contact = collections.Counter()
        own_loss = foe_loss = 0
        for d in sorted(e['days']):
            x = at.get((d, a))
            if not x:
                continue
            h = int(x['格'])
            defs = [y for y in byday[d] if y['势力'] == e['pown']]
            near = [y for y in defs if o.hex_distance(int(y['格']), h) <= NEAR]
            if near and resp_day is None:
                resp_day = d
                resp_task = TASKS.get(near[0]['军势任务'], near[0]['军势任务'])
            touch = [y for y in near if o.hex_distance(int(y['格']), h) == 1]
            if not touch:
                continue
            nd = nxt.get(d)
            x2 = at.get((nd, a)) if nd else None
            fl = int(x['兵数']) - (int(x2['兵数']) if x2 else 0)
            for y in touch:
                y2 = at.get((nd, key(y))) if nd else None
                if not y2 or not x2:
                    continue
                side = ('敌水' if TER[h] in WATER else '敌陆') + ('守水' if TER[int(y['格'])] in WATER else '守陆')
                contact[side] += 1
                ol = int(y['兵数']) - int(y2['兵数'])
                own_loss += ol
                foe_loss += fl / len(touch)
                e.setdefault('by_side', collections.Counter())
                e['by_side'][side + '守损'] += ol
                e['by_side'][side + '敌损'] += fl / len(touch)
        e['resp'] = None if resp_day is None else resp_day - e['first']
        g = garr[max([s for s in gdays if s <= e['first']], default=gdays[0])]
        e['garrison'] = sum(sol for cid, (f, sol) in g.items() if f == e['pown'] and cid in city_hex
                            and o.hex_distance(city_hex[cid], e['port']) <= 20)
        x0 = at.get((e['first'], a))
        e['group'] = sum(int(y['兵数']) for y in byday[e['first']] if y['势力'] == e['force'] and x0
                         and o.hex_distance(int(y['格']), int(x0['格'])) <= 4)
        e['resp_task'] = resp_task
        e['contact'] = contact
        e['own_loss'], e['foe_loss'] = own_loss, foe_loss
        # landed in the port's area within 10 days after the episode
        e['landed'] = any((x := at.get((d, a))) and TER[int(x['格'])] not in WATER and AREA[int(x['格'])] == AREA[e['port']]
                          for d in days if e['first'] <= d <= e['last'] + 10)
        pa = AREA[e['port']]
        e['port_lost'] = any(owner[d].get(pa) == e['force'] for d in days if e['first'] < d <= e['last'] + 30)
        ca = city_area.get(city_of_area.get(pa))
        # the port's area can belong to a city its 府 owner does not hold: only a change to the enemy counts
        e['city_lost'] = bool(ca) and owner[e['first']].get(ca) != e['force'] and any(
            owner[d].get(ca) == e['force'] for d in days if e['first'] < d <= e['last'] + 30)
        e['in_area'] = any((x := at.get((d, a))) and AREA[int(x['格'])] == pa for d in e['days'])
        ga = city_area.get(int(e['goal'])) if e['goal'].isdigit() else None
        e['goal_is_owner'] = ga is not None and owner[e['first']].get(ga) == e['pown']
    return episodes, len(days)


def pct(a, b):
    return '%d%%' % (100 * a // b) if b else '-'


def report(label, eps, ndays):
    n = len(eps)
    print('== %s：%d 天，靠港 %d 段（一年约 %.0f 段）' % (label, ndays, n, n * 365 / max(ndays, 1)))
    if not n:
        return
    t = collections.Counter(e['task'] for e in eps)
    print('  敌军任务：' + '，'.join('%s %d' % kv for kv in t.most_common()))
    big = [e for e in eps if e['task'] not in ('运输',)]
    print('  不算运输 %d 段：兵数中位 %d；军势目标是港主人的城 %s；停留天数中位 %d；最近离港中位 %d 格' % (
        len(big), sorted(e['sol'] for e in big)[len(big) // 2] if big else 0, pct(sum(e['goal_is_owner'] for e in big), len(big)),
        sorted(len(e['days']) for e in big)[len(big) // 2] if big else 0, sorted(e['mind'] for e in big)[len(big) // 2] if big else 0))
    lead = sorted(e['lead'] for e in big)
    if lead:
        print('  从进 %d 格到进靠港范围的提前量：中位 %d 天，0 天（直接出现在近处）%s' % (FAR, lead[len(lead) // 2], pct(sum(v == 0 for v in lead), len(lead))))
    resp = [e for e in big if e['resp'] is not None]
    print('  守方部队到敌军 %d 格内：%s（%d 段），到的那天离开始中位 %s 天；来的任务：%s' % (
        NEAR, pct(len(resp), len(big)), len(resp), sorted(e['resp'] for e in resp)[len(resp) // 2] if resp else '-',
        '，'.join('%s %d' % kv for kv in collections.Counter(e['resp_task'] for e in resp).most_common(4))))
    con = [e for e in big if e['contact']]
    side = collections.Counter()
    for e in con:
        side.update(e['contact'])
    ol = sum(e['own_loss'] for e in con)
    fl = sum(e['foe_loss'] for e in con)
    print('  贴上 %s（%d 段）：部队对·天 %s；守方掉 %d、敌军掉 %d（敌损 ÷ 守损 %.2f）' % (
        pct(len(con), len(big)), len(con), dict(side), ol, fl, fl / max(ol, 1)))
    print('  结果：10 天内在港区上岸 %s，30 天内港归敌 %s，港所在城归敌 %s' % (
        pct(sum(e['landed'] for e in big), len(big)), pct(sum(e['port_lost'] for e in big), len(big)),
        pct(sum(e['city_lost'] for e in big), len(big))))
    for lab, xs in (('守方出来了', resp), ('守方没出来', [e for e in big if e['resp'] is None])):
        if xs:
            print('    %s %d 段：上岸 %s，港归敌 %s，城归敌 %s' % (lab, len(xs), pct(sum(e['landed'] for e in xs), len(xs)),
                                                   pct(sum(e['port_lost'] for e in xs), len(xs)), pct(sum(e['city_lost'] for e in xs), len(xs))))
    bs = collections.Counter()
    for e in con:
        bs.update(e.get('by_side', {}))
    for k in ('敌水守水', '敌水守陆', '敌陆守水', '敌陆守陆'):
        if bs[k + '守损'] or bs[k + '敌损']:
            print('    %s：守方掉 %d、敌军掉 %d（%.2f）' % (k, bs[k + '守损'], bs[k + '敌损'], bs[k + '敌损'] / max(bs[k + '守损'], 1)))
    noresp = [e for e in big if e['resp'] is None]
    if noresp:
        rich = [e for e in noresp if e['garrison'] >= 1.5 * max(e['group'], 1)]
        print('  守方没出来的 %d 段：20 格内自家城的兵 ≥ 敌军那一群（4 格内）1.5 倍的 %s（%d 段），其中港后来归敌 %s；'
              '兵不够的港归敌 %s' % (len(noresp), pct(len(rich), len(noresp)), len(rich),
                                pct(sum(e['port_lost'] for e in rich), len(rich)),
                                pct(sum(e['port_lost'] for e in noresp if e not in rich), len(noresp) - len(rich))))
    print('  进了港所在区域（在水上）%s' % pct(sum(e['in_area'] for e in big), len(big)))
    pf = collections.Counter('%s 的港 %d' % (e['pown'], e['port']) for e in big)
    print('  被靠最多的港：' + '，'.join('%s %d' % kv for kv in pf.most_common(5)) + '（共 %d 个港）' % len(pf))


def main():
    a = sys.argv[1:]
    months = int(a[a.index('--months') + 1]) if '--months' in a else 12
    r = int(a[a.index('--r') + 1]) if '--r' in a else 12
    runs = [x for i, x in enumerate(a) if not x.startswith('--') and (i == 0 or a[i - 1] not in ('--months', '--r'))]
    alleps, alldays = [], 0
    for arg in runs:
        run, _, on = arg.partition('@')    # run@odd / run@even: which side had the module on in this run
        eps, nd = analyse(run, months, r)
        fid = {f['君主']: int(f['势力']) for f in o.read_csv(run, 'forces')}
        for e in eps:
            e['pid'] = fid.get(e['pown'], 0)
            e['on'] = (e['pid'] % 2 == 1) == (on == 'odd') if on else None
        report(os.path.basename(run.rstrip('/\\')), eps, nd)
        alleps += eps
        alldays += nd
    if len(runs) > 1:
        report('合计', alleps, alldays)
    if '--parity' in a and any(e['on'] is not None for e in alleps):     # A/B with run@odd / run@even
        report('港主人开了模块', [e for e in alleps if e['on']], alldays)
        report('港主人没开模块', [e for e in alleps if e['on'] is False], alldays)
    elif '--parity' in a:                  # A/B: by the port owner's force id
        report('港主人是奇数势力', [e for e in alleps if e['pid'] % 2 == 1], alldays)
        report('港主人是偶数势力', [e for e in alleps if e['pid'] % 2 == 0], alldays)


if __name__ == '__main__':
    main()
