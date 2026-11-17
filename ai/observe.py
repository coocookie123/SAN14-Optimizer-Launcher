"""看海记录：游戏开着时按月记录各势力的状况，并随时记下城市易主、势力灭亡等事件。只读内存。

  python -P ai/observe.py                 # 一直记录，游戏关掉或按 Ctrl+C 停止
  python -P ai/observe.py --interval 1    # 每 1 秒查一次（默认 0.5 秒）
  python -P ai/observe.py summary         # 汇总最近一次记录
  python -P ai/observe.py summary ai/runs/20261111-235900
  python -P ai/observe.py compare ai/runs/A ai/runs/B --months 12   # 两次记录并排比（同样只看前 12 个月）

每次记录写到 ai/runs/<开始时间>/：
  forces.csv  每月一行一个势力：城市数、军团数、武将数、城内兵力、出征兵力、资金、兵粮、攻略目标
  cities.csv  每月一行一座城：所属势力、兵力、资金、兵粮、士气、太守
  events.csv  随时：城市易主、势力灭亡、君主更替、攻略目标变更、日期倒退（读档或新开局）
  armies.csv  每天一行一支出征部队：主将、势力、军势和它的任务与目标、部队行动、所在格、兵数、士气、阵型、
              部队自己的目标（部队N = 第 N 支部队，城市N、关隘N、格N、物件N），所在区域、这一格涂成谁的、
              这个区域的府是谁的、府分（2026-11-12 起；府分＝所在区域和相邻区域里府属于本势力的，各按涂色
              占比加起来，就是游戏算府加成时加的那个数，函数 2b2b30，没算同盟），船型（部队 +0x1c，下水时用的船）、
              主将的水战个性（个性 68、154；2026-11-15 起）
  battles.csv 每次出征第一次掉兵那天（首次接战）：周围 3 格内的友军和敌军部队数、兵数
  sorties.csv 每次出征结束：出发和结束日期、最多和最后兵数、接战天数、结局（溃灭 / 交战后收兵 / 未交战）
  footholds.csv 各城市、关隘所在的格（只写一次）
  areas.csv   各区域所属的城、府所在的格、相邻区域（只写一次）
  fu.csv      每月一行一个有府的区域：府是谁的、区域里涂成府主人颜色的格子占几成
  meta.json   开始时间、游戏版本、起始日期

字段的含义和核对情况见 gamedata.py 开头的说明。
"""
import csv
import datetime
import glob
import json
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gamedata  # noqa: E402

P = gamedata.P
RUNS = os.path.join(HERE, 'runs')
SERVING = (1, 2, 3, 4)  # person +0x11e: 1 lord, 3 / 4 serving; 7 free, 0 / 9 not on the map
TRIBAL_CITY = 47       # cities 47..51 are the tribes (乌桓 鲜卑 羌 山越 南中), 300000 soldiers each


RESTART = ('日期倒退', '日期跳跃')


try:                                   # map terrain (research/terrain_exce.bin, hex +0x10): 14/24 water, 18 port
    TERRAIN = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'research', 'terrain_exce.bin'), 'rb').read()
except OSError:
    TERRAIN = b''


def day_number(d):
    return d[0] * 372 + d[1] * 31 + d[2]


def parse_date(s):
    y, m, d = s.split('-')
    return int(y), int(m), int(d)


def last_segment(rows, key='日期'):
    """Rows after the last load / new game. Events: from the last restart event. Monthly snapshots:
    from the last time the date went back or skipped more than two months."""
    start = 0
    prev = None
    for i, r in enumerate(rows):
        if '事件' in r:
            if r['事件'] in RESTART:
                start = i
            continue
        n = day_number(parse_date(r[key]))
        if prev is not None and (n < prev or n - prev > 62):
            start = i
        prev = n
    return rows[start:]


def text(raw, off, n=16):
    return raw[off:off + n].decode('utf-16le', 'replace').split('\0')[0]


MAP_W = 220   # hex index = row * 220 + column, 48400 hexes


def hex_distance(a, b):
    """The game's own hex distance (RVA 2acc20 in 1.0.2.58226): columns are offset, odd/even by column."""
    y1, x1 = divmod(a, MAP_W)
    y2, x2 = divmod(b, MAP_W)
    dx = abs(x2 - x1)
    dyy = abs(2 * (y2 - y1) - (x1 % 2 == 0) + (x2 % 2 == 0))
    return dx if dyy < dx else (dyy - dx) // 2 + dx


NEAR = 3      # hexes: "nearby" for the engagement statistics
TARGET_KINDS = {5: '城市', 6: '关隘', 9: '格', 11: '物件', 27: '部队'}  # army byte +0x38, id word +0x3a


def target_text(kind, tid):
    """Army's own target as '部队12' / '城市5' / '格31337'; empty when unset."""
    if not kind and not tid:
        return ''
    return f'{TARGET_KINDS.get(kind, kind)}{tid}'
FORMATIONS = {1: '鱼鳞', 2: '锋矢', 3: '雁行', 4: '方圆', 5: '鹤翼', 6: '长蛇', 7: '锥行', 8: '井阑', 9: '冲车',
              10: '投石', 11: '走舸', 12: '蒙冲', 13: '楼船', 14: '四夷', 17: '输船', 18: '输船', 19: '输送',
              20: '输船'}   # army byte +0x1b (condition 129 uses the same numbers)
NAVAL_TRAITS = (68, 154)   # person +0x150 trait ids the 优化伴侣 treats as 水战 and 操舵 (BEHAVIOR_PLAN 47)


def sortie_outcome(fight_days, peak, last):
    if not fight_days:
        return '未交战'
    return '溃灭?' if last <= peak // 5 else '交战后收兵'


def sorties_from_armies(rows):
    """Rebuild sorties from the daily army rows (one sortie = one slot with one general on consecutive
    records). Returns dicts like sorties.csv."""
    out = []
    cur = {}
    days = sorted({r['日期'] for r in rows}, key=lambda d: day_number(parse_date(d)))
    by_day = {}
    for r in rows:
        by_day.setdefault(r['日期'], {})[r['部队']] = r
    for d in days:
        today = by_day[d]
        for slot, s in list(cur.items()):
            r = today.get(slot)
            if r and r['主将'] == s['主将']:
                continue
            out.append(dict(s, 结束=d, 结局=sortie_outcome(s['接战天数'], s['最多兵数'], s['最后兵数'])))
            del cur[slot]
        for slot, r in today.items():
            n = int(r['兵数'])
            s = cur.get(slot)
            if s is None:
                cur[slot] = {'出发': d, '部队': slot, '主将': r['主将'], '势力': r['势力'], '军势任务': r['军势任务'],
                             '最多兵数': n, '最后兵数': n, '接战天数': 0}
                continue
            if s['最后兵数'] - n > max(30, s['最后兵数'] // 100):
                s['接战天数'] += 1
            s['最多兵数'] = max(s['最多兵数'], n)
            s['最后兵数'] = n
    return out


class Game:
    """One consistent read of the parts of the game the recorder needs."""

    def __init__(self, g):
        self.g = g
        self._naval = {}

    def date(self):
        st = self.g.q(self.g.root + gamedata.STATUS)
        raw = self.g.read(st + 0x34, 4) if st else None
        if not raw or len(raw) < 4:
            return None
        y, m, d = struct.unpack('<HBB', raw)
        return (y, m, d) if 1 <= m <= 12 else None

    def objects(self, name, size):
        out = {}
        for i, p in enumerate(self.g.array(name)):
            if i == 0 or not p:
                continue
            raw = self.g.read(p, size)
            if raw and len(raw) == size:
                out[i] = raw
        return out

    def person_name(self, pid):
        if not 0 < pid <= 6000:
            return ''
        p = self.g.obj('person', pid)
        raw = self.g.read(p, 0x36) if p else None
        return text(raw, 0x12) + text(raw, 0x24) if raw else ''

    def light(self):
        """Read every poll: cities, districts, forces."""
        cities = {}
        for i, raw in self.objects('city', 0x50).items():
            cities[i] = {'name': text(raw, 0x12), 'district': raw[0x30],
                         'money': struct.unpack_from('<i', raw, 0x34)[0],
                         'food': struct.unpack_from('<i', raw, 0x38)[0],
                         'soldiers': struct.unpack_from('<i', raw, 0x3c)[0],
                         'morale': raw[0x44], 'governor': struct.unpack_from('<H', raw, 0x2e)[0],
                         'tribal': i >= TRIBAL_CITY}
        districts = {i: {'force': raw[0x10], 'commander': struct.unpack_from('<H', raw, 0x12)[0]}
                     for i, raw in self.objects('district', 0x18).items()}
        forces = {}
        for i, raw in self.objects('force', 0x180).items():
            lord = struct.unpack_from('<H', raw, 0x10)[0]
            if lord:
                forces[i] = {'lord': lord, 'target': raw[0x17a]}
        for c in cities.values():
            d = districts.get(c['district'])
            c['force'] = d['force'] if d and c['district'] else 0
        return cities, districts, forces

    def heavy(self, districts):
        """Read once a month: serving persons and armies in the field, counted per force."""
        persons = {}
        for i, p in enumerate(self.g.array('person')):
            if i == 0 or not p:
                continue
            raw = self.g.read(p + 0x118, 8)
            if not raw or len(raw) < 8:
                continue
            dist, st = raw[0], raw[6]
            d = districts.get(dist)
            if st in SERVING and d and dist:
                persons[d['force']] = persons.get(d['force'], 0) + 1
        armies = {}
        for a in self.armies(districts).values():
            n, s = armies.get(a['force'], (0, 0))
            armies[a['force']] = (n + 1, s + a['soldiers'])
        return persons, armies

    def footholds(self):
        """[(kind, index, name, hex)] for cities and gates: foothold word +0x4e is its map object,
        whose word +0x10 is the hex (function 22edd0)."""
        out = []
        O = self.g.array('object')
        for kind, arr in (('城市', 'city'), ('关隘', 'gate')):
            for i, p in enumerate(self.g.array(arr)):
                if i == 0 or not p:
                    continue
                raw = self.g.read(p, 0x50)
                if not raw or len(raw) < 0x50:
                    continue
                oi = struct.unpack_from('<H', raw, 0x4e)[0]
                op = O[oi] if 0 < oi < len(O) else 0
                h = self.g.read(op + 0x10, 2) if op else None
                if h and len(h) == 2:
                    out.append((kind, i, text(raw, 0x12), struct.unpack('<H', h)[0]))
        return out

    def areas(self):
        """{area: (city, 府 hex)}: area word +0x3a is its 府's hex (area 1 of 襄平 is 襄平's own hex), byte +0x35
        the city it belongs to (0: none). Read with the game running, 2026-11-12."""
        out = {}
        for i, raw in self.objects('area', 0x40).items():
            h = struct.unpack_from('<H', raw, 0x3a)[0]
            if h:
                out[i] = (raw[0x35], h)
        return out

    def area_state(self):
        """{area: (owner force, painted share, [neighbour areas])}, read once a day. Area byte +0x78 is the
        force holding its 府, words +0x68 / +0x76 the hexes painted by that force / all its hexes, words
        +0x56.. up to 8 neighbour areas (0 = none). This is what the 府加成 function 2b2b30 adds up."""
        out = {}
        for i, raw in self.objects('area', 0x80).items():
            total = struct.unpack_from('<H', raw, 0x76)[0]
            if not total:
                continue
            nb = [n for n in struct.unpack_from('<8H', raw, 0x56) if n]
            out[i] = (raw[0x78], struct.unpack_from('<H', raw, 0x68)[0] / total, nb)
        return out

    def hex_info(self, h, H=None):
        """(area, force that painted it): hex word +0x12 is its area (hex info 1), byte +0x14 the force
        (0: nobody). Checked 2026-11-12: a city's hex has its owner, its 府 hex has the area's own id."""
        H = H or self.g.array('hex')
        raw = self.g.read(H[h] + 0x12, 3) if 0 <= h < len(H) and H[h] else None
        return (struct.unpack_from('<H', raw, 0)[0], raw[2]) if raw and len(raw) == 3 else (0, 0)

    def slot_soldiers(self, i):
        """Soldiers left in an army slot (also after the army is gone: 0 means it was wiped out)."""
        A = self.g.array('army')
        raw = self.g.read(A[i] + 0x16, 2) if 0 < i < len(A) and A[i] else None
        return struct.unpack('<H', raw)[0] if raw and len(raw) == 2 else None

    def armies(self, districts):
        """Armies in the field: {index: {...}}. A slot is live while its general's whereabouts
        (person +0x11c) is 61 + the slot index; finished armies leave stale data behind."""
        out = {}
        troops_cache = {}
        T = self.g.array('troops')
        for i, p in enumerate(self.g.array('army')):
            if i == 0 or not p:
                continue
            raw = self.g.read(p, 0x60)
            if not raw or len(raw) < 0x60:
                continue
            gen = struct.unpack_from('<H', raw, 0x12)[0]
            if not 0 < gen <= 6000:
                continue
            pp = self.g.obj('person', gen)
            pr = self.g.read(pp + 0x118, 8) if pp else None
            if not pr or struct.unpack_from('<H', pr, 4)[0] != gamedata.ARMY_WHERE_BASE + i:
                continue
            d = districts.get(pr[0])
            ti = struct.unpack_from('<H', raw, 0x58)[0]
            if ti not in troops_cache:
                tr = self.g.read(T[ti], 0x30) if 0 < ti < len(T) and T[ti] else None
                # +0x14 purpose, +0x16 target, +0x2c main general (his army is the 本队)
                troops_cache[ti] = (struct.unpack_from('<HH', tr, 0x14) + struct.unpack_from('<H', tr, 0x2c)
                                    if tr and len(tr) == 0x30 else (0, 0, 0))
            purpose, target, leader = troops_cache[ti]
            out[i] = {'general': gen, 'force': d['force'] if d and pr[0] else 0,
                      'hex': struct.unpack_from('<H', raw, 0x2a)[0],
                      'soldiers': struct.unpack_from('<H', raw, 0x16)[0], 'morale': raw[0x1a],
                      'action': raw[0x37], 'troops': ti, 'purpose': purpose, 'target': target,
                      'formation': raw[0x1b], 'lead': bool(leader) and leader == gen,
                      'ship': raw[0x1c], 'naval': self.naval_traits(gen),
                      'ttype': raw[0x38], 'tid': struct.unpack_from('<H', raw, 0x3a)[0]}
        return out


def _naval_traits(self, gen):
    """Water-combat traits of a general (person +0x150, five u16 trait ids): 68 and 154 (the companion's 水战, 操舵)."""
    if gen not in self._naval:
        pp = self.g.obj('person', gen)
        raw = self.g.read(pp + 0x150, 10) if pp else b''
        ids = struct.unpack('<5H', raw) if len(raw) == 10 else ()
        self._naval[gen] = '+'.join(str(t) for t in ids if t in NAVAL_TRAITS)
    return self._naval[gen]


Game.naval_traits = _naval_traits


class Recorder:
    def __init__(self, g, out):
        self.game = Game(g)
        self.out = out
        os.makedirs(out, exist_ok=True)
        self.files = {}
        self.writers = {}
        heads = {
            'forces': ['日期', '势力', '君主', '城市', '军团', '武将', '城内兵力', '出征部队', '出征兵力',
                       '资金', '兵粮', '攻略目标', '异民族'],
            'cities': ['日期', '城市', '势力', '兵力', '资金', '兵粮', '士气', '太守'],
            'events': ['日期', '事件', '对象', '从', '到'],
            'armies': ['日期', '部队', '主将', '势力', '军势', '军势任务', '军势目标', '部队行动', '格', '兵数',
                       '士气', '阵型', '部队目标', '本队', '区域', '格子势力', '府势力', '府分', '船型', '水战个性'],
            'fu': ['日期', '区域', '府格', '府势力', '涂色占比'],
            'battles': ['日期', '部队', '主将', '势力', '兵数', f'{NEAR}格内友军', f'{NEAR}格内友军兵数',
                        f'{NEAR}格内敌军', f'{NEAR}格内敌军兵数'],
            'sorties': ['出发', '结束', '部队', '主将', '势力', '军势任务', '最多兵数', '最后兵数', '接战天数',
                        '结局'],
        }
        for k, h in heads.items():
            f = open(os.path.join(out, k + '.csv'), 'w', encoding='utf-8-sig', newline='')
            self.files[k] = f
            self.writers[k] = csv.writer(f)
            self.writers[k].writerow(h)
        self.names = {}       # force index -> lord name, kept after the force is gone
        self.prev = None      # (cities, forces) from the last poll
        self.last_date = None
        self.last_month = None
        self.settle = None    # date at start / after a load; nothing is recorded until it moves
        self.months = 0
        self.events = 0
        self.last_day = None  # last date the armies were recorded
        self.wrote_footholds = False
        self.area_table = None
        self.sortie = {}      # army slot -> state of the sortie in progress

    def fname(self, i, forces=None):
        if not i:
            return '空城'
        if forces and i in forces:
            self.names[i] = self.game.person_name(forces[i]['lord']) or self.names.get(i, f'势力{i}')
        return self.names.get(i, f'势力{i}')

    @staticmethod
    def dstr(d):
        return f'{d[0]}-{d[1]:02d}-{d[2]:02d}'

    def event(self, date, kind, who, frm='', to=''):
        self.writers['events'].writerow([self.dstr(date), kind, who, frm, to])
        self.events += 1

    def poll(self):
        date = self.game.date()
        if not date:
            return False
        cities, districts, forces = self.game.light()
        for i in forces:
            self.fname(i, forces)
        if self.last_date:
            gap = day_number(date) - day_number(self.last_date)
            if gap < 0 or gap > 40:     # loaded a save or started a new game (also: title screen -> game)
                self.event(date, '日期倒退' if gap < 0 else '日期跳跃', '读档或新开局',
                            self.dstr(self.last_date), self.dstr(date))
                self.settle = date
                self.prev = None
                self.last_month = None
                self.sortie = {}
                self.last_day = None
        else:
            self.settle = date
        if date == self.settle:
            # The date has not moved since we started or since a load: this may be the title screen's
            # demo data or a game still being set up, so only remember it.
            self.prev = (cities, forces)
            self.last_date = date
            return True
        if self.prev:
            self.diff(date, cities, forces)
        if (date[0], date[1]) != self.last_month:
            self.snapshot(date, cities, districts, forces)
            self.last_month = (date[0], date[1])
        if date != self.last_day:
            self.track_armies(date, districts, forces)
            self.fu_changes(date, forces)
            self.last_day = date
        self.prev = (cities, forces)
        self.last_date = date
        for f in self.files.values():
            f.flush()
        return True

    def diff(self, date, cities, forces):
        pc, pf = self.prev
        for i, c in cities.items():
            old = pc.get(i)
            if old and old['force'] != c['force']:
                self.event(date, '城市易主', c['name'], self.fname(old['force']), self.fname(c['force'], forces))
        for i, f in pf.items():
            if i not in forces:
                self.event(date, '势力灭亡', self.fname(i))
                continue
            now = forces[i]
            if now['lord'] != f['lord']:
                self.event(date, '君主更替', self.fname(i), self.game.person_name(f['lord']),
                           self.game.person_name(now['lord']))
            if now['target'] != f['target']:
                self.event(date, '攻略目标', self.fname(i),
                           self.fname(f['target']) if f['target'] else '无',
                           self.fname(now['target'], forces) if now['target'] else '无')
        for i in forces:
            if i not in pf:
                self.event(date, '势力出现', self.fname(i, forces))

    def track_armies(self, date, districts, forces):
        """Once per game day: every army in the field, sorties starting and ending, first contact."""
        ds = self.dstr(date)
        armies = self.game.armies(districts)
        names = {}
        if self.area_table is None:
            self.area_table = self.game.areas()
        H = self.game.g.array('hex')
        st = self.game.area_state()

        def score(area, force):
            if area not in st:
                return 0
            return round(sum(st[a][1] for a in [area] + st[area][2] if a in st and st[a][0] == force), 2)

        def place(h):
            # 府势力 is the area's owner (area +0x78, what the 府加成 function reads). Until 2026-11-12 22:00 this
            # column was the paint of the 府 hex, which an army standing on it repaints at once: wrong while contested.
            area, owner = self.game.hex_info(h, H)
            return area, owner, st[area][0] if area in st else 0

        def gname(pid):
            if pid not in names:
                names[pid] = self.game.person_name(pid)
            return names[pid]

        for i, a in sorted(armies.items()):
            area, owner, fu = place(a['hex'])
            self.writers['armies'].writerow([ds, i, gname(a['general']), self.fname(a['force'], forces), a['troops'],
                                             a['purpose'], a['target'], a['action'], a['hex'], a['soldiers'],
                                             a['morale'], FORMATIONS.get(a['formation'], a['formation']),
                                             target_text(a['ttype'], a['tid']), '是' if a['lead'] else '',
                                             area, self.fname(owner, forces) if owner else '',
                                             self.fname(fu, forces) if fu else '', score(area, a['force']),
                                             FORMATIONS.get(a['ship'], a['ship']), a['naval']])
        # sorties: a slot with a new general is a new sortie
        for i, s in list(self.sortie.items()):
            a = armies.get(i)
            if a and a['general'] == s['general']:
                continue
            self.end_sortie(ds, i, s, forces, reused=a is not None)
            del self.sortie[i]
        for i, a in armies.items():
            s = self.sortie.get(i)
            if s is None:
                self.sortie[i] = s = {'start': ds, 'general': a['general'], 'force': a['force'],
                                      'purpose': a['purpose'], 'peak': a['soldiers'], 'last': a['soldiers'],
                                      'fight_days': 0, 'engaged': False}
                continue
            lost = s['last'] - a['soldiers']
            if lost > max(30, s['last'] // 100):      # losing men today: in combat
                s['fight_days'] += 1
                if not s['engaged']:
                    s['engaged'] = True
                    self.first_contact(ds, i, a, armies, gname, forces)
            s['peak'] = max(s['peak'], a['soldiers'])
            s['last'] = a['soldiers']

    def first_contact(self, ds, i, a, armies, gname, forces):
        near_f = [b for j, b in armies.items() if j != i and b['force'] == a['force']
                  and hex_distance(a['hex'], b['hex']) <= NEAR]
        near_e = [b for b in armies.values() if b['force'] != a['force']
                  and hex_distance(a['hex'], b['hex']) <= NEAR]
        self.writers['battles'].writerow([ds, i, gname(a['general']), self.fname(a['force'], forces), a['soldiers'],
                                          len(near_f), sum(b['soldiers'] for b in near_f),
                                          len(near_e), sum(b['soldiers'] for b in near_e)])

    def end_sortie(self, ds, i, s, forces, reused=False):
        # The slot's soldiers drop to 0 both when an army is wiped out and when it goes back into a city,
        # so judge by what was seen while it was in the field.
        how = sortie_outcome(s['fight_days'], s['peak'], s['last'])
        self.writers['sorties'].writerow([s['start'], ds, i, self.game.person_name(s['general']),
                                          self.fname(s['force'], forces), s['purpose'], s['peak'], s['last'],
                                          s['fight_days'], how])

    def snapshot(self, date, cities, districts, forces):
        if not self.wrote_footholds:      # positions never change; written once the game is set up
            with open(os.path.join(self.out, 'footholds.csv'), 'w', encoding='utf-8-sig', newline='') as f:
                w = csv.writer(f)
                w.writerow(['类型', '编号', '名称', '格'])
                w.writerows(self.game.footholds())
            if self.area_table is None:
                self.area_table = self.game.areas()
            with open(os.path.join(self.out, 'areas.csv'), 'w', encoding='utf-8-sig', newline='') as f:
                w = csv.writer(f)
                w.writerow(['区域', '城市', '府格', '相邻区域'])
                nb = {a: v[2] for a, v in self.game.area_state().items()}
                w.writerows([a, c, h, ' '.join(map(str, nb.get(a, [])))] for a, (c, h) in sorted(self.area_table.items()))
            self.wrote_footholds = True
        self.fu_snapshot(date, forces)
        persons, armies = self.game.heavy(districts)
        ds = self.dstr(date)
        per = {}
        tribal = {}
        for c in cities.values():
            tribal[c['force']] = tribal.get(c['force'], True) and c['tribal']
            s = per.setdefault(c['force'], [0, 0, 0, 0])
            s[0] += 1
            s[1] += c['soldiers']
            s[2] += c['money']
            s[3] += c['food']
        ndist = {}
        for i, d in districts.items():
            if d['commander']:
                ndist[d['force']] = ndist.get(d['force'], 0) + 1
        for i, f in sorted(forces.items()):
            n, sold, money, food = per.get(i, [0, 0, 0, 0])
            an, asold = armies.get(i, (0, 0))
            self.writers['forces'].writerow([ds, i, self.fname(i, forces), n, ndist.get(i, 0), persons.get(i, 0),
                                             sold, an, asold, money, food,
                                             self.fname(f['target'], forces) if f['target'] else '',
                                             '是' if tribal.get(i) else ''])
        for i, c in sorted(cities.items()):
            self.writers['cities'].writerow([ds, c['name'], self.fname(c['force'], forces), c['soldiers'],
                                             c['money'], c['food'], c['morale'],
                                             self.game.person_name(c['governor'])])
        self.months += 1
        alive = sum(1 for i in forces if per.get(i, [0])[0] and not tribal.get(i))
        print(f'{ds}  记录第 {self.months} 个月：{alive} 个势力有城，事件累计 {self.events} 条', flush=True)

    def fu_changes(self, date, forces):
        """Every day: an event 府易主 when the force holding an area's 府 changes (2026-11-13: ports are 府 hexes,
        terrain 18; the user saw besiegers lose an unguarded port and their supply). who: area:府 hex[:港]."""
        try:
            if self.area_table is None:
                self.area_table = self.game.areas()
            st = self.game.area_state()
        except Exception:
            return
        cur = {a: v[0] for a, v in st.items() if a in self.area_table}
        old = getattr(self, 'fu_prev', None)
        if old:
            for a, f in cur.items():
                if a in old and old[a] != f:
                    fh = self.area_table[a][1]
                    port = ':港' if TERRAIN and 0 <= fh < len(TERRAIN) and TERRAIN[fh] == 18 else ''
                    self.event(date, '府易主', '%d:%d%s' % (a, fh, port), self.fname(old[a], forces) if old[a] else '',
                               self.fname(f, forces) if f else '')
        self.fu_prev = cur

    def fu_snapshot(self, date, forces):
        """Once a month: who holds each 府, and what share of its area's hexes that force has painted."""
        if self.area_table is None:
            self.area_table = self.game.areas()
        H = self.game.g.array('hex')
        tally = {}
        for h in range(len(H)):
            area, owner = self.game.hex_info(h, H)
            if area in self.area_table:
                t = tally.setdefault(area, {})
                t[owner] = t.get(owner, 0) + 1
        ds = self.dstr(date)
        st = self.game.area_state()
        for area, (city, fh) in sorted(self.area_table.items()):
            fu = st[area][0] if area in st else 0
            t = tally.get(area, {})
            n = sum(t.values())
            self.writers['fu'].writerow([ds, area, fh, self.fname(fu, forces) if fu else '',
                                         round(t.get(fu, 0) / n, 2) if fu and n else 0])

    def close(self):
        for f in self.files.values():
            f.close()


def record(interval):
    try:
        g = gamedata.GameData()
    except SystemExit as e:
        sys.exit(f'{e}。先开游戏，进到大地图再运行。')
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    out = os.path.join(RUNS, stamp)
    rec = Recorder(g, out)
    first = rec.game.date()
    with open(os.path.join(out, 'meta.json'), 'w', encoding='utf-8') as f:
        json.dump({'started': stamp, 'exe': P.exe_key(g.hp, g.base), 'pid': g.pid,
                   'start_date': rec.dstr(first) if first else None, 'interval': interval},
                  f, ensure_ascii=False, indent=1)
    print(f'记录到 {out}，游戏关掉或按 Ctrl+C 停止', flush=True)
    fails = 0
    try:
        while True:
            if rec.poll():
                fails = 0
            else:
                fails += 1
                if fails >= 5 and P.find_pid() != g.pid:
                    print('游戏已关闭，停止记录')
                    break
            time.sleep(interval)
    except KeyboardInterrupt:
        print('已停止')
    finally:
        rec.close()
    summary(out)


def summary(run=None):
    if not run:
        runs = sorted(glob.glob(os.path.join(RUNS, '*')))
        if not runs:
            sys.exit('还没有记录')
        run = runs[-1]
    with open(os.path.join(run, 'forces.csv'), encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    with open(os.path.join(run, 'events.csv'), encoding='utf-8-sig') as f:
        events = list(csv.DictReader(f))
    # only the last game played: drop the title screen and anything before the last load / new game
    rows = last_segment(rows)
    events = last_segment(events)
    if events and events[0]['事件'] in RESTART:
        start = events[0]['日期']
        events = [e for e in events[1:] if e['日期'] != start]   # the load itself is still settling
    if not rows:
        print(f'{run}：还没有完整的月份')
        return
    dates = sorted({r['日期'] for r in rows})
    first, last = dates[0], dates[-1]
    hist = {}
    for r in rows:
        hist.setdefault(r['势力'], []).append(r)
    print(f'{run}\n{first} 到 {last}，共 {len(dates)} 个月，事件 {len(events)} 条（下表不含异民族）\n')
    print(f'{"势力":6s} {"城市 起→止(最多)":16s} {"兵力 起→止":18s} {"武将 起→止":10s} 存活月数')
    out = []
    for k, rs in hist.items():
        if rs[0].get('异民族'):
            continue
        a, b = rs[0], rs[-1]
        peak = max(int(r['城市']) for r in rs)
        alive = sum(1 for r in rs if int(r['城市']) > 0)
        sold_a = int(a['城内兵力']) + int(a['出征兵力'])
        sold_b = int(b['城内兵力']) + int(b['出征兵力'])
        out.append((int(b['城市']), peak, f'{b["君主"]:6s} {a["城市"]:>3s} → {b["城市"]:>3s} ({peak:2d})     '
                    f'{sold_a:7d} → {sold_b:7d}  {a["武将"]:>3s} → {b["武将"]:>3s}  {alive}'))
    for _, peak, line in sorted(out, key=lambda x: (-x[0], -x[1])):
        if peak:
            print(line)
    kinds = {}
    for e in events:
        kinds[e['事件']] = kinds.get(e['事件'], 0) + 1
    print('\n事件：' + '，'.join(f'{k} {v}' for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])))
    taken = [e for e in events if e['事件'] == '城市易主']
    if taken:
        print('最近的城市易主：')
        for e in taken[-8:]:
            print(f'  {e["日期"]} {e["对象"]}：{e["从"]} → {e["到"]}')
    army_summary(run, first)


def read_csv(run, name):
    p = os.path.join(run, name + '.csv')
    if not os.path.exists(p):
        return []
    with open(p, encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def army_summary(run, first):
    """Field battles per force: sorties, how they ended, and how many allies were near at first contact."""
    since = day_number(parse_date(first))
    armies = [r for r in read_csv(run, 'armies') if day_number(parse_date(r['日期'])) >= since]
    sorties = sorties_from_armies(armies)     # finished sorties, judged the same way for old runs
    battles = [b for b in read_csv(run, 'battles') if day_number(parse_date(b['日期'])) >= since]
    if not sorties and not battles:
        return
    per = {}
    for s in sorties:
        d = per.setdefault(s['势力'], {'n': 0, 'fought': 0, 'wiped': 0, 'contacts': 0, 'alone': 0, 'allies': 0,
                                       'outnumbered': 0})
        d['n'] += 1
        d['fought'] += s['结局'] != '未交战'
        d['wiped'] += s['结局'] == '溃灭?'
    for b in battles:
        d = per.setdefault(b['势力'], {'n': 0, 'fought': 0, 'wiped': 0, 'contacts': 0, 'alone': 0, 'allies': 0,
                                       'outnumbered': 0})
        allies = int(b[f'{NEAR}格内友军'])
        d['contacts'] += 1
        d['alone'] += allies == 0
        d['allies'] += allies
        own = int(b['兵数']) + int(b[f'{NEAR}格内友军兵数'])
        d['outnumbered'] += int(b[f'{NEAR}格内敌军兵数']) > own * 1.5
    print(f'\n野战（出征部队；接战 = 首次掉兵那天，周围按 {NEAR} 格算）')
    print(f'{"势力":6s} 出征 交战 溃灭  接战次数 孤军接战 平均身边友军 兵力劣势(敌>1.5倍)')
    for k, d in sorted(per.items(), key=lambda kv: -kv[1]['n']):
        if not d['n'] and not d['contacts']:
            continue
        avg = d['allies'] / d['contacts'] if d['contacts'] else 0
        print(f'{k:6s} {d["n"]:4d} {d["fought"]:4d} {d["wiped"]:4d}  {d["contacts"]:6d}  {d["alone"]:6d}'
              f'     {avg:6.1f}       {d["outnumbered"]:4d}')
    tot = {k: sum(d[k] for d in per.values()) for k in ('n', 'wiped', 'contacts', 'alone', 'outnumbered')}
    if tot['contacts']:
        print(f'合计：出征 {tot["n"]}，溃灭 {tot["wiped"]}；接战 {tot["contacts"]} 次，其中孤军 '
              f'{tot["alone"]} 次（{tot["alone"] * 100 // tot["contacts"]}%），兵力明显劣势 {tot["outnumbered"]} 次')


def run_metrics(run, months=None):
    """Headline numbers of the last game in a run, optionally only its first `months` months."""
    events = read_csv(run, 'events')
    seg = last_segment(events)
    if seg and seg[0]['事件'] in RESTART:
        start_s = seg[0]['日期']
        seg = [e for e in seg[1:] if e['日期'] != start_s]
    rows = last_segment(read_csv(run, 'forces'))
    if not rows:
        return None
    first = min((r['日期'] for r in rows), key=lambda d: day_number(parse_date(d)))
    lo = day_number(parse_date(first))
    hi = lo + months * 31 if months else 10 ** 9
    inside = lambda d: lo <= day_number(parse_date(d)) < hi  # noqa: E731
    armies = [r for r in read_csv(run, 'armies') if inside(r['日期'])]
    battles = [b for b in read_csv(run, 'battles') if inside(b['日期'])]
    sorties = sorties_from_armies(armies)
    last = max((r['日期'] for r in armies), key=lambda d: day_number(parse_date(d)), default=first)
    m = {'期间': f'{first} 到 {last}', '出征': len(sorties),
         '交战的出征': sum(s['结局'] != '未交战' for s in sorties),
         '溃灭': sum(s['结局'] == '溃灭?' for s in sorties), '接战': len(battles)}
    alone = sum(int(b[f'{NEAR}格内友军']) == 0 for b in battles)
    m['孤军接战'] = f'{alone}（{alone * 100 // len(battles) if battles else 0}%）'
    m['平均身边友军'] = round(sum(int(b[f'{NEAR}格内友军']) for b in battles) / len(battles), 2) if battles else 0
    m['兵力明显劣势'] = sum(int(b[f'{NEAR}格内敌军兵数']) > 1.5 * (int(b['兵数']) + int(b[f'{NEAR}格内友军兵数']))
                      for b in battles)
    m['城市易主'] = sum(e['事件'] == '城市易主' and inside(e['日期']) for e in seg)
    m['势力灭亡'] = sum(e['事件'] == '势力灭亡' and inside(e['日期']) for e in seg)
    m['未交战就收兵'] = sum(s['结局'] == '未交战' for s in sorties)
    days = [day_number(parse_date(s['结束'])) - day_number(parse_date(s['出发'])) for s in sorties]
    m['出征平均天数'] = round(sum(days) / len(days), 1) if days else 0
    return m


def compare(a, b, months=None):
    ma, mb = run_metrics(a, months), run_metrics(b, months)
    if not ma or not mb:
        sys.exit('有一份记录还没有完整的月份')
    print(f'{"":12s} {os.path.basename(a):>22s} {os.path.basename(b):>22s}' +
          (f'   （都只看前 {months} 个月）' if months else ''))
    for k in ma:
        print(f'{k:10s} {str(ma[k]):>22s} {str(mb[k]):>22s}')


def main():
    args = sys.argv[1:]
    if args and args[0] == 'summary':
        summary(args[1] if len(args) > 1 else None)
        return
    if args and args[0] == 'compare':
        months = None
        if '--months' in args:
            i = args.index('--months')
            months = int(args[i + 1])
            del args[i:i + 2]
        compare(args[1], args[2], months)
        return
    interval = 0.5
    if '--interval' in args:
        interval = float(args[args.index('--interval') + 1])
    record(interval)


if __name__ == '__main__':
    main()
