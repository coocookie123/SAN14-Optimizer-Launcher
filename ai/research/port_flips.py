"""港口（港所在区域的府）为什么丢：按每次「府易主」事件里的港，看易主前一旬谁踩上了港格（踩上后下一旬开头易主）、
从水上还是陆上来、守方部队在不在附近、之前 30 天攻方来了几个军势、后来夺没夺回（2026-11-15，BEHAVIOR_PLAN 第四十七节）。

  python -P ai/research/port_flips.py ai/runs/<run> [more runs] [--list]
"""
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import observe as o
from fu_stats import TASKS
import naval_ports as n
TER, WATER, AREA = n.TER, n.WATER, n.AREA
def dn(d): return o.day_number(o.parse_date(d))
out = []
for run in [a for a in sys.argv[1:] if not a.startswith('--')]:
    rows = o.read_csv(run, 'armies')
    byday = collections.defaultdict(list)
    for x in rows: byday[dn(x['日期'])].append(x)
    days = sorted(byday)
    key = lambda x: (x['部队'], x['主将'])
    at = {(dd, key(x)): x for dd in days for x in byday[dd]}
    areas = {int(a['区域']): a for a in o.read_csv(run, 'areas')}
    cityname = {int(f['编号']): f['名称'] for f in o.read_csv(run, 'footholds') if f['类型'] == '城市'}
    cityhex = {f['名称']: int(f['格']) for f in o.read_csv(run, 'footholds') if f['类型'] == '城市'}
    garr = collections.defaultdict(dict)
    for c in o.read_csv(run, 'cities'): garr[dn(c['日期'])][c['城市']] = (c['势力'], int(c['兵力']))
    gdays = sorted(garr)
    ev = o.read_csv(run, 'events')
    cflip = collections.defaultdict(list)
    for e in ev:
        if e['事件'] == '城市易主': cflip[e['对象']].append(dn(e['日期']))
    flips = [(dn(e['日期']), e) for e in ev if e['事件'] == '府易主' and e['对象'].endswith(':港')]
    later = collections.defaultdict(list)
    for d, e in flips: later[int(e['对象'].split(':')[1])].append((d, e['到']))
    for d, e in flips:
        if d < days[0] + 10: continue
        a, hx, _ = e['对象'].split(':'); a, hx = int(a), int(hx)
        frm, to = e['从'], e['到']
        cname = cityname.get(int(areas[a]['城市'])) if areas.get(a) and areas[a]['城市'] else None
        steps = [(dd, x) for dd in range(d - 10, d + 1) for x in byday.get(dd, []) if int(x['格']) == hx and x['势力'] == to]
        rec = dict(run=run[-15:], date=e['日期'], frm=frm, to=to, hx=hx, city=cname)
        if not steps:
            rec['kind'] = '城或势力转手' if (not frm or cname and any(abs(c - d) <= 12 for c in cflip.get(cname, []))) else '没查到谁踩'
            out.append(rec); continue
        sd, sx = steps[0]
        prev = [at.get((dd, key(sx))) for dd in range(sd - 3, sd)]
        by_water = any(p and TER[int(p['格'])] in WATER for p in prev)
        rec['kind'] = '踩港'
        rec['step'] = (TASKS.get(sx['军势任务'], sx['军势任务']), int(sx['兵数']), sx['主将'], '水上来' if by_water else '陆上来')
        # defenders: frm armies within 6 of the port on the stepping day .. flip day; within 12 in the 20 days before stepping
        d6 = {key(x): int(x['兵数']) for dd in range(sd - 2, d + 1) for x in byday.get(dd, []) if x['势力'] == frm and o.hex_distance(int(x['格']), hx) <= 6}
        d12 = {key(x): int(x['兵数']) for dd in range(sd - 20, sd + 1) for x in byday.get(dd, []) if x['势力'] == frm and o.hex_distance(int(x['格']), hx) <= 12}
        e12 = {}
        for dd in range(sd - 30, d + 1):
            for x in byday.get(dd, []):
                if x['势力'] == to and o.hex_distance(int(x['格']), hx) <= 12: e12.setdefault(key(x), (dd, int(x['兵数']), x['军势']))
        g = garr[max([s for s in gdays if s <= sd], default=gdays[0])]
        rec['own_city_soldiers'] = g.get(cname, (None, 0))[1] if cname and g.get(cname, (None,))[0] == frm else None
        rec['gar20'] = sum(sol for c, (f, sol) in g.items() if f == frm and c in cityhex and o.hex_distance(cityhex[c], hx) <= 20)
        rec['d6'], rec['d12'] = d6, d12
        rec['e12'] = e12
        rec['jun'] = len({v[2] for v in e12.values()})
        rec['esol'] = sum(v[1] for v in e12.values())
        bk = [dd for dd, t in later[hx] if dd > d and t == frm]
        rec['back'] = bk[0] - d if bk else None
        out.append(rec)
k = collections.Counter(r['kind'] for r in out)
print('港易主 %d 次：%s' % (len(out), dict(k)))
st = [r for r in out if r['kind'] == '踩港']
print('\n踩港 %d 次：' % len(st))
print('  谁踩：', dict(collections.Counter(r['step'][0] for r in st)), '；', dict(collections.Counter(r['step'][3] for r in st)))
sol = sorted(r['step'][1] for r in st)
print('  踩港部队兵数中位 %d（≤3000 的 %d 次）' % (sol[len(sol) // 2], sum(s <= 3000 for s in sol)))
print('  踩上到易主之间，守方部队到过港 6 格内：%d 次；前 20 天守方部队到过港 12 格内：%d 次；都没有 %d 次' % (
    sum(bool(r['d6']) for r in st), sum(bool(r['d12']) for r in st), sum(not r['d6'] and not r['d12'] for r in st)))
print('  港所在城是守方的 %d 次，城里兵中位 %s' % (sum(r['own_city_soldiers'] is not None for r in st),
      sorted(r['own_city_soldiers'] for r in st if r['own_city_soldiers'] is not None)[sum(r['own_city_soldiers'] is not None for r in st) // 2] if any(r['own_city_soldiers'] is not None for r in st) else '-'))
print('  攻方 30 天内来到港 12 格内：1 个军势 %d 次，2 个 %d 次，3 个以上 %d 次' % (
    sum(r['jun'] == 1 for r in st), sum(r['jun'] == 2 for r in st), sum(r['jun'] >= 3 for r in st)))
print('  守方之后夺回 %d 次；没夺回 %d 次' % (sum(r['back'] is not None for r in st), sum(r['back'] is None for r in st)))
for r in (st if '--list' in sys.argv else []):
    print('  %s %s %s→%s 港%d（%s）：%s；守方 6 格内 %d 支 %d 兵，20 天前 12 格内 %d 支；城兵 %s；20 格内守城兵 %d；攻方 %d 军势 %d 兵；%s' % (
        r['run'], r['date'], r['frm'], r['to'], r['hx'], r['city'], r['step'], len(r['d6']), sum(r['d6'].values()), len(r['d12']),
        r['own_city_soldiers'], r['gar20'], r['jun'], r['esol'], ('%d 天后夺回' % r['back']) if r['back'] else '没夺回'))

def cat(r):
    small = r['jun'] <= 1
    if r['d6']: return '守方部队到了港 6 格内'
    if r['d12']: return '守方部队在 12 格内，没去港边'
    return '附近没有守方部队，' + ('只来了 1 个军势' if small else '来了 2 个以上军势')
g = collections.defaultdict(list)
for r in st: g[cat(r)].append(r)
for c, rs in sorted(g.items(), key=lambda kv: -len(kv[1])):
    es = sorted(r['esol'] for r in rs); ss = sorted(r['step'][1] for r in rs)
    cs = sorted(r['own_city_soldiers'] for r in rs if r['own_city_soldiers'] is not None)
    print('%s：%d 次（水上来 %d）；踩港部队兵中位 %d；攻方 30 天总兵中位 %d；港所在城是守方的 %d 次、城兵中位 %s；任务 %s；夺回 %d' % (
        c, len(rs), sum(r['step'][3] == '水上来' for r in rs), ss[len(ss) // 2], es[len(es) // 2], len(cs), cs[len(cs) // 2] if cs else '-',
        dict(collections.Counter(r['step'][0] for r in rs)), sum(r['back'] is not None for r in rs)))
