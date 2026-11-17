"""Read the running game's data tables from outside the process (ReadProcessMemory only).

  python -P ai/gamedata.py survey        # list the object arrays under the data root, by RTTI class
  python -P ai/gamedata.py dump city 3   # hex/int view of one object (person/city/force/district)
  python -P ai/gamedata.py date

The data root G is a global pointer. Code reads it as `mov rax, [rip+X]` and indexes object
arrays off it, e.g. GetCity: `cmp ecx, 33h; jg; movsxd rcx, ecx; mov rax, [rax+rcx*8+0DD38h]`.
Arrays (1.0.2.58226; offsets found by scanning code for [G + index*8 + off] with a bound check):

  +0x158   persons    6001      CPersonData: +0x12 surname, +0x24 given name (UTF-16), +0x118 district,
                                +0x11e status (1 lord, 4 serving, 7 free), +0x120 loyalty,
                                +0x124..+0x128 base stats, +0x1a2..+0x1b8 hidden AI params (+0x1b2 Han court)
  +0xdd38  cities     52        CCityData: +0x12 name, +0x30 district, +0x34 money, +0x38 food,
                                +0x3c soldiers, +0x44 morale, +0x46 endurance, +0x2e governor,
                                +0xca recruiting officer, +0xcc training officer (person ids);
                                +0x94 / +0x98 are not the UI income or expense (large negatives, AI budget?)
  +0xdf30  forces     52        CForceData: +0x10 lord person id, +0x13+i friendship with force i,
                                +0x17a target force
  +0xe0d0  districts  52        CDistrictData: +0x10 force, +0x12 commander person id, +0x15 branch of tree 1
(checked live 2026-11-11 in a spectator game)
  +0x871e8 -> game status object; date at status +0x34: word year, byte month, byte day (checked live)
Other arrays are listed by `survey` with their class names.
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'tools'))
import patch as P  # noqa: E402
import sigs as S  # noqa: E402

# The data root pointer comes from GetCity's default path (sig "data_root" in sigs/sigs.json):
#   mov rax,[rip+X]; cmp ecx,33h; jg; movsxd rcx,ecx; mov rax,[rax+rcx*8+0DD38h]
# The manifest also checks the city array offset there (0xdd38, same as ARRAYS['city']).

ARRAYS = {  # name: (offset, count); class names checked with `survey`
    'person': (0x158, 6001),        # CPersonData
    'region': (0xdc20, 12),         # CRegionData (地区)
    'state': (0xdc80, 23),          # CStateData (州)
    'city': (0xdd38, 52),           # CCityData
    'gate': (0xded8, 11),           # CGateData
    'force': (0xdf30, 52),          # CForceData
    'district': (0xe0d0, 52),       # CDistrictData
    'hex': (0xe270, 48400),         # CHexData
    'area': (0x6caf0, 501),         # CAreaData
    'object': (0x6da98, 3001),      # CObjectData
    'facility': (0x73958, 31),      # CFacilityData
    'formation': (0x77428, 21),     # CFormationData
    'tactics': (0x774d0, 201),      # CTacticsData
    'item': (0x77b18, 202),         # CItemData
    'policy': (0x78d50, 121),       # CPolicyData
    'personality': (0x7dd18, 356),  # CPersonalityData
    'army': (0x7e838, 501),         # CArmyUnitData: +0x12 general id, +0x16 soldiers, +0x1a morale;
                                    # live while the general's whereabouts (+0x11c) == 61 + index
    'troops': (0x7f8d8, 501),       # CTroopsData
}
STATUS = 0x871e8
STATUS_DATE = 0x34    # status object: word year, byte month, byte day
ARMY_WHERE_BASE = 61  # person +0x11c: 1..51 cities, 52..61 gates, 61 + i army i
PERSON_SURNAME = 0x12  # UTF-16, up to the given name
PERSON_GIVEN = 0x24
CITY_NAME = 0x12
FORCE_LORD = 0x10      # word: person id of the lord


class GameData:
    def __init__(self, pid=None):
        self.pid, self.hp = P.open_game(pid)
        if not self.hp:
            raise SystemExit('game is not running (SAN14CE_SC.exe not found or not accessible)')
        self.base = P.main_module(self.hp)
        ta, tn = P.text_section(self.hp, self.base)
        text = P.rpm(self.hp, ta, tn)
        self.root_ptr = S.find('data_root', text, ta).target('root_ptr')
        self._cls = {}

    # raw reads
    def read(self, a, n):
        return P.rpm(self.hp, a, n)

    def q(self, a):
        b = self.read(a, 8)
        return struct.unpack('<Q', b)[0] if b and len(b) == 8 else 0

    def d(self, a):
        b = self.read(a, 4)
        return struct.unpack('<i', b)[0] if b and len(b) == 4 else 0

    def b(self, a):
        x = self.read(a, 1)
        return x[0] if x else 0

    @property
    def root(self):
        return self.q(self.root_ptr)

    def cls(self, obj):
        vt = self.q(obj)
        if not vt:
            return None
        if vt in self._cls:
            return self._cls[vt]
        name = None
        col = self.q(vt - 8)
        if col:
            td = self.d(col + 12)
            raw = self.read(self.base + (td & 0xFFFFFFFF) + 16, 120) or b''
            n = raw.split(b'\0')[0].decode('ascii', 'replace')
            if n.startswith('.?AV') and n.endswith('@@'):
                name = n[4:-2]
        self._cls[vt] = name
        return name

    def array(self, name):
        off, count = ARRAYS[name]
        raw = self.read(self.root + off, 8 * count) or b''
        return list(struct.unpack(f'<{len(raw) // 8}Q', raw))

    def obj(self, name, i):
        return self.array(name)[i]

    def date_raw(self):
        st = self.q(self.root + STATUS)
        return self.read(st + 0x34, 16) if st else None


def survey(g):
    print(f'data root pointer at {g.root_ptr - g.base:#x}, root {g.root:#x}')
    for name, (off, count) in ARRAYS.items():
        ptrs = g.array(name)
        nz = [p for p in ptrs if p]
        uniq = len(set(nz))
        classes = {}
        for p in set(nz[:2000]):
            c = g.cls(p)
            classes[c] = classes.get(c, 0) + 1
        top = sorted(classes.items(), key=lambda kv: -kv[1])[:3]
        print(f'  {name:10s} +{off:#07x} x{count:5d}: {len(nz)} set, {uniq} distinct, {top}')


def dump(g, name, i, size=0x200):
    p = g.obj(name, int(i))
    raw = g.read(p, size) or b''
    print(f'{name}[{i}] at {p:#x} class {g.cls(p)}')
    for o in range(0, len(raw), 16):
        chunk = raw[o:o + 16]
        ints = struct.unpack(f'<{len(chunk) // 4}i', chunk[:len(chunk) // 4 * 4])
        print(f'  +{o:04x}  {chunk.hex(" ")}   {ints}')


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'survey'
    g = GameData()
    if cmd == 'survey':
        survey(g)
    elif cmd == 'dump':
        dump(g, sys.argv[2], sys.argv[3], int(sys.argv[4], 0) if len(sys.argv) > 4 else 0x200)
    elif cmd == 'date':
        print(g.date_raw().hex(' '))
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
