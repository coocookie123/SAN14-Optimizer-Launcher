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
//    SAN14_AI_AB=odd|even limits the features to forces with odd / even ids (A/B tests); odd:defend+siege
//    limits only the features named after the colon (none if nothing follows it), the others act for all.
//
// Features:
//    stance  雁行站位: a 雁行 (ranged formation) stays out of melee. Each day:
//            enemy adjacent -> back off, best behind one of our armies (v5; v4 held there, which got fewer
//            雁行 out of contact); enemy exactly 2 away -> hold (it shoots by itself at range 2); enemy 3-4 away -> go to such a firing position instead of
//            walking into contact (v3); no enemy within 4 -> give back the order it had before we touched
//            it. Orders the tree or the game give later replace what we remember. Player armies are never
//            touched.
//    defend  守城迎击: see DayDefend.
//    target  攻击目标分配: which nearby enemy each close-combat army of a troops of 2+ attacks, every
//            day (see DayTarget).
//    siege   攻城会合: troops of one force going for the same city wait for each other outside it (see
//            DaySiege).
//    fu      占府: armies of siege and area-attack troops step on unguarded enemy 府 near their way, and fast
//            formations step on those around the siege target (see DayFu).
//    The later features (shore, ford, navaldef, gate, relief, siegelone, siegeprep, supplyguard, ...) are
//    described at their sections.
//    bonus   debug, changes nothing: every day calls the game's own 府加成 function for every army and logs
//            its value with the state of the army's area whenever either changes (see DayBonus).
// 3. trapnear 陷阱近身触发 (SAN14_TRAP_NEAR=1, independent of the AI features; see InstallTrapNear): when the game
//    reads the tree, the facility kinds' 「发动范围」 getter is hooked so that a trap goes off only when an enemy army
//    is within 1 hex of it (陷阱, 猛火陷阱, 落石 had 2), for every force; the effect range stays as it was.
//    SAN14_TRAP_LOG=1 logs every trap that goes off with the armies near it (tests).
// The pincer and contact experiments of 2026-11-12 were taken out (user: no forced pincers; the target
// assignment replaces them); their code is in aihook/archive/san14_ai_pincer_contact.cpp.
// 2026-11-16 (user): siegecomp, siegesplit, siegefight, cut and portguard were deleted: they made things worse or
// hardly ever came into play (BEHAVIOR_PLAN 第五十节). Their code is in git before that date.
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
static bool g_defend = false;
static bool g_assign = false;   // feature "target"
static bool g_siege = false;
static bool g_probe = false;              // debug: count tree probes MiscLogNode(99, 0, $key), see HkMiscLog
static bool g_bonus = false;              // debug: log the 府加成 of every army, see DayBonus
static bool g_fu = false;                 // 占府, see DayFu
static bool g_shore = false;              // 半渡而击, see DayShore
static bool g_ford = false;               // 渡江迎击, see HkStrength
static bool g_gate = false;               // 守关, see DayGate
static bool g_enough = false;             // 攻城凑够才出, see InstallDaily (siege_launch_check)
static bool g_scLog = false;              // debug: log the siege soldier assignment (HkAssign), change nothing
static bool g_siegeFu = false;            // 攻城门槛折进府加成, see SiegeFu
static bool g_waitFight = false;          // 贴敌待命改还手, see DayTarget
static bool g_prep = false;               // 攻城准备（城外集结、先占府）, see DaySiegePrep
static bool g_sguard = false;             // 攻城准备第二层（防断粮）, see DaySupplyGuard
static bool g_sgLog = false;              // debug: the same, logging the roads and threats only
static bool g_naval = false;              // 水战守港, see DayNavalDef
static bool g_relief = false;             // 援军继续守, see DayRelief
static bool g_slone = false;              // 攻城落单不单打, see DaySiegeLone
static thread_local int t_ford;           // set between the module's MiscLogNode(97) and (96) on this thread
static thread_local int t_reachHex = -1;  // gate v4: the retreat judgment's lead, inside the strength node after
static thread_local int t_reachForce = 0; // MiscLogNode(95); t_reachPend* hold it from the marker to that node
static thread_local int t_reachPend = -1, t_reachPendForce = 0;
static int g_ab = 0;                      // 0 every force, 1 odd ids only, 2 even ids only
static uint32_t g_abMask = ~0u;           // the features (bit = g_owner id) that g_ab limits

static int feature_owner(const wchar_t *t) {   // g_owner ids, see g_owner (8, 9, 13, 14, 16 were features taken out)
    return !wcscmp(t, L"stance") ? 1 : !wcscmp(t, L"defend") ? 3 : !wcscmp(t, L"target") ? 5
         : !wcscmp(t, L"siege") ? 6 : !wcscmp(t, L"fu") ? 7 : !wcscmp(t, L"shore") ? 10 : !wcscmp(t, L"ford") ? 11 : !wcscmp(t, L"gate") ? 12 : !wcscmp(t, L"siegefu") ? 15 : !wcscmp(t, L"waitfight") ? 17 : !wcscmp(t, L"siegeprep") ? 18 : !wcscmp(t, L"supplyguard") || !wcscmp(t, L"supplylog") ? 19 : !wcscmp(t, L"navaldef") ? 20 : !wcscmp(t, L"relief") ? 21 : !wcscmp(t, L"siegelone") ? 22 : -1;
}

static void read_features() {
    wchar_t buf[512];
    DWORD n = GetEnvironmentVariableW(L"SAN14_AI_FEATURES", buf, 512);
    if (n && n < 512) {
        for (wchar_t *ctx = nullptr, *t = wcstok(buf, L", ", &ctx); t; t = wcstok(nullptr, L", ", &ctx)) {
            if (!wcscmp(t, L"stance")) g_stance = true;
            else if (!wcscmp(t, L"defend")) g_defend = true;
            else if (!wcscmp(t, L"target")) g_assign = true;
            else if (!wcscmp(t, L"siege")) g_siege = true;
            else if (!wcscmp(t, L"probe")) g_probe = true;
            else if (!wcscmp(t, L"bonus")) g_bonus = true;
            else if (!wcscmp(t, L"fu")) g_fu = true;
            else if (!wcscmp(t, L"shore")) g_shore = true;
            else if (!wcscmp(t, L"ford")) g_ford = true;
            else if (!wcscmp(t, L"scomplog")) g_scLog = true;
            else if (!wcscmp(t, L"gate")) g_gate = true;
            else if (!wcscmp(t, L"siegeenough")) g_enough = true;
            else if (!wcscmp(t, L"siegefu")) g_siegeFu = true;
            else if (!wcscmp(t, L"waitfight")) g_waitFight = true;
            else if (!wcscmp(t, L"siegeprep")) g_prep = true;
            else if (!wcscmp(t, L"supplyguard")) g_sguard = true;
            else if (!wcscmp(t, L"supplylog")) g_sgLog = true;
            else if (!wcscmp(t, L"navaldef")) g_naval = true;
            else if (!wcscmp(t, L"relief")) g_relief = true;
            else if (!wcscmp(t, L"siegelone")) g_slone = true;
            else logf(L"unknown feature %ls (ignored)", t);
        }
    }
    n = GetEnvironmentVariableW(L"SAN14_AI_AB", buf, 512);
    if (n && n < 512) {
        wchar_t *colon = wcschr(buf, L':');
        if (colon) {
            *colon = 0;
            g_abMask = 0;
            for (wchar_t *ctx = nullptr, *t = wcstok(colon + 1, L"+, ", &ctx); t; t = wcstok(nullptr, L"+, ", &ctx)) {
                int o = feature_owner(t);
                if (o > 0) g_abMask |= 1u << o;
                else logf(L"A/B: unknown feature %ls (ignored)", t);
            }
        }
        g_ab = !wcscmp(buf, L"odd") ? 1 : !wcscmp(buf, L"even") ? 2 : 0;
    }
}

static bool any_feature() { return g_stance || g_defend || g_assign || g_siege || g_probe || g_bonus || g_fu || g_shore || g_ford || g_scLog || g_gate || g_enough || g_siegeFu || g_waitFight || g_prep || g_sguard || g_sgLog || g_naval || g_relief || g_slone; }
static const wchar_t *feature_names() {
    static wchar_t buf[320];
    buf[0] = 0;
    if (g_stance) wcscat(buf, L" stance");
    if (g_defend) wcscat(buf, L" defend");
    if (g_assign) wcscat(buf, L" target");
    if (g_siege) wcscat(buf, L" siege");
    if (g_probe) wcscat(buf, L" probe");
    if (g_bonus) wcscat(buf, L" bonus");
    if (g_fu) wcscat(buf, L" fu");
    if (g_shore) wcscat(buf, L" shore");
    if (g_ford) wcscat(buf, L" ford");
    if (g_scLog) wcscat(buf, L" scomplog");
    if (g_gate) wcscat(buf, L" gate");
    if (g_enough) wcscat(buf, L" siegeenough");
    if (g_siegeFu) wcscat(buf, L" siegefu");
    if (g_waitFight) wcscat(buf, L" waitfight");
    if (g_prep) wcscat(buf, L" siegeprep");
    if (g_sguard) wcscat(buf, L" supplyguard");
    if (g_sgLog) wcscat(buf, L" supplylog");
    if (g_naval) wcscat(buf, L" navaldef");
    if (g_relief) wcscat(buf, L" relief");
    if (g_slone) wcscat(buf, L" siegelone");
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
    ROOT_TROOPS = 0x7f8d8, N_TROOPS = 501,  // CTroopsData: +0x14 task (u16)
    T_PURPOSE = 0x14,
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
    void **vtArmy, **vtPerson, **vtHex, **vtDistrict, **vtWorld, **vtCity, **vtObject, **vtTroops, **vtGate;
    uint32_t offLock, offAction, offTType, offTId;
    uint8_t actMax;
    void **logVt;                         // CMiscLogNode vtable (optional: only the tree probes need it)
    void *logExec;                        // its original execute (slot 16)
    uint32_t logTypeOff;                  // node field of the first arg (type); the key is 8 bytes after it
    void *bonusFn;                        // 府加成 2b2b30 (optional: only the bonus log needs it)
    void **vtArea;
    uint8_t *assignFn;                    // siege departures' soldier assignment 43750 (optional: scomplog)
    void *statFn;                         // a person's ability 231590 (optional: scomplog)
    uint8_t *createFn;                    // create an army from a departure request 1ed8e0 (optional: gate)
    uint8_t *strengthFn;                  // an army's strength b6a10 (optional: ford)
    uint8_t *launchJbe;                   // siege departure: "short of strength, no command books left -> go anyway" (siegeenough)
    uint8_t *depCreateRet;                // CDepartureNode's call of 1ed8e0: its return address (gate)
    void *bbGet, *bbObj;                  // the tree's blackboard: 113a3a0(ctx) and b2ae60(bb, key, &obj, 0) (gate)
    void *bbInt, *bbSet;                  // b2acf0(bb, key, &int, 0) and b2b380(bb, key, int) (siegefu)
    void **saVt;                          // CGetHexAroundFightingStrengthNode vtable (gate)
    uint8_t *setTargetFn;                 // the army set-target function 2ecfe0 (army_setters hit + 60) (gate)
    void *saExec;                         // its original execute (slot 16)
    uint32_t fmtMaskOff;                  // person: formations he can use (bit n = formation n)
    void *unitBaseFn, *unitAtkFn, *unitDefFn; // an army's battle base 2aaff0, 攻军 2b4500, 防御 2ac140 (navaldef)
    int *dmgC1;                           // the damage function's "constant + 攻军 - 防御" constant (navaldef)
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
                         !ArrayIs(root, ROOT_OBJECT, N_OBJECT, g_n.vtObject, 1) ? L"object" :
                         !ArrayIs(root, ROOT_TROOPS, N_TROOPS, g_n.vtTroops, 1) ? L"troops" :
                         !ArrayIs(root, 0xded8, 11, g_n.vtGate, 1) ? L"gate" : nullptr;
    if (bad) {
        g_dataBad = true;
        logf(L"data layout check failed (%ls objects are not the expected class); features off", bad);
        return false;
    }
    g_dataOk = true;
    logf(L"data layout ok (status, persons, districts, hexes, armies, cities, map objects, troops, gates)");
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
static int g_owner = 0;                   // the feature running now: 1 stance, 3 defend, 5 target, 6 siege, 7 fu, 10 shore, ..., 19 supplyguard
static Army g_armies[N_ARMY];
static int g_nArmies = 0;
static Mem g_mem[N_ARMY];
static int16_t g_bySlot[N_ARMY];          // army slot -> index in g_armies, -1 none
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

static void CollectArmies(uint8_t *root) {
    if (!g_walkSeeded) {                  // terrain codes seen under land armies (live game, 2026-11-12);
        const int seed[] = {1, 2, 3, 5, 9, 10, 11, 12, 14, 15, 18};  // more are learned below
        for (int t : seed) g_walk[t] = true;
        g_walkSeeded = true;
    }
    memset(g_occ, 0, sizeof g_occ);
    memset(g_troopsN, 0, sizeof g_troopsN);
    memset(g_bySlot, 0xff, sizeof g_bySlot);
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
        g_bySlot[i] = (int16_t)n;
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
    if (g_ab && (g_abMask >> g_owner & 1)) {
        if (g_ab == 1 && !(force & 1)) return false;
        if (g_ab == 2 && (force & 1)) return false;
    }
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
static int NearestEnemyIdx(int hex, int force, int *dist) {
    int best = -1, bd = 99;
    for (int k = 0; k < g_nArmies; k++) {
        const Army &e = g_armies[k];
        if (e.force == force) continue;
        int d = HexDist(hex, e.hex);
        if (d < bd) { bd = d; best = k; }
    }
    *dist = bd;
    return best;
}

// gate v5: our side of each gate attacked yesterday (DayGate writes it, after stance); stance keeps a 雁行 standing on
// that side from picking a hex on the other side (in 203 its back-off took 黄祖 out through 武关). All 0 without gate.
enum { GT_SIDE = 6 };
static uint8_t g_gtSide[N_HEX];           // the gate's index for hexes of our side within GT_SIDE of it
static int g_gtHex[16];                   // the gate's hex by index
static bool OtherSideOfGate(int from, int to) {
    int sg = g_gtSide[from];
    return sg && g_gtSide[to] != sg && HexDist(to, g_gtHex[sg]) <= GT_SIDE;
}

// free walkable hex 1-2 away, 2+ from every enemy; -1 if none.
//  retreat (an enemy touches it): safety first, a hex with one of our armies between it and the nearest
//    enemy (that army is next to the hex and nearer to the enemy), then next to friends, out of reach;
//  firing position (enemy 3-4 away): exactly 2 from the nearest enemy (it shoots at 2), then behind or next
//    to friends, short move.
static int FiringHex(uint8_t *root, const Army &a, bool retreat) {
    int y = a.hex / MAP_W, x = a.hex % MAP_W;
    int best = -1, bestScore = -1000000;
    for (int dy = -2; dy <= 2; dy++)
        for (int dx = -2; dx <= 2; dx++) {
            int yy = y + dy, xx = x + dx;
            if (yy < 0 || yy >= MAP_W || xx < 0 || xx >= MAP_W) continue;
            int h = yy * MAP_W + xx;
            int move = HexDist(a.hex, h);
            if (move < 1 || move > 2 || !Walkable(root, h) || OtherSideOfGate(a.hex, h)) continue;
            int de, ei = NearestEnemyIdx(h, a.force, &de);
            if (de < 2) continue;
            int friends = 0;
            bool shield = false;
            for (int k = 0; k < g_nArmies; k++) {
                const Army &f = g_armies[k];
                if (f.force != a.force || f.slot == a.slot || HexDist(h, f.hex) != 1) continue;
                friends++;
                if (ei >= 0 && HexDist(f.hex, g_armies[ei].hex) < de) shield = true;
            }
            int fr = friends < 2 ? friends : 2;
            int score = retreat ? 60 * shield + 15 * fr + (de >= 3 ? 20 : 10) - 8 * move
                                : (de == 2 ? 100 : de == 3 ? 50 : 10) + 30 * shield + 15 * fr - 8 * move;
            if (score > bestScore) { bestScore = score; best = h; }
        }
    return best;
}

static void DayStance(uint8_t *root, int player, uint32_t date) {
    g_owner = 1;
    int hold = 0, restore = 0, seen = 0, aim = 0, back = 0;
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
        if (de <= 1) {
            // v5: back off, best behind one of our armies. Disengaging takes days, but whole contact episodes
            // show it works: v3 (back off) got free 40% / wiped 36% / ~6 days in contact, v4 (hold) 28% / 47% /
            // ~10 days, the original 21% / 65%. Keep the hex chosen earlier while it is still a safe one.
            int h = -1;
            if (m->mod && m->owner == g_owner && m->wAct == ACT_MOVE && m->wType == TT_HEX && m->wId != a.hex &&
                Walkable(root, m->wId) && NearestEnemy(m->wId, a.force) >= 2 && !OtherSideOfGate(a.hex, m->wId))
                h = m->wId;
            else
                h = FiringHex(root, a, true);
            if (h >= 0) {
                if (!(m->mod && m->wAct == ACT_MOVE && m->wId == h)) { Order(a.p, *m, ACT_MOVE, TT_HEX, h); back++; }
            } else {
                holdHere();
            }
        } else if (de == 2) {
            holdHere();
        } else if (de <= 4) {
            // v3: an order that walks it into contact (the tree's "follow the 本队", "attack army X", which
            // run once a 旬 and cover 3+ hexes a day) is replaced by a firing position 2 from the enemy
            int h = FiringHex(root, a, false);
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
    if (hold || restore || aim || back)
        logf(L"%u-%u-%u stance: 雁行 near enemies %d; back off %d, hold %d, firing position %d, restore %d",
             date / 10000, date / 100 % 100, date % 100, seen, back, hold, aim, restore);
}

static int DayNo(uint32_t d);
static bool AtWar(int f1, int f2, int today);    // see DayTarget: learned from the game's own attack orders

// ---- defend (守城迎击) ------------------------------------------------------------------------------
// Enemies are forces at war with it (AtWar, learned from the game's own attack orders), never allies.
// R1 engines first: a computer close-combat army not in contact attacks the nearest enemy siege engine
//    (井阑 冲车 投石) within 3 hexes, if our soldiers within 3 hexes of the engine are at least the enemy's
//    within 2 of it, engine included (v3; v2 sent 1000-3000 men at engines of 4000-11000: 0 of 7 engines
//    wiped, 3 of our 7 armies wiped); its earlier order comes back when no engine is that close.
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
    int today = DayNo(date);
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
                    if (dc > 5 || IsShip(e.form) || !AtWar(a.force, e.force, today)) continue;
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
                if (e.force == a.force || !IsEngine(e.form) || !AtWar(a.force, e.force, today)) continue;
                int d = HexDist(a.hex, e.hex);
                if (d > 3 || d >= best) continue;
                // v3: engines carry 4000-11000 soldiers and an escort; only when we are at least as strong there
                int ours = 0, theirs = 0;
                for (int i = 0; i < g_nArmies; i++) {
                    const Army &z = g_armies[i];
                    int dz = HexDist(z.hex, e.hex);
                    if (z.force == a.force) { if (dz <= 3) ours += U16(z.p, A_SOLDIERS); }
                    else if (dz <= 2 && !IsShip(z.form) && AtWar(a.force, z.force, today)) theirs += U16(z.p, A_SOLDIERS);
                }
                if (ours >= theirs) { best = d; target = j; }
            }
        }
        if (target >= 0) {
            int slot = g_armies[target].slot;
            if (!(m->mod && m->wAct == ACT_ATTACK_ARMY && m->wType == TT_ARMY && m->wId == slot)) {
                Order(a.p, *m, ACT_ATTACK_ARMY, TT_ARMY, slot);
                if (r2) defend++; else engines++;
                logf(L"%u-%u-%u defend %ls: army %d (force %d) -> army %d (force %d, formation %d), distance %d",
                     date / 10000, date / 100 % 100, date % 100, r2 ? L"R2" : L"R1", a.slot, a.force, slot,
                     g_armies[target].force, g_armies[target].form, HexDist(a.hex, g_armies[target].hex));
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

// ---- target (攻击目标分配) --------------------------------------------------------------------------
// The trees (会战协同) decide when a troops of 2+ armies goes into battle; this decides, every day, which
// nearby enemy each of its close-combat armies attacks (user, 2026-11-12: manage the targets, not every army
// on one enemy):
//  - only armies that are fighting: an enemy at war with them within 4 hexes. When none is that close the
//    order they had (the trees') comes back.
//  - an army touching enemies attacks one of those: it cannot move, so any other target, or a move order
//    (the trees' gather), leaves it standing there. It keeps the one it attacks while that one touches it.
//  - otherwise the nearest enemies first (no walking past one to get at another), and only enemies with a
//    free hex next to them.
//  - one enemy gets armies until ours on it have 1.5x its soldiers; the rest go to the next enemy and pile
//    on only when there is none. 锋矢 prefers 雁行 and siege engines; siege engines are preferred anyway.
// Left alone: 雁行 (stance), siege engines, transports, ships, troops of one army, troops not on a fighting
// task, armies attacking a city, waiting, going home or retreating, the player's armies.
// At war: learned from the game's own orders, i.e. forces whose armies were ordered to attack an army or a
// city of the other in the last 30 days. Allies never are, so they are never made a target.
enum { N_FORCE = 64, ACT_ATTACK_CITY = 4, WAR_DAYS = 30, R_FIGHT = 4 };
static int g_war[N_FORCE][N_FORCE];       // day number the two forces were last seen fighting

static int DayNo(uint32_t d) { return (int)(d / 10000) * 372 + (int)(d / 100 % 100) * 31 + (int)(d % 100); }
static bool AtWar(int f1, int f2, int today) {
    if (f1 <= 0 || f2 <= 0 || f1 >= N_FORCE || f2 >= N_FORCE || f1 == f2) return false;
    return g_war[f1][f2] && today - g_war[f1][f2] <= WAR_DAYS;
}
static int FootholdHex(uint8_t *root, int id);
static void LearnWars(uint8_t *root, int today) {
    for (int k = 0; k < g_nArmies; k++) {
        const Army &a = g_armies[k];
        int act = a.p[g_n.offAction], tt = a.p[g_n.offTType], id = U16(a.p, g_n.offTId), f = 0;
        if (act == ACT_ATTACK_ARMY && tt == TT_ARMY && id > 0 && id < (int)N_ARMY && g_bySlot[id] >= 0)
            f = g_armies[g_bySlot[id]].force;
        else if (act == ACT_ATTACK_CITY && tt == TT_CITY)
            f = CityForce(root, id);
        else if (g_gate && act == 5 && tt == 6 && id > 0 && id < 16) {
            // gate v4: attacking a gate (action 5, target type 6 = gate number) is war with its holder too. In 203
            // 曹操 attacked 武关 for weeks without another fight with 刘表; the war was forgotten after 30 days and
            // on 11-1 宛 sent close combat into the pass again.
            int gh = FootholdHex(root, 51 + id);
            uint8_t *gx = gh >= 0 ? Obj(root, ROOT_HEX, gh) : nullptr;
            f = gx ? gx[0x14] : 0;
        }
        if (f > 0 && f < N_FORCE && a.force > 0 && a.force < N_FORCE && f != a.force)
            g_war[a.force][f] = g_war[f][a.force] = today;
    }
}
static int TroopsTask(uint8_t *root, int t) {
    uint8_t *p = t > 0 && t < (int)N_TROOPS ? Obj(root, ROOT_TROOPS, t) : nullptr;
    return p ? U16(p, T_PURPOSE) : -1;
}
// the tasks 会战协同 handles: intercept, siege, area attack and the like (not transport, retreat, going home)
static bool FightingTask(int t) {
    return t == 1 || t == 2 || t == 3 || t == 8 || t == 9 || t == 10 || t == 13 || t == 14 || t == 15 ||
           t == 16 || t == 21 || t == 22;
}
static int FreeSides(uint8_t *root, int hex) {
    static const int nb[6][2] = {{0, 2}, {0, -2}, {1, 1}, {1, -1}, {-1, 1}, {-1, -1}};
    int n = 0;
    for (int i = 0; i < 6; i++)
        if (Walkable(root, HexAt(hex % MAP_W + nb[i][0], HexY(hex) + nb[i][1]))) n++;
    return n;
}
static int CurTarget(const Army &a) {     // index in g_armies of the army it is ordered to attack, -1 none
    int id = U16(a.p, g_n.offTId);
    if (a.p[g_n.offAction] != ACT_ATTACK_ARMY || a.p[g_n.offTType] != TT_ARMY || id <= 0 || id >= (int)N_ARMY) return -1;
    return g_bySlot[id];
}

// waitfight (BEHAVIOR_PLAN 第四十五节, 2026-11-14): in the 0.6.0 default set siege armies spent 21% of their days next
// to an enemy waiting (action 13; 181 of those 240 army-days belong to armies later wiped), intercepts 8%. One source:
// when the army it attacks is gone the game sets ours waiting where it stands until the next xun. With waitfight an
// army waiting next to an enemy at war is taken like one with a move order (single-army troops too); its wait comes
// back when no enemy is next to it. A wait another feature gave (siege meet) is not ours to touch (Mine).
enum { OWNER_WAITFIGHT = 17 };
static bool WaitFightFor(int force) {
    if (!g_waitFight) return false;
    if (g_ab && (g_abMask >> OWNER_WAITFIGHT & 1)) {
        if (g_ab == 1 && !(force & 1)) return false;
        if (g_ab == 2 && (force & 1)) return false;
    }
    return true;
}
static volatile LONG g_wfLogged = 0;

static void DayTarget(uint8_t *root, int player, uint32_t date) {
    g_owner = 5;
    int today = DayNo(date);           // wars are learned once a day in HkProgressUpdate
    static int load[N_ARMY], freeS[N_ARMY], order[N_ARMY], nearD[N_ARMY];
    static uint8_t touch[N_ARMY];
    int fight = 0, keep = 0, changed = 0, spread = 0, restore = 0, contactMove = 0, waitFought = 0;
    for (int f = 1; f < N_FORCE; f++) {
        if (!OursToTouch(f, player)) continue;
        bool wf = WaitFightFor(f);
        // our armies that may be given a target today
        int n = 0;
        for (int k = 0; k < g_nArmies; k++) {
            Army &a = g_armies[k];
            if (a.force != f) continue;
            Mem *m = Track(a, player);
            if (!m || !Mine(m) || a.p[g_n.offLock]) continue;
            // armies going home are left alone even in contact: disengaging normally takes days (median 3-4,
            // about half get free; user: that is normal). Making them fight back (tried 2026-11-12) showed no
            // gain in one 206 A/B run.
            bool multi = MultiTroops(a), waiting = wf && m->pAct == ACT_WAIT;
            bool ok = IsMelee(a.form) && (multi || waiting) && FightingTask(TroopsTask(root, a.troops));
            int dn = 99;
            uint8_t t = 0;
            if (ok)
                for (int j = 0; j < g_nArmies; j++) {
                    const Army &e = g_armies[j];
                    if (IsShip(e.form) || !AtWar(f, e.force, today)) continue;
                    int d = HexDist(a.hex, e.hex);
                    if (d < dn) dn = d;
                }
            if (dn <= 1) t = 1;
            // attack-army orders while an enemy is near; move orders only in contact (they cannot move)
            // waitfight: waiting next to an enemy, single-army troops too; otherwise as before (troops of 2+ only)
            ok = ok && dn <= R_FIGHT &&
                 ((multi && (m->pAct == ACT_ATTACK_ARMY || (t && (m->pAct == ACT_MOVE || m->pAct == 14)))) || (t && waiting));
            if (!ok) {
                if (m->mod) { Restore(a.p, *m); restore++; }
                continue;
            }
            touch[k] = t;
            nearD[k] = dn;
            order[n++] = k;
        }
        if (!n) continue;
        // what the rest of our armies already put on each enemy, and the free hexes around it
        for (int j = 0; j < g_nArmies; j++) { load[j] = 0; freeS[j] = -1; }
        for (int k = 0; k < g_nArmies; k++) {
            const Army &a = g_armies[k];
            if (a.force != f) continue;
            bool mine = false;
            for (int i = 0; i < n && !mine; i++) mine = order[i] == k;
            int t = CurTarget(a);
            if (!mine && t >= 0) load[t] += U16(a.p, A_SOLDIERS);
        }
        // armies in contact first, then the nearest
        for (int i = 1; i < n; i++)
            for (int j = i; j > 0; j--) {
                int x = order[j - 1], y = order[j];
                if (touch[x] > touch[y] || (touch[x] == touch[y] && nearD[x] <= nearD[y])) break;
                order[j - 1] = y;
                order[j] = x;
            }
        for (int i = 0; i < n; i++) {
            int k = order[i];
            Army &a = g_armies[k];
            Mem *m = &g_mem[a.slot];
            int cur = CurTarget(a), best = -1, bestScore = -1000000;
            if (touch[k] && cur >= 0 && HexDist(a.hex, g_armies[cur].hex) == 1 && AtWar(f, g_armies[cur].force, today)) {
                best = cur;                   // in contact with the one it attacks: keep it
                keep++;
            } else {
                for (int j = 0; j < g_nArmies; j++) {
                    const Army &e = g_armies[j];
                    if (IsShip(e.form) || !AtWar(f, e.force, today)) continue;
                    int d = HexDist(a.hex, e.hex);
                    if (touch[k] ? d != 1 : d > R_FIGHT) continue;
                    if (d > 1) {
                        if (freeS[j] < 0) freeS[j] = FreeSides(root, e.hex);
                        if (freeS[j] <= 0) continue;
                    }
                    int need = U16(e.p, A_SOLDIERS) * 3 / 2;
                    int s = -10 * d - U16(e.p, A_SOLDIERS) / 1000;
                    if (load[j] >= need) s -= 100;                     // enough of ours on it already
                    if (j == cur) s += 10;                             // no switching back and forth
                    if (IsEngine(e.form)) s += 15;
                    if (a.form == 2 && (e.form == F_YANXING || IsEngine(e.form))) s += 40;   // 锋矢
                    if (s > bestScore) { bestScore = s; best = j; }
                }
            }
            fight++;
            if (best < 0) {                   // nothing it can reach: its own order
                if (m->mod) { Restore(a.p, *m); restore++; }
                continue;
            }
            load[best] += U16(a.p, A_SOLDIERS);
            if (HexDist(a.hex, g_armies[best].hex) > 1 && freeS[best] > 0) freeS[best]--;
            int slot = g_armies[best].slot;
            if (cur == best) continue;
            if (m->pAct == ACT_ATTACK_ARMY && m->pType == TT_ARMY && m->pId == slot) {
                Restore(a.p, *m);
            } else {
                if (m->pAct != ACT_ATTACK_ARMY) contactMove++;
                if (m->pAct == ACT_WAIT) {
                    waitFought++;
                    if (InterlockedIncrement(&g_wfLogged) <= 40)
                        logf(L"%u-%u-%u waitfight: army %d (force %d, task %d, %d soldiers) waiting next to an enemy attacks "
                             L"army %d (force %d, %d soldiers)", date / 10000, date / 100 % 100, date % 100, a.slot, a.force,
                             TroopsTask(root, a.troops), U16(a.p, A_SOLDIERS), slot, g_armies[best].force,
                             U16(g_armies[best].p, A_SOLDIERS));
                }
                Order(a.p, *m, ACT_ATTACK_ARMY, TT_ARMY, slot);
            }
            changed++;
            // a target other than the one the rest of its troops attacks
            for (int q = 0; q < n; q++) {
                const Army &o = g_armies[order[q]];
                if (order[q] != k && o.troops == a.troops && CurTarget(o) >= 0 && CurTarget(o) != best) { spread++; break; }
            }
        }
    }
    if (changed || restore)
        logf(L"%u-%u-%u target: fighting %d; new target %d (move order in contact %d, of them waiting %d, not the troops' "
             L"target %d), kept in contact %d, restore %d", date / 10000, date / 100 % 100, date % 100, fight, changed,
             contactMove, waitFought, spread, keep, restore);
}

// ---- siege meet (攻城会合) -------------------------------------------------------------------------
// One force often sends several troops (军势) at the same city from different cities; they arrive weeks apart
// (median 20-55 days in the original game) and one troops alone takes the city 10-14% of the time, two or
// more that both arrive 50-66%. So troops of one force with the same siege target wait for each other
// outside the city: an army of a troops that has come within MEET_OUT hexes (but no closer than MEET_IN, and
// no army of its troops is closer than that: those are committed) holds its hex while another troops on the
// same target is still on the way (farther than MEET_OUT, at most MEET_FAR, and got nearer in the last
// MEET_STALL days). It goes on when the others are there, when they stop coming, after MEET_MAX days of
// waiting, or when an enemy army comes within 3 hexes of its troops. Only when, not who, how many or where.
// v1.1: v1 (40 hexes, 8 days, 20 days) waited up to 20 days for troops crawling toward the city (one went
// 16 -> 9 hexes in 20 days, itself waiting for its engines); now only for troops that are really coming.
// v2 近处晚出 (staggered start): troops of one force set out for the same city from several cities on the same
// day, the near ones first to arrive. A troops that has not gone more than START_MOVED hexes from where we
// first saw it (still by its own city) stays there while another troops of the group, still coming and at
// most START_FAR hexes out, is more than START_SLACK hexes farther from the target; then it sets out (at most
// START_MAX days). The meeting outside the city still lines them up at the end.
enum { MEET_IN = 6, MEET_OUT = 8, MEET_FAR = 24, MEET_STALL = 4, MEET_MAX = 12, ROOT_GATE = 0xded8, N_GATE = 11,
       START_MOVED = 4, START_FAR = 60, START_SLACK = 3, START_MAX = 30 };
struct TroopMem {
    uint16_t target;                      // what it was going for when we last saw it (a new one resets it)
    uint8_t dist;                         // nearest an army of it has been to the target so far
    int progress;                         // day it last got nearer
    int waitFrom;                         // day it started waiting (0: not waiting)
    uint8_t startDist;                    // distance when first seen (where it set out)
    int startFrom;                        // day it started waiting at its start (0: not waiting)
};
static TroopMem g_tm[N_TROOPS];

static bool SiegeTask(int t) { return t == 2 || t == 8 || t == 9 || t == 10 || t == 22; }
// hex of a foothold the troops go for: 1-51 cities, 52+ gates (gate = id - 51)
static int FootholdHex(uint8_t *root, int id) {
    if (id > 0 && id < (int)N_CITY) return CityHex(root, id);
    int g = id - 51;
    if (g <= 0 || g >= (int)N_GATE) return -1;
    uint8_t *gp = Obj(root, ROOT_GATE, g);
    if (!gp) return -1;
    int oi = U16(gp, C_OBJECT);
    uint8_t *op = oi > 0 && oi < (int)N_OBJECT ? Obj(root, ROOT_OBJECT, oi) : nullptr;
    int h = op ? U16(op, O_HEX) : -1;
    return h >= 0 && h < (int)N_HEX ? h : -1;
}

static void DaySiege(uint8_t *root, int player, uint32_t date) {
    g_owner = 6;
    int today = DayNo(date);
    static int16_t near_[N_TROOPS], force_[N_TROOPS], goal_[N_TROOPS], foe_[N_TROOPS];
    static int16_t list[N_TROOPS];
    int n = 0;
    for (int t = 0; t < (int)N_TROOPS; t++) near_[t] = -1;
    // siege troops of the forces we handle: nearest distance of their land armies to the target
    for (int k = 0; k < g_nArmies; k++) {
        const Army &a = g_armies[k];
        int t = a.troops;
        if (t <= 0 || t >= (int)N_TROOPS || IsShip(a.form) || !OursToTouch(a.force, player)) continue;
        if (near_[t] == -1) {
            uint8_t *tp = Obj(root, ROOT_TROOPS, t);
            int goal = tp ? U16(tp, T_PURPOSE + 2) : 0;
            int gh = tp && SiegeTask(U16(tp, T_PURPOSE)) ? FootholdHex(root, goal) : -1;
            near_[t] = -2;
            if (gh < 0) continue;
            near_[t] = 999;
            force_[t] = (int16_t)a.force;
            goal_[t] = (int16_t)goal;
            foe_[t] = 99;
            list[n++] = (int16_t)t;
            TroopMem &tm = g_tm[t];
            if (tm.target != goal) tm = TroopMem{(uint16_t)goal, 255, today, 0, 255, 0};
        }
        if (near_[t] < 0) continue;
        uint8_t *tp = Obj(root, ROOT_TROOPS, t);
        int d = HexDist(a.hex, FootholdHex(root, U16(tp, T_PURPOSE + 2)));
        if (d < near_[t]) near_[t] = (int16_t)d;
        int de = NearestEnemy(a.hex, a.force);
        if (de < foe_[t]) foe_[t] = (int16_t)de;
    }
    for (int i = 0; i < n; i++) {
        int t = list[i];
        TroopMem &tm = g_tm[t];
        if (near_[t] < tm.dist) { tm.dist = (uint8_t)(near_[t] < 255 ? near_[t] : 254); tm.progress = today; }
        if (tm.startDist == 255) tm.startDist = (uint8_t)(near_[t] < 255 ? near_[t] : 254);
    }
    // which troops wait: in the meeting band, others of the same force and target still coming
    static uint8_t hold[N_TROOPS];        // 1: wait in the meeting band, 2: wait at the start
    int waiting = 0, starting = 0, held = 0, released = 0;
    for (int i = 0; i < n; i++) {
        int t = list[i];
        hold[t] = 0;
        TroopMem &tm = g_tm[t];
        bool inBand = near_[t] >= MEET_IN && near_[t] <= MEET_OUT && foe_[t] > 3;
        bool coming = false;
        for (int j = 0; j < n && inBand && !coming; j++) {
            int u = list[j];
            coming = u != t && force_[u] == force_[t] && goal_[u] == goal_[t] && near_[u] > MEET_OUT &&
                     near_[u] <= MEET_FAR && today - g_tm[u].progress <= MEET_STALL;
        }
        if (inBand && coming) {
            if (!tm.waitFrom) tm.waitFrom = today;
            if (today - tm.waitFrom <= MEET_MAX) { hold[t] = 1; waiting++; }
        } else if (!inBand && near_[t] > MEET_OUT) {
            tm.waitFrom = 0;                  // not there yet: a later wait starts fresh
        }
        // v2: still at its start, and another troops of the group is coming from much farther
        bool atStart = near_[t] > MEET_OUT && near_[t] + START_MOVED >= tm.startDist && foe_[t] > 3;
        bool behind = false;
        for (int j = 0; j < n && atStart && !behind; j++) {
            int u = list[j];
            behind = u != t && force_[u] == force_[t] && goal_[u] == goal_[t] && near_[u] <= START_FAR &&
                     near_[u] - near_[t] > START_SLACK && today - g_tm[u].progress <= MEET_STALL;
        }
        if (atStart && behind) {
            if (!tm.startFrom) tm.startFrom = today;
            if (today - tm.startFrom <= START_MAX) { hold[t] = 2; starting++; }
        }
    }
    for (int k = 0; k < g_nArmies; k++) {
        Army &a = g_armies[k];
        int t = a.troops;
        if (t <= 0 || t >= (int)N_TROOPS || near_[t] < 0 || IsShip(a.form)) continue;
        Mem *m = Track(a, player);
        if (!m || !Mine(m) || a.p[g_n.offLock]) continue;
        int d = HexDist(a.hex, FootholdHex(root, goal_[t]));
        bool stay = NearestEnemy(a.hex, a.force) > 1 &&
                    ((hold[t] == 1 && d >= MEET_IN && d <= MEET_OUT) || hold[t] == 2);
        if (stay) {
            if (!(m->mod && m->wAct == ACT_MOVE && m->wType == TT_HEX)) {   // keep the hex it stopped at
                Order(a.p, *m, ACT_MOVE, TT_HEX, a.hex);
                held++;
            }
        } else if (m->mod) {
            Restore(a.p, *m);
            released++;
        }
    }
    if (held || released)
        logf(L"%u-%u-%u siege: troops waiting outside the city %d, at their start %d; armies stopped %d, released %d",
             date / 10000, date / 100 % 100, date % 100, waiting, starting, held, released);
}

// ---- siegelone (攻城落单不单打) ------------------------------------------------------------------------
// 2026-11-16 (BEHAVIOR_PLAN 第四十九、五十节). In three one-year runs of the 0.7.1 default set, siege armies touching
// an enemy with no army of their force within 4 hexes: 287 army-days, 142,000 soldiers (26% of what siege armies lose
// in contact), 22 of 42 wiped. Two days before such a contact 34 of 51 were already within 6 hexes of the target
// city, and for 25 an army of their force going for the same city was behind them within 15 hexes. The meeting
// outside the city (DaySiege) lines up troops, not armies: once they go, the first in arrives alone and the
// garrison comes out at it.
// So, every day: an army of a siege troops (city or gate), not a ship, SL_MIN..SL_MAX hexes from its target, no
// army of its force within SL_NEAR, not touching an enemy, while an army of its force going for the same target
// is within SL_JOIN of it and at least SL_AHEAD hexes farther from the target: it keeps its hex until one is
// next to it or none is behind any more. If an enemy at war comes within SL_FOE of it meanwhile, it steps back
// to a free hex by the nearest of those behind. At most SL_WAIT days per target, then not again for SL_BLOCK.
// Armies already nearer than SL_MIN, alone on that target, or held by another feature are left as they are.
enum { OWNER_LONE = 22, SL_MIN = 5, SL_MAX = 8, SL_NEAR = 4, SL_JOIN = 12, SL_AHEAD = 2, SL_WAIT = 8, SL_BLOCK = 20,
       SL_FOE = 3 };
struct SlMem { uint16_t gen, goal; int since, blockUntil; uint8_t on, oAct, oType; uint16_t oId; };
static SlMem g_slm[N_ARMY];
static volatile LONG g_slLogged = 0;

static int WarFoeDist(const Army &a, int today) {
    int best = 99;
    for (int k = 0; k < g_nArmies; k++) {
        const Army &e = g_armies[k];
        if (e.force == a.force || !AtWar(a.force, e.force, today)) continue;
        int d = HexDist(a.hex, e.hex);
        if (d < best) best = d;
    }
    return best;
}
// free walkable hex 1-2 from the hex `by`, nearest to the army; -1 none
static int HexBeside(uint8_t *root, const Army &a, int by) {
    int best = -1, bd = 999, x0 = by % MAP_W, y0 = HexY(by);
    for (int dx = -2; dx <= 2; dx++)
        for (int dy = -4; dy <= 4; dy++) {
            int h = HexAt(x0 + dx, y0 + dy);
            if (h < 0 || h == by || HexDist(h, by) > 2 || !Walkable(root, h)) continue;
            int d = HexDist(h, a.hex);
            if (d < bd) { bd = d; best = h; }
        }
    return best;
}

static void DaySiegeLone(uint8_t *root, int player, uint32_t date) {
    g_owner = OWNER_LONE;
    int today = DayNo(date);
    int alone = 0, held = 0, fresh = 0, fell = 0, rel[4] = {0, 0, 0, 0};   // joined, none behind / out of the band, contact, timeout
    for (int k = 0; k < g_nArmies; k++) {
        Army &a = g_armies[k];
        SlMem &sm = g_slm[a.slot];
        uint16_t gen = U16(a.p, A_GENERAL);
        if (sm.gen != gen) sm = SlMem{gen, 0, 0, 0, 0, 0, 0, 0};
        if (IsShip(a.form)) { sm.on = 0; continue; }
        Mem *m = Track(a, player);
        if (!m) { sm.on = 0; continue; }
        bool ours = m->mod && m->owner == OWNER_LONE;
        // the order to give back: the tree's when we took it; a later one from the tree or another feature replaces
        // it, but not the wait the game itself turns "go to the hex you stand on" into
        if (sm.on && !ours && m->pAct != ACT_WAIT) { sm.oAct = m->pAct; sm.oType = m->pType; sm.oId = m->pId; }
        auto giveBack = [&]() {
            if (!sm.on || !Mine(m)) return;
            Order(a.p, *m, sm.oAct, sm.oType, sm.oId);
            m->mod = 0;
        };
        uint8_t *tp = a.troops > 0 && a.troops < (int)N_TROOPS ? Obj(root, ROOT_TROOPS, a.troops) : nullptr;
        int goal = tp && SiegeTask(U16(tp, T_PURPOSE)) ? U16(tp, T_PURPOSE + 2) : 0;
        int gh = goal ? FootholdHex(root, goal) : -1;
        if (gh < 0 || !Mine(m) || a.p[g_n.offLock]) {
            if (gh >= 0) giveBack();             // no longer a siege troops: its new order stands
            if (sm.on) { sm.on = 0; rel[1]++; }
            continue;
        }
        if (sm.goal != goal) { sm.goal = (uint16_t)goal; sm.since = 0; sm.blockUntil = 0; }
        int d = HexDist(a.hex, gh), foe = WarFoeDist(a, today);
        bool company = false;
        int bi = -1, bd = 99;
        for (int j = 0; j < g_nArmies; j++) {
            const Army &x = g_armies[j];
            if (x.force != a.force || x.slot == a.slot || IsShip(x.form)) continue;
            int dx = HexDist(x.hex, a.hex);
            if (dx <= SL_NEAR) { company = true; break; }
            if (dx > SL_JOIN || dx >= bd || HexDist(x.hex, gh) < d + SL_AHEAD) continue;
            uint8_t *xp = x.troops > 0 && x.troops < (int)N_TROOPS ? Obj(root, ROOT_TROOPS, x.troops) : nullptr;
            if (!xp || !SiegeTask(U16(xp, T_PURPOSE)) || U16(xp, T_PURPOSE + 2) != goal) continue;
            bd = dx; bi = j;
        }
        bool want = d >= SL_MIN && d <= SL_MAX && !company && bi >= 0 && foe > 1 && today >= sm.blockUntil &&
                    FieldOrder(m->pAct);
        if (want && sm.since && today - sm.since > SL_WAIT) {
            sm.blockUntil = today + SL_BLOCK;
            sm.since = 0;
            giveBack();
            if (sm.on) { sm.on = 0; rel[3]++; }
            continue;
        }
        if (!want) {
            giveBack();
            if (sm.on) { sm.on = 0; rel[company ? 0 : foe <= 1 ? 2 : 1]++; }
            continue;
        }
        alone++;
        bool isNew = !sm.on;
        if (isNew) {
            sm.on = 1; sm.oAct = m->pAct; sm.oType = m->pType; sm.oId = m->pId;
            if (!sm.since) sm.since = today;
            fresh++;
        }
        int to = a.hex;
        if (foe <= SL_FOE) {
            int h = HexBeside(root, a, g_armies[bi].hex);
            if (h >= 0) { to = h; fell++; }
        }
        if (!(ours && m->wAct == ACT_MOVE && m->wType == TT_HEX && (to == a.hex || m->wId == (uint16_t)to)))
            Order(a.p, *m, ACT_MOVE, TT_HEX, to);
        held++;
        if (isNew && InterlockedIncrement(&g_slLogged) <= 400)
            logf(L"%u-%u-%u siegelone: army %d (force %d, troops %d, formation %d, %d soldiers) %d hexes from target %d, "
                 L"alone; army %d of its force is %d behind it, %d from the target; enemy at war %d away -> %ls",
                 date / 10000, date / 100 % 100, date % 100, a.slot, a.force, a.troops, a.form, U16(a.p, A_SOLDIERS), d,
                 goal, g_armies[bi].slot, bd, HexDist(g_armies[bi].hex, gh), foe, to == a.hex ? L"waits" : L"steps back to it");
    }
    if (alone || rel[0] + rel[1] + rel[2] + rel[3])
        logf(L"%u-%u-%u siegelone: alone ahead of their group %d (new %d); held %d, of them stepping back %d; let go: "
             L"company %d, none behind or out of the band %d, contact %d, timeout %d", date / 10000, date / 100 % 100,
             date % 100, alone, fresh, held, fell, rel[0], rel[1], rel[2], rel[3]);
}

// ---- tree probes (debug, feature "probe") ---------------------------------------------------------
// The game's MiscLogNode only reads its args (the print is compiled out) and returns success; with a type
// above 18 it does nothing at all. A module can place MiscLogNode(99, 0, $m.<id>.<name>) on a branch; we
// count how often each such node runs and log the counts once a day, keyed by the variable's key
// (btsrc.var_key of the name). Only counting: nothing in the tree changes.
enum { PROBE_TYPE = 99, N_PROBE = 64 };
static uint32_t g_probeKey[N_PROBE];
static int g_probeCount[N_PROBE];
static SRWLOCK g_probeLock = SRWLOCK_INIT;

enum { FORD_ON = 97, FORD_OFF = 96, REACH_ON = 95, REACH_OFF = 94, SIEGEFU_MARK = 93 };
static void ReachBegin(uint8_t *node, void *ctx);   // gate v4, see the gate section
static void SiegeFu(uint8_t *node, void *ctx);      // siegefu, see the siegefu section
static uint64_t HkMiscLog(uint8_t *node, void *ctx, void *a3, void *a4) {
    int type = *(int32_t *)(node + g_n.logTypeOff);
    if (type == FORD_ON) t_ford = g_ford;
    else if (type == FORD_OFF) t_ford = 0;
    else if (type == REACH_ON) ReachBegin(node, ctx);
    else if (type == REACH_OFF) t_reachHex = t_reachPend = -1;
    else if (type == SIEGEFU_MARK) SiegeFu(node, ctx);
    else if (g_probe && type == PROBE_TYPE) {
        uint32_t key = *(uint32_t *)(node + g_n.logTypeOff + 8);
        AcquireSRWLockExclusive(&g_probeLock);
        for (int i = 0; i < N_PROBE; i++) {
            if (g_probeKey[i] == key || !g_probeKey[i]) {
                g_probeKey[i] = key;
                g_probeCount[i]++;
                break;
            }
        }
        ReleaseSRWLockExclusive(&g_probeLock);
    }
    return ((uint64_t(*)(void *, void *, void *, void *))g_n.logExec)(node, ctx, a3, a4);
}

static void FlushProbes(uint32_t date) {
    wchar_t line[1600];
    int n = 0;
    AcquireSRWLockExclusive(&g_probeLock);
    for (int i = 0; i < N_PROBE && g_probeKey[i]; i++) {
        if (g_probeCount[i] && n < 1500)
            n += swprintf(line + n, 1600 - n, L" %08x=%d", g_probeKey[i], g_probeCount[i]);
        g_probeCount[i] = 0;
    }
    ReleaseSRWLockExclusive(&g_probeLock);
    if (n) logf(L"%u-%u-%u probes:%ls", date / 10000, date / 100 % 100, date % 100, line);
}

// ---- the daily hook ------------------------------------------------------------------------------
// ---- bonus (debug: 府加成 as the game computes it) --------------------------------------------------------
// 2b2b30(hex, force, int *sum) -> multiplier: sum = over the hex's area and its up-to-8 neighbours held by the
// force (or an ally), painted / all hexes x constant. Combat values reach it through 2aaff0 (22 callers).
// Logged per army whenever the value or its area's state changes: owner (area +0x78), painted / all hexes
// (+0x68 / +0x76), armies of other forces in the area, one standing on the 府 hex (+0x3a).
enum : uint32_t { ROOT_AREA = 0x6caf0, N_AREA = 501, AR_FU = 0x3a, AR_PAINTED = 0x68, AR_TOTAL = 0x76, AR_OWNER = 0x78,
                  HX_AREA = 0x12 };
struct BonusSeen { int area, owner, painted, foes, onFu, sum, milli; };
static BonusSeen g_bseen[N_ARMY];
static void DayBonus(uint8_t *root, uint32_t date) {
    typedef float (*BonusFn)(void *, int, int *);
    BonusFn fn = (BonusFn)g_n.bonusFn;
    if (!fn || !g_n.vtArea) return;
    static int areaOf[N_ARMY];
    for (int k = 0; k < g_nArmies; k++) {
        uint8_t *hx = Obj(root, ROOT_HEX, g_armies[k].hex);
        areaOf[k] = hx ? U16(hx, HX_AREA) : 0;
    }
    int logged = 0;
    for (int k = 0; k < g_nArmies; k++) {
        const Army &a = g_armies[k];
        uint8_t *hx = Obj(root, ROOT_HEX, a.hex);
        int ar = areaOf[k];
        uint8_t *ap = ar > 0 && ar < (int)N_AREA ? Obj(root, ROOT_AREA, ar) : nullptr;
        if (!hx || !IsA(ap, g_n.vtArea) || a.force <= 0) continue;
        int fu = U16(ap, AR_FU), foes = 0, onFu = 0;
        for (int j = 0; j < g_nArmies; j++) {
            if (g_armies[j].force == a.force) continue;
            if (areaOf[j] == ar) foes++;
            if (g_armies[j].hex == fu) onFu = 1;
        }
        int sum = 0;
        float m = fn(hx, a.force, &sum);
        BonusSeen now{ar, ap[AR_OWNER], U16(ap, AR_PAINTED), foes, onFu, sum, (int)(m * 1000.0f + 0.5f)};
        BonusSeen &was = g_bseen[a.slot];
        if (memcmp(&now, &was, sizeof now)) {
            was = now;
            logf(L"%u-%u-%u bonus: army %d force %d hex %d area %d owner %d painted %d/%d foes in area %d on fu %d "
                 L"sum %d x%.3f", date / 10000, date / 100 % 100, date % 100, a.slot, a.force, a.hex, ar, now.owner,
                 now.painted, U16(ap, AR_TOTAL), foes, onFu, sum, m);
            logged++;
        }
    }
    (void)logged;
}

// ---- fu (占府) --------------------------------------------------------------------------------------------
// Measured in the game (BEHAVIOR_PLAN 第三十二节): the 府加成 of an army is the painted share of its area and of
// the up-to-8 neighbouring areas whose 府 its force holds; an enemy army in the area changes nothing. An army
// that steps on an enemy 府 hex (terrain 15, painting it) takes the 府: the area's owner (+0x78) changes at the
// next xun start (seen on day 2, 12 or 22), even if the army has moved on; the area's official (+0x36) is
// then empty. Field exchange rises from 0.35 to 2.47 with the 府分 gap. So, every day:
// 1 顺路踩府: a close-combat army of a siege troops (v3: area-attack troops take 府 by their own task, v1 sent
//   them 61 of 83 times), marching (action move or wait), with no
//   enemy at war within 4 hexes, goes to step on an enemy 府 (at war with it) within 3 hexes that no army of
//   a force at war with it guards (none within 3 of the 府). Not the lead army of a troops of 2+; one army
//   per 府. 府 next to the siege target's area or to an area with enemy armies first.
// 2 先行: a close-combat army (not the lead) of a siege troops within 15 hexes of its target goes up to 6
//   hexes for an unguarded (nobody at war within 5) enemy 府 in the target's area or its neighbours; 锋矢 /
//   锥行 / 四夷 first (v2: v1 allowed only those, and siege troops near their target had them on 8 of 2567
//   army-days, so it never fired).
// Its earlier order comes back once it stands on the 府 hex or the hex is painted ours (v3: v2 waited for the
// area owner, which changes only at the next xun start, and gave up after 8 days, sometimes a day before),
// an enemy at war comes within 2 hexes, someone at war guards the 府, or after 12 days (moving through enemy
// paint is slow: 3 hexes took 3-9 days; that 府 is then left alone by this army for 30 days). Cities and
// gates (the 府 of a city's own area is the city) are never targets.
// v2: we keep our own record (the 府 hex and the order before ours). On arrival the game turns the move into
// a wait (action 13) at the 府 hex; v1 took that for a new order and let go, so the army sat on the 府 for a
// median 4 days (up to 17) until the troops tree came round. Now the army stays ours while its target is
// still the 府 hex, whatever the action, and gets its earlier order back the day the 府 is ours.
// v4 (2026-11-14, user: 「先占周边府」; BEHAVIOR_PLAN 41.1): at the walls the defender's 府分 is 3.58 on average
// (attacker about 1), the city's own share (about 1.0) cannot be taken, the rest are 1-4 府 6-11 hexes from the
// city. Rule 2 (先行) becomes: from the day the lead of a siege troops (target a city) is within 16 hexes of it,
// the zone = the areas of the hexes within 3 of the city and their neighbours, each weighted by how many of those
// hexes count it; enemy 府 there (not a city or gate), most 府分 for the defender first (weight x painted share);
// each goes to the nearest close-combat army of that troops (not the lead, marching / waiting, or attacking the
// city from 4+ hexes) within 8 hexes, no enemy at war within 4 of it, the 府 unguarded (none at war within 4);
// at most 2 of a troops out at once, and one close-combat army (the lead counts) always stays. 20 days to get
// there (enemy paint is slow).
enum { FU_R = 3, FU_GUARD = 3, FU_SAFE = 4, FU_ABORT = 2, FU_DAYS = 12, FU_BLOCK = 30, OWNER_FU = 7, AR_NB = 0x56,
       T_LEADER = 0x2c, FU_Z_GOAL = 16, FU_Z_STAND = 3, FU_Z_R = 8, FU_Z_GUARD = 4, FU_Z_OUT = 2, FU_Z_DAYS = 20 };
static bool AreaTask(int t) { return t == 3 || t == 13 || t == 14 || t == 15 || t == 16 || t == 21; }
static bool FastForm(int f) { return f == 2 || f == 7 || f == 14; }
struct FuMem { uint16_t gen; int since; int blockHex; int blockUntil; int hex; uint8_t act, type; uint16_t id; uint8_t zone; uint16_t goal;
               int lastHex; uint8_t still; };      // still: days a siege-zone army has not moved (stuck by the city)
// The city's own fire reaches CITY_FIRE hexes (measured 2026-11-15 over eight one-year runs: siege armies standing
// still with no enemy army within 3 lost 105-191 soldiers a day 2-3 hexes from the city, nothing from 4 on).
// Siege-zone 府 work keeps out of it: no army within CITY_FIRE + 1 is sent, no 府 within CITY_FIRE is a target,
// an army the tree has taken that close is let go, and one that has not moved for FU_STUCK days gives up.
enum { CITY_FIRE = 3, FU_STUCK = 3 };
enum { OWNER_PREP = 18 };
static int g_prepCand[N_TROOPS];          // siegeprep: last day FuZone had 府 to send for (not used for holding)
static int g_prepStepFC[64][64];          // siegeprep: last day a siege-zone 府 was stepped on, by force and city
static FuMem g_fum[N_ARMY];
static int16_t g_fuHex[N_AREA], g_areaOwner[N_AREA], g_nb[N_AREA][8], g_armyArea[N_ARMY];
static uint8_t g_foothold[N_AREA], g_claimed[N_AREA];

static int AreaOfHex(uint8_t *root, int h) {
    uint8_t *hx = h >= 0 && h < (int)N_HEX ? Obj(root, ROOT_HEX, h) : nullptr;
    int a = hx ? U16(hx, HX_AREA) : 0;
    return a > 0 && a < (int)N_AREA ? a : 0;
}
static bool AtWarNear(int force, int hex, int r, int today) {
    for (int j = 0; j < g_nArmies; j++) {
        const Army &e = g_armies[j];
        if (e.force != force && !IsShip(e.form) && AtWar(force, e.force, today) && HexDist(hex, e.hex) <= r)
            return true;
    }
    return false;
}
static bool Neighbour(int a, int b) {
    for (int j = 0; j < 8; j++)
        if (g_nb[a][j] == b) return true;
    return false;
}

static volatile LONG g_fuZoneSent = 0, g_fuZoneDone = 0, g_fuZoneGaveUp = 0, g_fuZoneKept = 0;
static int FuZone(uint8_t *root, int player, uint32_t date, int today) {
    static uint8_t seen[N_TROOPS];
    static int16_t w[N_AREA];
    memset(seen, 0, sizeof seen);
    int sent = 0;
    for (int k0 = 0; k0 < g_nArmies; k0++) {
        int t = g_armies[k0].troops, force = g_armies[k0].force;
        if (t <= 0 || t >= (int)N_TROOPS || seen[t]) continue;
        seen[t] = 1;
        if (!SiegeTask(TroopsTask(root, t))) continue;
        uint8_t *tp = Obj(root, ROOT_TROOPS, t);
        if (!tp) continue;
        int goal = U16(tp, T_PURPOSE + 2);
        if (goal < 1 || goal >= (int)N_CITY) continue;        // cities only, not gates
        int c = FootholdHex(root, goal);
        if (c < 0) continue;
        int leadGen = U16(tp, T_LEADER), leadK = -1, melee = 0, out = 0;
        for (int k = 0; k < g_nArmies; k++) {
            const Army &a = g_armies[k];
            if (a.troops != t) continue;
            if (U16(a.p, A_GENERAL) == leadGen) leadK = k;
            const FuMem &fm = g_fum[a.slot];
            bool away = fm.hex >= 0 && fm.gen == U16(a.p, A_GENERAL);
            if (away && fm.zone) out++;
            if (IsMelee(a.form) && !away) melee++;
        }
        if (leadK < 0 || HexDist(g_armies[leadK].hex, c) > FU_Z_GOAL || out >= FU_Z_OUT) continue;
        // the zone and its weights
        memset(w, 0, sizeof w);
        for (int dy = -FU_Z_STAND - 1; dy <= FU_Z_STAND + 1; dy++)
            for (int dx = -FU_Z_STAND - 1; dx <= FU_Z_STAND + 1; dx++) {
                int yy = c / MAP_W + dy, xx = c % MAP_W + dx;
                if (yy < 0 || yy >= MAP_W || xx < 0 || xx >= MAP_W) continue;
                int h = yy * MAP_W + xx;
                if (h == c || HexDist(h, c) > FU_Z_STAND) continue;
                int sa = AreaOfHex(root, h);
                if (!sa) continue;
                w[sa]++;
                for (int j = 0; j < 8; j++)
                    if (g_nb[sa][j] > 0 && g_nb[sa][j] < (int)N_AREA) w[g_nb[sa][j]]++;
            }
        struct Cand { int ar; float v; } cand[64];
        int nc = 0;
        for (int ar = 1; ar < (int)N_AREA && nc < 64; ar++) {
            if (!w[ar] || g_fuHex[ar] < 0 || g_foothold[ar] || g_claimed[ar]) continue;
            if (HexDist(g_fuHex[ar], c) <= CITY_FIRE) continue;   // under the city's fire
            int own = g_areaOwner[ar];
            if (!own || own == force || !AtWar(force, own, today)) continue;
            if (AtWarNear(force, g_fuHex[ar], FU_Z_GUARD, today)) continue;
            uint8_t *ap = Obj(root, ROOT_AREA, ar);
            int tot = ap ? U16(ap, AR_TOTAL) : 0;
            float share = tot > 0 ? (float)U16(ap, AR_PAINTED) / tot : 1.0f;
            cand[nc++] = {ar, w[ar] * share};
        }
        if (nc) g_prepCand[t] = today;
        for (int i = 1; i < nc; i++)
            for (int j = i; j > 0 && cand[j].v > cand[j - 1].v; j--) { Cand x = cand[j]; cand[j] = cand[j - 1]; cand[j - 1] = x; }
        for (int i = 0; i < nc && out < FU_Z_OUT && melee >= 2; i++) {
            int ar = cand[i].ar, h = g_fuHex[ar], bestK = -1, bestD = FU_Z_R + 1;
            for (int k = 0; k < g_nArmies; k++) {
                Army &a = g_armies[k];
                if (a.troops != t || k == leadK || !IsMelee(a.form) || a.p[g_n.offLock]) continue;
                if (HexDist(a.hex, c) <= CITY_FIRE + 1) continue;    // already at the walls: part of the siege
                FuMem &fm = g_fum[a.slot];
                if (fm.gen != U16(a.p, A_GENERAL)) fm = FuMem{U16(a.p, A_GENERAL), 0, -1, 0, -1, 0, 0, 0, 0, 0};
                if (fm.hex >= 0 || (fm.blockHex == h && today < fm.blockUntil)) continue;
                int act = a.p[g_n.offAction];
                bool marching = act == ACT_MOVE || act == ACT_WAIT || act == 14;
                bool farAttack = act >= 2 && act <= 6 && HexDist(a.hex, c) > FU_Z_STAND;
                if (!marching && !farAttack) continue;
                int d = HexDist(a.hex, h);
                if (d >= bestD || AtWarNear(a.force, a.hex, FU_SAFE, today)) continue;
                Mem *m = Track(a, player);
                if (!m || (m->mod && m->owner != OWNER_PREP)) continue;   // siegeprep's hold may be broken
                bestD = d;
                bestK = k;
            }
            if (bestK < 0) continue;
            Army &a = g_armies[bestK];
            Mem *m = Track(a, player);
            FuMem &fm = g_fum[a.slot];
            fm.act = a.p[g_n.offAction];
            fm.type = a.p[g_n.offTType];
            fm.id = U16(a.p, g_n.offTId);
            fm.hex = h;
            fm.zone = 1;
            fm.goal = (uint16_t)goal;
            fm.since = today;
            Order(a.p, *m, ACT_MOVE, TT_HEX, h);
            g_claimed[ar] = 1;
            out++;
            melee--;
            sent++;
            InterlockedIncrement(&g_fuZoneSent);
            logf(L"%u-%u-%u fu: army %d (force %d, formation %d) of siege troops %d (target city %d, lead %d hexes "
                 L"away) -> 府 hex %d (area %d, owner %d, worth %.1f), distance %d (siege zone)", date / 10000,
                 date / 100 % 100, date % 100, a.slot, a.force, a.form, t, goal, HexDist(g_armies[leadK].hex, c), h, ar,
                 g_areaOwner[ar], cand[i].v, bestD);
        }
    }
    return sent;
}

static void DayFu(uint8_t *root, int player, uint32_t date) {
    g_owner = OWNER_FU;
    if (!g_n.vtArea) return;
    int today = DayNo(date);
    for (int i = 0; i < (int)N_AREA; i++) {
        uint8_t *ap = i ? Obj(root, ROOT_AREA, i) : nullptr;
        bool ok = IsA(ap, g_n.vtArea);
        g_fuHex[i] = ok ? (int16_t)U16(ap, AR_FU) : -1;
        g_areaOwner[i] = ok ? ap[AR_OWNER] : 0;
        for (int j = 0; j < 8; j++) g_nb[i][j] = ok ? (int16_t)U16(ap, AR_NB + 2 * j) : 0;
        g_foothold[i] = 0;
        g_claimed[i] = 0;
    }
    for (int c = 1; c < (int)N_CITY + (int)N_GATE; c++) {    // 1..51 cities, 52.. gates
        int h = FootholdHex(root, c);
        int a = h >= 0 ? AreaOfHex(root, h) : 0;
        if (a && g_fuHex[a] == h) g_foothold[a] = 1;
    }
    for (int k = 0; k < g_nArmies; k++) g_armyArea[k] = (int16_t)AreaOfHex(root, g_armies[k].hex);
    // ongoing first: they keep their 府 claimed
    int taken = 0, aborted = 0, route = 0, ahead = 0;
    for (int k = 0; k < g_nArmies; k++) {
        Army &a = g_armies[k];
        Mem *m = Track(a, player);
        FuMem &fm = g_fum[a.slot];
        if (fm.gen != U16(a.p, A_GENERAL)) fm = FuMem{U16(a.p, A_GENERAL), 0, -1, 0, -1, 0, 0, 0, 0, 0};
        if (!m || fm.hex < 0 || a.p[g_n.offLock]) continue;
        int h = fm.hex, ar = AreaOfHex(root, h);
        // still going for (or standing on) our 府? The game turns the move into a wait on arrival.
        bool ours = a.p[g_n.offTType] == TT_HEX && U16(a.p, g_n.offTId) == h;
        // 2026-11-15 (user: they waited inside the city's fire): an army sent for a 府 that the tree then marched
        // to the city was sent back to the 府 every day; by the walls the move is not carried out, so it stood 2
        // hexes out under fire until fu gave up after 21 days (runs 20261115-115442: 赵云, 许褚, 魏越).
        int zoneCity = fm.zone && fm.goal > 0 && fm.goal < (int)N_CITY ? CityHex(root, fm.goal) : -1;
        bool drawnIn = zoneCity >= 0 && HexDist(a.hex, zoneCity) <= CITY_FIRE + 1;
        if (a.hex == fm.lastHex) { if (fm.still < 255) fm.still++; } else { fm.lastHex = a.hex; fm.still = 0; }
        if (!ours && fm.zone && drawnIn) {                // it is in the siege now: the tree's order stands
            fm.hex = -1;
            g_fuZoneGaveUp++;
            logf(L"%u-%u-%u fu: army %d (force %d) let go of 府 hex %d: the tree took it to the walls of city %d",
                 date / 10000, date / 100 % 100, date % 100, a.slot, a.force, h, fm.goal);
            continue;
        }
        if (!ours && fm.zone) {
            // v4.1: in the first A/B (runs 20261114-123950, 124855, 125750) the troops tree's xun-start order replaced
            // a siege-zone order on its way (41 of 174) and the army walked back until sent again (army 4 of 203:
            // sent 2-11, 2-22, 3-2, 3-22). While its troops still besieges the same city, the new order is a march,
            // wait or attack on the city and nothing at war is next to it, the 府 order comes back the next day; the
            // tree's new order is the one restored afterwards.
            uint8_t *tp = a.troops > 0 && a.troops < (int)N_TROOPS ? Obj(root, ROOT_TROOPS, a.troops) : nullptr;
            int act = a.p[g_n.offAction];
            bool keep = tp && SiegeTask(TroopsTask(root, a.troops)) && U16(tp, T_PURPOSE + 2) == fm.goal &&
                        (act == ACT_MOVE || act == ACT_WAIT || act == 14 || (act >= 2 && act <= 6)) &&
                        !AtWarNear(a.force, a.hex, FU_ABORT, today) && today - fm.since <= FU_Z_DAYS;
            if (keep) {
                fm.act = (uint8_t)act;
                fm.type = a.p[g_n.offTType];
                fm.id = U16(a.p, g_n.offTId);
                Order(a.p, *m, ACT_MOVE, TT_HEX, h);
                ours = true;
                if (InterlockedIncrement(&g_fuZoneKept) <= 30)
                    logf(L"%u-%u-%u fu: army %d (force %d) kept on 府 hex %d after the tree's new order (action %d)",
                         date / 10000, date / 100 % 100, date % 100, a.slot, a.force, h, act);
            }
        }
        if (!ours) { fm.hex = -1; continue; }          // the tree or the game gave it something else
        uint8_t *fh = Obj(root, ROOT_HEX, h);
        bool done = a.hex == h || (fh && fh[0x14] == a.force) || (ar && g_areaOwner[ar] == a.force);
        bool stop = !ar || !AtWar(a.force, g_areaOwner[ar], today) || AtWarNear(a.force, a.hex, FU_ABORT, today) ||
                    AtWarNear(a.force, h, (fm.zone ? FU_Z_GUARD : FU_GUARD) - 1, today) ||
                    today - fm.since > (fm.zone ? FU_Z_DAYS : FU_DAYS) || (fm.zone && fm.still >= FU_STUCK);
        if (done || stop) {
            Order(a.p, *m, fm.act, fm.type, fm.id);
            m->mod = 0;
            fm.hex = -1;
            if (done) taken++;
            else { aborted++; fm.blockHex = h; fm.blockUntil = today + FU_BLOCK; }
            logf(L"%u-%u-%u fu: army %d (force %d) %ls 府 hex %d (area %d) after %d days%ls", date / 10000,
                 date / 100 % 100, date % 100, a.slot, a.force, done ? L"stepped on" : L"gave up", h, ar,
                 today - fm.since, fm.zone ? L" (siege zone)" : L"");
            if (fm.zone) { if (done) g_fuZoneDone++; else g_fuZoneGaveUp++; }
            if (fm.zone && done && a.force > 0 && a.force < 64 && fm.goal > 0 && fm.goal < 64) g_prepStepFC[a.force][fm.goal] = today;
        } else {
            g_claimed[ar] = 1;
        }
    }
    for (int k = 0; k < g_nArmies; k++) {
        Army &a = g_armies[k];
        if (!IsMelee(a.form) || a.troops <= 0 || a.troops >= (int)N_TROOPS) continue;
        int task = TroopsTask(root, a.troops);
        bool siege = SiegeTask(task);
        if (!siege) continue;
        Mem *m = Track(a, player);
        if (!m || m->mod || g_fum[a.slot].hex >= 0 || a.p[g_n.offLock]) continue;
        int act = a.p[g_n.offAction];
        if (act != ACT_MOVE && act != ACT_WAIT && act != 14) continue;
        uint8_t *tp = Obj(root, ROOT_TROOPS, a.troops);
        bool lead = tp && U16(tp, T_LEADER) == U16(a.p, A_GENERAL);
        if (lead && MultiTroops(a)) continue;
        if (AtWarNear(a.force, a.hex, FU_SAFE, today)) continue;
        int goalHex = siege && tp ? FootholdHex(root, U16(tp, T_PURPOSE + 2)) : -1;
        int goalArea = goalHex >= 0 ? AreaOfHex(root, goalHex) : 0;
        FuMem &fm = g_fum[a.slot];
        int best = -1, bestScore = 1 << 30, bestR = 0;
        for (int ar = 1; ar < (int)N_AREA; ar++) {
            int h = g_fuHex[ar];
            if (h < 0 || g_foothold[ar] || g_claimed[ar]) continue;
            int own = g_areaOwner[ar];
            if (!own || own == a.force || !AtWar(a.force, own, today)) continue;
            if (fm.blockHex == h && today < fm.blockUntil) continue;
            int d = HexDist(a.hex, h);
            bool byGoal = goalArea && (ar == goalArea || Neighbour(goalArea, ar));
            int r = d <= FU_R ? 1 : 0;
            if (!r) continue;
            if (AtWarNear(a.force, h, FU_GUARD, today)) continue;
            // next to the fight: the target's area or its neighbours, or next to an area with enemy armies
            bool hot = byGoal;
            for (int e = 0; e < g_nArmies && !hot; e++)
                hot = g_armyArea[e] && Neighbour(ar, g_armyArea[e]) && g_armies[e].force != a.force &&
                      AtWar(a.force, g_armies[e].force, today);
            int score = d - (hot ? 3 : 0);
            if (score < bestScore) { bestScore = score; best = ar; bestR = r; }
        }
        if (best < 0) continue;
        fm.act = a.p[g_n.offAction];
        fm.type = a.p[g_n.offTType];
        fm.id = U16(a.p, g_n.offTId);
        fm.hex = g_fuHex[best];
        fm.zone = 0;
        Order(a.p, *m, ACT_MOVE, TT_HEX, g_fuHex[best]);
        fm.since = today;
        g_claimed[best] = 1;
        if (bestR == 1) route++; else ahead++;
        logf(L"%u-%u-%u fu: army %d (force %d, formation %d, task %d) -> 府 hex %d (area %d, owner %d), distance %d, %ls",
             date / 10000, date / 100 % 100, date % 100, a.slot, a.force, a.form, task, g_fuHex[best], best,
             g_areaOwner[best], HexDist(a.hex, g_fuHex[best]), bestR == 1 ? L"on the way" : L"ahead");
    }
    ahead = FuZone(root, player, date, today);
    (void)taken; (void)aborted; (void)route; (void)ahead;
}

// ---- siegeprep (攻城准备第一层：城外集结、先占府) --------------------------------------------------------
// 2026-11-14 (BEHAVIOR_PLAN 第四十六节; user: siege armies get beaten under the walls on the defender's 府, they should
// send mobile units to take the 府 around the city first). Exchange by the 府 gap where two armies fight: behind by
// 0.5+ 0.32-0.44, level 1.00, ahead 2.9-3.1; siege armies fought 76% of their days behind (0.30-0.37). fu's siege
// zone (v4) sent units for the 府 around the city, but the main body went on and the 府 changed hands (next xun
// start) after the fight had begun.
// A siege troops whose target is a city stops PREP_IN..PREP_OUT hexes out (no enemy within 3), once per target. The
// troops of one force on one city are one group: from the group's close-combat armies that have come within
// PREP_OUT + 2 (single-army troops too) we send up to PREP_DET for the unguarded enemy 府 that weigh most on the hexes
// within 3 of the city (their area or a neighbour of it), each within PREP_REACH of the army sent, keeping at least
// half the group's soldiers at the stop; fu then looks after them (arrival, giving up, the tree's new orders). The
// rest hold their hex while a unit is out, a 府 stepped on had less than PREP_FLIP days to change hands (next xun
// start), another troops of ours for the city is still coming, or in the first PREP_GRACE days; at most PREP_MAX
// days; then the tree's orders go on. Armies with an enemy next to them are not held. Needs fu.
// v1.1: v1 held while fu had 府 to send for, but fu sends only from troops of 2+ melee and never the lead: three
// single-army troops held 13 days at 建业 for nothing (smoke run 20261114-214347).
// v1.2 (smoke run 20261114-214851): a 府 stepped on keeps its owner until the next xun start, so the same army was
// sent to the same 府 five days running; a 府 whose hex is painted ours is no longer a candidate. The nearest army was
// sent even when it was the group's 12000-soldier main (15 days through enemy ground): the pick is now distance plus
// one hex per 3000 soldiers, at most PREP_REACH hexes (v1.1 let 13 through).
// v1.3 (2026-11-15, user: 「如果攻城前要占府，需要占府后再一起发动攻城，否则在城市攻击范围内等一下，如果不需要占领府则可以一起攻城」;
// siege armies stood still inside the city's fire 88-206 army-days a year, losing 28-42k, against 76-103 and 6-13k
// with the original AI): no army within CITY_FIRE + 1 of the city is sent and no 府 within CITY_FIRE is a target;
// fu lets go of one the tree has drawn to the walls and gives up when stuck; an army of a held group back from a 府
// returns to the group's stop; with no 府 to take the group waits only for its other troops (no grace days).
enum { PREP_IN = 7, PREP_OUT = 10, PREP_FLIP = 10, PREP_MAX = 15, PREP_FAR = 24, PREP_DET = 2, PREP_REACH = 12,
       PREP_GRACE = 2 };
struct PrepMem { uint16_t goal; int from; uint8_t done; };
static PrepMem g_pm[N_TROOPS];
static volatile LONG g_prepStarted = 0, g_prepEnded = 0, g_prepSent = 0;
static int g_prepNear[N_TROOPS];
static void DaySiegePrep(uint8_t *root, int player, uint32_t date) {
    g_owner = OWNER_PREP;
    int today = DayNo(date), held = 0, released = 0, holding = 0;
    static uint8_t hold[N_TROOPS], seenT[N_TROOPS];
    static int16_t goalT[N_TROOPS], forceT[N_TROOPS], foeT[N_TROOPS];
    static int16_t list[N_TROOPS];
    static int16_t w[N_AREA];
    int n = 0;
    memset(seenT, 0, sizeof seenT);
    for (int k = 0; k < g_nArmies; k++) {
        const Army &a = g_armies[k];
        int t = a.troops;
        if (t <= 0 || t >= (int)N_TROOPS || IsShip(a.form) || !OursToTouch(a.force, player)) continue;
        if (!seenT[t]) {
            seenT[t] = 1;
            uint8_t *tp = Obj(root, ROOT_TROOPS, t);
            int goal = tp && SiegeTask(U16(tp, T_PURPOSE)) ? U16(tp, T_PURPOSE + 2) : 0;
            goalT[t] = (int16_t)(goal >= 1 && goal < (int)N_CITY ? goal : 0);   // cities only
            forceT[t] = (int16_t)a.force;
            g_prepNear[t] = 999;
            foeT[t] = 99;
            if (!goalT[t]) continue;
            list[n++] = (int16_t)t;
            if (g_pm[t].goal != goalT[t]) g_pm[t] = PrepMem{(uint16_t)goalT[t], 0, 0};
        }
        if (!goalT[t]) continue;
        int d = HexDist(a.hex, CityHex(root, goalT[t]));
        if (d < g_prepNear[t]) g_prepNear[t] = d;
        int de = NearestEnemy(a.hex, a.force);
        if (de < foeT[t]) foeT[t] = (int16_t)de;
    }
    // which troops stop
    for (int i = 0; i < n; i++) {
        int t = list[i];
        PrepMem &pm = g_pm[t];
        if (pm.done || pm.from) continue;
        int nr = g_prepNear[t];
        if (nr < PREP_IN) { pm.done = 1; continue; }              // already committed: not pulled back
        if (nr > PREP_OUT || foeT[t] <= 3) continue;               // not there yet, or fighting
        pm.from = today;
        if (InterlockedIncrement(&g_prepStarted) <= 60)
            logf(L"%u-%u-%u siegeprep: troops %d (force %d) stops %d hexes from city %d", date / 10000,
                 date / 100 % 100, date % 100, t, forceT[t], nr, goalT[t]);
    }
    // per group (force, city) with troops stopped: send mobile units for the 府 around the city
    static uint8_t doneG[64][64], candG[64][64];
    memset(doneG, 0, sizeof doneG);
    memset(candG, 0, sizeof candG);
    for (int i = 0; i < n; i++) {
        int t = list[i], f = forceT[t], goal = goalT[t];
        if (f <= 0 || f >= 64 || goal <= 0 || goal >= 64 || doneG[f][goal]) continue;
        bool stopped = false;
        for (int j = 0; j < n && !stopped; j++) {
            int u = list[j];
            stopped = forceT[u] == f && goalT[u] == goal && g_pm[u].from && !g_pm[u].done;
        }
        if (!stopped) continue;
        doneG[f][goal] = 1;
        int c = CityHex(root, goal);
        if (c < 0) continue;
        int total = 0, away = 0, out = 0;
        for (int k = 0; k < g_nArmies; k++) {
            const Army &a = g_armies[k];
            if (a.force != f || a.troops <= 0 || a.troops >= (int)N_TROOPS || goalT[a.troops] != goal || !seenT[a.troops]) continue;
            if (HexDist(a.hex, c) > PREP_OUT + 2) continue;
            int sol = U16(a.p, A_SOLDIERS);
            total += sol;
            const FuMem &fm = g_fum[a.slot];
            if (fm.hex >= 0 && fm.gen == U16(a.p, A_GENERAL)) { away += sol; if (fm.zone) out++; }
        }
        if (out >= PREP_DET) continue;
        // the 府 that weigh on the hexes within 3 of the city
        memset(w, 0, sizeof w);
        for (int dy = -4; dy <= 4; dy++)
            for (int dx = -4; dx <= 4; dx++) {
                int yy = c / MAP_W + dy, xx = c % MAP_W + dx;
                if (yy < 0 || yy >= MAP_W || xx < 0 || xx >= MAP_W) continue;
                int h = yy * MAP_W + xx;
                if (h == c || HexDist(h, c) > 3) continue;
                int sa = AreaOfHex(root, h);
                if (!sa) continue;
                w[sa]++;
                for (int j = 0; j < 8; j++)
                    if (g_nb[sa][j] > 0 && g_nb[sa][j] < (int)N_AREA) w[g_nb[sa][j]]++;
            }
        struct Cand { int ar; float v; } cand[64];
        int nc = 0;
        for (int ar = 1; ar < (int)N_AREA && nc < 64; ar++) {
            if (!w[ar] || g_fuHex[ar] < 0 || g_foothold[ar] || g_claimed[ar]) continue;
            if (HexDist(g_fuHex[ar], c) <= CITY_FIRE) continue;   // under the city's fire
            int own = g_areaOwner[ar];
            if (!own || own == f || !AtWar(f, own, today)) continue;
            uint8_t *fh = Obj(root, ROOT_HEX, g_fuHex[ar]);
            if (fh && fh[0x14] == f) continue;                    // stepped on: changes hands at the next xun start
            if (AtWarNear(f, g_fuHex[ar], FU_Z_GUARD, today)) continue;
            uint8_t *ap = Obj(root, ROOT_AREA, ar);
            int tot = ap ? U16(ap, AR_TOTAL) : 0;
            float share = tot > 0 ? (float)U16(ap, AR_PAINTED) / tot : 1.0f;
            cand[nc++] = {ar, w[ar] * share};
        }
        candG[f][goal] = nc > 0;
        for (int a1 = 1; a1 < nc; a1++)
            for (int j = a1; j > 0 && cand[j].v > cand[j - 1].v; j--) { Cand x = cand[j]; cand[j] = cand[j - 1]; cand[j - 1] = x; }
        for (int ci = 0; ci < nc && out < PREP_DET; ci++) {
            int ar = cand[ci].ar, h = g_fuHex[ar], bestK = -1, bestScore = 1 << 30, bestD = 0;
            for (int k = 0; k < g_nArmies; k++) {
                Army &a = g_armies[k];
                if (a.force != f || a.troops <= 0 || a.troops >= (int)N_TROOPS || goalT[a.troops] != goal || !seenT[a.troops])
                    continue;
                if (!IsMelee(a.form) || a.p[g_n.offLock] || HexDist(a.hex, c) > PREP_OUT + 2) continue;
                if (HexDist(a.hex, c) <= CITY_FIRE + 1) continue;    // already at the walls
                FuMem &fm = g_fum[a.slot];
                if (fm.gen != U16(a.p, A_GENERAL)) fm = FuMem{U16(a.p, A_GENERAL), 0, -1, 0, -1, 0, 0, 0, 0, 0};
                if (fm.hex >= 0 || (fm.blockHex == h && today < fm.blockUntil)) continue;
                int sol = U16(a.p, A_SOLDIERS);
                if ((away + sol) * 2 > total) continue;             // keep half the group at the stop
                int d = HexDist(a.hex, h), score = d * 3000 + sol;    // a hex counts as 3000 soldiers
                if (d > PREP_REACH || score >= bestScore || AtWarNear(a.force, a.hex, FU_SAFE, today)) continue;
                Mem *m = Track(a, player);
                if (!m || (m->mod && m->owner != OWNER_PREP)) continue;
                bestScore = score; bestD = d; bestK = k;
            }
            if (bestK < 0) continue;
            Army &a = g_armies[bestK];
            Mem *m = Track(a, player);
            FuMem &fm = g_fum[a.slot];
            fm.act = a.p[g_n.offAction];
            fm.type = a.p[g_n.offTType];
            fm.id = U16(a.p, g_n.offTId);
            fm.hex = h; fm.zone = 1; fm.goal = (uint16_t)goal; fm.since = today;
            g_owner = OWNER_FU;                                   // fu looks after it from now on
            Order(a.p, *m, ACT_MOVE, TT_HEX, h);
            g_owner = OWNER_PREP;
            g_claimed[ar] = 1;
            away += U16(a.p, A_SOLDIERS);
            out++;
            if (InterlockedIncrement(&g_prepSent) <= 60)
                logf(L"%u-%u-%u siegeprep: army %d (force %d, formation %d, %d soldiers) of the group on city %d -> 府 hex %d "
                     L"(area %d, owner %d, weight %.1f), %d hexes", date / 10000, date / 100 % 100, date % 100, a.slot, f,
                     a.form, U16(a.p, A_SOLDIERS), goal, h, ar, g_areaOwner[ar], cand[ci].v, bestD);
        }
    }
    // hold or go on
    for (int i = 0; i < n; i++) {
        int t = list[i];
        PrepMem &pm = g_pm[t];
        hold[t] = 0;
        if (pm.done || !pm.from) continue;
        int f = forceT[t], goal = goalT[t];
        bool out = false;
        for (int k = 0; k < g_nArmies && !out; k++) {
            const Army &a = g_armies[k];
            const FuMem &fm = g_fum[a.slot];
            out = a.force == f && fm.hex >= 0 && fm.zone && fm.goal == goal && fm.gen == U16(a.p, A_GENERAL);
        }
        int step = f > 0 && f < 64 && goal < 64 ? g_prepStepFC[f][goal] : 0;
        // v1.3 (user 2026-11-15): with no 府 to take there is nothing to wait for but the other troops coming
        bool flip = step && today - step <= PREP_FLIP, grace = today - pm.from < PREP_GRACE && candG[f][goal];
        bool coming = false;
        for (int j = 0; j < n && !coming; j++) {
            int u = list[j];
            coming = u != t && forceT[u] == f && goalT[u] == goal && g_prepNear[u] > PREP_OUT && g_prepNear[u] <= PREP_FAR &&
                     today - g_tm[u].progress <= MEET_STALL;
        }
        if ((out || flip || coming || grace) && today - pm.from <= PREP_MAX) { hold[t] = 1; holding++; continue; }
        pm.done = 1;
        if (InterlockedIncrement(&g_prepEnded) <= 60)
            logf(L"%u-%u-%u siegeprep: troops %d (force %d) goes on to city %d after %d days (last 府 stepped on %d days ago, "
                 L"others coming %d)", date / 10000, date / 100 % 100, date % 100, t, f, goal, today - pm.from,
                 step ? today - step : -1, coming);
    }
    // v1.3 (user 2026-11-15: take the 府 first, then attack together): the stop of a held group is the hex of its
    // biggest army holding there; an army of a held troops that is nearer the city than the stop but still out of the
    // city's fire (one back from a 府) goes back to it instead of going on to the walls alone.
    static int16_t rallyHex[64][64];
    static int rallySol[64][64];
    memset(rallySol, 0, sizeof rallySol);
    for (int k = 0; k < g_nArmies; k++) {
        const Army &a = g_armies[k];
        int t = a.troops;
        if (t <= 0 || t >= (int)N_TROOPS || !seenT[t] || !hold[t] || IsShip(a.form)) continue;
        int f = forceT[t], goal = goalT[t], d = HexDist(a.hex, CityHex(root, goal));
        if (f <= 0 || f >= 64 || goal <= 0 || goal >= 64 || d < PREP_IN - 1 || d > PREP_OUT + 1) continue;
        int sol = U16(a.p, A_SOLDIERS);
        if (sol > rallySol[f][goal]) { rallySol[f][goal] = sol; rallyHex[f][goal] = (int16_t)a.hex; }
    }
    int rejoined = 0;
    for (int k = 0; k < g_nArmies; k++) {
        Army &a = g_armies[k];
        int t = a.troops;
        if (t <= 0 || t >= (int)N_TROOPS || !seenT[t] || !goalT[t] || IsShip(a.form)) continue;
        Mem *m = Track(a, player);
        if (!m || !Mine(m) || a.p[g_n.offLock]) continue;
        int d = HexDist(a.hex, CityHex(root, goalT[t])), f = forceT[t], goal = goalT[t];
        bool stay = hold[t] && d >= PREP_IN - 1 && d <= PREP_OUT + 1 && NearestEnemy(a.hex, a.force) > 1;
        const FuMem &fm = g_fum[a.slot];
        bool forFu = fm.hex >= 0 && fm.gen == U16(a.p, A_GENERAL);
        int rh = f > 0 && f < 64 && goal > 0 && goal < 64 && rallySol[f][goal] ? rallyHex[f][goal] : -1;
        bool rejoin = !stay && hold[t] && rh >= 0 && !forFu && d > CITY_FIRE + 1 && d < PREP_IN - 1 &&
                      NearestEnemy(a.hex, a.force) > 1;
        if (stay) {
            if (!(m->mod && m->wAct == ACT_MOVE && m->wType == TT_HEX)) {
                Order(a.p, *m, ACT_MOVE, TT_HEX, a.hex);
                held++;
            }
        } else if (rejoin) {
            if (!(m->mod && m->wAct == ACT_MOVE && m->wType == TT_HEX && m->wId == rh)) {
                Order(a.p, *m, ACT_MOVE, TT_HEX, rh);
                rejoined++;
            }
        } else if (m->mod) {
            Restore(a.p, *m);
            released++;
        }
    }
    if (held || released || rejoined)
        logf(L"%u-%u-%u siegeprep: troops holding %d; armies stopped %d, back to the stop %d, released %d", date / 10000,
             date / 100 % 100, date % 100, holding, held, rejoined, released);
}

// ---- scomplog (debug) and the army creation hook ---------------------------------------------------------
// The game's siege departure (CommandFootholdAttackDepatureNode, one composition function for each siege task
// 8 9 10 11 22) picks generals one by one; for each, 43750 gives him min(his capacity, what the city can
// spare) soldiers and the best formation; the caller then keeps the city's reserve, drops him under 3000, and
// stops once the strength it was asked for is reached. scomplog hooks 43750 and logs what each general got; it
// changes nothing. The creation hook belongs to gate (GateCreate); with scomplog on it also logs the siege army.
// (Two features changed the soldiers here until 2026-11-16, siegecomp and siegesplit: more, smaller armies took
// fewer cities, BEHAVIOR_PLAN 第三十三、四十节. The user had them deleted; the code is in git before that date.)
enum { REQ_SOLDIERS = 8, ASSIGN_STEAL = 15, CREATE_STEAL = 12 };
typedef void (*AssignFn)(void *, uint8_t *, uint8_t *, uint8_t *, int);
typedef int (*StatFn)(uint8_t *, int, int);
static AssignFn g_assignOrig;
static int g_scPerson = -1;               // the general 43750 handled last (HkCreate logs his army)

static void HkAssign(void *ctx, uint8_t *city, uint8_t *person, uint8_t *req, int avail) {
    g_assignOrig(ctx, city, person, req, avail);
    uint8_t *root = g_n.rootPtr ? *g_n.rootPtr : nullptr;
    if (!root || !person || !req || !g_n.statFn || !g_dataOk) return;
    int d = person[P_DISTRICT];
    uint8_t *dp = d > 0 && d < (int)N_DISTRICT ? Obj(root, ROOT_DISTRICT, d) : nullptr;
    int force = dp ? dp[D_FORCE] : 0;
    uint8_t *st = Ptr(root, ROOT_STATUS);
    int player = st ? st[ST_PLAYER] : 0;
    if (!force || (player && force == player)) return;
    g_scPerson = U16(person, 0x10);
    int soldiers;
    memcpy(&soldiers, req + REQ_SOLDIERS, 4);
    if (soldiers <= 0) return;
    int tong = ((StatFn)g_n.statFn)(person, 0, 0xfff);
    int need = 0;                         // strength still wanted (the caller's context +0x84)
    if (ctx) memcpy(&need, (uint8_t *)ctx + 0x84, 4);
    uint32_t date = g_lastDate;
    // the line keeps the format the two removed features wrote (research scripts read it)
    logf(L"%u-%u-%u siegecomp: person %d (force %d, not changed, 统率 %d, in city %d, strong there 0) %d -> %d soldiers; "
         L"city can spare %d, strength still wanted %d", date / 10000, date / 100 % 100, date % 100, g_scPerson, force,
         tong, U16(person, P_WHERE), soldiers, soldiers, avail, need);
}

typedef void *(*CreateFn)(uint8_t *, int);
static CreateFn g_createOrig;
static int GateCreate(uint8_t *req);      // gate v3, see the gate section
static void *HkCreate(uint8_t *req, int flag) {
    if (g_gate && req && g_n.depCreateRet && (uint8_t *)__builtin_return_address(0) == g_n.depCreateRet) {
        int r = GateCreate(req);
        if (r < 0) return nullptr;        // not created: the departure node fails, the intercept loop takes the next
        if (r > 0) { int y = F_YANXING; memcpy(req + 0x0c, &y, 4); }
    }
    if (req && g_scPerson > 0 && *(int32_t *)req == g_scPerson) {   // scomplog: the army 43750 just sized
        int soldiers, form;
        memcpy(&soldiers, req + REQ_SOLDIERS, 4);
        memcpy(&form, req + 0x0c, 4);
        uint32_t date = g_lastDate;
        logf(L"%u-%u-%u siegecomp: army made: person %d, %d soldiers, formation %d", date / 10000, date / 100 % 100,
             date % 100, g_scPerson, soldiers, form);
        g_scPerson = -1;
    }
    return g_createOrig(req, flag);
}

// Detour: the first `steal` bytes of fn (whole instructions, position independent) move to a trampoline that
// jumps back; fn starts with mov rax, hook; jmp rax. Returns the trampoline (the original function) or null.
static void *Detour(uint8_t *fn, void *hook, int steal) {
    uint8_t *tr = (uint8_t *)VirtualAlloc(nullptr, 64, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    if (!tr) return nullptr;
    memcpy(tr, fn, steal);
    uint64_t back = (uint64_t)(fn + steal), to = (uint64_t)hook;
    tr[steal] = 0x48; tr[steal + 1] = 0xB8; memcpy(tr + steal + 2, &back, 8); tr[steal + 10] = 0xFF; tr[steal + 11] = 0xE0;
    uint8_t jmp[32];
    jmp[0] = 0x48; jmp[1] = 0xB8; memcpy(jmp + 2, &to, 8); jmp[10] = 0xFF; jmp[11] = 0xE0;
    for (int i = 12; i < steal; i++) jmp[i] = 0x90;
    DWORD old;
    if (!VirtualProtect(fn, steal, PAGE_EXECUTE_READWRITE, &old)) { VirtualFree(tr, 0, MEM_RELEASE); return nullptr; }
    memcpy(fn, jmp, steal);
    VirtualProtect(fn, steal, old, &old);
    FlushInstructionCache(GetCurrentProcess(), fn, steal);
    return tr;
}

enum { A_MORALE = 0x1a };                 // army: morale (byte)

// ---- supplyguard (攻城准备第二层：防断粮) ----------------------------------------------------------------
// 2026-11-14 (BEHAVIOR_PLAN 第四十六节第二层; user: the siege is most easily cut off while it takes the 府 and losing the
// supply loses the army; send capable mobile units at whoever comes round the back, not the whole army back and forth,
// unless a large force does it, then fight it in the field; what matters is noticing it and deciding whom to send;
// relief armies cut the road without meaning to). An army is supplied through hexes painted in its colour back to an
// area whose 府 its force holds (第三十五节). Every day, for each siege troops of ours whose lead is within SG_NEAR of
// the target city, the road is the own-painted hexes on the shortest ways from the lead back to such an area (BFS out
// to SG_DEPTH steps, then back from the areas reached); its width is the fewest road hexes on one step. An enemy army
// at war (not a ship) is a threat within SG_R of a road hex SG_BEHIND or more steps behind the lead; with no road
// (cut), within SG_CUT_R of the lead and farther from the city than it (its distance counts SG_CUT_R - SG_R less).
// Roads and threats are logged for every computer force (A/B: compare the sides); guards only for the forces under test.
// supplylog only logs (how often, who, and the game's own cut flag +0x1f0 bit 8 with target +0x1f4, 第三十五节);
// supplyguard also sends, per group (one force on one city): a threat whose enemy soldiers within SG_CLUSTER reach half
// the group is met by the group's armies not in contact within SG_BIG_R of it (a field battle); a smaller one by the
// group's nearest close-combat army with SG_ODDS times those soldiers (else the two nearest together), within
// SG_REACH, not out for a 府, while the guards keep within half the group's soldiers. A guard goes back to the tree's
// order when its enemy is gone, is more than SG_LEAVE from the road and not touching it, or after SG_DAYS. Guards
// override siegeprep's hold and the siege meet (they run before them the next day and keep their armies).
// Road 府 (user, 2026-11-14 night: an unguarded enemy 府 near the siege army paints its area back and cuts the supply by
// itself, so send a fast unit that is close to step on it; 第十二节 too: standing in its area does not stop it, only
// stepping on the 府 does): the enemy 府 (at war, no enemy at war within FU_Z_GUARD, hex not yet ours, not a city or
// gate) of the areas the road runs through (all the hexes reached, when cut), most road hexes first; the group's
// nearest close-combat army not in contact (a fast formation counts SG_FAST hexes nearer, then fewer soldiers), within
// SG_FU_REACH, while the guards and those out for a 府 keep within half the group, at most SG_FU_OUT out per group.
// fu looks after it from then on (zone 2: arrival, giving up, the tree's new orders), as with siegeprep's.
enum { OWNER_SGUARD = 19, SG_NEAR = 14, SG_DEPTH = 40, SG_BEHIND = 3, SG_R = 4, SG_CUT_R = 8, SG_REACH = 12,
       SG_BIG_R = 16, SG_LEAVE = 6, SG_DAYS = 10, SG_CLUSTER = 2, SG_GROUP_R = 28, SG_NEW = 5, SG_MAXG = 64,
       SG_FU_REACH = 8, SG_FU_OUT = 2, SG_FAST = 3 };
static const float SG_ODDS = 1.2f;
enum : uint32_t { A_AIFLAGS = 0x1f0, A_AITARGET = 0x1f4 };
struct SgMem { uint16_t gen; int enemy; uint16_t enemyGen; int since, goal, force; uint8_t big; };
struct SgTroop { uint16_t goal; uint8_t state; };
struct SgSeen { uint16_t gen; int key, day; };
static SgMem g_sgm[N_ARMY];
static SgTroop g_sgT[N_TROOPS];
static SgSeen g_sgSeen[N_ARMY];
static uint16_t g_sgGen[N_HEX], g_sgStamp;
static int16_t g_sgDist[N_HEX];
static uint8_t g_sgOn[N_HEX];
static int g_sgQ[N_HEX];
static volatile LONG g_sgLogged = 0, g_sgFlagLogged = 0, g_sgSentLogged = 0;

// The road from hex start (in force's colour) back to an area whose 府 the force holds: its length in steps (0 when
// start is in one), -1 cut, -2 longer than SG_DEPTH. Marks g_sgOn on the hexes of the shortest roads (valid where
// g_sgGen == g_sgStamp); *width the fewest of them on one step in between, *choke one hex of that step; *nq the hexes
// visited (g_sgQ[0..nq), in BFS order).
static int SupplyRoad(uint8_t *root, int start, int force, int *width, int *choke, int *nq) {
    if (++g_sgStamp == 0) { memset(g_sgGen, 0, sizeof g_sgGen); g_sgStamp = 1; }
    static const int nb[6][2] = {{0, 2}, {0, -2}, {1, 1}, {1, -1}, {-1, 1}, {-1, -1}};
    int qh = 0, qt = 0, L = -1;
    bool deep = false;
    *width = 0; *choke = -1;
    g_sgGen[start] = g_sgStamp; g_sgDist[start] = 0; g_sgOn[start] = 0; g_sgQ[qt++] = start;
    while (qh < qt) {
        int h = g_sgQ[qh++], d = g_sgDist[h];
        if (L >= 0 && d > L) break;
        int a = AreaOfHex(root, h);
        if (a && g_areaOwner[a] == force) { if (L < 0) L = d; g_sgOn[h] = 1; continue; }
        if (L >= 0) continue;
        if (d >= SG_DEPTH) { deep = true; continue; }
        for (int i = 0; i < 6; i++) {
            int n = HexAt(h % MAP_W + nb[i][0], HexY(h) + nb[i][1]);
            if (n < 0 || g_sgGen[n] == g_sgStamp) continue;
            uint8_t *hx = Obj(root, ROOT_HEX, n);
            if (!hx || hx[0x14] != force) continue;
            g_sgGen[n] = g_sgStamp; g_sgDist[n] = (int16_t)(d + 1); g_sgOn[n] = 0; g_sgQ[qt++] = n;
        }
    }
    *nq = qt;
    if (L < 0) return deep ? -2 : -1;
    if (L == 0) return 0;
    static int cnt[SG_DEPTH + 1], pick[SG_DEPTH + 1];
    memset(cnt, 0, sizeof cnt);
    for (int i = qt - 1; i >= 0; i--) {                       // back from the areas reached, one step at a time
        int h = g_sgQ[i], d = g_sgDist[h];
        if (d >= L) continue;
        for (int j = 0; j < 6 && !g_sgOn[h]; j++) {
            int n = HexAt(h % MAP_W + nb[j][0], HexY(h) + nb[j][1]);
            if (n >= 0 && g_sgGen[n] == g_sgStamp && g_sgDist[n] == d + 1 && g_sgOn[n]) g_sgOn[h] = 1;
        }
        if (g_sgOn[h] && d > 0) { if (!cnt[d]) pick[d] = h; cnt[d]++; }
    }
    int bw = 1 << 30;
    for (int d = 1; d < L; d++)
        if (cnt[d] && cnt[d] < bw) { bw = cnt[d]; *choke = pick[d]; }
    *width = bw == (1 << 30) ? 0 : bw;
    return L;
}

static int FoeCluster(int force, int hex, int today) {
    int n = 0;
    for (int j = 0; j < g_nArmies; j++) {
        const Army &e = g_armies[j];
        if (e.force != force && !IsShip(e.form) && AtWar(force, e.force, today) && HexDist(hex, e.hex) <= SG_CLUSTER)
            n += U16(e.p, A_SOLDIERS);
    }
    return n;
}
static bool SgMayTake(const Mem *m) {
    return !m->mod || m->owner == OWNER_PREP || m->owner == 6 || m->owner == OWNER_SGUARD;
}
static bool FuAway(const Army &a) {
    const FuMem &fm = g_fum[a.slot];
    return fm.gen == U16(a.p, A_GENERAL) && fm.hex >= 0;
}

static void DaySupplyGuard(uint8_t *root, int player, uint32_t date) {
    g_owner = OWNER_SGUARD;
    if (!g_n.vtArea) return;
    int today = DayNo(date);
    if (!g_fu)                                                // DayFu fills it otherwise
        for (int i = 0; i < (int)N_AREA; i++) {
            uint8_t *ap = i ? Obj(root, ROOT_AREA, i) : nullptr;
            g_areaOwner[i] = IsA(ap, g_n.vtArea) ? ap[AR_OWNER] : 0;
        }
    static int16_t tGoal[N_TROOPS];
    static uint8_t tSeen[N_TROOPS];
    static int gF[SG_MAXG], gGoal[SG_MAXG], gCity[SG_MAXG], gSol[SG_MAXG], gGuardSol[SG_MAXG], gLeadHex[SG_MAXG];
    static uint8_t gBig[SG_MAXG], gCut[SG_MAXG], gAct[SG_MAXG];   // gAct: the force is under test (guards)
    static uint8_t tD[SG_MAXG][N_ARMY];                       // per group, per army index: steps from the road behind
    static int gCov[SG_MAXG][N_ARMY];                         // per group, per enemy army index: our guards' soldiers on it
    static uint16_t roadW[SG_MAXG][N_AREA];                   // per group: road hexes in each area
    int gN = 0;
    memset(tSeen, 0, sizeof tSeen);
    for (int k = 0; k < g_nArmies; k++) {                     // the siege target (a city) of every troops of ours
        const Army &a = g_armies[k];
        int t = a.troops;
        if (t <= 0 || t >= (int)N_TROOPS || tSeen[t]) continue;
        tSeen[t] = 1;
        tGoal[t] = 0;
        if (IsShip(a.form) || !a.force || (player && a.force == player)) continue;   // every computer force is logged
        uint8_t *tp = Obj(root, ROOT_TROOPS, t);
        int goal = tp && SiegeTask(U16(tp, T_PURPOSE)) ? U16(tp, T_PURPOSE + 2) : 0;
        if (goal >= 1 && goal < (int)N_CITY) tGoal[t] = (int16_t)goal;
    }
    int nNear = 0, nRoad = 0, nNarrow = 0, nCut = 0, nLong = 0, sumLen = 0, pNear[2] = {0, 0}, pCut[2] = {0, 0}, pThreat[2] = {0, 0};
    for (int k = 0; k < g_nArmies; k++) {                     // each troops' lead near its city: the road
        const Army &lead = g_armies[k];
        int t = lead.troops;
        if (t <= 0 || t >= (int)N_TROOPS || !tGoal[t] || IsShip(lead.form)) continue;
        uint8_t *tp = Obj(root, ROOT_TROOPS, t);
        if (!tp || U16(tp, T_LEADER) != U16(lead.p, A_GENERAL)) continue;
        int goal = tGoal[t], f = lead.force, c = CityHex(root, goal);
        if (c < 0 || HexDist(lead.hex, c) > SG_NEAR) continue;
        int gi = -1;
        for (int i = 0; i < gN; i++)
            if (gF[i] == f && gGoal[i] == goal) gi = i;
        if (gi < 0) {
            if (gN >= SG_MAXG) continue;
            gi = gN++;
            gF[gi] = f; gGoal[gi] = goal; gCity[gi] = c; gSol[gi] = 0; gGuardSol[gi] = 0; gBig[gi] = 0; gCut[gi] = 0;
            gLeadHex[gi] = lead.hex;
            gAct[gi] = OursToTouch(f, player);
            memset(tD[gi], 99, sizeof tD[gi]);
            memset(gCov[gi], 0, sizeof gCov[gi]);
            memset(roadW[gi], 0, sizeof roadW[gi]);
        }
        nNear++;
        pNear[f & 1]++;
        int width = 0, choke = -1, nq = 0;
        int L = SupplyRoad(root, lead.hex, f, &width, &choke, &nq);
        int st = L > 0 && width == 1 ? 2 : L >= 0 ? 1 : L == -1 ? 3 : 4;
        if (L >= 0) { nRoad++; sumLen += L; }
        if (st == 2) nNarrow++;
        if (st == 3) { nCut++; pCut[f & 1]++; gCut[gi] = 1; }
        if (st == 4) nLong++;
        SgTroop &tr = g_sgT[t];
        if (tr.goal != goal) tr = SgTroop{(uint16_t)goal, 0};
        if (st != tr.state && (st == 3 || tr.state == 3) && InterlockedIncrement(&g_sgLogged) <= 400)
            logf(L"%u-%u-%u supply: troops %d (force %d, city %d, lead army %d %d hexes out, %d soldiers, morale %d) %ls "
                 L"(road %d, narrowest %d at hex %d)", date / 10000, date / 100 % 100, date % 100, t, f, goal,
                 lead.slot, HexDist(lead.hex, c), U16(lead.p, A_SOLDIERS), lead.p[A_MORALE],
                 st == 3 ? L"is CUT OFF" : L"has its road back", L, width, choke);
        tr.state = (uint8_t)st;
        if (L > 0 || L == -1)                                 // the areas the road runs through (all reached when cut)
            for (int i = 0; i < nq; i++) {
                int h = g_sgQ[i];
                if (L > 0 && !g_sgOn[h]) continue;
                int ar = AreaOfHex(root, h);
                if (ar && roadW[gi][ar] < 60000) roadW[gi][ar]++;
            }
        // enemies: steps from the road behind the lead
        for (int j = 0; j < g_nArmies; j++) {
            const Army &e = g_armies[j];
            if (e.force == f || IsShip(e.form) || !AtWar(f, e.force, today)) continue;
            int dl = HexDist(e.hex, lead.hex), best = tD[gi][j];
            if (L > 0) {
                if (dl - L > 30) continue;
                for (int i = 0; i < nq; i++) {
                    int h = g_sgQ[i];
                    if (!g_sgOn[h] || g_sgDist[h] < SG_BEHIND) continue;
                    int d = HexDist(e.hex, h);
                    if (d < best) best = d;
                }
            } else if (L == -1 && dl <= SG_CUT_R + SG_LEAVE && HexDist(e.hex, c) > HexDist(lead.hex, c)) {
                int d = dl - (SG_CUT_R - SG_R);
                if (d < best) best = d < 0 ? 0 : d;
            }
            tD[gi][j] = (uint8_t)(best > 99 ? 99 : best);
        }
    }
    // the groups' soldiers
    for (int k = 0; k < g_nArmies; k++) {
        const Army &a = g_armies[k];
        int t = a.troops;
        if (t <= 0 || t >= (int)N_TROOPS || !tGoal[t] || IsShip(a.form)) continue;
        for (int gi = 0; gi < gN; gi++)
            if (gF[gi] == a.force && gGoal[gi] == tGoal[t] && HexDist(a.hex, gCity[gi]) <= SG_GROUP_R)
                gSol[gi] += U16(a.p, A_SOLDIERS);
    }
    // threats: log the new ones
    int nThreat = 0, nNew = 0;
    for (int gi = 0; gi < gN; gi++)
        for (int j = 0; j < g_nArmies; j++) {
            if (tD[gi][j] > SG_R) continue;
            const Army &e = g_armies[j];
            nThreat++;
            pThreat[gF[gi] & 1]++;
            SgSeen &sn = g_sgSeen[e.slot];
            int key = gF[gi] * 64 + gGoal[gi];
            bool fresh = sn.gen != U16(e.p, A_GENERAL) || sn.key != key || today - sn.day > SG_NEW;
            sn = SgSeen{U16(e.p, A_GENERAL), key, today};
            if (!fresh) continue;
            nNew++;
            int contact = 0;
            for (int k = 0; k < g_nArmies; k++)
                if (g_armies[k].force == gF[gi] && HexDist(g_armies[k].hex, e.hex) <= 1) contact++;
            uint32_t fl;
            memcpy(&fl, e.p + A_AIFLAGS, 4);
            if (InterlockedIncrement(&g_sgLogged) <= 400)
                logf(L"%u-%u-%u supply: threat to the group of force %d on city %d (%d soldiers%ls): army %d (force %d, "
                     L"formation %d, %d soldiers, %d within %d, troops task %d, action %d -> %d:%d, ai flags %x) at hex %d, "
                     L"%d from the road behind, %d from the city, touching %d of ours",
                     date / 10000, date / 100 % 100, date % 100, gF[gi], gGoal[gi], gSol[gi], gCut[gi] ? L", cut off" : L"",
                     e.slot, e.force, e.form, U16(e.p, A_SOLDIERS), FoeCluster(gF[gi], e.hex, today), SG_CLUSTER,
                     TroopsTask(root, e.troops), e.p[g_n.offAction], e.p[g_n.offTType], U16(e.p, g_n.offTId), fl, e.hex,
                     tD[gi][j], HexDist(e.hex, gCity[gi]), contact);
        }
    // the game's own cut flag on enemies near our groups
    int nFlag = 0;
    for (int j = 0; j < g_nArmies; j++) {
        const Army &e = g_armies[j];
        uint32_t fl;
        memcpy(&fl, e.p + A_AIFLAGS, 4);
        if (!(fl & 0x100)) continue;
        int gi = -1;
        for (int i = 0; i < gN && gi < 0; i++)
            if (e.force != gF[i] && HexDist(e.hex, gCity[i]) <= SG_GROUP_R) gi = i;
        if (gi < 0) continue;
        nFlag++;
        int tgt = U16(e.p, A_AITARGET);
        if (InterlockedIncrement(&g_sgFlagLogged) <= 60)
            logf(L"%u-%u-%u supply: army %d (force %d, hex %d) has the game's cut flag, target %d (as an army slot: force "
                 L"%d, hex %d); near the group of force %d on city %d", date / 10000, date / 100 % 100, date % 100, e.slot,
                 e.force, e.hex, tgt, tgt > 0 && tgt < (int)N_ARMY && g_bySlot[tgt] >= 0 ? g_armies[g_bySlot[tgt]].force : -1,
                 tgt > 0 && tgt < (int)N_ARMY && g_bySlot[tgt] >= 0 ? g_armies[g_bySlot[tgt]].hex : -1, gF[gi], gGoal[gi]);
    }
    int sent = 0, kept = 0, released = 0, battles = 0, short_ = 0, lost = 0, roadFu = 0;
    if (g_sguard) {
        // guards out: keep or send back
        for (int k = 0; k < g_nArmies; k++) {
            Army &a = g_armies[k];
            SgMem &sg = g_sgm[a.slot];
            if (sg.gen != U16(a.p, A_GENERAL)) sg = SgMem{U16(a.p, A_GENERAL), -1, 0, 0, 0, 0, 0};
            if (sg.enemy < 0) continue;
            Mem *m = Track(a, player);
            if (!m || !m->mod || m->owner != OWNER_SGUARD) { sg.enemy = -1; lost++; continue; }   // the tree or game took over
            int gi = -1;
            for (int i = 0; i < gN && gi < 0; i++)
                if (gF[i] == sg.force && gGoal[i] == sg.goal) gi = i;
            int ek = g_bySlot[sg.enemy];
            bool gone = ek < 0 || U16(g_armies[ek].p, A_GENERAL) != sg.enemyGen;
            bool touching = !gone && HexDist(a.hex, g_armies[ek].hex) <= 1;
            int dr = gone || gi < 0 ? 99 : tD[gi][ek];
            if (gi < 0 || gone || (dr > SG_LEAVE && !touching) || today - sg.since > SG_DAYS) {
                if (InterlockedIncrement(&g_sgSentLogged) <= 300)
                    logf(L"%u-%u-%u supplyguard: army %d (force %d, %d soldiers) back to its order after %d days (%ls)",
                         date / 10000, date / 100 % 100, date % 100, a.slot, a.force, U16(a.p, A_SOLDIERS),
                         today - sg.since, gi < 0 ? L"siege over or far" : gone ? L"enemy gone" :
                         dr > SG_LEAVE && !touching ? L"enemy left the road" : L"days up");
                Restore(a.p, *m);
                sg.enemy = -1;
                released++;
                continue;
            }
            gCov[gi][ek] += U16(a.p, A_SOLDIERS);
            gGuardSol[gi] += U16(a.p, A_SOLDIERS);
            kept++;
        }
        // new threats, nearest the road first
        for (int gi = 0; gi < gN; gi++) {
            int f = gF[gi];
            if (!gAct[gi]) continue;
            int list[64], nl = 0;
            for (int j = 0; j < g_nArmies && nl < 64; j++)
                if (tD[gi][j] <= SG_R) list[nl++] = j;
            for (int x = 1; x < nl; x++)
                for (int y = x; y > 0 && tD[gi][list[y]] < tD[gi][list[y - 1]]; y--) { int s = list[y]; list[y] = list[y - 1]; list[y - 1] = s; }
            for (int li = 0; li < nl; li++) {
                int ek = list[li];
                Army &e = g_armies[ek];
                int clus = FoeCluster(f, e.hex, today);
                if (gCov[gi][ek] >= (int)(SG_ODDS * clus)) continue;
                bool big = 2 * clus >= gSol[gi];
                int pick[N_ARMY], np = 0, pickSol = 0;
                if (big) {
                    if (gBig[gi]) continue;
                    gBig[gi] = 1;
                    for (int k = 0; k < g_nArmies && np < (int)N_ARMY; k++) {
                        Army &a = g_armies[k];
                        if (a.force != f || a.troops <= 0 || a.troops >= (int)N_TROOPS || tGoal[a.troops] != gGoal[gi]) continue;
                        if (IsShip(a.form) || a.p[g_n.offLock] || FuAway(a) || g_sgm[a.slot].enemy >= 0) continue;
                        if (HexDist(a.hex, e.hex) > SG_BIG_R || NearestEnemy(a.hex, a.force) <= 1) continue;
                        // at the walls: in the siege, and a march order there is not carried out (2026-11-15: 于禁 and
                        // 张郃 stood 2-3 hexes from 庐江 under its fire for days, sent at a relief army 4 hexes off)
                        if (HexDist(a.hex, CityHex(root, gGoal[gi])) <= CITY_FIRE + 1) continue;
                        Mem *m = Track(a, player);
                        if (!m || !SgMayTake(m)) continue;
                        pick[np++] = k;
                        pickSol += U16(a.p, A_SOLDIERS);
                    }
                    if (np) battles++;
                } else {
                    int need = (int)(SG_ODDS * clus) - gCov[gi][ek];
                    int room = gSol[gi] / 2 - gGuardSol[gi];
                    int cand[64], nc = 0;
                    for (int k = 0; k < g_nArmies && nc < 64; k++) {
                        Army &a = g_armies[k];
                        if (a.force != f || a.troops <= 0 || a.troops >= (int)N_TROOPS || tGoal[a.troops] != gGoal[gi]) continue;
                        if (!IsMelee(a.form) || a.p[g_n.offLock] || FuAway(a) || g_sgm[a.slot].enemy >= 0) continue;
                        if (HexDist(a.hex, e.hex) > SG_REACH || NearestEnemy(a.hex, a.force) <= 1) continue;
                        // at the walls: in the siege, and a march order there is not carried out (2026-11-15: 于禁 and
                        // 张郃 stood 2-3 hexes from 庐江 under its fire for days, sent at a relief army 4 hexes off)
                        if (HexDist(a.hex, CityHex(root, gGoal[gi])) <= CITY_FIRE + 1) continue;
                        if (U16(a.p, A_SOLDIERS) > room) continue;
                        Mem *m = Track(a, player);
                        if (!m || !SgMayTake(m)) continue;
                        cand[nc++] = k;
                    }
                    for (int x = 1; x < nc; x++)
                        for (int y = x; y > 0 && HexDist(g_armies[cand[y]].hex, e.hex) < HexDist(g_armies[cand[y - 1]].hex, e.hex); y--) {
                            int s = cand[y]; cand[y] = cand[y - 1]; cand[y - 1] = s;
                        }
                    for (int x = 0; x < nc && !np; x++)              // the nearest strong enough alone
                        if (U16(g_armies[cand[x]].p, A_SOLDIERS) >= need) { pick[np++] = cand[x]; pickSol = U16(g_armies[cand[x]].p, A_SOLDIERS); }
                    for (int x = 0; x < nc && !np; x++)              // else the two nearest that are together
                        for (int y = x + 1; y < nc && !np; y++) {
                            int s = U16(g_armies[cand[x]].p, A_SOLDIERS) + U16(g_armies[cand[y]].p, A_SOLDIERS);
                            if (s >= need && s <= room) { pick[0] = cand[x]; pick[1] = cand[y]; np = 2; pickSol = s; }
                        }
                    if (!np) {
                        short_++;
                        if (InterlockedIncrement(&g_sgSentLogged) <= 300)
                            logf(L"%u-%u-%u supplyguard: nobody to send at army %d (force %d, %d soldiers with those near it) "
                                 L"behind the group of force %d on city %d (%d soldiers, %d guarding, %d candidates)",
                                 date / 10000, date / 100 % 100, date % 100, e.slot, e.force, clus, f, gGoal[gi], gSol[gi],
                                 gGuardSol[gi], nc);
                        continue;
                    }
                }
                for (int x = 0; x < np; x++) {
                    Army &a = g_armies[pick[x]];
                    Mem *m = Track(a, player);
                    SgMem &sg = g_sgm[a.slot];
                    sg = SgMem{U16(a.p, A_GENERAL), e.slot, U16(e.p, A_GENERAL), today, gGoal[gi], f, (uint8_t)big};
                    Order(a.p, *m, ACT_ATTACK_ARMY, TT_ARMY, e.slot);
                    gCov[gi][ek] += U16(a.p, A_SOLDIERS);
                    gGuardSol[gi] += U16(a.p, A_SOLDIERS);
                    sent++;
                }
                if (np && InterlockedIncrement(&g_sgSentLogged) <= 300)
                    logf(L"%u-%u-%u supplyguard: %ls %d armies (%d soldiers, first army %d, %d hexes away) at army %d (force %d, "
                         L"%d soldiers with those near it, %d from the road behind) for the group of force %d on city %d "
                         L"(%d soldiers%ls)", date / 10000, date / 100 % 100, date % 100, big ? L"FIELD BATTLE," : L"guard,",
                         np, pickSol, g_armies[pick[0]].slot, HexDist(g_armies[pick[0]].hex, e.hex), e.slot, e.force, clus,
                         tD[gi][ek], f, gGoal[gi], gSol[gi], gCut[gi] ? L", cut off" : L"");
            }
        }
        // road 府: step on the enemy 府 whose areas the road runs through
        for (int gi = 0; gi < gN && g_fu; gi++) {
            if (!gAct[gi]) continue;
            int f = gF[gi], goal = gGoal[gi], out = 0, away = 0;
            for (int k = 0; k < g_nArmies; k++) {
                const Army &a = g_armies[k];
                if (a.force != f || a.troops <= 0 || a.troops >= (int)N_TROOPS || tGoal[a.troops] != goal || !FuAway(a)) continue;
                away += U16(a.p, A_SOLDIERS);
                if (g_fum[a.slot].zone == 2) out++;
            }
            while (out < SG_FU_OUT) {
                int bestAr = -1, bestW = 0;
                for (int ar = 1; ar < (int)N_AREA; ar++) {
                    if (roadW[gi][ar] <= bestW || g_fuHex[ar] < 0 || g_foothold[ar] || g_claimed[ar]) continue;
                    int own = g_areaOwner[ar];
                    if (!own || own == f || !AtWar(f, own, today)) continue;
                    uint8_t *fh = Obj(root, ROOT_HEX, g_fuHex[ar]);
                    if (!fh || fh[0x14] == f || AtWarNear(f, g_fuHex[ar], FU_Z_GUARD, today)) continue;
                    bestAr = ar; bestW = roadW[gi][ar];
                }
                if (bestAr < 0) break;
                g_claimed[bestAr] = 1;                            // looked at today, sent or not
                int h = g_fuHex[bestAr], room = gSol[gi] / 2 - gGuardSol[gi] - away, bestK = -1, bestScore = 1 << 30, bestD = 0;
                for (int k = 0; k < g_nArmies; k++) {
                    Army &a = g_armies[k];
                    if (a.force != f || a.troops <= 0 || a.troops >= (int)N_TROOPS || tGoal[a.troops] != goal) continue;
                    if (!IsMelee(a.form) || a.p[g_n.offLock] || g_sgm[a.slot].enemy >= 0) continue;
                    FuMem &fm = g_fum[a.slot];
                    if (fm.gen != U16(a.p, A_GENERAL)) fm = FuMem{U16(a.p, A_GENERAL), 0, -1, 0, -1, 0, 0, 0, 0, 0};
                    if (fm.hex >= 0 || (fm.blockHex == h && today < fm.blockUntil)) continue;
                    int sol = U16(a.p, A_SOLDIERS), d = HexDist(a.hex, h);
                    if (d > SG_FU_REACH || sol > room || NearestEnemy(a.hex, a.force) <= 1) continue;
                    if (HexDist(a.hex, CityHex(root, goal)) <= CITY_FIRE + 1) continue;   // at the walls: in the siege
                    int score = (d - (FastForm(a.form) ? SG_FAST : 0)) * 100000 + sol;
                    if (score >= bestScore) continue;
                    Mem *m = Track(a, player);
                    if (!m || !SgMayTake(m)) continue;
                    bestScore = score; bestK = k; bestD = d;
                }
                if (bestK < 0) continue;
                Army &a = g_armies[bestK];
                Mem *m = Track(a, player);
                FuMem &fm = g_fum[a.slot];
                fm.act = a.p[g_n.offAction];
                fm.type = a.p[g_n.offTType];
                fm.id = U16(a.p, g_n.offTId);
                fm.hex = h; fm.zone = 2; fm.goal = (uint16_t)goal; fm.since = today;
                g_owner = OWNER_FU;                               // fu looks after it from now on
                Order(a.p, *m, ACT_MOVE, TT_HEX, h);
                g_owner = OWNER_SGUARD;
                away += U16(a.p, A_SOLDIERS);
                out++;
                roadFu++;
                if (InterlockedIncrement(&g_sgSentLogged) <= 300)
                    logf(L"%u-%u-%u supplyguard: army %d (force %d, formation %d, %d soldiers) -> road 府 hex %d (area %d, "
                         L"owner %d, %d road hexes in it%ls), %d hexes; group on city %d", date / 10000, date / 100 % 100,
                         date % 100, a.slot, f, a.form, U16(a.p, A_SOLDIERS), h, bestAr, g_areaOwner[bestAr], bestW,
                         gCut[gi] ? L", cut off" : L"", bestD, goal);
            }
        }
    }
    if (nNear)
        logf(L"%u-%u-%u supply: siege troops near their city %d: road %d (avg %d steps, %d with a 1-hex step), cut %d, "
             L"farther than %d %d; threats %d (new %d), game cut flags near %d; guards sent %d (field battles %d), "
             L"holding %d, back %d, none to send %d, taken over %d, road 府 sent %d (odd/even forces: near %d/%d, cut %d/%d, "
             L"threats %d/%d)",
             date / 10000, date / 100 % 100, date % 100, nNear, nRoad, nRoad ? sumLen / nRoad : 0, nNarrow, nCut, SG_DEPTH,
             nLong, nThreat, nNew, nFlag, sent, battles, kept, released, short_, lost, roadFu, pNear[1], pNear[0], pCut[1], pCut[0],
             pThreat[1], pThreat[0]);
}

// ---- shore (半渡而击) ---------------------------------------------------------------------------------
// Data (research/naval_stats.py, 2026-11-13): an army on water loses 5-20 times the soldiers of the army on
// land it touches (one on one: 834 vs 41 a day in 197 original, 385 vs 82 in 206 default). Yet the original
// almost never strikes an enemy while it crosses: enemy armies on water within 8 of another force's city are
// some 150 army-days a year, and that force's armies touch them on 0-6 of those days.
// So, every day: an enemy army at war, on water (terrain 14 river, 24 the other water), on a fighting task;
// our close-combat armies on land within 6 of it, not fighting on land, on intercept / area attack / paint
// defence, go to the land hex next to it (nearest to them; if no land hex touches it, the bank hex nearest to
// it within 3), until ours there have its soldiers, 3 at most. v2 (after the 197 run): only armies of 1500+
// soldiers, and only to a hex they can walk to over land within 10 steps (one on the far bank used to cross). Next day the hex is worked out again from where
// it is. Given back when it lands, is gone, our army is on water, an enemy at war on land touches the hex,
// or after 12 days; when the trees or the game give it a new order we let go.
// v4 (2026-11-13, user: 「还需要优化测试效果」; v3 in 197 sent most armies to bank hexes 2-3 from the enemy,
// where a close-combat army does nothing): close combat only to a land hex touching the enemy; 雁行 (shoots at
// 2) only to a land hex 2 from it, so an enemy two hexes off the bank is shot at instead of waited for.
// v5 (2026-11-13, 197 run with ford: armies let out to intercept took ships to chase enemies on the water, the
// enemy landed and ours fought from the water, 曹操's interceptors 18 days and 4306 soldiers): an army of ours on
// land (intercept / area tasks) ordered to attack an enemy army that is on the water and not next to it waits
// where it is instead of boarding; once that enemy has landed or is gone, its order comes back.
enum { SH_REACH = 6, SH_BANK = 3, SH_DAYS = 12, SH_MAX = 3, SH_MIN = 1500, OWNER_SHORE = 10, TER_RIVER = 14,
       TER_WATER2 = 24 };
struct ShoreMem { uint16_t gen; int enemy; uint16_t enemyGen; int since, hex; };
static ShoreMem g_shm[N_ARMY];
static uint8_t g_shRes[N_HEX];

static bool Water(uint8_t *root, int hex) { int t = Terrain(root, hex); return t == TER_RIVER || t == TER_WATER2; }
static bool LandFoeNear(uint8_t *root, int force, int hex, int r, int today) {
    for (int k = 0; k < g_nArmies; k++) {
        const Army &e = g_armies[k];
        if (e.force != force && AtWar(force, e.force, today) && HexDist(e.hex, hex) <= r && !Water(root, e.hex)) return true;
    }
    return false;
}
// can army a walk to hex `to` over land in a few steps (not across the river)? Other armies' hexes count as
// passable (ours move through ours; an enemy on the way is the game's business).
enum { SH_WALK = 10 };
static uint16_t g_lrGen[N_HEX], g_lrStamp;
static int16_t g_lrDist[N_HEX];
static int g_lrQ[N_HEX];
static bool LandReach(uint8_t *root, int from, int to) {
    if (++g_lrStamp == 0) { memset(g_lrGen, 0, sizeof g_lrGen); g_lrStamp = 1; }
    static const int nb[6][2] = {{0, 2}, {0, -2}, {1, 1}, {1, -1}, {-1, 1}, {-1, -1}};
    int qh = 0, qt = 0;
    g_lrGen[from] = g_lrStamp; g_lrDist[from] = 0; g_lrQ[qt++] = from;
    while (qh < qt) {
        int h = g_lrQ[qh++];
        if (h == to) return true;
        if (g_lrDist[h] >= SH_WALK) continue;
        for (int i = 0; i < 6; i++) {
            int n = HexAt(h % MAP_W + nb[i][0], HexY(h) + nb[i][1]);
            if (n < 0 || g_lrGen[n] == g_lrStamp) continue;
            int t = Terrain(root, n);
            if (t < 0 || !g_walk[t] || t == TER_RIVER || t == TER_WATER2) continue;
            g_lrGen[n] = g_lrStamp; g_lrDist[n] = (int16_t)(g_lrDist[h] + 1); g_lrQ[qt++] = n;
        }
    }
    return false;
}
static bool ShoreTask(int t) { return t == 1 || t == 3 || t == 13 || t == 14 || t == 15 || t == 16 || t == 21 || t == 23; }
// the land hex for army a against enemy e: close combat touching it, 雁行 two hexes from it (nearest to a)
static int ShoreRange(const Army &a) { return a.form == F_YANXING ? 2 : 1; }
static bool ShoreForm(int form) { return IsMelee(form) || form == F_YANXING; }
static int ShoreHex(uint8_t *root, const Army &a, const Army &e, int today) {
    int y = e.hex / MAP_W, x = e.hex % MAP_W, best = -1, bestScore = 1 << 30, want = ShoreRange(a);
    for (int dy = -SH_BANK; dy <= SH_BANK; dy++)
        for (int dx = -SH_BANK; dx <= SH_BANK; dx++) {
            int yy = y + dy, xx = x + dx;
            if (yy < 0 || yy >= MAP_W || xx < 0 || xx >= MAP_W) continue;
            int h = yy * MAP_W + xx, d = HexDist(e.hex, h);
            if (d != want || Water(root, h)) continue;
            if (h != a.hex && (!Walkable(root, h) || g_shRes[h])) continue;
            if (LandFoeNear(root, a.force, h, 1, today)) continue;
            int score = HexDist(a.hex, h);
            if (score >= bestScore) continue;
            if (h != a.hex && !LandReach(root, a.hex, h)) continue;   // v2: not across the river
            bestScore = score; best = h;
        }
    return best;
}

// the nearest free land hex within 2 of an army on the water, with no enemy at war on land next to it
static int BankHex(uint8_t *root, const Army &a, int today) {
    int y = a.hex / MAP_W, x = a.hex % MAP_W, best = -1, bd = 99;
    for (int dy = -2; dy <= 2; dy++)
        for (int dx = -2; dx <= 2; dx++) {
            int yy = y + dy, xx = x + dx;
            if (yy < 0 || yy >= MAP_W || xx < 0 || xx >= MAP_W) continue;
            int h = yy * MAP_W + xx, d = HexDist(a.hex, h);
            if (d < 1 || d > 2 || d >= bd || Water(root, h) || !Walkable(root, h) || g_shRes[h]) continue;
            if (LandFoeNear(root, a.force, h, 1, today)) continue;
            bd = d; best = h;
        }
    return best;
}

static void DayShore(uint8_t *root, int player, uint32_t date) {
    g_owner = OWNER_SHORE;
    int today = DayNo(date);
    memset(g_shRes, 0, sizeof g_shRes);
    static int sent[N_ARMY], nsent[N_ARMY];
    memset(sent, 0, sizeof sent);
    memset(nsent, 0, sizeof nsent);
    int kept = 0, moved = 0, ordered = 0, landed = 0, gone = 0, timeout = 0, wet = 0, contact = 0, replaced = 0;
    int overruled = 0, ashore = 0;
    // ongoing
    for (int k = 0; k < g_nArmies; k++) {
        Army &a = g_armies[k];
        ShoreMem &sm = g_shm[a.slot];
        if (sm.gen != U16(a.p, A_GENERAL)) sm = ShoreMem{U16(a.p, A_GENERAL), -1, 0, 0, -1};
        if (sm.enemy < 0) continue;
        Mem *m = Track(a, player);
        if (!m) { sm.enemy = -1; replaced++; continue; }
        if (!m->mod || m->owner != OWNER_SHORE) {
            // v3: a new order from the trees or the game. Let go, unless it takes the army onto the water: attack
            // an army on the water, or go to a water hex (it fights from the bank anyway: armies next to each other
            // fight whatever their orders, 197 run: waiting on the bank 9868 vs 889 lost)
            int act = a.p[g_n.offAction], tt = a.p[g_n.offTType], id = U16(a.p, g_n.offTId);
            bool wetOrder = (act == ACT_ATTACK_ARMY && tt == TT_ARMY && id > 0 && id < (int)N_ARMY && g_bySlot[id] >= 0 &&
                             Water(root, g_armies[g_bySlot[id]].hex)) || (act == ACT_MOVE && tt == TT_HEX && Water(root, id));
            if (!wetOrder || !Mine(m)) { sm.enemy = -1; replaced++; continue; }
            sm.hex = -1;                  // ordered again below
            overruled++;
        }
        int ei = sm.enemy > 0 && sm.enemy < (int)N_ARMY ? g_bySlot[sm.enemy] : -1;
        const char *why = nullptr;
        if (ei < 0 || U16(g_armies[ei].p, A_GENERAL) != sm.enemyGen) why = "gone";
        else if (!Water(root, g_armies[ei].hex)) why = "landed";
        else if (today - sm.since > SH_DAYS) why = "timeout";
        else if (!Water(root, a.hex) && LandFoeNear(root, a.force, a.hex, 1, today)) why = "contact";
        int h = -1;
        if (!why && Water(root, a.hex)) {
            // v3: carried onto the water (an army moves into the hex of the one it beat): back to the nearest bank
            h = BankHex(root, a, today);
            if (h < 0) why = "wet"; else ashore++;
        } else if (!why) h = ShoreHex(root, a, g_armies[ei], today);
        if (!why && h < 0) why = "gone";
        if (why) {
            Restore(a.p, *m);
            sm.enemy = -1;
            if (!strcmp(why, "gone")) gone++; else if (!strcmp(why, "landed")) landed++;
            else if (!strcmp(why, "wet")) wet++; else if (!strcmp(why, "timeout")) timeout++; else contact++;
            continue;
        }
        g_shRes[h] = 1;
        sent[ei] += U16(a.p, A_SOLDIERS);
        nsent[ei]++;
        if (h != sm.hex) { Order(a.p, *m, ACT_MOVE, TT_HEX, h); sm.hex = h; moved++; }
        kept++;
    }
    // new
    int onWater = 0;
    for (int k = 0; k < g_nArmies; k++) {
        const Army &e = g_armies[k];
        if (!Water(root, e.hex) || !FightingTask(TroopsTask(root, e.troops))) continue;
        int need = U16(e.p, A_SOLDIERS);
        bool counted = false;
        while (nsent[k] < SH_MAX && sent[k] < need) {
            int best = -1, bestD = 99, bestH = -1;
            for (int j = 0; j < g_nArmies; j++) {
                Army &a = g_armies[j];
                if (a.force == e.force || !ShoreForm(a.form) || a.p[g_n.offLock] || g_shm[a.slot].enemy >= 0) continue;
                if (U16(a.p, A_SOLDIERS) < SH_MIN) continue;
                if (!AtWar(a.force, e.force, today)) continue;
                int d = HexDist(a.hex, e.hex);
                if (d > SH_REACH || d >= bestD || Water(root, a.hex)) continue;
                if (!ShoreTask(TroopsTask(root, a.troops)) || !FieldOrder(a.p[g_n.offAction])) continue;
                if (LandFoeNear(root, a.force, a.hex, 2, today)) continue;
                Mem *m = Track(a, player);
                if (!m || !Mine(m)) continue;
                int h = ShoreHex(root, a, e, today);
                if (h < 0) continue;
                best = j; bestD = d; bestH = h;
            }
            if (!counted) { onWater++; counted = true; }
            if (best < 0) break;
            Army &a = g_armies[best];
            Mem *m = Track(a, player);
            ShoreMem &sm = g_shm[a.slot];
            sm.enemy = e.slot; sm.enemyGen = U16(e.p, A_GENERAL); sm.since = today; sm.hex = bestH;
            Order(a.p, *m, ACT_MOVE, TT_HEX, bestH);
            g_shRes[bestH] = 1;
            sent[k] += U16(a.p, A_SOLDIERS);
            nsent[k]++;
            ordered++;
            logf(L"%u-%u-%u shore: army %d (force %d, formation %d, %d soldiers) -> hex %d (%d from it) against army %d "
                 L"(force %d, formation %d, %d soldiers, troops task %d, on terrain %d), %d away", date / 10000,
                 date / 100 % 100, date % 100, a.slot, a.force, a.form, U16(a.p, A_SOLDIERS), bestH,
                 HexDist(bestH, e.hex), e.slot, e.force, e.form, need, TroopsTask(root, e.troops), Terrain(root, e.hex),
                 HexDist(a.hex, bestH));
        }
    }
    // v5: no boarding to chase an enemy on the water
    int stay = 0, stayKept = 0, stayFreed = 0;
    for (int k = 0; k < g_nArmies; k++) {
        Army &a = g_armies[k];
        if (g_shm[a.slot].enemy >= 0 || IsShip(a.form) || a.p[g_n.offLock] || Water(root, a.hex)) continue;
        Mem *m = Track(a, player);
        if (!m || !Mine(m)) continue;
        bool holding = m->mod && m->owner == OWNER_SHORE;
        int act = holding ? m->pAct : a.p[g_n.offAction], tt = holding ? m->pType : a.p[g_n.offTType];
        int id = holding ? m->pId : U16(a.p, g_n.offTId);
        int ti = act == ACT_ATTACK_ARMY && tt == TT_ARMY && id > 0 && id < (int)N_ARMY ? g_bySlot[id] : -1;
        bool chase = ti >= 0 && Water(root, g_armies[ti].hex) && HexDist(a.hex, g_armies[ti].hex) > 1 &&
                     ShoreTask(TroopsTask(root, a.troops));
        if (holding) {
            if (chase) stayKept++;
            else { Restore(a.p, *m); stayFreed++; }
        } else if (chase) {
            Order(a.p, *m, ACT_WAIT, TT_HEX, a.hex);
            stay++;
        }
    }
    if (stay || stayFreed)
        logf(L"%u-%u-%u shore: not boarding to chase an enemy on the water: %d new, %d waiting, %d given back",
             date / 10000, date / 100 % 100, date % 100, stay, stayKept, stayFreed);
    if (ordered || kept || landed || gone || timeout || wet || contact || replaced)
        logf(L"%u-%u-%u shore: %d new, %d holding (%d moved on), released: landed %d, gone %d, timeout %d, on water %d, "
             L"land contact %d, new order from the game %d; kept off the water: %d orders, %d back to the bank",
             date / 10000, date / 100 % 100, date % 100, ordered, kept, moved, landed, gone, timeout, wet, contact,
             replaced, overruled, ashore);
    (void)onWater;
}

// ---- navaldef (水战守港) ------------------------------------------------------------------------------
// 2026-11-15 (BEHAVIOR_PLAN 第四十七节; user: 「主要是判断在岸上守合适，还是在水里迎击合适，如果水军科技高攻防数值高，
// 在水里迎击合适……陆战打水站有优势就守岸边，否则等着上岸再打」，「如果是能够水战，则敌方靠近港口水域边界就可以开始迎击了，
// 而不是走到港口附近」). Data: an enemy army on the water comes within 12 of another force's port about 46 times a
// year; the port goes to it within 30 days in a third of them; when the defenders touched it, almost all on the
// water, they lost 1.7 for 1 (research/naval_ports.py). One hit of B on A is ((A + B soldiers) / 300 + 300) x
// max(1, (C + B's 攻军 - A's 防御)^2 / 2000) / 1500 with C = 1000 (16afd0), so one hit's exchange is
// ((C + my 攻军 - its 防御) / (C + its 攻军 - my 防御))^2 and the soldiers cancel; 攻军 and 防御 are the panel values
// (battle base 2aaff0 x the formation's bytes, policies, traits), on the water the ship's (the force's +0x15e:
// 走舸 / 蒙冲 / 楼船, policy 培养水军). Measured: 楼船 against one tier lower 1.78 (formula 1.70), bank against ship 6.3.
// So, every day: an enemy army at war, on a fighting task, on the water (not a transport ship), inside a port's
// area or within ND_EDGE of its water, the port's 府 held by a force under test. That force's armies already out
// (intercept, area attack, paint defence; close combat or 雁行, ND_MIN+ soldiers) within ND_R of it, not touching
// an enemy on land, nearest first, until ND_ODDS x the enemies within ND_CLUS of it, at most ND_MAX, each pick the
// best of: water (our ship against its ship: attack it, the game takes it aboard), bank (our formation against
// its ship: the land hex touching it, 雁行 2 from it, if one is reachable over land) and wait (our formation
// against its land formation: stay on land, one afloat goes back to the bank; only while the enemy has been at
// the port less than ND_WAIT days). The values come from the game's own functions with the formation object
// swapped. Water wins when it is at least ND_WATER_PREF of the best (meet it early, keep the port). Re-chosen
// every day. A new order from the trees or the game is kept only if it would take an army not picked for the
// water onto it. Let go when the enemy is gone, has landed, is more than ND_LEAVE from the port's water, after
// ND_DAYS, or when ours is touched by an enemy on land. Whether to sortie at all stays the original's.
// v1.1 (2026-11-16, before it went into the default set): in the A/B of v1 (第四十七节) it sent armies whatever the
// odds and they lost 0.69 for 1 on the water. Now an army goes only by a way that wins (exchange ND_GO or more;
// when all three lose it keeps its own order), and nobody goes when those who could are together under half the
// enemies there (ND_HALF).
enum { OWNER_NAVAL = 20, ND_EDGE = 3, ND_LEAVE = 6, ND_R = 12, ND_MAX = 3, ND_DAYS = 12, ND_WAIT = 5, ND_MIN = 1500,
       ND_CLUS = 2, A_SHIP = 0x1c, FORCE_SHIP = 0x15e, ND_PORTS = 64, ND_TER_PORT = 18 };
enum : uint32_t { ROOT_FORMATION = 0x77428, N_FORMATION = 21, ROOT_FORCE = 0xdf30, N_FORCE_OBJ = 52 };
static const float ND_ODDS = 1.2f, ND_WATER_PREF = 0.9f, ND_GO = 1.0f, ND_HALF = 0.5f;
enum { ND_NONE = 0, ND_WATER = 1, ND_BANK = 2, ND_HOLD = 3 };
struct NdMem { uint16_t gen; int enemy; uint16_t enemyGen; int since; uint8_t mode; int hex; };
static NdMem g_ndm[N_ARMY];
struct NdSeen { uint16_t gen; int first, last; };
static NdSeen g_nds[N_ARMY];                 // enemy armies: days at a port's edge
static int16_t g_ndPort[N_HEX];              // water hex -> nearest port (index) whose area's water is within ND_LEAVE
static uint8_t g_ndDist[N_HEX];              // ... and its distance in water steps (255: none)
static int g_ndPortHex[ND_PORTS], g_ndNPorts = -1;
static volatile LONG g_ndLogged = 0;

static bool TransportShip(int ship) { return ship == 17 || ship == 18 || ship == 19 || ship == 20; }
static bool NdReady() { return g_n.unitBaseFn && g_n.unitAtkFn && g_n.unitDefFn && g_n.dmgC1; }
// the ports and, over the water, how far each water hex is from a port area's water (multi-source BFS, once)
static void NdMap(uint8_t *root) {
    static int q[N_HEX];
    static const int nb[6][2] = {{0, 2}, {0, -2}, {1, 1}, {1, -1}, {-1, 1}, {-1, -1}};
    g_ndNPorts = 0;
    memset(g_ndDist, 255, sizeof g_ndDist);
    for (int h = 0; h < (int)N_HEX; h++) g_ndPort[h] = -1;
    static int16_t portOfArea[N_AREA];
    for (int a = 0; a < (int)N_AREA; a++) portOfArea[a] = -1;
    for (int h = 0; h < (int)N_HEX && g_ndNPorts < ND_PORTS; h++)
        if (Terrain(root, h) == ND_TER_PORT) {
            int a = AreaOfHex(root, h);
            g_ndPortHex[g_ndNPorts] = h;
            if (a && portOfArea[a] < 0) portOfArea[a] = (int16_t)g_ndNPorts;
            g_ndNPorts++;
        }
    int qh = 0, qt = 0;
    for (int h = 0; h < (int)N_HEX; h++) {
        if (!Water(root, h)) continue;
        int a = AreaOfHex(root, h), p = a ? portOfArea[a] : -1;
        if (p < 0) {                          // an area with a port but hardly any water: water next to the port
            for (int i = 0; i < g_ndNPorts && p < 0; i++)
                if (HexDist(h, g_ndPortHex[i]) <= 1) p = i;
        }
        if (p < 0) continue;
        g_ndPort[h] = (int16_t)p; g_ndDist[h] = 0; q[qt++] = h;
    }
    while (qh < qt) {
        int h = q[qh++];
        if (g_ndDist[h] >= ND_LEAVE) continue;
        for (int i = 0; i < 6; i++) {
            int n = HexAt(h % MAP_W + nb[i][0], HexY(h) + nb[i][1]);
            if (n < 0 || g_ndDist[n] != 255 || !Water(root, n)) continue;
            g_ndDist[n] = (uint8_t)(g_ndDist[h] + 1); g_ndPort[n] = g_ndPort[h]; q[qt++] = n;
        }
    }
}
static int PortOwner(uint8_t *root, int p) {
    int a = AreaOfHex(root, g_ndPortHex[p]);
    uint8_t *ap = a ? Obj(root, ROOT_AREA, a) : nullptr;
    return IsA(ap, g_n.vtArea) ? ap[AR_OWNER] : 0;
}
static int ForceShip(uint8_t *root, int force) {
    uint8_t *fp = force > 0 && force < (int)N_FORCE_OBJ ? Obj(root, ROOT_FORCE, force) : nullptr;
    int s = fp ? fp[FORCE_SHIP] : 0;
    return s >= 11 && s <= 13 ? s : 11;
}
// one hit's exchange (its loss / ours) with army d in formation fd against army e in formation fe; -1 if unknown
static float NdRatio(uint8_t *root, const Army &d, int fd, const Army &e, int fe) {
    typedef float (*BaseFn)(void *, void *, int, int);
    typedef float (*StatFn)(float, void *, void *, void *, int, int);
    uint8_t *od = fd > 0 && fd < (int)N_FORMATION ? Obj(root, ROOT_FORMATION, fd) : nullptr;
    uint8_t *oe = fe > 0 && fe < (int)N_FORMATION ? Obj(root, ROOT_FORMATION, fe) : nullptr;
    if (!od || !oe) return -1;
    float bd = ((BaseFn)g_n.unitBaseFn)(d.p, nullptr, 1, 1), be = ((BaseFn)g_n.unitBaseFn)(e.p, nullptr, 1, 1);
    float ad = ((StatFn)g_n.unitAtkFn)(bd, d.p, nullptr, od, 0, 0), dd = ((StatFn)g_n.unitDefFn)(bd, d.p, nullptr, od, 0, 0);
    float ae = ((StatFn)g_n.unitAtkFn)(be, e.p, nullptr, oe, 0, 0), de = ((StatFn)g_n.unitDefFn)(be, e.p, nullptr, oe, 0, 0);
    float c = (float)*g_n.dmgC1, out = c + ad - de, in = c + ae - dd;
    if (out < 1) out = 1;
    if (in < 1) in = 1;
    return (out / in) * (out / in);
}
struct NdPick { int mode, hex; float rw, rb, rh; };
static NdPick NdChoose(uint8_t *root, const Army &d, const Army &e, int today, int atPortDays) {
    NdPick r{ND_NONE, -1, -1, -1, -1};
    int eShip = e.p[A_SHIP], dShip = ForceShip(root, d.force);
    bool dWet = Water(root, d.hex);
    r.rw = NdRatio(root, d, dShip, e, eShip >= 11 && eShip <= 13 ? eShip : 11);
    int bank = dWet ? -1 : ShoreHex(root, d, e, today);
    if (bank >= 0) r.rb = NdRatio(root, d, d.form, e, eShip >= 11 && eShip <= 13 ? eShip : 11);
    if (atPortDays < ND_WAIT) r.rh = NdRatio(root, d, d.form, e, e.form);
    float best = r.rw;
    if (r.rb > best) best = r.rb;
    if (r.rh > best) best = r.rh;
    if (best < ND_GO) return r;              // v1.1: every way loses: not this army
    if (r.rw >= ND_GO && r.rw >= ND_WATER_PREF * best) { r.mode = ND_WATER; return r; }
    if (r.rb >= best) { r.mode = ND_BANK; r.hex = bank; return r; }
    if (r.rh < ND_GO) { r.mode = r.rb >= ND_GO ? ND_BANK : ND_NONE; r.hex = r.rb >= ND_GO ? bank : -1; return r; }
    r.mode = ND_HOLD;
    r.hex = dWet ? BankHex(root, d, today) : d.hex;
    if (r.hex < 0) r.mode = r.rw >= ND_GO ? ND_WATER : ND_NONE;   // afloat with no bank near: fight where it is, if it wins
    return r;
}
static void NdOrder(Army &a, Mem &m, const NdPick &pk, const Army &e) {
    if (pk.mode == ND_WATER) Order(a.p, m, ACT_ATTACK_ARMY, TT_ARMY, e.slot);
    else if (pk.mode == ND_BANK) { Order(a.p, m, ACT_MOVE, TT_HEX, pk.hex); g_shRes[pk.hex] = 1; }
    else if (pk.hex == a.hex) Order(a.p, m, ACT_WAIT, TT_HEX, a.hex);
    else Order(a.p, m, ACT_MOVE, TT_HEX, pk.hex);
}
static bool NdSameOrder(const Army &a, const Mem &m, const NdPick &pk, const Army &e) {
    if (!m.mod || m.owner != OWNER_NAVAL) return false;
    if (pk.mode == ND_WATER) return m.wAct == ACT_ATTACK_ARMY && m.wType == TT_ARMY && m.wId == e.slot;
    return m.wType == TT_HEX && m.wId == (uint16_t)pk.hex && (m.wAct == ACT_MOVE || m.wAct == ACT_WAIT);
}
static const wchar_t *NdModeName(int m) { return m == ND_WATER ? L"water" : m == ND_BANK ? L"bank" : m == ND_HOLD ? L"wait" : L"-"; }

static void DayNavalDef(uint8_t *root, int player, uint32_t date) {
    g_owner = OWNER_NAVAL;
    if (!NdReady() || !g_n.vtArea) return;
    int today = DayNo(date);
    if (g_ndNPorts < 0) {
        NdMap(root);
        logf(L"%u-%u-%u navaldef: %d ports; damage constant %d", date / 10000, date / 100 % 100, date % 100, g_ndNPorts,
             *g_n.dmgC1);
    }
    if (!g_shore) memset(g_shRes, 0, sizeof g_shRes);        // with shore on, DayShore (just before) cleared it and holds its bank hexes
    // threats: enemy armies on the water at the edge of a port held by a force under test
    static int16_t thrPort[N_ARMY];
    static int sent[N_ARMY], nsent[N_ARMY];
    int nThreat = 0;
    for (int k = 0; k < g_nArmies; k++) {
        const Army &e = g_armies[k];
        thrPort[k] = -1; sent[k] = 0; nsent[k] = 0;
        if (!Water(root, e.hex) || TransportShip(e.p[A_SHIP]) || !FightingTask(TroopsTask(root, e.troops))) continue;
        int p = g_ndPort[e.hex];
        if (p < 0 || g_ndDist[e.hex] > ND_EDGE) continue;
        int f = PortOwner(root, p);
        if (!f || f == e.force || !OursToTouch(f, player) || !AtWar(f, e.force, today)) continue;
        thrPort[k] = (int16_t)p;
        NdSeen &sn = g_nds[e.slot];
        uint16_t gen = U16(e.p, A_GENERAL);
        if (sn.gen != gen || today - sn.last > 3) sn = NdSeen{gen, today, today};
        sn.last = today;
        nThreat++;
    }
    int kept = 0, changed = 0, rel[5] = {0, 0, 0, 0, 0}, modes[4] = {0, 0, 0, 0}, overruled = 0, noWay = 0, tooFew = 0;
    // ongoing
    for (int k = 0; k < g_nArmies; k++) {
        Army &a = g_armies[k];
        NdMem &nm = g_ndm[a.slot];
        if (nm.gen != U16(a.p, A_GENERAL)) nm = NdMem{U16(a.p, A_GENERAL), -1, 0, 0, 0, -1};
        if (nm.enemy < 0) continue;
        Mem *m = Track(a, player);
        if (!m) { nm.enemy = -1; continue; }
        if (!m->mod || m->owner != OWNER_NAVAL) {
            // a new order: keep it unless it takes an army we keep off the water onto it
            int act = a.p[g_n.offAction], tt = a.p[g_n.offTType], id = U16(a.p, g_n.offTId);
            bool wet = (act == ACT_ATTACK_ARMY && tt == TT_ARMY && id > 0 && id < (int)N_ARMY && g_bySlot[id] >= 0 &&
                        Water(root, g_armies[g_bySlot[id]].hex)) || (act == ACT_MOVE && tt == TT_HEX && Water(root, id));
            if (nm.mode == ND_WATER || !wet || !Mine(m)) { nm.enemy = -1; rel[4]++; continue; }
            overruled++;
        }
        int ei = nm.enemy > 0 && nm.enemy < (int)N_ARMY ? g_bySlot[nm.enemy] : -1;
        int why = -1;                          // 0 gone, 1 landed, 2 left, 3 timeout or contact
        if (ei < 0 || U16(g_armies[ei].p, A_GENERAL) != nm.enemyGen) why = 0;
        else if (!Water(root, g_armies[ei].hex)) why = 1;
        else if (g_ndDist[g_armies[ei].hex] > ND_LEAVE) why = 2;
        else if (today - nm.since > ND_DAYS || (!Water(root, a.hex) && LandFoeNear(root, a.force, a.hex, 1, today))) why = 3;
        NdPick pk{};
        if (why < 0) {
            const Army &e = g_armies[ei];
            pk = NdChoose(root, a, e, today, today - g_nds[e.slot].first);
            if (pk.mode == ND_NONE) why = 3;
        }
        if (why >= 0) {
            if (m->mod && m->owner == OWNER_NAVAL) Restore(a.p, *m);
            nm.enemy = -1;
            rel[why]++;
            continue;
        }
        const Army &e = g_armies[ei];
        if (pk.mode != nm.mode) changed++;
        nm.mode = (uint8_t)pk.mode; nm.hex = pk.hex;
        if (!NdSameOrder(a, *m, pk, e)) NdOrder(a, *m, pk, e);
        else if (pk.mode == ND_BANK) g_shRes[pk.hex] = 1;
        sent[ei] += U16(a.p, A_SOLDIERS);
        nsent[ei]++;
        modes[pk.mode]++;
        kept++;
    }
    // new: the strongest threats first
    int order[N_ARMY], no = 0, fresh = 0;
    for (int k = 0; k < g_nArmies; k++) if (thrPort[k] >= 0) order[no++] = k;
    for (int i = 1; i < no; i++)
        for (int j = i; j > 0 && U16(g_armies[order[j]].p, A_SOLDIERS) > U16(g_armies[order[j - 1]].p, A_SOLDIERS); j--) {
            int x = order[j]; order[j] = order[j - 1]; order[j - 1] = x;
        }
    for (int oi = 0; oi < no; oi++) {
        int k = order[oi];
        const Army &e = g_armies[k];
        int p = thrPort[k], f = PortOwner(root, p), clus = 0;
        for (int j = 0; j < g_nArmies; j++) {
            const Army &x = g_armies[j];
            if (x.force == e.force && Water(root, x.hex) && HexDist(x.hex, e.hex) <= ND_CLUS) clus += U16(x.p, A_SOLDIERS);
        }
        int need = (int)(ND_ODDS * clus);
        // who would go: the nearest first, each by its best way; those with no way that wins are passed over
        static int tried[N_ARMY], stamp = 0;
        stamp++;
        int cand[ND_MAX], candD[ND_MAX], nc = 0, could = sent[k];
        NdPick picks[ND_MAX];
        while (nsent[k] + nc < ND_MAX && could < need) {
            int best = -1, bestD = 99;
            for (int j = 0; j < g_nArmies; j++) {
                Army &a = g_armies[j];
                if (tried[a.slot] == stamp) continue;
                if (a.force != f || IsShip(a.form) || !ShoreForm(a.form) || a.p[g_n.offLock] || g_ndm[a.slot].enemy >= 0) continue;
                if (U16(a.p, A_SOLDIERS) < ND_MIN || !ShoreTask(TroopsTask(root, a.troops)) || !FieldOrder(a.p[g_n.offAction])) continue;
                int d = HexDist(a.hex, e.hex);
                if (d > ND_R || d >= bestD) continue;
                if (!Water(root, a.hex) && LandFoeNear(root, a.force, a.hex, 1, today)) continue;
                Mem *m = Track(a, player);
                if (!m || !Mine(m)) continue;
                best = j; bestD = d;
            }
            if (best < 0) break;
            Army &a = g_armies[best];
            tried[a.slot] = stamp;
            NdPick pk = NdChoose(root, a, e, today, today - g_nds[e.slot].first);
            if (pk.mode == ND_NONE) { noWay++; continue; }
            if (pk.mode == ND_BANK) g_shRes[pk.hex] = 1;
            cand[nc] = best; candD[nc] = bestD; picks[nc] = pk; nc++;
            could += U16(a.p, A_SOLDIERS);
        }
        if (nc && could < ND_HALF * clus) {      // together under half of them: nobody goes
            for (int i = 0; i < nc; i++)
                if (picks[i].mode == ND_BANK) g_shRes[picks[i].hex] = 0;
            tooFew++;
            continue;
        }
        for (int i = 0; i < nc; i++) {
            Army &a = g_armies[cand[i]];
            Mem *m = Track(a, player);
            const NdPick &pk = picks[i];
            NdMem &nm = g_ndm[a.slot];
            nm.gen = U16(a.p, A_GENERAL);
            nm.enemy = e.slot; nm.enemyGen = U16(e.p, A_GENERAL); nm.since = today; nm.mode = (uint8_t)pk.mode; nm.hex = pk.hex;
            NdOrder(a, *m, pk, e);
            sent[k] += U16(a.p, A_SOLDIERS);
            nsent[k]++;
            modes[pk.mode]++;
            fresh++;
            if (InterlockedIncrement(&g_ndLogged) <= 400)
                logf(L"%u-%u-%u navaldef: army %d (force %d, formation %d, ship %d, %d soldiers) %ls against army %d "
                     L"(force %d, formation %d, ship %d, %d soldiers, %d with those near it, at the port %d days) by port "
                     L"hex %d (force %d), %d away; exchange water %.2f, bank %.2f, wait %.2f", date / 10000,
                     date / 100 % 100, date % 100, a.slot, a.force, a.form, ForceShip(root, a.force), U16(a.p, A_SOLDIERS),
                     NdModeName(pk.mode), e.slot, e.force, e.form, e.p[A_SHIP], U16(e.p, A_SOLDIERS), clus,
                     today - g_nds[e.slot].first, g_ndPortHex[p], f, candD[i], pk.rw, pk.rb, pk.rh);
        }
    }
    if (nThreat || kept || fresh || noWay || tooFew || rel[0] + rel[1] + rel[2] + rel[3] + rel[4])
        logf(L"%u-%u-%u navaldef: threats %d; armies new %d, kept %d (water %d, bank %d, wait %d; changed %d, kept off the "
             L"water %d); released: enemy gone %d, landed %d, left %d, timeout or contact or losing %d, new order %d; not "
             L"sent: no way that wins %d armies, under half of the enemies %d threats", date / 10000,
             date / 100 % 100, date % 100, nThreat, fresh, kept, modes[ND_WATER], modes[ND_BANK], modes[ND_HOLD], changed,
             overruled, rel[0], rel[1], rel[2], rel[3], rel[4], noWay, tooFew);
}

// ---- relief (援军继续守) ------------------------------------------------------------------------------
// 2026-11-15 (BEHAVIOR_PLAN 第四十八节; user: 「A 城市派出援军支援 B 城市，B 城市第一波敌人退了，但是第二波敌人到了，
// A 城市援军不是继续支援而是直接回城，等回到城市后再派出去支援 B」，「应该看支援的城市是否还在被敌人攻势即将攻击，是否有
// 敌人大部队赶过来以该城市为目标，如果该城市还有被攻击威胁，应该继续支援该城市攻击来犯敌人」). The original, once the army
// it intercepts is gone, only looks for alert-list enemies within 6 of the lead army (troops tree [136]) and sends the
// troops home otherwise; armies march about 1.2 hexes a day and that choice holds for ten days. In five one-year runs the
// second wave was 8-14 hexes away when such relief troops turned home, and they were sent back 20-odd days later.
// So, every day: an army of a force under test whose troops were intercepting within RL_RECENT days and are now going
// home (task 17/18, the army's order: to its city), not a ship or an engine, RL_MIN+ soldiers and RL_MORALE+ morale,
// its home city holding RL_FOOD+ food. If another city of ours within RL_NEAR of it (and nearer than home) is still
// threatened: enemy troops at war going for it (siege task, that city as the goal) with armies within RL_THREAT of it,
// or enemy armies on a fighting task within RL_CLOSE of it. Then it stays for that city instead of going home:
// - the nearest attacker (engines first) is within RL_ENGAGE of it or of the city, or our armies within RL_HELP of
//   that attacker (this one included) have at least RL_ODDS of the enemy soldiers within RL_CLUS of it: attack it;
// - otherwise go into that city (the tree itself sends the troops home to the nearest city at the next xun).
// Re-chosen every day. v1 (smoke 197, runs/20261115-171036) waited on a hex 2 from 陈留 instead: three armies stood there
// 10-17 days while a siege army 7 away was being fought, because the threat counted the second wave 18 away too. Let go (its order home given back) when the city has no threat left or is no longer ours, after
// RL_DAYS, or below RL_KEEP soldiers / RL_KEEP_MORALE morale; dropped without giving back when its troops get a new
// task or another feature takes it. Whether the city is defended at all stays the original's: only troops that were
// already relieving near it are kept.
enum { OWNER_RELIEF = 21, RL_RECENT = 12, RL_NEAR = 12, RL_THREAT = 20, RL_CLOSE = 6, RL_OWN = 12, RL_DAYS = 20,
       RL_MIN = 1500, RL_KEEP = 1000, RL_MORALE = 50, RL_KEEP_MORALE = 40, RL_FOOD = 5000, RL_ENGAGE = 4, RL_HELP = 6,
       RL_CLUS = 3, C_FOOD = 0x38, RL_ATTACK = 1, RL_INTO = 2 };
static const float RL_ODDS = 0.6f;
struct RlMem { uint16_t gen; int city, since; uint8_t act, type, mode; uint16_t id; };
struct RlTroop { uint16_t leader; int lastIntercept; };
static RlMem g_rlm[N_ARMY];
static RlTroop g_rlt[N_TROOPS];
static int g_rlThr[N_CITY], g_rlThrN[N_CITY], g_rlThrD[N_CITY], g_rlOwn[N_CITY];
static volatile LONG g_rlLogged = 0;

static int CityFood(uint8_t *root, int c) {
    uint8_t *cp = c > 0 && c < (int)N_CITY ? Obj(root, ROOT_CITY, c) : nullptr;
    if (!cp) return 0;
    int v; memcpy(&v, cp + C_FOOD, 4);
    return v;
}
// an attacker of city c (index in g_armies): at war with its holder f, siege troops going for it within RL_THREAT,
// or any army on a fighting task within RL_CLOSE
static bool RlAttacker(uint8_t *root, const Army &e, int c, int ch, int f, int today) {
    if (e.force == f || !AtWar(f, e.force, today) || (Water(root, e.hex) && TransportShip(e.p[A_SHIP]))) return false;
    int d = HexDist(e.hex, ch);
    if (d > RL_THREAT) return false;
    uint8_t *tp = e.troops > 0 && e.troops < (int)N_TROOPS ? Obj(root, ROOT_TROOPS, e.troops) : nullptr;
    int task = tp ? U16(tp, T_PURPOSE) : -1;
    if (SiegeTask(task) && U16(tp, T_PURPOSE + 2) == c) return true;
    return d <= RL_CLOSE && FightingTask(task);
}
static int RlTarget(uint8_t *root, const Army &a, int c, int ch, int today) {
    int best = -1, bd = 999;
    for (int k = 0; k < g_nArmies; k++) {
        const Army &e = g_armies[k];
        if (!RlAttacker(root, e, c, ch, a.force, today)) continue;
        int d = HexDist(a.hex, e.hex) - (IsEngine(e.form) ? 3 : 0);   // engines first, unless much farther
        if (d < bd) { bd = d; best = k; }
    }
    return best;
}
// attack the target (index in g_armies) rather than go into the city?
// (2026-11-16: a v1.2 that kept yesterday's choice until the odds passed 0.4 / 0.8 was tried and taken out: the kept
// armies walked at far enemies for days and ended outside the city, BEHAVIOR_PLAN 第五十节.)
static bool RlGo(uint8_t *root, const Army &a, int ti, int ch, int today) {
    if (ti < 0) return false;
    const Army &e = g_armies[ti];
    if (HexDist(e.hex, a.hex) <= RL_ENGAGE || HexDist(e.hex, ch) <= RL_ENGAGE) return true;
    int foe = 0, ours = HexDist(a.hex, e.hex) > RL_HELP ? U16(a.p, A_SOLDIERS) : 0;
    for (int k = 0; k < g_nArmies; k++) {
        const Army &x = g_armies[k];
        int d = HexDist(x.hex, e.hex);
        if (x.force == a.force) { if (d <= RL_HELP) ours += U16(x.p, A_SOLDIERS); }
        else if (d <= RL_CLUS && AtWar(a.force, x.force, today)) foe += U16(x.p, A_SOLDIERS);
    }
    return ours >= RL_ODDS * foe;
}

static void DayRelief(uint8_t *root, int player, uint32_t date) {
    g_owner = OWNER_RELIEF;
    int today = DayNo(date);
    // troops intercepting today
    for (int t = 1; t < (int)N_TROOPS; t++) {
        uint8_t *tp = Obj(root, ROOT_TROOPS, t);
        RlTroop &rt = g_rlt[t];
        uint16_t lead = tp ? U16(tp, T_LEADER) : 0;
        if (rt.leader != lead) rt = RlTroop{lead, -999};
        if (tp && lead && U16(tp, T_PURPOSE) == 1) rt.lastIntercept = today;
    }
    // threats and our strength by city (only cities of forces under test that someone may stay for)
    for (int c = 1; c < (int)N_CITY; c++) { g_rlThr[c] = -1; g_rlThrN[c] = 0; g_rlThrD[c] = 99; g_rlOwn[c] = 0; }
    auto threat = [&](int c) {
        if (g_rlThr[c] >= 0) return g_rlThr[c];
        int f = CityForce(root, c), ch = CityHex(root, c);
        g_rlThr[c] = 0;
        if (!f || ch < 0) return 0;
        for (int k = 0; k < g_nArmies; k++) {
            const Army &e = g_armies[k];
            if (e.force == f) {
                if (HexDist(e.hex, ch) <= RL_OWN) g_rlOwn[c] += U16(e.p, A_SOLDIERS);
                continue;
            }
            if (!RlAttacker(root, e, c, ch, f, today)) continue;
            g_rlThr[c] += U16(e.p, A_SOLDIERS);
            g_rlThrN[c]++;
            int d = HexDist(e.hex, ch);
            if (d < g_rlThrD[c]) g_rlThrD[c] = d;
        }
        return g_rlThr[c];
    };
    auto homebound = [&](const Army &a) {
        int t = TroopsTask(root, a.troops);
        return t == 17 || t == 18;
    };
    int kept = 0, modes[3] = {0, 0, 0}, reissued = 0, fresh = 0, rel[6] = {0, 0, 0, 0, 0, 0};
    int cand = 0, noThreat = 0, lowFood = 0, weak = 0;
    for (int i = 1; i < (int)N_ARMY; i++)                    // 5: gone (into a city, or wiped out)
        if (g_rlm[i].city && g_bySlot[i] < 0) { g_rlm[i].city = 0; rel[5]++; }
    // ongoing
    for (int k = 0; k < g_nArmies; k++) {
        Army &a = g_armies[k];
        RlMem &rm = g_rlm[a.slot];
        uint16_t gen = U16(a.p, A_GENERAL);
        if (rm.gen != gen) rm = RlMem{gen, 0, 0, 0, 0, 0, 0};
        if (!rm.city) continue;
        Mem *m = Track(a, player);
        if (!m || !homebound(a) || !Mine(m)) { rm.city = 0; rel[3]++; continue; }   // new task, or another feature's
        int c = rm.city, ch = CityHex(root, c), why = -1;     // 0 no threat, 1 timeout, 2 weak, 4 city lost
        if (CityForce(root, c) != a.force || ch < 0) why = 4;
        else if (!threat(c)) why = 0;
        else if (today - rm.since > RL_DAYS) why = 1;
        else if (U16(a.p, A_SOLDIERS) < RL_KEEP || a.p[A_MORALE] < RL_KEEP_MORALE) why = 2;
        if (why >= 0) {
            Order(a.p, *m, rm.act, rm.type, rm.id);           // home, as the tree had it
            m->mod = 0;
            rm.city = 0;
            rel[why]++;
            continue;
        }
        int ti = RlTarget(root, a, c, ch, today);
        bool go = RlGo(root, a, ti, ch, today);
        if (a.p[g_n.offLock]) { modes[rm.mode]++; kept++; continue; }   // in a fight the game holds it
        uint8_t act = a.p[g_n.offAction], tt = a.p[g_n.offTType];
        uint16_t tid = U16(a.p, g_n.offTId);
        bool ours = m->mod && m->owner == OWNER_RELIEF;
        if (go) {
            const Army &e = g_armies[ti];
            if (!(act == ACT_ATTACK_ARMY && tt == TT_ARMY && tid == e.slot)) {
                if (!ours) reissued++;
                Order(a.p, *m, ACT_ATTACK_ARMY, TT_ARMY, e.slot);
            }
            rm.mode = RL_ATTACK;
        } else {
            if (!((act == ACT_GO_CITY || act == ACT_ENTER_CITY) && tt == TT_CITY && tid == c)) {
                if (!ours) reissued++;
                Order(a.p, *m, ACT_GO_CITY, TT_CITY, c);
            }
            rm.mode = RL_INTO;
        }
        modes[rm.mode]++;
        kept++;
    }
    // new: going home from an intercept, another city of ours nearby still threatened
    for (int k = 0; k < g_nArmies; k++) {
        Army &a = g_armies[k];
        RlMem &rm = g_rlm[a.slot];
        if (rm.city || IsShip(a.form) || IsEngine(a.form) || a.p[g_n.offLock] || !homebound(a)) continue;
        if (a.troops <= 0 || a.troops >= (int)N_TROOPS || today - g_rlt[a.troops].lastIntercept > RL_RECENT) continue;
        uint8_t act = a.p[g_n.offAction], tt = a.p[g_n.offTType];
        int home = U16(a.p, g_n.offTId);
        if ((act != ACT_GO_CITY && act != ACT_ENTER_CITY) || tt != TT_CITY || CityForce(root, home) != a.force) continue;
        Mem *m = Track(a, player);
        if (!m || !Mine(m) || m->mod) continue;
        cand++;
        int hh = CityHex(root, home), dh = hh >= 0 ? HexDist(a.hex, hh) : 99, best = -1, bd = 99;
        for (int c = 1; c < (int)N_CITY; c++) {
            if (c == home || CityForce(root, c) != a.force) continue;
            int ch = CityHex(root, c), d = ch >= 0 ? HexDist(a.hex, ch) : 99;
            if (d > RL_NEAR || d >= dh || d >= bd || !threat(c)) continue;
            best = c; bd = d;
        }
        if (best < 0) { noThreat++; continue; }
        if (CityFood(root, home) < RL_FOOD) { lowFood++; continue; }
        if (U16(a.p, A_SOLDIERS) < RL_MIN || a.p[A_MORALE] < RL_MORALE) { weak++; continue; }
        int c = best, ch = CityHex(root, c);
        rm = RlMem{U16(a.p, A_GENERAL), c, today, act, tt, 0, (uint16_t)home};
        int ti = RlTarget(root, a, c, ch, today);
        bool go = RlGo(root, a, ti, ch, today);
        if (go) {
            Order(a.p, *m, ACT_ATTACK_ARMY, TT_ARMY, g_armies[ti].slot);
            rm.mode = RL_ATTACK;
        } else {
            Order(a.p, *m, ACT_GO_CITY, TT_CITY, c);
            rm.mode = RL_INTO;
        }
        modes[rm.mode]++;
        fresh++;
        if (InterlockedIncrement(&g_rlLogged) <= 400)
            logf(L"%u-%u-%u relief: army %d (force %d, troops %d, %d soldiers, morale %d) going home to city %d (%d away) "
                 L"stays for city %d (%d away): attackers %d soldiers in %d armies, nearest %d from the city; ours within %d "
                 L"of it %d -> %ls %d", date / 10000, date / 100 % 100, date % 100, a.slot, a.force, a.troops,
                 U16(a.p, A_SOLDIERS), a.p[A_MORALE], home, dh, c, bd, g_rlThr[c], g_rlThrN[c], g_rlThrD[c], RL_OWN,
                 g_rlOwn[c], go ? L"attack army" : L"into city", go ? g_armies[ti].slot : c);
    }
    if (cand || kept || fresh || rel[0] + rel[1] + rel[2] + rel[3] + rel[4] + rel[5])
        logf(L"%u-%u-%u relief: home from an intercept %d (no threatened city of ours nearer than home %d, home low on food "
             L"%d, weak %d); armies new %d, kept %d (attack %d, into the city %d; orders put back %d); released: no "
             L"threat %d, timeout %d, weak %d, new task or taken %d, city lost %d, gone (into a city or wiped out) %d",
             date / 10000, date / 100 % 100, date % 100, cand, noThreat, lowFood, weak, fresh, kept, modes[RL_ATTACK],
             modes[RL_INTO], reissued, rel[0], rel[1], rel[2], rel[3], rel[4], rel[5]);
}

// ---- gate (守关) ------------------------------------------------------------------------------------
// User (2026-11-13): 「守关不是兵多，而是防守的方式总是大排长龙」. 203 default run, 武关 8-21: six of 刘表's
// armies, all ordered to attack one army outside, stood in single file down the two-hex pass behind the gate;
// only the one at the gate could shoot, the next day all retreated. Close combat cannot reach through the
// gate; 雁行 shoots at 2, so from the hexes just behind the gate it hits the attackers at the gate. Stance
// only moves a 雁行 that is within 4 of an enemy and 2 steps of a firing hex; the ones further down the
// queue walk on with the tree's order. So, every day, while enemies at war stand within 2 of a gate held by
// a force under test: its 雁行 within 10 of the gate (on intercept / area tasks, not in contact) go to a free
// hex 2 from an attacker, on our side of the gate (reached without passing gate hexes, terrain 17), nearest
// first; a close-combat army of ours standing on such a hex while one of our 雁行 there has none steps back
// to the nearest free hex of ours 3+ from the attackers. Given back when no enemy stands at the gate.
// v2 (2026-11-13, user with a screenshot of ten armies queued behind 阳平关: 「守关隘，这里只要远程在关后面就行，
// 排队再多也没用」): every other army of ours near the gate that cannot reach an attacker without passing the
// gate (close combat, and 雁行 left without a firing hex) goes into our nearest city instead of queuing.
enum { GT_NEAR = 2, GT_REACH = 10, GT_WALK = 14, OWNER_GATE = 12, TER_GATE = 17, HX_OWNER = 0x14 };
static int NearestOwnCity(uint8_t *root, int force, int hex) {
    int best = 0, bd = 1 << 30;
    for (int c = 1; c < (int)N_CITY; c++) {
        if (CityForce(root, c) != force) continue;
        int h = CityHex(root, c);
        if (h < 0) continue;
        int d = HexDist(h, hex);
        if (d < bd) { bd = d; best = c; }
    }
    return best;
}
struct GateMem { uint16_t gen; int gate, hex; };
static GateMem g_gtm[N_ARMY];
static uint8_t g_gtRes[N_HEX];
static uint16_t g_gwGen[N_HEX], g_gwStamp;
static int16_t g_gwDist[N_HEX];
static int g_gwQ[N_HEX];
// hexes reachable from `from` without passing a gate hex or water, within GT_WALK steps (stamp g_gwGen)
static void GateWalk(uint8_t *root, int from) {
    if (++g_gwStamp == 0) { memset(g_gwGen, 0, sizeof g_gwGen); g_gwStamp = 1; }
    static const int nb[6][2] = {{0, 2}, {0, -2}, {1, 1}, {1, -1}, {-1, 1}, {-1, -1}};
    int qh = 0, qt = 0;
    g_gwGen[from] = g_gwStamp; g_gwDist[from] = 0; g_gwQ[qt++] = from;
    while (qh < qt) {
        int h = g_gwQ[qh++];
        if (g_gwDist[h] >= GT_WALK) continue;
        for (int i = 0; i < 6; i++) {
            int n = HexAt(h % MAP_W + nb[i][0], HexY(h) + nb[i][1]);
            if (n < 0 || g_gwGen[n] == g_gwStamp) continue;
            int t = Terrain(root, n);
            if (t < 0 || !g_walk[t] || t == TER_GATE || t == TER_RIVER || t == TER_WATER2) continue;
            g_gwGen[n] = g_gwStamp; g_gwDist[n] = (int16_t)(g_gwDist[h] + 1); g_gwQ[qt++] = n;
        }
    }
}
static bool GateReached(int h) { return g_gwGen[h] == g_gwStamp; }
static bool GateTask(int t) { return t == 1 || t == 3 || t == 13 || t == 14 || t == 15 || t == 16 || t == 21 || t == 23; }

// v3 (2026-11-13, user: 「继续做」): the tree cannot tell a gate (in its neighbour lists no foothold is without money:
// 197 probe, a month), so the choice moves to the army creation. Each day DayGate notes, for every city of ours
// within GT_CITY of an attacked gate of ours, whether the nearest enemy army at war is one of that gate's
// attackers. When the tree's intercept departure (CDepartureNode; return address = sig departure_create_call)
// creates an army in such a city, a general who can use 雁行 goes as 雁行, at most GT_ARCH per gate (ours
// already within GT_REACH of it count); anyone else is not created: 1ed8e0 returns null, the node fails and the
// intercept loop goes on to the next general (the node handles a null army: 332410(null) is 0).
enum { GT_CITY = 20, GT_ARCH = 3, TT_GATE = 6, GT_POST = 2 };
static SRWLOCK g_gateLock = SRWLOCK_INIT;
static int g_cityGate[N_CITY];            // gate foothold id (52..61) when the city's nearest enemy attacks it
static int g_gateArch[N_GATE];            // our 雁行 near that gate (counted daily, plus the ones made since)
static volatile LONG g_gateYan = 0, g_gateNot = 0;
static int GateCreate(uint8_t *req) {     // 0 as asked, 1 as 雁行, -1 not created
    uint8_t *root = g_n.rootPtr ? *g_n.rootPtr : nullptr;
    if (!root || !g_dataOk) return 0;
    int pid = *(int32_t *)req;
    if (pid <= 0 || pid >= (int)N_PERSON) return 0;
    uint8_t *person = Obj(root, ROOT_PERSON, pid);
    if (!person) return 0;
    int city = U16(person, P_WHERE), form;
    if (city <= 0 || city >= (int)N_CITY) return 0;
    memcpy(&form, req + 0x0c, 4);
    if (IsEngine(form) || IsShip(form)) return 0;
    uint32_t mask = g_n.fmtMaskOff ? *(uint32_t *)(person + g_n.fmtMaskOff) : 0;
    int r = 0, gid, n = 0;
    AcquireSRWLockExclusive(&g_gateLock);
    gid = g_cityGate[city];
    if (gid > 51 && gid - 51 < (int)N_GATE) {
        n = g_gateArch[gid - 51];
        if ((mask >> F_YANXING & 1) && n < GT_ARCH) { g_gateArch[gid - 51] = n + 1; r = 1; }
        else r = -1;
    }
    ReleaseSRWLockExclusive(&g_gateLock);
    if (!r) return 0;
    InterlockedIncrement(r > 0 ? &g_gateYan : &g_gateNot);
    int soldiers;
    memcpy(&soldiers, req + REQ_SOLDIERS, 4);
    uint32_t date = g_lastDate;
    logf(L"%u-%u-%u gate: intercept from city %d while gate %d is attacked: person %d (%d soldiers, formation %d) %ls",
         date / 10000, date / 100 % 100, date % 100, city, gid, pid, soldiers, form,
         r > 0 ? L"goes as 雁行" : (mask >> F_YANXING & 1) ? L"not sent (enough 雁行 there)" : L"not sent (no 雁行)");
    return r;
}

static uint8_t g_gtHot[N_GATE];           // v5: the gate is attacked today
static volatile LONG g_guarded = 0;
static void DayGate(uint8_t *root, int player, uint32_t date) {
    g_owner = OWNER_GATE;
    int today = DayNo(date);
    memset(g_gtRes, 0, sizeof g_gtRes);
    memset(g_gtSide, 0, sizeof g_gtSide);
    memset(g_gtHot, 0, sizeof g_gtHot);
    int attacked = 0, toFire = 0, held = 0, stepBack = 0, released = 0, home = 0, stay = 0;
    static int atk[64];
    static int cityGate[N_CITY], gateArch[N_GATE];
    memset(cityGate, 0, sizeof cityGate);
    memset(gateArch, 0, sizeof gateArch);
    for (int gi = 1; gi < (int)N_GATE; gi++) {
        int gid = 51 + gi, gh = FootholdHex(root, gid);
        if (gh < 0) continue;
        uint8_t *gx = Obj(root, ROOT_HEX, gh);
        int owner = gx ? gx[HX_OWNER] : 0;
        if (!owner || !OursToTouch(owner, player)) continue;
        int na = 0;
        for (int k = 0; k < g_nArmies && na < 64; k++) {
            const Army &e = g_armies[k];
            if (e.force != owner && !IsShip(e.form) && AtWar(owner, e.force, today) && HexDist(e.hex, gh) <= GT_NEAR)
                atk[na++] = k;
        }
        // let go of the armies of this gate when nobody attacks it
        for (int k = 0; k < g_nArmies; k++) {
            Army &a = g_armies[k];
            GateMem &gm = g_gtm[a.slot];
            if (gm.gen != U16(a.p, A_GENERAL)) gm = GateMem{U16(a.p, A_GENERAL), 0, -1};
            if (gm.gate != gid) continue;
            Mem *m = Track(a, player);
            bool ours = m && m->mod && m->owner == OWNER_GATE;
            if (!ours) { gm.gate = 0; continue; }
            if (!na) {
                if (m->wAct == ACT_ENTER_CITY) m->mod = 0;      // on its way into the city: let it go on
                else Restore(a.p, *m);
                gm.gate = 0;
                released++;
            }
        }
        // v3: the cities of ours whose nearest enemy at war threatens the gate (stands within GT_NEAR + 1 of it, or,
        // v3.1, heads for it: its target is the gate or the gate's hex; in 203 刘表's close combat left 宛 for 武关
        // the day before the attackers reached the gate), and our 雁行 near the gate
        auto threat = [&](const Army &e) {
            int tt = e.p[g_n.offTType], tid = U16(e.p, g_n.offTId);
            return HexDist(e.hex, gh) <= GT_NEAR + 1 || (tt == TT_GATE && tid == gi) || (tt == TT_HEX && tid == gh);
        };
        bool threatened = na > 0;
        for (int k = 0; k < g_nArmies && !threatened; k++) {
            const Army &e = g_armies[k];
            if (e.force != owner && !IsShip(e.form) && AtWar(owner, e.force, today) && threat(e)) threatened = true;
        }
        if (threatened) {
            for (int k = 0; k < g_nArmies; k++) {
                const Army &a = g_armies[k];
                if (a.force == owner && a.form == F_YANXING && HexDist(a.hex, gh) <= GT_REACH) gateArch[gi]++;
            }
            for (int c = 1; c < (int)N_CITY; c++) {
                int ch = CityForce(root, c) == owner ? CityHex(root, c) : -1;
                if (ch < 0 || HexDist(ch, gh) > GT_CITY) continue;
                int bd = 1 << 30, bk = -1;
                for (int k = 0; k < g_nArmies; k++) {
                    const Army &e = g_armies[k];
                    if (e.force == owner || IsShip(e.form) || !AtWar(owner, e.force, today)) continue;
                    int d = HexDist(e.hex, ch);
                    if (d < bd) { bd = d; bk = k; }
                }
                if (bk >= 0 && threat(g_armies[bk])) cityGate[c] = gid;
            }
        }
        if (!na) continue;
        attacked++;
        g_gtHot[gi] = 1;
        g_gtHex[gi] = gh;
        auto distAtk = [&](int h) {
            int b = 99;
            for (int i = 0; i < na; i++) { int d = HexDist(h, g_armies[atk[i]].hex); if (d < b) b = d; }
            return b;
        };
        // v4.5: a firing hex already given stays with its 雁行 while it is on the way (in 203 霍峻, out of 宛 ten days
        // after 黄祖 and with a lower army number, took the only firing hex behind 武关 from him; 黄祖 went back)
        for (int k = 0; k < g_nArmies; k++) {
            Army &a = g_armies[k];
            if (a.force != owner || a.form != F_YANXING) continue;
            GateMem &gm = g_gtm[a.slot];
            if (gm.gen != U16(a.p, A_GENERAL) || gm.gate != gid || gm.hex < 0) continue;
            Mem *m = Track(a, player);
            if (m && m->mod && m->owner == OWNER_GATE && m->wAct == ACT_MOVE && m->wId == gm.hex &&
                (distAtk(gm.hex) == 2 || HexDist(gm.hex, gh) <= GT_POST) && NearestEnemy(gm.hex, a.force) >= 2)
                g_gtRes[gm.hex] = 2;                                // 2: kept for the army that has it
        }
        // pass 0: our 雁行 to firing hexes; pass 1: close combat off a firing hex a 雁行 of ours still needs
        for (int pass = 0; pass < 2; pass++) {
            for (int k = 0; k < g_nArmies; k++) {
                Army &a = g_armies[k];
                if (a.force != owner || a.p[g_n.offLock] || IsShip(a.form) || HexDist(a.hex, gh) > GT_REACH) continue;
                bool archer = a.form == F_YANXING;
                if (pass == 0 && !archer) continue;
                if (pass == 1 && IsEngine(a.form)) continue;
                if (!GateTask(TroopsTask(root, a.troops))) continue;
                Mem *m = Track(a, player);
                // v4.7: near an attacked gate the 雁行 not in contact are the gate's, whatever stance or target ordered
                // (in 203 stance sent 黄祖 to a hex 2 from 刘备's 雁行, which lay outside 武关; he lost 1400 there)
                if (!m || (pass == 1 && !Mine(m)) || !FieldOrder(a.p[g_n.offAction])) continue;
                int de = NearestEnemy(a.hex, a.force);
                GateMem &gm = g_gtm[a.slot];
                if (pass == 0) {
                    if (de < 2) continue;                                   // in contact: stance backs it off
                    // the hex its order leads to (-1: none we can tell); "outside": near the gate, not on our side
                    auto orderHex = [&]() {
                        int tt = a.p[g_n.offTType], tid = U16(a.p, g_n.offTId);
                        if (tt == TT_HEX && tid < (int)N_HEX) return tid;
                        if (tt == TT_ARMY && tid > 0 && tid < (int)N_ARMY && g_bySlot[tid] >= 0) return g_armies[g_bySlot[tid]].hex;
                        return -1;
                    };
                    auto stayHere = [&](int th) {
                        gm.gate = gid; gm.hex = a.hex;
                        if (m->mod && m->owner == OWNER_GATE && m->wAct == ACT_WAIT && m->wId == a.hex) { held++; return; }
                        Order(a.p, *m, ACT_WAIT, TT_HEX, a.hex);
                        stay++;
                        logf(L"%u-%u-%u gate: 雁行 army %d (force %d, %d soldiers) at gate %d: its order (to hex %d, through the gate or onto itself) "
                             L"becomes waiting at hex %d", date / 10000, date / 100 % 100, date % 100, a.slot, a.force,
                             U16(a.p, A_SOLDIERS), gid, th, a.hex);
                    };
                    GateWalk(root, a.hex);
                    {   // v5: our side of this gate around it, for the target guard (from armies no attacker can walk to)
                        bool reachAtk = false;
                        for (int i = 0; i < na && !reachAtk; i++) reachAtk = GateReached(g_armies[atk[i]].hex);
                        if (!reachAtk)
                            for (int dy = -GT_SIDE; dy <= GT_SIDE; dy++)
                                for (int dx = -GT_SIDE; dx <= GT_SIDE; dx++) {
                                    int yy = gh / MAP_W + dy, xx = gh % MAP_W + dx;
                                    if (yy < 0 || yy >= MAP_W || xx < 0 || xx >= MAP_W) continue;
                                    int h = yy * MAP_W + xx;
                                    if (HexDist(h, gh) <= GT_SIDE && GateReached(h)) g_gtSide[h] = (uint8_t)gi;
                                }
                    }
                    {   // v4.9: enemy ranged armies in range of it (over the gate) with 30% more soldiers than it: step
                        // back on our side out of their range at once (the retreat judgment runs once a 旬; in 197
                        // 杜畿's 3333 behind 潼关 were shot to 43 by 满宠's 雁行 in six days)
                        auto range = [](int f) { return f == 10 ? 3 : (f == F_YANXING || f == 8) ? 2 : 0; };
                        int rs = 0;
                        for (int j = 0; j < g_nArmies; j++) {
                            const Army &e = g_armies[j];
                            if (e.force == owner || !AtWar(owner, e.force, today) || !range(e.form)) continue;
                            if (HexDist(e.hex, a.hex) <= range(e.form)) rs += U16(e.p, A_SOLDIERS);
                        }
                        if (rs * 10 > U16(a.p, A_SOLDIERS) * 13) {
                            int bh = -1, bdd = 1 << 30;
                            for (int dy = -4; dy <= 4; dy++)
                                for (int dx = -4; dx <= 4; dx++) {
                                    int yy = a.hex / MAP_W + dy, xx = a.hex % MAP_W + dx;
                                    if (yy < 0 || yy >= MAP_W || xx < 0 || xx >= MAP_W) continue;
                                    int h = yy * MAP_W + xx;
                                    if (h == a.hex || !GateReached(h) || g_gtRes[h] || !Walkable(root, h)) continue;
                                    bool safe = true;
                                    for (int j = 0; j < g_nArmies && safe; j++) {
                                        const Army &e = g_armies[j];
                                        if (e.force != owner && range(e.form) && AtWar(owner, e.force, today) &&
                                            HexDist(e.hex, h) <= range(e.form)) safe = false;
                                    }
                                    if (safe && NearestEnemy(h, a.force) >= 2 && g_gwDist[h] < bdd) { bdd = g_gwDist[h]; bh = h; }
                                }
                            if (bh >= 0) {
                                g_gtRes[bh] = 1;
                                gm.gate = gid; gm.hex = bh;
                                if (!(m->mod && m->owner == OWNER_GATE && m->wAct == ACT_MOVE && m->wId == bh)) {
                                    Order(a.p, *m, ACT_MOVE, TT_HEX, bh);
                                    stepBack++;
                                    logf(L"%u-%u-%u gate: 雁行 army %d (force %d, %d soldiers) is shot over gate %d by %d: back to "
                                         L"hex %d", date / 10000, date / 100 % 100, date % 100, a.slot, a.force,
                                         U16(a.p, A_SOLDIERS), gid, rs, bh);
                                } else held++;
                                continue;
                            }
                        }
                    }
                    if (de == 2 && distAtk(a.hex) == 2) {                   // already shooting
                        g_gtRes[a.hex] = 1;
                        // v4.7: and stays (stance or the tree may give it a hex 2 from an enemy outside the gate);
                        // v4.8: a move to its own hex is turned into waiting there as well
                        int th = orderHex();
                        if (th >= 0 && (th == a.hex ? a.p[g_n.offAction] != ACT_WAIT
                                                    : !GateReached(th) && HexDist(th, gh) <= GT_NEAR + 3)) stayHere(th);
                        continue;
                    }
                    int best = -1, bd = 1 << 30;
                    for (int i = 0; i < na; i++) {
                        int eh = g_armies[atk[i]].hex, y = eh / MAP_W, x = eh % MAP_W;
                        for (int dy = -2; dy <= 2; dy++)
                            for (int dx = -2; dx <= 2; dx++) {
                                int yy = y + dy, xx = x + dx;
                                if (yy < 0 || yy >= MAP_W || xx < 0 || xx >= MAP_W) continue;
                                int h = yy * MAP_W + xx;
                                bool mine = g_gtRes[h] == 2 && h == gm.hex && gm.gate == gid;
                                if (HexDist(eh, h) != 2 || (g_gtRes[h] && !mine) || !GateReached(h)) continue;
                                if (h != a.hex && !Walkable(root, h)) continue;
                                if (NearestEnemy(h, a.force) < 2) continue;
                                int sc = mine ? -1 : g_gwDist[h];
                                if (sc < bd) { bd = sc; best = h; }
                            }
                    }
                    bool post = false;
                    if (best < 0) {
                        // v4.6: no firing hex now: the gate post, a free hex of our side within GT_POST of the gate,
                        // nearest the gate (in 203 the attackers kept stepping between 1 and 3 from 武关, the firing
                        // hex came and went, and the 雁行 told to wait stood 10 back). From there it shoots whoever
                        // comes up to the gate; close combat cannot reach it. One already on a post stays.
                        int py = gh / MAP_W, px = gh % MAP_W;
                        for (int dy = -GT_POST; dy <= GT_POST; dy++)
                            for (int dx = -GT_POST; dx <= GT_POST; dx++) {
                                int yy = py + dy, xx = px + dx;
                                if (yy < 0 || yy >= MAP_W || xx < 0 || xx >= MAP_W) continue;
                                int h = yy * MAP_W + xx;
                                if (h == gh || HexDist(h, gh) > GT_POST || Terrain(root, h) == TER_GATE) continue;
                                bool mine = g_gtRes[h] == 2 && h == gm.hex && gm.gate == gid;
                                if ((g_gtRes[h] && !mine) || !GateReached(h)) continue;
                                if (h != a.hex && !Walkable(root, h)) continue;
                                if (NearestEnemy(h, a.force) < 2) continue;
                                int sc = h == a.hex ? -2 : mine ? -1 : HexDist(h, gh) * 100 + g_gwDist[h];
                                if (sc < bd) { bd = sc; best = h; }
                            }
                        post = best >= 0;
                    }
                    if (best < 0) {
                        // v4.2: no firing hex on our side. In 203 the tree turned 黄祖's troops into an area attack on
                        // 刘备's 雁行 standing 2 outside 武关; 黄祖 walked out through the gate and lost 1200 of his 2000
                        // there. An order whose target lies near the gate on the other side waits on our side instead.
                        int th = orderHex();
                        if (th < 0 || GateReached(th) || HexDist(th, gh) > GT_NEAR + 3) continue;
                        stayHere(th);
                        continue;
                    }
                    g_gtRes[best] = 1;
                    gm.gate = gid; gm.hex = best;
                    if (best == a.hex) {
                        // v4.8: already on it: wait there, the order the game itself gives an army that stops (a move
                        // to its own hex, 4.6-4.7, was re-given every day, and once 黄祖 walked out of 武关 under it)
                        if (a.p[g_n.offAction] == ACT_WAIT && a.p[g_n.offTType] == TT_HEX && U16(a.p, g_n.offTId) == a.hex)
                            held++;
                        else {
                            Order(a.p, *m, ACT_WAIT, TT_HEX, a.hex);
                            stay++;
                        }
                        continue;
                    }
                    if (!(m->mod && m->wAct == ACT_MOVE && m->wId == best)) {
                        Order(a.p, *m, ACT_MOVE, TT_HEX, best);
                        toFire++;
                        logf(L"%u-%u-%u gate: 雁行 army %d (force %d, %d soldiers) -> hex %d, %ls gate %d, %d steps",
                             date / 10000, date / 100 % 100, date % 100, a.slot, a.force, U16(a.p, A_SOLDIERS), best,
                             post ? L"the post behind" : L"2 from the attackers of", gid, g_gwDist[best]);
                    } else held++;
                } else {
                    // v2: cannot reach an attacker without passing the gate -> into our nearest city
                    // v2.1: 雁行 without a firing hex waits for one (sending it home made the tree send it again)
                    if (de < 2 || archer) continue;
                    if (gm.gate == gid && m->mod && m->owner == OWNER_GATE && m->wAct == ACT_ENTER_CITY) { held++; continue; }
                    GateWalk(root, a.hex);
                    bool reach = false;
                    for (int i = 0; i < na && !reach; i++) reach = GateReached(g_armies[atk[i]].hex);
                    if (reach) continue;
                    int c = NearestOwnCity(root, a.force, a.hex);
                    if (!c) continue;
                    gm.gate = gid; gm.hex = -1;
                    Order(a.p, *m, ACT_ENTER_CITY, TT_CITY, c);
                    home++;
                    logf(L"%u-%u-%u gate: army %d (force %d, formation %d, %d soldiers) cannot reach the attackers of gate %d: "
                         L"into city %d", date / 10000, date / 100 % 100, date % 100, a.slot, a.force, a.form,
                         U16(a.p, A_SOLDIERS), gid, c);
                }
            }
        }
    }
    AcquireSRWLockExclusive(&g_gateLock);
    memcpy(g_cityGate, cityGate, sizeof cityGate);
    memcpy(g_gateArch, gateArch, sizeof gateArch);
    ReleaseSRWLockExclusive(&g_gateLock);
    long yan = InterlockedExchange(&g_gateYan, 0), notSent = InterlockedExchange(&g_gateNot, 0);
    long guarded = InterlockedExchange(&g_guarded, 0);
    if (guarded) logf(L"%u-%u-%u gate: targets outside a gate turned into waiting yesterday: %ld", date / 10000,
                      date / 100 % 100, date % 100, guarded);
    if (attacked || released || yan || notSent)
        logf(L"%u-%u-%u gate: gates attacked %d; 雁行 sent to firing hexes %d, kept from going through the gate %d, "
             L"stepped back from stronger archers %d (kept %d), sent into the city %d, released %d; intercepts made as "
             L"雁行 %ld, not made %ld", date / 10000, date / 100 % 100, date % 100, attacked, toFire, stay, stepBack, held,
             home, released, yan, notSent);
}

// v5 (2026-11-14): the daily layer runs when the date changes; the trees run after it, and an army walks under the
// tree's new order the same day. In 203 the 旬初 order of 12-1 sent 黄祖 from the hex behind 武关 out through the gate
// (4.1, 4.6, 4.8 alike). So the set-target function itself (2ecfe0, every order of the game and the trees goes
// through it) is hooked: a 雁行 of ours held at a gate attacked today, standing on our side of it, whose new target
// lies near the gate (GT_NEAR + 3) but not on our side, waits where it is instead.
typedef void (*SetTargetFn)(uint8_t *, int, int);
static SetTargetFn g_setTargetOrig;
static void GateGuard(uint8_t *army) {
    uint8_t *root = g_n.rootPtr ? *g_n.rootPtr : nullptr;
    if (!root || !g_dataOk || army[A_FORMATION] != F_YANXING || army[g_n.offLock]) return;
    int slot = -1;
    for (int k = 0; k < g_nArmies; k++)
        if (g_armies[k].p == army) { slot = g_armies[k].slot; break; }
    if (slot < 0) return;
    const GateMem &gm = g_gtm[slot];
    int gi = gm.gate - 51;
    if (gm.gen != U16(army, A_GENERAL) || gi <= 0 || gi >= (int)N_GATE || !g_gtHot[gi]) return;
    int tt = army[g_n.offTType], tid = U16(army, g_n.offTId), th = -1, hex = U16(army, A_HEX);
    if (tt == TT_HEX && tid < (int)N_HEX) th = tid;
    else if (tt == TT_ARMY && tid > 0 && tid < (int)N_ARMY) {
        uint8_t *t = Obj(root, ROOT_ARMY, tid);
        if (IsA(t, g_n.vtArmy)) th = U16(t, A_HEX);
    }
    int gh = FootholdHex(root, gm.gate);
    if (th < 0 || th >= (int)N_HEX || hex >= (int)N_HEX || gh < 0) return;
    if (HexDist(th, gh) > GT_NEAR + 3 || g_gtSide[th] == gi || g_gtSide[hex] != gi) return;
    army[g_n.offAction] = ACT_WAIT;
    army[g_n.offTType] = TT_HEX;
    uint16_t v = (uint16_t)hex;
    memcpy(army + g_n.offTId, &v, 2);
    if (InterlockedIncrement(&g_guarded) <= 20)
        logf(L"%u-%u-%u gate: 雁行 army %d at gate %d was given a target outside (hex %d): waits at hex %d",
             g_lastDate / 10000, g_lastDate / 100 % 100, g_lastDate % 100, slot, gm.gate, th, hex);
}
static void HkSetTarget(uint8_t *army, int type, int id) {
    g_setTargetOrig(army, type, id);
    if (g_gate && army) GateGuard(army);
}

// v4 (2026-11-14, user: 「接着做」): in 203 黄祖's 雁行 reached the hex behind 武关 and two days later its whole troops
// turned back to 宛: the retreat module counts the enemy strength within 8 of the lead, and the attackers at the
// gate were more than ours, though they cannot reach us through the gate. The module now brackets that sum with
// MiscLogNode(95, 0, $lead) .. MiscLogNode(94); in between, on that thread, an enemy army counts 0 when it can reach
// the lead's hex in REACH_STEPS steps only by passing a gate that is not its own (two walks from the lead: gates
// open, gates shut). Walls of mountains or water count as before: only the gate makes the difference.
enum { REACH_STEPS = 16, REACH_FAR = 12 };
static SRWLOCK g_reachLock = SRWLOCK_INIT;
static uint16_t g_rgOpen[N_HEX], g_rgShut[N_HEX], g_rgStamp;
static int16_t g_rgDist[N_HEX];
static int g_rgQ[N_HEX];
static int g_rcHex = -1, g_rcForce = -1;
static uint32_t g_rcDate = 0;
static volatile LONG g_reachChecks = 0, g_reachCalls = 0, g_reachZero = 0, g_reachLogged = 0;
static int ArmyForce(uint8_t *root, const uint8_t *army) {
    int gen = U16(army, A_GENERAL);
    if (gen <= 0 || gen >= (int)N_PERSON) return 0;
    uint8_t *pp = Obj(root, ROOT_PERSON, gen);
    int dist = pp ? pp[P_DISTRICT] : 0;
    uint8_t *dp = dist > 0 && dist < (int)N_DISTRICT ? Obj(root, ROOT_DISTRICT, dist) : nullptr;
    return dp ? dp[D_FORCE] : 0;
}
static void ReachWalk(uint8_t *root, int from, int force, bool open, uint16_t *gen) {
    static const int nb[6][2] = {{0, 2}, {0, -2}, {1, 1}, {1, -1}, {-1, 1}, {-1, -1}};
    int qh = 0, qt = 0;
    gen[from] = g_rgStamp; g_rgDist[from] = 0; g_rgQ[qt++] = from;
    while (qh < qt) {
        int h = g_rgQ[qh++];
        if (g_rgDist[h] >= REACH_STEPS) continue;
        for (int i = 0; i < 6; i++) {
            int n = HexAt(h % MAP_W + nb[i][0], HexY(h) + nb[i][1]);
            if (n < 0 || gen[n] == g_rgStamp) continue;
            int t = Terrain(root, n);
            if (t < 0) continue;
            if (t == TER_GATE) {
                uint8_t *hx = Obj(root, ROOT_HEX, n);
                if (!open && (!hx || hx[HX_OWNER] != force)) continue;
            } else if (!g_walk[t] && t != TER_RIVER && t != TER_WATER2) continue;
            gen[n] = g_rgStamp; g_rgDist[n] = (int16_t)(g_rgDist[h] + 1); g_rgQ[qt++] = n;
        }
    }
}
static void ReachBegin(uint8_t *node, void *ctx) {
    t_reachPend = -1;
    uint8_t *root = g_n.rootPtr ? *g_n.rootPtr : nullptr;
    if (!g_gate || !root || !g_dataOk || !g_n.bbGet || !g_n.bbObj) return;
    uint32_t key = *(uint32_t *)(node + g_n.logTypeOff + 8);
    void *bb = ((void *(*)(void *))g_n.bbGet)(ctx);
    uint8_t *obj = nullptr;
    if (!bb || !(((uint64_t(*)(void *, uint32_t, void *, int))g_n.bbObj)(bb, key, &obj, 0) & 0xff)) return;
    if (!IsA(obj, g_n.vtArmy)) return;
    int hex = U16(obj, A_HEX), force = ArmyForce(root, obj);
    if (hex >= (int)N_HEX || !force) return;
    t_reachPend = hex;
    t_reachPendForce = force;
    InterlockedIncrement(&g_reachChecks);
}
// ---- siegefu (攻城门槛折进府加成) ----------------------------------------------------------------------
// 2026-11-14 (BEHAVIOR_PLAN 第四十三节; user: the siege estimate leaves out the 府加成, 「改门槛」): the district
// tree's siege decision takes the enemy strength $b57eaf65 from GetFootholdFightingStrengthNode (7e640: bb980 +
// b6a10, both through 2aaff0 with the 府 flag off) and needs our strength > it x 2.1 + 耐久 x 0.4 to go, then
// drafts up to x 2.6. In the fights at the city the defender has 府分 3-4 (x1.3 at setting 10), we about 1.
// The module puts MiscLogNode(93, 0, $target) right after the strength (and the 都督 好战 script); here the
// strength is multiplied by the defender's 府加成 over ours (the game's own 2b2b30), each averaged over the
// hexes 1-3 from the city where the attackers stand (the city hex itself sits in the defender's core and gave
// x1.4-1.8 in the smoke run 20261114-165030, the fights around it about x1.3), kept within 0.75-1.6. Our side:
// the district in $d6a5eaae (GetMyselfDistrictNode). Only cities.
enum : uint32_t { SF_DISTRICT_KEY = 0xd6a5eaae, SF_STRENGTH_KEY = 0xb57eaf65 };
enum { SF_R = 3 };
static volatile LONG g_sfuDone = 0, g_sfuSkipped = 0;
static void SiegeFu(uint8_t *node, void *ctx) {
    uint8_t *root = g_n.rootPtr ? *g_n.rootPtr : nullptr;
    if (!g_siegeFu || !root || !g_dataOk || !g_n.bbGet || !g_n.bbObj || !g_n.bbInt || !g_n.bbSet || !g_n.bonusFn) return;
    typedef uint64_t (*ObjFn)(void *, uint32_t, void *, int);
    typedef uint64_t (*IntFn)(void *, uint32_t, int *, int);
    typedef uint64_t (*SetFn)(void *, uint32_t, int);
    typedef float (*BonusFn)(void *, int, int *);
    uint32_t key = *(uint32_t *)(node + g_n.logTypeOff + 8);
    void *bb = ((void *(*)(void *))g_n.bbGet)(ctx);
    uint8_t *city = nullptr, *dist = nullptr;
    int e = 0;
    if (!bb || !(((ObjFn)g_n.bbObj)(bb, key, &city, 0) & 0xff) || !IsA(city, g_n.vtCity) ||
        !(((ObjFn)g_n.bbObj)(bb, SF_DISTRICT_KEY, &dist, 0) & 0xff) || !IsA(dist, g_n.vtDistrict)) {
        InterlockedIncrement(&g_sfuSkipped);
        return;
    }
    ((IntFn)g_n.bbInt)(bb, SF_STRENGTH_KEY, &e, 0);
    int att = dist[D_FORCE], cd = city[C_DISTRICT];
    uint8_t *dp = cd > 0 && cd < (int)N_DISTRICT ? Obj(root, ROOT_DISTRICT, cd) : nullptr;
    int def = dp ? dp[D_FORCE] : 0, oi = U16(city, C_OBJECT);
    uint8_t *op = oi > 0 && oi < (int)N_OBJECT ? Obj(root, ROOT_OBJECT, oi) : nullptr;
    int hex = op ? U16(op, O_HEX) : -1;
    if (e <= 0 || !att || !def || att == def || hex < 0 || hex >= (int)N_HEX) { InterlockedIncrement(&g_sfuSkipped); return; }
    float md = 0, ma = 0;
    int sd = 0, sa = 0, nh = 0;
    for (int dy = -SF_R; dy <= SF_R; dy++)
        for (int dx = -SF_R; dx <= SF_R; dx++) {
            int yy = hex / MAP_W + dy, xx = hex % MAP_W + dx;
            if (yy < 0 || yy >= MAP_W || xx < 0 || xx >= MAP_W) continue;
            int h = yy * MAP_W + xx, dd = HexDist(h, hex);
            if (dd < 1 || dd > SF_R || !AreaOfHex(root, h)) continue;
            uint8_t *hp = Obj(root, ROOT_HEX, h);
            int s1 = 0, s2 = 0;
            float a1 = hp ? ((BonusFn)g_n.bonusFn)(hp, def, &s1) : 0, a2 = hp ? ((BonusFn)g_n.bonusFn)(hp, att, &s2) : 0;
            if (!(a1 >= 0.5f && a1 <= 4.0f) || !(a2 >= 0.5f && a2 <= 4.0f)) continue;
            md += a1; ma += a2; sd += s1; sa += s2; nh++;
        }
    if (!nh) { InterlockedIncrement(&g_sfuSkipped); return; }
    md /= nh; ma /= nh; sd /= nh; sa /= nh;
    // what it will be in the fight: in 70 sieges of the 8-module runs (research/siege_force.py) the defender's 府分
    // at the attackers' hexes was 0.77 of its value around the city on departure (the attackers paint as they
    // come) and the attackers had about 1 (a fifth of the defender's 4.4 then)
    float mdF = 1.0f + 0.77f * (md - 1.0f), maF = 1.0f + 0.2f * (md - 1.0f);
    if (ma > maF) maF = ma;
    float r = mdF / maF;
    r = r < 0.75f ? 0.75f : r > 1.6f ? 1.6f : r;
    int e2 = (int)(e * r + 0.5f);
    ((SetFn)g_n.bbSet)(bb, SF_STRENGTH_KEY, e2);
    LONG n = InterlockedIncrement(&g_sfuDone);
    if (n <= 80) {
        uint32_t date = g_lastDate;
        logf(L"%u-%u-%u siegefu: force %d sizing up city %d (force %d, hex %d): enemy strength %d -> %d "
             L"(府 over %d hexes 1-3 from the city: defender sum %d x%.3f, ours sum %d x%.3f; in the fight x%.3f / x%.3f)",
             date / 10000, date / 100 % 100, date % 100, att, U16(city, C_OBJECT), def, hex, e, e2, nh, sd, md, sa, ma,
             mdF, maF);
    }
}

// the next GetHexAroundFightingStrengthNode on this thread after MiscLogNode(95) (the module puts it right after)
static uint64_t HkStrengthAround(uint8_t *node, void *ctx, void *a3, void *a4) {
    int c = t_reachPend;
    t_reachPend = -1;
    if (c >= 0) { t_reachHex = c; t_reachForce = t_reachPendForce; }
    uint64_t r = ((uint64_t(*)(void *, void *, void *, void *))g_n.saExec)(node, ctx, a3, a4);
    t_reachHex = -1;
    return r;
}
// inside the bracket: does this enemy army reach the lead only through a gate that is not its own?
static bool ReachBlocked(const uint8_t *army) {
    uint8_t *root = g_n.rootPtr ? *g_n.rootPtr : nullptr;
    if (!root) return false;
    int eh = U16(army, A_HEX), ef = ArmyForce(root, army), c = t_reachHex, form = army[A_FORMATION];
    if (eh >= (int)N_HEX || !ef || ef == t_reachForce || HexDist(eh, c) > REACH_FAR) return false;
    // v4.3/4.4: ranged armies shoot over the gate (雁行 3 and 井阑 8 at 2, 投石 10 at 3). In 197 李傕's 杜畿 (雁行,
    // 3333) held the hex behind 潼关 while 满宠's 雁行 (7000) stood 2 outside, and was wiped. Counting every ranged
    // army (4.3) turned 黄祖 back 6 hexes before 武关 because of 刘备's 雁行, which in 4.1 never came within 2 of the
    // firing hex (黄祖 held it 16 days without a loss). So a ranged army counts once it is in range of the lead.
    if ((form == F_YANXING || form == 8) && HexDist(eh, c) <= 2) return false;
    if (form == 10 && HexDist(eh, c) <= 3) return false;
    bool blocked;
    AcquireSRWLockExclusive(&g_reachLock);
    if (g_rcHex != c || g_rcForce != ef || g_rcDate != g_lastDate) {
        if (++g_rgStamp == 0) { memset(g_rgOpen, 0, sizeof g_rgOpen); memset(g_rgShut, 0, sizeof g_rgShut); g_rgStamp = 1; }
        ReachWalk(root, c, ef, true, g_rgOpen);
        ReachWalk(root, c, ef, false, g_rgShut);
        g_rcHex = c; g_rcForce = ef; g_rcDate = g_lastDate;
    }
    blocked = g_rgOpen[eh] == g_rgStamp && g_rgShut[eh] != g_rgStamp;
    ReleaseSRWLockExclusive(&g_reachLock);
    if (blocked && InterlockedIncrement(&g_reachLogged) <= 30) {
        uint32_t date = g_lastDate;
        logf(L"%u-%u-%u gate: retreat check of the lead at hex %d (force %d): enemy army at hex %d (force %d, %d soldiers) "
             L"reaches it only through a gate, not counted", date / 10000, date / 100 % 100, date % 100, c, t_reachForce,
             eh, ef, U16(army, A_SOLDIERS));
    }
    return blocked;
}

// ---- ford (渡江迎击) ----------------------------------------------------------------------------------
// User (2026-11-13, 「可以开放」): open the defenders' intercept judgment for enemies on water. The district
// tree sums the strength of the enemy armies near a threatening troops' lead with b6a10 (soldiers x combat
// value) and sizes the intercept by that sum; it does not know that an army on water fights at a fraction of
// it (one on one, the side on water loses 5-20 times the soldiers of the side on land, naval_stats.py).
// Enemy armies wait on the river 6-9 hexes from our cities for weeks while the defenders stay inside.
// The module "ford" brackets the judgment's strength sums with MiscLogNode(97) .. MiscLogNode(96); between
// them, on this thread only, b6a10 returns half for an army on water (terrain 14, 24). The module also reads
// the lead's strength once with and once without the bracket to tell the tree that the lead is on water.
enum { STRENGTH_STEAL = 14 };
typedef float (*StrengthFn)(uint8_t *army);
static StrengthFn g_strengthOrig;
static volatile LONG g_fordCalls, g_fordHalved;
static float HkStrength(uint8_t *army) {
    float r = g_strengthOrig(army);
    if (t_reachHex >= 0 && army && r > 0) {
        InterlockedIncrement(&g_reachCalls);
        if (ReachBlocked(army)) { InterlockedIncrement(&g_reachZero); return 0.0f; }
    }
    if (t_ford && army) {
        uint8_t *root = g_n.rootPtr ? *g_n.rootPtr : nullptr;
        InterlockedIncrement(&g_fordCalls);
        int hex = U16(army, A_HEX);
        if (root && hex < (int)N_HEX) {
            int t = Terrain(root, hex);
            if (t == TER_RIVER || t == TER_WATER2) { r *= 0.5f; InterlockedIncrement(&g_fordHalved); }
        }
    }
    return r;
}

static uint64_t HkProgressUpdate(uint8_t *self) {
    uint64_t r = ((uint64_t(*)(void *))g_n.progUpdate)(self);
    uint8_t *root = *g_n.rootPtr;
    if (!root || g_dataBad) return r;
    uint8_t *st = Ptr(root, ROOT_STATUS);
    if (!st) return r;
    uint32_t date = U16(st, ST_DATE) * 10000u + st[ST_DATE + 2] * 100u + st[ST_DATE + 3];
    if (date == g_lastDate) return r;
    if (g_probe && g_n.logExec && g_lastDate) FlushProbes(g_lastDate);   // what the trees did on that day
    if (g_ford && g_lastDate && g_fordCalls) {
        logf(L"%u-%u-%u ford: %ld army strengths read in the intercept judgment, %ld of them on water (halved)",
             g_lastDate / 10000, g_lastDate / 100 % 100, g_lastDate % 100, (long)g_fordCalls, (long)g_fordHalved);
        InterlockedExchange(&g_fordCalls, 0);
        InterlockedExchange(&g_fordHalved, 0);
    }
    if (g_gate && g_lastDate && g_reachChecks) {
        logf(L"%u-%u-%u gate: retreat checks %ld, enemy strengths read %ld, not counted (behind a gate) %ld",
             g_lastDate / 10000, g_lastDate / 100 % 100, g_lastDate % 100, (long)g_reachChecks, (long)g_reachCalls,
             (long)g_reachZero);
        InterlockedExchange(&g_reachChecks, 0);
        InterlockedExchange(&g_reachCalls, 0);
        InterlockedExchange(&g_reachZero, 0);
        InterlockedExchange(&g_reachLogged, 0);
    }
    // a save was loaded (date went back, or jumped more than a month): forget the orders we remember
    auto days = [](uint32_t d) { return (int)(d / 10000) * 372 + (int)(d / 100 % 100) * 31 + (int)(d % 100); };
    if (g_lastDate && (date < g_lastDate || days(date) - days(g_lastDate) > 31)) {
        memset(g_mem, 0, sizeof g_mem);
        memset(g_war, 0, sizeof g_war);
        memset(g_tm, 0, sizeof g_tm);
        memset(g_rlm, 0, sizeof g_rlm);
        memset(g_rlt, 0, sizeof g_rlt);
        memset(g_slm, 0, sizeof g_slm);
        logf(L"date %u -> %u: a save was loaded, remembered orders cleared", g_lastDate, date);
    }
    g_lastDate = date;
    if (!CheckData(root)) return r;
    int player = st[ST_PLAYER];
    CollectArmies(root);
    if (g_defend || g_assign || g_fu || g_shore || g_gate || g_sguard || g_sgLog || g_naval || g_relief || g_slone) LearnWars(root, DayNo(date));
    if (g_stance) DayStance(root, player, date);
    if (g_defend) DayDefend(root, player, date);
    if (g_gate) DayGate(root, player, date);
    if (g_shore) DayShore(root, player, date);
    if (g_naval) DayNavalDef(root, player, date);
    if (g_relief) DayRelief(root, player, date);
    if (g_assign) DayTarget(root, player, date);
    if (g_fu) DayFu(root, player, date);
    if (g_prep && g_fu) DaySiegePrep(root, player, date);
    if (g_sguard || g_sgLog) DaySupplyGuard(root, player, date);
    if (g_siege) DaySiege(root, player, date);
    if (g_slone) DaySiegeLone(root, player, date);
    if (g_bonus) DayBonus(root, date);
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
    {   // the set-target function starts 60 bytes into the army_setters match; its first 15 bytes are 3 movs/push/sub
        static const uint8_t head[15] = {0x48, 0x89, 0x5C, 0x24, 0x08, 0x48, 0x89, 0x74, 0x24, 0x10, 0x57, 0x48, 0x83, 0xEC, 0x20};
        if (!memcmp(h.p + 60, head, 15)) a.setTargetFn = h.p + 60;
    }
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
    a.vtTroops = VtableByRtti(a.base, ".?AVCTroopsData@@");
    a.vtGate = VtableByRtti(a.base, ".?AVCGateData@@");
    if (!a.vtArmy || !a.vtPerson || !a.vtHex || !a.vtDistrict || !a.vtWorld || !a.vtCity || !a.vtObject || !a.vtTroops ||
        !a.vtGate)
        return L"data class vtables not found";
    // optional: the siege departure (scomplog, siegeenough), army creation (gate)
    if (SigScan(text, tn, SIG_siege_assign_fn, &h)) a.assignFn = h.p;
    if (SigScan(text, tn, SIG_person_stat_fn, &h)) a.statFn = h.p;
    if (SigScan(text, tn, SIG_army_create_fn, &h)) a.createFn = h.p;
    if (SigScan(text, tn, SIG_army_strength_fn, &h)) a.strengthFn = h.p;
    if (SigScan(text, tn, SIG_siege_launch_check, &h)) a.launchJbe = h.p + h.f[F_siege_launch_check_jbe].at;
    if (SigScan(text, tn, SIG_departure_create_call, &h)) a.depCreateRet = h.p + h.f[F_departure_create_call_ret].at;
    if (SigScan(text, tn, SIG_person_formation_fn, &h)) a.fmtMaskOff = SigU32(h, F_person_formation_fn_mask_off);
    // optional: the 府加成 log
    a.vtArea = VtableByRtti(a.base, ".?AVCAreaData@@");
    if (a.vtArea && SigScan(text, tn, SIG_area_bonus_fn, &h)) a.bonusFn = h.p;
    if (SigScan(text, tn, SIG_unit_base_fn, &h)) a.unitBaseFn = h.p;
    if (SigScan(text, tn, SIG_unit_attack_fn, &h)) a.unitAtkFn = h.p;
    if (SigScan(text, tn, SIG_unit_defense_fn, &h)) a.unitDefFn = h.p;
    if (SigScan(text, tn, SIG_damage_c1, &h)) a.dmgC1 = (int *)SigRel(h, F_damage_c1_c1);
    // optional: the tree probes (missing only turns them off)
    a.logVt = VtableByRtti(a.base, kRtti_misclog_vtable);
    if (a.logVt && SigAt((uint8_t *)a.logVt[SLOT_misclog_vtable_execute], SIG_misclog_execute, &h))
        a.logTypeOff = h.p[h.f[F_misclog_execute_type_off].at];
    else
        a.logVt = nullptr;
    // optional: reading a tree variable (gate v4)
    void **sv = VtableByRtti(a.base, kRtti_strength_around_vtable);
    if (sv && SigAt((uint8_t *)sv[SLOT_strength_around_vtable_execute], SIG_strength_around_execute, &h)) {
        a.bbGet = SigRel(h, F_strength_around_execute_bb_get);
        a.bbObj = SigRel(h, F_strength_around_execute_bb_obj);
        a.saVt = sv;
    }
    // optional: writing a tree integer (siegefu)
    void **fv = VtableByRtti(a.base, kRtti_foothold_strength_vtable);
    if (fv && SigAt((uint8_t *)fv[SLOT_foothold_strength_vtable_execute], SIG_foothold_strength_execute, &h))
        a.bbInt = SigRel(h, F_foothold_strength_execute_bb_int);
    if (SigScan(text, tn, SIG_foothold_strength_set, &h))
        a.bbSet = SigRel(h, F_foothold_strength_set_bb_set);
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
        else if ((g_probe || g_ford || g_gate || g_siegeFu) && !(g_n.logVt && WriteSlot(&g_n.logVt[SLOT_misclog_vtable_execute],
                                                                           (void *)HkMiscLog, &g_n.logExec)))
            logf(L"tree probes / ford, gate and siegefu markers NOT installed (CMiscLogNode not found or not writable)");
        if (g_siegeFu)
            logf(g_n.bbGet && g_n.bbObj && g_n.bbInt && g_n.bbSet && g_n.bonusFn && g_n.logExec
                     ? L"siegefu: siege decisions take the defender's 府加成 (blackboard int +%llx / +%llx)"
                     : L"siegefu NOT installed (blackboard int functions, 府加成 or CMiscLogNode not found) %llx %llx",
                 (unsigned long long)(g_n.bbInt ? (uint8_t *)g_n.bbInt - g_n.base : 0),
                 (unsigned long long)(g_n.bbSet ? (uint8_t *)g_n.bbSet - g_n.base : 0));
        if (g_enough) {
            // 2026-11-13 (user: sieges go out too weak; idea from 三国志14优化伴侣): at 30ad5 the siege departure
            // compares the strength it drafted with the target; when short, it gives the drafted armies back
            // only if the corps still has command books this 旬, else it launches anyway. Make it always give
            // them back (the top-difficulty "attack the player regardless" branch before it is kept).
            uint8_t *q = g_n.launchJbe;
            DWORD old;
            if (q && q[0] == 0x76 && q[1] == 0x54 && VirtualProtect(q, 2, PAGE_EXECUTE_READWRITE, &old)) {
                q[0] = 0x90; q[1] = 0x90;
                VirtualProtect(q, 2, old, &old);
                FlushInstructionCache(GetCurrentProcess(), q, 2);
                logf(L"siegeenough: short siege departures are always given back (+%llx)",
                     (unsigned long long)(q - g_n.base));
            } else
                logf(L"siegeenough NOT installed (check not found or not as expected)");
        }
        if (g_ford || g_gate) {
            if (g_n.strengthFn && g_n.logExec &&
                (g_strengthOrig = (StrengthFn)Detour(g_n.strengthFn, (void *)HkStrength, STRENGTH_STEAL)) != nullptr)
                logf(L"ford / gate: strength function %llx hooked", (unsigned long long)(g_n.strengthFn - g_n.base));
            else
                logf(L"ford / gate strength markers NOT installed (strength function or CMiscLogNode not found)");
            if (g_gate && !(g_n.bbGet && g_n.bbObj && g_n.saVt && g_n.logExec && g_strengthOrig &&
                            WriteSlot(&g_n.saVt[SLOT_strength_around_vtable_execute], (void *)HkStrengthAround,
                                      &g_n.saExec)))
                logf(L"gate: tree variable reader or strength node not found: the retreat check counts the enemies "
                     L"behind a gate");
        }
        if (g_bonus && !g_n.bonusFn)
            logf(L"bonus log NOT installed (府加成 function or CAreaData not found)");
        if (g_fu && !g_n.vtArea)
            logf(L"fu NOT installed (CAreaData not found)");
        if (g_naval && (!NdReady() || !g_n.vtArea))
            logf(L"navaldef NOT installed (攻军 / 防御 / battle base functions, damage constant or CAreaData not found)");
        if (g_gate) {
            if (g_n.setTargetFn &&
                (g_setTargetOrig = (SetTargetFn)Detour(g_n.setTargetFn, (void *)HkSetTarget, 15)) != nullptr)
                logf(L"gate: army set-target +%llx hooked (targets outside an attacked gate)",
                     (unsigned long long)(g_n.setTargetFn - g_n.base));
            else
                logf(L"gate: army set-target NOT hooked (function not found)");
        }
        if (g_gate) {
            if (g_n.createFn && g_n.depCreateRet && g_n.fmtMaskOff &&
                (g_createOrig = (CreateFn)Detour(g_n.createFn, (void *)HkCreate, CREATE_STEAL)) != nullptr)
                logf(L"gate: army creation +%llx hooked for the intercept departures (call +%llx)",
                     (unsigned long long)(g_n.createFn - g_n.base), (unsigned long long)(g_n.depCreateRet - g_n.base));
            else
                logf(L"gate: intercept creation NOT hooked (function, call site or formation mask not found)");
        }
        if (g_scLog) {
            if (g_n.assignFn && g_n.statFn &&
                (g_assignOrig = (AssignFn)Detour(g_n.assignFn, (void *)HkAssign, ASSIGN_STEAL)) != nullptr)
                logf(L"scomplog (log only): soldier assignment +%llx hooked",
                     (unsigned long long)(g_n.assignFn - g_n.base));
            else
                logf(L"scomplog NOT installed");
        }
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
    if (g_ab && g_abMask != ~0u) {
        wchar_t lim[256] = L"";
        const wchar_t *names[] = {L"stance", L"defend", L"target", L"siege", L"fu", L"shore", L"ford", L"gate", L"siegefu", L"waitfight", L"siegeprep", L"supplyguard", L"navaldef", L"relief", L"siegelone"};
        for (const wchar_t *nm : names)
            if (g_abMask >> feature_owner(nm) & 1) { wcscat(lim, L" "); wcscat(lim, nm); }
        logf(L"A/B limits only:%ls (the other features act for all forces)", lim[0] ? lim : L" none");
    }
}

// ---- trapnear (陷阱近身触发) --------------------------------------------------------------------------------
// 2026-11-14 (user: a trap whose effect covers several hexes goes off as soon as an enemy army touches the edge of
// its range and is wasted; it should go off only when the enemy reaches the hexes right around it, with the effect
// range as it was; for both sides; a launcher option, off by default). Read in 1.0.2.58226: every day
// CProgressState::Update runs 18f7e0 over the map's facilities (CObjectData: +0x10 hex, +0x12 kind, +0x13 force):
// range = 191080(kind) is the kind's 「发动范围」 (+0x40, read from the kind's encoded copy); 2b0500 collects the
// armies within that many hexes of the facility; an enemy among them -> 191d60 applies the effect to the armies
// within the 「效果范围」 (another getter, bae80: +0x37 plus the 陷阱影响范围 policy bonus), and the facility is removed.
// 发动范围: 陷阱, 猛火陷阱, 落石 2; 火陷阱, 火船 1; every other kind 0. 191080 is called only there and by the two
// functions that show 「发动范围」 in the facility info (the AI does not read it), so it is hooked: a kind of the trap
// category (kind +0x2c == 4, both read from the game's own check in 191d60) gets at most 1. Nothing else changes.
// SAN14_TRAP_LOG=1 (tests): 191d60 is hooked too, and every trap that goes off is logged with the armies of other
// forces near it, as the game saw them when it decided.
enum : uint32_t { ROOT_KIND = 0x73958, N_KIND = 31, K_NAME = 0x10, O_KIND = 0x12, O_FORCE = 0x13 };
enum { TRAP_ACT_STEAL = 13, TRAP_EFFECT_STEAL = 12 };     // whole instructions, see sigs trap_act_range / trap_effect_fn
static bool g_trapNear = false, g_trapLog = false;
static volatile LONG g_trapState = 0;     // 0 not tried, 1 hooked, 2 failed
static volatile LONG g_trapCuts = 0;      // times a trap's 发动范围 was cut to 1
struct TrapNative {
    uint8_t *act;                         // 191080: a facility kind's 发动范围
    uint8_t *effect;                      // 191d60: apply a facility's effect (optional: the test log only)
    uint32_t catOff;                      // facility kind: category (u8)
    uint8_t cat;                          // ... of traps
    uint8_t **rootPtr;                    // data root (the test log only)
};
static TrapNative g_tn;
typedef uint32_t (*ActRangeFn)(uint8_t *kind);
typedef uint32_t (*TrapEffectFn)(void *mgr, uint8_t *obj);
static ActRangeFn g_actOrig;
static TrapEffectFn g_trapEffOrig;

// Detour whose trampoline jumps back with jmp [rip+0] (no register touched): 191080's first 13 bytes end with
// mov rax,[rcx+58h] and the next instruction uses rax, so the plain Detour's mov rax / jmp rax way back would
// break it. The patch at fn itself is the same (rax is free at a function's entry).
static void *DetourKeepRegs(uint8_t *fn, void *hook, int steal) {
    if (steal < 12 || steal > 32) return nullptr;
    uint8_t *tr = (uint8_t *)VirtualAlloc(nullptr, 64, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    if (!tr) return nullptr;
    memcpy(tr, fn, steal);
    uint64_t back = (uint64_t)(fn + steal), to = (uint64_t)hook;
    tr[steal] = 0xFF; tr[steal + 1] = 0x25; memset(tr + steal + 2, 0, 4); memcpy(tr + steal + 6, &back, 8);
    FlushInstructionCache(GetCurrentProcess(), tr, 64);
    uint8_t jmp[32];
    jmp[0] = 0x48; jmp[1] = 0xB8; memcpy(jmp + 2, &to, 8); jmp[10] = 0xFF; jmp[11] = 0xE0;
    for (int i = 12; i < steal; i++) jmp[i] = 0x90;
    DWORD old;
    if (!VirtualProtect(fn, steal, PAGE_EXECUTE_READWRITE, &old)) { VirtualFree(tr, 0, MEM_RELEASE); return nullptr; }
    memcpy(fn, jmp, steal);
    VirtualProtect(fn, steal, old, &old);
    FlushInstructionCache(GetCurrentProcess(), fn, steal);
    return tr;
}

static const wchar_t *LocateTrap(TrapNative &t, uint8_t *base) {
    uint8_t *text;
    size_t tn;
    if (!Section(base, ".text", &text, &tn)) return L".text not found";
    SigHit h, g, c;
    if (!SigScan(text, tn, SIG_trap_trigger, &h)) return L"trap trigger check (trap_trigger) not found";
    t.act = SigRel(h, F_trap_trigger_act_range);
    if (t.act < text || t.act >= text + tn || !SigAt(t.act, SIG_trap_act_range, &g))
        return L"发动范围 getter (trap_act_range) not as expected";
    if (g.p[g.f[F_trap_act_range_raw_off].at] != 0x40) return L"发动范围 getter reads another field";
    if (!SigScan(text, tn, SIG_trap_category, &c)) return L"trap category check (trap_category) not found";
    t.catOff = c.p[c.f[F_trap_category_cat_off].at];
    t.cat = c.p[c.f[F_trap_category_cat_trap].at];
    if (t.catOff < 0x10 || t.catOff >= 0x50 || !t.cat) return L"trap category check reads something else";
    t.effect = nullptr;
    if (SigScan(text, tn, SIG_trap_fire_call, &h)) {
        uint8_t *e = SigRel(h, F_trap_fire_call_effect_fn);
        if (e >= text && e < text + tn && SigAt(e, SIG_trap_effect_fn, &g)) t.effect = e;
    }
    t.rootPtr = SigScan(text, tn, SIG_data_root, &h) ? (uint8_t **)SigRel(h, F_data_root_root_ptr) : nullptr;
    return nullptr;
}

static uint32_t HkActRange(uint8_t *kind) {
    uint32_t r = g_actOrig(kind);
    if (r > 1 && kind && kind[g_tn.catOff] == g_tn.cat) {
        InterlockedIncrement(&g_trapCuts);
        return 1;
    }
    return r;
}

// test log: the trap, then every army of another force within 3 hexes (distance:force), nearest first
static uint32_t HkTrapEffect(void *mgr, uint8_t *obj) {
    uint8_t *root = g_tn.rootPtr ? *g_tn.rootPtr : nullptr;
    int k = obj ? obj[O_KIND] : -1;
    uint8_t *kind = root && k >= 0 && k < (int)N_KIND ? Obj(root, ROOT_KIND, k) : nullptr;
    if (!kind || kind[g_tn.catOff] != g_tn.cat) return g_trapEffOrig(mgr, obj);
    int hex = U16(obj, O_HEX), force = obj[O_FORCE];
    wchar_t name[12] = L"";
    memcpy(name, kind + K_NAME, sizeof name - sizeof(wchar_t));
    uint8_t *st = Ptr(root, ROOT_STATUS);
    uint32_t date = st ? U16(st, ST_DATE) * 10000u + st[ST_DATE + 2] * 100u + st[ST_DATE + 3] : 0;
    int foes[24], nn = 0, nearest = 99;
    for (int i = 1; i < (int)N_ARMY && nn < 24; i++) {
        uint8_t *p = Obj(root, ROOT_ARMY, i);
        if (!p) continue;
        int gen = U16(p, A_GENERAL);
        uint8_t *pp = gen > 0 && gen < (int)N_PERSON ? Obj(root, ROOT_PERSON, gen) : nullptr;
        if (!pp || U16(pp, P_WHERE) != WHERE_ARMY + i) continue;
        int dist = pp[P_DISTRICT];
        uint8_t *dp = dist > 0 && dist < (int)N_DISTRICT ? Obj(root, ROOT_DISTRICT, dist) : nullptr;
        int f = dp ? dp[D_FORCE] : 0, ah = U16(p, A_HEX);
        if (f == force || ah >= (int)N_HEX || hex >= (int)N_HEX) continue;
        int d = HexDist(hex, ah);
        if (d > 3) continue;
        foes[nn++] = d * 100 + f;
        if (d < nearest) nearest = d;
    }
    for (int i = 1; i < nn; i++)
        for (int j = i; j > 0 && foes[j] < foes[j - 1]; j--) { int x = foes[j]; foes[j] = foes[j - 1]; foes[j - 1] = x; }
    wchar_t list[24 * 8] = L"";
    for (int i = 0; i < nn; i++) {
        wchar_t one[16];
        swprintf(one, 16, L" %d:%d", foes[i] / 100, foes[i] % 100);
        wcscat(list, one);
    }
    uint32_t act = g_actOrig(kind), now = g_trapNear && act > 1 ? 1 : act;
    uint32_t res = g_trapEffOrig(mgr, obj);
    logf(L"%u-%u-%u trap: %ls (kind %d, force %d) at hex %d went off (发动范围 %u, original %u, effect %ls); "
         L"nearest army of another force %d away; within 3 (distance:force):%ls", date / 10000, date / 100 % 100,
         date % 100, name, k, force, hex, now, act, res ? L"applied" : L"none", nearest == 99 ? -1 : nearest,
         nn ? list : L" none");
    return res;
}

static void InstallTrapNear() {
    if (InterlockedCompareExchange(&g_trapState, 2, 0) != 0) return;   // once; 2 until it succeeds
    TrapNative t{};
    const wchar_t *why = LocateTrap(t, (uint8_t *)GetModuleHandleW(nullptr));
    if (why) {
        logf(L"trapnear NOT installed: %ls (traps go off as in the original game)", why);
        return;
    }
    g_tn = t;
    uint8_t *base = (uint8_t *)GetModuleHandleW(nullptr);
    if (g_trapNear) {
        if (!(g_actOrig = (ActRangeFn)DetourKeepRegs(t.act, (void *)HkActRange, TRAP_ACT_STEAL))) {
            logf(L"trapnear NOT installed: could not hook the 发动范围 getter +%llx (traps as in the original game)",
                 (unsigned long long)(t.act - base));
            return;
        }
        logf(L"trapnear installed: 发动范围 getter +%llx hooked; a facility kind with +0x%x == %u (trap) goes off "
             L"within 1 hex at most, its effect range as before", (unsigned long long)(t.act - base), t.catOff, t.cat);
    } else
        g_actOrig = (ActRangeFn)t.act;
    if (g_trapLog) {
        if (t.effect && t.rootPtr &&
            (g_trapEffOrig = (TrapEffectFn)DetourKeepRegs(t.effect, (void *)HkTrapEffect, TRAP_EFFECT_STEAL)) != nullptr)
            logf(L"trap log: facility effect +%llx hooked (every trap that goes off is logged)",
                 (unsigned long long)(t.effect - base));
        else
            logf(L"trap log NOT installed (effect function or data root not found)");
    }
    InterlockedExchange(&g_trapState, 1);
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

// ---- third-party DLL files (人力资源管理器) ----------------------------------------------------------------
// 2026-11-14 (user: test 「三国志14人力资源管理器 0.6.1」 loaded from outside, nothing in the game directory). Its DLL
// (the autoload dinput8 build) keeps logs in <game dir>\SAN14HR\Logs. With SAN14_HR_REDIRECT=<dir> (set by
// ai/launch.py when it loads that DLL too) every path under <game dir>\SAN14HR (absolute, \\?\ or relative to
// the game directory) goes to <dir> instead, for the file calls it imports from kernel32: kernel32's exports of
// them are stubs that jump through kernel32's own import table, so patching that table catches every module's
// calls; nothing else is touched. Off (no hooks) without the variable.
static wchar_t g_hrFrom[MAX_PATH * 2], g_hrTo[MAX_PATH * 2];
static size_t g_hrFromLen = 0;
static volatile LONG g_hrRedirects = 0;
static bool HrSep(wchar_t c) { return c == L'\\' || c == L'/'; }
static bool HrPrefix(const wchar_t *q, const wchar_t *pre, size_t n) {
    for (size_t i = 0; i < n; i++) {
        wchar_t a = towlower(q[i]), b = towlower(pre[i]);
        if (HrSep(a) && HrSep(b)) continue;
        if (a != b || !a) return false;
    }
    return true;
}
static LPCWSTR HrPath(LPCWSTR p, wchar_t *buf, size_t n) {
    if (!g_hrFromLen || !p) return p;
    const wchar_t *q = p;
    if (!wcsncmp(q, L"\\\\?\\", 4)) q += 4;
    size_t rest;
    if (HrPrefix(q, g_hrFrom, g_hrFromLen) && (!q[g_hrFromLen] || HrSep(q[g_hrFromLen])))
        rest = g_hrFromLen;
    else if (q == p && !HrSep(q[0]) && q[0] && q[1] != L':' && HrPrefix(q, L"SAN14HR", 7) && (!q[7] || HrSep(q[7])))
        rest = 7;                                          // relative to the game directory (its working dir)
    else
        return p;
    _snwprintf(buf, n, L"%ls%ls", g_hrTo, q + rest);
    buf[n - 1] = 0;
    if (InterlockedIncrement(&g_hrRedirects) <= 20) logf(L"SAN14HR file: %ls -> %ls", p, buf);
    return buf;
}
typedef BOOL(WINAPI *CreateDirectoryW_t)(LPCWSTR, LPSECURITY_ATTRIBUTES);
typedef BOOL(WINAPI *Path1_t)(LPCWSTR);
typedef BOOL(WINAPI *MoveFileExW_t)(LPCWSTR, LPCWSTR, DWORD);
typedef BOOL(WINAPI *CopyFileW_t)(LPCWSTR, LPCWSTR, BOOL);
typedef DWORD(WINAPI *GetFileAttributesW_t)(LPCWSTR);
typedef BOOL(WINAPI *GetFileAttributesExW_t)(LPCWSTR, GET_FILEEX_INFO_LEVELS, LPVOID);
typedef HANDLE(WINAPI *FindFirstFileW_t)(LPCWSTR, LPWIN32_FIND_DATAW);
typedef HANDLE(WINAPI *FindFirstFileExW_t)(LPCWSTR, FINDEX_INFO_LEVELS, LPVOID, FINDEX_SEARCH_OPS, LPVOID, DWORD);
static CreateDirectoryW_t g_rCreateDir;
static Path1_t g_rRemoveDir, g_rDeleteFile;
static MoveFileExW_t g_rMoveFileEx;
static CopyFileW_t g_rCopyFile;
static GetFileAttributesW_t g_rGetAttr;
static GetFileAttributesExW_t g_rGetAttrEx;
static FindFirstFileW_t g_rFindFirst;
static FindFirstFileExW_t g_rFindFirstEx;
enum { HR_BUF = MAX_PATH * 2 + 64 };
static BOOL WINAPI hk_CreateDirectoryW(LPCWSTR a, LPSECURITY_ATTRIBUTES sa) {
    wchar_t b[HR_BUF];
    return g_rCreateDir(HrPath(a, b, HR_BUF), sa);
}
static BOOL WINAPI hk_RemoveDirectoryW(LPCWSTR a) {
    wchar_t b[HR_BUF];
    return g_rRemoveDir(HrPath(a, b, HR_BUF));
}
static BOOL WINAPI hk_DeleteFileW(LPCWSTR a) {
    wchar_t b[HR_BUF];
    return g_rDeleteFile(HrPath(a, b, HR_BUF));
}
static BOOL WINAPI hk_MoveFileExW(LPCWSTR a, LPCWSTR c, DWORD f) {
    wchar_t b[HR_BUF], d[HR_BUF];
    return g_rMoveFileEx(HrPath(a, b, HR_BUF), HrPath(c, d, HR_BUF), f);
}
static BOOL WINAPI hk_CopyFileW(LPCWSTR a, LPCWSTR c, BOOL f) {
    wchar_t b[HR_BUF], d[HR_BUF];
    return g_rCopyFile(HrPath(a, b, HR_BUF), HrPath(c, d, HR_BUF), f);
}
static DWORD WINAPI hk_GetFileAttributesW(LPCWSTR a) {
    wchar_t b[HR_BUF];
    return g_rGetAttr(HrPath(a, b, HR_BUF));
}
static BOOL WINAPI hk_GetFileAttributesExW(LPCWSTR a, GET_FILEEX_INFO_LEVELS l, LPVOID o) {
    wchar_t b[HR_BUF];
    return g_rGetAttrEx(HrPath(a, b, HR_BUF), l, o);
}
static HANDLE WINAPI hk_FindFirstFileW(LPCWSTR a, LPWIN32_FIND_DATAW o) {
    wchar_t b[HR_BUF];
    return g_rFindFirst(HrPath(a, b, HR_BUF), o);
}
static HANDLE WINAPI hk_FindFirstFileExW(LPCWSTR a, FINDEX_INFO_LEVELS l, LPVOID o, FINDEX_SEARCH_OPS so, LPVOID f, DWORD x) {
    wchar_t b[HR_BUF];
    return g_rFindFirstEx(HrPath(a, b, HR_BUF), l, o, so, f, x);
}

// The DLL is the dinput8 proxy build: installed as <game dir>\dinput8.dll the game's static import of
// DINPUT8!DirectInput8Create lands in it, and that first call starts it (loaded any other way it stays idle: test
// run 20261114 18:34, loaded by APC, F9 did nothing and it wrote no file). With SAN14_HR_DLL=<its path> the game's
// import slot points here; the first call loads that DLL and goes through its DirectInput8Create (which forwards
// to the system dinput8), so it starts exactly as when installed. Any failure: the system function, as before.
typedef HRESULT(WINAPI *DI8Create_t)(HINSTANCE, DWORD, REFIID, LPVOID *, LPUNKNOWN);
static DI8Create_t g_sysDI8 = nullptr, g_hrDI8 = nullptr;
static wchar_t g_hrDll[MAX_PATH * 2];
static volatile LONG g_hrTried = 0;
// Keeping one of its features out (SAN14_HR_BLOCK, comma list). It installs each feature by swapping a few vtable
// slots of a game class with compare-exchange against the originals it found, and drops the feature (logging
// "...unavailable") when a slot already holds something else, as it did with the report skip's hook. So before
// it starts, the slots are pointed at small jumps to the very same functions: the game runs as before and the
// feature is not installed. commandbook: the button on the 命令书 icon (command-books.void), which it hangs on
// CMainTopLayoutOffset slots 0, 2, 5 (seen in the running game 2026-11-14: those three slots pointed into it,
// next to its command-book install code).
static wchar_t g_hrBlock[128];
static void HrOccupy(const char *cls, const int *slots, int n, const wchar_t *what) {
    void **vt = VtableByRtti((uint8_t *)GetModuleHandleW(nullptr), cls);
    if (!vt) { logf(L"SAN14HR block %ls: %hs not found, not blocked", what, cls); return; }
    uint8_t *mem = (uint8_t *)VirtualAlloc(nullptr, 4096, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    if (!mem) { logf(L"SAN14HR block %ls: no memory, not blocked", what); return; }
    int done = 0;
    for (int i = 0; i < n; i++) {
        uint8_t *t = mem + 16 * i;                   // jmp qword ptr [rip+0] ; <original>
        void *orig = vt[slots[i]];
        t[0] = 0xFF; t[1] = 0x25; memset(t + 2, 0, 4); memcpy(t + 6, &orig, 8);
        DWORD old;
        if (VirtualProtect(&vt[slots[i]], sizeof(void *), PAGE_READWRITE, &old)) {
            vt[slots[i]] = t;
            VirtualProtect(&vt[slots[i]], sizeof(void *), old, &old);
            done++;
        }
    }
    FlushInstructionCache(GetCurrentProcess(), mem, 4096);
    logf(L"SAN14HR block %ls: %hs %d of %d slots now go through forwarding jumps", what, cls, done, n);
}

static HRESULT WINAPI hk_DirectInput8Create(HINSTANCE hi, DWORD ver, REFIID riid, LPVOID *out, LPUNKNOWN outer) {
    if (!InterlockedExchange(&g_hrTried, 1)) {
        if (wcsstr(g_hrBlock, L"commandbook")) {
            static const int cb[] = {0, 2, 5};
            HrOccupy(".?AVCMainTopLayoutOffset@@", cb, 3, L"commandbook");
        }
        HMODULE h = LoadLibraryW(g_hrDll);
        g_hrDI8 = h ? (DI8Create_t)GetProcAddress(h, "DirectInput8Create") : nullptr;
        if (g_hrDI8 == (DI8Create_t)hk_DirectInput8Create) g_hrDI8 = nullptr;
        logf(L"SAN14HR: DirectInput8Create goes through %ls: %ls", g_hrDll,
             g_hrDI8 ? L"ok" : h ? L"no DirectInput8Create export (system one used)" : L"load FAILED (system one used)");
    }
    if (g_hrDI8) return g_hrDI8(hi, ver, riid, out, outer);
    return g_sysDI8(hi, ver, riid, out, outer);
}

static HANDLE WINAPI hook_CreateFileW(LPCWSTR name, DWORD acc, DWORD share, LPSECURITY_ATTRIBUTES sa,
                                      DWORD disp, DWORD flags, HANDLE tmpl) {
    wchar_t hrbuf[HR_BUF];
    name = HrPath(name, hrbuf, HR_BUF);
    LONG k = InterlockedIncrement(&g_opens);
    bool tree = is_tree_file(name) && !(acc & GENERIC_WRITE);
    if (tree && any_feature()) InstallDaily();      // the game's code is decrypted by now
    if (tree && (g_trapNear || g_trapLog)) InstallTrapNear();
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

// Replace the IAT slot for dll!func in module `mod`. Returns the number of slots patched; *orig gets the old value.
static int patch_iat(HMODULE mod, const char *func, void *hook, void **orig = nullptr) {
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
                if (orig && !*orig) *orig = (void *)slots->u1.Function;
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
    wchar_t tv[8];
    DWORD tl = GetEnvironmentVariableW(L"SAN14_TRAP_NEAR", tv, 8);
    g_trapNear = tl && tl < 8 && tv[0] == L'1';
    tl = GetEnvironmentVariableW(L"SAN14_TRAP_LOG", tv, 8);
    g_trapLog = tl && tl < 8 && tv[0] == L'1';
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
    if (g_trapNear || g_trapLog)
        logf(L"trapnear %ls, trap log %ls (installed when the game reads the tree)", g_trapNear ? L"on" : L"off",
             g_trapLog ? L"on" : L"off");
    // third-party DLL (人力资源管理器): the game's DirectInput8Create through it (see hk_DirectInput8Create)
    DWORD dn = GetEnvironmentVariableW(L"SAN14_HR_DLL", g_hrDll, MAX_PATH * 2);
    DWORD bn = GetEnvironmentVariableW(L"SAN14_HR_BLOCK", g_hrBlock, 128);
    if (!bn || bn >= 128) g_hrBlock[0] = 0;
    if (dn && dn < MAX_PATH * 2) {
        int c = patch_iat(GetModuleHandleW(nullptr), "DirectInput8Create", (void *)hk_DirectInput8Create, (void **)&g_sysDI8);
        if (c <= 0 || !g_sysDI8) logf(L"SAN14HR: the game's DirectInput8Create import not found; %ls not started", g_hrDll);
        else logf(L"SAN14HR: the game's DirectInput8Create will go through %ls", g_hrDll);
    }
    // third-party DLL files (see HrPath)
    DWORD hn = GetEnvironmentVariableW(L"SAN14_HR_REDIRECT", g_hrTo, MAX_PATH * 2);
    if (hn && hn < MAX_PATH * 2 - 40) {
        DWORD m = GetModuleFileNameW(nullptr, g_hrFrom, MAX_PATH * 2 - 16);
        wchar_t *slash = m ? wcsrchr(g_hrFrom, L'\\') : nullptr;
        size_t tl = wcslen(g_hrTo);
        while (tl && HrSep(g_hrTo[tl - 1])) g_hrTo[--tl] = 0;
        HMODULE k32 = GetModuleHandleW(L"kernel32.dll");
        struct { const char *fn; void *hook; void **orig; } hk[] = {
            {"CreateDirectoryW", (void *)hk_CreateDirectoryW, (void **)&g_rCreateDir},
            {"RemoveDirectoryW", (void *)hk_RemoveDirectoryW, (void **)&g_rRemoveDir},
            {"DeleteFileW", (void *)hk_DeleteFileW, (void **)&g_rDeleteFile},
            {"MoveFileExW", (void *)hk_MoveFileExW, (void **)&g_rMoveFileEx},
            {"CopyFileW", (void *)hk_CopyFileW, (void **)&g_rCopyFile},
            {"GetFileAttributesW", (void *)hk_GetFileAttributesW, (void **)&g_rGetAttr},
            {"GetFileAttributesExW", (void *)hk_GetFileAttributesExW, (void **)&g_rGetAttrEx},
            {"FindFirstFileW", (void *)hk_FindFirstFileW, (void **)&g_rFindFirst},
            {"FindFirstFileExW", (void *)hk_FindFirstFileExW, (void **)&g_rFindFirstEx},
        };
        if (slash && tl) {
            wcscpy(slash, L"\\SAN14HR");
            int done = 0;
            for (auto &h : hk) {
                if (patch_iat(k32, h.fn, h.hook, h.orig) > 0 && *h.orig) done++;
                else logf(L"SAN14HR redirect: kernel32 import of %hs not found", h.fn);
            }
            if (done == (int)(sizeof hk / sizeof hk[0])) {
                g_rCreateDir(g_hrTo, nullptr);
                g_hrFromLen = wcslen(g_hrFrom);
                logf(L"SAN14HR redirect on: %ls -> %ls (%d file functions)", g_hrFrom, g_hrTo, done);
            } else
                logf(L"SAN14HR redirect NOT on (only %d of %d file functions hooked; their calls pass through)", done,
                     (int)(sizeof hk / sizeof hk[0]));
        }
    }
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
