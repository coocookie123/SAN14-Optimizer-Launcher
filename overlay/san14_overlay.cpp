// san14_overlay.dll - draws an image supplied by the Python helper on top of the game's frame and
// reports clicks on it. Injected into SAN14CE_*.exe by tools/report_button.py (nothing on disk in the
// game folder). All look & logic live in Python; this DLL only:
//   * hooks IDXGISwapChain::Present / Present1 / ResizeBuffers through the swap-chain vtable,
//   * draws the BGRA (premultiplied) image from shared memory with its own D3D11 context state
//     (ID3D11DeviceContext1::SwapDeviceContextState, so the game's pipeline state is untouched),
//   * subclasses the game window to report hover / press / click over the hit rectangle and to keep
//     those clicks away from the game,
//   * unloads itself on request (cmd = 1) once no thread is inside its code,
//   * skips the turn-start report on the game's own state thread (skip_req = 1, see "report skip").
// Any failure only disables drawing / skipping; the game keeps running.
#define WIN32_LEAN_AND_MEAN
#define COBJMACROS
#include <windows.h>
#include <d3d11.h>
#include <d3d11_1.h>
#include <dxgi1_2.h>
#include <d3dcompiler.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>

#include "sigs_gen.h"   // generated from sigs/sigs.json by overlay/build.py

#pragma pack(push, 1)
struct Shared {
    uint32_t magic;            // 'S14R'
    uint32_t version;          // 1
    uint32_t py_heartbeat;     // GetTickCount() written by Python
    uint32_t visible;          // Python: 1 = draw
    uint32_t img_serial;       // Python: bumped after a new image is written
    uint32_t img_w, img_h;     // Python
    int32_t img_x, img_y;      // Python: back-buffer pixels
    int32_t hit_x0, hit_y0, hit_x1, hit_y1;  // Python: back-buffer pixels
    uint32_t bb_w, bb_h;       // DLL
    uint32_t client_w, client_h;  // DLL
    uint32_t hover, pressed, clicks;  // DLL
    uint32_t dll_frame;        // DLL
    uint32_t cmd;              // Python -> DLL: 1 = unload
    uint32_t dll_state;        // DLL: 1 hooked, 2 unloaded, 3 hook failed
    uint32_t bb_format;        // DLL
    uint32_t skip_req;         // Python: 1 = skip the turn-start report (hooks installed on first request)
    uint32_t skip_state;       // DLL: 0 not tried, 1 active, 2 removed, 3 game code not recognised
    uint32_t skip_count;       // DLL: reports skipped
    uint32_t reopen_req;       // Python: bump to show this turn's report again ("回看报告")
    uint32_t reopen_ack;       // DLL: last reopen_req taken (Python sets reopen_req = reopen_ack to cancel)
    uint32_t foreign_hook;     // DLL: 1 = another tool (优化伴侣) patched the strategy update; it skips, we stand by
    uint8_t pad[128 - 4 * 30];
    uint8_t pixels[1024 * 256 * 4];
};
#pragma pack(pop)
static_assert(offsetof(Shared, pixels) == 128, "layout");

static const uint32_t MAGIC = 0x52343153;  // 'S14R'
static const UINT TEX_W = 1024, TEX_H = 256;

static HMODULE g_module;
static FILE* g_log;
static HANDLE g_map;
static Shared* g_sh;
static void** g_vtbl;
static void* g_origPresent;
static void* g_origPresent1;
static void* g_origResize;
static HWND g_hwnd;
static WNDPROC g_origWndProc;
static volatile LONG g_inflight;
static volatile LONG g_unloading;

// D3D resources (created on the render thread)
static IDXGISwapChain* g_sc;
static ID3D11Device* g_dev;
static ID3D11DeviceContext* g_ctx;
static ID3D11DeviceContext1* g_ctx1;
static ID3DDeviceContextState* g_state;
static ID3D11VertexShader* g_vs;
static ID3D11PixelShader* g_ps;
static ID3D11Buffer* g_cb;
static ID3D11Texture2D* g_tex;
static ID3D11ShaderResourceView* g_srv;
static ID3D11SamplerState* g_smp;
static ID3D11BlendState* g_blend;
static ID3D11RasterizerState* g_rs;
static ID3D11RenderTargetView* g_rtv;
static ID3D11Texture2D* g_rtvTex;
static uint32_t g_uploaded = 0xffffffff;
static bool g_resFailed;

static void Log(const char* fmt, ...) {
    if (!g_log) return;
    if (ftell(g_log) > 256 * 1024) {           // size cap; the helper keeps one previous log
        fputs("log size limit reached\n", g_log);
        fclose(g_log);
        g_log = nullptr;
        return;
    }
    SYSTEMTIME t;
    GetLocalTime(&t);
    fprintf(g_log, "%02d:%02d:%02d.%03d ", t.wHour, t.wMinute, t.wSecond, t.wMilliseconds);
    va_list ap;
    va_start(ap, fmt);
    vfprintf(g_log, fmt, ap);
    va_end(ap);
    fputc('\n', g_log);
    fflush(g_log);
}

template <class T> static void SafeRelease(T*& p) { if (p) { p->Release(); p = nullptr; } }

static void ReleaseResources() {
    SafeRelease(g_rtv); g_rtvTex = nullptr;
    SafeRelease(g_srv); SafeRelease(g_tex); SafeRelease(g_smp); SafeRelease(g_blend); SafeRelease(g_rs);
    SafeRelease(g_cb); SafeRelease(g_ps); SafeRelease(g_vs); SafeRelease(g_state);
    SafeRelease(g_ctx1); SafeRelease(g_ctx); SafeRelease(g_dev);
    g_sc = nullptr;
    g_uploaded = 0xffffffff;
}

static bool Fresh() {
    return g_sh && g_sh->magic == MAGIC && (GetTickCount() - g_sh->py_heartbeat) < 1500;
}

static void OpenShared() {
    if (g_sh) return;
    g_map = OpenFileMappingW(FILE_MAP_ALL_ACCESS, FALSE, L"Local\\SAN14ReportSkipOverlay");
    if (!g_map) return;
    g_sh = (Shared*)MapViewOfFile(g_map, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(Shared));
    if (!g_sh) { CloseHandle(g_map); g_map = nullptr; return; }
    Log("shared memory opened");
}

// ---- report skip (runs on the game's own state thread) ------------------------------------
// CUserStrategyState::Update (vtable slot 5) starts the turn-start report coroutine while its
// sub-phase [this+470h] is 0: hide the map UI, push CReportWaitState (up to 2 s), push
// CReportDisplayState, and the window's callback sets sub-phase 1. We replace slot 5. At sub-phase 0
// we do what the report callback and the end of the coroutine do (without waking a coroutine that
// never started): show the HUD again, sub-phase 1, slot 3 (resume: HUD and map input), slot 11 with 0
// (open the command menu, sub-phase 2), turn-start sound. The original Update then reads sub-phase 2
// in the same frame, so the turn starts with no pause and no state switches.
// "回看报告": Python bumps reopen_req; at sub-phase 1/2 we set sub-phase 0 and let the original
// Update run the game's own report, with its CReportWaitState ended on its first frame.
// Everything is found by RTTI name and byte pattern; any mismatch -> skip_state 3, nothing hooked.
// The patterns, field offsets and class names are in sigs/sigs.json (overlay/sigs_gen.h is generated from it
// by overlay/build.py); the structure offsets SkipReport uses are read from the matched game code.
// Another tool: 「三国志14 优化伴侣」 (Frida) patches the original Update at +0x16 (the sub-phase read)
// to skip the report itself. While those bytes differ from the original we do not skip (else its own
// "open the report" (F8) would be skipped by us and ours by it) and report foreign_hook = 1, so the
// Python side opens the report through that tool's F8.
struct GameApi {
    uint8_t* base;
    void** usVt;                         // CUserStrategyState vtable
    void** rwVt;                         // CReportWaitState vtable
    void* usUpdate;                      // original slot 5 of each
    void* rwUpdate;
    int (*isTop)(void*);                 // state is on top of the state stack
    int (*isObserver)();                 // spectator mode (看海)
    void* (*getMgr)();                   // state manager
    void* (*findState)(void*, const char*);
    const char* gameStateName;           // "CGameState"
    void (*restoreBtn)(void*);           // HUD: progress button back to normal
    void (*resume)(void*);               // CUserStrategyState slot 3
    void (*openCmd)(void*, int);         // CUserStrategyState slot 11
    int* (*sndFlag)();
    void* (*sndMgr)();
    void (*playSnd)(void*, int);
    // structure offsets, read from the matched code (1.0.2.58226 values in brackets)
    uint32_t usStage;                    // CUserStrategyState sub-phase [470h]
    uint32_t rwState;                    // CReportWaitState "done" flag [470h]
    uint32_t hudOff;                     // CGameState -> HUD [480h]
    uint32_t panelOff, panel2Off, panelVfn;  // HUD -> panel -> panel, its show(1) [178h, 188h, vtable +0C8h]
    uint32_t btnOff, btnVfn;             // HUD -> progress button, its enable(1) [1D0h, vtable +0D0h]
    uint32_t stageReadOff, stageReadLen; // Update: the sub-phase read 优化伴侣 patches [+16h, 6 bytes]
    uint8_t stageRead[8];                // its original bytes
};
static GameApi g_game;
static volatile LONG g_skipStarted;
static volatile LONG g_skipHooked;
static volatile LONG g_waitFast;         // next CReportWaitState ends on its first frame

#include "../native/sigscan.h"   // MakePat, FindPat, Section, VtableByRtti, SigAt, SigScan, SigRel, SigU32

// Fills g_game from the image at `base`. Returns nullptr or a short reason.
static const char* LocateGame(uint8_t* base) {
    GameApi a{};
    a.base = base;
    uint8_t* text;
    size_t tn;
    if (!Section(base, ".text", &text, &tn)) return "no .text";
    a.usVt = VtableByRtti(base, kRtti_strategy_vtable);
    a.rwVt = VtableByRtti(base, kRtti_report_wait_vtable);
    if (!a.usVt || !a.rwVt) return "state vtables not found";
    SigHit up, co, tail, cb, rs, oc, rw;
    uint8_t* upd = (uint8_t*)a.usVt[SLOT_strategy_vtable_update];
    if (!SigAt(upd, SIG_us_update, &up)) return "strategy update not recognised";
    a.isTop = (int (*)(void*))SigRel(up, F_us_update_is_top);
    a.usStage = SigU32(up, F_us_update_stage_off);
    a.stageReadOff = up.f[F_us_update_stage_read].at;
    a.stageReadLen = up.f[F_us_update_stage_read].len;
    if (a.stageReadLen > sizeof a.stageRead) return "strategy update: sub-phase read too long";
    memcpy(a.stageRead, upd + a.stageReadOff, a.stageReadLen);
    uint8_t* coFn = SigRel(up, F_us_update_coroutine);
    if (!SigAt(coFn, SIG_report_co_head, &co)) return "report coroutine not recognised";
    a.isObserver = (int (*)())SigRel(co, F_report_co_head_is_spectator);
    if (!SigScan(coFn, SIG_report_co_tail.range, SIG_report_co_tail, &tail)) return "report coroutine end not recognised";
    a.sndFlag = (int* (*)())SigRel(tail, F_report_co_tail_snd_flag);
    if ((void*)SigRel(tail, F_report_co_tail_snd_flag2) != (void*)a.sndFlag) return "report coroutine end: two sound flags";
    a.sndMgr = (void* (*)())SigRel(tail, F_report_co_tail_snd_mgr);
    a.playSnd = (void (*)(void*, int))SigRel(tail, F_report_co_tail_play_snd);
    if (!SigScan(text, tn, SIG_report_callback, &cb)) return "report callback not found";
    a.getMgr = (void* (*)())SigRel(cb, F_report_callback_get_mgr);
    a.gameStateName = (const char*)SigRel(cb, F_report_callback_state_name);
    a.findState = (void* (*)(void*, const char*))SigRel(cb, F_report_callback_find_state);
    a.restoreBtn = (void (*)(void*))SigRel(cb, F_report_callback_restore_btn);
    if (strcmp(a.gameStateName, kStr_report_callback_state_name) != 0) return "state name not recognised";
    a.hudOff = SigU32(cb, F_report_callback_hud_off);
    a.panelOff = SigU32(cb, F_report_callback_panel_off);
    a.panel2Off = SigU32(cb, F_report_callback_panel2_off);
    a.panelVfn = SigU32(cb, F_report_callback_panel_vfn);
    a.btnOff = SigU32(cb, F_report_callback_btn_off);
    a.btnVfn = SigU32(cb, F_report_callback_btn_vfn);
    if (SigU32(cb, F_report_callback_stage_off) != a.usStage) return "report callback: other sub-phase offset";
    if (!SigAt((uint8_t*)a.usVt[SLOT_strategy_vtable_resume], SIG_us_resume, &rs)) return "strategy resume not recognised";
    if (!SigAt((uint8_t*)a.usVt[SLOT_strategy_vtable_open_cmd], SIG_us_open_cmd, &oc))
        return "strategy command menu not recognised";
    if (SigU32(oc, F_us_open_cmd_stage_off) != a.usStage) return "strategy command menu: other sub-phase offset";
    a.resume = (void (*)(void*))a.usVt[SLOT_strategy_vtable_resume];
    a.openCmd = (void (*)(void*, int))a.usVt[SLOT_strategy_vtable_open_cmd];
    uint8_t* rwFn = (uint8_t*)a.rwVt[SLOT_report_wait_vtable_update];
    if (!SigAt(rwFn, SIG_report_wait_update, &rw) || (void*)SigRel(rw, F_report_wait_update_is_top) != (void*)a.isTop)
        return "report wait not recognised";
    a.rwState = SigU32(rw, F_report_wait_update_state_off);
    a.usUpdate = a.usVt[SLOT_strategy_vtable_update];
    a.rwUpdate = a.rwVt[SLOT_report_wait_vtable_update];
    g_game = a;
    return nullptr;
}

static void VCall(void* obj, size_t off, int arg) {
    void** vt = *(void***)obj;
    ((void (*)(void*, int))vt[off / sizeof(void*)])(obj, arg);
}

static void SkipReport(uint8_t* self) {
    GameApi& a = g_game;
    void* mgr = a.getMgr();
    uint8_t* gs = mgr ? (uint8_t*)a.findState(mgr, a.gameStateName) : nullptr;
    uint8_t* hud = gs ? *(uint8_t**)(gs + a.hudOff) : nullptr;
    if (hud) {                                     // report callback body
        uint8_t* p = *(uint8_t**)(hud + a.panelOff);
        if (p) p = *(uint8_t**)(p + a.panel2Off);
        if (p) VCall(p, a.panelVfn, 1);
        a.restoreBtn(hud);
        uint8_t* b = *(uint8_t**)(hud + a.btnOff);
        if (b) VCall(b, a.btnVfn, 1);
    }
    *(volatile int32_t*)(self + a.usStage) = 1;
    a.resume(self);
    a.openCmd(self, 0);
    int* f = a.sndFlag();                          // end of the coroutine
    if (f && *f) {
        void* m = a.sndMgr();
        if (m) a.playSnd(m, 1);
    }
}

static bool SkipWanted() { return !g_unloading && g_sh && g_sh->skip_req == 1; }

static volatile LONG g_foreign;

static uint64_t HkStrategyUpdate(uint8_t* self) {
    InterlockedIncrement(&g_inflight);
    // Update+0x16 "mov eax,[rsi+470h]" (us_update.stage_read) still the bytes LocateGame saw?
    LONG foreign = memcmp((uint8_t*)g_game.usUpdate + g_game.stageReadOff, g_game.stageRead, g_game.stageReadLen) != 0;
    if (foreign != g_foreign) {
        InterlockedExchange(&g_foreign, foreign);
        if (g_sh) g_sh->foreign_hook = (uint32_t)foreign;
        Log(foreign ? "another tool patched the strategy update (SAN14 companion?): it skips the report, we stand by"
                    : "the other tool's patch is gone: skipping the report again");
    }
    if (SkipWanted() && g_game.isTop(self)) {
        int32_t st = *(volatile int32_t*)(self + g_game.usStage);
        if (st == 0) {
            if (!foreign && !g_game.isObserver()) {
                SkipReport(self);
                g_sh->skip_count++;
                Log("report skipped (#%u), sub-phase now %d", g_sh->skip_count, *(volatile int32_t*)(self + g_game.usStage));
            }
        } else if ((st == 1 || st == 2) && g_sh->reopen_req != g_sh->reopen_ack) {
            g_sh->reopen_ack = g_sh->reopen_req;
            if (!g_game.isObserver()) {
                InterlockedExchange(&g_waitFast, 1);
                *(volatile int32_t*)(self + g_game.usStage) = 0;     // the original below runs the report
                Log("report shown again");
            }
        }
    }
    uint64_t r = ((uint64_t (*)(void*))g_game.usUpdate)(self);
    InterlockedDecrement(&g_inflight);
    return r;
}

static uint64_t HkReportWaitUpdate(uint8_t* self) {
    InterlockedIncrement(&g_inflight);
    if (InterlockedExchange(&g_waitFast, 0)) *(volatile int32_t*)(self + g_game.rwState) = 1;
    uint64_t r = ((uint64_t (*)(void*))g_game.rwUpdate)(self);
    InterlockedDecrement(&g_inflight);
    return r;
}

static bool WriteSlot(void** slot, void* fn, void** orig) {
    DWORD old;
    if (!VirtualProtect(slot, sizeof(void*), PAGE_READWRITE, &old)) return false;
    if (orig) *orig = *slot;
    InterlockedExchangePointer(slot, fn);
    VirtualProtect(slot, sizeof(void*), old, &old);
    return true;
}

static DWORD WINAPI SkipInstallThread(LPVOID) {
    DWORD t0 = GetTickCount();
    const char* why = LocateGame((uint8_t*)GetModuleHandleW(nullptr));
    bool ok = !why && !g_unloading && WriteSlot(&g_game.rwVt[5], (void*)HkReportWaitUpdate, nullptr) &&
              WriteSlot(&g_game.usVt[5], (void*)HkStrategyUpdate, nullptr);
    if (ok) InterlockedExchange(&g_skipHooked, 1);
    if (g_sh) g_sh->skip_state = ok ? 1 : 3;
    Log("report skip: %s (%lu ms)", ok ? "hooked" : (why ? why : "vtable write failed"), GetTickCount() - t0);
    if (ok)
        Log("  strategy vtable +%llx, update +%llx, report wait update +%llx", (unsigned long long)((uint8_t*)g_game.usVt - g_game.base),
            (unsigned long long)((uint8_t*)g_game.usUpdate - g_game.base), (unsigned long long)((uint8_t*)g_game.rwUpdate - g_game.base));
    InterlockedDecrement(&g_inflight);
    return 0;
}

static void UnhookSkip() {
    if (!InterlockedExchange(&g_skipHooked, 0)) return;
    WriteSlot(&g_game.usVt[5], g_game.usUpdate, nullptr);
    WriteSlot(&g_game.rwVt[5], g_game.rwUpdate, nullptr);
    if (g_sh) g_sh->skip_state = 2;
    Log("report skip unhooked");
}

static const char* kShader =
    "cbuffer cb : register(b0) { float4 rect; float4 prm; };\n"
    "struct VO { float4 pos : SV_Position; float2 uv : TEXCOORD0; };\n"
    "VO vs(uint id : SV_VertexID) {\n"
    "  float2 t = float2((id & 1) ? 1.0 : 0.0, (id & 2) ? 1.0 : 0.0);\n"
    "  VO o; o.pos = float4(lerp(rect.x, rect.z, t.x), lerp(rect.y, rect.w, t.y), 0, 1);\n"
    "  o.uv = t * prm.zw; return o; }\n"
    "Texture2D tex : register(t0); SamplerState smp : register(s0);\n"
    "float4 ps(VO i) : SV_Target {\n"
    "  float4 c = tex.Sample(smp, i.uv);\n"
    "  if (prm.x > 0) {\n"            // linear back buffer (sRGB view or scRGB float): decode sRGB
    "    float3 s = c.a > 0 ? c.rgb / c.a : float3(0, 0, 0);\n"
    "    c.rgb = pow(saturate(s), 2.2) * c.a * prm.x; }\n"
    "  return c; }\n";

typedef HRESULT(WINAPI* D3DCompileFn)(LPCVOID, SIZE_T, LPCSTR, const D3D_SHADER_MACRO*, ID3DInclude*, LPCSTR,
                                      LPCSTR, UINT, UINT, ID3DBlob**, ID3DBlob**);

static bool CreateResources(IDXGISwapChain* sc) {
    ReleaseResources();
    g_sc = sc;
    if (FAILED(sc->GetDevice(__uuidof(ID3D11Device), (void**)&g_dev))) { Log("GetDevice failed"); return false; }
    g_dev->GetImmediateContext(&g_ctx);
    if (FAILED(g_ctx->QueryInterface(__uuidof(ID3D11DeviceContext1), (void**)&g_ctx1))) {
        Log("no ID3D11DeviceContext1"); return false;
    }
    ID3D11Device1* dev1 = nullptr;
    if (FAILED(g_dev->QueryInterface(__uuidof(ID3D11Device1), (void**)&dev1))) { Log("no ID3D11Device1"); return false; }
    D3D_FEATURE_LEVEL fl = g_dev->GetFeatureLevel();
    UINT flags = (g_dev->GetCreationFlags() & D3D11_CREATE_DEVICE_SINGLETHREADED)
                     ? D3D11_1_CREATE_DEVICE_CONTEXT_STATE_SINGLETHREADED : 0;
    HRESULT hr = dev1->CreateDeviceContextState(flags, &fl, 1, D3D11_SDK_VERSION, __uuidof(ID3D11Device), nullptr, &g_state);
    dev1->Release();
    if (FAILED(hr)) { Log("CreateDeviceContextState failed %08lx", hr); return false; }

    HMODULE dc = LoadLibraryW(L"d3dcompiler_47.dll");
    D3DCompileFn compile = dc ? (D3DCompileFn)GetProcAddress(dc, "D3DCompile") : nullptr;
    if (!compile) { Log("d3dcompiler_47 not available"); return false; }
    ID3DBlob *vsb = nullptr, *psb = nullptr, *err = nullptr;
    if (FAILED(compile(kShader, strlen(kShader), "ov", nullptr, nullptr, "vs", "vs_4_0", 0, 0, &vsb, &err)) ||
        FAILED(compile(kShader, strlen(kShader), "ov", nullptr, nullptr, "ps", "ps_4_0", 0, 0, &psb, &err))) {
        Log("shader compile failed: %s", err ? (const char*)err->GetBufferPointer() : "?");
        SafeRelease(err); SafeRelease(vsb); SafeRelease(psb);
        return false;
    }
    g_dev->CreateVertexShader(vsb->GetBufferPointer(), vsb->GetBufferSize(), nullptr, &g_vs);
    g_dev->CreatePixelShader(psb->GetBufferPointer(), psb->GetBufferSize(), nullptr, &g_ps);
    vsb->Release(); psb->Release();

    D3D11_BUFFER_DESC bd = {};
    bd.ByteWidth = 32; bd.Usage = D3D11_USAGE_DYNAMIC; bd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    bd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    g_dev->CreateBuffer(&bd, nullptr, &g_cb);

    D3D11_TEXTURE2D_DESC td = {};
    td.Width = TEX_W; td.Height = TEX_H; td.MipLevels = 1; td.ArraySize = 1;
    td.Format = DXGI_FORMAT_B8G8R8A8_UNORM; td.SampleDesc.Count = 1;
    td.Usage = D3D11_USAGE_DYNAMIC; td.BindFlags = D3D11_BIND_SHADER_RESOURCE; td.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    g_dev->CreateTexture2D(&td, nullptr, &g_tex);
    if (g_tex) g_dev->CreateShaderResourceView(g_tex, nullptr, &g_srv);

    D3D11_SAMPLER_DESC sd = {};
    sd.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
    sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sd.MaxLOD = D3D11_FLOAT32_MAX;
    g_dev->CreateSamplerState(&sd, &g_smp);

    D3D11_BLEND_DESC bl = {};
    bl.RenderTarget[0].BlendEnable = TRUE;
    bl.RenderTarget[0].SrcBlend = D3D11_BLEND_ONE;
    bl.RenderTarget[0].DestBlend = D3D11_BLEND_INV_SRC_ALPHA;
    bl.RenderTarget[0].BlendOp = D3D11_BLEND_OP_ADD;
    bl.RenderTarget[0].SrcBlendAlpha = D3D11_BLEND_ONE;
    bl.RenderTarget[0].DestBlendAlpha = D3D11_BLEND_INV_SRC_ALPHA;
    bl.RenderTarget[0].BlendOpAlpha = D3D11_BLEND_OP_ADD;
    bl.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL;
    g_dev->CreateBlendState(&bl, &g_blend);

    D3D11_RASTERIZER_DESC rd = {};
    rd.FillMode = D3D11_FILL_SOLID; rd.CullMode = D3D11_CULL_NONE; rd.DepthClipEnable = TRUE;
    g_dev->CreateRasterizerState(&rd, &g_rs);

    if (!g_vs || !g_ps || !g_cb || !g_tex || !g_srv || !g_smp || !g_blend || !g_rs) { Log("resource creation failed"); return false; }
    DXGI_SWAP_CHAIN_DESC scd;
    sc->GetDesc(&scd);
    Log("resources ready: back buffer %ux%u format %d, feature level %x", scd.BufferDesc.Width, scd.BufferDesc.Height,
        scd.BufferDesc.Format, fl);
    return true;
}

static void Upload() {
    uint32_t serial = g_sh->img_serial;
    if (serial == g_uploaded) return;
    UINT w = g_sh->img_w, h = g_sh->img_h;
    if (w == 0 || h == 0 || w > TEX_W || h > TEX_H) return;
    D3D11_MAPPED_SUBRESOURCE m;
    if (FAILED(g_ctx->Map(g_tex, 0, D3D11_MAP_WRITE_DISCARD, 0, &m))) return;
    for (UINT y = 0; y < h; y++)
        memcpy((uint8_t*)m.pData + (size_t)y * m.RowPitch, g_sh->pixels + (size_t)y * w * 4, (size_t)w * 4);
    g_ctx->Unmap(g_tex, 0);
    g_uploaded = serial;
}

static void BeginUnload();

static void OnFrame(IDXGISwapChain* sc) {
    if (g_unloading) return;
    static uint32_t tries;
    if (!g_sh && (tries++ % 60) == 0) OpenShared();
    if (!g_sh) return;
    if (g_sh->cmd == 1) { BeginUnload(); return; }
    g_sh->dll_frame++;
    if (g_sh->skip_req == 1 && !InterlockedExchange(&g_skipStarted, 1)) {   // pattern scan off the render thread
        InterlockedIncrement(&g_inflight);
        HANDLE t = CreateThread(nullptr, 0, SkipInstallThread, nullptr, 0, nullptr);
        if (t) CloseHandle(t);
        else { InterlockedDecrement(&g_inflight); g_sh->skip_state = 3; Log("report skip: CreateThread failed"); }
    }
    if (g_resFailed && sc == g_sc) return;
    if (sc != g_sc) { g_resFailed = !CreateResources(sc); if (g_resFailed) return; }

    DXGI_SWAP_CHAIN_DESC scd;
    if (FAILED(sc->GetDesc(&scd))) return;
    UINT bw = scd.BufferDesc.Width, bh = scd.BufferDesc.Height;
    g_sh->bb_w = bw; g_sh->bb_h = bh; g_sh->bb_format = scd.BufferDesc.Format;
    if (!Fresh() || !g_sh->visible) return;

    ID3D11Texture2D* bb = nullptr;
    if (FAILED(sc->GetBuffer(0, __uuidof(ID3D11Texture2D), (void**)&bb))) return;
    if (bb != g_rtvTex) {
        SafeRelease(g_rtv);
        D3D11_RENDER_TARGET_VIEW_DESC rv = {};
        rv.Format = scd.BufferDesc.Format;
        rv.ViewDimension = D3D11_RTV_DIMENSION_TEXTURE2D;
        if (FAILED(g_dev->CreateRenderTargetView(bb, &rv, &g_rtv))) { bb->Release(); return; }
        g_rtvTex = bb;
    }

    ID3DDeviceContextState* prev = nullptr;
    g_ctx1->SwapDeviceContextState(g_state, &prev);
    Upload();
    float x0 = (float)g_sh->img_x / bw * 2 - 1, y0 = 1 - (float)g_sh->img_y / bh * 2;
    float x1 = (float)(g_sh->img_x + (int)g_sh->img_w) / bw * 2 - 1, y1 = 1 - (float)(g_sh->img_y + (int)g_sh->img_h) / bh * 2;
    DXGI_FORMAT f = scd.BufferDesc.Format;
    float lin = 0;
    if (f == DXGI_FORMAT_R16G16B16A16_FLOAT) lin = 2.5f;  // scRGB: 1.0 = 80 nits, map UI white to ~200 nits
    else if (f == DXGI_FORMAT_R8G8B8A8_UNORM_SRGB || f == DXGI_FORMAT_B8G8R8A8_UNORM_SRGB) lin = 1.0f;
    D3D11_MAPPED_SUBRESOURCE m;
    if (SUCCEEDED(g_ctx->Map(g_cb, 0, D3D11_MAP_WRITE_DISCARD, 0, &m))) {
        float* c = (float*)m.pData;
        c[0] = x0; c[1] = y0; c[2] = x1; c[3] = y1;
        c[4] = lin; c[5] = 0; c[6] = (float)g_sh->img_w / TEX_W; c[7] = (float)g_sh->img_h / TEX_H;
        g_ctx->Unmap(g_cb, 0);
    }
    D3D11_VIEWPORT vp = {0, 0, (float)bw, (float)bh, 0, 1};
    g_ctx->OMSetRenderTargets(1, &g_rtv, nullptr);
    g_ctx->RSSetViewports(1, &vp);
    g_ctx->RSSetState(g_rs);
    float bf[4] = {0, 0, 0, 0};
    g_ctx->OMSetBlendState(g_blend, bf, 0xffffffff);
    g_ctx->OMSetDepthStencilState(nullptr, 0);
    g_ctx->IASetInputLayout(nullptr);
    g_ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP);
    g_ctx->VSSetShader(g_vs, nullptr, 0);
    g_ctx->VSSetConstantBuffers(0, 1, &g_cb);
    g_ctx->PSSetShader(g_ps, nullptr, 0);
    g_ctx->PSSetConstantBuffers(0, 1, &g_cb);
    g_ctx->PSSetShaderResources(0, 1, &g_srv);
    g_ctx->PSSetSamplers(0, 1, &g_smp);
    g_ctx->Draw(4, 0);
    ID3D11RenderTargetView* none = nullptr;
    g_ctx->OMSetRenderTargets(1, &none, nullptr);   // do not keep the back buffer bound in our state
    g_ctx1->SwapDeviceContextState(prev, nullptr);
    if (prev) prev->Release();
    bb->Release();
}

// ---- hooks --------------------------------------------------------------------------------
typedef HRESULT(STDMETHODCALLTYPE* PresentFn)(IDXGISwapChain*, UINT, UINT);
typedef HRESULT(STDMETHODCALLTYPE* Present1Fn)(IDXGISwapChain1*, UINT, UINT, const DXGI_PRESENT_PARAMETERS*);
typedef HRESULT(STDMETHODCALLTYPE* ResizeFn)(IDXGISwapChain*, UINT, UINT, UINT, DXGI_FORMAT, UINT);

static HRESULT STDMETHODCALLTYPE HkPresent(IDXGISwapChain* sc, UINT sync, UINT flags) {
    InterlockedIncrement(&g_inflight);
    static bool once;
    if (!once) { once = true; Log("Present called"); }
    if (!(flags & DXGI_PRESENT_TEST)) OnFrame(sc);
    HRESULT hr = ((PresentFn)g_origPresent)(sc, sync, flags);
    InterlockedDecrement(&g_inflight);
    return hr;
}

static HRESULT STDMETHODCALLTYPE HkPresent1(IDXGISwapChain1* sc, UINT sync, UINT flags, const DXGI_PRESENT_PARAMETERS* p) {
    InterlockedIncrement(&g_inflight);
    static bool once;
    if (!once) { once = true; Log("Present1 called"); }
    if (!(flags & DXGI_PRESENT_TEST)) OnFrame(sc);
    HRESULT hr = ((Present1Fn)g_origPresent1)(sc, sync, flags, p);
    InterlockedDecrement(&g_inflight);
    return hr;
}

static HRESULT STDMETHODCALLTYPE HkResize(IDXGISwapChain* sc, UINT n, UINT w, UINT h, DXGI_FORMAT f, UINT fl) {
    InterlockedIncrement(&g_inflight);
    if (sc == g_sc) { SafeRelease(g_rtv); g_rtvTex = nullptr; }   // the back buffers must not be referenced
    HRESULT hr = ((ResizeFn)g_origResize)(sc, n, w, h, f, fl);
    Log("ResizeBuffers %ux%u -> %08lx", w, h, hr);
    InterlockedDecrement(&g_inflight);
    return hr;
}

static LRESULT CALLBACK HkWndProc(HWND h, UINT msg, WPARAM wp, LPARAM lp) {
    InterlockedIncrement(&g_inflight);
    LRESULT r = 0;
    bool eat = false;
    if (!g_unloading && Fresh() && g_sh->visible && g_sh->bb_w && msg >= WM_MOUSEFIRST && msg <= WM_MOUSELAST &&
        msg != WM_MOUSEWHEEL && msg != WM_MOUSEHWHEEL) {
        RECT cr;
        GetClientRect(h, &cr);
        int cw = cr.right > 0 ? cr.right : 1, ch = cr.bottom > 0 ? cr.bottom : 1;
        g_sh->client_w = cw; g_sh->client_h = ch;
        int x = (short)LOWORD(lp) * (int)g_sh->bb_w / cw, y = (short)HIWORD(lp) * (int)g_sh->bb_h / ch;
        bool inside = x >= g_sh->hit_x0 && x < g_sh->hit_x1 && y >= g_sh->hit_y0 && y < g_sh->hit_y1;
        if (msg == WM_MOUSEMOVE) {
            g_sh->hover = inside;
        } else if (msg == WM_LBUTTONDOWN || msg == WM_LBUTTONDBLCLK) {
            if (inside) { g_sh->pressed = 1; eat = true; }
        } else if (msg == WM_LBUTTONUP) {
            if (g_sh->pressed) { g_sh->pressed = 0; if (inside) g_sh->clicks++; eat = true; }
        }
    } else if (g_sh && (msg == WM_MOUSELEAVE || msg == WM_KILLFOCUS)) {
        g_sh->hover = 0; g_sh->pressed = 0;
    }
    if (!eat) r = CallWindowProcW(g_origWndProc, h, msg, wp, lp);
    InterlockedDecrement(&g_inflight);
    return r;
}

static bool PatchSlot(int i, void* fn, void** orig) {
    DWORD old;
    if (!VirtualProtect(&g_vtbl[i], sizeof(void*), PAGE_READWRITE, &old)) return false;
    if (orig) *orig = g_vtbl[i];
    g_vtbl[i] = fn;
    VirtualProtect(&g_vtbl[i], sizeof(void*), old, &old);
    return true;
}

static bool FindVtable() {
    WNDCLASSEXW wc = {sizeof(wc)};
    wc.lpfnWndProc = DefWindowProcW;
    wc.hInstance = g_module;
    wc.lpszClassName = L"S14OverlayDummy";
    RegisterClassExW(&wc);
    HWND w = CreateWindowExW(0, wc.lpszClassName, L"", WS_OVERLAPPEDWINDOW, 0, 0, 64, 64, nullptr, nullptr, g_module, nullptr);
    DXGI_SWAP_CHAIN_DESC d = {};
    d.BufferCount = 1; d.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM; d.BufferDesc.Width = 64; d.BufferDesc.Height = 64;
    d.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT; d.OutputWindow = w; d.SampleDesc.Count = 1; d.Windowed = TRUE;
    d.SwapEffect = DXGI_SWAP_EFFECT_DISCARD;
    IDXGISwapChain* sc = nullptr; ID3D11Device* dev = nullptr; ID3D11DeviceContext* ctx = nullptr;
    HRESULT hr = D3D11CreateDeviceAndSwapChain(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, nullptr, 0, D3D11_SDK_VERSION,
                                               &d, &sc, &dev, nullptr, &ctx);
    if (FAILED(hr))
        hr = D3D11CreateDeviceAndSwapChain(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, nullptr, 0, D3D11_SDK_VERSION,
                                           &d, &sc, &dev, nullptr, &ctx);
    if (SUCCEEDED(hr)) g_vtbl = *(void***)sc;
    SafeRelease(ctx); SafeRelease(dev); SafeRelease(sc);
    DestroyWindow(w);
    UnregisterClassW(wc.lpszClassName, g_module);
    Log("dummy swap chain: %08lx, vtable %p", hr, g_vtbl);
    return g_vtbl != nullptr;
}

static DWORD WINAPI UnloadThread(LPVOID) {
    for (int i = 0; i < 100; i++) {           // up to 5 s for threads to leave our code
        Sleep(50);
        if (g_inflight == 0) {
            Sleep(100);
            if (g_inflight == 0) {
                if (g_sh) { g_sh->dll_state = 2; UnmapViewOfFile(g_sh); g_sh = nullptr; }
                if (g_map) CloseHandle(g_map);
                Log("unloaded");
                if (g_log) fclose(g_log);
                FreeLibraryAndExitThread(g_module, 0);
            }
        }
    }
    Log("unload: threads still inside, staying loaded but inactive");
    return 0;
}

static void BeginUnload() {   // called on the render thread
    if (InterlockedExchange(&g_unloading, 1)) return;
    Log("unload requested");
    UnhookSkip();
    if (g_vtbl) {
        PatchSlot(8, g_origPresent, nullptr);
        PatchSlot(13, g_origResize, nullptr);
        if (g_origPresent1) PatchSlot(22, g_origPresent1, nullptr);
    }
    if (g_hwnd && g_origWndProc && (WNDPROC)GetWindowLongPtrW(g_hwnd, GWLP_WNDPROC) == HkWndProc)
        SetWindowLongPtrW(g_hwnd, GWLP_WNDPROC, (LONG_PTR)g_origWndProc);
    ReleaseResources();
    HANDLE t = CreateThread(nullptr, 0, UnloadThread, nullptr, 0, nullptr);
    if (t) CloseHandle(t);
}

static DWORD WINAPI InitThread(LPVOID) {
    wchar_t path[MAX_PATH];
    GetModuleFileNameW(g_module, path, MAX_PATH);
    wchar_t* slash = wcsrchr(path, L'\\');
    if (slash) {
        wcscpy(slash + 1, L"..\\logs");
        CreateDirectoryW(path, nullptr);
        wcscat(path, L"\\overlay_dll.log");
    }
    g_log = _wfopen(path, L"a");
    Log("---- san14_overlay loaded (pid %lu)", GetCurrentProcessId());
    for (int i = 0; i < 600 && !g_hwnd; i++) {
        g_hwnd = FindWindowW(L"KT_SANGOKUSHI14_WINDOW", nullptr);
        if (!g_hwnd) Sleep(100);
    }
    if (!g_hwnd) { Log("game window not found"); return 0; }
    OpenShared();
    if (!FindVtable()) { if (g_sh) g_sh->dll_state = 3; return 0; }
    bool ok = PatchSlot(8, (void*)HkPresent, &g_origPresent) && PatchSlot(13, (void*)HkResize, &g_origResize) &&
              PatchSlot(22, (void*)HkPresent1, &g_origPresent1);
    g_origWndProc = (WNDPROC)SetWindowLongPtrW(g_hwnd, GWLP_WNDPROC, (LONG_PTR)HkWndProc);
    Log("hooks %s, wndproc %p", ok ? "installed" : "FAILED", (void*)g_origWndProc);
    if (g_sh) g_sh->dll_state = ok ? 1 : 3;
    return 0;
}

BOOL WINAPI DllMain(HINSTANCE h, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_module = h;
        DisableThreadLibraryCalls(h);
        HANDLE t = CreateThread(nullptr, 0, InitThread, nullptr, 0, nullptr);
        if (t) CloseHandle(t);
    }
    return TRUE;
}
