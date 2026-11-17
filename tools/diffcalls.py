"""Compare two call recordings: functions called in A but never in B (and the reverse).

Usage: python -P tools/diffcalls.py analysis/calls_A.json analysis/calls_B.json [top]
Threads are merged; counts are per recording.
"""
import json
import sys


def load(path):
    with open(path, encoding='utf-8') as f:
        d = json.load(f)
    tot = {}
    for per in d['calls'].values():
        for r, c in per.items():
            tot[int(r, 16)] = tot.get(int(r, 16), 0) + c
    return d, tot


def main():
    da, a = load(sys.argv[1])
    db, b = load(sys.argv[2])
    top = int(sys.argv[3]) if len(sys.argv) > 3 else 80
    print('A=%s %ss %d funcs | B=%s %ss %d funcs' % (da['label'], da['seconds'], len(a), db['label'], db['seconds'], len(b)))
    only_a = sorted(((c, r) for r, c in a.items() if r not in b), reverse=True)
    only_b = sorted(((c, r) for r, c in b.items() if r not in a), reverse=True)
    print('\n== only in A (%d)' % len(only_a))
    for c, r in only_a[:top]:
        print('  rva 0x%07x  calls %d' % (r, c))
    print('\n== only in B (%d)' % len(only_b))
    for c, r in only_b[:top]:
        print('  rva 0x%07x  calls %d' % (r, c))


if __name__ == '__main__':
    main()
