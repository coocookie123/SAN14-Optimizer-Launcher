// san14_ai.dll: the native half of the computer-AI modules.
//
// Loaded before the game's own code runs (ai/launch.py starts SAN14CE_SC.exe suspended and queues
// LoadLibraryW as an APC on its main thread). Nothing is written anywhere except the log (SAN14_AI_LOG);
// the game directory is never touched.
//
// 1. Tree redirect. CreateFileW is replaced in two import tables: the game's own and kernel32's
//    (kernel32!CreateFileW is a thunk through its IAT into KernelBase). When the game opens
//    ...\0000\bt_exce.bin (either slash), the hook opens the file named by SAN14_AI_BT instead.
//
// 2. Daily layer (only when SAN14_AI_FEATURES names features). When the game first opens the tree file
//    its code is decrypted, so then we locate, by the sigs in sigs/sigs.json (aihook/sigs_gen.h):
//      data_root        the data root pointer (arrays of persons, armies, hexes ... hang off it)
//      progress_vtable  CProgressState's vtable (the state on top while days pass), progress_update
//                       checks that slot 5 is its Update
//      army_setters     the game's own "set action" / "set target" functions: offsets and their rule
//                       (write only while army +0x140 == 0)
//    and replace CProgressState slot 5. Our Update runs the original, then, when the game date changed,
//    the day's features, on the game's own state thread. Object classes are checked by RTTI before the
//    first use. Any mismatch: nothing is hooked or the features are switched off, the game runs as usual.
//    SAN14_AI_AB=odd|even limits the features to forces with odd / even ids (A/B tests).
//
// Features:
//    stance  雁行站位: a 雁行 (ranged formation) stays out of melee. Each day:
//            enemy adjacent -> step back to such a hex (armies in contact mostly cannot move; with the
//            contact experiment it attacks instead); enemy exactly 2 away -> hold (it
//            shoots by itself at range 2); enemy 3-4 away -> go to such a firing position instead of
//            walking into contact (v3); no enemy within 4 -> give back the order it had before we touched
//            it. Orders the tree or the game give later replace what we remember. Player armies are never
//            touched.
//    contact 接战目标: any other computer land army touching an enemy while under a move order (it cannot
//            move) attacks an adjacent enemy instead: the one we hold from the other side (pincer), else
//            an engine or 雁行, else the weakest; its order comes back when nothing touches it.
#include <windows.h>
#include <wchar.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "sigs_gen.h"            // generated from sigs/sigs.json by aihook/build.py
#include "../native/sigscan.h"   // MakePat, FindPat, Section, VtableByRtti, SigAt, SigScan, SigRel, SigU32

typedef HANDLE(WINAPI *CreateFileW_t)(LPCWSTR, DWORD, DWORD, LPSECURITY_ATTRIBUTES, DWORD, DWORD, HANDLE);

static CreateFileW_t g_real = nullptr;
static wchar_t g_target[MAX_PATH * 2];
static wchar_t g_logpath[MAX_PATH * 2];
static volatile LONG g_opens = 0;
static volatile LONG g_redirects = 0;
static CRITICAL_SECTION g_logcs;

static void logf(const wchar_t *fmt, ...) {
    if (!g_logpath[0]) return;
    EnterCriticalSection(&g_logcs);
    FILE *f = _wfopen(g_logpath, L"a, ccs=UTF-8");
    if (f) {
        SYSTEMTIME t;
        GetLocalTime(&t);
        fwprintf(f, L"%02d:%02d:%02d.%03d ", t.wHour, t.wMinute, t.wSecond, t.wMilliseconds);
        va_list ap;
        va_start(ap, fmt);
        vfwprintf(f, fmt, ap);
        va_end(ap);
        fputwc(L'\n', f);
        fclose(f);
    }
    LeaveCriticalSection(&g_logcs);
}

// ---- features -------------------------------------------------------------------------------------
static bool g_stance = false;
static bool g_pincer = false;
static bool g_defend = false;
static bool g_contact = false;
static int g_ab = 0;                      // 0 every force, 1 odd ids only, 2 even ids only

static void read_features() {
    wchar_t buf[512];
    DWORD n = GetEnvironmentVariableW(L"SAN14_AI_FEATURES", buf, 512);
    if (n && n < 512) {
        for (wchar_t *ctx = nullptr, *t = wcstok(buf, L", ", &ctx); t; t = wcstok(nullptr, L", ", &ctx)) {
            if (!wcscmp(t, L"stance")) g_stance = true;
            else if (!wcscmp(t, L"pincer")) g_pincer = true;
            else if (!wcscmp(t, L"defend")) g_defend = true;
            else if (!wcscmp(t, L"contact")) g_contact = true;
            else logf(L"unknown feature %ls (ignored)", t);
        }
    }
    n = GetEnvironmentVariableW(L"SAN14_AI_AB", buf, 512);
    if (n && n < 512) g_ab = !wcscmp(buf, L"odd") ? 1 : !wcscmp(buf, L"even") ? 2 : 0;
}

static bool any_feature() { return g_stance || g_pincer || g_defend || g_contact; }
static const wchar_t *feature_names() {
    static wchar_t buf[64];
    buf[0] = 0;
    if (g_stance) wcscat(buf, L" stance");
    if (g_pincer) wcscat(buf, L" pincer");
    if (g_defend) wcscat(buf, L" defend");
    if (g_contact) wcscat(buf, L" contact");
    return buf[0] ? buf : L" none";
}

// ---- game data ------------------------------------------------------------------------------------
// Layout of 1.0.2.58226 (ai/gamedata.py). Arrays of object pointers under the data root; the class of
// every array's objects is checked by RTTI before the first use.
enum : uint32_t {
    ROOT_PERSON = 0x158, N_PERSON = 6001,
    ROOT_DISTRICT = 0xe0d0, N_DISTRICT = 52,
    ROOT_HEX = 0xe270, N_HEX = 48400,
    ROOT_ARMY = 0x7e838, N_ARMY = 501,
    ROOT_CITY = 0xdd38, N_CITY = 52,       // CCityData: +0x30 district (u8), +0x4e map object (u16)
    ROOT_OBJECT = 0x6da98, N_OBJECT = 3001, // CObjectData: +0x10 hex (u16)
    C_DISTRICT = 0x30, C_OBJECT = 0x4e, O_HEX = 0x10,
    ROOT_STATUS = 0x871e8,                // CWorldData: +0x34 date (u16 year, u8 month, u8 day), +0x3a player force
    ST_DATE = 0x34, ST_PLAYER = 0x3a,
    P_DISTRICT = 0x118, P_WHERE = 0x11c,  // person: district (u8), whereabouts (u16; 61 + i = in army i)
    WHERE_ARMY = 61,
    D_FORCE = 0x10,                       // district: force (u8)
    A_GENERAL = 0x12, A_SOLDIERS = 0x16, A_FORMATION = 0x1b, A_HEX = 0x2a, A_TROOPS = 0x58,
    H_TERRAIN = 0x10,                     // hex: terrain code (u8)
    MAP_W = 220,
};
enum { F_YANXING = 3 };                   // formation 雁行
enum { ACT_MOVE = 1, ACT_WAIT = 13, TT_HEX = 9 };

struct Native {
    uint8_t *base;
    uint8_t **rootPtr;
    void **progVt;
    void *progUpdate;                     // original CProgressState::Update
    void **vtArmy, **vtPerson, **vtHex, **vtDistrict, **vtWorld, **vtCity, **vtObject;
    uint32_t offLock, offAction, offTType, offTId;
    uint8_t actMax;
};
static Native g_n;
static volatile LONG g_installState = 0;  // 0 not tried, 1 hooked, 2 failed
static bool g_dataOk = false, g_dataBad = false;
static uint32_t g_lastDate = 0;

static inline uint16_t U16(const uint8_t *p, uint32_t o) { uint16_t v; memcpy(&v, p + o, 2); return v; }
static inline uint8_t *Ptr(const uint8_t *p, uint32_t o) { uint8_t *v; memcpy(&v, p + o, 8); return v; }
static inline uint8_t *Obj(uint8_t *root, uint32_t arr, int i) { return Ptr(root, arr + 8u * (uint32_t)i); }
static inline bool IsA(const uint8_t *obj, void **vt) { return obj && Ptr(obj, 0) == (uint8_t *)vt; }

// the game's own hex distance (2acc20): index = row * 220 + column, columns offset odd/even
static int HexDist(int a, int b) {
    int y1 = a / MAP_W, x1 = a % MAP_W, y2 = b / MAP_W, x2 = b % MAP_W;
    int dx = abs(x2 - x1);
    int dyy = abs(2 * (y2 - y1) - (x1 % 2 == 0) + (x2 % 2 == 0));
    return dyy < dx ? dx : (dyy - dx) / 2 + dx;
}

// first object of an array, checked by RTTI
static bool ArrayIs(uint8_t *root, uint32_t arr, int n, void **vt, int from) {
    for (int i = from; i < n; i++) {
        uint8_t *o = Obj(root, arr, i);
        if (o) return IsA(o, vt);
    }
    return false;
}

static bool CheckData(uint8_t *root) {
    if (g_dataOk) return true;
    if (g_dataBad) return false;
    uint8_t *st = Ptr(root, ROOT_STATUS);
    const wchar_t *bad = !IsA(st, g_n.vtWorld) ? L"status" :
                         !ArrayIs(root, ROOT_PERSON, N_PERSON, g_n.vtPerson, 1) ? L"person" :
                         !ArrayIs(root, ROOT_DISTRICT, N_DISTRICT, g_n.vtDistrict, 1) ? L"district" :
                         !ArrayIs(root, ROOT_HEX, N_HEX, g_n.vtHex, 0) ? L"hex" :
                         !ArrayIs(root, ROOT_ARMY, N_ARMY, g_n.vtArmy, 1) ? L"army" :
                         !ArrayIs(root, ROOT_CITY, N_CITY, g_n.vtCity, 1) ? L"city" :
                         !ArrayIs(root, ROOT_OBJECT, N_OBJECT, g_n.vtObject, 1) ? L"object" : nullptr;
    if (bad) {
        g_dataBad = true;
        logf(L"data layout check failed (%ls objects are not the expected class); features off", bad);
        return false;
    }
    g_dataOk = true;
    logf(L"data layout ok (status, persons, districts, hexes, armies, cities, map objects)");
    return true;
}

// ---- the day's armies (shared by the features) ------------------------------------------------------
struct Army {
    uint8_t *p;
    int slot, force, hex, form, troops;
};
struct Mem {                              // per army slot: the order before we changed it, and ours
    uint16_t gen;
    uint8_t mod;
    uint8_t pAct, pType;
    uint16_t pId;
    uint8_t wAct, wType;
    uint16_t wId;
    uint8_t owner;                        // which feature changed it (g_owner at the time)
};
static int g_owner = 0;                   // the feature running now: 1 stance, 2 pincer, 3 defend, 4 contact
static Army g_armies[N_ARMY];
static int g_nArmies = 0;
static Mem g_mem[N_ARMY];
static uint8_t g_occ[N_HEX];
static uint8_t g_troopsN[N_ARMY];
static bool g_walk[256];
static bool g_walkSeeded = false;
enum { TT_ARMY = 27, ACT_ATTACK_ARMY = 7 };

static bool IsShip(int form) { return form == 11 || form == 12 || form == 13 || form == 17 || form == 18 || form == 20; }
// close-combat land formations: 鱼鳞 锋矢 方圆 鹤翼 长蛇 锥行 四夷
static bool IsMelee(int form) { return (form >= 1 && form <= 7 && form != F_YANXING) || form == 14; }

static int Terrain(uint8_t *root, int hex) {
    uint8_t *h = Obj(root, ROOT_HEX, hex);
    return h ? h[H_TERRAIN] : -1;
}
static bool Walkable(uint8_t *root, int hex) {
    if (hex < 0 || hex >= (int)N_HEX || g_occ[hex]) return false;
    int t = Terrain(root, hex);
    return t >= 0 && g_walk[t];
}

// doubled-row coordinates: Y = 2 * row + (column even); the six neighbours differ by (0, ±2) or (±1, ±1)
static int HexY(int h) { int x = h % MAP_W; return 2 * (h / MAP_W) + (x % 2 == 0); }
static int HexAt(int x, int Y) {
    if (x < 0 || x >= MAP_W) return -1;
    int e = x % 2 == 0, y2 = Y - e;
    if (y2 < 0 || (y2 & 1)) return -1;
    int y = y2 / 2;
    return y < MAP_W ? y * MAP_W + x : -1;
}
// the hex on the other side of e from its neighbour a
static int Opposite(int a, int e) { return HexAt(2 * (e % MAP_W) - a % MAP_W, 2 * HexY(e) - HexY(a)); }

static void CollectArmies(uint8_t *root) {
    if (!g_walkSeeded) {                  // terrain codes seen under land armies (live game, 2026-11-12);
        const int seed[] = {1, 2, 3, 5, 9, 10, 11, 12, 14, 15, 18};  // more are learned below
        for (int t : seed) g_walk[t] = true;
        g_walkSeeded = true;
    }
    memset(g_occ, 0, sizeof g_occ);
    memset(g_troopsN, 0, sizeof g_troopsN);
    int n = 0;
    for (int i = 1; i < (int)N_ARMY; i++) {
        uint8_t *p = Obj(root, ROOT_ARMY, i);
        if (!p) continue;
        int gen = U16(p, A_GENERAL);
        if (gen <= 0 || gen >= (int)N_PERSON) continue;
        uint8_t *pp = Obj(root, ROOT_PERSON, gen);
        if (!pp || U16(pp, P_WHERE) != WHERE_ARMY + i) continue;
        int dist = pp[P_DISTRICT];
        uint8_t *dp = dist > 0 && dist < (int)N_DISTRICT ? Obj(root, ROOT_DISTRICT, dist) : nullptr;
        int hex = U16(p, A_HEX);
        if (hex >= (int)N_HEX) continue;
        Army &a = g_armies[n++];
        a = {p, i, dp ? dp[D_FORCE] : 0, hex, p[A_FORMATION], U16(p, A_TROOPS)};
        g_occ[hex] = 1;
        if (a.troops > 0 && a.troops < (int)N_ARMY && g_troopsN[a.troops] < 255) g_troopsN[a.troops]++;
        if (!IsShip(a.form)) {
            int t = Terrain(root, hex);
            if (t >= 0 && !g_walk[t]) {
                g_walk[t] = true;
                logf(L"walkable terrain learned: %d", t);
            }
        }
    }
    g_nArmies = n;
}

// a computer army of the forces under test, with its memory brought up to date: a new order from the
// tree or the game since ours replaces what we remember. nullptr: not ours to touch.
static bool OursToTouch(int force, int player) {
    if (!force || (player && force == player)) return false;
    if (g_ab == 1 && !(force & 1)) return false;
    if (g_ab == 2 && (force & 1)) return false;
    return true;
}
static Mem *Track(const Army &a, int player) {
    if (!OursToTouch(a.force, player)) return nullptr;
    Mem &m = g_mem[a.slot];
    uint16_t gen = U16(a.p, A_GENERAL);
    if (m.gen != gen) m = Mem{gen, 0, 0, 0, 0, 0, 0, 0};
    uint8_t act = a.p[g_n.offAction], tt = a.p[g_n.offTType];
    uint16_t tid = U16(a.p, g_n.offTId);
    if (m.mod && (act != m.wAct || tt != m.wType || tid != m.wId)) m.mod = 0;
    if (!m.mod) { m.pAct = act; m.pType = tt; m.pId = tid; }
    return &m;
}

static bool MultiTroops(const Army &a) { return a.troops > 0 && a.troops < (int)N_ARMY && g_troopsN[a.troops] >= 2; }
// orders we may override: move (1, 14), attack city/gate/object (2-6), attack army (7), wait (13)
static bool FieldOrder(int act) { return (act >= 1 && act <= 7) || act == ACT_WAIT || act == 14; }

static void Order(uint8_t *a, Mem &m, int act, int type, int id) {
    a[g_n.offAction] = (uint8_t)act;
    a[g_n.offTType] = (uint8_t)type;
    uint16_t v = (uint16_t)id;
    memcpy(a + g_n.offTId, &v, 2);
    m.wAct = (uint8_t)act;
    m.wType = (uint8_t)type;
    m.wId = v;
    m.mod = 1;
    m.owner = (uint8_t)g_owner;
}
// not changed by another feature
static bool Mine(const Mem *m) { return !m->mod || m->owner == g_owner; }
static void Restore(uint8_t *a, Mem &m) {
    Order(a, m, m.pAct, m.pType, m.pId);
    m.mod = 0;
}

static int NearestEnemy(int hex, int force) {
    int best = 99;
    for (int k = 0; k < g_nArmies; k++) {
        const Army &e = g_armies[k];
        if (e.force == force) continue;
        int d = HexDist(hex, e.hex);
        if (d < best) best = d;
    }
    return best;
}

// ---- stance (雁行站位) -----------------------------------------------------------------------------
// free walkable hex 1-2 away, 2+ from every enemy (best: exactly 2 from the nearest, next to friends,
// short move); -1 if none. Used both to step back from contact and to take a firing position.
static int FiringHex(uint8_t *root, const Army &a) {
    int y = a.hex / MAP_W, x = a.hex % MAP_W;
    int best = -1, bestScore = -1000000;
    for (int dy = -2; dy <= 2; dy++)
        for (int dx = -2; dx <= 2; dx++) {
            int yy = y + dy, xx = x + dx;
            if (yy < 0 || yy >= MAP_W || xx < 0 || xx >= MAP_W) continue;
            int h = yy * MAP_W + xx;
            int move = HexDist(a.hex, h);
            if (move < 1 || move > 2 || !Walkable(root, h)) continue;
            int de = NearestEnemy(h, a.force);
            if (de < 2) continue;
            int friends = 0;
            for (int k = 0; k < g_nArmies; k++)
                if (g_armies[k].force == a.force && g_armies[k].slot != a.slot && HexDist(h, g_armies[k].hex) == 1) friends++;
            int score = (de == 2 ? 100 : de == 3 ? 50 : 10) + 15 * (friends < 2 ? friends : 2) - 8 * move;
            if (score > bestScore) { bestScore = score; best = h; }
        }
    return best;
}

static int BestAdjacentEnemy(const Army &a);

static void DayStance(uint8_t *root, int player, uint32_t date) {
    g_owner = 1;
    int back = 0, hold = 0, restore = 0, stuck = 0, seen = 0, aim = 0;
    for (int k = 0; k < g_nArmies; k++) {
        Army &a = g_armies[k];
        if (a.form != F_YANXING) continue;
        Mem *m = Track(a, player);
        if (!m || !Mine(m) || a.p[g_n.offLock]) continue;
        int de = NearestEnemy(a.hex, a.force);
        if (de <= 2) seen++;
        if (!FieldOrder(m->pAct)) {           // v2: every 雁行 (v1 only those in troops of 2+ armies)
            if (m->mod) { Restore(a.p, *m); restore++; }
            continue;
        }
        auto holdHere = [&]() {
            if (!(m->mod && m->wAct == ACT_WAIT && m->wId == a.hex)) { Order(a.p, *m, ACT_WAIT, TT_HEX, a.hex); hold++; }
        };
        if (de <= 1 && !g_contact) {
            // v3 (the default): try to step back; armies in contact mostly cannot move (39 of 40 stayed)
            int h = FiringHex(root, a);
            if (h >= 0) { Order(a.p, *m, ACT_MOVE, TT_HEX, h); back++; }
            else stuck++;
        } else if (de <= 1) {
            // with the contact feature (experiment): fight the enemy next to it instead of standing there
            // under a move order. One year with it was not better than v3 (multi-army exchange 1.05 vs 1.36)
            int t = BestAdjacentEnemy(a);
            if (t >= 0) {
                int slot = g_armies[t].slot;
                if (!(m->mod && m->wAct == ACT_ATTACK_ARMY && m->wId == slot)) {
                    Order(a.p, *m, ACT_ATTACK_ARMY, TT_ARMY, slot);
                    back++;
                }
            } else {
                stuck++;
            }
        } else if (de == 2) {
            holdHere();
        } else if (de <= 4) {
            // v3: an order that walks it into contact (the tree's "follow the 本队", "attack army X", which
            // run once a 旬 and cover 3+ hexes a day) is replaced by a firing position 2 from the enemy
            int h = FiringHex(root, a);
            if (h >= 0 && NearestEnemy(h, a.force) == 2) {
                if (!(m->mod && m->wAct == ACT_MOVE && m->wId == h)) { Order(a.p, *m, ACT_MOVE, TT_HEX, h); aim++; }
            } else if (m->mod) {
                Restore(a.p, *m);
                restore++;
            }
        } else if (m->mod) {
            Restore(a.p, *m);
            restore++;
        }
    }
    if (back || hold || restore || stuck || aim)
        logf(L"%u-%u-%u stance: 雁行 near enemies %d; back or fight in contact %d, no room %d, hold %d, firing position %d, restore %d",
             date / 10000, date / 100 % 100, date % 100, seen, back, stuck, hold, aim, restore);
}

// ---- pincer (夹击绕侧) -----------------------------------------------------------------------------
// One of our close-combat armies A touches an enemy land army E and none of ours is on E's far side yet:
// send a free close-combat army B of the same troops (touching no enemy, at most 4 hexes from E, and no
// farther from the far-side hex than from E) to the hex opposite A around E (or one of the two next to it
// that also touch E), then attack E from there; once B touches any enemy it attacks it (v2).
// B's earlier order comes back when E is gone or no longer held by one of ours.
static int FarSide(uint8_t *root, int a, int e, int from) {
    int o = Opposite(a, e);
    if (o < 0) return -1;
    int cand[3] = {o, -1, -1}, k = 1;
    static const int nb[6][2] = {{0, 2}, {0, -2}, {1, 1}, {1, -1}, {-1, 1}, {-1, -1}};
    for (int i = 0; i < 6 && k < 3; i++) {    // the two hexes next to o that also touch e
        int h = HexAt(o % MAP_W + nb[i][0], HexY(o) + nb[i][1]);
        if (h >= 0 && h != e && HexDist(h, e) == 1 && HexDist(h, a) == 2) cand[k++] = h;
    }
    int best = -1, bestD = 99;
    for (int i = 0; i < 3; i++) {
        int h = cand[i];
        if (h < 0 || (h != from && !Walkable(root, h))) continue;
        int d = HexDist(from, h) + (i ? 1 : 0);   // the true opposite first
        if (d < bestD) { bestD = d; best = h; }
    }
    return best;
}

static bool OurOnFarSide(int force, int a, int e) {
    for (int k = 0; k < g_nArmies; k++) {
        const Army &f = g_armies[k];
        if (f.force == force && f.hex != a && HexDist(f.hex, e) == 1 && HexDist(f.hex, a) == 2) return true;
    }
    return false;
}

// the enemy (index into g_armies) next to hex h that one of ours other than `self` holds; -1 if none
static int HeldEnemyNear(int h, int force, int self) {
    for (int j = 0; j < g_nArmies; j++) {
        const Army &e = g_armies[j];
        if (e.force == force || IsShip(e.form) || HexDist(e.hex, h) != 1) continue;
        for (int i = 0; i < g_nArmies; i++) {
            const Army &f = g_armies[i];
            if (f.force == force && f.slot != self && IsMelee(f.form) && HexDist(f.hex, e.hex) == 1) return j;
        }
    }
    return -1;
}

static void DayPincer(uint8_t *root, int player, uint32_t date) {
    g_owner = 2;
    static uint8_t used[N_ARMY];
    memset(used, 0, sizeof used);
    int sent = 0, attack = 0, restore = 0, chances = 0;
    // 1. armies sent earlier: arrived -> attack that enemy; enemy gone or no longer held -> restore
    for (int k = 0; k < g_nArmies; k++) {
        Army &b = g_armies[k];
        if (!IsMelee(b.form)) continue;
        Mem *m = Track(b, player);
        if (!m || !m->mod || m->owner != g_owner || m->wAct != ACT_MOVE || b.p[g_n.offLock]) continue;
        used[b.slot] = 1;
        int t = HeldEnemyNear(m->wId, b.force, b.slot);
        // in contact with someone (the target, or another enemy on the way): a move order would leave it
        // standing there (armies in contact do not move), so attack whoever it touches, the target first
        int touch = -1;
        for (int j = 0; j < g_nArmies; j++) {
            const Army &e = g_armies[j];
            if (e.force == b.force || HexDist(b.hex, e.hex) != 1) continue;
            if (touch < 0 || j == t) touch = j;
        }
        if (touch >= 0) {                         // an ordinary order from now on
            Order(b.p, *m, ACT_ATTACK_ARMY, TT_ARMY, g_armies[touch].slot);
            m->mod = 0;
            m->pAct = ACT_ATTACK_ARMY;
            m->pType = TT_ARMY;
            m->pId = (uint16_t)g_armies[touch].slot;
            attack++;
            continue;
        }
        if (t < 0) { Restore(b.p, *m); restore++; }
    }
    // 2. new pincers
    for (int k = 0; k < g_nArmies; k++) {
        const Army &a = g_armies[k];
        if (!IsMelee(a.form) || !MultiTroops(a) || !OursToTouch(a.force, player)) continue;
        for (int j = 0; j < g_nArmies; j++) {
            const Army &e = g_armies[j];
            if (e.force == a.force || IsShip(e.form) || HexDist(a.hex, e.hex) != 1) continue;
            if (OurOnFarSide(a.force, a.hex, e.hex)) continue;
            chances++;
            int best = -1, bestHex = -1, bestD = 99;
            for (int i = 0; i < g_nArmies; i++) {
                const Army &b = g_armies[i];
                if (b.slot == a.slot || b.troops != a.troops || !IsMelee(b.form) || used[b.slot]) continue;
                if (b.p[g_n.offLock] || NearestEnemy(b.hex, b.force) <= 1 || HexDist(b.hex, e.hex) > 4) continue;
                Mem *m = Track(b, player);
                if (!m || !Mine(m) || !FieldOrder(m->pAct)) continue;
                int h = FarSide(root, a.hex, e.hex, b.hex);
                // only armies already level with or beyond the enemy: going round it from behind A would
                // run into it on the near side and stick there (v1: 55 sent, 12 arrived)
                if (h < 0 || HexDist(b.hex, h) > HexDist(b.hex, e.hex)) continue;
                int dh = HexDist(b.hex, h);
                if (dh < bestD) { bestD = dh; best = i; bestHex = h; }
            }
            if (best < 0) continue;
            Army &b = g_armies[best];
            Order(b.p, g_mem[b.slot], ACT_MOVE, TT_HEX, bestHex);
            used[b.slot] = 1;
            sent++;
            break;                                // one pincer per holding army a day
        }
    }
    if (sent || attack || restore)
        logf(L"%u-%u-%u pincer: held enemies without a far side %d; sent %d, attack %d, restore %d", date / 10000,
             date / 100 % 100, date % 100, chances, sent, attack, restore);
}

// ---- defend (守城迎击) ------------------------------------------------------------------------------
// R1 engines first: a computer close-combat army not in contact attacks the nearest enemy siege engine
//    (井阑 冲车 投石) within 3 hexes; its earlier order comes back when no engine is that close.
// R2 keep defending: an army ordered back to its own city (action 2 or 11) while enemies are within 5 hexes
//    of that city attacks them instead (engines first, else the nearest to it), as long as it has 1500+
//    soldiers and our armies within 6 hexes of the city have at least 60% of the attackers' soldiers;
//    otherwise, or once no enemy is near the city, it goes home as ordered.
enum { ACT_GO_CITY = 2, ACT_ENTER_CITY = 11, TT_CITY = 5 };   // going home is mostly 2 (to the city), sometimes 11
static bool IsEngine(int form) { return form == 8 || form == 9 || form == 10; }

static int CityHex(uint8_t *root, int c) {
    if (c <= 0 || c >= (int)N_CITY) return -1;
    uint8_t *cp = Obj(root, ROOT_CITY, c);
    if (!cp) return -1;
    int oi = U16(cp, C_OBJECT);
    uint8_t *op = oi > 0 && oi < (int)N_OBJECT ? Obj(root, ROOT_OBJECT, oi) : nullptr;
    int h = op ? U16(op, O_HEX) : -1;
    return h >= 0 && h < (int)N_HEX ? h : -1;
}
static int CityForce(uint8_t *root, int c) {
    if (c <= 0 || c >= (int)N_CITY) return 0;
    uint8_t *cp = Obj(root, ROOT_CITY, c);
    int d = cp ? cp[C_DISTRICT] : 0;
    uint8_t *dp = d > 0 && d < (int)N_DISTRICT ? Obj(root, ROOT_DISTRICT, d) : nullptr;
    return dp ? dp[D_FORCE] : 0;
}

static void DayDefend(uint8_t *root, int player, uint32_t date) {
    g_owner = 3;
    int engines = 0, defend = 0, home = 0, restore = 0;
    for (int k = 0; k < g_nArmies; k++) {
        Army &a = g_armies[k];
        if (!IsMelee(a.form)) continue;
        Mem *m = Track(a, player);
        if (!m || !Mine(m) || a.p[g_n.offLock]) continue;
        int target = -1;
        bool r2 = false;
        if ((m->pAct == ACT_GO_CITY || m->pAct == ACT_ENTER_CITY) && m->pType == TT_CITY && CityForce(root, m->pId) == a.force) {
            // R2: going home to a besieged city of ours
            int ch = CityHex(root, m->pId);
            if (ch >= 0) {
                int foes = 0, ours = 0, eng = -1, closest = -1, engD = 99, closestD = 99;
                for (int j = 0; j < g_nArmies; j++) {
                    const Army &e = g_armies[j];
                    int dc = HexDist(e.hex, ch);
                    if (e.force == a.force) {
                        if (dc <= 6) ours += U16(e.p, A_SOLDIERS);
                        continue;
                    }
                    if (dc > 5 || IsShip(e.form)) continue;
                    foes += U16(e.p, A_SOLDIERS);
                    int d = HexDist(a.hex, e.hex);
                    if (IsEngine(e.form) && d < engD) { engD = d; eng = j; }
                    if (d < closestD) { closestD = d; closest = j; }
                }
                if (foes && U16(a.p, A_SOLDIERS) >= 1500 && ours * 10 >= foes * 6) {
                    target = eng >= 0 ? eng : closest;
                    r2 = true;
                }
            }
        } else if (FieldOrder(m->pAct) && NearestEnemy(a.hex, a.force) > 1) {
            // R1: an enemy siege engine within 3
            int best = 99;
            for (int j = 0; j < g_nArmies; j++) {
                const Army &e = g_armies[j];
                if (e.force == a.force || !IsEngine(e.form)) continue;
                int d = HexDist(a.hex, e.hex);
                if (d <= 3 && d < best) { best = d; target = j; }
            }
        }
        if (target >= 0) {
            int slot = g_armies[target].slot;
            if (!(m->mod && m->wAct == ACT_ATTACK_ARMY && m->wType == TT_ARMY && m->wId == slot)) {
                Order(a.p, *m, ACT_ATTACK_ARMY, TT_ARMY, slot);
                if (r2) defend++; else engines++;
            }
        } else if (m->mod) {
            Restore(a.p, *m);
            if (m->pType == TT_CITY && CityForce(root, m->pId) == a.force) home++; else restore++;
        }
    }
    if (engines || defend || home || restore)
        logf(L"%u-%u-%u defend: at engines %d, back out at a besieged city %d, home %d, restore %d", date / 10000,
             date / 100 % 100, date % 100, engines, defend, home, restore);
}

// ---- contact (接战目标) -----------------------------------------------------------------------------
// An army touching an enemy cannot move, so a move order (action 1/14: the trees' gather, catch-up) leaves
// it standing there: one year of records, exchange 0.3-0.9 on such days against 1.3-2.2 for armies that
// attack the enemy next to them. Such an army attacks an adjacent enemy instead, and gets its order back
// once nothing touches it. Which enemy: one that another of ours holds from the opposite side (that makes
// the pincer), else a siege engine or 雁行, else the weakest.
static int BestAdjacentEnemy(const Army &a) {
    int best = -1, bestScore = -1000000;
    for (int j = 0; j < g_nArmies; j++) {
        const Army &e = g_armies[j];
        if (e.force == a.force || IsShip(e.form) || HexDist(a.hex, e.hex) != 1) continue;
        bool pincer = false;
        for (int i = 0; i < g_nArmies && !pincer; i++) {
            const Army &f = g_armies[i];
            pincer = f.force == a.force && f.slot != a.slot && HexDist(f.hex, e.hex) == 1 && HexDist(f.hex, a.hex) == 2;
        }
        int score = (pincer ? 100000 : 0) + (IsEngine(e.form) || e.form == F_YANXING ? 50000 : 0) -
                    (int)U16(e.p, A_SOLDIERS);
        if (score > bestScore) { bestScore = score; best = j; }
    }
    return best;
}

static void DayContact(uint8_t *root, int player, uint32_t date) {
    g_owner = 4;
    int attack = 0, restore = 0;
    for (int k = 0; k < g_nArmies; k++) {
        Army &a = g_armies[k];
        if (IsShip(a.form) || a.form == F_YANXING) continue;   // 雁行: stance does the same for them
        Mem *m = Track(a, player);
        if (!m || !Mine(m) || a.p[g_n.offLock]) continue;
        int t = BestAdjacentEnemy(a);
        if (t >= 0 && (m->pAct == ACT_MOVE || m->pAct == 14)) {
            int slot = g_armies[t].slot;
            if (!(m->mod && m->wAct == ACT_ATTACK_ARMY && m->wId == slot)) {
                Order(a.p, *m, ACT_ATTACK_ARMY, TT_ARMY, slot);
                attack++;
            }
        } else if (t < 0 && m->mod) {
            Restore(a.p, *m);
            restore++;
        }
    }
    if (attack || restore)
        logf(L"%u-%u-%u contact: move orders in contact turned into attacks %d, restore %d", date / 10000,
             date / 100 % 100, date % 100, attack, restore);
}

// ---- the daily hook ------------------------------------------------------------------------------
static uint64_t HkProgressUpdate(uint8_t *self) {
    uint64_t r = ((uint64_t(*)(void *))g_n.progUpdate)(self);
    uint8_t *root = *g_n.rootPtr;
    if (!root || g_dataBad) return r;
    uint8_t *st = Ptr(root, ROOT_STATUS);
    if (!st) return r;
    uint32_t date = U16(st, ST_DATE) * 10000u + st[ST_DATE + 2] * 100u + st[ST_DATE + 3];
    if (date == g_lastDate) return r;
    // a save was loaded (date went back, or jumped more than a month): forget the orders we remember
    auto days = [](uint32_t d) { return (int)(d / 10000) * 372 + (int)(d / 100 % 100) * 31 + (int)(d % 100); };
    if (g_lastDate && (date < g_lastDate || days(date) - days(g_lastDate) > 31)) {
        memset(g_mem, 0, sizeof g_mem);
        logf(L"date %u -> %u: a save was loaded, remembered orders cleared", g_lastDate, date);
    }
    g_lastDate = date;
    if (!CheckData(root)) return r;
    int player = st[ST_PLAYER];
    CollectArmies(root);
    if (g_stance) DayStance(root, player, date);
    if (g_pincer) DayPincer(root, player, date);
    if (g_defend) DayDefend(root, player, date);
    if (g_contact) DayContact(root, player, date);
    return r;
}

static bool WriteSlot(void **slot, void *fn, void **orig) {
    DWORD old;
    if (!VirtualProtect(slot, sizeof(void *), PAGE_READWRITE, &old)) return false;
    if (orig) *orig = *slot;
    InterlockedExchangePointer(slot, fn);
    VirtualProtect(slot, sizeof(void *), old, &old);
    return true;
}

static const wchar_t *Locate(Native &a, uint8_t *base) {
    a.base = base;
    uint8_t *text;
    size_t tn;
    if (!Section(a.base, ".text", &text, &tn)) return L"no .text";
    SigHit h;
    if (!SigScan(text, tn, SIG_data_root, &h)) return L"data_root not found";
    if (SigU32(h, F_data_root_city_arr) != 0xdd38) return L"data_root: city array offset changed (data layout differs)";
    a.rootPtr = (uint8_t **)SigRel(h, F_data_root_root_ptr);
    if (!SigScan(text, tn, SIG_army_setters, &h)) return L"army_setters not found";
    a.offLock = SigU32(h, F_army_setters_lock_off);
    if (SigU32(h, F_army_setters_lock_off2) != a.offLock) return L"army_setters: the two lock offsets differ";
    a.offAction = h.p[h.f[F_army_setters_action_off].at];
    a.actMax = h.p[h.f[F_army_setters_action_max].at];
    a.offTType = h.p[h.f[F_army_setters_ttype_off].at];
    a.offTId = h.p[h.f[F_army_setters_tid_off].at];
    if (a.actMax < 14) return L"army_setters: fewer actions than expected";
    a.progVt = VtableByRtti(a.base, kRtti_progress_vtable);
    if (!a.progVt) return L"CProgressState vtable not found";
    if (!SigAt((uint8_t *)a.progVt[SLOT_progress_vtable_update], SIG_progress_update, &h))
        return L"CProgressState slot 5 is not the expected Update";
    a.vtArmy = VtableByRtti(a.base, ".?AVCArmyUnitData@@");
    a.vtPerson = VtableByRtti(a.base, ".?AVCPersonData@@");
    a.vtHex = VtableByRtti(a.base, ".?AVCHexData@@");
    a.vtDistrict = VtableByRtti(a.base, ".?AVCDistrictData@@");
    a.vtWorld = VtableByRtti(a.base, ".?AVCWorldData@@");
    a.vtCity = VtableByRtti(a.base, ".?AVCCityData@@");
    a.vtObject = VtableByRtti(a.base, ".?AVCObjectData@@");
    if (!a.vtArmy || !a.vtPerson || !a.vtHex || !a.vtDistrict || !a.vtWorld || !a.vtCity || !a.vtObject)
        return L"data class vtables not found";
    return nullptr;
}

static void InstallDaily() {
    if (InterlockedCompareExchange(&g_installState, 2, 0) != 0) return;   // once; 2 until it succeeds
    DWORD t0 = GetTickCount();
    Native a{};
    const wchar_t *why = Locate(a, (uint8_t *)GetModuleHandleW(nullptr));
    if (!why) {
        g_n = a;
        if (!WriteSlot(&g_n.progVt[SLOT_progress_vtable_update], (void *)HkProgressUpdate, &g_n.progUpdate))
            why = L"vtable write failed";
    }
    if (why) {
        logf(L"daily layer NOT installed: %ls (AI features off, the game runs as usual)", why);
        return;
    }
    InterlockedExchange(&g_installState, 1);
    logf(L"daily layer installed in %lu ms: CProgressState::Update +%llx, root ptr +%llx, army lock +%x action +%x "
         L"target +%x/+%x; features:%ls; forces: %ls",
         GetTickCount() - t0, (unsigned long long)((uint8_t *)g_n.progUpdate - g_n.base),
         (unsigned long long)((uint8_t *)g_n.rootPtr - g_n.base), g_n.offLock, g_n.offAction, g_n.offTType,
         g_n.offTId, feature_names(), g_ab == 1 ? L"odd ids" : g_ab == 2 ? L"even ids" : L"all");
}

// ---- tree redirect --------------------------------------------------------------------------------
static bool is_tree_file(LPCWSTR p) {
    if (!p) return false;
    size_t n = wcslen(p);
    const wchar_t *tail = L"0000/bt_exce.bin";
    size_t m = wcslen(tail);
    if (n < m) return false;
    const wchar_t *s = p + n - m;
    for (size_t i = 0; i < m; i++) {
        wchar_t a = towlower(s[i]), b = tail[i];
        if (a == L'\\') a = L'/';
        if (a != b) return false;
    }
    return n == m || s[-1] == L'/' || s[-1] == L'\\';
}

static HANDLE WINAPI hook_CreateFileW(LPCWSTR name, DWORD acc, DWORD share, LPSECURITY_ATTRIBUTES sa,
                                      DWORD disp, DWORD flags, HANDLE tmpl) {
    LONG k = InterlockedIncrement(&g_opens);
    bool tree = is_tree_file(name) && !(acc & GENERIC_WRITE);
    if (tree && any_feature()) InstallDaily();      // the game's code is decrypted by now
    if (g_target[0] && tree) {
        HANDLE h = g_real(g_target, acc, share, sa, disp, flags, tmpl);
        DWORD err = GetLastError();
        InterlockedIncrement(&g_redirects);
        logf(L"redirect #%ld: %ls -> %ls : %ls (error %lu)", k, name, g_target,
             h != INVALID_HANDLE_VALUE ? L"ok" : L"FAILED", h != INVALID_HANDLE_VALUE ? 0UL : err);
        if (h != INVALID_HANDLE_VALUE) {
            SetLastError(err);
            return h;
        }
        logf(L"falling back to the original file");
    }
    if (k <= 40) logf(L"open #%ld: %ls", k, name ? name : L"(null)");
    return g_real(name, acc, share, sa, disp, flags, tmpl);
}

// Replace the IAT slot for dll!func in module `mod`. Returns the number of slots patched.
static int patch_iat(HMODULE mod, const char *func, void *hook) {
    auto base = (BYTE *)mod;
    auto dos = (IMAGE_DOS_HEADER *)base;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) return 0;
    auto nt = (IMAGE_NT_HEADERS *)(base + dos->e_lfanew);
    auto &dir = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!dir.VirtualAddress) return 0;
    int n = 0;
    for (auto imp = (IMAGE_IMPORT_DESCRIPTOR *)(base + dir.VirtualAddress); imp->Name; imp++) {
        if (!imp->OriginalFirstThunk) continue;
        auto names = (IMAGE_THUNK_DATA *)(base + imp->OriginalFirstThunk);
        auto slots = (IMAGE_THUNK_DATA *)(base + imp->FirstThunk);
        for (; names->u1.AddressOfData; names++, slots++) {
            if (IMAGE_SNAP_BY_ORDINAL(names->u1.Ordinal)) continue;
            auto ibn = (IMAGE_IMPORT_BY_NAME *)(base + names->u1.AddressOfData);
            if (strcmp((const char *)ibn->Name, func) != 0) continue;
            DWORD old;
            if (VirtualProtect(&slots->u1.Function, sizeof(void *), PAGE_READWRITE, &old)) {
                slots->u1.Function = (ULONG_PTR)hook;
                VirtualProtect(&slots->u1.Function, sizeof(void *), old, &old);
                n++;
            }
        }
    }
    return n;
}

static void init() {
    InitializeCriticalSection(&g_logcs);
    GetEnvironmentVariableW(L"SAN14_AI_LOG", g_logpath, MAX_PATH * 2);
    DWORD got = GetEnvironmentVariableW(L"SAN14_AI_BT", g_target, MAX_PATH * 2);
    if (!got || got >= MAX_PATH * 2) g_target[0] = 0;
    read_features();
    HMODULE kb = GetModuleHandleW(L"kernelbase.dll");
    g_real = kb ? (CreateFileW_t)GetProcAddress(kb, "CreateFileW") : nullptr;
    if (!g_real) g_real = (CreateFileW_t)GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "CreateFileW");
    if (!g_real) {
        logf(L"CreateFileW not found; doing nothing");
        return;
    }
    DWORD attr = g_target[0] ? GetFileAttributesW(g_target) : INVALID_FILE_ATTRIBUTES;
    logf(L"san14_ai.dll loaded in pid %lu; tree file %ls (%ls); daily features:%ls", GetCurrentProcessId(),
         g_target[0] ? g_target : L"(none)", attr == INVALID_FILE_ATTRIBUTES ? L"MISSING" : L"exists",
         feature_names());
    int a = patch_iat(GetModuleHandleW(nullptr), "CreateFileW", (void *)hook_CreateFileW);
    int b = patch_iat(GetModuleHandleW(L"kernel32.dll"), "CreateFileW", (void *)hook_CreateFileW);
    logf(L"hooked CreateFileW: game IAT %d slot(s), kernel32 IAT %d slot(s)", a, b);
}

extern "C" __declspec(dllexport) LONG san14_ai_redirects() { return g_redirects; }
extern "C" __declspec(dllexport) LONG san14_ai_daily_state() { return g_installState; }

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(inst);
        init();
    }
    return TRUE;
}
