"""Read SAN14CE game-state information from outside the process (ReadProcessMemory only).

The state-stack manager is found without hard-coded addresses: the spectator-mode check
(IsSpectator: find "CGameState", mode == 1; sig "spectator_check" in sigs/sigs.json) starts with a call
to the manager getter, whose body loads the manager with "lea rbx,[rip+X]" (sig "state_mgr").
"""
import struct

import patch as P
import sigs as S

SUBPHASE = 0x470          # CUserStrategyState / CReportDisplayState sub-state


def locate_mgr(hp, base):
    """Address of the state-stack manager: the spectator check calls its getter, which loads it."""
    ta, tn = P.text_section(hp, base)
    text = P.rpm(hp, ta, tn)
    getter = S.find('spectator_check', text, ta).target('mgr_getter')
    w = S.sig('state_mgr').where
    return S.scan('state_mgr', text, ta, getter - ta, getter - ta + w['range']).checked().target('mgr')


class Game:
    def __init__(self, pid, hp):
        self.pid, self.hp = pid, hp
        self.base = P.main_module(hp)
        self.mgr = locate_mgr(hp, self.base)
        self._names = {}
        names = [n for _, n in self.stack()]
        if not names or names[0] != 'CRootState':
            raise RuntimeError('state stack not recognised: %s' % names[:3])

    def q(self, a):
        b = P.rpm(self.hp, a, 8)
        return struct.unpack('<Q', b)[0] if b and len(b) == 8 else None

    def d(self, a):
        b = P.rpm(self.hp, a, 4)
        return struct.unpack('<I', b)[0] if b and len(b) == 4 else None

    def cls(self, obj):
        vt = self.q(obj)
        if vt in self._names:
            return self._names[vt]
        col = self.q(vt - 8)
        td = self.d(col + 12)
        raw = P.rpm(self.hp, self.base + td + 16, 120) or b''
        name = raw.split(b'\0')[0].decode('ascii', 'replace')
        if name.startswith('.?AV') and name.endswith('@@'):
            name = name[4:-2]
        self._names[vt] = name
        return name

    def stack(self):
        cnt = self.q(self.mgr + 0x10) or 0
        arr = self.q(self.mgr + 0x20)
        if not arr or not 0 < cnt < 64:
            return []
        out = []
        for i in range(cnt):
            s = self.q(arr + 8 * i)
            out.append((s, self.cls(s)))
        return out

    def find(self, name):
        for s, n in self.stack():
            if n == name:
                return s
        return None

    def subphase(self, obj):
        return self.d(obj + SUBPHASE)

    def set_subphase(self, obj, value):
        data = struct.pack('<I', value)
        got = P.ctypes.c_size_t()
        ok = P.k32.WriteProcessMemory(self.hp, obj + SUBPHASE, data, 4, P.ctypes.byref(got))
        return bool(ok) and got.value == 4


def open_game():
    pid, hp = P.open_game()
    if not hp:
        return None
    return Game(pid, hp)
