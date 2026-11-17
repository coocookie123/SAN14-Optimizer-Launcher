"""Record which functions inside the game exe get called, per thread, until a stop flag appears.

Uses Frida Stalker call summaries; calls into other modules (d3d, ntdll, ...) are excluded.
Output: analysis/calls_<label>.json  {"label", "tids", "seconds", "calls": {tid: {rva_hex: count}}}

Usage: python -P tools/callsum.py <label> <tid>[,<tid>...]
Stop:  create analysis/stop.flag (the script deletes it when done).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ANALYSIS, attach, find_game  # noqa: E402

JS = r"""
const m = Process.mainModule;
const lo = m.base, hi = m.base.add(m.size);
let followed = [];
let totals = {};
rpc.exports = {
  start(tids) {
    for (const mod of Process.enumerateModules()) {
      if (mod.base.equals(m.base)) continue;
      try { Stalker.exclude({base: mod.base, size: mod.size}); } catch (e) {}
    }
    for (const tid of tids) {
      const t = {};
      totals[tid] = t;
      Stalker.follow(tid, {
        events: {call: true},
        onCallSummary(summary) {
          for (const k in summary) {
            const p = ptr(k);
            if (p.compare(lo) >= 0 && p.compare(hi) < 0) {
              const r = p.sub(lo).toUInt32();
              t[r] = (t[r] || 0) + summary[k];
            }
          }
        },
      });
      followed.push(tid);
    }
    return followed;
  },
  stop() {
    for (const tid of followed) Stalker.unfollow(tid);
    Stalker.flush();
    followed = [];
    return true;
  },
  take() {
    const out = totals;
    totals = {};
    for (const tid in out) totals[tid] = {};
    return out;
  },
};
"""


def merge(acc, part):
    for tid, d in part.items():
        a = acc.setdefault(str(tid), {})
        for r, c in d.items():
            k = '%x' % int(r)
            a[k] = a.get(k, 0) + c


def main():
    label = sys.argv[1]
    tids = [int(x) for x in sys.argv[2].split(',')]
    flag = os.path.join(ANALYSIS, 'stop.flag')
    if os.path.exists(flag):
        os.remove(flag)
    p = find_game()
    if not p:
        sys.exit('game process not found')
    sess = attach(p.pid)
    sc = sess.create_script(JS)
    sc.load()
    api = sc.exports_sync
    acc = {}
    t0 = time.time()
    print('following', api.start(tids), flush=True)
    try:
        while not os.path.exists(flag):
            time.sleep(2)
            merge(acc, api.take())
    finally:
        api.stop()
        time.sleep(1.0)
        merge(acc, api.take())
        sess.detach()
        if os.path.exists(flag):
            os.remove(flag)
    secs = time.time() - t0
    out = os.path.join(ANALYSIS, 'calls_%s.json' % label)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump({'label': label, 'tids': tids, 'seconds': round(secs, 1), 'calls': acc}, f)
    print('saved', out, 'seconds', round(secs, 1),
          {tid: len(d) for tid, d in acc.items()}, flush=True)


if __name__ == '__main__':
    main()
