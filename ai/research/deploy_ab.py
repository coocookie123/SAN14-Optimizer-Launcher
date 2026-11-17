"""出兵编成 A/B（--ab odd:deploy2 配 deploy1e，或反过来 --ab even:deploy2 配 deploy1o）：迎击编成和交战结果按势力编号奇偶分开（2026-11-14）。

  python -P ai/research/deploy_ab.py ai/runs/<run> [more runs]

每个迎击军势（军势任务 1，同一势力同一军势编号，中间断开 5 天以上算两次）：几支部队、合计兵数、每支兵数分布、
雁行几支；迎击出征按兵数档和阵型分的接战、溃灭（11-14 第二版发现 1000–1500 兵的近战啦啦队接战后 43% 溃灭）；
另把每局 check.txt 里「A/B」一节（城市、交战后溃灭、按对手分组的交换比）原样列出。
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402

BINS = [(1000, '≤1000'), (1500, '1001–1500'), (2000, '1501–2000'), (3000, '2001–3000'), (6000, '3001–6000'), (10 ** 9, '>6000')]


def dn(d):
    return o.day_number(o.parse_date(d))


def troops(p):
    fid = {f['君主']: int(f['势力']) for f in o.read_csv(p, 'forces')}
    ep = collections.defaultdict(list)
    for r in o.read_csv(p, 'armies'):
        if r['军势任务'] == '1':
            ep[(r['势力'], r['军势'])].append(r)
    out = []
    for (force, t), rs in ep.items():
        rs.sort(key=lambda x: dn(x['日期']))
        cur = [rs[0]]
        for x in rs[1:]:
            if dn(x['日期']) - dn(cur[-1]['日期']) > 5:
                out.append((fid.get(force, 0), cur))
                cur = []
            cur.append(x)
        out.append((fid.get(force, 0), cur))
    res = []
    for f, rs in out:
        units = {}
        for x in rs:
            units.setdefault((x['部队'], x['主将']), x)
        res.append((f, [(int(x['兵数']), x['阵型']) for x in units.values()]))
    return res


def by_size(runs):
    """迎击出征按兵数档和阵型（雁行 / 其他）分：出征、接战（接战天数 > 0）、接战后溃灭；按势力编号奇偶分开。"""
    g = collections.defaultdict(lambda: [0, 0, 0])
    for p in runs:
        fid = {f['君主']: int(f['势力']) for f in o.read_csv(p, 'forces')}
        form = {}
        for x in o.read_csv(p, 'armies'):
            form.setdefault((x['部队'], x['主将'], x['势力']), x['阵型'])
        for s in o.read_csv(p, 'sorties'):
            if s['军势任务'] != '1':
                continue
            mx = int(s['最多兵数'])
            b = '≤1500' if mx <= 1500 else '1501–2000' if mx <= 2000 else '2001–3000' if mx <= 3000 else '>3000'
            f = '雁行' if form.get((s['部队'], s['主将'], s['势力'])) == '雁行' else '其他'
            k = (fid.get(s['势力'], 0) % 2, b, f)
            g[k][0] += 1
            if int(s['接战天数']) > 0:
                g[k][1] += 1
                g[k][2] += s['结局'].startswith('溃灭')
    for side, lab in ((1, '奇数方'), (0, '偶数方')):
        print('%s 迎击出征按兵数：' % lab + '；'.join(
            '%s%s 出征 %d 接战 %d 溃灭 %d（%d%%）' % (b, f, n, e, w, 100 * w // max(e, 1))
            for (sd, b, f), (n, e, w) in sorted(g.items(), key=lambda kv: (['≤1500', '1501–2000', '2001–3000', '>3000'].index(kv[0][1]), kv[0][2]))
            if sd == side))


def main():
    side = {1: collections.defaultdict(list), 0: collections.defaultdict(list)}
    for p in sys.argv[1:]:
        for f, us in troops(p):
            s = side[f % 2]
            s['n'].append(len(us))
            s['tot'].append(sum(u[0] for u in us))
            s['units'] += us
    for k, lab in ((1, '奇数方'), (0, '偶数方')):
        s = side[k]
        n = len(s['n'])
        if not n:
            continue
        us = s['units']
        dist = collections.Counter(next(lab_ for lim, lab_ in BINS if u[0] <= lim) for u in us)
        print('%s：迎击军势 %d 个，平均 %.2f 支、合计 %d 兵；5 支以上的 %d 个；每支兵数 %s；雁行 %d 支（%d%%）' % (
            lab, n, sum(s['n']) / n, sum(s['tot']) // n, sum(1 for x in s['n'] if x >= 5),
            '，'.join('%s %d%%' % (b, 100 * dist[b] // len(us)) for _, b in BINS),
            sum(1 for u in us if u[1] == '雁行'), 100 * sum(1 for u in us if u[1] == '雁行') // len(us)))
    by_size(sys.argv[1:])
    for p in sys.argv[1:]:
        txt = open(os.path.join(p, 'check.txt'), encoding='utf-8').read()
        i = txt.find('== A/B')
        if i >= 0:
            block = txt[i:].split('\n')
            first = next(j for j, ln in enumerate(block) if ln.strip().startswith('['))
            print('== %s' % os.path.basename(p.rstrip('/\\')))
            for ln in block[first + 1:first + 7]:
                if ln.strip().startswith('['):
                    break
                print(ln)


if __name__ == '__main__':
    main()
