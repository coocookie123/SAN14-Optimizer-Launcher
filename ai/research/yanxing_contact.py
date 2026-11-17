# usage: python -P ai/research/yanxing_contact.py [--odd] ai/runs/A [ai/runs/B ...]
"""雁行 contact episodes: from the day an enemy army comes next to a 雁行 until it is free again (no enemy
adjacent), wiped, back in a city, or the year ends. How long the contact lasts, how it ends, the 雁行's loss
per episode and per contact day, and the order it had on the first contact day.
--odd: only armies of odd force ids (A/B runs: the side with the modules)."""
import sys, collections, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import check_modules as C, observe as o

args = [a for a in sys.argv[1:] if not a.startswith('--')]
odd_only = '--odd' in sys.argv
for path in args:
    r = C.Run(path, 12)
    ids = {f['君主']: int(f['势力']) for f in o.read_csv(r.path, 'forces')}
    fate = {(s['部队'], s['主将']): s['结局'] for s in o.read_csv(r.path, 'sorties')}
    days = r.days
    by = {(x['日期'], x['部队']): x for x in r.rows}

    def touching(d, x):
        return any(y['势力'] != x['势力'] and o.hex_distance(int(x['格']), int(y['格'])) <= 1 for y in r.byday[d])

    ends = collections.Counter()
    first = collections.Counter()
    length, lost, cdays = [], 0, 0
    for i, d in enumerate(days):
        for x in r.byday[d]:
            if x['阵型'] != '雁行' or (odd_only and ids.get(x['势力'], 0) % 2 == 0) or not touching(d, x):
                continue
            p = by.get((days[i - 1], x['部队'])) if i else None
            if p and p['主将'] == x['主将'] and touching(days[i - 1], p):
                continue                     # not the first day of this contact
            a = x['部队行动']
            first['attack' if a == '7' else 'wait' if a == '13' else 'move' if a in ('1', '14') else 'go home' if a in ('2', '11') else 'other'] += 1
            cur, end, n = int(x['兵数']), 'still at the end', 1
            for j in range(i + 1, len(days)):
                nx = by.get((days[j], x['部队']))
                if not nx or nx['主将'] != x['主将']:
                    end = 'wiped' if fate.get((x['部队'], x['主将']), '').startswith('溃灭') else 'back in a city'
                    break
                lost += max(0, cur - int(nx['兵数']))
                cur = int(nx['兵数'])
                if not touching(days[j], nx):
                    end = 'got free'
                    break
                n += 1
            ends[end] += 1
            length.append(n)
            cdays += n
    k = sum(ends.values())
    if not k:
        print(path, 'no 雁行 contact')
        continue
    length.sort()
    print('%s  雁行 contact episodes %d: got free %d%%, wiped %d%%, back in a city %d%%; days in contact median %d, mean %.1f; '
          'loss per episode %d, per contact day %d; first-day order %s' % (
              path, k, 100 * ends['got free'] // k, 100 * ends['wiped'] // k, 100 * ends['back in a city'] // k,
              length[len(length) // 2], sum(length) / k, lost // k, lost // max(cdays, 1), dict(first)))
