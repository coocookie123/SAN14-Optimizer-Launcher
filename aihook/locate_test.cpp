// Offline check of the AI DLL's locator (Locate) against a memory dump made by tools/dump.py.
// Build and run: python -P aihook/build.py --test [dump dir]
// Prints what Locate found; build.py compares the addresses with tools/sigs.py on the same dump.
#include "san14_ai.cpp"

int main(int argc, char **argv) {
    if (argc < 3) {
        printf("usage: locate_test <image.bin> <base hex>\n");
        return 2;
    }
    FILE *f = fopen(argv[1], "rb");
    if (!f) { printf("cannot open %s\n", argv[1]); return 2; }
    fseek(f, 0, SEEK_END);
    size_t n = (size_t)ftell(f);
    fseek(f, 0, SEEK_SET);
    uintptr_t want = (uintptr_t)strtoull(argv[2], nullptr, 16);
    uint8_t *base = (uint8_t *)VirtualAlloc((void *)want, n, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    if (base != (uint8_t *)want) { printf("cannot map at %p (got %p)\n", (void *)want, base); return 2; }
    if (fread(base, 1, n, f) != n) { printf("short read\n"); return 2; }
    fclose(f);
    Native a{};
    const wchar_t *why = Locate(a, base);
    if (why) { printf("FAIL: %ls\n", why); return 1; }
    auto r = [&](const void *p) { return (unsigned long long)((const uint8_t *)p - base); };
    printf("sig data_root.root_ptr %llx\n", r(a.rootPtr));
    printf("sig progress_vtable %llx\n", r(a.progVt));
    printf("sig progress_vtable.update %llx\n", r(a.progVt[SLOT_progress_vtable_update]));
    printf("sig army_setters.lock_off %x\n", a.offLock);
    printf("sig army_setters.action_off %x\n", a.offAction);
    printf("sig army_setters.action_max %x\n", a.actMax);
    printf("sig army_setters.ttype_off %x\n", a.offTType);
    printf("sig army_setters.tid_off %x\n", a.offTId);
    if (a.logVt) {
        printf("sig misclog_vtable %llx\n", r(a.logVt));
        printf("sig misclog_vtable.execute %llx\n", r(a.logVt[SLOT_misclog_vtable_execute]));
        printf("sig misclog_execute.type_off %x\n", a.logTypeOff);
    } else {
        printf("misclog not found (tree probes off)\n");
    }
    printf("vtables army %llx person %llx hex %llx district %llx world %llx\n", r(a.vtArmy), r(a.vtPerson),
           r(a.vtHex), r(a.vtDistrict), r(a.vtWorld));
    TrapNative t{};                       // trapnear (陷阱近身触发)
    const wchar_t *tw = LocateTrap(t, base);
    if (tw) { printf("FAIL trapnear: %ls\n", tw); return 1; }
    printf("sig trap_trigger.act_range %llx\n", r(t.act));
    printf("sig trap_act_range %llx\n", r(t.act));
    printf("sig trap_category.cat_off %x\n", t.catOff);
    printf("sig trap_category.cat_trap %x\n", t.cat);
    if (t.effect) {
        printf("sig trap_fire_call.effect_fn %llx\n", r(t.effect));
        printf("sig trap_effect_fn %llx\n", r(t.effect));
    } else
        printf("trap effect function not found (trap log off)\n");
    if (t.rootPtr) printf("sig data_root.root_ptr %llx\n", r(t.rootPtr));
    return 0;
}
