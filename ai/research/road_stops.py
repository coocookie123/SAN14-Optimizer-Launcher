"""迎击军势路上停住：本队和前一天在同一格、3 格内没有敌军、离自家城 4 格以上的日子。

  python -P ai/research/road_stops.py ai/runs/<run> [more runs] [--months 12]

按军势（势力、军势编号、这次迎击开始那天）统计：路上停的天数、停了几段（连续的停算一段）、停两段以上的军势数，
以及这些军势从出发到第一次接敌（本队 1 格内有敌军）用了几天。另看接战时孤军（本队周围 3 格内没有本军势其他部队）的比例。
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402


def dn(d):
    return o.day_number(o.parse_date(d))


def analyse(p, months):
    rows = o.read_csv(p, 'armies')
    days = sorted({r['日期'] for r in rows}, key=dn)
    lo = dn(days[0])
    byday = collections.defaultdict(list)
    for r in rows:
        if not months or dn(r['日期']) < lo + months * 31:
            byday[r['日期']].append(r)
    days = sorted(byday, key=dn)
    cities = [int(f['格']) for f in o.read_csv(p, 'footholds') if f['类型'] == '城市']
    trips = {}
    prev_lead = {}
    for d in days:
        today = byday[d]
        leads = {(x['势力'], x['军势']): x for x in today if x['本队'] == '是' and x['军势任务'] == '1'}
        for k, x in leads.items():
            h = int(x['格'])
            t = trips.get(k)
            if t is None or dn(d) - t['last'] > 3:
                t = trips[k] = dict(start=dn(d), last=dn(d), stops=0, runs=0, instop=False, contact=None)
                prev_lead[k] = None
            t['last'] = dn(d)
            foe_d = min([o.hex_distance(h, int(y['格'])) for y in today if y['势力'] != k[0]] or [99])
            if foe_d <= 1 and t['contact'] is None:
                t['contact'] = dn(d) - t['start']
            near_city = min(o.hex_distance(h, c) for c in cities) <= 3
            stopped = prev_lead.get(k) == h and foe_d > 3 and not near_city
            if stopped:
                t['stops'] += 1
                if not t['instop']:
                    t['runs'] += 1
                t['instop'] = True
            else:
                t['instop'] = False
            prev_lead[k] = h
    ts = list(trips.values())
    n = len(ts)
    if not n:
        return ['（没有迎击军势）']
    multi = sum(1 for t in ts if t['runs'] >= 2)
    stops = sum(t['stops'] for t in ts)
    cont = sorted(t['contact'] for t in ts if t['contact'] is not None)
    return ['迎击军势 %d 个：路上停住共 %d 天（每个 %.1f 天），停两段以上的 %d 个（%d%%）；出发到第一次接敌中位 %s 天（%d 个接了敌）' % (
        n, stops, stops / n, multi, 100 * multi // n, cont[len(cont) // 2] if cont else '-', len(cont))]


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
