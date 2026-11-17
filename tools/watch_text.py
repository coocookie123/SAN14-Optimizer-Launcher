"""Find a UTF-16 text in game memory and catch the code that reads it, using CPU hardware watchpoints.

No game code is rewritten: Frida sets debug registers (DR0-DR3) on every thread and a vectored
exception handler logs the instruction and the call stack of each read.

Usage:
  python -P tools/watch_text.py scan <text> [<text> ...]          # just list where the text lives
  python -P tools/watch_text.py watch <label> <text> [<text> ...] # arm up to 4 watchpoints, log hits
      until analysis/stop.flag appears; writes analysis/watch_<label>.json
  python -P tools/watch_text.py watchaddr <label> <size 1|2|4|8> <hexaddr> [...]  # same, explicit addresses
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ANALYSIS, attach, find_game  # noqa: E402

JS = r"""
const m = Process.mainModule;
const hits = [];
let total = 0;
const MAX_RECORD = 400;

function where(p) {
  const mod = Process.findModuleByAddress(p);
  if (mod === null) return p.toString();
  return mod.name + '+0x' + p.sub(mod.base).toString(16);
}

Process.setExceptionHandler(details => {
  if (details.type !== 'single-step') return false;
  total++;
  if (hits.length < MAX_RECORD) {
    let bt = [];
    try { bt = Thread.backtrace(details.context, Backtracer.ACCURATE).slice(0, 40).map(where); } catch (e) { bt = ['bt-error ' + e]; }
    hits.push({tid: Process.getCurrentThreadId(), pc: where(details.context.pc), addr: where(details.address), bt: bt, t: Date.now()});
  }
  return true;
});

rpc.exports = {
  scan(pattern) {
    const out = [];
    for (const r of Process.enumerateRanges({protection: 'rw-', coalesce: true})) {
      try {
        for (const h of Memory.scanSync(r.base, r.size, pattern)) out.push(h.address.toString());
      } catch (e) {}
    }
    for (const r of Process.enumerateRanges({protection: 'r--', coalesce: true})) {
      try {
        for (const h of Memory.scanSync(r.base, r.size, pattern)) out.push(h.address.toString() + ' (ro)');
      } catch (e) {}
    }
    return out;
  },
  context(addr, before, after) {
    const p = ptr(addr);
    return [p.sub(before).readByteArray(before + after)];
  },
  arm(addrs, size) {
    let n = 0, fail = 0;
    for (const t of Process.enumerateThreads()) {
      try {
        addrs.forEach((a, i) => t.setHardwareWatchpoint(i, ptr(a), size, 'r'));
        n++;
      } catch (e) { fail++; }
    }
    return {armed: n, failed: fail};
  },
  disarm(count) {
    let n = 0;
    for (const t of Process.enumerateThreads()) {
      for (let i = 0; i < count; i++) { try { t.unsetHardwareWatchpoint(i); } catch (e) {} }
      n++;
    }
    return n;
  },
  take() { return {total: total, hits: hits.splice(0)}; },
};
"""


def pattern(text):
    return ' '.join('%02x' % b for b in text.encode('utf-16le'))


def main():
    cmd = sys.argv[1]
    p = find_game()
    if not p:
        sys.exit('game process not found')
    sess = attach(p.pid)
    sc = sess.create_script(JS)
    sc.load()
    api = sc.exports_sync
    if cmd == 'scan':
        for text in sys.argv[2:]:
            found = api.scan(pattern(text))
            print('%s: %d' % (text, len(found)))
            for a in found[:20]:
                addr = int(a.split()[0], 16)
                raw = bytes(api.context(hex(addr), 64, 96)[0])
                s = raw.decode('utf-16le', 'replace').replace('\x00', '|').replace('\n', ' ')
                print('  ', a, repr(s[:80]))
        sess.detach()
        return

    label = sys.argv[2]
    if cmd == 'watchaddr':
        # watchaddr <label> <size> <hexaddr>...
        size = int(sys.argv[3])
        texts = []
        addrs = sys.argv[4:8]
    else:
        size = 2
        texts = sys.argv[3:]
        addrs = []
        for text in texts:
            for a in api.scan(pattern(text)):
                if '(ro)' not in a:
                    addrs.append(a)
        addrs = addrs[:4]
    print('watching', addrs, 'size', size, flush=True)
    flag = os.path.join(ANALYSIS, 'stop.flag')
    if os.path.exists(flag):
        os.remove(flag)
    print('armed', api.arm(addrs, size), flush=True)
    allhits, total = [], 0
    try:
        while not os.path.exists(flag):
            time.sleep(1)
            r = api.take()
            total = r['total']
            if r['hits']:
                allhits += r['hits']
                print('hits so far', len(allhits), 'total', total, flush=True)
    finally:
        print('disarmed', api.disarm(len(addrs)), flush=True)
        time.sleep(0.5)
        r = api.take()
        allhits += r['hits']
        total = r['total']
        sess.detach()
        if os.path.exists(flag):
            os.remove(flag)
    out = os.path.join(ANALYSIS, 'watch_%s.json' % label)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump({'label': label, 'texts': texts, 'addrs': addrs, 'total': total, 'hits': allhits}, f, ensure_ascii=False, indent=1)
    print('saved', out, 'recorded', len(allhits), 'total exceptions', total, flush=True)


if __name__ == '__main__':
    main()
