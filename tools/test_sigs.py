"""Offline tests for the signature manifest (sigs/sigs.json) and everything that reads it.

  python -P tools/test_sigs.py

  1. the manifest is well formed (features, where, fields, references to other sigs, versions);
  2. every sig is found in the archived image of every tested version, at the RVAs recorded in the manifest;
  3. the runtime code (patch.locate, area_bonus.locate, gamestate.locate_mgr, takeover.Takeover,
     ai/gamedata.GameData) runs against that image through a fake process and finds the same addresses;
  4. constants the Python code still hard-codes agree with the manifest's expected values;
  5. overlay/sigs_gen.h is what overlay/build.py generates from the manifest (the DLL's side;
     `python -P overlay/build.py --test` also runs the DLL's locator on the image and compares).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'ai'))
sys.path.insert(0, os.path.join(ROOT, 'overlay'))
import sigs as S  # noqa: E402
import sigcheck  # noqa: E402
import patch as P  # noqa: E402

FAILS = []


def check(cond, what):
    print('  %s %s' % ('ok  ' if cond else 'FAIL', what))
    if not cond:
        FAILS.append(what)


def test_manifest(m):
    print('manifest')
    refs = set(m.sigs)
    for name, s in m.sigs.items():
        check(s.kind in S.WHERE_KINDS, '%s: where %s' % (name, s.where))
        check(s.features and all(f in m.features for f in s.features), '%s: features %s known' % (name, s.features))
        if isinstance(s.optional, list):
            check(set(s.optional) <= set(s.features), '%s: optional features are its features' % name)
        for d in s.deps():
            check(d in refs, '%s: refers to sig %s' % (name, d))
        if s.kind == 'rtti':
            check(not s.variants, '%s: rtti sig has no pattern' % name)
            continue
        check(bool(s.variants), '%s: has a pattern' % name)
        for v in s.variants:
            check(all(b is not None for b in v.toks[:1]), '%s/%d: pattern starts with a fixed byte (the DLL needs it)'
                  % (name, v.index))
            check(all(ver in m.versions for ver in v.versions), '%s/%d: versions known' % (name, v.index))
            for f, spec in v.fields.items():
                ok = spec.get('kind') in S.FIELD_KINDS and (spec['kind'] != 'rel' or spec.get('len', 0) >= 5)
                check(ok, '%s/%d.%s: kind %s' % (name, v.index, f, spec.get('kind')))
        for a in s.anchors:
            check(len(a) == 1 and next(iter(a)) in ('vslot', 'func_of', 'string', 'calls', 'called_by', 'const', 'refs'),
                  '%s: anchor %s' % (name, a))


class FakeProcess:
    """ReadProcessMemory on an image laid out by RVA, mapped at its runtime base."""

    def __init__(self, img):
        self.img = img

    def rpm(self, hp, addr, n):
        o = addr - self.img.base
        if o < 0 or o + n > len(self.img.data):
            return None
        return bytes(self.img.data[o:o + n])


def test_runtime(ver, img, res):
    print('runtime code on %s (%s)' % (ver, img.label))
    fake = FakeProcess(img)
    saved = (P.rpm, P.main_module, P.open_game)
    P.rpm = fake.rpm
    P.main_module = lambda hp: img.base
    P.open_game = lambda pid=None: (0, 'fake')
    b = img.base
    try:
        info = P.locate('fake')
        check(info['ent'] - b == res['report_enter'].hit.at('site'), 'patch.locate: v4 site %x' % (info['ent'] - b))
        check(info['root'] - b == res['report_enter'].hit.at('root'), 'patch.locate: v5 site %x' % (info['root'] - b))
        check(info['wait'] - b == res['report_wait_update'].hit.at('wait_ms') and info['wait_cur'] == 2000,
              'patch.locate: report wait %x = %s ms' % (info['wait'] - b, info['wait_cur']))
        check(info['je0'] - b == res['report_update'].hit.at('je0'), 'patch.locate: old v3 site %x' % (info['je0'] - b))
        check(info['co'] - b == res['report_coroutine'].hit.at('site'), 'patch.locate: old v1/v2 site %x' % (info['co'] - b))
        check((P.v4_state(info), P.root_state(info), P.v3_state(info), info['co_state']) ==
              ('original', 'original', 'original', 'original'), 'patch: every site is the original code')
        check(info['root_new'] == res['report_enter'].hit.expect('root', 'patched'),
              'patch: the v5 jmp is the manifest\'s patched bytes')
        import area_bonus as AB
        a = AB.locate('fake')
        check(all(a[k] - b == res['area_bonus'].values[k] for k in AB.SLOTS),
              'area_bonus.locate: %s' % ', '.join('%s %x' % (k, a[k] - b) for k in AB.SLOTS))
        check(AB.read('fake', a) == {'player': 4, 'super': 16, 'ultimate': 24}, 'area_bonus: values 4 / 16 / 24 in the image')
        import gamestate as G
        mgr = G.locate_mgr('fake', b)
        check(mgr - b == res['state_mgr'].values['mgr'], 'gamestate.locate_mgr: %x' % (mgr - b))
        import takeover as T
        t = T.Takeover(0, 'fake', log=lambda s: None)
        check((t.root_ptr - b, t.status_off, t.flags_off, t.player_off, t.force_arr) ==
              (res['auto_check'].values['root_ptr'], 0x871e8, 0x16a8, 0x3a, 0xdf30),
              'takeover: data root %x, status +%x, flags +%x, player +%x, forces +%x' % (
                  t.root_ptr - b, t.status_off, t.flags_off, t.player_off, t.force_arr))
        check(t.hud_vt and t.hud_vt - b == res['hud_layout'].addr, 'takeover: top-left layout vtable %x' % (t.hud_vt - b))
        import gamedata as GD
        g = GD.GameData()
        check(g.root_ptr - b == res['data_root'].values['root_ptr'], 'gamedata: data root %x' % (g.root_ptr - b))
    except Exception as e:      # report and go on with the other tests
        import traceback
        traceback.print_exc()
        check(False, 'runtime code raised %r' % e)
    finally:
        P.rpm, P.main_module, P.open_game = saved


def test_constants(m):
    print('constants still in the code vs the manifest')
    import gamedata as GD
    import gamestate as G

    def exp(sig, f):
        return m.sig(sig).variants[0].fields[f]['expect']
    check(G.SUBPHASE == exp('us_update', 'stage_off'), 'gamestate.SUBPHASE == us_update.stage_off')
    check(GD.ARRAYS['city'][0] == exp('data_root', 'city_arr'), "gamedata.ARRAYS['city'] == data_root.city_arr")
    check(GD.ARRAYS['force'][0] == exp('player_force_getter', 'force_arr'),
          "gamedata.ARRAYS['force'] == player_force_getter.force_arr")
    check(GD.STATUS == exp('auto_check', 'status_off'), 'gamedata.STATUS == auto_check.status_off')
    check(P.WAIT_ORIG == exp('report_wait_update', 'wait_ms'), 'patch.WAIT_ORIG == report_wait_update.wait_ms')
    import takeover as T
    check(T.AUTO_BIT == 1 << exp('auto_check', 'auto_shift'), 'takeover.AUTO_BIT == 1 << auto_check.auto_shift')


def test_dll_header():
    print('DLL')
    import build
    check(build.header_current(), 'overlay/sigs_gen.h is up to date with the manifest')


def main():
    m = S.load()
    test_manifest(m)
    for ver in sorted(m.tested_versions()):
        d = sigcheck.dump_for_version(ver, m)
        if not d:
            print('no archived image for %s, skipped' % ver)
            continue
        img = S.Image.from_dump(d)
        print('image of %s' % ver)
        check(img.file_version() == ver, 'image version %s' % img.file_version())
        res = S.resolve(img, m)
        for name, r in res.items():
            rec = m.sig(name).rva.get(ver, {})
            same = r.ok and rec.get('hit') == '%x' % r.addr and all(
                rec.get(f) is None or rec[f] == '%x' % (r.hit.at(f) if r.hit and r.hit.spec(f)['kind'] in ('site', 'bytes')
                                                         else v)
                for f, v in r.values.items() if isinstance(v, int) and not (r.hit and r.hit.spec(f)['kind'] in S.NUMERIC))
            check(same, '%s found at the recorded RVA %s' % (name, rec.get('hit')) if r.ok else '%s: %s' % (name, r.why))
        test_runtime(ver, img, res)
    test_constants(m)
    test_dll_header()
    print()
    print('%s (%d failed)' % ('ALL OK' if not FAILS else 'FAILED', len(FAILS)))
    return 1 if FAILS else 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
