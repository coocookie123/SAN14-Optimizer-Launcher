"""Build the launcher: icon (PIL) -> resources (llvm-windres) -> exe (llvm-mingw, static runtime).

Usage: python -P launcher/build.py   -> launcher/build/W_San14Launcher.exe
"""
import os
import re
import subprocess
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
LLVM = os.environ.get('SAN14_LLVM') or r'E:\Tools\extracted\llvm-mingw\llvm-mingw-20261020-ucrt-x86_64\bin'
OUT_DIR = os.path.join(HERE, 'build')
EXE = os.path.join(OUT_DIR, 'W_San14Launcher.exe')


def make_icon(path):
    """launcher.ico from icon.png (1024 x 1024 RGBA): crimson tile, gold 三, ivory seal with 14.

    The art was drawn with Qwen-Image 2512 (Apache-2.0, run locally) and cut out with a SAM3 mask, 2026-11-15
    (prompt and steps in docs/开发笔记.md, 「图标」)."""
    img = Image.open(os.path.join(HERE, 'icon.png')).convert('RGBA')
    sizes = [256, 64, 48, 32, 24, 16]
    frames = [img.resize((z, z), Image.LANCZOS) for z in sizes]
    frames[0].save(path, sizes=[(z, z) for z in sizes], append_images=frames[1:])
    frames[0].save(os.path.join(OUT_DIR, 'icon_preview.png'))


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', cwd=HERE)
    sys.stdout.write(r.stdout + r.stderr)
    if r.returncode:
        sys.exit('failed: %s' % cmd[0])


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    make_icon(os.path.join(HERE, 'launcher.ico'))
    res = os.path.join(OUT_DIR, 'launcher.res.o')
    ver = re.search(r"^VERSION = '([^']+)'", open(os.path.join(HERE, '..', 'tools', 'report_button.py'),
                    encoding='utf-8').read(), re.M).group(1)
    nums = ','.join((ver.split('.') + ['0'] * 4)[:4])
    with open(os.path.join(OUT_DIR, 'version.h'), 'w', encoding='utf-8') as f:
        f.write('#define VER_NUM %s\n#define VER_STR "%s"\n' % (nums, ver))
    # --codepage=65001: without it llvm-windres ignores the .rc's code_page pragma and the Chinese strings come out garbled
    run([os.path.join(LLVM, 'x86_64-w64-mingw32-windres.exe'), '--codepage=65001', 'launcher.rc', '-O', 'coff', '-o', res])
    run([os.path.join(LLVM, 'x86_64-w64-mingw32-clang++.exe'), '-O2', '-municode', '-mwindows', '-static', '-s',
         '-Wall', '-o', EXE, 'launcher.cpp', res, '-lcomctl32', '-lshell32', '-luser32', '-lgdi32', '-lole32', '-luuid', '-ladvapi32'])
    deps = subprocess.run([os.path.join(LLVM, 'llvm-objdump.exe'), '-p', EXE], capture_output=True, text=True).stdout
    print('built', EXE, os.path.getsize(EXE), 'bytes')
    print('imports:', ', '.join(l.split(':', 1)[1].strip() for l in deps.splitlines() if 'DLL Name' in l))


if __name__ == '__main__':
    main()
