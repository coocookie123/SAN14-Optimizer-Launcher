"""攻城出兵停在哪：读 DLL 的配兵日志（功能 siegecomp 或 scomplog 记的「siegecomp: person …」行），按势力奇偶分组。

  python -P ai/research/scomp_stop.py <ai_dll.log>

同一天、同一势力连续的配兵行算一次出兵（多座出兵城、多名武将）。每行有：配给这名武将的兵、配之前出兵城还能出的兵
（已扣留守线）、还缺的战力（编成上下文 +0x84，凑够就不再挑人）。看每次出兵最后一行：还缺的战力 ≤ 0 是凑兵目标
够了；否则多半是城里的兵用到留守线（还能出的兵不到 3000，派不出）或武将用完了。
"""
import collections
import re
import sys

PAT = re.compile(r'(\d+)-(\d+)-(\d+) siegecomp: person (\d+) \(force (\d+)(, not changed)?, 统率 (\d+), in city (\d+), '
                 r'strong there (\d+)\) (\d+) -> (\d+) soldiers; city can spare (-?\d+), strength still wanted (-?\d+)')


def main():
    rows = []
    for ln in open(sys.argv[1], encoding='utf-8', errors='replace'):
        m = PAT.search(ln)
        if m:
            g = m.groups()
            rows.append(dict(day=g[:3], person=int(g[3]), force=int(g[4]), city=int(g[7]), got=int(g[10]),
                             spare=int(g[11]), need=int(g[12])))
    deps = []
    cur = None
    for r in rows:
        key = (r['day'], r['force'])
        if cur is None or cur['key'] != key:
            cur = dict(key=key, rows=[])
            deps.append(cur)
        cur['rows'].append(r)
    for grp in (1, 0):
        ds = [d for d in deps if d['key'][1] % 2 == grp]
        if not ds:
            continue
        n = len(ds)
        # the same batch may be priced twice (a dry run first, see BEHAVIOR_PLAN 33): keep each person once
        sold, gens, cities, reason = [], [], [], collections.Counter()
        spare_last = []
        for d in ds:
            seen = {}
            for r in d['rows']:
                seen[r['person']] = r
            rs = list(seen.values())
            sold.append(sum(r['got'] for r in rs))
            gens.append(len(rs))
            cities.append(len({r['city'] for r in rs}))
            last = d['rows'][-1]
            left = last['spare'] - last['got']
            spare_last.append(left)
            if last['need'] - 0 <= 0:
                reason['凑兵目标已够'] += 1
            elif left < 3000:
                reason['城里能出的兵用完（<3000）'] += 1
            else:
                reason['还有兵、还缺战力（武将用完？）'] += 1
        print('%s：出兵 %d 次，每次 %.1f 名武将、%.2f 座城、%d 兵；最后一名配完后出兵城还能出的兵中位 %d；停下原因：%s' % (
            '奇数' if grp else '偶数', n, sum(gens) / n, sum(cities) / n, sum(sold) / n,
            sorted(spare_last)[n // 2], dict(reason.most_common())))


if __name__ == '__main__':
    main()
