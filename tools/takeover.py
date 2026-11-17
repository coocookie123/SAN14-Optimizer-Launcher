"""看海时按 Esc 接管势力（只改运行时内存，见 ai/TAKEOVER.md）。

游戏的「看海」= 选势力开局后点系统菜单「自动操作」。之后原版无法收回。这里在后台线程里：
  - 不停读状态对象 S = [数据根 + 0x871e8]：S+0x3a 是担当势力（byte，0 为无），
    S+0x16a8 的 0x100 位是「自动操作」。担当势力非 0 时记下来，作为开局势力。
  - 游戏窗口在前台、自动操作开着、按下 Esc 时：写回 S+0x3a、清掉 0x100 位。下一旬游戏自己进入玩家回合。
    接管后左上角的君主头像不刷新，存档再读档就好。Esc 照常也会打开系统菜单。
  - 不知道开局势力（例如读的是自动操作中存的档）时，接管左上角头像的势力：
    左上角是 CMainTopLayoutOffset 界面对象，+0x188 是军团（CDistrictData），军团 +0x10 是势力。
    这个对象在堆上：按 RTTI 找到虚表，在后台扫一次内存（约 20 秒）找到它，之后缓存。

地址全部用特征码找：自动操作判断函数
  mov rax,[rip+G]; mov rcx,[rax+871e8h]; mov eax,[rcx+16a8h]; shr eax,8; and eax,1; ret
同时给出数据根的位置、状态对象偏移和标志偏移；担当势力偏移从取玩家势力的函数里核对。

  python tools/takeover.py            # 单独运行（游戏开着），Ctrl+C 结束
"""
import ctypes
import struct
import threading
import time

import patch as P
import sigs as S

# patterns: sigs "auto_check", "player_force_getter" and "hud_layout" in sigs/sigs.json
AUTO_BIT = 0x100
VK_ESCAPE = 0x1B
FORCE_COUNT = 52
HUD_CLASS = S.sig('hud_layout').where['rtti'].encode('ascii')
HUD_DISTRICT = 0x188
DISTRICT_FORCE = 0x10


class MBI(ctypes.Structure):
    _fields_ = [('BaseAddress', ctypes.c_void_p), ('AllocationBase', ctypes.c_void_p),
                ('AllocationProtect', ctypes.c_ulong), ('pad', ctypes.c_ulong), ('RegionSize', ctypes.c_size_t),
                ('State', ctypes.c_ulong), ('Protect', ctypes.c_ulong), ('Type', ctypes.c_ulong),
                ('pad2', ctypes.c_ulong)]


k32 = ctypes.WinDLL('kernel32', use_last_error=True)
u32 = ctypes.WinDLL('user32', use_last_error=True)
k32.VirtualQueryEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.WriteProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_size_t)]
u32.GetForegroundWindow.restype = ctypes.c_void_p
u32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
u32.GetAsyncKeyState.restype = ctypes.c_short
u32.MessageBeep.argtypes = [ctypes.c_uint]


class Takeover:
    def __init__(self, pid, hp, log=print):
        self.pid, self.hp, self.log = pid, hp, log
        self.base = P.main_module(hp)
        ta, tn = P.text_section(hp, self.base)
        text = P.rpm(hp, ta, tn)
        h = S.find('auto_check', text, ta)
        self.root_ptr = h.target('root_ptr')
        self.status_off = h.value('status_off')
        self.flags_off = h.value('flags_off')
        h2 = S.find('player_force_getter', text, ta)       # checks player_off 0x3a, force_arr 0xdf30
        self.player_off = h2.value('player_off')
        self.force_arr = h2.value('force_arr')
        if h.check('status_off') or h.check('flags_off'):
            self.log('takeover: offsets differ from 1.0.2.58226: status %#x flags %#x (getter at %#x)' % (
                self.status_off, self.flags_off, h2.addr))
        self.own = 0            # the force picked at game start (last non-zero player force seen)
        self.stop = False
        self._esc_down = False
        self.hud_vt = self._find_vtable(HUD_CLASS)
        self.hud = 0            # CMainTopLayoutOffset instance, found by a heap scan
        self._scanning = False
        if not self.hud_vt:
            self.log('takeover: top-left layout class not found; only the starting force can be taken back')

    def _q(self, a):
        b = P.rpm(self.hp, a, 8)
        return struct.unpack('<Q', b)[0] if b and len(b) == 8 else 0

    def _find_vtable(self, name):
        """Vtable address of an MSVC class with RTTI, searched in the module image."""
        hdr = P.rpm(self.hp, self.base, 0x400)
        size = struct.unpack_from('<I', hdr, struct.unpack_from('<I', hdr, 0x3c)[0] + 0x50)[0]
        img = bytearray()
        for off in range(0, size, 0x100000):
            n = min(0x100000, size - off)
            img += P.rpm(self.hp, self.base + off, n) or bytes(n)
        i = img.find(name)
        if i < 16:
            return 0
        td = struct.pack('<I', i - 16)
        j = img.find(td)
        while j >= 0:
            col = j - 12
            if col >= 0 and struct.unpack_from('<I', img, col)[0] == 1:
                k = img.find(struct.pack('<Q', self.base + col))
                if k >= 0:
                    return self.base + k + 8
            j = img.find(td, j + 1)
        return 0

    def _scan_hud(self):
        """Find the top-left layout object on the heap (private read-write memory)."""
        t0, pat, found = time.time(), struct.pack('<Q', self.hud_vt), 0
        a, m = 0, MBI()
        while not found and k32.VirtualQueryEx(self.hp, a, ctypes.byref(m), ctypes.sizeof(m)):
            base, sz = m.BaseAddress or 0, m.RegionSize
            if m.State == 0x1000 and m.Protect == 4 and m.Type == 0x20000 and sz < 0x40000000:
                for off in range(0, sz, 0x1000000):
                    b = P.rpm(self.hp, base + off, min(0x1000000, sz - off)) or b''
                    i = b.find(pat)
                    while i >= 0 and i % 8:
                        i = b.find(pat, i + 1)
                    if i >= 0:
                        found = base + off + i
                        break
            a = base + sz
        self.hud = found
        self._scanning = False
        self.log('takeover: top-left layout %s (%.1f s)' % ('at %#x' % found if found else 'not found',
                                                             time.time() - t0))

    def _hud_force(self):
        """Force shown at the top left, 0 if unknown (starts a background search when needed)."""
        if not self.hud or self._q(self.hud) != self.hud_vt:
            self.hud = 0
            if self.hud_vt and not self._scanning:
                self._scanning = True
                threading.Thread(target=self._scan_hud, daemon=True).start()
            return 0
        d = self._q(self.hud + HUD_DISTRICT)
        f = P.rpm(self.hp, d + DISTRICT_FORCE, 1) if d else None
        return f[0] if f and 0 < f[0] < FORCE_COUNT else 0

    def _status(self):
        root = self._q(self.root_ptr)
        return self._q(root + self.status_off) if root else 0, root

    def _write(self, a, data):
        n = ctypes.c_size_t()
        return k32.WriteProcessMemory(self.hp, a, data, len(data), ctypes.byref(n)) and n.value == len(data)

    def _force_alive(self, root, fid):
        f = self._q(root + self.force_arr + fid * 8)
        if not f:
            return False
        lord = P.rpm(self.hp, f + 0x10, 2)
        return bool(lord) and 0 < struct.unpack('<h', lord)[0] < 6001

    def _game_foreground(self):
        h = u32.GetForegroundWindow()
        if not h:
            return False
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        return pid.value == self.pid

    def poll(self):
        """One step: remember the player's force, or take a force back on Esc."""
        st, root = self._status()
        if not st:
            return                          # title screen / loading
        raw = P.rpm(self.hp, st + self.player_off, 1)
        fl = P.rpm(self.hp, st + self.flags_off, 4)
        if not raw or not fl:
            return
        fid, flags = raw[0], struct.unpack('<I', fl)[0]
        if not flags & AUTO_BIT:
            if 0 < fid < FORCE_COUNT and fid != self.own:
                self.log('takeover: player force is %d' % fid)
                self.own = fid
            return
        if not self.own and not self.hud:
            self._hud_force()               # look for the top-left layout in the background
        down = bool(u32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)
        pressed, self._esc_down = down and not self._esc_down, down
        if not pressed or not self._game_foreground():
            return
        target, why = self.own, 'starting force'
        if not target or not self._force_alive(root, target):
            target, why = self._hud_force(), 'top-left force'
        if not target or not self._force_alive(root, target):
            self.log('takeover: Esc in auto mode, but no force to take (starting %d, top-left %s)' % (
                self.own, 'still searching' if self._scanning else target))
            u32.MessageBeep(0x10)
            return
        ok = self._write(st + self.player_off, bytes([target])) and \
            self._write(st + self.flags_off, struct.pack('<I', flags & ~AUTO_BIT))
        self.log('takeover: Esc -> force %d (%s) goes to the player from the next xun: %s' % (
            target, why, 'ok' if ok else 'FAILED'))
        if ok:
            u32.MessageBeep(0x40)

    def run(self):
        while not self.stop and P.find_pid() == self.pid:
            try:
                self.poll()
            except Exception as e:
                self.log('takeover: poll error %r' % e)
                time.sleep(2)
            time.sleep(0.03)

    def start(self):
        t = threading.Thread(target=self.run, daemon=True)
        t.start()
        return t


if __name__ == '__main__':
    pid, hp = P.open_game()
    if not hp:
        raise SystemExit('game is not running')
    t = Takeover(pid, hp)
    print('ready: root ptr %#x, status +%#x, flags +%#x, top-left vtable %#x' % (
        t.root_ptr, t.status_off, t.flags_off, t.hud_vt), flush=True)
    try:
        t.run()
    except KeyboardInterrupt:
        pass
