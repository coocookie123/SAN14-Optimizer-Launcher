"""围城军势的粮道：测试局运行时在旁边只读内存，每 10 个游戏日看每个正在攻城的军势（本队离目标城 8 格内）：
从本队所在格沿本势力涂色的格子往外找，能不能走到本势力的府所在区域（区域 +0x78 是自己）；走到要几格；
以及路上最窄处有几格宽（按离本队的步数分层，取「路上」那几层里最少的格数）。

  python -P ai/research/supply_sample.py [--minutes 9] > out.txt

格子编号：行 × 220 + 列；相邻格＝游戏距离为 1 的格（observe.hex_distance）。
"""
import collections
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gamedata  # noqa: E402
import observe as o  # noqa: E402

W = 220
SIEGE = {2, 8, 9, 10, 22}


_NB = {}


def neighbours(h):
    """The six hexes at the game's distance 1 (observe.hex_distance), cached."""
    if h not in _NB:
        r, c = divmod(h, W)
        _NB[h] = [rr * W + cc for rr in (r - 1, r, r + 1) for cc in (c - 1, c, c + 1)
                  if 0 <= rr < W and 0 <= cc < W and (rr, cc) != (r, c) and o.hex_distance(h, rr * W + cc) == 1]
    return _NB[h]


def main():
    minutes = float(sys.argv[sys.argv.index('--minutes') + 1]) if '--minutes' in sys.argv else 9
    g = gamedata.GameData()
    game = o.Game(g)
    H = g.array('hex')
    t0 = time.time()
    last = None
    while time.time() - t0 < minutes * 60:
        try:
            d = game.date()
        except Exception:
            break
        if d is None or d == last or d[2] not in (5, 15, 25):
            time.sleep(0.3)
            continue
        last = d
        raw = g.read(H[0], 32 * len(H))
        if not raw:
            break
        area = [struct.unpack_from('<H', raw, i * 32 + 0x12)[0] for i in range(len(H))]
        paint = [raw[i * 32 + 0x14] for i in range(len(H))]
        st = game.area_state()
        cities, districts, forces = game.light()
        armies = game.armies(districts)
        fh = {f[1]: f[3] for f in game.footholds() if f[0] == '城市'}
        seen = set()
        for i, a in armies.items():
            if a['purpose'] not in SIEGE or not a['lead'] or a['troops'] in seen:
                continue
            seen.add(a['troops'])
            goal = fh.get(a['target'])
            if goal is None or o.hex_distance(a['hex'], goal) > 8:
                continue
            f = a['force']
            start = a['hex']
            dist = {start: 0}
            q = collections.deque([start])
            found = None
            while q:
                h = q.popleft()
                ar = area[h]
                if ar in st and st[ar][0] == f and dist[h] > 0:
                    found = h
                    break
                if dist[h] > 60:
                    continue
                for nb in neighbours(h):
                    if nb not in dist and paint[nb] == f:
                        dist[nb] = dist[h] + 1
                        q.append(nb)
            if found is None:
                print('%s troops %d force %d: NOT connected to own territory (reach %d hexes)' % (d, a['troops'], f, len(dist)), flush=True)
                continue
            L = dist[found]
            layers = collections.Counter(v for v in dist.values() if 0 < v < L)
            width = min(layers.values()) if layers else 0
            print('%s troops %d force %d: connected in %d hexes, narrowest layer %d hexes' % (d, a['troops'], f, L, width), flush=True)


if __name__ == '__main__':
    main()
