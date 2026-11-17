"""Did each AI module do what it is meant to do? Run after every test game, before asking the user anything.

  python -P ai/research/check_modules.py ai/runs/<test> [ai/runs/<baseline>] [--months 12]

Every check has a target. 达标 / 未达标 is decided against the target; 参考 lines are for comparison
with the baseline only. The trees run once per xun (10 days), so most checks look at the record on the
day after each xun start (days 2, 12, 22) and compare it with the next such day.

Checks:
  总体      交战、溃灭、孤军接战（observe.run_metrics）
  集结齐进  落后的部队追本队、本队原地等；下一旬整体散开范围缩小、本队不往回走
  会战协同  敌军 3 格内时正面部队打同一支敌军；雁行在前排 2 格内；锋矢打远程或攻城兵器
  野战撤退  迎击转撤退的次数；一起撤时目标一致；在自家城边撤退的次数（第十八节要改的）
  迎击多派兵 迎击的军势数和每个军势的部队数；迎击时孤军接战
  守城      迎击后回城时自己的城仍被围（第十八节）
  攻城      第一支到城下时其余部队到了多少；城拿没拿下；攻城部队溃灭
  异常扫描  出城不久回原城、早撤、来回跑、干等、贴敌开往，和对照局比（research/anomalies.py）
"""
import collections
import os
import re
import sys
import types

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
try:
    import observe as o  # noqa: E402
except Exception:            # tools/patch.py being edited elsewhere: the CSV helpers do not need it
    sys.modules['patch'] = types.SimpleNamespace(compile_pattern=lambda p: re.compile(b'x'))
    import observe as o  # noqa: E402

SIEGE = {'2', '8', '9', '10', '22'}
AREA = {'3', '13', '14', '15', '16', '21'}
FIELD = {'1'} | AREA | SIEGE              # 12 is transport
BACK = {'雁行'}
ENGINES = {'井阑', '冲车', '投石'}
NOT_FRONT = BACK | ENGINES | {'锥行'}
CITYR = 3


class Run:
    def __init__(self, path, months):
        self.path = path
        rows = o.read_csv(path, 'armies')
        days = sorted({r['日期'] for r in rows}, key=self.dn)
        lo = self.dn(days[0])
        self.rows = [r for r in rows if not months or self.dn(r['日期']) < lo + months * 31]
        self.days = sorted({r['日期'] for r in self.rows}, key=self.dn)
        self.byday = collections.defaultdict(list)
        for r in self.rows:
            self.byday[r['日期']].append(r)
        self.troops = collections.defaultdict(list)      # (day, force, troops) -> rows
        for r in self.rows:
            self.troops[(r['日期'], r['势力'], r['军势'])].append(r)
        self.xun = [d for d in self.days if int(d.split('-')[2]) in (2, 12, 22)]
        self.next_xun = {self.xun[i]: self.xun[i + 1] for i in range(len(self.xun) - 1)}
        self.fh = {}
        for f in o.read_csv(path, 'footholds'):
            self.fh.setdefault(f['编号'], int(f['格']))      # cities first in the file, then gates
        self.has_lead = bool(self.rows) and '本队' in self.rows[0]

    @staticmethod
    def dn(d):
        return o.day_number(o.parse_date(d))

    def lead(self, rs):
        ls = [r for r in rs if r.get('本队') == '是']
        return ls[0] if len(ls) == 1 else None

    def enemies_near(self, day, force, hexno, radius):
        return [x for x in self.byday[day] if x['势力'] != force and o.hex_distance(hexno, int(x['格'])) <= radius]


def pct(a, b):
    return '%d/%d（%d%%）' % (a, b, 100 * a // b) if b else '0/0'


def verdict(a, b, target):
    if not b:
        return '无样本'
    return '达标' if a / b >= target else '未达标'


def spread(rs):
    hs = [int(x['格']) for x in rs]
    return max(o.hex_distance(a, b) for a in hs for b in hs)


def check_rally(r):
    """Xun starts where the module's own condition holds (8th version: meet at the lead, nobody pulled back).
    The home city is not in the record, so "behind" is judged by the troops' first position (where it set out)."""
    if not r.has_lead:
        return ['（这份记录没有「本队」列，跳过）']
    origin = {}
    for d in r.days:
        for (day, force, t), rs in ((k, v) for k, v in r.troops.items() if k[0] == d):
            lead = r.lead(rs)
            if lead and (force, t) not in origin:
                origin[(force, t)] = int(lead['格'])
    ok = ok_n = hold = conv = back = n = 0
    for d in r.xun:
        nd = r.next_xun.get(d)
        for (day, force, t), rs in ((k, v) for k, v in r.troops.items() if k[0] == d):
            if len(rs) < 2 or rs[0]['军势任务'] not in FIELD or rs[0]['军势任务'] in SIEGE:
                continue      # 5th version on: sieges have their own batch rule (see check_siege)
            lead = r.lead(rs)
            o0 = origin.get((force, t))
            if not lead or o0 is None:
                continue
            lh = int(lead['格'])
            lhome = o.hex_distance(lh, o0)
            behind = [x for x in rs if 4 < o.hex_distance(lh, int(x['格'])) <= 12 and o.hex_distance(int(x['格']), o0) < lhome]
            if not behind or r.enemies_near(d, force, lh, 4):
                continue
            n += 1
            for x in behind:
                ok_n += 1
                m = re.match(r'格(\d+)$', x['部队目标'])
                ok += bool(m) and o.hex_distance(int(m.group(1)), lh) <= 1
            hold += lead['部队行动'] == '13' or lead['部队目标'] == '格%d' % lh
            nrs = r.troops.get((nd, force, t)) if nd else None
            if nrs and len(nrs) >= 2:
                conv += spread(nrs) < spread(rs)
                nl = r.lead(nrs)
                back += bool(nl) and o.hex_distance(int(nl['格']), o0) < lhome - 1
    return [
        '迎击、区域攻击应集结的旬（有部队落后本队 5–12 格、本队 4 格内无敌）：%d 次' % n,
        '落后的部队目标设到本队 1 格内：%s  %s（目标 80%%）' % (pct(ok, ok_n), verdict(ok, ok_n, 0.8)),
        '本队原地等：%s  %s（目标 80%%）' % (pct(hold, n), verdict(hold, n, 0.8)),
        '下一旬整体散开范围缩小：%s  %s（目标 60%%）' % (pct(conv, n), verdict(conv, n, 0.6)),
        '下一旬本队往回走（离出发处近了 2 格以上）：%s  参考（第八版起应接近 0）' % pct(back, n),
    ]


def check_engage(r):
    same = front_n = 0
    for d in r.xun:
        for (day, force, t), rs in ((k, v) for k, v in r.troops.items() if k[0] == d):
            if len(rs) < 2 or rs[0]['军势任务'] not in FIELD:
                continue
            lead = r.lead(rs) if r.has_lead else None
            if not lead or not r.enemies_near(d, force, int(lead['格']), 3):
                continue
            fronts = [x for x in rs if x['阵型'] not in NOT_FRONT and x['阵型'] != '锋矢']
            if rs[0]['军势任务'] in SIEGE:
                g = r.fh.get(rs[0]['军势目标'])
                fronts = [x for x in fronts if g is None or o.hex_distance(int(x['格']), g) > CITYR]
            if len(fronts) < 2:
                continue
            front_n += 1
            tg = collections.Counter(x['部队目标'] for x in fronts if x['部队行动'] == '7')
            same += bool(tg) and tg.most_common(1)[0][1] == len(fronts)
    back = back_n = 0
    for (day, force, t), rs in r.troops.items():
        fr = [int(x['格']) for x in rs if x['阵型'] not in NOT_FRONT]
        for x in rs:
            if x['阵型'] in BACK and fr:
                back_n += 1
                back += min(o.hex_distance(int(x['格']), h) for h in fr) <= 2
    hit = hit_n = 0
    for x in r.rows:
        m = re.match(r'部队(\d+)$', x['部队目标'])
        if x['阵型'] == '锋矢' and m:
            tgt = [y for y in r.byday[x['日期']] if y['部队'] == m.group(1)]
            if tgt and tgt[0]['势力'] != x['势力']:
                hit_n += 1
                hit += tgt[0]['阵型'] in BACK | ENGINES
    nxt = {r.days[i]: r.days[i + 1] for i in range(len(r.days) - 1)}
    by = {(x['日期'], x['部队']): x for x in r.rows}
    ys = collections.defaultdict(lambda: [0, 0])
    ym = collections.defaultdict(lambda: [0, 0])   # only 雁行 in a troops of 2+ armies (what stance handles)
    for d, rs in r.byday.items():
        size = collections.Counter((y['势力'], y['军势']) for y in rs)
        for x in rs:
            if x['阵型'] != '雁行':
                continue
            ds = [o.hex_distance(int(x['格']), int(y['格'])) for y in rs if y['势力'] != x['势力']]
            near = min(ds) if ds else 99
            if near > 2:
                continue
            n = by.get((nxt.get(d), x['部队']))
            lost = max(0, int(x['兵数']) - int(n['兵数'])) if n and n['主将'] == x['主将'] else 0
            ys[near][0] += 1
            ys[near][1] += lost
            if size[(x['势力'], x['军势'])] >= 2:
                ym[near][0] += 1
                ym[near][1] += lost
    y1, y2 = ys[1][0], ys[2][0]
    m1, m2 = ym[1][0], ym[2][0]
    return [
        '雁行离敌 2 格内的部队日里贴脸（1 格）：%s  %s（目标 ≤40%%）；每天掉兵 贴脸 %.0f / 隔一格 %.0f' % (
            pct(y1, y1 + y2), '达标' if y1 + y2 and y1 / (y1 + y2) <= 0.4 else '未达标',
            ys[1][1] / y1 if y1 else 0, ys[2][1] / y2 if y2 else 0),
        '  其中 2 支以上军势里的雁行：贴脸 %s；每天掉兵 贴脸 %.0f / 隔一格 %.0f  参考' % (
            pct(m1, m1 + m2), ym[1][1] / m1 if m1 else 0, ym[2][1] / m2 if m2 else 0),
        '敌军已到本队 3 格内、2 支以上正面部队：都打同一支敌军 %s  参考（集火要配合夹击才有利，见交换比）' % pct(same, front_n),
        '雁行在己方前排 2 格内（部队日）：%s  参考' % pct(back, back_n),
        '锋矢打敌方雁行或攻城兵器：%s  参考' % pct(hit, hit_n),
    ]


def check_retreat(r):
    seq = collections.defaultdict(list)
    for x in r.rows:
        seq[(x['势力'], x['军势'])].append(x)
    turned = near_home = 0
    for k, rs in seq.items():
        for a, b in zip(rs, rs[1:]):
            if a['日期'] != b['日期'] and a['军势任务'] == '1' and b['军势任务'] == '0':
                turned += 1
                home = r.fh.get(b['军势目标'])
                lead = r.lead([y for y in r.troops[(b['日期'], b['势力'], b['军势'])]]) if r.has_lead else None
                if home is not None and lead and o.hex_distance(int(lead['格']), home) <= 4:
                    near_home += 1
    together = tog_n = 0
    for (d, f, t), rs in r.troops.items():
        if len(rs) >= 2 and rs[0]['军势任务'] == '0':
            tog_n += 1
            together += len({x['部队目标'] for x in rs}) == 1
    return [
        '迎击转撤退：%d 次，其中本队在自家城 4 格内：%d 次  参考' % (turned, near_home),
        '两支以上一起撤时目标一致（军势日）：%s  %s（目标 90%%）' % (pct(together, tog_n), verdict(together, tog_n, 0.9)),
    ]


def check_intercept(r):
    first = {}
    for x in r.rows:
        first.setdefault((x['势力'], x['部队'], x['主将']), x)
    groups = collections.defaultdict(list)
    for x in first.values():
        if x['军势任务'] == '1':
            groups[(x['势力'], x['军势'], x['日期'][:7])].append(x)
    n = len(groups)
    armies = sum(len(v) for v in groups.values())
    battles = [b for b in o.read_csv(r.path, 'battles') if b['日期'] in r.byday]
    purpose = {(x['日期'], x['部队']): x['军势任务'] for x in r.rows}
    ib = [b for b in battles if purpose.get((b['日期'], b['部队'])) == '1']
    alone = sum(int(b['%d格内友军' % o.NEAR]) == 0 for b in ib)
    multi = [v for v in groups.values() if len(v) >= 2]
    top = [max(int(x['兵数']) for x in v) / max(1, sum(int(x['兵数']) for x in v)) for v in multi]
    cheer = sum(1 for v in groups.values() for x in v if x['阵型'] == '雁行' and int(x['兵数']) <= 2000)
    flank = sum(1 for v in groups.values() for x in v if x['阵型'] == '锋矢')
    return [
        '迎击军势 %d 个，平均 %.2f 支部队，单支 %d 个，多支 %s  参考' % (
            n, armies / n if n else 0, sum(len(v) == 1 for v in groups.values()), pct(len(multi), n)),
        '多支迎击里兵最多的一支占总兵力：平均 %d%%；2000 兵以内的雁行（啦啦队）%d 支，锋矢 %d 支  参考' % (
            round(100 * sum(top) / len(top)) if top else 0, cheer, flank),
        '迎击中的接战里孤军：%s  参考' % pct(alone, len(ib)),
    ]


def check_defence(r):
    seq = collections.defaultdict(list)
    for x in r.rows:
        seq[(x['势力'], x['军势'])].append(x)
    back = besieged = engines = 0
    for k, rs in seq.items():
        for a, b in zip(rs, rs[1:]):
            if a['日期'] != b['日期'] and a['军势任务'] in ('1', '7') and b['军势任务'] in ('17', '18'):
                back += 1
                bs = [x for x in r.byday[b['日期']] if x['势力'] != b['势力'] and x['军势任务'] in SIEGE
                      and x['军势目标'] == b['军势目标']]
                besieged += bool(bs)
                engines += any(x['阵型'] in ENGINES for x in bs)
    return ['迎击后回城 %d 次，其中自己的城仍被围 %d 次（围城方带攻城兵器 %d 次）  参考' % (back, besieged, engines)]


def check_siege(r):
    """Siege episodes (force, troops, goal): how many armies were near when the first one reached the city's
    range, whether the city fell to that force, and siege sorties wiped out."""
    names = {}
    for f in o.read_csv(r.path, 'footholds'):
        names.setdefault(f['编号'], f['名称'])
    ep = collections.defaultdict(list)
    for x in r.rows:
        if x['军势任务'] in SIEGE:
            ep[(x['势力'], x['军势'], x['军势目标'])].append(x)
    took = n = 0
    together, multi = [], 0
    events = [e for e in o.read_csv(r.path, 'events') if e['事件'] == '城市易主' and e['日期'] in r.byday]
    for (force, t, goal), rs in ep.items():
        g = r.fh.get(goal)
        if g is None:
            continue
        n += 1
        days = sorted({x['日期'] for x in rs}, key=r.dn)
        first = next((d for d in days if any(o.hex_distance(int(x['格']), g) <= CITYR
                                             for x in rs if x['日期'] == d)), None)
        if first:
            arm = [x for x in rs if x['日期'] == first]
            if len(arm) >= 2:
                multi += 1
                together.append(sum(o.hex_distance(int(x['格']), g) <= CITYR + 3 for x in arm) / len(arm))
        lo, hi = r.dn(days[0]), r.dn(days[-1]) + 10
        took += any(e['对象'] == names.get(goal) and e['到'] == force and lo <= r.dn(e['日期']) <= hi for e in events)
    so = [x for x in o.read_csv(r.path, 'sorties') if x['出发'] in r.byday and x['军势任务'] in SIEGE]
    wiped = sum(x['结局'].startswith('溃灭') for x in so)
    avg = sum(together) / len(together) if together else 0
    # per episode: daily smallest distance of its armies to the goal
    away = reach = 0
    for (force, t, goal), rs in ep.items():
        g = r.fh.get(goal)
        if g is None:
            continue
        per = collections.defaultdict(list)
        for x in rs:
            per[x['日期']].append(o.hex_distance(int(x['格']), g))
        ds = [min(per[d]) for d in sorted(per, key=r.dn)]
        away += sum(1 for a_, b_ in zip(ds, ds[1:]) if b_ > a_)
        reach += min(ds) <= CITYR
    form = {}
    for x in r.rows:
        form.setdefault((x['势力'], x['部队'], x['主将']), x['阵型'])
    eng = [x for x in so if form.get((x['势力'], x['部队'], x['主将'])) in ENGINES]
    eng_w = sum(x['结局'].startswith('溃灭') for x in eng)
    return [
        '攻城军势离城变远的天数：%d（原版 19）；打到城下 %d 格内：%s  参考' % (away, CITYR, pct(reach, n)),
        '攻城兵器出征 %d 次，溃灭 %s  参考' % (len(eng), pct(eng_w, len(eng))),
        '攻城军势 %d 个，拿下目标城 %s  参考' % (n, pct(took, n)),
        '两支以上的攻城军势，第一支进入城市攻击范围时，其余部队已到城下 %d 格内：平均 %d%%（%d 个军势）  参考' % (CITYR + 3, round(avg * 100), multi),
        '攻城出征 %d 次，溃灭 %s  参考' % (len(so), pct(wiped, len(so))),
    ]


def check_exchange(r):
    """Exchange in close combat for troops with 2+ armies vs single armies: over each day, soldiers lost by
    our armies that have an enemy within 1 hex, against soldiers lost by those enemies. Reinforcements
    (soldiers going up) are ignored. Also: pincers - an enemy army with two of our armies adjacent on
    roughly opposite sides (their distance 2 through it) - and the exchange around it."""
    nxt = {r.days[i]: r.days[i + 1] for i in range(len(r.days) - 1)}
    by = {(x['日期'], x['部队']): x for x in r.rows}
    multi = {(x['日期'], x['势力'], x['军势']) for x in r.rows}
    size = collections.Counter((x['日期'], x['势力'], x['军势']) for x in r.rows)
    acc = collections.defaultdict(lambda: [0, 0])     # kind -> [our loss, enemy loss]
    for d, rs in r.byday.items():
        nd = nxt.get(d)
        if not nd:
            continue
        for x in rs:
            hx = int(x['格'])
            foes = [y for y in rs if y['势力'] != x['势力'] and o.hex_distance(hx, int(y['格'])) <= 1]
            if not foes:
                continue
            kind = '多支军势' if size[(d, x['势力'], x['军势'])] >= 2 else '单支军势'
            n = by.get((nd, x['部队']))
            if n and n['主将'] == x['主将']:
                acc[kind][0] += max(0, int(x['兵数']) - int(n['兵数']))
            for y in foes:
                m = by.get((nd, y['部队']))
                if m and m['主将'] == y['主将']:
                    # an enemy next to several of ours is shared: count it once per attacker
                    k = sum(1 for z in rs if z['势力'] == x['势力'] and o.hex_distance(int(z['格']), int(y['格'])) <= 1)
                    acc[kind][1] += max(0, int(y['兵数']) - int(m['兵数'])) / max(k, 1)
    # pincers: enemy with two of the same force adjacent at distance 2 from each other
    pin = [0, 0, 0]       # count, our loss, enemy loss
    side = [0, 0, 0]      # same: two adjacent at distance 1 from each other (same side)
    for d, rs in r.byday.items():
        nd = nxt.get(d)
        if not nd:
            continue
        for y in rs:
            hy = int(y['格'])
            for f in {z['势力'] for z in rs if z['势力'] != y['势力']}:
                adj = [z for z in rs if z['势力'] == f and o.hex_distance(hy, int(z['格'])) == 1]
                if len(adj) < 2:
                    continue
                far = any(o.hex_distance(int(a['格']), int(b['格'])) == 2 for a in adj for b in adj)
                t = pin if far else side
                m = by.get((nd, y['部队']))
                if not m or m['主将'] != y['主将']:
                    continue
                t[0] += 1
                t[2] += max(0, int(y['兵数']) - int(m['兵数']))
                for a in adj:
                    n = by.get((nd, a['部队']))
                    if n and n['主将'] == a['主将']:
                        t[1] += max(0, int(a['兵数']) - int(n['兵数']))
    ratio = lambda our, en: ('%.2f' % (en / our)) if our else '-'   # noqa: E731
    out = []
    for kind in ('多支军势', '单支军势'):
        our, en = acc[kind]
        out.append('%s贴身交战：我方掉兵 %d，敌方掉兵 %d，交换比 %s（敌损 / 我损）  参考' % (kind, our, en, ratio(our, en)))
    out.append('夹击（两支从相对两侧贴住同一敌军）%d 次：交换比 %s；同侧两支贴住 %d 次：交换比 %s  参考' % (
        pin[0], ratio(pin[1], pin[2]), side[0], ratio(side[1], side[2])))
    return out


def check_ab(r):
    """Forces grouped by id parity (A/B runs: modules only for odd ids). Cities, sieges, sorties, exchange."""
    forces = o.read_csv(r.path, 'forces')
    days = sorted({f['日期'] for f in forces if f['日期'] in r.byday}, key=r.dn)
    if not days:
        return ['（没有势力记录）']
    name2id = {}
    for f in forces:
        name2id[(f['日期'], f['君主'])] = int(f['势力'])
    lastname = {f['君主']: int(f['势力']) for f in forces}
    def grp(day, name):
        fid = name2id.get((day, name), lastname.get(name))
        return None if fid is None else ('奇数' if fid % 2 else '偶数')
    city = collections.defaultdict(lambda: [0, 0])
    for f in forces:
        g = '奇数' if int(f['势力']) % 2 else '偶数'
        if f['日期'] == days[0]:
            city[g][0] += int(f['城市'] or 0)
        if f['日期'] == days[-1]:
            city[g][1] += int(f['城市'] or 0)
    so = collections.defaultdict(lambda: [0, 0, 0])
    # who an army last touched before its sortie ended: (day, slot, general) -> group of the adjacent enemies
    last_foe = {}
    for d, rs in r.byday.items():
        for x in rs:
            foes = [grp(d, y['势力']) for y in rs
                    if y['势力'] != x['势力'] and o.hex_distance(int(x['格']), int(y['格'])) <= 1]
            foes = [f for f in foes if f]
            if foes:
                key = (x['部队'], x['主将'])
                if key not in last_foe or r.dn(d) >= r.dn(last_foe[key][0]):
                    last_foe[key] = (d, collections.Counter(foes).most_common(1)[0][0])
    wipe2 = collections.defaultdict(lambda: [0, 0])     # (our group, the last enemy group): fought, wiped
    for x in o.read_csv(r.path, 'sorties'):
        if x['出发'] not in r.byday:
            continue
        g = grp(x['出发'], x['势力'])
        if g:
            so[g][0] += 1
            so[g][1] += x['结局'] != '未交战'
            so[g][2] += x['结局'].startswith('溃灭')
            lf = last_foe.get((x['部队'], x['主将']))
            if x['结局'] != '未交战' and lf and r.dn(x['出发']) <= r.dn(lf[0]):
                wipe2[(g, lf[1])][0] += 1
                wipe2[(g, lf[1])][1] += x['结局'].startswith('溃灭')
    nxt = {r.days[i]: r.days[i + 1] for i in range(len(r.days) - 1)}
    by = {(x['日期'], x['部队']): x for x in r.rows}
    ex = collections.defaultdict(lambda: [0, 0])
    ex2 = collections.defaultdict(lambda: [0, 0])    # (our group, the group of the enemies touching us)
    for d, rs in r.byday.items():
        nd = nxt.get(d)
        if not nd:
            continue
        for x in rs:
            g = grp(d, x['势力'])
            hx = int(x['格'])
            foes = [y for y in rs if y['势力'] != x['势力'] and o.hex_distance(hx, int(y['格'])) <= 1]
            if not g or not foes:
                continue
            hg = collections.Counter(grp(d, y['势力']) for y in foes).most_common(1)[0][0]
            n = by.get((nd, x['部队']))
            if n and n['主将'] == x['主将']:
                ex[g][0] += max(0, int(x['兵数']) - int(n['兵数']))
                ex2[(g, hg)][0] += max(0, int(x['兵数']) - int(n['兵数']))
            for y in foes:
                m = by.get((nd, y['部队']))
                if m and m['主将'] == y['主将']:
                    k = sum(1 for z in rs if z['势力'] == x['势力'] and o.hex_distance(int(z['格']), int(y['格'])) <= 1)
                    ex[g][1] += max(0, int(y['兵数']) - int(m['兵数'])) / max(k, 1)
                    ex2[(g, hg)][1] += max(0, int(y['兵数']) - int(m['兵数'])) / max(k, 1)
    # sieges by the attacker's group: episodes and captures (same episode rule as check_siege)
    names = {}
    for f in o.read_csv(r.path, 'footholds'):
        names.setdefault(f['编号'], f['名称'])
    events = [e for e in o.read_csv(r.path, 'events') if e['事件'] == '城市易主' and e['日期'] in r.byday]
    ep = collections.defaultdict(list)
    for x in r.rows:
        if x['军势任务'] in SIEGE:
            ep[(x['势力'], x['军势'], x['军势目标'])].append(x)
    # the defender: the city's owner at the last city record on or before the episode's first day
    hist = collections.defaultdict(list)
    for c in o.read_csv(r.path, 'cities'):
        if True:
            try:
                hist[c['城市']].append((r.dn(c['日期']), c['势力']))
            except (KeyError, ValueError):
                pass
    for v in hist.values():
        v.sort()

    def owner_at(day, city):
        best = hist[city][0][1] if hist.get(city) else None
        for dn, lord in hist.get(city, []):
            if dn <= r.dn(day):
                best = lord
        return best
    sg = collections.defaultdict(lambda: [0, 0])
    vs = collections.defaultdict(lambda: [0, 0])     # (attacker group, defender group)
    for (force, t, goal), rs in ep.items():
        days_ = sorted({x['日期'] for x in rs}, key=r.dn)
        g = grp(days_[0], force)
        if not g or r.fh.get(goal) is None:
            continue
        lo, hi = r.dn(days_[0]), r.dn(days_[-1]) + 10
        took = any(e['对象'] == names.get(goal) and e['到'] == force and lo <= r.dn(e['日期']) <= hi for e in events)
        sg[g][0] += 1
        sg[g][1] += took
        dl = owner_at(days_[0], names.get(goal))
        dg = grp(days_[0], dl) if dl else None
        if dg:
            vs[(g, dg)][0] += 1
            vs[(g, dg)][1] += took
    out = []
    for g in ('奇数', '偶数'):
        a, b = city[g]
        n, e, w = so[g]
        our, en = ex[g]
        out.append('%s编号势力：城市 %d -> %d；出征 %d，交战 %d，溃灭 %d（交战后 %s）；贴身交换比 %s；攻城 %s  参考' % (
            g, a, b, n, e, w, ('%d%%' % (100 * w // e)) if e else '-', ('%.2f' % (en / our)) if our else '-',
            pct(sg[g][1], sg[g][0])))
    out.append('交战后溃灭按对手分组（对手取覆灭或收兵前最后贴身的敌军）：%s  参考' % '；'.join(
        '%s对%s %s' % (a, d, pct(wipe2[(a, d)][1], wipe2[(a, d)][0])) for a in ('奇数', '偶数') for d in ('奇数', '偶数')))
    out.append('贴身交换比按对手分组：%s  参考（看「奇数对偶数」：开模块的一方对原版）' % '；'.join(
        '%s对%s %s' % (a, d, ('%.2f' % (ex2[(a, d)][1] / ex2[(a, d)][0])) if ex2[(a, d)][0] else '-')
        for a in ('奇数', '偶数') for d in ('奇数', '偶数')))
    out.append('攻城按攻守双方分组：%s  参考（看「奇数攻偶数」：开模块的一方打原版守城）' % '；'.join(
        '%s攻%s %s' % (a, d, pct(vs[(a, d)][1], vs[(a, d)][0])) for a in ('奇数', '偶数') for d in ('奇数', '偶数')
        if vs[(a, d)][0]))
    return out


def main():
    args = sys.argv[1:]
    months = 12
    if '--months' in args:
        i = args.index('--months')
        months = int(args[i + 1])
        del args[i:i + 2]
    runs = [Run(p, months) for p in args]
    names = [os.path.basename(p) for p in args]
    print('统计前 %d 个月。' % months, '  '.join('%s：%s 到 %s' % (nm, r.days[0], r.days[-1]) for nm, r in zip(names, runs)))
    ms = [o.run_metrics(p, months) for p in args]
    print('\n== 总体')
    for k in ms[0]:
        print('  %-10s %s' % (k, '   '.join('%22s' % str(m.get(k)) for m in ms)))
    for title, fn in (('集结齐进', check_rally), ('会战协同', check_engage), ('野战撤退', check_retreat),
                      ('迎击多派兵', check_intercept), ('守城（第十八节）', check_defence), ('攻城', check_siege), ('交换比', check_exchange), ('A/B（按势力编号奇偶分组）', check_ab)):
        print('\n== ' + title)
        for nm, r in zip(names, runs):
            print('  [%s]' % nm)
            for line in fn(r):
                print('    ' + line)
    if len(args) >= 2:
        # behaviour anomalies against the baseline (research/anomalies.py): what doubled needs a cause
        import importlib.util
        spec = importlib.util.spec_from_file_location('anomalies', os.path.join(HERE, 'research', 'anomalies.py'))
        an = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(an)
        print('\n== 异常扫描（%s 对 %s；比对照多一倍以上、至少 10 次的标「要查」）' % (names[0], names[1]))
        for line in an.report(args[0], args[1], months):
            print('    ' + line)


if __name__ == '__main__':
    main()
