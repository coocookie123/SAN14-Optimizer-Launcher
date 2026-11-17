"""援军继续守（DLL 功能 relief，BEHAVIOR_PLAN 第四十八节，2026-11-15）的效果。

  python -P ai/research/relief_check.py ai/runs/<run>[@odd|@even] [more runs]

@odd / @even：这一局开模块的是奇数还是偶数编号势力（A/B）；不写就当所有势力都开。

1. 日志（ai_dll.log）：留下几次、打 / 在城边等、放手原因，命令被冲掉后补回几次。
2. 每次留下：之后 20 天这支部队掉了多少兵、有没有和敌军贴上、是回了城还是没了；它守的城 30 天内有没有易主。
3. 白跑：同一武将一次迎击结束转回城时，离家以外的一座本方城 16 格内，回城后 30 天内又被派回那座城 10 格内（「A 援 B
   → 回 A → 再去 B」），按开模块方 / 没开方分开数；其中转回城那天那座城已有第二波（以它为攻城目标或在它 15 格内的
   敌方进攻部队）的另记。
"""
import collections
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402

ATK = {'2', '8', '9', '10', '22', '3', '13', '14', '15', '16', '21'}
SIEGE = {'2', '8', '9', '10', '22'}
NEW = re.compile(r'(\d+)-(\d+)-(\d+) relief: army (\d+) \(force (\d+), troops (\d+), (\d+) soldiers, morale (\d+)\) going home '
                 r'to city (\d+) \((\d+) away\) stays for city (\d+) \((\d+) away\): attackers (\d+) soldiers in (\d+) armies, '
                 r'nearest (\d+) from the city; ours within \d+ of it (\d+) -> (attack army|into city|wait by the city at hex) (\d+)')
DAY = re.compile(r'relief: home from an intercept (\d+) \(no threatened city of ours nearer than home (\d+), home low on food '
                 r'(\d+), weak (\d+)\); armies new (\d+), kept (\d+) \(attack (\d+), (?:wait by|into) the city (\d+); orders put '
                 r'back (\d+)\); released: no threat (\d+), timeout (\d+), weak (\d+), new task or taken (\d+), city lost (\d+)'
                 r'(?:, gone \(into a city or wiped out\) (\d+))?')
DAY_KEYS = ['回城的迎击部队', '附近没有受威胁的城', '家城缺粮', '兵少或士气低', '新留下', '在管部队日', '打', '进城（第一版是城边等）',
            '补回命令', '放手：没威胁了', '放手：满 20 天', '放手：兵少', '放手：换任务或被接手', '放手：城丢了', '没了（进了城或被灭）']


def dn(d):
    return o.day_number(o.parse_date(d))


def odd_on(force_id, on):
    return None if not on else (force_id % 2 == 1) == (on == 'odd')


def analyse(run, on):
    rows = o.read_csv(run, 'armies')
    days = sorted({r['日期'] for r in rows}, key=dn)
    didx = {d: i for i, d in enumerate(days)}
    byday = collections.defaultdict(list)
    for r in rows:
        byday[r['日期']].append(r)
    fid = {f['君主']: int(f['势力']) for f in o.read_csv(run, 'forces')}
    fh = [f for f in o.read_csv(run, 'footholds') if f['类型'] == '城市']
    cpos = {f['名称']: int(f['格']) for f in fh}
    cname = {int(f['编号']): f['名称'] for f in fh}
    cid = {f['名称']: f['编号'] for f in fh}
    hist = collections.defaultdict(list)
    for c in o.read_csv(run, 'cities'):
        hist[c['城市']].append((dn(c['日期']), c['势力']))
    for e in o.read_csv(run, 'events'):
        if e['事件'] == '城市易主':
            hist[e['对象']].append((dn(e['日期']), e['到']))
    for v in hist.values():
        v.sort()

    def owner(name, d):
        w = None
        for dd, f in hist.get(name, []):
            if dd <= d or w is None:
                w = f
        return w

    # 1-2. the log
    log = os.path.join(run, 'ai_dll.log')
    text = open(log, encoding='utf-8-sig', errors='replace').read() if os.path.exists(log) else ''
    tot = collections.Counter()
    for m in DAY.finditer(text):
        for k, v in zip(DAY_KEYS, m.groups()):
            tot[k] += int(v or 0)
    keeps = []
    for m in NEW.finditer(text):
        g = m.groups()
        date = '%s-%02d-%02d' % (g[0], int(g[1]), int(g[2]))
        slot, city = g[3], cname.get(int(g[10]), g[10])
        seq = [r for d in days[didx[date]:didx[date] + 21] if d in didx for r in byday[d] if r['部队'] == slot] if date in didx else []
        if seq:
            gen = seq[0]['主将']
            seq = [r for r in seq if r['主将'] == gen]
        s0 = int(seq[0]['兵数']) if seq else int(g[6])
        last = seq[-1] if seq else None
        touched = 0
        for r in seq:
            foes = [x for x in byday[r['日期']] if x['势力'] != r['势力']]
            if any(o.hex_distance(int(x['格']), int(r['格'])) == 1 for x in foes):
                touched += 1
        home_near = last is not None and min(o.hex_distance(int(last['格']), h) for h in cpos.values()) <= 3
        f = last['势力'] if last else None
        lost = f is not None and owner(city, dn(date) + 31) != f
        keeps.append({'date': date, 'gen': seq[0]['主将'] if seq else '?', 'force': f, 'city': city, 'mode': g[16],
                      'thr': int(g[12]), 'ours': int(g[15]), 's0': s0, 's1': int(last['兵数']) if last else 0,
                      'touched': touched, 'end': '到城边（进城）' if home_near else ('兵很少，可能被灭' if last and int(last['兵数']) < 500 else '还在外面'),
                      'city_lost': lost})

    # 3. relief trips home and back again
    trips = collections.defaultdict(list)
    for d in days:
        for r in byday[d]:
            trips[r['主将']].append(r)
    again = []
    for gen, seq in trips.items():
        parts, cur = [], [seq[0]]
        for a, b in zip(seq, seq[1:]):
            if didx[b['日期']] - didx[a['日期']] > 1:
                parts.append(cur)
                cur = []
            cur.append(b)
        parts.append(cur)
        for t1, t2 in zip(parts, parts[1:]):
            gap = didx[t2[0]['日期']] - didx[t1[-1]['日期']]
            if gap > 30 or not any(x['军势任务'] == '1' for x in t1):
                continue
            k = len(t1) - 1
            while k > 0 and (t1[k - 1]['军势任务'] in ('17', '18') or t1[k - 1]['部队行动'] in ('2', '11')):
                k -= 1
            if t1[k]['军势任务'] not in ('17', '18') and t1[k]['部队行动'] not in ('2', '11'):
                continue
            turn, d0, f = int(t1[k]['格']), dn(t1[k]['日期']), t1[k]['势力']
            home = min(cpos, key=lambda c: o.hex_distance(cpos[c], int(t1[-1]['格'])))
            own = [c for c in cpos if c != home and owner(c, d0) == f and o.hex_distance(cpos[c], turn) <= 16]
            if not own:
                continue
            b = min(own, key=lambda c: o.hex_distance(cpos[c], turn))
            if min(o.hex_distance(int(x['格']), cpos[b]) for x in t2[:30]) > 10:
                continue
            wave = [x for x in byday[t1[k]['日期']] if x['势力'] != f and x['军势任务'] in ATK and
                    ((x['军势任务'] in SIEGE and x['军势目标'] == cid[b]) or o.hex_distance(int(x['格']), cpos[b]) <= 15)]
            again.append({'side': odd_on(fid.get(f, 0), on), 'wave': bool(wave), 'gen': gen, 'home': home, 'city': b,
                          'date': t1[k]['日期'], 'back': t2[0]['日期']})
    return tot, keeps, again, len(days)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    tot, keeps, again, nd = collections.Counter(), [], [], 0
    for arg in args:
        run, _, on = arg.partition('@')
        t, k, a, n = analyse(run, on)
        print('== %s：留下 %d 次；白跑 %d 次（开模块方 %d、没开方 %d）' % (os.path.basename(run.rstrip('/\\')), len(k), len(a),
              sum(x['side'] is True for x in a), sum(x['side'] is False for x in a)))
        tot.update(t)
        keeps += k
        again += a
        nd += n
    print('\n日志合计：' + '，'.join('%s %d' % (k, tot[k]) for k in DAY_KEYS))
    if keeps:
        print('\n留下 %d 次：打 %d，进城或城边等 %d；之后 20 天贴上过敌军 %d 次；兵 %d → %d（掉 %d%%）；结局：%s；守的城 30 天内易主 %d 次' % (
            len(keeps), sum(k['mode'].startswith('attack') for k in keeps), sum(not k['mode'].startswith('attack') for k in keeps),
            sum(k['touched'] > 0 for k in keeps), sum(k['s0'] for k in keeps), sum(k['s1'] for k in keeps),
            100 - 100 * sum(k['s1'] for k in keeps) // max(sum(k['s0'] for k in keeps), 1),
            dict(collections.Counter(k['end'] for k in keeps)), sum(k['city_lost'] for k in keeps)))
        for k in keeps[:20]:
            print('  %s %s（%s）守 %s：%s；来敌 %d、我方 %d；兵 %d → %d，贴上 %d 天，%s%s' % (
                k['date'], k['gen'], k['force'], k['city'], k['mode'], k['thr'], k['ours'], k['s0'], k['s1'], k['touched'], k['end'],
                '，城丢了' if k['city_lost'] else ''))
    for lab, side in (('开模块方', True), ('没开方', False), ('不分边', None)):
        xs = [x for x in again if x['side'] is side]
        if xs:
            print('\n白跑（%s）%d 次，转回城那天已有第二波 %d 次' % (lab, len(xs), sum(x['wave'] for x in xs)))
            for x in xs[:8]:
                print('  %s %s（%s 援 %s）%s 转回城，%s 再出%s' % ('', x['gen'], x['home'], x['city'], x['date'], x['back'],
                                                       '，当时已有第二波' if x['wave'] else ''))


if __name__ == '__main__':
    main()
