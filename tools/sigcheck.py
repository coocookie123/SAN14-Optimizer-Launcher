"""Check every signature in sigs/sigs.json against the running game or a dumped image, per feature.

  python -P tools/sigcheck.py                    # the running game (else the newest dump in analysis/)
  python -P tools/sigcheck.py --dump analysis/dump_1793841123
  python -P tools/sigcheck.py --version 1.0.2.58226   # the archived dump of that version
  python -P tools/sigcheck.py --live             # only the running game (also checks game data)
  options: -v (every sig and field), --json (machine-readable), --record (write the RVAs of this
           version into sigs.json and add the version to the matching variants)

Output: per feature 可用 / 不可用 with the reasons, then (running game only) a plausibility check of the
game data through the offsets in ai/gamedata.py: a few person, city and lord names must be readable
text and the date must make sense. Exit code 0 when every runtime feature is usable.

check_features(hp) is the same check for tools/report_button.py: it reads the game's image once and
returns {feature: (ok, reasons)}.
"""
import argparse
import glob
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import sigs as S  # noqa: E402

ANALYSIS = os.path.join(ROOT, 'analysis')


# ---- images ----------------------------------------------------------------------------------------

def live_image(hp):
    """Image of the game's main module read with ReadProcessMemory (unreadable pages stay zero)."""
    import patch as P
    base = P.main_module(hp)
    hdr = P.rpm(hp, base, 0x1000)
    if not hdr:
        raise S.SigError('cannot read the game module')
    pe = struct.unpack_from('<I', hdr, 0x3c)[0]
    size = struct.unpack_from('<I', hdr, pe + 24 + 56)[0]
    data = bytearray(size)
    chunk = 0x100000
    for off in range(0, size, chunk):
        n = min(chunk, size - off)
        b = P.rpm(hp, base + off, n)
        if b and len(b) == n:
            data[off:off + n] = b
            continue
        for po in range(off, off + n, 0x1000):
            pb = P.rpm(hp, base + po, min(0x1000, off + n - po))
            if pb:
                data[po:po + len(pb)] = pb
    img = S.Image(bytes(data), base, 'running game')
    img.meta = {}
    return img


def dumps():
    """[(dir, meta)] of analysis/dump_*, oldest exe first."""
    out = []
    for d in glob.glob(os.path.join(ANALYSIS, 'dump_*')):
        try:
            with open(os.path.join(d, 'meta.json'), encoding='utf-8') as f:
                out.append((d, json.load(f)))
        except (OSError, ValueError):
            pass
    return sorted(out, key=lambda x: x[1].get('exe_mtime', 0))


def dump_for_version(ver, manifest=None):
    m = manifest or S.load()
    rel = m.versions.get(ver, {}).get('dump')
    if rel and os.path.isdir(os.path.join(ROOT, rel)):
        return os.path.join(ROOT, rel)
    for d, meta in dumps():
        if meta.get('file_version') == ver:
            return d
    for d, meta in dumps():           # older dumps have no file_version in meta.json
        if S.Image.from_dump(d).file_version() == ver:
            return d
    return None


def open_image(args):
    """(Image, live game handle or None)."""
    if args.dump:
        return S.Image.from_dump(args.dump), None
    if args.version:
        d = dump_for_version(args.version)
        if not d:
            sys.exit('no dump of version %s in analysis/ (make one with tools/dump.py while that version runs)' % args.version)
        return S.Image.from_dump(d), None
    import patch as P
    pid, hp = P.open_game()
    if hp:
        img = live_image(hp)
        img.label = 'running game (pid %d)' % pid
        return img, hp
    if args.live:
        sys.exit('the game is not running')
    ds = dumps()
    if not ds:
        sys.exit('the game is not running and there is no dump in analysis/')
    print('游戏没在运行，检查最新的镜像 %s' % os.path.relpath(ds[-1][0], ROOT))
    return S.Image.from_dump(ds[-1][0]), None


def version_of(img):
    return img.file_version() or '?'


def code_decrypted(img):
    """SteamStub: until the game has started, .text in memory is still encrypted. Runs of int3 padding
    between functions only show up once it is decrypted."""
    a, n = img.sec('.text')
    return img.data[a:a + 0x4000].count(b'\xcc\xcc\xcc\xcc') >= 8


# ---- game data plausibility (running game only) ----------------------------------------------------

def _cjk(s):
    return bool(s) and all('㐀' <= c <= '鿿' or '豈' <= c <= '﫿' for c in s)


def data_checks(img, hp, results):
    """[(ok, text)] about the game's data, read through the data root and ai/gamedata.py's offsets."""
    import patch as P
    try:
        sys.path.insert(0, os.path.join(ROOT, 'ai'))
        import gamedata as GD
    except ImportError:
        return [(None, '没有 ai/gamedata.py，跳过数据检查')]
    r = results.get('data_root')
    if not r or not r.ok:
        return [(False, '找不到数据根（data_root），无法检查游戏数据')]
    out = []

    def q(a):
        b = P.rpm(hp, a, 8)
        return struct.unpack('<Q', b)[0] if b and len(b) == 8 else 0

    def wstr(a, n):
        b = P.rpm(hp, a, n) or b''
        return b.decode('utf-16le', 'replace').split('\0')[0]

    root = q(img.base + r.values['root_ptr'])
    if not root:
        return [(None, '数据根是空的（游戏还在标题画面之前？），跳过数据检查')]

    def array(name):
        off, count = GD.ARRAYS[name]
        raw = P.rpm(hp, root + off, 8 * count) or b''
        return list(struct.unpack('<%dQ' % (len(raw) // 8), raw))

    def person_name(p):
        return wstr(p + GD.PERSON_SURNAME, GD.PERSON_GIVEN - GD.PERSON_SURNAME) + wstr(p + GD.PERSON_GIVEN, 0x12)

    persons = array('person')
    named = [person_name(p) for p in persons[:2000] if p]
    good = [n for n in named if _cjk(n) and 2 <= len(n) <= 6]
    out.append((len(named) > 100 and len(good) > 0.9 * len(named),
                '武将：前 2000 个里有 %d 个对象，%d 个名字是正常的中文，例如 %s' % (
                    len(named), len(good), '、'.join(good[:6]) or '（无）')))
    cities = [wstr(c + GD.CITY_NAME, 0x12) for c in array('city') if c]
    cgood = [c for c in cities if _cjk(c) and 1 <= len(c) <= 4]
    out.append((len(cities) >= 40 and len(cgood) == len(cities),
                '城市：%d 个，%d 个名字正常，例如 %s' % (len(cities), len(cgood), '、'.join(cities[:8]))))
    lords = []
    for f in array('force'):
        if not f:
            continue
        lb = P.rpm(hp, f + GD.FORCE_LORD, 2)
        lid = struct.unpack('<h', lb)[0] if lb else -1
        if 0 <= lid < len(persons) and persons[lid]:
            lords.append(person_name(persons[lid]))
    lgood = [n for n in lords if _cjk(n)]
    out.append((bool(lords) and len(lgood) == len(lords),
                '势力君主：%d 个，例如 %s' % (len(lords), '、'.join(lords[:8]) or '（无）')))
    st = q(root + GD.STATUS)
    raw = P.rpm(hp, st + GD.STATUS_DATE, 4) if st else None
    if raw:
        y, mo, d = struct.unpack('<HBB', raw)
        out.append((150 <= y <= 300 and 1 <= mo <= 12 and 1 <= d <= 31, '日期：%d 年 %d 月 %d 日' % (y, mo, d)))
    else:
        out.append((False, '读不到游戏状态对象（数据根 +%#x）' % GD.STATUS))
    return out


# ---- per-feature check for the launcher --------------------------------------------------------------

def check_features(hp, manifest=None):
    """{feature: (ok, [reasons])} for the running game. Raises SigError if the image cannot be read."""
    img = live_image(hp)
    return S.features(S.resolve(img, manifest), manifest)


# ---- --record ----------------------------------------------------------------------------------------

def record(img, results, manifest):
    ver = version_of(img)
    if ver == '?':
        sys.exit('the image has no version resource; cannot record')
    vmeta = manifest.d.setdefault('versions', {}).setdefault(ver, {})
    vmeta.setdefault('pe_timestamp', '%#010x' % img.timestamp)
    vmeta.setdefault('size_of_image', '%#x' % img.size_of_image)
    meta = getattr(img, 'meta', {}) or {}
    for k in ('exe_size', 'exe_mtime'):
        if k in meta:
            vmeta.setdefault(k, meta[k])
    if img.label and os.path.isdir(img.label):
        try:
            rel = os.path.relpath(img.label, ROOT)
        except ValueError:                 # another drive
            rel = img.label
        vmeta.setdefault('dump', rel.replace('\\', '/'))
    n = 0
    for name, r in results.items():
        if not r.ok:
            continue
        d = manifest.d['sigs'][name]
        entry = {'hit': '%x' % r.addr}
        for f, v in r.values.items():
            if r.hit is None:                       # rtti slots
                entry[f] = '%x' % v
            elif r.hit.spec(f)['kind'] == 'rel':
                entry[f] = '%x' % v
            elif r.hit.spec(f)['kind'] in ('site', 'bytes'):
                entry[f] = '%x' % r.hit.at(f)
        d.setdefault('rva', {})[ver] = entry
        if r.hit is not None:
            vd = d['variants'][r.hit.variant.index]
            if ver not in vd.setdefault('versions', []):
                vd['versions'].append(ver)
        n += 1
    manifest.save()
    print('已把版本 %s 的 %d 条记录写进 %s' % (ver, n, manifest.path))


# ---- output ------------------------------------------------------------------------------------------

def report(img, results, manifest, verbose, hp=None):
    ver = version_of(img)
    known = manifest.versions.get(ver)
    print('检查对象：%s，游戏版本 %s（%s），PE 时间戳 %08x' % (
        img.label, ver, '清单里有，已测试' if known and known.get('tested') else
        ('清单里有' if known else '清单里没有这个版本'), img.timestamp))
    if not code_decrypted(img):
        print('注意：代码段看起来还没解密（游戏还没到标题画面，或镜像是在那之前导出的），下面的结果不可信')
    feats = S.features(results, manifest)
    all_ok = True
    print()
    for fid, (ok, reasons) in feats.items():
        f = manifest.features[fid]
        tag = '可用' if ok else '不可用'
        if not ok and not f.get('offline'):
            all_ok = False
        print('[%s] %s' % (tag, f['name']))
        for why in reasons:
            print('      - %s' % why)
    if verbose:
        print()
        for name, r in results.items():
            s = r.sig
            if r.ok:
                var = ' 变体 %d' % r.hit.variant.index if r.hit and len(s.variants) > 1 else ''
                vals = ', '.join('%s=%s' % (k, ('%x' % v) if isinstance(v, int) else v.hex(' ') if isinstance(v, bytes) else v)
                                 for k, v in r.values.items())
                states = [f for f in (r.hit.variant.fields if r.hit else {}) if r.hit.state(f) == 'patched']
                print('  ok   %-20s %x%s  %s%s' % (name, r.addr, var, vals, ('  已打补丁: ' + ','.join(states)) if states else ''))
            else:
                print('  FAIL %-20s %s' % (name, r.why))
    checks = []
    if hp is not None:
        print()
        print('游戏数据：')
        checks = data_checks(img, hp, results)
        for ok, text in checks:
            print('  [%s] %s' % ({True: '正常', False: '异常', None: '跳过'}[ok], text))
            if ok is False:
                all_ok = False
    return all_ok, feats, checks


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--dump')
    ap.add_argument('--version')
    ap.add_argument('--live', action='store_true')
    ap.add_argument('--sigs', help='another manifest file (default sigs/sigs.json)')
    ap.add_argument('-v', '--verbose', action='store_true')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--record', action='store_true')
    args = ap.parse_args()
    manifest = S.Manifest(args.sigs) if args.sigs else S.load()
    img, hp = open_image(args)
    results = S.resolve(img, manifest)
    if args.json:
        out = {'version': version_of(img), 'image': img.label, 'sigs': {}, 'features': {}}
        for name, r in results.items():
            out['sigs'][name] = {'ok': r.ok, 'why': r.why, 'rva': '%x' % r.addr if r.ok else None,
                                 'variant': r.hit.variant.index if r.ok and r.hit else None,
                                 'values': {k: ('%x' % v if isinstance(v, int) else v.hex(' '))
                                            for k, v in r.values.items()} if r.ok else {}}
        for fid, (ok, reasons) in S.features(results, manifest).items():
            out['features'][fid] = {'ok': ok, 'reasons': reasons}
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    ok, _, _ = report(img, results, manifest, args.verbose, hp)
    if args.record:
        record(img, results, manifest)
    return 0 if ok else 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
