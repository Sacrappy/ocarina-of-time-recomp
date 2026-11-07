// From metroid-prime-4-recomp runtime/nvn_procs.c @ 586e91e1b6f38c9e512e39f2d47a046fdf273cf0 (MIT, Copyright (c) 2026 Jeff Aigner), copied; up to 256 distinct unknown proc names are logged; NVN2 procs without an implementation get logging stubs (nvn_procs_stub.inc).
// From fe3h-pc-recomp runtime/nvn_procs.c @ 47ef99126793dbd13e4a475feb910a13ef6639b8 (MIT, Copyright (c) 2026 Jeff Aigner), copied unchanged.
// From totk-pc-recomp runtime/nvn_procs.c @ c944f9f94520874f2a669add30dd08d29ee9f7b5 (MIT, Copyright (c) 2026 Valiantsin Dzerakh), copied unchanged.
// NVN proc table. Every name the game's proc loader asks for gets a synthetic host
// address through hostfn_register before hostfn_seal; nvnBootstrapLoader and nvnDeviceGetProcAddress hand them out.
// nvnp_<name> is the implementation of one proc: the weak definitions here stop with the proc's name, nvn_api_*.c
// and nvn_ext.c define the strong ones. The name list is runtime/nvn_procs.inc (tools/gen_nvn_procs.py).
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

#include "gfx.h"
#include "cfg.h"
#include "host.h"

// The strong nvnp_nvnDeviceGetProcAddress below replaces the stand-in generated for the same row.
#define nvnp_nvnDeviceGetProcAddress nvnp_stub_nvnDeviceGetProcAddress

#define NVN_PROC(n) void nvnp_##n(Cpu *c);
#include "nvn_procs.inc"
#undef NVN_PROC

#define NVN_PROC(n)                                                                                                  \
    __attribute__((weak)) void nvnp_##n(Cpu *c) {                                                                     \
        (void)c;                                                                                                     \
        rt_fatal("NVN " #n " is not implemented");                                                                   \
    }
#include "nvn_procs.inc"
#undef NVN_PROC
#undef nvnp_nvnDeviceGetProcAddress
void nvnp_nvnDeviceGetProcAddress(Cpu *c);

// NVN2 names the game loads that have no implementation (nvn_procs_stub.inc): a stub logs its first calls with the
// caller and the first arguments (three, or OOT_STUB_LOG of them), and its call count at every power of two from
// 1024, and returns 0, so the game runs on and the log shows which procs it uses and how often. For an *Indirect
// proc it also prints the 16 bytes at x1 when they are readable (the indirect arguments as the CPU sees them then).
static int stub_log_n(void) {
    static int n = -1;
    if (n < 0) {
        const char *e = cfg_get("OOT_STUB_LOG");
        n = e && *e ? atoi(e) : 3;
    }
    return n;
}
static void stub_log(const char *name, Cpu *c, long k) {
    char at[128], peek[80] = "";
    if (strstr(name, "Indirect")) {
        MEMORY_BASIC_INFORMATION mi;
        const uint32_t *p = (const uint32_t *)(uintptr_t)c->x[1];
        if (VirtualQuery(p, &mi, sizeof mi) && mi.State == MEM_COMMIT && !(mi.Protect & (PAGE_NOACCESS | PAGE_GUARD)) &&
            (const uint8_t *)p + 16 <= (const uint8_t *)mi.BaseAddress + mi.RegionSize)
            snprintf(peek, sizeof peek, "; [x1] %08x %08x %08x %08x", p[0], p[1], p[2], p[3]);
    }
    rt_log("nvn2 stub %s #%ld from %s: x0 %#llx x1 %#llx x2 %#llx x3 %#llx x4 %#llx x5 %#llx%s (returns 0)", name, k,
           ldr_describe(c->x[30] - 4, at, sizeof at), (unsigned long long)c->x[0], (unsigned long long)c->x[1],
           (unsigned long long)c->x[2], (unsigned long long)c->x[3], (unsigned long long)c->x[4],
           (unsigned long long)c->x[5], peek);
}
#define NVN_STUB(n)                                                                                                  \
    static void nvnp_stub_##n(Cpu *c) {                                                                               \
        static volatile LONG seen;                                                                                   \
        LONG k = InterlockedIncrement(&seen);                                                                         \
        if (k >= 1024 && !(k & (k - 1))) rt_log("nvn2 stub " #n ": %ld calls", (long)k);                              \
        if (k <= stub_log_n()) stub_log(#n, c, k);                                                                    \
        ret_x(c, 0);                                                                                                 \
    }
#include "nvn_procs_stub.inc"
#undef NVN_STUB
typedef struct { const char *name; uint64_t addr; } Proc;

#define NVN_PROC(n) +1
#define NVN_STUB(n) +1
enum { PROC_COUNT = 0
#include "nvn_procs.inc"
#include "nvn_procs_stub.inc"
};
#undef NVN_PROC
#undef NVN_STUB

static Proc procs[PROC_COUNT];
static int nprocs;

static int by_name(const void *a, const void *b) { return strcmp(((const Proc *)a)->name, ((const Proc *)b)->name); }

void nvn_procs_register(void) {
    if (nprocs) return;
#define NVN_PROC(n) procs[nprocs++] = (Proc){#n, hostfn_register("nvn:" #n, nvnp_##n)};
#include "nvn_procs.inc"
#undef NVN_PROC
#define NVN_STUB(n) procs[nprocs++] = (Proc){#n, hostfn_register("nvn:" #n, nvnp_stub_##n)};
#include "nvn_procs_stub.inc"
#undef NVN_STUB
    qsort(procs, (size_t)nprocs, sizeof *procs, by_name);
}

uint64_t nvn_proc_address(const char *name) {
    if (!name) return 0;
    const Proc *p = bsearch(&(Proc){name, 0}, procs, (size_t)nprocs, sizeof *procs, by_name);
    if (p) return p->addr;
    // Unknown names get NULL, as on hardware; each distinct one is logged once.
    static SRWLOCK lock = SRWLOCK_INIT;
    static char seen[256][96];
    static int nseen;
    AcquireSRWLockExclusive(&lock);
    int fresh = 1;
    for (int i = 0; i < nseen; i++)
        if (!strncmp(seen[i], name, sizeof seen[i] - 1)) fresh = 0;
    if (fresh && nseen < 256) {
        strncpy(seen[nseen], name, sizeof seen[0] - 1);
        nseen++;
    } else {
        fresh = 0;
    }
    ReleaseSRWLockExclusive(&lock);
    if (fresh) rt_log("nvn: no such proc \"%s\", returning NULL", name);
    return 0;
}

// PFNNVNGENERICFUNC nvnDeviceGetProcAddress(const NVNdevice *device, const char *name)
void nvnp_nvnDeviceGetProcAddress(Cpu *c) {
    const char *name = arg_p(c, 1);
    ret_x(c, nvn_proc_address(name));
}
