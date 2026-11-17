"""守城迎击（DLL defend）每一条命令之后怎样：把 DLL 日志里的「defend R1/R2: army 甲 -> army 乙」对上看海记录。

  python -P ai/research/defend_orders.py logs/ai_dll.log ai/runs/<run> [--from HH:MM:SS]

R1 打攻城兵器，R2 城被围时不进城、去打围城敌军。每条命令（同一支部队打同一目标只算第一次）看：
3 天内有没有贴上目标、目标 5 天内掉了多少兵、15 天内是否溃灭、我方这支 15 天内是否溃灭。
对照：research/defend_events.py 的「打攻城兵器的机会」一节（另一组势力里同样的情况，没人下令时怎样）。
"""
import collections
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import observe as o  # noqa: E402


def dn(d):
    return o.day_number(o.parse_date(d))


def main():
    a = sys.argv[1:]
    since = a[a.index('--from') + 1] if '--from' in a else None
    a = [x for i, x in enumerate(a) if x != '--from' and (i == 0 or a[i - 1] != '--from')]
    log, run = a[0], a[1]
    orders = []
    with open(log, encoding='utf-8', errors='replace') as f:
        for ln in f:
            m = re.match(r'(\d\d:\d\d:\d\d)\.\d+ (\d+)-(\d+)-(\d+) defend (R\d): army (\d+) \(force (\d+)\) -> army (\d+) '
                         r'\(force (\d+), formation (\d+)\), distance (\d+)', ln.strip())
            if m and not (since and m.group(1) < since):
                y, mo, d = int(m.group(2)), int(m.group(3)), int(m.group(4))
                orders.append(('%d-%02d-%02d' % (y, mo, d), m.group(5), m.group(6), m.group(8), int(m.group(11))))
    rows = o.read_csv(run, 'armies')
    by = {(x['日期'], x['部队']): x for x in rows}
    days = sorted({x['日期'] for x in rows}, key=dn)
    sorties = collections.defaultdict(list)
    for s in o.read_csv(run, 'sorties'):
        sorties[(s['部队'], s['主将'])].append(s)

    def wiped_within(slot, gen, d, n):
        return any(s['结局'].startswith('溃灭') and dn(s['出发']) <= dn(d) <= dn(s['结束']) and dn(s['结束']) - dn(d) <= n
                   for s in sorties.get((slot, gen), []))
    seen = set()
    stat = collections.defaultdict(lambda: [0, 0, 0, 0, 0])
    lines = []
    for d, rule, us, them, dist in orders:
        x, y = by.get((d, us)), by.get((d, them))
        if not x or not y:
            continue
        key = (rule, us, x['主将'], them, y['主将'])
        if key in seen:
            continue
        seen.add(key)
        i = days.index(d)
        touch = any((by.get((dd, us)) or {}).get('主将') == x['主将'] and (by.get((dd, them)) or {}).get('主将') == y['主将']
                    and o.hex_distance(int(by[(dd, us)]['格']), int(by[(dd, them)]['格'])) <= 1 for dd in days[i:i + 4])
        later = [by.get((dd, them)) for dd in days[i:i + 6]]
        later = [z for z in later if z and z['主将'] == y['主将']]
        loss = int(y['兵数']) - (int(later[-1]['兵数']) if later else 0)
        tw = wiped_within(them, y['主将'], d, 15)
        uw = wiped_within(us, x['主将'], d, 15)
        s = stat[rule]
        s[0] += 1
        s[1] += touch
        s[2] += max(0, loss)
        s[3] += tw
        s[4] += uw
        lines.append('  %s %s %s(%s %s兵) -> %s(%s %s %s兵) 距 %d：3 天内贴上 %s，目标 5 天掉 %d，目标溃灭 %s，我方溃灭 %s' % (
            d, rule, x['主将'], x['势力'], x['兵数'], y['主将'], y['势力'], y['阵型'], y['兵数'], dist,
            '是' if touch else '否', loss, '是' if tw else '否', '是' if uw else '否'))
    for rule, s in sorted(stat.items()):
        print('%s：命令 %d 条，3 天内贴上 %d，目标 5 天掉兵平均 %d，目标 15 天内溃灭 %d（%d%%），我方 15 天内溃灭 %d' % (
            rule, s[0], s[1], s[2] // s[0], s[3], 100 * s[3] // s[0], s[4]))
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
