"""守城的整段过程：每次一座城被围，守方的野战部队做了什么，结局怎样。按守方编号奇偶分组（A/B 局里
奇数一方开着被测的模块）。

  python -P ai/research/defend_events.py ai/runs/<run> [more runs] [--months 12] [--list]

一次围城：同一座城、同一守方，攻城军势（军势任务 2 8 9 10 22，军势目标是这座城）的日子连起来，中间断开
不超过 10 天算同一次。窗口从第一支攻城部队进入离城 8 格算起，到最后一个攻城日后 5 天。每天看：
  攻方：攻城势力在离城 6 格内的部队（含攻城兵器）；
  守方：守城势力不在攻城的野战部队在离城 6 格内的部队。
统计：
  城丢没丢（窗口后 10 天内易主给攻城的一方）；
  守方出城：有守方部队在城外 6 格内的天数；打攻城兵器的部队日；
  攻城兵器：到过城下 6 格内的攻城兵器，其中出征以溃灭收场的；
  回城：守方部队这一天改成回城（部队行动 2 或 11）时，城 5 格内还有攻方部队、而城外 6 格内守方部队兵力
        不低于攻方的 60%（守城迎击模块第二条会接手的情况），以及之后几天这支部队怎样；
  城外贴身交换：守方部队和攻方部队贴身时双方第二天的掉兵（同 check_modules 的算法）。
另一节「打攻城兵器的机会」不限守城：近战阵型的部队没和敌军贴身、3 格内有交战中的敌方攻城兵器（守城迎击
第一条会接手的情况），按部队一方分组，看之后 3 天里有没有以它为目标、5 天里它掉了多少兵、15 天内是否溃灭。
交战中＝30 天内一方有部队以另一方的部队或城为目标（和 DLL 的 AtWar 同一个办法）。
--list 列出每一次围城。
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402

SIEGE = {'2', '8', '9', '10', '22'}
ENGINES = {'井阑', '冲车', '投石'}
SHIPS = {'走舸', '蒙冲', '楼船', '输船'}
NEAR, HOME_R, START_R = 6, 5, 8


def dn(d):
    return o.day_number(o.parse_date(d))


def analyse(path, months, listing):
    rows = o.read_csv(path, 'armies')
    days = sorted({r['日期'] for r in rows}, key=dn)
    lo = dn(days[0])
    rows = [r for r in rows if not months or dn(r['日期']) < lo + months * 31]
    byday = collections.defaultdict(list)
    for r in rows:
        byday[r['日期']].append(r)
    days = sorted(byday, key=dn)
    nxt = {days[i]: days[i + 1] for i in range(len(days) - 1)}
    by = {(x['日期'], x['部队']): x for x in rows}
    fh, names = {}, {}
    for f in o.read_csv(path, 'footholds'):
        if f['类型'] == '城市':
            fh.setdefault(f['编号'], int(f['格']))
            names.setdefault(f['编号'], f['名称'])
    forces = o.read_csv(path, 'forces')
    lastid = {}
    for f in forces:
        lastid[f['君主']] = int(f['势力'])
    grp = lambda name: None if name not in lastid else ('奇数' if lastid[name] % 2 else '偶数')  # noqa: E731
    hist = collections.defaultdict(list)
    for c in o.read_csv(path, 'cities'):
        hist[c['城市']].append((dn(c['日期']), c['势力']))
    for v in hist.values():
        v.sort()

    def owner_at(day, city):
        best = hist[city][0][1] if hist.get(city) else None
        for d_, lord in hist.get(city, []):
            if d_ <= dn(day):
                best = lord
        return best
    falls = [e for e in o.read_csv(path, 'events') if e['事件'] == '城市易主']
    for e in falls:
        hist[e['对象']].append((dn(e['日期']), e['到']))
        hist[e['对象']].sort()
    sorties = o.read_csv(path, 'sorties')
    end_of = {}
    for s in sorties:
        end_of[(s['部队'], s['主将'], s['出发'])] = s
    # each army's sortie: the sortie whose start is the latest one on or before the day
    sortie_by_army = collections.defaultdict(list)
    for s in sorties:
        sortie_by_army[(s['部队'], s['主将'])].append(s)

    def sortie_of(x):
        best = None
        for s in sortie_by_army.get((x['部队'], x['主将']), []):
            if dn(s['出发']) <= dn(x['日期']) and (best is None or dn(s['出发']) > dn(best['出发'])):
                best = s
        return best

    # siege days per (city, attacker force)
    sd = collections.defaultdict(set)
    for x in rows:
        if x['军势任务'] in SIEGE and x['军势目标'] in fh:
            sd[x['军势目标']].add((dn(x['日期']), x['势力']))
    sieges = []
    for city, s in sd.items():
        ds = sorted({d for d, _ in s})
        start = prev = ds[0]
        for d in ds[1:] + [None]:
            if d is None or d - prev > 10:
                sieges.append((city, start, prev, {f for dd, f in s if start <= dd <= prev}))
                if d is not None:
                    start = d
            if d is not None:
                prev = d
    out = []
    for city, a, b, att in sieges:
        g = fh[city]
        win = [d for d in days if a - 10 <= dn(d) <= b + 5]
        # the window starts on the first day an attacker is within START_R
        first = next((d for d in win if any(x['势力'] in att and o.hex_distance(int(x['格']), g) <= START_R
                                             for x in byday[d])), None)
        if not first:
            continue
        defender = owner_at(first, names[city])
        if not defender or defender in att:
            continue
        win = [d for d in win if dn(d) >= dn(first)]
        fell = any(e['对象'] == names[city] and e['到'] in att and dn(first) <= dn(e['日期']) <= b + 10 for e in falls)
        lost_any = any(e['对象'] == names[city] and e['从'] == defender and dn(first) <= dn(e['日期']) <= b + 10
                       for e in falls)
        out_days = eng_days = 0
        engines, engines_wiped, def_armies, def_wiped = set(), set(), set(), set()
        att_peak = 0
        home = []
        ex = [0, 0]                 # defender losses, attacker losses while in contact near the city
        for d in win:
            rs = byday[d]
            attn = [x for x in rs if x['势力'] in att and o.hex_distance(int(x['格']), g) <= NEAR]
            defn = [x for x in rs if x['势力'] == defender and x['军势任务'] not in SIEGE and x['阵型'] not in SHIPS
                    and o.hex_distance(int(x['格']), g) <= NEAR]
            att_peak = max(att_peak, sum(int(x['兵数']) for x in attn))
            out_days += bool(defn)
            eslot = {x['部队'] for x in attn if x['阵型'] in ENGINES}
            for x in attn:
                if x['阵型'] in ENGINES:
                    engines.add((x['部队'], x['主将']))
            for x in defn:
                def_armies.add((x['部队'], x['主将']))
                if x['部队目标'].startswith('部队') and x['部队目标'][2:] in eslot:
                    eng_days += 1
            # going home while the city is still under attack
            near5 = [x for x in attn if o.hex_distance(int(x['格']), g) <= HOME_R]
            foes = sum(int(x['兵数']) for x in near5)
            ours = sum(int(x['兵数']) for x in defn)
            nd = nxt.get(d)
            for x in defn:
                y = by.get((nd, x['部队'])) if nd else None
                if not y or y['主将'] != x['主将']:
                    continue
                if x['部队行动'] not in ('2', '11') and y['部队行动'] in ('2', '11') and foes and ours * 10 >= foes * 6 \
                        and int(x['兵数']) >= 1500:
                    s = sortie_of(y)
                    home.append((d, x['主将'], int(x['兵数']), ours, foes, s['结局'] if s else '?'))
            # contact exchange
            if nd:
                for x in defn:
                    foes1 = [y for y in attn if o.hex_distance(int(x['格']), int(y['格'])) <= 1]
                    if not foes1:
                        continue
                    n = by.get((nd, x['部队']))
                    if n and n['主将'] == x['主将']:
                        ex[0] += max(0, int(x['兵数']) - int(n['兵数']))
                    for y in foes1:
                        m = by.get((nd, y['部队']))
                        if m and m['主将'] == y['主将']:
                            k = sum(1 for z in defn if o.hex_distance(int(z['格']), int(y['格'])) <= 1)
                            ex[1] += max(0, int(y['兵数']) - int(m['兵数'])) / max(k, 1)
        for key in engines:
            ss = [s for s in sortie_by_army.get(key, []) if dn(s['出发']) <= b and dn(s['结束']) >= dn(first)]
            if any(s['结局'].startswith('溃灭') for s in ss):
                engines_wiped.add(key)
        for key in def_armies:
            ss = [s for s in sortie_by_army.get(key, []) if dn(s['出发']) <= b + 5 and dn(s['结束']) >= dn(first)]
            if any(s['结局'].startswith('溃灭') for s in ss):
                def_wiped.add(key)
        out.append(dict(city=names[city], first=first, last=win[-1] if win else first, defender=defender,
                        g=grp(defender), att='/'.join(sorted(att)), fell=fell, lost=lost_any, out_days=out_days,
                        days=len(win), eng=len(engines), eng_wiped=len(engines_wiped), eng_days=eng_days,
                        def_armies=len(def_armies), def_wiped=len(def_wiped), home=home, ex=ex, att_peak=att_peak))
    return out


MELEE = {'鱼鳞', '锋矢', '方圆', '鹤翼', '长蛇', '锥行', '四夷'}


def engine_chances(path, months):
    rows = o.read_csv(path, 'armies')
    days = sorted({r['日期'] for r in rows}, key=dn)
    lo = dn(days[0])
    rows = [r for r in rows if not months or dn(r['日期']) < lo + months * 31]
    byday = collections.defaultdict(list)
    for r in rows:
        byday[r['日期']].append(r)
    days = sorted(byday, key=dn)
    by = {(x['日期'], x['部队']): x for x in rows}
    lastid = {}
    for f in o.read_csv(path, 'forces'):
        lastid[f['君主']] = int(f['势力'])
    grp = lambda name: None if name not in lastid else ('奇数' if lastid[name] % 2 else '偶数')  # noqa: E731
    names = {f['编号']: f['名称'] for f in o.read_csv(path, 'footholds') if f['类型'] == '城市'}
    hist = collections.defaultdict(list)
    for c in o.read_csv(path, 'cities'):
        hist[c['城市']].append((dn(c['日期']), c['势力']))
    for e in o.read_csv(path, 'events'):
        if e['事件'] == '城市易主':
            hist[e['对象']].append((dn(e['日期']), e['到']))
    for v in hist.values():
        v.sort()

    def owner(day, city):
        best = None
        for d_, lord in hist.get(city, []):
            if d_ <= dn(day) or best is None:
                best = lord if d_ <= dn(day) or best is None else best
        return best
    war = {}
    sortie_by_army = collections.defaultdict(list)
    for s_ in o.read_csv(path, 'sorties'):
        sortie_by_army[(s_['部队'], s_['主将'])].append(s_)
    chances = {}
    for d in days:
        rs = byday[d]
        slot = {x['部队']: x for x in rs}
        for x in rs:
            t = x['部队目标']
            f = None
            if t.startswith('部队') and t[2:] in slot:
                f = slot[t[2:]]['势力']
            elif t.startswith('城市') and x['部队行动'] == '4':
                f = owner(d, names.get(t[2:]))
            if f and f != x['势力']:
                war[frozenset((x['势力'], f))] = dn(d)
        at_war = lambda a, b: dn(d) - war.get(frozenset((a, b)), -999) <= 30  # noqa: E731
        for x in rs:
            if x['阵型'] not in MELEE:
                continue
            hx = int(x['格'])
            if any(y['势力'] != x['势力'] and at_war(x['势力'], y['势力']) and o.hex_distance(hx, int(y['格'])) <= 1
                   for y in rs):
                continue
            for y in rs:
                if y['阵型'] in ENGINES and y['势力'] != x['势力'] and at_war(x['势力'], y['势力'])                         and o.hex_distance(hx, int(y['格'])) <= 3:
                    key = (x['部队'], x['主将'], y['部队'], y['主将'])
                    if key not in chances:
                        chances[key] = (d, x, y)
    out = collections.defaultdict(lambda: [0, 0, 0, 0, 0])   # group: chances, attacked it, its loss 5 days, wiped, ours wiped
    for (xs, xg, ys, yg), (d, x, y) in chances.items():
        g = grp(x['势力'])
        if not g:
            continue
        i = days.index(d)
        nxt3 = days[i:i + 4]
        hit = any((by.get((dd, xs)) or {}).get('部队目标') == '部队' + ys and by[(dd, xs)]['主将'] == xg for dd in nxt3)
        later = [by.get((dd, ys)) for dd in days[i:i + 6]]
        later = [z for z in later if z and z['主将'] == yg]
        loss = int(y['兵数']) - (int(later[-1]['兵数']) if later else 0)
        ss = [s_ for s_ in sortie_by_army.get((ys, yg), []) if dn(s_['出发']) <= dn(d) <= dn(s_['结束'])]
        wiped = any(s_['结局'].startswith('溃灭') and dn(s_['结束']) - dn(d) <= 15 for s_ in ss)
        ws = [s_ for s_ in sortie_by_army.get((xs, xg), []) if dn(s_['出发']) <= dn(d) <= dn(s_['结束'])]
        ours = any(s_['结局'].startswith('溃灭') and dn(s_['结束']) - dn(d) <= 15 for s_ in ws)
        o_ = out[g]
        o_[0] += 1
        o_[1] += hit
        o_[2] += max(0, loss)
        o_[3] += wiped
        o_[4] += ours
    return ['%s：机会 %d 次，3 天内以它为目标 %d，它 5 天掉兵平均 %d，15 天内溃灭 %d（%d%%），我方这支 15 天内溃灭 %d' % (
        g, v[0], v[1], v[2] // v[0] if v[0] else 0, v[3], 100 * v[3] // v[0] if v[0] else 0, v[4])
        for g, v in sorted(out.items())]


def summary(evs):
    lines = []
    for g in ('奇数', '偶数'):
        e = [x for x in evs if x['g'] == g]
        if not e:
            continue
        n = len(e)
        fell = sum(x['fell'] for x in e)
        lost = sum(x['lost'] for x in e)
        eng = sum(x['eng'] for x in e)
        ew = sum(x['eng_wiped'] for x in e)
        out = sum(x['out_days'] > 0 for x in e)
        da = sum(x['def_armies'] for x in e)
        dw = sum(x['def_wiped'] for x in e)
        home = [h for x in e for h in x['home']]
        exd = sum(x['ex'][0] for x in e)
        exa = sum(x['ex'][1] for x in e)
        engdays = sum(x['eng_days'] for x in e)
        withe = [x for x in e if x['eng']]
        lines.append('%s守方：围城 %d 次，丢城 %d（%d%%，含丢给别家 %d）；有守方部队出城 %d 次；出城部队 %d 支，溃灭 %d；'
                     '城外贴身交换比 %s' % (g, n, fell, 100 * fell // n, lost, out, da, dw,
                                     ('%.2f' % (exa / exd)) if exd else '-'))
        lines.append('    攻城兵器到城下 %d 支，溃灭 %d（%d%%）；带攻城兵器的围城 %d 次，丢城 %d；守方打攻城兵器 %d 部队日' % (
            eng, ew, 100 * ew // eng if eng else 0, len(withe), sum(x['fell'] for x in withe), engdays))
        lines.append('    城还被围、守方兵力够时回城 %d 次：结局 %s' % (
            len(home), dict(collections.Counter(h[5].split('（')[0] for h in home))))
    return lines


def main():
    args = sys.argv[1:]
    months = 12
    if '--months' in args:
        i = args.index('--months')
        months = int(args[i + 1])
        del args[i:i + 2]
    listing = '--list' in args
    args = [a for a in args if a != '--list']
    for p in args:
        evs = analyse(p, months, listing)
        print('== %s' % os.path.basename(p.rstrip('/\\')))
        for ln in summary(evs):
            print('  ' + ln)
        print('  打攻城兵器的机会（按部队一方分组）：')
        for ln in engine_chances(p, months):
            print('    ' + ln)
        if listing:
            for x in sorted(evs, key=lambda x: dn(x['first'])):
                print('    %s %s 守 %s(%s) 攻 %s：%d 天，%s；攻方峰值 %d；守方出城 %d 天 %d 支（溃灭 %d）；攻城兵器 %d（溃灭 %d）；回城 %d' % (
                    x['first'], x['city'], x['defender'], x['g'], x['att'], x['days'], '丢城' if x['fell'] else '守住',
                    x['att_peak'], x['out_days'], x['def_armies'], x['def_wiped'], x['eng'], x['eng_wiped'],
                    len(x['home'])))


if __name__ == '__main__':
    main()
