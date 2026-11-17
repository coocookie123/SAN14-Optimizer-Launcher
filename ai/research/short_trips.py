"""出征后进了另一座城、或出去不久又回原城的部队：起点城、终点城、天数、有没有贴过敌军、军势任务。

  python -P ai/research/short_trips.py ai/runs/<run> [more runs] [--months 12] [--list N]

一次出征＝同一部队槽、同一主将连续出现的日子。起点、终点取第一天、最后一天离部队最近的自家城（3 格内才算）。
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402

KIND = {'1': '迎击', '2': '攻城', '8': '攻城', '9': '攻城', '10': '攻城', '22': '攻城', '3': '区域攻击', '13': '区域攻击',
        '14': '区域攻击', '15': '区域攻击', '16': '区域攻击', '21': '区域攻击', '12': '运输', '5': '建设', '17': '回城',
        '18': '回城', '0': '撤退', '7': '集合', '23': '涂地防守', '6': '随本队', '4': '攻设施', '11': '占领', '20': '援军'}


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
    fh = {f['编号']: (f['名称'], int(f['格'])) for f in o.read_csv(path, 'footholds') if f['类型'] == '城市'}
    hist = collections.defaultdict(list)
    for c in o.read_csv(path, 'cities'):
        hist[c['城市']].append((dn(c['日期']), c['势力']))
    for e in o.read_csv(path, 'events'):
        if e['事件'] == '城市易主':
            hist[e['对象']].append((dn(e['日期']), e['到']))
    for v in hist.values():
        v.sort()

    def owner(name, d):
        w = None
        for dd, f in hist.get(name, []):
            if dd <= d or w is None:
                w = f if dd <= d or w is None else w
        return w

    def city_at(h, force, d):
        best = None
        for cid, (name, ch) in fh.items():
            dist = o.hex_distance(h, ch)
            if dist <= 3 and owner(name, d) == force and (best is None or dist < best[1]):
                best = (name, dist)
        return best[0] if best else None
    trips = []
    open_ = {}
    for d in days:
        here = set()
        for x in byday[d]:
            k = (x['部队'], x['主将'])
            here.add(k)
            t = open_.get(k)
            if t is None:
                t = open_[k] = dict(start=d, first=x, rows=[], contact=False)
            t['rows'].append(x)
            if not t['contact'] and any(y['势力'] != x['势力'] and o.hex_distance(int(x['格']), int(y['格'])) <= 1
                                        for y in byday[d]):
                t['contact'] = True
        for k in list(open_):
            if k not in here:
                trips.append(open_.pop(k))
    out = collections.Counter()
    by_task = collections.defaultdict(collections.Counter)
    examples = collections.defaultdict(list)
    for t in trips:
        a, b = t['first'], t['rows'][-1]
        if dn(a['日期']) == lo:            # already out when the record started
            continue
        f = a['势力']
        s = city_at(int(a['格']), f, dn(a['日期']))
        e = city_at(int(b['格']), f, dn(b['日期']))
        n = dn(b['日期']) - dn(a['日期']) + 1
        task = KIND.get(a['军势任务'], a['军势任务'])
        tasks = '→'.join(dict.fromkeys(KIND.get(x['军势任务'], x['军势任务']) for x in t['rows']))
        out['出征'] += 1
        if not s or not e:
            continue
        if e != s and not t['contact']:
            k = '进了另一座城、没交战'
        elif e == s and not t['contact'] and n <= 15:
            k = '15 天内回原城、没交战'
        else:
            continue
        out[k] += 1
        by_task[k][task] += 1
        examples[(k, task)].append('%s %s(%s) %s→%s %d 天，任务 %s' % (a['日期'], a['主将'], f, s, e, n, tasks))
    lines = ['出征 %d 次；%s' % (out['出征'], '；'.join('%s %d 次（按出城时的军势任务：%s）' % (
        k, out[k], '、'.join('%s %d' % kv for kv in by_task[k].most_common())) for k in ('进了另一座城、没交战', '15 天内回原城、没交战')))]
    if listing:
        for (k, task), v in sorted(examples.items(), key=lambda kv: -len(kv[1]))[:6]:
            lines.append('  例：%s / %s：%s' % (k, task, '；'.join(v[:listing])))
    return lines


def main():
    args = sys.argv[1:]
    months, listing = 12, 0
    if '--months' in args:
        i = args.index('--months')
        months = int(args[i + 1])
        del args[i:i + 2]
    if '--list' in args:
        i = args.index('--list')
        listing = int(args[i + 1])
        del args[i:i + 2]
    for p in args:
        print('== %s' % os.path.basename(p.rstrip('/\\')))
        for ln in analyse(p, months, listing):
            print('  ' + ln)


if __name__ == '__main__':
    main()
