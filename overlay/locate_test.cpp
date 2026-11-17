// Offline check of the report-skip locator (LocateGame) against a memory dump made by tools/dump.py.
// The dump is mapped at the base address it was taken at, so absolute pointers in it stay valid.
// Build and run: python -P overlay/build.py --test <dump dir>
// Prints "sig <manifest name>[.<field>] <hex>" for everything LocateGame found; build.py compares these
// lines with what tools/sigs.py finds in the same dump (sigs/sigs.json), so the DLL and the Python side
// cannot drift apart.
#include "san14_overlay.cpp"

#include <stdlib.h>

int main(int argc, char** argv) {
    if (argc < 3) {
        printf("usage: locate_test <image.bin> <base hex>\n");
        return 2;
    }
    FILE* f = fopen(argv[1], "rb");
    if (!f) { printf("cannot open %s\n", argv[1]); return 2; }
    fseek(f, 0, SEEK_END);
    size_t n = (size_t)ftell(f);
    fseek(f, 0, SEEK_SET);
    uintptr_t want = (uintptr_t)strtoull(argv[2], nullptr, 16);
    uint8_t* base = (uint8_t*)VirtualAlloc((void*)want, n, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    if (base != (uint8_t*)want) { printf("cannot map at %p (got %p)\n", (void*)want, base); return 2; }
    if (fread(base, 1, n, f) != n) { printf("short read\n"); return 2; }
    fclose(f);
    const char* why = LocateGame(base);
    if (why) { printf("FAIL: %s\n", why); return 1; }
    GameApi& a = g_game;
    auto r = [&](const void* p) { return (unsigned long long)((const uint8_t*)p - base); };
    auto addr = [&](const char* ref, const void* p) { printf("sig %s %llx\n", ref, r(p)); };
    auto val = [&](const char* ref, uint32_t v) { printf("sig %s %x\n", ref, v); };
    addr("strategy_vtable", a.usVt);
    addr("report_wait_vtable", a.rwVt);
    addr("us_update", a.usUpdate);
    addr("report_wait_update", a.rwUpdate);
    addr("us_update.is_top", (void*)a.isTop);
    addr("report_co_head.is_spectator", (void*)a.isObserver);
    addr("report_callback.get_mgr", (void*)a.getMgr);
    addr("report_callback.find_state", (void*)a.findState);
    addr("report_callback.state_name", a.gameStateName);
    addr("report_callback.restore_btn", (void*)a.restoreBtn);
    addr("us_resume", (void*)a.resume);
    addr("us_open_cmd", (void*)a.openCmd);
    addr("report_co_tail.snd_flag", (void*)a.sndFlag);
    addr("report_co_tail.snd_mgr", (void*)a.sndMgr);
    addr("report_co_tail.play_snd", (void*)a.playSnd);
    val("us_update.stage_off", a.usStage);
    val("report_wait_update.state_off", a.rwState);
    val("report_callback.hud_off", a.hudOff);
    val("report_callback.panel_off", a.panelOff);
    val("report_callback.panel2_off", a.panel2Off);
    val("report_callback.panel_vfn", a.panelVfn);
    val("report_callback.btn_off", a.btnOff);
    val("report_callback.btn_vfn", a.btnVfn);
    printf("stage read at update +%x: ", a.stageReadOff);
    for (unsigned i = 0; i < a.stageReadLen; i++) printf("%02x ", a.stageRead[i]);
    printf("(%s)\n", a.gameStateName);
    return 0;
}
