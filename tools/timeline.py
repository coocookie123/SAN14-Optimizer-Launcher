"""Turn a stacks_<label>.json recording into readable timelines.

Usage:
  python -P tools/timeline.py analysis/stacks_X.json threads          # which threads are busy in the exe
  python -P tools/timeline.py analysis/stacks_X.json segs <tid> [depth]  # collapse consecutive samples with
                                                                        # the same outer exe frames
  python -P tools/timeline.py analysis/stacks_X.json frames <tid> [min] [max]
        # every exe frame seen on this thread with the time spans it was on the stack
"""
import json
import sys


def exe_frames(sample, main):
    # outermost first
    return [int(x.split('+')[1], 16) for x in reversed(sample['f']) if x.startswith(main + '+')]


def spans(times, gap=1.6):
    out = []
    for t in sorted(times):
        if out and t - out[-1][1] <= gap:
            out[-1][1] = t
        else:
            out.append([t, t])
    return out


def main():
    d = json.load(open(sys.argv[1], encoding='utf-8'))
    main_name = d['main']
    cmd = sys.argv[2]
    if cmd == 'threads':
        for tid, st in d['threads'].items():
            sets = [tuple(exe_frames(s, main_name)) for s in st]
            uniq = len(set(sets))
            depth = max(len(x) for x in sets)
            print('tid %-6s samples %4d  distinct-exe-stacks %4d  max-exe-depth %3d  t=%s..%s' % (
                tid, len(st), uniq, depth, st[0]['t'], st[-1]['t']))
        return
    if cmd == 'win':
        # frames seen in window A (a0..a1) but never in window B (b0..b1), all threads
        a0, a1, b0, b1 = map(float, sys.argv[3:7])
        for tid, st in d['threads'].items():
            fa, fb = {}, set()
            for s in st:
                fr = exe_frames(s, main_name)
                if a0 <= s['t'] <= a1:
                    for i, r in enumerate(fr):
                        fa.setdefault(r, [0, i, []])
                        fa[r][0] += 1
                        fa[r][2].append(s['t'])
                if b0 <= s['t'] <= b1:
                    fb.update(fr)
            only = {r: v for r, v in fa.items() if r not in fb}
            if not only:
                continue
            print('== tid', tid)
            for r, (n, depth, ts) in sorted(only.items(), key=lambda kv: (kv[1][1], -kv[1][0])):
                print('  depth %2d  rva %7x  n=%-3d %s' % (depth, r, n,
                      ' '.join('%.1f-%.1f' % (x, y) for x, y in spans(ts)[:6])))
        return
    tid = sys.argv[3]
    st = d['threads'][tid]
    if cmd == 'segs':
        depth = int(sys.argv[4]) if len(sys.argv) > 4 else 8
        segs = []
        for s in st:
            key = tuple(exe_frames(s, main_name)[:depth])
            if segs and segs[-1][2] == key:
                segs[-1][1] = s['t']
                segs[-1][3] += 1
            else:
                segs.append([s['t'], s['t'], key, 1])
        for a, b, key, n in segs:
            print('%7.1f-%7.1f n=%-4d %s' % (a, b, n, ' > '.join('%x' % r for r in key)))
    elif cmd == 'frames':
        lo = int(sys.argv[4]) if len(sys.argv) > 4 else 1
        hi = int(sys.argv[5]) if len(sys.argv) > 5 else 10 ** 9
        seen = {}
        for s in st:
            for r in set(exe_frames(s, main_name)):
                seen.setdefault(r, []).append(s['t'])
        rows = []
        for r, ts in seen.items():
            if lo <= len(ts) <= hi:
                rows.append((min(ts), r, len(ts), spans(ts)))
        rows.sort()
        for first, r, n, sp in rows:
            print('rva %7x  n=%-4d %s' % (r, n, ' '.join('%.1f-%.1f' % (a, b) for a, b in sp[:8])))


if __name__ == '__main__':
    main()
