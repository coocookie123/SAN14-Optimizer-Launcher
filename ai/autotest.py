"""One automated AI test: start the game like the W launcher, load a save, switch auto mode on, run until a
date, stop the game, then check the modules against a baseline.

  python -P ai/autotest.py --until 198-12-21                       # 4 modules, save slot 2
  python -P ai/autotest.py --ai none --until 198-12-21             # official AI (baseline)
  python -P ai/autotest.py --ai rally,engage --baseline ai/runs/X  # compare against run X
  python -P ai/autotest.py --until 198-1-1 --months 1             # short run: only checks that the loop works
  python -P ai/autotest.py --ai deploy --ab odd                    # A/B: modules only for odd force ids
  python -P ai/autotest.py --ai deploy,experiments/defend --ab odd:defend   # A/B of one module (by id): the
                                                                   # others act for all forces
  python -P ai/autotest.py --ai stance --dev                       # helper, modules, DLLs from this checkout
  python -P ai/autotest.py --slot 4 --start 206-1-1 --until 206-12-21 --baseline ai/runs/X   # late-game save
                                                                   # (no release rebuild; logs in logs/)
  python -P ai/autotest.py --make-save --slot 3 --to 4 --until 206-1-1   # official AI, then save to slot 4
  python -P ai/autotest.py --ai none --trap-log [--trap-near] --dev ...  # 陷阱近身触发 A/B: every trap that goes
                                                                   # off is logged (ai/research/trap_check.py)

Steps (each logged to analysis/autotest.log, screenshots in analysis/shots/):
  1. start: gamectl.start (same command line as the launcher), wait for the window and the title state;
  2. load: skip the two opening movies and the title prompt with clicks, 载入, scroll the list to the top,
     click save slot 2, 是; wait until the date is START_DATE on the strategy map;
  3. auto: gear (top right) -> 自动操作 -> 是, like a player (writing the flag alone does not leave the player's
     command state); check that the date moves on;
  4. record: ai/observe.py runs alongside and writes ai/runs/<time>;
  5. stop at --until (or after --minutes), kill the game, run research/check_modules.py.
The user should not touch mouse and keyboard during step 2 (about half a minute).
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import gamectl as C  # noqa: E402

LOG = os.path.join(ROOT, 'analysis', 'autotest.log')
START_DATE = (197, 12, 1)            # save slot 2 (backup: analysis/saves/svdexceSC02_197-12_三模块结束.s14)
BASELINE = os.path.join(HERE, 'runs', '20261111-230308')
# Click positions in a 1920x1080 frame (found from screenshots 2026-11-12, 1.0.2.58226).
CENTER = (960, 540)
TITLE_LOAD = (474, 385)          # 载入
LIST_HOVER = (570, 700)          # over the save list, for the mouse wheel
SLOT_Y = {0: 205, 1: 320, 2: 435, 3: 552}   # rows when the list is scrolled to the top
SLOT_X = 567
YES = (873, 902)                 # 是 in the yes/no dialogs (load and auto mode)
GEAR = (1869, 36)                # 设定, top right of the strategy map
AUTO_ITEM = (960, 453)           # 自动操作 in the 设定 menu
SAVE_ITEM = (960, 213)           # 保存 in the 设定 menu
BACK = (960, 1017)               # 返回 at the bottom of the 设定 menu and the save list
LOAD_SCROLL = 100                # how far the load list moves up when a row at its bottom edge is clicked
SLOT_Y.update({4: 668, 5: 784})
# load list rows 6-7 after scrolling to the top and then 12 notches down (~47 px a notch; calibrated from
# analysis/shots/calib_load_down12.png, 2026-11-13). Load only: a wrong row loads a wrong save, which the date
# check stops; saving goes to 4-5 (rows that cannot reach the user's 0-3 however the list is scrolled).
SLOT_Y_DOWN12 = {6: 326, 7: 442}  # slots 4-5 when the list is scrolled to the top (row pitch ~116; 6-7 need scrolling)
MY_SLOTS = (4, 5, 6, 7)          # empty slots the user allowed for test saves (2026-11-12); 0-3 are theirs
USER_SLOTS = (0, 1, 2, 3)


def save_files():
    """The manual save files (svdexceSC<nn>.s14) in every Steam user's remote folder of the game."""
    import glob
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as k:
            root = winreg.QueryValueEx(k, 'SteamPath')[0]
    except OSError:
        root = r'C:\Program Files (x86)\Steam'
    out = {}
    for d in glob.glob(os.path.join(root, 'userdata', '*', '872410', 'remote')):
        for n in range(10):
            out[(d, n)] = os.path.join(d, 'svdexceSC%02d.s14' % n)
    return out


def digest(path):
    import hashlib
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def guard_user_saves():
    """Copy the user's slots 0-3 aside and return a check function: it compares them afterwards and puts any
    changed one back (the save dialog is driven by screen positions; this makes a wrong click harmless)."""
    import shutil
    keep = os.path.join(ROOT, 'analysis', 'saves', 'guard_%s' % time.strftime('%Y%m%d-%H%M%S'))
    os.makedirs(keep, exist_ok=True)
    before = {}
    for (d, n), path in save_files().items():
        if n in USER_SLOTS and os.path.exists(path):
            shutil.copy2(path, os.path.join(keep, '%s_%s' % (os.path.basename(os.path.dirname(os.path.dirname(d))),
                                                             os.path.basename(path))))
            before[path] = digest(path)

    def check():
        bad = [p for p, h in before.items() if digest(p) != h]
        for p in bad:
            src = os.path.join(keep, '%s_%s' % (os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(p)))),
                                                os.path.basename(p)))
            shutil.copy2(src, p)
            log('USER SAVE %s CHANGED: restored from %s' % (p, src))
        if not bad:
            log('user saves 0-3 unchanged (%d files checked, copies in %s)' % (len(before), keep))
        return not bad
    return check


def log(msg):
    line = time.strftime('%H:%M:%S ') + msg
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def wait(cond, timeout, step=1.0, what=''):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            v = cond()
            if v:
                return v
        except Exception:
            pass
        time.sleep(step)
    raise TimeoutError('timed out: ' + what)


def attach():
    return wait(lambda: C.game(), 120, 2, 'game process')


def top_states(g):
    try:
        return C.states(g)
    except Exception:
        return []


def top(g):
    st = top_states(g)
    return st[-1] if st else ''


def click(xy, settle=1.5):
    C.front(wait=0.3)
    C.click(*xy)
    time.sleep(settle)


def wheel_up(xy, notches=10):
    import ctypes
    h = C.window()
    cx, cy, cw, ch = C.client_rect(h)
    ctypes.windll.user32.SetCursorPos(cx + round(xy[0] * cw / C.REF_W), cy + round(xy[1] * ch / C.REF_H))
    for _ in range(notches):
        i = C.INPUT(type=0)
        i.u.mi = C.MOUSEINPUT(0, 0, 120, 0x0800, 0, 0)
        C._send(i)
        time.sleep(0.15)
    time.sleep(1)


def wheel_down(xy, notches):
    import ctypes
    h = C.window()
    cx, cy, cw, ch = C.client_rect(h)
    ctypes.windll.user32.SetCursorPos(cx + round(xy[0] * cw / C.REF_W), cy + round(xy[1] * ch / C.REF_H))
    for _ in range(notches):
        i = C.INPUT(type=0)
        i.u.mi = C.MOUSEINPUT(0, 0, -120 & 0xffffffff, 0x0800, 0, 0)
        C._send(i)
        time.sleep(0.15)
    time.sleep(1.2)


def load_slot(g, slot):
    """From the opening movies to the strategy map of save `slot`."""
    t0 = time.time()
    while time.time() - t0 < 120:            # movies and the 请点击鼠标 prompt: click until loading starts
        st = top(g)
        if st == 'CGameStartInitState':
            break
        if st in ('CCmnMovieState', 'CTitleState'):
            click(CENTER, 2.0)
        else:
            time.sleep(1)
    wait(lambda: top(g) == 'CTitleState', 120, 1, 'title menu')
    time.sleep(1.5)
    log('title menu')
    click(TITLE_LOAD)
    wait(lambda: top(g) == 'CSaveLoadState', 20, 0.5, 'save list')
    C.front(wait=0.3)
    # the list opens scrolled to the last save used (e.g. slot 5 at its bottom edge) and may scroll there a
    # moment after it appears: wait, then scroll up twice (2026-11-13: one scroll right away left slot 5 at
    # slot 4's place)
    time.sleep(1.5)
    wheel_up(LIST_HOVER)
    wheel_up(LIST_HOVER)
    if slot in SLOT_Y_DOWN12:
        wheel_down(LIST_HOVER, 12)
        click((SLOT_X, SLOT_Y_DOWN12[slot]))
        wait(lambda: 'YesNo' in top(g), 6, 0.5, 'load confirm')
        C.shot(os.path.join(C.SHOTS, 'autotest_load_confirm.png'))
        click(YES, 1.0)
        wait(lambda: top(g) == 'CUserStrategyState', 180, 2, 'strategy map')
        time.sleep(2)
        return
    click((SLOT_X, SLOT_Y[slot]))
    try:
        wait(lambda: 'YesNo' in top(g), 4, 0.5, 'load confirm')
    except TimeoutError:
        # the load list has tabs above it: a row at its bottom edge (slot 5) only scrolls into view on the
        # first click, the list moving up ~100 (2026-11-13); click it where it is now
        click((SLOT_X, SLOT_Y[slot] - LOAD_SCROLL))
        wait(lambda: 'YesNo' in top(g), 10, 0.5, 'load confirm')
    C.shot(os.path.join(C.SHOTS, 'autotest_load_confirm.png'))
    click(YES, 1.0)
    wait(lambda: top(g) == 'CUserStrategyState', 180, 2, 'strategy map')
    time.sleep(2)


def auto_mode(g):
    click(GEAR)
    wait(lambda: top(g) == 'CConfigDlgState', 10, 0.5, 'settings menu')
    click(AUTO_ITEM)
    wait(lambda: 'YesNo' in top(g), 10, 0.5, 'auto confirm')
    click(YES, 2.0)
    st = C.status(g)
    return bool(g.d(st + C.FLAGS_OFF) & C.AUTO_BIT)


def save_slot(g, slot):
    """设定 -> 保存 -> slot -> 是 (-> close the message) -> 返回. Only slots in MY_SLOTS."""
    if slot not in MY_SLOTS or slot not in SLOT_Y:
        raise ValueError('slot %s is not a test slot' % slot)
    shot = lambda name: C.shot(os.path.join(C.SHOTS, 'save_%s.png' % name))   # noqa: E731
    click(GEAR)
    wait(lambda: top(g) == 'CConfigDlgState', 15, 0.5, 'settings menu')
    shot('1_menu')
    click(SAVE_ITEM)
    wait(lambda: top(g) == 'CSaveLoadState', 15, 0.5, 'save list')
    C.front(wait=0.3)
    time.sleep(1.5)                       # the list scrolls to the last save used a moment after it opens
    wheel_up(LIST_HOVER)
    wheel_up(LIST_HOVER)
    shot('2_list')
    files = [p for (d, n), p in save_files().items() if n == slot]
    before = {p: os.path.getmtime(p) if os.path.exists(p) else 0 for p in files}
    written = lambda: any((os.path.getmtime(p) if os.path.exists(p) else 0) > t for p, t in before.items())  # noqa: E731
    click((SLOT_X, SLOT_Y[slot]))
    # an empty slot is saved at once; a used one asks to overwrite first; a "saved" message may follow
    t0 = time.time()
    while time.time() - t0 < 25 and not (written() and top(g) == 'CSaveLoadState'):
        if 'Dialog' in top(g):
            shot('3_dialog')
            click(YES if 'YesNo' in top(g) else CENTER, 1.5)
        else:
            time.sleep(0.5)
    shot('5_after')
    if not written():
        raise RuntimeError('slot %d was not written' % slot)
    log('saved to slot %d at %s' % (slot, C.date(g)))
    try:                                  # back to the map (not needed when the game is closed next)
        click(BACK)
        wait(lambda: top(g) == 'CConfigDlgState', 10, 0.5, 'back to settings')
        click(BACK)
    except TimeoutError as e:
        log('after saving: %s (%s)' % (e, top_states(g)[-2:]))


def make_save(a, opt):
    keep_display_on()
    """Load --slot (default 3), auto mode with --ai (default none = official AI), run to --until, save to --to."""
    ai = opt('--ai', 'none')
    ai = () if ai == 'none' else tuple(x for x in ai.split(',') if x)
    src, dst = int(opt('--slot', 3)), int(opt('--to', 4))
    until = tuple(int(x) for x in opt('--until', '206-1-1').split('-'))
    if dst not in MY_SLOTS:
        sys.exit('slot %d is not a test slot (allowed: %s)' % (dst, MY_SLOTS))
    log('make save: slot %d -> %d at %s, ai %s' % (src, dst, until, ','.join(ai) or 'none'))
    check = guard_user_saves()
    # 保存 is greyed out while auto mode runs; the helper's takeover (Esc) ends auto mode and gives the
    # player's strategy phase back at the next 旬, where saving works
    C.start(ai, (10, 10), takeover=True, dev='--dev' in a)
    wait(C.window, 120, 1, 'game window')
    g = attach()
    wait(lambda: any('Title' in s for s in top_states(g)), 180, 1, 'title screen')
    load_slot(g, src)
    log('loaded: %s' % (C.date(g),))
    log('auto on: %s' % auto_mode(g))
    t0 = time.time()
    while time.time() - t0 < float(opt('--minutes', 120)) * 60:
        d = game_date()
        if d is None:
            log('game exited early: no save made')
            return
        if d >= until:
            break
        time.sleep(5)
    log('reached %s after %.1f min' % (C.date(g), (time.time() - t0) / 60))
    try:
        C.front(wait=0.3)
        C.key('ESCAPE')                   # takeover: ends auto mode (the system menu may open as well)
        t1 = time.time()
        while time.time() - t1 < 240 and top(g) != 'CUserStrategyState':
            if top(g) == 'CConfigDlgState':
                click(BACK)
            time.sleep(1)
        if top(g) != 'CUserStrategyState':
            raise TimeoutError("the player's strategy phase did not come after Esc (%s)" % top_states(g)[-3:])
        time.sleep(2)
        log('taken over at %s' % (C.date(g),))
        save_slot(g, dst)
        time.sleep(3)
    finally:
        C.kill()
        time.sleep(3)
        check()
    made = [p for (d, n), p in save_files().items() if n == dst and os.path.exists(p)]
    log('slot %d file: %s' % (dst, ', '.join('%s (%s)' % (p, time.ctime(os.path.getmtime(p))) for p in made) or 'NOT FOUND'))


def keep_display_on():
    """While this process runs, keep the display (and system) awake: the fullscreen game quits when the
    monitor powers off (s14_logs.txt then shows the output as 'WinDisc'). Released when the process exits."""
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x1, 0x2
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)


def game_date():
    """Current date, or None once the game has exited."""
    if not C.window():
        return None
    try:
        return C.date(C.game())
    except BaseException:
        return None


def main():
    keep_display_on()
    a = sys.argv[1:]
    opt = lambda k, d=None: a[a.index(k) + 1] if k in a else d   # noqa: E731
    if '--make-save' in a:
        return make_save(a, opt)
    ai = opt('--ai', 'deploy,rally,engage,retreat,stance,walls,siegeplan,gate')
    ai = () if ai == 'none' else tuple(x for x in ai.split(',') if x)
    until = tuple(int(x) for x in opt('--until', '198-12-21').split('-'))
    baseline = opt('--baseline', BASELINE)
    if C.window():
        sys.exit('游戏已经开着，先关掉（python -P ai/gamectl.py kill）')
    ab = opt('--ab')
    dev = '--dev' in a           # helper, modules and DLLs from this checkout instead of the release package
    log('start: ai %s%s, until %s%s' % (','.join(ai) or 'none', ' (only %s force ids)' % ab if ab else '', until,
                                        ' (dev checkout)' if dev else ''))
    C.start(ai, (10, 10), ab=ab, dev=dev, hr_dll=opt('--hr-dll'), trap='--trap-near' in a, trap_log='--trap-log' in a)
    wait(C.window, 120, 1, 'game window')
    g = attach()
    wait(lambda: any('Title' in s for s in top_states(g)), 180, 1, 'title screen')
    load_slot(g, int(opt('--slot', 2)))
    start = tuple(int(x) for x in opt('--start').split('-')) if opt('--start') else START_DATE
    if C.date(g) != start:
        C.shot(os.path.join(C.SHOTS, 'autotest_wrong_save.png'))
        C.kill()
        sys.exit('loaded %s, expected %s: stopped' % (C.date(g), start))
    log('loaded: %s %s' % (C.date(g), top_states(g)[-3:]))
    rec = subprocess.Popen([sys.executable, '-P', os.path.join(HERE, 'observe.py')], cwd=ROOT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    log('auto on: %s' % auto_mode(g))
    d0 = C.date(g)
    wait(lambda: C.date(C.game()) != d0, 120, 2, 'date moving')
    log('running from %s' % (d0,))
    t0 = time.time()
    limit = float(opt('--minutes', 40)) * 60
    last = d0
    while time.time() - t0 < limit:
        d = game_date()
        if d is None:
            time.sleep(3)
            if game_date() is None:
                log('game exited early at %s after %.1f min (see s14_logs.txt)' % (last, (time.time() - t0) / 60))
                break
            continue
        last = d
        if d >= until:
            break
        time.sleep(5)
    log('stop at %s after %.1f min' % (last, (time.time() - t0) / 60))
    C.kill()
    rec.wait(60)
    runs = sorted(os.listdir(os.path.join(HERE, 'runs')))
    run = os.path.join(HERE, 'runs', runs[-1])
    log('run %s' % run)
    out = subprocess.run([sys.executable, '-P', os.path.join(HERE, 'research', 'check_modules.py'), run, baseline,
                          '--months', opt('--months', '12')], cwd=ROOT, capture_output=True, text=True,
                         encoding='utf-8', errors='replace', env=dict(os.environ, PYTHONIOENCODING='utf-8'))
    rep = os.path.join(run, 'check.txt')
    with open(rep, 'w', encoding='utf-8') as f:
        f.write((out.stdout or '') + (out.stderr or ''))
    log('check written to %s' % rep)


if __name__ == '__main__':
    main()
