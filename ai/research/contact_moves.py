# usage: python -P ai/research/contact_moves.py ai/runs/A [ai/runs/B ...]
"""Move orders (action 1/14) on armies touching an enemy: where they come from.
For each such army-day: lead or not, whether the order target is the lead's hex / another army's hex of the
same troop (gather) / something else, and whether the army was already in contact on the day the order
(same action + target) first appeared (order given while in contact) or made contact later."""
import sys, collections
sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__))))
import check_modules as C, observe as o

for path in sys.argv[1:]:
    r = C.Run(path, 12)
    days = r.days
    prev = {days[i + 1]: days[i] for i in range(len(days) - 1)}
    by = {(x['日期'], x['部队']): x for x in r.rows}

    def contact(d, x):
        hx = int(x['格'])
        return any(y['势力'] != x['势力'] and o.hex_distance(hx, int(y['格'])) <= 1 for y in r.byday[d])

    kinds = collections.Counter()
    for d, rs in r.byday.items():
        for x in rs:
            if x['部队行动'] not in ('1', '14') or not contact(d, x):
                continue
            t = x['部队目标']
            same = [y for y in rs if y['军势'] == x['军势'] and y['部队'] != x['部队']]
            lead = next((y for y in rs if y['军势'] == x['军势'] and y['本队'] == '是'), None)
            if not t.startswith('格'):
                tk = 'target ' + t[:2]
            elif lead and lead is not x and t == '格' + lead['格']:
                tk = 'lead hex'
            elif any(t == '格' + y['格'] for y in same):
                tk = 'friend hex'
            elif t == '格' + x['格']:
                tk = 'own hex'
            else:
                tk = 'other hex'
            # when did this order start?
            dd, start = d, d
            while dd in prev:
                p = by.get((prev[dd], x['部队']))
                if not p or p['主将'] != x['主将'] or p['部队行动'] != x['部队行动'] or p['部队目标'] != t:
                    break
                dd = prev[dd]
                start = dd
            x0 = by[(start, x['部队'])]
            given = 'given in contact' if contact(start, x0) else 'contact later'
            if t.startswith('格'):
                th = int(t[1:])
                fr = [y for y in r.byday[start] if y['军势'] == x['军势'] and y['部队'] != x['部队']]
                if any(int(y['格']) == th for y in fr):
                    tk = 'gather(friend hex at start)'
                elif x['阵型'] == '雁行' and o.hex_distance(th, int(x0['格'])) <= 2:
                    tk = 'yanxing step (stance)'
                else:
                    tk = 'other hex, %s away' % ('<=3' if o.hex_distance(th, int(x0['格'])) <= 3 else '>3')
            kinds[('lead' if x['本队'] == '是' else 'member', tk, given, 'solo' if not same else 'multi', x['阵型'], x['军势任务'])] += 1
    print(path, sum(kinds.values()))
    for k, v in kinds.most_common():
        print('  %-7s %-28s %-17s %-6s %-4s task%-3s %d' % (k + (v,)))
