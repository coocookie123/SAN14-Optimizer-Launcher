"""一部分撤、一部分迎击：同一势力的一个军势（迎击或区域攻击）转撤退的那天，10 格内有本势力另一个军势仍在迎击 / 区域攻击。

  python -P ai/research/split_retreat.py ai/runs/<run> [more runs] [--months 12]

转撤退＝军势任务从 1（迎击）或 3、13–16、21、23（区域攻击）变成 0（撤退）。按本队位置算距离。另列这些撤退时：
撤的军势本队 8 格内的敌兵、5 格内和 8 格内的我方兵（含另一个军势），看「我方只算 5 格」是不是撤的原因。
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402

FIGHT = {'1', '3', '13', '14', '15', '16', '21', '23'}


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
    task_prev = {}
    bytask = collections.Counter()
    turns = split = 0
    detail = []
    for d in days:
        troops = {}
        for x in byday[d]:
            k = (x['势力'], x['军势'])
            if x['本队'] == '是':
                troops[k] = x
        for k, lead in troops.items():
            t0, t1 = task_prev.get(k), lead['军势任务']
            if t1 == '0' and t0 in FIGHT:
                turns += 1
                h = int(lead['格'])
                others = [v for kk, v in troops.items() if kk != k and kk[0] == k[0] and v['军势任务'] in FIGHT
                          and o.hex_distance(int(v['格']), h) <= 10]
                foes8 = sum(int(y['兵数']) for y in byday[d] if y['势力'] != k[0] and o.hex_distance(int(y['格']), h) <= 8)
                own5 = sum(int(y['兵数']) for y in byday[d] if y['势力'] == k[0] and o.hex_distance(int(y['格']), h) <= 5)
                own8 = sum(int(y['兵数']) for y in byday[d] if y['势力'] == k[0] and o.hex_distance(int(y['格']), h) <= 8)
                if others:
                    split += 1
                    detail.append((foes8, own5, own8))
                    bytask[t0] += 1
        task_prev = {k: v['军势任务'] for k, v in troops.items()}
    out = ['军势转撤退 %d 次，其中 10 格内本势力另一个军势仍在迎击 / 区域攻击 %d 次（撤前任务：%s）' % (
        turns, split, '、'.join('%s %d' % ('迎击' if k == '1' else '区域' + k, v) for k, v in bytask.most_common()))]
    if detail:
        n = len(detail)
        r5 = sorted(f / max(a, 1) for f, a, b in detail)[n // 2]
        r8 = sorted(f / max(b, 1) for f, a, b in detail)[n // 2]
        more = sum(1 for f, a, b in detail if b > a)
        foe = sum(1 for f, a, b in detail if f >= 1000)
        out.append('  这些撤退时：8 格内敌兵 ÷ 5 格内我兵 中位 %.2f，÷ 8 格内我兵 中位 %.2f；8 格内比 5 格内多出友军的 %d/%d；'
                   '8 格内有敌军（1000 兵以上）的 %d' % (r5, r8, more, n, foe))
    return out


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
