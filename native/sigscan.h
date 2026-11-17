// Byte-pattern and RTTI helpers shared by the two in-game DLLs (overlay/san14_overlay.cpp, aihook/san14_ai.cpp).
// Include after the DLL's generated sigs_gen.h (Sig, SigField come from there).
#pragma once
#include <windows.h>
#include <stdint.h>
#include <string.h>

struct Pat {
    int16_t b[256];
    int n;
};

static Pat MakePat(const char* s) {
    Pat p{};
    auto hex = [](char c) { return c <= '9' ? c - '0' : (c | 0x20) - 'a' + 10; };
    while (*s && p.n < 256) {
        if (*s == ' ') { s++; continue; }
        p.b[p.n++] = s[0] == '?' ? -1 : (int16_t)(hex(s[0]) * 16 + hex(s[1]));
        s += 2;
    }
    return p;
}

static bool MatchAt(const uint8_t* m, const Pat& p) {
    for (int i = 0; i < p.n; i++)
        if (p.b[i] >= 0 && m[i] != (uint8_t)p.b[i]) return false;
    return true;
}

// first match in [from, from+len) and the number of matches (the first pattern byte is never a wildcard)
static uint8_t* FindPat(uint8_t* from, size_t len, const Pat& p, int* count) {
    uint8_t* hit = nullptr;
    *count = 0;
    if (len < (size_t)p.n) return nullptr;
    uint8_t* end = from + len - p.n + 1;
    for (uint8_t* q = from; q < end; q++) {
        q = (uint8_t*)memchr(q, (uint8_t)p.b[0], end - q);
        if (!q) break;
        if (MatchAt(q, p)) {
            if (!hit) hit = q;
            (*count)++;
        }
    }
    return hit;
}

static uint8_t* RelTarget(uint8_t* insn, int len) { return insn + len + *(int32_t*)(insn + len - 4); }

static bool Section(uint8_t* base, const char* name, uint8_t** p, size_t* n) {
    IMAGE_NT_HEADERS64* nt = (IMAGE_NT_HEADERS64*)(base + ((IMAGE_DOS_HEADER*)base)->e_lfanew);
    IMAGE_SECTION_HEADER* s = IMAGE_FIRST_SECTION(nt);
    for (unsigned i = 0; i < nt->FileHeader.NumberOfSections; i++, s++)
        if (strncmp((const char*)s->Name, name, 8) == 0) {
            *p = base + s->VirtualAddress;
            *n = s->Misc.VirtualSize;
            return true;
        }
    return false;
}

// MSVC RTTI: type descriptor name in .data -> complete object locator in .rdata -> the one vtable after it
static void** VtableByRtti(uint8_t* base, const char* mangled) {
    uint8_t *data, *rdata;
    size_t dn, rn;
    if (!Section(base, ".data", &data, &dn) || !Section(base, ".rdata", &rdata, &rn)) return nullptr;
    size_t len = strlen(mangled) + 1;
    uint8_t* str = nullptr;
    for (uint8_t* q = data; q + len <= data + dn; q++) {
        q = (uint8_t*)memchr(q, mangled[0], data + dn - len + 1 - q);
        if (!q) break;
        if (memcmp(q, mangled, len) == 0) { str = q; break; }
    }
    if (!str) return nullptr;
    uint32_t td = (uint32_t)(str - 0x10 - base);
    void** vt = nullptr;
    int found = 0;
    for (uint8_t* q = rdata; q + 24 <= rdata + rn; q += 4) {
        const uint32_t* c = (const uint32_t*)q;
        if (c[3] != td || c[0] != 1 || c[1] != 0 || c[5] != (uint32_t)(q - base)) continue;
        for (uint8_t* v = rdata; v + 16 <= rdata + rn; v += 8)
            if (*(uint8_t**)v == q) { vt = (void**)(v + 8); found++; }
    }
    return found == 1 ? vt : nullptr;
}

// ---- sigs from sigs_gen.h: a match of one variant -----------------------------------------------
struct SigHit {
    uint8_t* p;                          // start of the match
    const SigField* f;                   // that variant's fields
};

// first variant that matches at `at`
static bool SigAt(uint8_t* at, const Sig& s, SigHit* h) {
    for (int i = 0; i < s.nvariants; i++)
        if (MatchAt(at, MakePat(s.variants[i].pattern))) {
            *h = {at, s.variants[i].fields};
            return true;
        }
    return false;
}

// first variant that matches exactly s.count times in [from, from+len)
static bool SigScan(uint8_t* from, size_t len, const Sig& s, SigHit* h) {
    for (int i = 0; i < s.nvariants; i++) {
        int n;
        uint8_t* hit = FindPat(from, len, MakePat(s.variants[i].pattern), &n);
        if (n == s.count) {
            *h = {hit, s.variants[i].fields};
            return true;
        }
    }
    return false;
}

static uint8_t* SigRel(const SigHit& h, int f) { return RelTarget(h.p + h.f[f].at, h.f[f].len); }
static uint32_t SigU32(const SigHit& h, int f) { return *(const uint32_t*)(h.p + h.f[f].at); }
