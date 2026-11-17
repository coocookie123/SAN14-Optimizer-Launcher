"""水上的部队：多少部队日在水面、谁在水上、水上接战掉兵多少，和陆上比。

  python -P ai/research/naval_stats.py ai/runs/<run> [more runs] [--months 12]

地形表 research/terrain_exce.bin：每格 1 字节，格子对象 +0x10（传承版地图，从内存里导出，静态）。
按建业、柴桑、江陵、江夏周围核对：14 江面（长江一大片），24 江里的另一种水面（推测浅滩或奔流），
18 港（40 格，都贴着水），16 城（每城 7 格），15 府，17 关，19 关两边的山（过不去），23 海和地图外，
20 多在江岸和城边（推测断崖之类过不去的）。部队站过的只有 1–18 和 24。
接战＝相邻格（距离 1）有别的势力的部队；掉兵＝第二天同一部队兵数少了多少。
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402

WATER = {14, 24}
PORT = 18
TER = open(os.path.join(HERE, 'research', 'terrain_exce.bin'), 'rb').read()


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
    nxt = {days[i]: days[i + 1] for i in range(len(days) - 1)}
    key = lambda x: (x['部队'], x['主将'])  # noqa: E731
    total = water = 0
    by_force = collections.Counter()
    by_task = collections.Counter()
    forms = collections.Counter()
    sorties_w = set()
    sorties = set()
    loss = collections.defaultdict(list)  # (me water?, foe water?) -> [loss per day]
    foe_loss = collections.defaultdict(list)
    for d in days:
        today = byday[d]
        tomorrow = {key(x): x for x in byday.get(nxt.get(d), [])}
        for x in today:
            h = int(x['格'])
            t = TER[h]
            total += 1
            sorties.add(key(x))
            if t in WATER:
                water += 1
                by_force[x['势力']] += 1
                by_task[x['军势任务']] += 1
                forms[x['阵型']] += 1
                sorties_w.add(key(x))
            foes = [y for y in today if y['势力'] != x['势力'] and o.hex_distance(int(y['格']), h) == 1]
            if not foes:
                continue
            y2 = tomorrow.get(key(x))
            if not y2:
                continue
            lost = max(0, int(x['兵数']) - int(y2['兵数']))
            fw = any(TER[int(y['格'])] in WATER for y in foes)
            loss[(t in WATER, fw)].append(lost)
    out = []
    out.append('部队日 %d，其中在水面 %d（%.1f%%）；出征 %d 次，下过水的 %d 次' % (
        total, water, 100.0 * water / max(total, 1), len(sorties), len(sorties_w)))
    out.append('  水面部队日按势力：%s' % '、'.join('%s %d' % kv for kv in by_force.most_common(8)))
    out.append('  按军势任务：%s' % '、'.join('%s %d' % kv for kv in by_task.most_common(8)))
    out.append('  阵型列（陆上阵型）：%s' % '、'.join('%s %d' % kv for kv in forms.most_common(6)))
    names = {(False, False): '我陆敌陆', (False, True): '我陆敌水', (True, False): '我水敌陆', (True, True): '我水敌水'}
    for k in ((False, False), (False, True), (True, False), (True, True)):
        v = loss.get(k, [])
        if v:
            out.append('  接战日 %s：%d 天，每天掉兵均值 %d、中位 %d' % (names[k], len(v), sum(v) / len(v), sorted(v)[len(v) // 2]))
    # one-on-one contacts: x and y adjacent, neither has another foe adjacent; who lost more
    pair = collections.defaultdict(lambda: [0, 0, 0])  # (x water, y water) -> [days, x loss, y loss]
    for d in days:
        today = byday[d]
        tomorrow = {key(x): x for x in byday.get(nxt.get(d), [])}
        adj = collections.defaultdict(list)
        for i, x in enumerate(today):
            hx = int(x['格'])
            for j in range(i + 1, len(today)):
                y = today[j]
                if y['势力'] != x['势力'] and o.hex_distance(int(y['格']), hx) == 1:
                    adj[i].append(j)
                    adj[j].append(i)
        for i, js in adj.items():
            if len(js) != 1 or len(adj[js[0]]) != 1 or i > js[0]:
                continue
            x, y = today[i], today[js[0]]
            x2, y2 = tomorrow.get(key(x)), tomorrow.get(key(y))
            if not x2 or not y2:
                continue
            xw, yw = TER[int(x['格'])] in WATER, TER[int(y['格'])] in WATER
            if yw and not xw:
                x, y, x2, y2, xw, yw = y, x, y2, x2, yw, xw
            c = pair[(xw, yw)]
            c[0] += 1
            c[1] += max(0, int(x['兵数']) - int(x2['兵数']))
            c[2] += max(0, int(y['兵数']) - int(y2['兵数']))
    pn = {(False, False): '陆对陆', (True, False): '水上一方对陆上一方', (True, True): '水对水'}
    for k in ((False, False), (True, False), (True, True)):
        if k in pair:
            c = pair[k]
            out.append('  一对一贴身 %s：%d 天，前者每天掉兵 %d、后者 %d' % (pn[k], c[0], c[1] / c[0], c[2] / c[0]))
    # crossings: enemy armies on water within 8 hexes of a city held by another force
    cities = {f['名称']: int(f['格']) for f in o.read_csv(p, 'footholds') if f['类型'] == '城市'}
    owner = {}
    for c in o.read_csv(p, 'cities'):
        owner.setdefault(dn(c['日期']), {})[c['城市']] = c['势力']
    odays = sorted(owner)

    def who(name, d):
        best = None
        for od in odays:
            if od <= d:
                best = od
        return owner[best or odays[0]].get(name)
    fid = {f['君主']: int(f['势力']) for f in o.read_csv(p, 'forces')}
    cross = 0
    hit = 0
    near_ours = 0
    side = collections.defaultdict(lambda: [0, 0, 0, 0, 0])   # city owner parity -> [days, near, hit, our loss, their loss]
    for d in days:
        today = byday[d]
        dd = dn(d)
        for x in today:
            h = int(x['格'])
            if TER[h] not in WATER or x['军势任务'] == '12':
                continue
            near = [n for n, ch in cities.items() if o.hex_distance(ch, h) <= 8 and who(n, dd) not in (None, x['势力'])]
            if not near:
                continue
            cross += 1
            hold = who(near[0], dd)
            sd = side[fid.get(hold, 0) % 2]
            sd[0] += 1
            if any(y['势力'] == hold and o.hex_distance(int(y['格']), h) <= 4 for y in today):
                near_ours += 1
                sd[1] += 1
            touching = [y for y in today if y['势力'] == hold and o.hex_distance(int(y['格']), h) == 1]
            if touching:
                hit += 1
                sd[2] += 1
                tom = {key(z): z for z in byday.get(nxt.get(d), [])}
                x2 = tom.get(key(x))
                if x2:
                    sd[4] += max(0, int(x['兵数']) - int(x2['兵数']))
                for y in touching:
                    y2 = tom.get(key(y))
                    if y2:
                        sd[3] += max(0, int(y['兵数']) - int(y2['兵数']))
    out.append('  敌方（非运输）部队在水上、离别家城 8 格内：%d 部队日；城主的部队 4 格内有 %d，贴上 %d' % (cross, near_ours, hit))
    for k in (1, 0):
        v = side[k]
        out.append('    城主势力编号%s：%d 部队日，4 格内有 %d，贴上 %d；贴上的日子城主部队掉兵 %d、水上敌军掉兵 %d' % (
            '奇数' if k else '偶数', v[0], v[1], v[2], v[3], v[4]))
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
