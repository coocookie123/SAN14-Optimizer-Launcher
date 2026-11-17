"""陷阱近身触发（DLL 功能 trapnear）的实测汇总：每个陷阱发动时，最近的他国部队离它几格。

  python -P ai/research/trap_check.py <ai_dll.log> [<另一份 ai_dll.log> ...]

日志来自 SAN14_TRAP_LOG=1（autotest.py / gamectl.start 的 --trap-log，或 ai/launch.py --trap-log）：DLL 挂住设施效果
函数 191d60，陷阱每发动一次记一行，记的是游戏判定那一刻的部队位置：

  12:00:00.000 206-1-5 trap: 陷阱 (kind 14, force 3) at hex 12345 went off (发动范围 1, original 2, effect applied);
  nearest army of another force 1 away; within 3 (distance:force): 1:5 2:5 3:7

「他国」只按势力不同算，没区分是否交战，所以最近的那支可能是同盟；看的是分布：原版（发动范围 2）里陷阱、猛火陷阱、
落石应该有不少「最近 2 格」，开了 trapnear 后应该全在 1 格内；火陷阱、火船本来就是 1 格，两边都应该在 1 格内，
可以当对照（说明日志的位置和游戏判定时一致）。
"""
import re
import sys
from collections import Counter, defaultdict

LINE = re.compile(r'(\d+)-(\d+)-(\d+) trap: (\S+) \(kind (\d+), force (\d+)\) at hex (\d+) went off \(发动范围 (\d+), '
                  r'original (\d+), effect (\w+)\); nearest army of another force (-?\d+) away; within 3 '
                  r'\(distance:force\):(.*)$')


def read(path):
    out = []
    with open(path, encoding='utf-8-sig', errors='replace') as f:
        for line in f:
            m = LINE.search(line)
            if m:
                g = m.groups()
                out.append(dict(date='%s-%s-%s' % g[:3], name=g[3], kind=int(g[4]), force=int(g[5]), hex=int(g[6]),
                                act=int(g[7]), orig=int(g[8]), effect=g[9], nearest=int(g[10]), around=g[11].strip()))
    return out


def report(path, rows):
    print('== %s: %d 次陷阱发动' % (path, len(rows)))
    if not rows:
        return
    print('   日期 %s 到 %s' % (rows[0]['date'], rows[-1]['date']))
    by = defaultdict(list)
    for r in rows:
        by[(r['name'], r['orig'], r['act'])].append(r)
    print('   %-8s %-12s %5s   最近他国部队距离（格: 次数）' % ('种类', '发动范围', '次数'))
    for (name, orig, act), rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        c = Counter(r['nearest'] for r in rs)
        dist = '  '.join('%s: %d' % ('无' if d < 0 else d, c[d]) for d in sorted(c))
        far = sum(n for d, n in c.items() if d >= 2 or d < 0)
        print('   %-8s %-12s %5d   %s%s' % (name, '%d（原版 %d）' % (act, orig) if act != orig else str(act), len(rs),
                                           dist, '   ← 2 格及以外 %d 次' % far if far else ''))
    applied = Counter(r['effect'] for r in rows)
    print('   效果：%s' % ', '.join('%s %d' % kv for kv in applied.items()))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for p in sys.argv[1:]:
        report(p, read(p))


if __name__ == '__main__':
    main()
