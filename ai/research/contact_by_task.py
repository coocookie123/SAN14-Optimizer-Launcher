"""贴身交战（相邻格有敌军）的掉兵，按「军势单支 / 多支」和军势任务分：哪类任务亏得最多（2026-11-16，BEHAVIOR_PLAN 第四十九节）。
我掉 = 第二天比当天少的兵（含城防打的）；敌掉 = 贴着的敌军掉的兵，按贴着它的我方部队数平分。

  python -P ai/research/contact_by_task.py ai/runs/<run> [more runs]
"""
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import observe as o
from fu_stats import TASKS
def dn(d): return o.day_number(o.parse_date(d))
KIND = {'1': '迎击', '2': '攻城', '8': '攻城', '9': '攻城', '10': '攻城', '22': '攻城', '3': '区域攻击', '13': '区域攻击', '14': '区域攻击',
        '15': '区域攻击', '16': '区域攻击', '21': '区域攻击', '12': '运输', '5': '建设', '17': '回城', '18': '回城', '0': '撤退', '7': '集合', '23': '涂地防守'}
tot = collections.defaultdict(lambda: [0, 0, 0])
for run in sys.argv[1:]:
    rows = o.read_csv(run, 'armies')
    byday = collections.defaultdict(list)
    for r in rows: byday[dn(r['日期'])].append(r)
    days = sorted(byday)
    key = lambda x: (x['部队'], x['主将'])
    for i, d in enumerate(days[:-1]):
        nd = days[i + 1]
        nxt = {key(x): int(x['兵数']) for x in byday[nd]}
        cnt = collections.Counter((x['势力'], x['军势']) for x in byday[d])
        pos = collections.defaultdict(list)
        for x in byday[d]: pos[int(x['格'])].append(x)
        for x in byday[d]:
            h = int(x['格'])
            foes = [y for y in byday[d] if y['势力'] != x['势力'] and o.hex_distance(int(y['格']), h) == 1]
            if not foes: continue
            me = int(x['兵数']) - nxt.get(key(x), 0)
            fo = sum((int(y['兵数']) - nxt.get(key(y), 0)) / max(1, sum(1 for z in byday[d] if z['势力'] != y['势力'] and o.hex_distance(int(z['格']), int(y['格'])) == 1)) for y in foes)
            k = ('单支' if cnt[(x['势力'], x['军势'])] == 1 else '多支', KIND.get(x['军势任务'], x['军势任务']))
            t = tot[k]; t[0] += 1; t[1] += max(me, 0); t[2] += max(fo, 0)
print('%-6s %-8s %8s %10s %10s %6s' % ('军势', '任务', '部队日', '我掉', '敌掉', '交换'))
for k in sorted(tot, key=lambda k: -tot[k][1]):
    n, a, b = tot[k]
    if n >= 20: print('%-6s %-8s %8d %10d %10d %6.2f' % (k[0], k[1], n, a, b, b / max(a, 1)))
