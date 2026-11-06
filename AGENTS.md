# Ocarina of Time PC Port

Working repository for the static recompilation of *The Legend of Zelda: Ocarina of Time* (title id
`04004860224DEC00`, a Switch 2 title) into a native Windows program. It started from the upstream reference
`..\ocarina-of-time-recomp`; that directory is read-only and may be ahead of this copy. `..\Lichii` is the GPL-3.0
Switch 2 emulator reference. All edits, builds, generated files and commits belong here.

## Read first

- `docs/phase3.md`: the current status, including the dated sections. The title cinematic now renders through dav1d
  and the game's tensor-core upscaler; the remaining work is frame rate, a start-up crash and gameplay past it.
- `docs/plan.md`: phased history and the measured facts. Trust it over `CLAUDE.md` when they disagree.
- `README.md`: public status. `CLAUDE.md`: longer session guide, partly stale.

## Reference vs working tree

- `..\ocarina-of-time-recomp` is the read-only upstream reference. It may contain newer Phase 3 work; sync tracked
  files from it instead of reimplementing, but keep this `AGENTS.md`, `config.local.toml`, `build/`, `generated/`,
  `refs/` and `toolchain/` local.
- `refs/` (created by `tools/fetch_refs.sh`) holds pinned upstream recomp ports. Read-only.
- `..\Lichii` (GPL-3.0) is the Switch 2 emulator reference: `NX2_NOTES.md`, the Ampere frontend and
  `lower_to_maxwell.h`. `..\Lichii-Frizti-v2` is the working Windows build (`lichii.exe`) used to produce a save.
  Read behaviour only, never copy code.
- `..\Eglantine`, `..\Eden-NS2`, `..\compound`, `..\pound` and `..\Instaurix` are further emulator/recomp references.
  Treat them like Lichii: behaviour and encoding facts only; GPL code is never copied.

## Non-negotiable

- Guest code is retail logic. Lift it; do not rewrite it. Generated C under `generated/` is never edited by hand:
  change `recomp/` and regenerate.
- No HLE shortcut inside guest code. Host overrides at OS/NVN/service boundaries are acceptable only when they
  reproduce observed retail behaviour; trace the game to infer semantics instead of guessing.
- Code patches to lifted code go through `recomp/patches.py` (switchable, checked against the original word), never
  ad-hoc edits. Keep them minimal and documented; a patch is not a way to bypass an unsupported instruction form.
- Optimizations must preserve observable behaviour; verify with `tests/diff/` and a game run.
- The target is Windows x86-64-v3 (AVX2/FMA/BMI2), Vulkan 1.3. Build and run are Windows-only; the lifter, extractor
  and differential tests are cross-platform.

## Repository map

- `recomp/`: Python ARM64 lifter and C generator. `gen.py` is the entry point; `lifter.py`, `simd_c.py`, `layout.py`,
  `nso.py`, `patches.py`, `jumptables.py` hold the pieces.
- `runtime/`: C host linked with the generated C. `main.c` has the modes, logging and crash reporter; `boot.c`/`loader.c`
  load the modules; `kernel.c`/`svc.c`/`memory.c` are the OS; `ipc.c` and `ov_*.c` are services and game-specific
  overrides; `ov_movie22.c` is the dav1d AV1 movie decoder; `nvn_*.c` is the NVN2 front end; `vk_*.c` is the Vulkan
  back end; `gfx_shader.c` is the shader path; `sm86_lower.c` parses the NVN2 control section and lowers SM86 to
  Maxwell, including the tensor-core instructions.
- `tools/`: extraction (`extract_nup.py`), build (`build_game.py`), translator (`build_xlate.py`), NVN2 shader corpus
  (`shader_census.py`), single-program disassembly (`sm86_dis.py`), offline lowering driver
  (`sm86_lower_main.c`), profiling (`prof_report.py`), release and survey tools.
- `installer/`: public-release installer. Still targets Switch 1 1.1.0; not a source of truth for this port.
- `tests/`: standalone scripts, not pytest.
- `generated/`, `build/`, `toolchain/`, `refs/`: gitignored. Generated C/build output, local toolchain and read-only
  upstream checkouts. A fresh copy has none of them.

## Bootstrap

- `config.local.toml` is ignored. Copy `config.example.toml` and set `[input] base` to the `.nup`. The current NUP is
  unencrypted, so extraction needs no keys; do not ask for keys unless a future XCI/NSP path needs them. Never print,
  copy or commit key values. If the dump is missing, ask the user for its path.
- Python is split on purpose: `.venv` 3.12 for the lifter and tests (`tools/requirements.txt`), `.venv-py314` 3.14+ for
  extraction and the shader census (stdlib `compression.zstd`), `.venv-win` for the Windows build and run. Extraction
  fails on Python < 3.14.
- Windows build needs MSYS2 UCRT64 clang/lld/ninja at `C:\msys64\ucrt64\bin` (hardcoded in `tools/build_game.py`), plus
  MSYS2's Vulkan, SDL3, Opus, libvpx, VMA, **dav1d** (`mingw-w64-ucrt-x86_64-dav1d`, linked statically) and OpenH264
  headers (`mingw-w64-ucrt-x86_64-openh264`). `build_xlate.py` needs the .NET 10 SDK, MSVC and `vswhere.exe`; a
  per-user SDK in `toolchain/dotnet` works, prepend it to `PATH`. `build_zbic.py` needs MSYS2 gcc; `build_shaders.py`
  needs `glslc` and `spirv-val`; `fetch_refs.sh` needs git inside MSYS2 or the Windows git directory on `PATH`.
- Commands below assume the matching venv is active. One Python with all requirements also works on Windows.

## Build and run

```sh
python tools/build_zbic.py                                  # toolchain/zbic/nso_unzbic.exe, once
python tools/extract_nup.py --verify                        # build/extracted/v131072/{exefs,romfs,...}
python recomp/gen.py --exefs build/extracted/v131072/exefs --out generated/v131072 \
    --modules main,subsdk0,sdk                              # ~1.2 GB of C, ~2 min
python tools/build_xlate.py                                 # build/xlate/oot_xlate.exe, 15 patches
python tools/build_game.py                                  # build/v131072/oot.exe, incremental Ninja
build/v131072/oot.exe --selftest                            # no game files
build/v131072/oot.exe --boot                                # game files needed
build/v131072/oot.exe                                      # title screen/cinematic; Ctrl+C or a save for gameplay
```

- `build_game.py` defaults to `v131072` (display version 1.1.1) and bakes `build/extracted/v131072` as the default
  ExeFS/RomFS. `--modules subsdk0` builds one module, `--out DIR` forks a build (reusing the main build's generated
  objects by hardlink), `--make-runtime-lib` builds the library the installer links against.
- After editing `runtime/shaders/*.comp`, run `python tools/build_shaders.py` (or `--check`). `build_game.py` refuses to
  build when a committed SPIR-V header no longer matches its source.
- The shader translator is separate: `python tools/build_xlate.py` -> `build/xlate/oot_xlate.exe`. `build_game.py` does
  not build it, and shader translation fails without it. Rebuild it after changing `tools/xlate/` or its patches.
- For reproducible runs: `python tools/run_game.py build/v131072 --seconds 15 --tag idle --env OOT_TRACE=gfx`. It copies
  `build/profile` to a scratch profile and never writes the original; it needs `build/profile` and a save to exist.

## Phase 3: shader corpus and lowering

The reference has been through the SM86 lowering work; this copy carries its current implementation. `docs/phase3.md`
is the authoritative status and measurement log.

- `runtime/sm86_lower.c` is the SM86-to-Maxwell lowering and the NVN2 control-section parser
  (`sm86_control_parse`). It covers 13,514 of 14,363 corpus programs (94.1%) and every lowered program translates.
  Tensor-core instructions (HMMA, LDSM, MOVM), SUST, LEA/IMAD.X carries and the uniform-register/predicate analysis
  are already implemented. Do not re-port them; extend the existing file.
- Control fields are pinned: size `+0xd4` (BNSH repeats it at `+0x30`/`+0x98`; agl leaves those 0), instruction end
  `+0x38`, c1 length `+0x3c`, c1 offset `+0x40`, code size `+0x44`, stage `+0x56` (NVN enum + 6 task/7 mesh), compute
  workgroup `+0x58`. Graphics code starts with the 0x80-byte Turing+ header; its first 0x50 bytes are the Maxwell SPH
  the translator reads. The caching keys are `KEY_VERSION` 5, `IN_VERSION` 2, `PRG_VERSION` 2; bump on layout changes.
- Corpus command (`.venv-py314`): `python tools/shader_census.py --version v131072 --lower`. It reads the extracted
  RomFS, writes `build/v131072/shaders/{programs.json,opcodes.txt,lowered/,lowered.txt}` and builds the offline
  lowering driver with `cc` (`tools/sm86_lower_main.c`). On Windows put MSYS2 gcc on `PATH`; `--translate` is written
  for the WSL interop path and may need adaptation when run directly on Windows.
- Inspect one program with `python tools/sm86_dis.py <key>`; failed host translations dump
  `shader-cache/<key>.fail.{ctl,code}`.
- Remaining lowering gaps: 656 vertex shaders (skinning, up to 247 live registers), the surface stores (`SUST`, 86
  compute programs), `ISBERD`/`ISBEWR` (task and mesh), a few rare forms in `lowered.txt`.

## Tests

There is no pytest, lint or typecheck config. Run the standalone scripts with the Python that has
`tools/requirements.txt`; the differential tests compile `tests/diff/harness.c` with MSYS2 clang (or `$CC`).

- No game files: `python tests/installer_test.py`, `python tests/flags_check.py`, `python tests/excl_mt.py`,
  `python tests/diff/atomic.py`, `python tests/diff/simd.py`.
- Need the extracted ExeFS: `python tests/diff/lift.py --exefs build/extracted/v131072/exefs`,
  `python tests/diff/func.py --exefs build/extracted/v131072/exefs`,
  `python tests/jumptables_check.py --exefs build/extracted/v131072/exefs`.
- `func.py` and `jumptables_check.py` still default to `v121`; always pass `--exefs build/extracted/v131072/exefs`.
  `lift.py` defaults to `v131072` but accepts the same flag.
- Focus a form: `lift.py --only VAL/MASK --model max` (max covers LDAPR, UDOT/SDOT and FP16), `simd.py --filter
  VAL/MASK`, `func.py --functions N --cases K`. `lift.py --coverage` lifts every distinct word instead of sampling.

## Running and debugging

- `oot.exe` reads `oot.ini` next to it; every key has an `OOT_*` environment override. `--exefs`, `--romfs` and
  `--profile` override the baked defaults.
- Trace: `OOT_TRACE=svc,host,ipc,fs,gfx,perf,sched,audio,present,all`. Breakpoints: `OOT_BREAK_AT=main+0x...`,
  `OOT_BREAK_BRIEF=1`, `OOT_BREAK_WATCH=...`, `OOT_BREAK_FOLLOW=...`, `OOT_BREAK_COUNT=1`; crash dump:
  `OOT_CRASH_PEEK=<bytes>`. Back end: `OOT_GPU=null|vulkan`.
- Shader/movie diagnostics: `OOT_PROG_DUMP=1` (compute control sections), `OOT_BIND_LOG=1` (compute binds/pushes/
  dispatches), `OOT_DUMP=dispatch` (frame dumps), `OOT_SKIP_DISPATCH_FROM`, `OOT_SM86_STUB=hmma|ldsm|all` (timing),
  `--probe-tensor` and `--probe-cs <dir>` (one kernel in isolation).
- Profiling: `OOT_PROFILE=...` with `python tools/prof_report.py`; `OOT_THREAD_DUMP=s25,s50` for thread states.
- `--seconds` counts world time only after a save loads; an empty `build/profile` reaches the title screen and then
  the cinematic. Put a save under `build/profile/save/<id>` or use `run_game.py --save` for gameplay runs.
- When launching from WSL, every `OOT_*` variable must be named in `WSLENV` or it is silently dropped. On Windows this
  is not needed.
- Logs: `run_game.py` writes `<exe dir>/logs/<tag>.log`; a direct run writes `logs/oot.log` next to the exe, and
  `build/logs/` holds the session logs.

## Current state and blockers

- The game boots, presents frames and plays the AV1 title cinematic: dav1d decodes it, the game's neural upscaler runs
  through the emulated tensor-core instructions, and the result is presented at 2560x1440. The cinematic runs at about
  21 fps; the frame rate before it is 60 fps.
- Open: the cinematic frame rate (dav1d threads, NV12 interleave, or the game waiting on frames); an intermittent
  start-up crash right after the first frame (an SDK abort in MultiWait; see `docs/phase3.md`, test with
  `OOT_ASYNC_PREEMPT=0`); and what lies past the cinematic (the file select/opening are cinematics too). A real save
  is needed for gameplay tests.

## Stale Switch 1 assumptions

- `docs/installer.md`, `installer/layout.py`, `tools/make_release.py` and `tools/prepare_shaders.py` still describe
  Switch 1 1.1.0 / XCI / NSP and reference `recomp/patches-v131072.txt` and `tools/data/expected-v1.1.0.json`, which
  this port deliberately did not take. Do not use them as truth for the .nup / 1.1.1 port.
- `runtime/frame_rate.c` refers to the missing `recomp/patches-v131072.txt`, so the frame-rate/realtime patch path is
  not wired. `CLAUDE.md`'s "still MP4's" list is partly stale: Phase 1 rewrote `boot.c`, `loader.c`,
  `build_game.py` and `ov_oot.c`. Trust current code and `docs/plan.md`.
- `runtime/main.c`'s memory selftest was still MP4's: it mapped 4 GiB to force `LimitReached`, but OoT's application pool
  is 9809 MiB, so that mapping succeeds. It now maps 16 GiB. When a probe fails only on a threshold, re-check the pool
  assumption before changing runtime semantics.

## Game content boundary

This repository is public. Game data lives outside it and every derived file stays in ignored folders.

- The user's game and key paths are in `config.local.toml` (ignored). Pass the keys file path to tools; the tools read
  it themselves. Key values stay out of your output, logs, commands and files.
- Everything extracted or generated goes under `build/` or `generated/`.
- Every file the code generator emits carries the marker defined as `GENERATED_MARKER` in `tools/check_content.py`.
- Run `python tools/check_content.py --staged` before proposing a commit. CI runs the same check.
- Enable the hook once per clone: `git config core.hooksPath .githooks`.

## Reusing code

- Copy only MIT, BSD, ISC, zlib or Apache-2.0 code, keep its header, and add a row to `THIRD_PARTY_NOTICES.md`.
- yuzu, suyu, Ryujinx and other GPL code: read for behaviour, write our own. Lichii is GPL-3.0: behaviour only.
- Code taken from metroid-prime-4-recomp, fe3h-pc-recomp or totk-pc-recomp is attributed like any other upstream,
  naming the commit it came from. When a piece is copied unchanged, note it so it can later move into a shared library.

## Code and writing

- Generated C is never edited by hand: change the generator.
- Comments say what the code does and why. Cite our code by file plus function, upstream code by project, file and
  function.
- Code, comments, docs and commit messages are in English. Commit messages are plain sentences without type prefixes.
- When the project state changes, update `docs/plan.md` (and `docs/phase3.md` if the shader blocker moved); do not leave
  a stale claim in place.

## Working style

- Work autonomously; use subagents for parallel investigation and large mechanical checks.
- Ask the user only for external inputs you cannot obtain (the dump, a save for gameplay, keys if a future path needs
  them, a missing reference checkout) or for decisions that change scope. Do not ask about facts the repository
  already answers.
- Keep new files out of the repository root; put probe and generated artifacts under `build/` or `generated/`.
- Before reporting success, run the relevant smoke test/probe and name the exact command and log path.
