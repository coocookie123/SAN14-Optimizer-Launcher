"""Dump the decrypted in-memory image of the running game to analysis/dump_<exe mtime>_<file version>/image.bin.

image.bin is laid out by RVA (file offset == RVA); unreadable pages are zero. meta.json records the exe,
its file version and PE timestamp, the runtime base and the sections.
The directory name starts with the exe's mtime, so the newest game version sorts last (the analysis tools
take sorted(glob('analysis/dump_*'))[-1]); keep the dump of every version the mod supported, tools/sigfind.py
needs the old one to re-find the patterns in a new one (docs/开发笔记.md: 游戏更新后怎么适配).
Wait until the game shows its title screen (the code is decrypted then); the script refuses otherwise.
Usage: python -P tools/dump.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ANALYSIS, attach, find_game, pe_sections  # noqa: E402
import sigs as S  # noqa: E402

JS = r"""
const m = Process.mainModule;
rpc.exports = {
  info() { return {name: m.name, base: m.base.toString(), size: m.size, path: m.path}; },
  read(off, len) {
    try { return m.base.add(off).readByteArray(len); } catch (e) { return null; }
  },
};
"""

CHUNK = 0x10000


def main():
    p = find_game()
    if not p:
        sys.exit('game process not found')
    sess = attach(p.pid)
    sc = sess.create_script(JS)
    sc.load()
    api = sc.exports_sync
    info = api.info()
    path = info['path']
    image_base, size_of_image, secs = pe_sections(path)
    st = os.stat(path)

    # Is .text decrypted? Compare the first bytes in memory with the file on disk.
    text = next(s for s in secs if s[0] == '.text')
    with open(path, 'rb') as f:
        f.seek(text[3])
        disk = f.read(256)
    mem = api.read(text[1], 256)
    decrypted = mem is not None and bytes(mem) != disk
    if not decrypted:
        sess.detach()
        sys.exit('the game code is not decrypted yet (SteamStub): wait for the title screen and run again')

    img = bytearray(size_of_image)
    bad = 0
    for off in range(0, size_of_image, CHUNK):
        n = min(CHUNK, size_of_image - off)
        b = api.read(off, n)
        if b is None:
            # retry page by page so a single guard page does not blank 64 KB
            for po in range(off, off + n, 0x1000):
                pb = api.read(po, min(0x1000, off + n - po))
                if pb is None:
                    bad += 1
                else:
                    img[po:po + len(pb)] = pb
        else:
            img[off:off + n] = b
    view = S.Image(bytes(img), int(info['base'], 16))
    ver = view.file_version() or 'unknown'
    out_dir = os.path.join(ANALYSIS, 'dump_%d_%s' % (int(st.st_mtime), ver))
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, 'image.bin'), 'wb') as f:
        f.write(img)
    meta = {
        'exe': path, 'file_version': ver, 'pe_timestamp': '%#010x' % view.timestamp,
        'exe_size': st.st_size, 'exe_mtime': int(st.st_mtime),
        'runtime_base': info['base'], 'image_base': hex(image_base),
        'size_of_image': size_of_image, 'unreadable_pages': bad,
        'text_decrypted': decrypted,
        'sections': [dict(name=s[0], rva=hex(s[1]), vsize=hex(s[2])) for s in secs],
    }
    with open(os.path.join(out_dir, 'meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=1)
    print(json.dumps(meta, indent=1))
    sess.detach()


if __name__ == '__main__':
    main()
