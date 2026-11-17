# usage: python -P ai/research/probe_log.py <dll log> <module.json> [module.json ...] [--from HH:MM:SS]
"""Sum the tree probes (DLL feature "probe": MiscLogNode(99, 0, $m.<id>.<name>) counted per day) from
logs/ai_dll.log, with the probe keys mapped back to the variable names used in the given modules.
--from: only log lines at or after this time of day (the log holds several games)."""
import collections, json, os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import btsrc

a = sys.argv[1:]
since = a[a.index('--from') + 1] if '--from' in a else None
a = [x for i, x in enumerate(a) if x != '--from' and (i == 0 or a[i - 1] != '--from')]
log, mods = a[0], a[1:]
names = {}
for p in mods:
    with open(p, encoding='utf-8') as f:
        text = json.dumps(json.load(f), ensure_ascii=False)
    for n in set(re.findall(r'\$(m\.[\w]+\.[\w]+)', text)):
        names['%08x' % btsrc.var_key(n)] = n
total, days = collections.Counter(), collections.Counter()
order = []
with open(log, encoding='utf-8', errors='replace') as f:
    for ln in f:
        m = re.match(r'(\d\d:\d\d:\d\d)\.\d+ (\d+-\d+-\d+) probes:(.*)', ln.strip())
        if not m or (since and m.group(1) < since):
            continue
        for k, v in re.findall(r'([0-9a-f]{8})=(\d+)', m.group(3)):
            if k not in order:
                order.append(k)
            total[k] += int(v)
            days[k] += 1
for k in order:
    print('%-28s %7d  on %d days' % (names.get(k, k), total[k], days[k]))
