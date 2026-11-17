"""交战地点的府分差：按任务和攻方势力编号奇偶，看贴身交战时「我方府分 − 对方府分」（各自所在格）的分布和交换比（2026-11-14）。

  python -P ai/research/fu_gap_ab.py --ab ai/runs/A1 A2 ... [--base ai/runs/B1 B2 ...] [--task 攻城]

每个部队日：这支部队有贴身（1 格内）的别的势力部队、两边第二天都还在时，府分差 = 自己的府分 − 贴身敌军府分的平均；
自己掉的兵 = 今天 − 明天，对方掉的兵 = 贴身敌军掉兵的平均；按府分差分组算交换比（对方掉 ÷ 自己掉）。
第四十六节：全部贴身交战落后 0.5 分以上交换比 0.32–0.44、持平 1.00、领先 2.9–3.1；攻城四分之三在落后时打。
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import observe as o  # noqa: E402
from fu_stats import TASKS  # noqa: E402

ORDER = ['落后≥2', '落后0.5~2', '持平', '领先0.5~2', '领先≥2']


def bucket(d):
    return '落后≥2' if d <= -2 else '落后0.5~2' if d < -0.5 else '持平' if d <= 0.5 else '领先0.5~2' if d < 2 else '领先≥2'


def group(runs, odd, task):
    n, own, opp = collections.Counter(), collections.Counter(), collections.Counter()
    for p in runs:
        fid = {f['君主']: int(f['势力']) for f in o.read_csv(p, 'forces')}
        byday = collections.defaultdict(dict)
        for x in o.read_csv(p, 'armies'):
            byday[o.day_number(o.parse_date(x['日期']))][(x['势力'], x['部队'], x['主将'])] = x
        for d, rs in byday.items():
            nxt = byday.get(d + 1)
            if not nxt:
                continue
            for k, a in rs.items():
                if k not in nxt or not a['府分'] or (fid.get(a['势力'], 0) % 2 == 1) != odd:
                    continue
                if TASKS.get(a['军势任务'], '其他') != task:
                    continue
                h = int(a['格'])
                adj = [(kb, b) for kb, b in rs.items() if kb[0] != k[0] and kb in nxt and b['府分'] and
                       o.hex_distance(h, int(b['格'])) == 1]
                if not adj:
                    continue
                fe = sum(float(b['府分']) for _, b in adj) / len(adj)
                key = bucket(float(a['府分']) - fe)
                n[key] += 1
                own[key] += max(0, int(a['兵数']) - int(nxt[k]['兵数']))
                opp[key] += sum(max(0, int(b['兵数']) - int(nxt[kb]['兵数'])) for kb, b in adj) / len(adj)
    return n, own, opp


def line(label, n, own, opp):
    tot = sum(n.values()) or 1
    print('%-10s 贴敌部队日 %d：' % (label, sum(n.values())) + '；'.join(
        '%s %d%% 交换 %.2f' % (b, 100 * n[b] // tot, opp[b] / max(own[b], 1)) for b in ORDER if n[b]) +
        '；合计交换 %.2f' % (sum(opp.values()) / max(sum(own.values()), 1)))


def main():
    a = sys.argv[1:]
    task = a[a.index('--task') + 1] if '--task' in a else '攻城'
    a = [x for i, x in enumerate(a) if x != '--task' and (i == 0 or a[i - 1] != '--task')]
    ab = a[a.index('--ab') + 1:a.index('--base')] if '--base' in a else a[a.index('--ab') + 1:]
    base = a[a.index('--base') + 1:] if '--base' in a else []
    print('任务：%s' % task)
    line('A/B 奇数', *group(ab, True, task))
    line('A/B 偶数', *group(ab, False, task))
    if base:
        line('对照 奇数', *group(base, True, task))
        line('对照 偶数', *group(base, False, task))


if __name__ == '__main__':
    main()
