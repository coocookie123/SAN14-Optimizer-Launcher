"""Build bin/san14_ai.dll with llvm-mingw (same toolchain as overlay/build.py).

The DLL's byte patterns, field offsets and RTTI names come from sigs/sigs.json: every build first writes
aihook/sigs_gen.h from the manifest (the sigs whose "users" list aihook/san14_ai.cpp), with the same
generator as the overlay DLL.

  python -P aihook/build.py                    # write sigs_gen.h, build the DLL
  python -P aihook/build.py --test [dump_dir]  # also run the DLL's locator offline against a memory dump
                                               # (tools/dump.py; default: newest in analysis/) and compare
                                               # what it finds with tools/sigs.py
"""
import glob
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LLVM = os.environ.get('SAN14_LLVM') or r'E:\Tools\extracted\llvm-mingw\llvm-mingw-20261020-ucrt-x86_64\bin'
SRC = os.path.join(ROOT, 'aihook', 'san14_ai.cpp')
HEADER = os.path.join(ROOT, 'aihook', 'sigs_gen.h')
OUT = os.path.join(ROOT, 'bin', 'san14_ai.dll')
TEST_SRC = os.path.join(ROOT, 'aihook', 'locate_test.cpp')
TEST_OUT = os.path.join(tempfile.gettempdir(), 'san14_ai_locate_test.exe')
CXX = [os.path.join(LLVM, 'x86_64-w64-mingw32-clang++.exe'), '-O2', '-std=c++17', '-static', '-s', '-Wall',
       '-Wno-unused-function', '-Wno-unused-const-variable']
USER = 'aihook/san14_ai.cpp'


def overlay_build():
    spec = importlib.util.spec_from_file_location('overlay_build', os.path.join(ROOT, 'overlay', 'build.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_header():
    text = overlay_build().gen_header(user=USER, by='aihook/build.py')
    old = open(HEADER, encoding='utf-8').read() if os.path.exists(HEADER) else None
    if old != text:
        with open(HEADER, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        print('wrote', HEADER)


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(r.stdout + r.stderr)
    return r.returncode


def test(dump_dir):
    if not dump_dir:
        dumps = sorted(glob.glob(os.path.join(ROOT, 'analysis', 'dump_*')), key=os.path.getmtime)
        if not dumps:
            sys.exit('no dump in analysis/ (make one with tools/dump.py)')
        dump_dir = dumps[-1]
    with open(os.path.join(dump_dir, 'meta.json'), encoding='utf-8') as f:
        base = json.load(f)['runtime_base']
    if run(CXX + ['-o', TEST_OUT, TEST_SRC]):
        sys.exit('test build failed')
    print('locate test on', dump_dir)
    r = subprocess.run([TEST_OUT, os.path.join(dump_dir, 'image.bin'), base], capture_output=True, text=True)
    sys.stdout.write(r.stdout + r.stderr)
    if r.returncode:
        sys.exit('locate test FAILED')
    lines = [l for l in r.stdout.splitlines() if l.startswith('sig ')]
    bad = overlay_build().compare_with_manifest(lines, dump_dir)
    if bad or not lines:
        sys.exit('locate test: %d of %d values differ from tools/sigs.py' % (bad, len(lines)))
    print('locate test ok: all %d values the DLL found agree with tools/sigs.py' % len(lines))


def main():
    write_header()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    if run(CXX + ['-municode', '-shared', '-o', OUT, SRC]):
        sys.exit('build failed')
    deps = subprocess.run([os.path.join(LLVM, 'llvm-objdump.exe'), '-p', OUT], capture_output=True, text=True).stdout
    print('built', OUT, os.path.getsize(OUT), 'bytes')
    print('imports:', ', '.join(l.split(':', 1)[1].strip() for l in deps.splitlines() if 'DLL Name' in l))
    if '--test' in sys.argv:
        i = sys.argv.index('--test')
        test(sys.argv[i + 1] if len(sys.argv) > i + 1 else None)


if __name__ == '__main__':
    main()
