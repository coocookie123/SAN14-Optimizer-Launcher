"""行为异常扫描：每局看海测试后和对照局（最好是原版局）比，明显变多的标「要查」。check_modules.py 会调用它。

  python -P ai/research/anomalies.py ai/runs/<本局> ai/runs/<对照局> [--months 12] [--list]

扫的几项（一年的次数）：
  回原城：出城 15 天内回到出发的城，没贴过敌军（按出城时的军势任务分）；
  早撤：迎击部队出城 3 天内军势任务就变成撤退；
  来回跑：一支部队 4 天内离开某格 2 格以上又回到它 1 格内（不在交战、不在城边 3 格内）；
  干等：不在城边、不在攻城、10 格内没有敌军，同一格连续停 10 天以上；
  贴敌开往：贴着敌军（1 格）却带着「开往某格」（部队行动 1、目标是格子）的部队日。
本局比对照局多一倍以上、而且至少 10 次的标「要查」。
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402

SIEGE = {'2', '8', '9', '10', '22'}
KIND = {'1': '迎击', '2': '攻城', '8': '攻城', '9': '攻城', '10': '攻城', '22': '攻城', '3': '区域攻击', '13': '区域攻击',
        '14': '区域攻击', '15': '区域攻击', '16': '区域攻击', '21': '区域攻击', '12': '运输', '5': '建设', '17': '回城',
        '18': '回城', '0': '撤退', '7': '集合', '23': '涂地防守', '6': '随本队', '4': '攻设施', '11': '占领', '20': '援军'}


def dn(d):
    return o.day_number(o.parse_date(d))


EXAMPLES = []      # back-and-forth stretches of the last scan, for --list


def scan(path, months):
    rows = o.read_csv(path, 'armies')
    days = sorted({r['日期'] for r in rows}, key=dn)
    lo = dn(days[0])
    rows = [r for r in rows if not months or dn(r['日期']) < lo + months * 31]
    byday = collections.defaultdict(list)
    for r in rows:
        byday[r['日期']].append(r)
    days = sorted(byday, key=dn)
    cities = [int(f['格']) for f in o.read_csv(path, 'footholds')]
    near_city = {}

    def by_city(h, r=3):
        k = (h, r)
        if k not in near_city:
            near_city[k] = min(o.hex_distance(h, c) for c in cities) <= r
        return near_city[k]
    # per army trip (slot + general, consecutive days)
    trips = collections.defaultdict(list)
    for d in days:
        for x in byday[d]:
            trips[(x['部队'], x['主将'])].append(x)
    contact_day = set()
    for d in days:
        rs = byday[d]
        for x in rs:
            hx = int(x['格'])
            if any(y['势力'] != x['势力'] and o.hex_distance(hx, int(y['格'])) <= 1 for y in rs):
                contact_day.add((d, x['部队']))
    out = collections.Counter()
    detail = collections.defaultdict(collections.Counter)
    for k, seq in trips.items():
        # split at gaps (a slot reused later by the same general)
        parts, cur = [], [seq[0]]
        for a, b in zip(seq, seq[1:]):
            if dn(b['日期']) - dn(a['日期']) > 2:
                parts.append(cur)
                cur = []
            cur.append(b)
        parts.append(cur)
        for t in parts:
            a, b = t[0], t[-1]
            if dn(a['日期']) == lo:
                continue
            contact = any((x['日期'], x['部队']) in contact_day for x in t)
            n = dn(b['日期']) - dn(a['日期']) + 1
            ha, hb = int(a['格']), int(b['格'])
            if not contact and n <= 15 and o.hex_distance(ha, hb) <= 3 and by_city(ha) and dn(b['日期']) < dn(days[-1]):
                out['回原城'] += 1
                detail['回原城'][KIND.get(a['军势任务'], a['军势任务'])] += 1
            if a['军势任务'] == '1' or (a['军势任务'] == '0' and len(t) > 0):
                early = next((x for x in t[:4] if x['军势任务'] == '0'), None)
                if early and a['军势任务'] in ('1', '0') and dn(early['日期']) - dn(a['日期']) <= 3:
                    out['早撤'] += 1
            # back and forth
            for i in range(len(t) - 2):
                x0 = int(t[i]['格'])
                if (t[i]['日期'], t[i]['部队']) in contact_day or by_city(x0):
                    continue
                far = False
                for j in range(i + 1, min(i + 5, len(t))):
                    xj = int(t[j]['格'])
                    if o.hex_distance(x0, xj) >= 2:
                        far = True
                    elif far and o.hex_distance(x0, xj) <= 1:
                        out['来回跑'] += 1
                        detail['来回跑'][KIND.get(t[i]['军势任务'], t[i]['军势任务'])] += 1
                        EXAMPLES.append(t[max(0, i - 1):j + 2])
                        break
            # idle
            still = 1
            for p, q in zip(t, t[1:]):
                hq = int(q['格'])
                quiet = (p['格'] == q['格'] and not by_city(hq) and q['军势任务'] not in SIEGE and
                         not any(y['势力'] != q['势力'] and o.hex_distance(hq, int(y['格'])) <= 10 for y in byday[q['日期']]))
                still = still + 1 if quiet else 1
                if still == 10:
                    out['干等'] += 1
                    detail['干等'][KIND.get(q['军势任务'], q['军势任务'])] += 1
    for d in days:
        for x in byday[d]:
            if (d, x['部队']) in contact_day and x['部队行动'] == '1' and x['部队目标'].startswith('格'):
                out['贴敌开往'] += 1
                detail['贴敌开往'][x['阵型']] += 1
    return out, detail


def report(run, base, months=12):
    a, da = scan(run, months)
    b, db = scan(base, months) if base else (collections.Counter(), {})
    lines = []
    for k in ('回原城', '早撤', '来回跑', '干等', '贴敌开往'):
        flag = '  要查' if a[k] >= 10 and a[k] >= 2 * max(b[k], 1) else ''
        top = '、'.join('%s %d' % kv for kv in da.get(k, collections.Counter()).most_common(4))
        what = '按阵型' if k == '贴敌开往' else '按任务'
        lines.append('%s：%d（对照 %d）%s%s' % (k, a[k], b[k], ('；%s ' % what + top) if top else '', flag))
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
    a, _ = scan(args[0], months)
    ex = list(EXAMPLES)
    for ln in report(args[0], args[1] if len(args) > 1 else None, months):
        print(ln)
    if listing:
        print('来回跑的例子（日期 格 军势任务 部队行动 部队目标 本队）：')
        for seg in ex[:12]:
            x = seg[0]
            print('  %s(%s) 阵型 %s：' % (x['主将'], x['势力'], x['阵型']) + ' | '.join(
                '%s %s 任%s 行%s %s%s' % (y['日期'][5:], y['格'], y['军势任务'], y['部队行动'], y['部队目标'],
                                         '*' if y['本队'] == '是' else '') for y in seg))


if __name__ == '__main__':
    main()
