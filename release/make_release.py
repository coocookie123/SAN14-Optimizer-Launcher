"""Assemble the release folder and zip:  dist/[W]三国志14优化启动器/  and  dist/[W]三国志14优化启动器_v<ver>.zip

Inputs (already downloaded and verified, see README):
  E:\\Tools\\extracted\\python-3.13.16-embed-amd64   (python.org embeddable package, PSF-signed)
  E:\\Tools\\extracted\\pillow-12.3.0-cp313-win_amd64  (Pillow wheel, sha256 checked against PyPI)
Usage: python -P release/make_release.py
"""
import os
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = '[W]三国志14优化启动器'
OLD_NAMES = ['[W]三国优化启动器']   # before 0.7.0: that folder is renamed once, so its settings and mods stay
PY_EMBED = os.environ.get('SAN14_PY_EMBED') or r'E:\Tools\extracted\python-3.13.16-embed-amd64'
PILLOW = os.environ.get('SAN14_PILLOW') or r'E:\Tools\extracted\pillow-12.3.0-cp313-win_amd64'
RUNTIME_SCRIPTS = ['report_button.py', 'patch.py', 'gamestate.py', 'ingame.py', 'area_bonus.py', 'takeover.py',
                   'sigs.py', 'sigcheck.py']
# the signature manifest the scripts read (tools/sigs.py looks for ../sigs/sigs.json next to tools/)
SIGS_FILES = ['sigs.json']
# computer-AI modules: tree builder + early-injection start (ai/launch.py), data it needs, and the modules
# offered by the launcher's 电脑 AI checkboxes (keep in sync with AI_MODULES in launcher/launcher.cpp)
AI_FILES = ['launch.py', 'btpatch.py', 'btdump.py', 'btsrc.py', 'btfile.py', 'conditions.py', 'node_types.json',
            'info_names.json']
AI_MODULES = ['deploy', 'rally', 'engage', 'retreat', 'stance', 'walls', 'siegeplan', 'gate']
KEEP = ['settings.ini']           # the user's launcher settings survive a rebuild
# other people's mods the user keeps inside their own W folder (e.g. 第三方Mod\人力资源管理器\dinput8.dll, closed source,
# all rights reserved): kept across rebuilds and NEVER put into the zip
KEEP_DIRS = ['第三方Mod']


def in_use(folder):
    """A file of a running copy (launcher, its Python, a DLL loaded in the game) cannot be replaced."""
    for rel in (NAME + '.exe', r'runtime\pythonw.exe', r'runtime\python.exe', r'app\bin\san14_overlay.dll',
                r'app\bin\san14_ai.dll'):
        path = os.path.join(folder, rel)
        if os.path.exists(path):
            try:
                open(path, 'r+b').close()
            except PermissionError:
                return rel
    return None


def version():
    src = open(os.path.join(ROOT, 'tools', 'report_button.py'), encoding='utf-8').read()
    return re.search(r"^VERSION = '([^']+)'", src, re.M).group(1)


def run(args):
    r = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if r.returncode:
        sys.stdout.write(r.stdout + r.stderr)
        sys.exit('failed: %s' % ' '.join(args))
    return r.stdout


def main():
    ver = version()
    dist_root = os.path.join(ROOT, 'dist')
    out = os.path.join(dist_root, NAME)
    print('version', ver)
    # 1. build the native parts
    run([sys.executable, '-P', os.path.join(ROOT, 'overlay', 'build.py')])
    run([sys.executable, '-P', os.path.join(ROOT, 'launcher', 'build.py')])
    run([sys.executable, '-P', os.path.join(ROOT, 'aihook', 'build.py')])
    # 2. fresh folder (keeping the user's settings and the other mods they keep in it)
    for old in OLD_NAMES:
        old_dir = os.path.join(dist_root, old)
        if os.path.isdir(old_dir) and not os.path.exists(out):
            os.rename(old_dir, out)       # fails while a file in it is in use
            print('renamed', old_dir, '->', out)
    kept = {}
    kept_dirs = os.path.join(dist_root, '_kept_dirs')
    if os.path.exists(out):
        busy = in_use(out)
        if busy:
            sys.exit('%s is in use (launcher, helper or game still running): close them first' % busy)
        for k in KEEP:
            if os.path.exists(os.path.join(out, k)):
                kept[k] = open(os.path.join(out, k), 'rb').read()
        if os.path.exists(kept_dirs):
            shutil.rmtree(kept_dirs)
        for d in KEEP_DIRS:
            if os.path.isdir(os.path.join(out, d)):
                shutil.copytree(os.path.join(out, d), os.path.join(kept_dirs, d))
        shutil.rmtree(out)
    os.makedirs(out)
    for k, data in kept.items():
        with open(os.path.join(out, k), 'wb') as f:
            f.write(data)
    for d in KEEP_DIRS:
        if os.path.isdir(os.path.join(kept_dirs, d)):
            shutil.copytree(os.path.join(kept_dirs, d), os.path.join(out, d))
    if os.path.exists(kept_dirs):
        shutil.rmtree(kept_dirs)
    shutil.copy2(os.path.join(ROOT, 'launcher', 'build', 'W_San14Launcher.exe'), os.path.join(out, NAME + '.exe'))
    # runtime: embeddable Python + Pillow in Lib/site-packages
    rt = os.path.join(out, 'runtime')
    shutil.copytree(PY_EMBED, rt)
    sp = os.path.join(rt, 'Lib', 'site-packages')
    os.makedirs(sp)
    shutil.copytree(os.path.join(PILLOW, 'PIL'), os.path.join(sp, 'PIL'))
    shutil.copytree(os.path.join(PILLOW, 'pillow-12.3.0.dist-info'), os.path.join(sp, 'pillow-12.3.0.dist-info'))
    pth = os.path.join(rt, 'python313._pth')
    with open(pth, 'w', encoding='utf-8') as f:
        f.write('python313.zip\n.\nLib\\site-packages\n\n# site is not imported: the app adds its own folder to sys.path\n#import site\n')
    # app
    tools = os.path.join(out, 'app', 'tools')
    os.makedirs(tools)
    for s in RUNTIME_SCRIPTS:
        shutil.copy2(os.path.join(ROOT, 'tools', s), tools)
    os.makedirs(os.path.join(out, 'app', 'sigs'))
    for s in SIGS_FILES:
        shutil.copy2(os.path.join(ROOT, 'sigs', s), os.path.join(out, 'app', 'sigs'))
    os.makedirs(os.path.join(out, 'app', 'bin'))
    shutil.copy2(os.path.join(ROOT, 'bin', 'san14_overlay.dll'), os.path.join(out, 'app', 'bin'))
    shutil.copy2(os.path.join(ROOT, 'bin', 'san14_ai.dll'), os.path.join(out, 'app', 'bin'))
    ai = os.path.join(out, 'app', 'ai')
    os.makedirs(os.path.join(ai, 'modules'))
    for s in AI_FILES:
        shutil.copy2(os.path.join(ROOT, 'ai', s), ai)
    for m in AI_MODULES:
        shutil.copy2(os.path.join(ROOT, 'ai', 'modules', m + '.json'), os.path.join(ai, 'modules'))
    # docs and licenses
    guide = open(os.path.join(ROOT, 'release', '使用说明.txt'), encoding='utf-8').read().replace('{version}', ver)
    with open(os.path.join(out, '使用说明.txt'), 'w', encoding='utf-8-sig', newline='\r\n') as f:
        f.write(guide)
    lic = os.path.join(out, 'LICENSES')
    os.makedirs(lic)
    shutil.copy2(os.path.join(PY_EMBED, 'LICENSE.txt'), os.path.join(lic, 'Python-LICENSE.txt'))
    shutil.copy2(os.path.join(PILLOW, 'pillow-12.3.0.dist-info', 'licenses', 'LICENSE'), os.path.join(lic, 'Pillow-LICENSE.txt'))

    # 3. smoke test with the bundled interpreter: imports and one button render
    test = ("import sys; sys.path.insert(0, r'%s'); import report_button as R, ingame, gamestate, patch, takeover, PIL, mmap, ctypes; "
            "d, w, h = R.button_bgra(1.0, False, False, False); print('smoke ok', PIL.__version__, w, h, len(d))") % tools
    print(run([os.path.join(rt, 'python.exe'), '-P', '-c', test]).strip())
    # the manifest the package carries loads, and finds every sig in the archived image of each tested version
    # (the images stay in the repository's analysis/, the package has none)
    test = ("import sys, os; sys.path.insert(0, r'%s'); import sigs, sigcheck; m = sigs.load(); "
            "sigcheck.ROOT = r'%s'; sigcheck.ANALYSIS = os.path.join(sigcheck.ROOT, 'analysis'); "
            "ds = {v: sigcheck.dump_for_version(v, m) for v in sorted(m.tested_versions())}; "
            "bad = [(v, n) for v, d in ds.items() if d for n, r in sigs.resolve(sigs.Image.from_dump(d), m).items() if not r.ok]; "
            "print('sigs ok: %%d sigs, checked on %%s' %% (len(m.sigs), ','.join(v for v, d in ds.items() if d) or 'no image') "
            "if not bad else 'SIGS FAIL %%s' %% bad)") % (tools, ROOT)
    out_sigs = run([os.path.join(rt, 'python.exe'), '-P', '-c', test]).strip()
    print(out_sigs)
    if not out_sigs.startswith('sigs ok'):
        sys.exit('the signature manifest does not match the archived images')
    # the AI modules build against the installed game's tree (check only: nothing written)
    test = ("import sys, os; sys.path.insert(0, r'%s'); sys.argv = ['x']; import report_button as R; L = R.ai_launch(); "
            "d = L.steam_game_dir(); src = os.path.join(d, '0000', 'bt_exce.bin'); "
            "mods = [os.path.join(R.AI_DIR, 'modules', m + '.json') for m in %r]; "
            "print('ai smoke', L.btpatch.build(mods, check_only=True, source=src) if os.path.exists(src) else 'no game here')"
            ) % (tools, AI_MODULES)
    print(run([os.path.join(rt, 'python.exe'), '-P', '-c', test]).strip())

    # 4. zip
    zpath = os.path.join(dist_root, '%s_v%s.zip' % (NAME, ver))
    if os.path.exists(zpath):
        os.remove(zpath)
    files = 0
    with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for base, _, names in os.walk(out):
            parts = os.path.relpath(base, out).split(os.sep)
            if '__pycache__' in parts or 'logs' in parts or 'build' in parts or parts[0] in KEEP_DIRS:
                continue                # logs (also logs\san14HR\Logs), and other people's mods: never in the zip
            for n in names:
                if n.endswith('.pyc') or n in KEEP:
                    continue
                p = os.path.join(base, n)
                z.write(p, os.path.relpath(p, dist_root))
                files += 1
    with zipfile.ZipFile(zpath) as z:                # other people's files must not be in what gets shared
        bad = [n for n in z.namelist() if any(k in n for k in KEEP_DIRS) or 'san14hr' in n.lower() or
               n.lower().endswith('dinput8.dll')]
    if bad:
        os.remove(zpath)
        sys.exit('zip would contain other mods\' files, removed: %s' % bad[:5])
    size = sum(os.path.getsize(os.path.join(b, n)) for b, _, ns in os.walk(out) for n in ns)
    print('folder', out, '%.1f MB' % (size / 1e6))
    print('zip', zpath, '%.1f MB, %d files' % (os.path.getsize(zpath) / 1e6, files))


if __name__ == '__main__':
    main()
