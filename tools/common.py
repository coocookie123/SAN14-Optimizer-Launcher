"""Shared helpers: find the SAN14 game process, attach Frida, read PE info from disk."""
import os
import struct
import time

import frida

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANALYSIS = os.path.join(ROOT, 'analysis')
GAME_DIR = r'E:\Steam\steamapps\common\Romance_of_the_Three_Kingdoms_14'
# CE (传承版) executables; the launcher is excluded on purpose.
GAME_EXES = ['san14ce_sc.exe', 'san14ce.exe', 'san14ce_tc.exe', 'san14ce_ko.exe']


def find_game(dev=None):
    dev = dev or frida.get_local_device()
    for p in dev.enumerate_processes():
        if p.name.lower() in GAME_EXES:
            return p
    return None


def wait_game(timeout=None, poll=1.0):
    t0 = time.time()
    while True:
        p = find_game()
        if p:
            return p
        if timeout is not None and time.time() - t0 > timeout:
            return None
        time.sleep(poll)


def attach(pid):
    return frida.get_local_device().attach(pid)


def pe_sections(path):
    """Return (image_base, size_of_image, [(name, rva, vsize, raw_off, raw_size)]) from the file on disk."""
    with open(path, 'rb') as f:
        d = f.read(4096)
    pe = struct.unpack_from('<I', d, 0x3c)[0]
    nsec = struct.unpack_from('<H', d, pe + 6)[0]
    opt = pe + 24
    image_base = struct.unpack_from('<Q', d, opt + 24)[0]
    size_of_image = struct.unpack_from('<I', d, opt + 56)[0]
    so = opt + struct.unpack_from('<H', d, pe + 20)[0]
    secs = []
    for i in range(nsec):
        n, vs, va, rs, ro = struct.unpack_from('<8sIIII', d, so + 40 * i)
        secs.append((n.rstrip(b'\0').decode(), va, vs, ro, rs))
    return image_base, size_of_image, secs
