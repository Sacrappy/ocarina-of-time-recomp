# Plan

## Phase 0: survey

What the dump is, before any code is lifted. metroid-prime-4-recomp's `docs/plan.md` (Phase 0) is the model.

| Question | Answer |
|---|---|
| Dump | `The Legend of Zelda Ocarina of TIme [04004860224DEC000].nup`, 25,020,741,061 bytes. One package: kind update, flagged standalone (it carries the whole game), title id `0400486024DEC000` (the file name has one digit too many), version 131072 (`v131072`), display version 1.1.1, title "The Legend of Zelda Ocarina of Time" |
| Container | NUP, header version 1: loose ExeFS and RomFS trees, each file raw or zstd, with a SHA-256 per file and over the TOC; nothing encrypted, so no keys are needed to read it. Layout in `tools/extract_nup.py` (ours, written from Lichii's reader, GPL-3.0, read for behaviour only). ExeFS extracted to `build/extracted/v131072/exefs`, every hash verified |
| ExeFS | `main` 29.9 MB, `sdk` 7.8 MB, `subsdk0` 0.5 MB, `rtld`, `main.npdm`. RomFS: 186,587 files, 24.43 GiB, extracted (2026-11-05) as `build/extracted/v131072/romfs/romfs.bin` (26,236,424,856 bytes, SHA-256 `70968824...`) + `index.json`, status pass: every file checked against the TOC's hash; a 400-file sample read through the index matched a loose extraction byte for byte |
| Executable format | NSO0, but `main`'s flags are 0xFF: bit 7 marks ZBIC segments, zstd frames with the magic "ZBIC" whose FSE table headers (NCount) are coded by binary interpolative coding instead of zstd's own form (first seen in Switch firmware 22.0.0). `sdk`, `subsdk0`, `rtld` are LZ4 as before. Our decoder: `tools/zbic/zbic_ncount.c` (the format is described there) hooked into upstream zstd 1.5.7 (BSD) by `tools/zbic/zbic.patch`, built by `tools/build_zbic.py` into `toolchain/zbic/nso_unzbic`; `tools/extract_nup.py` runs it on ZBIC modules and checks every segment against the SHA-256 in the NSO header. Written from kinnay/zbic's behaviour (GPL-2.0, Atmosphère's zstd), no code taken. Checks (2026-11-05): `main` decompresses byte for byte as kinnay/zbic does, its three segment hashes match, on Linux and on Windows (MSYS2 UCRT64 gcc); and 600 frames made locally by kinnay/zbic's encoder (levels 1 to 19, 1 byte to 2 MB, random, skewed and repetitive data; 2,502 table headers, 1,134 of them with low-probability symbols) all decode. The encoder stays out of the repository, so that check is not reproducible from it |
| Images | `main` 0x4EA0000 (60 MB of code), `subsdk0` 0xE0000, `sdk` 0x1080000; `sdk` 28,707 symbols, `subsdk0` 1,083, `main` 1,249 |
| CPU | ARMv8.3 code: return address signing (`PACIBSP` ... `RETAB` in every function), `LDAPR`, plus a few ARMv8.2 `UDOT`/`SDOT` and FP16 `FNEG`/`FABS`. All now lifted (`recomp/lifter.py`, `recomp/simd_c.py`); see below |
| Engine | Nintendo EPD's TotK-era engine by its file names (`.byml.zs`, `.blarc`, `.bfarc`), Havok 2025.2 (Physics, Cloth, AI, Animation), NPLN Google APIs 1.55.1 |
| Graphics | NVN2 22.2.8 (`NintendoSDK_NVN-22_2_8` in `sdk`; `Nin_Ounce_NVN2` in asset names; Ounce is the Switch 2), NintendoWare G3d2 22.2.8, Vfx3, SMAA. See "NVN2 survey" below |
| SDK version | 22.2.8 (`NintendoSdk_nnSdk-Generic_22_2_8`), movie 22.2.8, libcurl 22.2.8, NPLN 1.55.1. Product path `Owl\App\Rom\Ounce64`. `main` imports 1,183 symbols, 924 of them `nn::*`/`movie::*`; the TotK host replaces 123 by symbol, the FE3H host 138 (`tools/survey_imports.py`) |
| Updates, DLC | None in the dump |

**Lifter run (2026-11-05)**, `recomp/gen.py` over v131072: `main` 249,589 functions, 1,061 MB of C; `sdk` 45,907, 151 MB; `subsdk0` 1,547, 9 MB; 47 s. As copied from MP4, 223,448 instructions were unsupported: `RETAB` 149,725, `LDAPR` 73,664, `PACGA` 58, `PACIA`/`PACIB`/`AUTIB`/`XPACI` 10, `UDOT` 4, FP16 `FNEG` 4. Now 0 in every module:

- PAC branches (`BRAA`, `BLRAA`, `RETAA` and their B and Z forms) lift as the plain branch, and `PACIA`...`AUTDB`, `XPACI`/`XPACD` as identities. The signing instructions in hint space already lifted as no-ops, so no pointer is ever signed and authentication always succeeds. `PACGA` gives a deterministic mix of its two inputs in the upper half, as `sdk` only recomputes and compares it.
- `LDAPR` lifts like `LDAR`.
- `UDOT`/`SDOT` (vector) and FP16 `FABS`/`FNEG` (vector) are lowered in `simd_c.py`.

Trap sites left: 394,261 in `main`, all zero padding (393,731), `udf` words (528) and 2 data words, the same class MP4 had.

Differential tests (Windows venv): `tests/diff/lift.py` over this game, 72,000 encodings and 215,928 cases, 0 failures (A57 model). It now takes the module bases from `recomp/layout.py`, and has `--model max` and `--only` to test later-architecture forms: every distinct `LDAPR`, `UDOT`/`SDOT` and FP16 `FNEG`/`FABS` word of the game, 320 encodings and 16,000 cases on Unicorn's max CPU, 0 failures. `tests/diff/simd.py` passes, its lifter-only list being exactly the new DotProd and FP16 forms. The PAC lowering is not checked against Unicorn, as its max CPU signs pointers for real. Not run yet: `func.py`, `jumptables_check.py`. The harness's `fnprof_hit` stub, stale in MP4 too, follows `cpu.h` again.

**NVN2 survey (2026-11-05)**

- Proc names: `main` holds 738 distinct `nvn*` names as 738 separate string literals spread over 1.6 MB of rodata, one
  per load site (the SDK's per-function `nvnDeviceGetProcAddress` pattern), not one table; `sdk` 13. Our NVN front end
  implements 539 (`runtime/nvn_procs.inc`); 528 of the game's are among them, 219 are not, and 10 of ours the game
  never names. The new ones are mostly whole NVN2 features: ray tracing (acceleration structures, `DispatchRays`),
  mesh and task shaders (`DrawMeshTasks*`), DLSS (`EvaluateDlss`), shading rate images, cooperative vectors, device
  command buffers, push constants, debug groups, workload priorities. A name in the table is not a call: which the
  game reaches, and whether the shared names kept their Switch 1 signatures and builder layouts, is the next question.
- Shaders: the RomFS ships TotK's set of archives under the NVN2 name (`Shader/Model.Product.110.product.Nin_Ounce_NVN2.bfsha.zs`
  and its siblings, `Layout2Shader/MergeShader...bfsha.zs`, `Shader/ApplicationPackage.Nin_Ounce_NVN2.release.sarc.zs`,
  effect `.vsetb`, fonts), zstd with `zs.zsdic` from `Pack/ZsDic.pack.zs`. `Model.bfsha` (320 MB) holds 2 BNSH, 25,029
  programs: 23,346 vertex and 25,029 pixel stages. The BNSH container and the NVN control magic (0x98761234) are as on
  Switch 1, but the control section is 224 to 280 bytes (0x880 on Switch 1) and the code is Ampere (SM86, the GPU of
  the Switch 2's T239): every stage has a 128-bit `EXIT` (opcode 0x94d) and none Maxwell's, and the code starts with
  the 0x60-byte Turing+ program header (Maxwell's is 0x50). The vendored Ryujinx translator decodes Maxwell only, so
  every shader needs an SM86 front end: the largest single piece of work this port has.
- Lichii (GPL-3.0, read for behaviour only: `NX2_NOTES.md`, `src/shader_recompiler/frontend/ampere/`, its
  `lower_to_maxwell.h` and the `src/tests/video_core/ampere_*.cpp` cases) runs these games by emulating the GPU: Ampere classes
  C797/C7C0/C7B5, Turing MME64 macros, QMD 3.0, and an Ampere-to-Maxwell lowering of SM86 programs ahead of yuzu's
  recompiler. Only the shader half concerns this port, which replaces NVN at its API: its rows on SM86 forms
  (uniform registers and `R2UR`/`UP2UR`, `LDSM`, `ISBERD`/`ISBEWR` for task and mesh programs, IPA modes, formatted
  surface access, `CALL.REL.NOINC`) are a checklist, as are its notes on the console's blit constant bank (bank 24)
  and texture memory kinds.

Next:

1. Done. RomFS: `runtime/romfs.c` reads one image through an index of (offset, size, path) and never parses the RomFS
   format, so `tools/extract_nup.py` writes the NUP's loose files back to back, in the index's path order, into
   `romfs/romfs.bin` with the `index.json` `tools/romfs_index.py` reads (status "pass" when every file was checked
   against its SHA-256). No runtime change; one file instead of 186,587 opens. `--romfs-loose` still writes loose files.
2. Done: ZBIC without GPL code (see the table).
3. The SDK and NVN versions; the NVN2 entry points `main` imports (`tools/survey_imports.py`).
4. Rewrite the game-bound tables and reach a build (Phase 1).

## Phase 1: build

**Reached (2026-11-05): `--boot` passes.** `oot.exe` (302 MiB, 267 MiB of lifted code for 297,043 functions) builds
with MSYS2 clang in 257 s; `--selftest`, `--probe-loader`, `--probe-kernel`, `--probe-gfx-abi`, `--probe-astc` and
`--probe-dpi` pass; `--boot` runs through `nn::init::Start`, the three modules' constructors and `nnMain` to the first
`nvnBootstrapLoader` call, from `main+0x346bf8c` as Phase 0 found, after 50 SVCs and 509 host calls. The game-bound
tables rewritten: `boot.c` (init_array sizes 9783/0/96, the NVN return address, the `SetFocusHandlingMode` checkpoint at
`main+0x1202d44`, which TotK's engine reaches and MP4 never did), `loader.c` (probe table), `build_game.py` (1.1.1,
the ExeFS manifest from `extract_nup.py`), `ov_oot.c` (empty of MP4's entries, none imported here). Between the build
and the pass, each a Switch 2 or SDK 22 difference the three earlier hosts never met:

- `GetInfo` 29 to 33 (`memory.c`): `nnosInitialize` asks for the VAMM region (29/30) before anything else; the alias
  reservation is split, the game's alias region its lower half and the VAMM region its upper half, so
  `MapPhysicalMemory` works in both; 31/32 (remote memory for the graphics stack) 0, 33 (VAMM granule) the page.
  Meanings from Lichii's `svc_types.h` (GPL-3.0, read for behaviour).
- The constructor flag decoder (`loader.c`) takes the `PACIBSP` prologue SDK 22 puts before `DT_INIT`'s `ADRP`.
- The thread handle at `TLR+0x110` (`kernel.c`): SDK 22's `nn::os::detail::InternalCriticalSection` reads the owner tag
  for its CAS from there (`mrs tpidrro_el0; ldr w, [x, #0x110]`), where Switch 2 kernels write it (Lichii
  `k_thread.cpp`), instead of from the thread object behind `TLR+0x1F8`. With 0 there every unlocked mutex read as held
  by the current thread: the first heap allocation of `main`'s constructors aborted in `pthread_mutex_lock`.
- `nn::oe` (SDK 22): `SetGraphicsScaleChangedNotificationEnabled` (no-op), `GetCurrentGraphicsScale` (0, as Lichii's
  `IGraphicsScaleController` answers), `GetDisplayVersion` ("1.1.1"): lifted, they read the applet session the
  overridden `Initialize` never opens.
- `nn::vi` (SDK 22): HDR status, paper white, VRR and 120 Hz capability, extended swap interval and HDR layer metadata
  stubbed in `ov_oot.c` (SDR, fixed rate); `QueryDisplayCurrentRefreshRate` is a `u32` Hz (60 or 120) plus a bool.
- `nv::InitializeGraphics(0, 0)`: the Switch 2 game lends no work memory (`ov_gfx.c` accepts the form).

Still MP4's: `ipc.c`'s SDK heap constructor address for `--probe-ipc` (fails: "sdk+0x217770 is not an sdk
constructor"), `--probe-services`' override list (52 of MP4's host overrides match no import here), the installer
(`installer/layout.py`, `make_release.py`). Not run yet: `tests/diff/func.py`, `tests/jumptables_check.py`.

## Phase 2: the game past the checkpoint

Started 2026-11-05 (`oot.exe --seconds N`; run it with `WSLENV=OOT_TRACE:OOT_VOLUME:... OOT_TRACE=gfx,fs ...`, as WSL's
environment does not reach a Windows process otherwise; every `OOT_*` variable set in this session before that was
silently ignored). Reached so far: the NVN device, queue, three memory pools, texture pool (33,068 descriptors), sampler
pool, a 2560x1440 window with 3 textures, command buffer and buffers; HID; the SDK's VAMM mapping memory into the
region GetInfo 29/30 gave it; 31 RomFS files read (heap definition, region mask, rom config, address table, resource
size table, zstd dictionaries, the first packs, the RSDB tables, agl/gsys resources, the shader ApplicationPackage).
Fixed on the way:

- NVN2 ABI: after `nvnDeviceInitialize` the game requires `nvnDeviceGetInteger` API major 231 and minor >= 40
  (`main+0x346c10c`) and otherwise abandons its graphics bring-up, leaving a global it later locks null (the first
  crash past the checkpoint, `main+0x946a20`). `devinfo[0..1]` = 231.40; the other limits are still the Tegra X1's.
- 32 NVN2 procs the bring-up loads (`runtime/nvn_api_nvn2.c`, `nvnDeviceGetFlags` in `nvn_api_core.c`, names in
  `tools/gen_nvn_procs.py` NVN2_OOT): window HDR/colour space/acquire timeout, queue ray tracing memory and primary
  queue, device fast-clear registration, the program builder (device at +0, sample shading at +8 in the game's
  object). The game then asks for 174 more NVN2 names we lack (58 command buffer, 57 DLSS, 21 device, 20 program,
  13 dirty tile map, 3 multisample state, 1 texture, 1 sampler pool; `build/logs/nvn-missing-loaded.txt`), none called
  yet.
- Six application cores (`kernel.c` NCORE, its CreateThread check, `svc.c` GetInfo 0 mask 0x3f): the game puts a thread
  on core 5.
- SDK 22's `clock_gettime`: nnmusl dispatches it through hooks (`sdk+0xaf0f8` installs them) pointing at
  `nn::time::GetStandardSteadyClockCurrentTimespec` and the two user-clock getters, which read the time service's
  shared-memory client the overridden `nn::time::Initialize` never sets up and fail with 0x74; libc++'s
  `steady_clock::now` then throws a `system_error` on a worker thread. Overridden in `ov_system.c` from the host clocks.
- Offline and without add-on content (`ov_oot.c`): `nn::aoc::CountAddOnContent`, `nn::nifm`, `nn::socket`, `nn::ssl`,
  the network service account calls, `nn::pctl::CheckAgcPermissionUnderMinimumAge`, `nn::prepo::PlayReport::Send`;
  `nn::vi::QueryDisplayCurrentRefreshRate` (60 Hz).
- The crash dump prints the memory behind each register (`OOT_CRASH_PEEK=<bytes>` widens it); `OOT_BREAK_AT`,
  `OOT_BREAK_BRIEF` and `OOT_BREAK_WATCH` from MP4 work and were the tools that got through the analysis below.

**Resolved (2026-11-05): the shader archive table was never allocated** because the Graphics module's 841 MiB heap
could not be created: the game sizes its arena from `TotalMemorySize` minus the HeapDef budgets (VirtualAddressMemory
2.7 GiB, ...), and the Switch 1 pool left 387 MiB. The 12 GB arrangement's application pool (9809 MiB, Lichii
`k_system_control.cpp`) is the default now (`memory.c`, with a 12 GiB heap cap), and the arena is 6.75 GiB. Found with
`OOT_BREAK_AT` on the heap-size lookup (`main+0x5a2630`), the allocator (`main+0x3ea70`) and the heap creation
(`main+0x9bf710`), the TLS dump the crash handler now prints, and `OOT_BREAK_WATCH` on the HeapMgr.

**Then, to a 15 s run without a crash (2026-11-05):** the NVN2 program path (`nvnProgramBuilderSetShaders`,
`nvnProgramInitializeFromBuilder`; 3,661 programs set), logging stubs for every other NVN2 proc the game loads
(`runtime/nvn_procs_stub.inc`; a stub logs its first calls and returns 0, and the log shows which are used: so far only
`nvnCommandBufferSetRecordingMode`, `SetComputeWorkloadPriority`, `SetComputeOverlapMode`), SDK 22's audio services
(`aud:u` hands out the renderer manager, `auddmg:u` lists no virtual devices, IAudioDevice 15 to 21), the public
six-axis and vibration forms and the Switch 2 `Jcms`/`Nmouse`/sensor fusion stubs, the `nnsocket`/`nnssl` C APIs
offline with working local descriptors (gRPC's event engine needs a wakeup fd even offline), `nn::nsd` ("lp1"),
`MountRom(const char *)` and several mount names for the one image (`_npln`), `WaitForAddress` type 3
(`WaitIfEqual64`) and `SwitchToFiber(nullptr)` for SDK 22's libc++ and `nn::async`. The run: 82 threads, 10,988 IPC
messages, 1,485 file opens, the audio renderer at 48 kHz (12,002 renders in 60 s, none late), three frames presented at
2560x1440, then a stall in the third frame.

**Open: shader translation.** Every program fails with "bad control section" (`gfx_shader.c`): the SM86 control block is
224 to 280 bytes against Maxwell's 0x880, and the code is Ampere. Draws with failed programs are skipped, and the game
waits for GPU work that never completes. This is Phase 3: an SM86 front end (see "NVN2 survey" and `docs/phase3.md`, the
hand-off with the measured control-section fields and the order of work).

Also met: the C++ unwinder's `nn::ro::detail::FindModuleHeader` reads past `subsdk0`'s image into the unmapped gap
before `sdk` (`0x70050e0000`) when an exception is thrown; `QueryMemory` should report that gap as free so the walk
skips it. Not fixed yet, as nothing throws any more.


## Phase 3: SM86 shaders

Started 2026-11-05 from `docs/phase3.md` (the hand-off, which carries the measurements and the status). In short:
the translator built; `tools/shader_census.py` reads the NVN2 archives (14,363 programs, the opcode histogram as the
work list); `runtime/sm86_lower.c` lowers SM86 to Maxwell in front of the unchanged Ryujinx translator (93.6% of the
corpus, every lowered program translates); the host reads the NVN2 control section, lowers before translating and
binds the lowered jump tables as constant bank 1. The game presents its first frames through translated shaders.

Found on the way, outside the shaders: during loading the frames came 10 to 50 s apart with every thread idle in a
wait (the sampling profiler, `OOT_PROFILE`, `tools/prof_report.py`). The thread dump (`OOT_THREAD_DUMP=s25,s50`)
showed the main thread, pinned to core 2, waiting for that core, held by the NPLN event-engine thread sleeping inside
the host's `nnsocketPoll` override (`ov_oot.c`): a blocking host wait that did not give its virtual core up, as the
NVN waits do through `kern_core_away`. Fixed there.

The boot logo renders at 60 fps; then the title screen, which is a looping AV1 cinematic (1920x1080, 60 fps, 321
samples per loop) played through SDK 22's `movie::VideoDecoder`, an API none of the earlier ports had: its
`Initialize` opens the Switch 2's codec services and aborts in the SDK. `runtime/ov_movie22.c` replaces the decoder
classes with a null decoder that carries the game's protocol (events, registered frames, end-of-stream buffers), and
the game runs through the title loop with a black picture. Found on the way: NVN2 device property `0x67`, unknown
to the earlier ports, is a capability the game keeps as a flag and sizes its video surfaces by; answering 0 made the
player abort in its surface setup, 1 (unverified) lets it through. Also seen: about one run in three dies right after
the first frame with an access violation on a host thread at an address in no module (exit code 40 or 5, no FATAL
line); `main.c`'s crash handler now dumps that thread's registers and stack for the next occurrence. The next day the
cinematic decodes on the host (dav1d, NV12 frames back to the game), and the picture is still black: the game's
video path, like its rendering at 2560x1440, runs a neural upscaler on the tensor cores (HMMA, LDSM, MOVM in 19
compute programs that the shader archives do not hold), which no Maxwell translator has. The lowering emulates them
(shuffles and half-precision FMAs), and by the night of 2026-11-06 the cinematic plays, upscaled through that
emulation (`docs/phase3.md`, Status 2026-11-06 night: two translator defects, two lowering defects, the sRGB storage
image and the movie worker's pacing stood in the way). Open: the frame rate in the cinematic (21 fps), the start-up
crash that the crash dump traces to an SDK abort in MultiWait, and what lies past the cinematic.


## Shared core

The copy of 2026-11-05, from metroid-prime-4-recomp @ `586e91e`: 606 tracked files, 119 changed only by the rename of
`mp4` to `oot` (`MP4_` to `OOT_`, the window title and installer text naming the game), the rest unchanged; each
file's first line says which. Not taken: `recomp/patches-v131072.txt`, `tools/data/expected-v1.0.0.json`,
`tools/data/expected-v1.1.0.json`. Unrenamed on purpose: `tools/vendor/` and `tools/xlate/patches/`, whose "MP4"
comments record where a change was made.

## 2026-11-07: gameplay past the opening

The longjmp resume chain (`recomp/gen.py`, `Cpu.resumed`, the `longjmp`/`_longjmp` observer) got the game past
`main+0x166215c` and into the Kokiri Forest on the user's save; the SM86 lowering batch (TLD offsets, indexed IPA,
SURED, LEA) lifted 156 of the 193 failing programs; the reference's bindless pool set with GPU sampling feedback
(`d84bf5d`) replaced the per-draw pool scan that cost about 100 ms a frame. The level draws (3D with
`OOT_SKIP_CS=1a62084a`), the HUD and the minimap render, and the frame is now GPU-bound (2 fps at 2560x1440) with the
fog NaN and overexposed materials open. Details in `docs/phase3.md`.

## 2026-11-07, night: pointer tables and the white flash

Translator patches 0019/0020 bind the storage-buffer pointer tables OoT's object records live behind (the
emulator's "loaded pointer" case): the pass emits the access as an offset from the loaded pointer and the host
resolves the pointer when it binds the buffer. 23 level programs use it and their zero reads drop from 253 to 184.
A new white-frame trigger in the frame dump (`OOT_DUMP_WHEN=white:`) caught the flash and shows it is overexposure:
the 1080p-to-1440p upscaler writes 5568 against 2.18 in a clean frame, from an exposure/scale that swings by 10^6
between frames while the kernels and buffers stay the same. Details in `docs/phase3.md`.
