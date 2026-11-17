"""贴敌待命改还手 A/B（--ab odd:waitfight）：贴敌时待命的部队日、接战后溃灭，按任务和攻方势力编号奇偶分开（2026-11-14）。

  python -P ai/research/waitfight_ab.py --ab ai/runs/A1 A2 ... [--base ai/runs/B1 B2 ...]

每组、每个任务（迎击、攻城）：近战部队（不含雁行、攻城兵器）贴着敌军（1 格内有别的势力的部队）的部队日里，
打部队 / 待命 / 打城或据点的占比；出征里接战的、接战后溃灭的、掉的兵（最多兵 − 最后兵）。
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import observe as o  # noqa: E402
from fu_stats import TASKS  # noqa: E402

KEEP = ('迎击', '攻城')
SKIP_FORM = ('雁行', '井阑', '冲车', '投石')


def group(runs, odd):
    days = collections.Counter()
    so = collections.Counter()
    for p in runs:
        fid = {f['君主']: int(f['势力']) for f in o.read_csv(p, 'forces')}
        mine = lambda force: (fid.get(force, 0) % 2 == 1) == odd  # noqa: E731
        for s in o.read_csv(p, 'sorties'):
            t = TASKS.get(s['军势任务'], '')
            if t not in KEEP or not mine(s['势力']):
                continue
            if int(s['接战天数']) > 0:
                so[(t, '接战')] += 1
                so[(t, '溃灭')] += s['结局'].startswith('溃灭')
            so[(t, '掉兵')] += max(0, int(s['最多兵数']) - int(s['最后兵数']))
        byday = collections.defaultdict(list)
        for x in o.read_csv(p, 'armies'):
            byday[x['日期']].append(x)
        for d, rs in byday.items():
            for x in rs:
                t = TASKS.get(x['军势任务'], '')
                if t not in KEEP or x['阵型'] in SKIP_FORM or not mine(x['势力']):
                    continue
                h = int(x['格'])
                if not any(y['势力'] != x['势力'] and o.hex_distance(h, int(y['格'])) <= 1 for y in rs):
                    continue
                tg = x['部队目标']
                kind = ('打部队' if tg.startswith('部队') else '待命' if x['部队行动'] == '13'
                        else '打城或据点' if x['部队行动'] in ('4', '5', '6') else '其他')
                days[(t, kind)] += 1
                days[(t, '合计')] += 1
    return days, so


def line(label, days, so):
    parts = []
    for t in KEEP:
        n = days[(t, '合计')] or 1
        e = so[(t, '接战')] or 1
        parts.append('%s 贴敌部队日 %d：打部队 %d%%、待命 %d%%、打城或据点 %d%%；接战 %d、溃灭 %d（%d%%）、掉兵 %d' % (
            t, days[(t, '合计')], 100 * days[(t, '打部队')] // n, 100 * days[(t, '待命')] // n,
            100 * days[(t, '打城或据点')] // n, so[(t, '接战')], so[(t, '溃灭')], 100 * so[(t, '溃灭')] // e,
            so[(t, '掉兵')]))
    print('%-10s %s' % (label, ' | '.join(parts)))


def main():
    a = sys.argv[1:]
    ab = a[a.index('--ab') + 1:a.index('--base')] if '--base' in a else a[a.index('--ab') + 1:]
    base = a[a.index('--base') + 1:] if '--base' in a else []
    line('A/B 奇数', *group(ab, True))
    line('A/B 偶数', *group(ab, False))
    if base:
        line('对照 奇数', *group(base, True))
        line('对照 偶数', *group(base, False))


if __name__ == '__main__':
    main()
