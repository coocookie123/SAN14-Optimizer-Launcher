"""Does the rally module (集结齐进) take effect? Per troops (军势) with 2+ armies and a rally purpose, per day:

  python -P ai/research/rally_check.py ai/runs/<run> [--months 12]

spread = largest distance between two armies of the troops. "Quiet" days: no army of another force within
4 hexes of any army of the troops (the module only rallies then). For quiet days with spread > 4 + 1 slack
(the module's threshold is 4 from the leader, and the leader is unknown in the record, so spread > 5 is
"certainly scattered"), it shows what the armies were doing (action 13 = wait, 1 = move) and whether the
spread shrank the next day. Without the module the original game sends the armies straight on.
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402

RALLY_PURPOSES = {'1', '2', '3', '8', '9', '10', '13', '14', '15', '16', '21', '22'}   # 12 is transport
SIEGE = {'2', '8', '9', '10', '22'}


def main():
    args = sys.argv[1:]
    months = None
    if '--months' in args:
        i = args.index('--months')
        months = int(args[i + 1])
        del args[i:i + 2]
    run = args[0]
    rows = o.read_csv(run, 'armies')
    days = sorted({r['日期'] for r in rows}, key=lambda d: o.day_number(o.parse_date(d)))
    lo = o.day_number(o.parse_date(days[0]))
    if months:
        keep = {d for d in days if o.day_number(o.parse_date(d)) < lo + months * 31}
        rows = [r for r in rows if r['日期'] in keep]
        days = [d for d in days if d in keep]
    by_day = collections.defaultdict(list)
    for r in rows:
        by_day[r['日期']].append(r)
    troops = {}          # (day, force, troops) -> rows
    for d, rs in by_day.items():
        for r in rs:
            troops.setdefault((d, r['势力'], r['军势']), []).append(r)
    nextday = {days[i]: days[i + 1] for i in range(len(days) - 1)}
    stat = collections.Counter()
    acts = collections.Counter()
    shrink = collections.Counter()
    examples = []
    for (d, force, t), rs in troops.items():
        if len(rs) < 2 or rs[0]['军势任务'] not in RALLY_PURPOSES:
            continue
        hexes = [int(r['格']) for r in rs]
        spread = max(o.hex_distance(a, b) for a in hexes for b in hexes)
        others = [int(r['格']) for r in by_day[d] if r['势力'] != force]
        quiet = not any(o.hex_distance(h, e) <= 4 for h in hexes for e in others)
        kind = 'siege' if rs[0]['军势任务'] in SIEGE else 'field'
        stat[(kind, quiet, spread > 5)] += 1
        if not (quiet and spread > 5):
            continue
        a = tuple(sorted(r['部队行动'] for r in rs))
        acts[(kind, '13' in a)] += 1
        nd = nextday.get(d)
        nrs = troops.get((nd, force, t)) if nd else None
        if nrs and len(nrs) >= 2:
            nh = [int(r['格']) for r in nrs]
            ns = max(o.hex_distance(x, y) for x in nh for y in nh)
            shrink[(kind, 'shrank' if ns < spread else 'same' if ns == spread else 'grew')] += 1
        if len(examples) < 12:
            examples.append((d, force, t, rs[0]['军势任务'], spread,
                             ' '.join('%s:%s@%s' % (r['主将'], r['部队行动'], r['格']) for r in rs)))
    print('军势日（2 支以上部队、集结适用的任务）：')
    for kind in ('field', 'siege'):
        name = '野战/迎击' if kind == 'field' else '攻城'
        q = stat[(kind, True, True)] + stat[(kind, True, False)]
        print('  %s：附近 4 格无他势力 %d 天，其中散开（最远两支 > 5 格）%d 天；附近有他势力 %d 天' % (
            name, q, stat[(kind, True, True)], stat[(kind, False, True)] + stat[(kind, False, False)]))
        print('    散开的那些天：有部队在待命(13) %d 天，没有 %d 天；第二天 缩小 %d / 不变 %d / 变大 %d' % (
            acts[(kind, True)], acts[(kind, False)], shrink[(kind, 'shrank')], shrink[(kind, 'same')],
            shrink[(kind, 'grew')]))
    print('例子（日期 势力 军势 任务 散开 主将:行动@格）：')
    for e in examples:
        print('  %s %s #%s 任务%s 散开%d  %s' % e)
    if rows and '本队' in rows[0]:
        by_leader(troops, nextday)


def by_leader(troops, nextday):
    """Runs recorded with the 本队 column: the module's own condition (an army > 4 from the 本队, no army of
    another force within 4 of the 本队) and what happened: did the 本队 wait, did the others head for its hex,
    did they get closer the next day."""
    c = collections.Counter()
    for (d, force, t), rs in troops.items():
        lead = [r for r in rs if r['本队'] == '是']
        if len(rs) < 2 or len(lead) != 1 or rs[0]['军势任务'] not in RALLY_PURPOSES:
            continue
        lead = lead[0]
        lh = int(lead['格'])
        far = [r for r in rs if r is not lead and o.hex_distance(lh, int(r['格'])) > 4]
        if not far:
            continue
        kind = 'siege' if rs[0]['军势任务'] in SIEGE else 'field'
        c[(kind, 'days')] += 1
        c[(kind, 'lead13')] += lead['部队行动'] == '13'
        nd = nextday.get(d)
        nxt = {r['部队']: r for r in troops.get((nd, force, t), [])} if nd else {}
        for r in far:
            c[(kind, 'far')] += 1
            c[(kind, 'to_lead')] += r['部队目标'] == '格%d' % lh
            n = nxt.get(r['部队'])
            nl = [x for x in nxt.values() if x['本队'] == '是']
            if n and nl:
                before = o.hex_distance(lh, int(r['格']))
                after = o.hex_distance(int(nl[0]['格']), int(n['格']))
                c[(kind, 'closer' if after < before else 'same' if after == before else 'farther')] += 1
    print('按本队看（有部队离本队超过 4 格的军势日）：')
    for kind, name in (('field', '野战/迎击'), ('siege', '攻城')):
        if not c[(kind, 'days')]:
            continue
        print('  %s：%d 天，本队待命 %d 天；离得远的部队 %d 支次，目标是本队所在格 %d，'
              '第二天离本队 近了 %d / 不变 %d / 远了 %d' % (
                  name, c[(kind, 'days')], c[(kind, 'lead13')], c[(kind, 'far')], c[(kind, 'to_lead')],
                  c[(kind, 'closer')], c[(kind, 'same')], c[(kind, 'farther')]))


if __name__ == '__main__':
    main()
