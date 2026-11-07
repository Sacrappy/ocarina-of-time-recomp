# Offline (ahead-of-time) shader translation

Status: implemented by `tools/prepare_all_shaders.py`, verified against the runtime's own cache and the recorded
variant set (see "Verification performed"). This note describes the runtime path as it is, the exact cache and record
formats, the offline design, the commands, and the limits.

The goal is to translate every shader variant the game needs to SPIR-V **before play**, into the runtime's own shader
cache, so `oot.exe` loads the modules from disk and never starts `oot_xlate.exe`. The path is reproducible from the
user's extracted game files: the script reads `build/extracted/<version>/romfs`, writes only under `build/` (or a
caller-given directory outside the repository), and no shader bytes are committed.

## The runtime path today

All paths below are relative to `runtime/` and refer to `gfx_shader.c` unless stated otherwise.

### Entry points and lookup

- `gfx_shader_get(stage, control, code, code_size, c1, answers)` is called by the NVN front end / replay when a draw or
  dispatch needs a program. `gfx_shader_get_async` / `gfx_shader_await` are the same path with the translation queued
  to background workers.
- `req_init` validates the NVN2 control section with `sm86_control_parse` (`sm86_lower.c`), takes `r->length =
  ctl.code_size`, `r->c1 = code + ctl.c1_off`, and computes the **code hash** with `build_code_hash`: SHA-256 over
  `u32 stage`, `u32 ctl_size`, the control section, `u64 code_size`, and the code block. `ctl_size` is the clamped size,
  not necessarily the archive's `+0xd4` (see "the compute clamp" below).
- `use_find(code_hash)` looks up the **use mask** in memory, then in the preloaded table, then in
  `<cache>/<use name>.use` or the cache pack. The use name is `sha256("TKSU", translator identity, code hash)`.
  A hit makes the request *final*: the input id and cache key are computed from the answers masked by the use mask.
  A miss makes the key **provisional**: `sha256("TKSP", KEY_VERSION, identity, code hash, full answers)`.
- `get_one` / `get_async_one` first look in the in-memory cache, then `disk_load(key)`:
  `<cache>/<key>.spv` and `<cache>/<key>.res.json` (the pack first, then the loose files). `disk_load_from` checks the
  reflection (`reflection_valid`), the SPIR-V magic and the instruction chain (`spirv_complete`), parses the reflection
  (`reflect`), applies the pool-array moves (`move_pool_arrays`), and scans interface Locations
  (`reflect_spirv`). A valid pair is a cache hit and no translator is started.
- On a miss, `translate_one` lowers the program with `sm86_lower_ex`, writes `<key>.code`, sends the JSON request, and
  the translator writes `<key>.spv` and `<key>.res.json` into the cache directory. Then the use mask is derived from
  the new reflection (`use_of`), the final input id and key are computed, the files are renamed to the final key, and
  `<use name>.use` is written. Finally `in_store` writes `<cache>/in/<input id>.in`.
- `gfx_shader_load_input(input id, xlate)` computes `key_of_input(iid)` and loads the same way, falling back to
  `in_load` + `gfx_shader_get` when the key is not cached. `gfx_shader_load_private` is the disk-only variant used by
  the stage-library prebuild. `gfx_shader_prewarm` loads a list of input ids on several threads and translates the
  stored inputs that are not cached; `gfx_shader_warm_list` builds that list from `shader-answers.bin` (priority 0) and
  `shader-prepared.bin` (priority 1+).

### The three hashes

```
code_hash = sha256( u32 stage | u32 ctl_size | control[0:ctl_size]
                    | u64 code_size | code[0:code_size] )

use mask  = (slots, attrs, fouts)
  slots  = bits of res.json "tex_queried" and "img_queried"
  attrs  = Input Locations declared by a vertex shader's SPIR-V
  fouts  = Output Locations declared by a fragment shader's SPIR-V (low 8 bits)

input id  = sha256( "TKSI" | u32 IN_VERSION | code_hash | pack_answers(mask_answers(answers, mask)) )
cache key = sha256( "TKSK" | u32 KEY_VERSION | translator identity | input id )
use name  = sha256( "TKSU" | translator identity | code_hash )
```

`pack_answers` is the fixed 147-byte layout (below); `mask_answers` zeroes everything outside the mask, including
`depth_minus_w`; `local` and `shared_bytes` are always kept. The input id therefore does not depend on the translator,
while the cache key and the use name do.

### Translator identity

`xlate_identity` reads `OOT_XLATE_ID` if set (64 hex digits, or any text hashed), else `<translator>.id` next to the
executable (written by `tools/build_xlate.py`), else the SHA-256 of the executable. `gfx_shader_set_host` then mixes
the device answers in when they are not the defaults (Float64, exact storage offsets, 32 lanes):

```
identity = sha256(identity | "TKHO f64=%d sbuf=%u sg=%u,%u,%u")
```

and `gfx_shader_set_tensor` mixes in `"TKTC tensor=1"` when HMMA runs on cooperative matrices (`vk_caps.c`). Both
mixes happen before the first translation. A translator rebuild changes every key without a `KEY_VERSION` bump;
the host options and the tensor choice give a device its own keys.

### The three record files next to `pipeline-keys.bin`

- `shader-answers.bin` (`"TOTKSANS"`, version 1, record 216): `stage u32`, code hash, input id, 147-byte packed
  answers, one pad byte (seed priority; 0 when recorded). One record per input id. `gfx_shader_warm_list` uses it.
- `shader-programs.bin` (`"TOTKSPRG"`, version 2): `stage u32`, code length, control size, code hash, control, code.
  One record per code hash. `gfx_shader_prepare_recorded` uses it to translate answers without guest memory. The
  control-size field is `r->ctl.size` (clamped), and the code includes the c1 constants.
- `shader-prepared.bin` (`"TOTKSPRP"`, version 1, record 36): `stage u8`, priority `u8`, two zero bytes, input id.
  Written by `oot.exe --prepare-shaders`; `--warm-shaders` reads it.
- `pipeline-keys.bin` (`"TOTKPKEY"`) holds `VkPipeState` records with the shaders named by input id; it is game/pipeline
  state, not a shader translation, and is not written by this script (see "Limits").

## Cache formats in full

All integers are little-endian.

### `<cache>/<key>.spv`

The translator's SPIR-V module, verbatim. The runtime validates the magic, the word count, and the instruction chain,
then applies `move_pool_arrays` in memory; the on-disk module must **not** be pre-processed.

### `<cache>/<key>.res.json`

The translator's reflection, verbatim. The runtime parses `stage` (`vs`/`ps`/`cs`/`ts`/`ms`), `cbufs`, `sbufs`,
`textures`, `images`, `tex_queried`, `img_queried`, and `c1` (the lowered constant bank 1 as hex). `reflection_valid`
requires one JSON object, no NUL, and a known stage. The use mask comes from `tex_queried`/`img_queried` and the
SPIR-V Locations; nothing else in the file affects the key.

### `<cache>/<use name>.use` (20 bytes)

```
u32 USE_MAGIC ("TKUM" = 0x4d554b54)
u32 USE_VERSION (2)
u32 slots       texture/image slots the translation queried
u32 attrs       vertex input Locations
u32 fouts       fragment output Locations (low 8)
```

Per code hash **and translator identity**. Without it the runtime cannot compute the final key, translates with the
provisional key, and renames the output; writing it is the single most important part of the offline path.

### `<cache>/in/<input id>.in`

```
u32 IN_MAGIC ("TKSI" = 0x49534b54)
u32 IN_VERSION (2)
u32 stage          GFX_STAGE_* (0 vs, 4 fs, 5 cs, 6 ts, 7 ms)
u32 length         code block bytes
u32 ctl_size       control bytes actually hashed, 0xe0..0x400
u32 answers_size   sizeof(GfxShaderAnswers) = 152 on x86-64
GfxShaderAnswers   152 bytes (see below)
control            ctl_size bytes
code               length bytes
```

The runtime's `in_load` requires the total size to be exactly `24 + answers_size + ctl_size + length`, so the two
answer layouts below must not be confused. `in_store` writes the **struct** layout, not the packed one.

### `GfxShaderAnswers`: two layouts

The C struct (`in` files, 152 bytes) has an alignment hole; the hash record (`pack_answers`, 147 bytes) does not:

| field | struct offset | packed offset | bytes |
|---|---:|---:|---:|
| `tex_type[32]` | 0 | 0 | 32 |
| `img_type[32]` | 32 | 32 | 32 |
| `img_format[32]` | 64 | 64 | 32 |
| `attr_type[32]` | 96 | 96 | 32 |
| `frag_out_type[8]` | 128 | 128 | 8 |
| `local[3]` | 136 | 136 | 6 |
| padding | 142 | — | 2 |
| `shared_bytes` | 144 | 142 | 4 |
| `depth_minus_w` | 148 | 146 | 1 |
| padding/end | 149 | 147 | 3 |

The packed layout is what `sha256` sees and what `shader-answers.bin` stores. `mask_answers` writes the packed layout
and leaves `depth_minus_w` zero.

### `shader-cache.pack` (read, not written)

`"TKSPACK1"`, `u32 count`, `u32 0`, then `count` 48-byte entries `{ name[32], u32 kind, u32 size, u64 offset }` sorted
by `(kind, name)`, then the data. Kinds: 1 `.spv`, 2 `.res.json`, 3 `.use`, 4 `.in`. The runtime reads the pack first
and falls back to the loose files; the script reads it the same way for its cache checks.

## The offline design (zero runtime changes)

The runtime already implements exactly the lookup the offline path needs. To make a variant load without a translator,
it is enough to create:

1. `<cache>/<use name>.use` for its code hash and the current identity, with the mask its translation produced;
2. `<cache>/<key>.spv` and `<cache>/<key>.res.json` under the key that mask gives;
3. `<cache>/in/<input id>.in` so `gfx_shader_load_input`, `gfx_shader_load_private` and the warm prewarm can find it;
4. optionally `shader-answers.bin` and `shader-prepared.bin`, so `gfx_shader_warm_list` hands the input ids to the boot
   prewarm.

No runtime code changes are needed for this. The script mirrors the runtime's key math, builds the same jobs, and
writes those files. On the next boot `req_init` finds the mask, computes the final key, `disk_load` finds the pair, and
`translate` is never called for that variant. A `--verify-only` run checks the same files the runtime will check.

The script's pipeline:

1. **Discover programs.** `tools/shader_census.py inventory()` walks the RomFS's BFSHA/BNSH/SARC/Zstd shader
   archives and returns every unique NVN2 program: control section, code block, NVN stage. The runtime code hash is
   recomputed from those bytes with the same clamp as `sm86_control_parse`.
2. **Discover variants.** The recorded `shader-answers.bin` (and any `--answers` files, including seed/list files)
   give `(stage, code hash, full answers)`. Optionally, one **default-answer** variant of every program is added:
   zeros, except a compute program's workgroup size from the control section and `shared_bytes = 0xc000` (the value
   `vk_exec_draw.c answers_for` passes every kernel). The default-answer variant is the one the runtime asks for a
   program whose answers are all zero; it is not a universal translation (see "Variant coverage").
3. **Plan.** For each variant, if `<use name>.use` exists and its key's `.spv`/`.res.json` pair is valid, the variant
   is cached and skipped. Everything else is queued.
4. **Lower.** Only the queued programs are lowered, in one batch, through `tools/sm86_lower_main.c` and
   `runtime/sm86_lower.c`. The helper is compiled once per source change and cached under the work directory; the
   tensor-core choice is passed with `SM86_TENSOR` and part of the lowering cache stamp.
5. **Translate.** The script writes the same JSON jobs `build_request` builds (`key`, `stage`, `debug`, `code`, `c1`,
   `tex`, `img`, `attr`, `fout`, `shared`, `local` for compute, `local`/`maxv`/`maxp`/`payload`/`omap` for task/mesh,
   `softfloat64`/`sbuf_align`/`subgroup` for the host answers) and runs `oot_xlate.exe` in batch mode on several
   processes.
6. **Validate.** The module must be structurally complete and pass `spirv-val` (Vulkan SDK, PATH, or `--spirv-val`)
   **before** it enters the cache. A module the runtime's light checks would accept but the driver would reject must
   not be cached.
7. **Key and write.** The use mask is derived from the new reflection exactly as `use_of` does; the input id and final
   key are computed; `.spv`, `.res.json`, `.use`, and `.in` are written in that order. A recorded input id that differs
   is reported (the new mask is the one the runtime will use).
8. **Record.** The cached variants are merged by input id into `shader-answers.bin` and `shader-prepared.bin`;
   existing records are kept. `--write-programs` optionally merges `shader-programs.bin` (large).

Everything is idempotent: a second run finds the `.use` files, recomputes the keys, sees the pairs, and translates
nothing. The lowered programs, jobs, and translator output live under `<work>` (`build/<version>/shader-aot` by
default), never in the cache directory; `<work>/manifest.json` records the identity, the host options, the counts, and
each cached variant's stage, code hash, input id, key, mask, and source for comparing runs.

## Variant coverage: what "every shader" means

A program alone does not determine its translation. The translator reads the answers: the sampler type and integer
flag of every queried texture slot, the format of every image, the vertex attribute types, the fragment output types,
the compute workgroup size, and the shared-memory size. The runtime masks the answers to the slots the program actually
uses, so a program with no queried slots has one translation, while a program that samples 2D, cube, and integer
textures has one per combination it meets.

Those answers come from the game's draw state (texture objects, pipeline formats), not from the shader archive. They
cannot be enumerated from the RomFS alone. The reproducible sources are:

- **Recorded variants** (`shader-answers.bin`, or a seed list made from one) — exactly the `(program, answers)` pairs
  play asked for. This is the set that makes the first-run cache complete for a known playthrough; it is also what
  `tools/prepare_shaders.py` ships as a release list. The script translates these first and validates each computed
  input id against the recorded one.
- **Default-answer variants** (the `--baseline` pass, on by default) — one per program, with zero answers. This covers
  vertex shaders whose attributes are all float, fragment shaders with no texture/image queries and float outputs, and
  compute programs (whose local size and shared size are known from the control section and the runtime constant).
  A program that queries a texture or has an integer attribute/output will miss this variant at play and be translated
  on demand, exactly as today.

Shipping a complete offline cache for an arbitrary machine therefore needs a variant list from play. That list is
game-derived data: it stays in the ignored build directory (or a release data file prepared from it), never in the
tracked tree.

## Reproducibility and identity

The script's identity must equal the running game's identity:

```
base      = OOT_XLATE_ID (64 hex) | <translator>.id | sha256(translator file)
identity  = sha256(base | "TKHO f64=... sbuf=... sg=..." if not defaults)
identity  = sha256(identity | "TKTC tensor=1") if tensor cores
```

`--log FILE` (or the default newest log under `<build>/logs` and `<build>/<version>/logs`) reads the runtime's own
`gfx_shader: host options ...` line and `HMMA runs on cooperative matrices` line, so the device-specific options come
from a real run rather than guesses. The log prints only the first 8 hex digits of the base identity; the script
compares them and warns if the translator was rebuilt since the log. Explicit `--sbuf-align`, `--no-float64`,
`--subgroup`, and `--tensor` override the log; `--xlate-id` overrides the base (the game must then run with the same
`OOT_XLATE_ID`).

A translator rebuild changes the base and therefore every key: rerun the script (it will translate again because the
`.use` names are new). Do not rebuild the translator while the script runs.

## Running it

Prerequisites: the extracted RomFS (`python tools/extract_nup.py --verify`), a built translator
(`python tools/build_xlate.py`), and Python 3.14 (`.venv-py314`) for `compression.zstd`. MSYS2 clang is needed to build
the lowering helper (the same requirement as `tools/shader_census.py --lower`); `spirv-val` from the Vulkan SDK is
used when found.

From the repository root (Windows paths; use `python3.14` on Linux/WSL):

```sh
# the recorded variants + one default-answer variant of every program, into build/v131072/shader-cache
.venv-py314\Scripts\python.exe tools\prepare_all_shaders.py

# recorded variants only (the smallest useful cache)
.venv-py314\Scripts\python.exe tools\prepare_all_shaders.py --no-baseline

# a smoke test: 8 variants, no baseline, a scratch cache
.venv-py314\Scripts\python.exe tools\prepare_all_shaders.py --no-baseline --limit 8 \
    --out build\shader-aot\test-cache --records build\shader-aot\test-records --work build\shader-aot\test-work

# the identity the game uses, from its newest log
.venv-py314\Scripts\python.exe tools\prepare_all_shaders.py --print-identity

# check the key math against the cache's own masks, then plan without writing
.venv-py314\Scripts\python.exe tools\prepare_all_shaders.py --self-test --dry-run --no-baseline

# verify every recorded variant is loadable, with no translator
.venv-py314\Scripts\python.exe tools\prepare_all_shaders.py --verify-only

# a packaged build elsewhere (--romfs is the extracted RomFS, romfs.bin + index.json)
.venv-py314\Scripts\python.exe tools\prepare_all_shaders.py --build E:\oot --out E:\oot\shader-cache \
    --records E:\oot --xlate E:\oot\oot_xlate.exe --romfs E:\oot-extract\romfs --log E:\oot\logs\oot.log
```

Useful options: `--jobs` (translator processes), `--retries`, `--stage vs|ps|cs|ts|ms`, `--mesh off`,
`--validate-spv off|sample|all`, `--spirv-val PATH`, `--write-programs`, `--strict`, `--limit`, `--dry-run`.

Output directories default to `build/<version>/{shader-cache,shader-answers.bin,shader-prepared.bin,shader-aot}`. The
script refuses to write inside the repository outside `build/` unless `--allow-repo-output` is given, so game-derived
files cannot enter the tracked tree.

Disk: the recorded set (about 1,000 variants) is roughly 150 MB of SPIR-V plus 15 MB of inputs; the full default-answer
corpus is on the order of 2 GB of SPIR-V, because some compute programs are large. Everything is under `build/`.

After preparing, `oot.exe --warm-shaders` builds every cache entry's stage library into the driver's disk cache without
translating. `tools/pack_shader_cache.py` can pack the loose files; the runtime reads the pack first, so run the
preparation before packing (or use a fresh cache directory).

## Verification performed

The script was exercised against the live build on 2026-11-07, while the game was running:

- **`--self-test`**: 1,018 of 1,018 recorded input ids were reproduced from the cache's own 236 distinct use masks.
  This checks `pack_answers`, `mask_answers`, `build_code_hash`, and `key_of_input` against the runtime's output.
- **End-to-end over the recorded set** (1,018 variants; 2 have no program in the current RomFS): 1,015 were lowered
  and translated, 1,011 produced valid SPIR-V and were cached. **All 1,011 computed input ids equalled their recorded
  ids** (the script would note a difference). `spirv-val` accepted all 1,011. Four **task** programs were rejected by
  the translator built at that moment (it predated `patches/0021-mesh-and-task-shaders.patch`;
  `OpReturn` outside a block); one fragment program hits the existing lowering gap `TEX unpacked operands`. Neither
  is written to the cache, so the runtime will translate those on demand (as it does today).
- **Idempotent rerun**: the same command found 1,011 cached variants, translated 0, and only retried the 5 known
  failures.
- **First probe** (before the full run): one vertex, one fragment, and one compute variant were lowered and translated;
  all three computed input ids matched their recorded values and all three passed `spirv-val`.
- **Baseline paths**: 4 compute variants and 4 task/mesh variants were translated with default answers; compute
  `.in` files carry the control's local size and `shared_bytes = 0xc000`. Of the task/mesh variants, the one mesh
  program that reached a valid module passed `spirv-val`; the three task programs were rejected for the same missing
  translator patch (and were not cached).
- **Runtime key match**: a dry run against the live `build/v131072/shader-cache` found 6 recorded variants that the
  running game had translated under the same identity; the script computed their use names and keys from its own
  math and found the files. This is the zero-translation lookup as the runtime performs it.
- **Translator rebuild**: the other session rebuilt `oot_xlate.exe` with patch 0021 during this work (base identity
  `9ca42481...` -> `06200a36...`). Under the new binary, 3 task and 2 mesh default-answer variants translate and pass
  `spirv-val`, so the task/mesh job fields are right. The same rebuild exposed one new translator bug: a recorded
  compute variant (`ba8a9e4db963...`) now emits `OpCapability CooperativeMatrixKHR` without `VulkanMemoryModel`, which
  `spirv-val` rejects. The script refuses to cache it (the runtime's light checks would have accepted it), and
  `--verify-only` reports it when it is already in a cache; the running game had accepted that module. This is a
  translator output issue for the shader/xlate session, not a script issue. A rebuild also changes every key, so the
  recorded set must be prepared again (the script does that automatically).

Not verified here: a game run against a cache produced by the script. The task prohibited launching the game, so the
claim "no translator at play" rests on the exact file-layout equivalence, the matching identity, and the runtime code
path cited above, not on an executed run. A future session should confirm with `OOT_TRACE=gfx` that a run after
`tools/prepare_all_shaders.py --no-baseline` reports zero `gfx_shader: translation` requests for the recorded variants.

## Limits and what would need runtime support

- **The variant set is not derivable from the archive alone.** A complete first-run cache for every machine needs the
  `(program, answers)` pairs play meets. `shader-answers.bin` supplies them after one recorded run; a release seed list
  (`tools/prepare_shaders.py --make-list`) supplies them in a form that ships with a release. The default-answer pass
  is best-effort, not complete.
- **The identity is device-dependent.** `sbuf_align`, the subgroup sizes, Float64, and the tensor-core choice are the
  device's (`vk_caps.c`). The script reads them from the newest run log, or takes flags. A different GPU needs its own
  log/flags and therefore its own keys. A full 64-hex identity line in the runtime log, or a `--print-shaders-identity`
  runtime mode, would remove the 8-digit check and the log guesswork; that is a small runtime change.
- **No "fail instead of translate" mode.** There is no way to ask the runtime to make a cache miss fatal, so a
  coverage test can only inspect the logs. An `OOT_SHADER_NO_XLATE=1` switch (count a miss as a failure instead of
  starting the service) would let a run prove the cache is complete. This is the most useful small runtime change.
- **`pipeline-keys.bin` is not written.** Its `KeyRec` embeds `VkPipeState`, which only the game produces; generating
  it offline would require the full pipeline state. It is not needed for shader translation: the warm list
  (`shader-answers.bin` + `shader-prepared.bin`) drives the boot prewarm and `--warm-shaders`. Without key records the
  boot pipeline prebuild has nothing to rebuild, but shaders are still loaded without a translator when a draw needs
  them, and the stage-library prebuild still uses the prepared variants.
- **Task/mesh support is in flux.** The script mirrors the current `build_request` mesh fields (`local`, `maxv`,
  `maxp`, `payload`, `omap`) and the current `GFX_STAGE_TS/MS` numbers. The built translator must include patch 0021;
  until then those 12 programs are rejected (and reported), like the 4 task ones above.
- **The `.in` header changed without an `IN_VERSION` bump in this tree.** Files written by the older build have a
  28-byte header with the control size in a different field; the current `in_load` rejects them. The script writes the
  current 24-byte header and rewrites invalid `.in` files. If the runtime layout changes again, update `InHeader`,
  `ANSWERS_STRUCT`, and the validation in one place at the top of the script.
- **Cache pack interaction.** If `<cache>/shader-cache.pack` already holds an entry, the runtime uses the packed copy.
  The script reads the pack for its checks and writes loose files; run it before packing, or into a fresh directory, if
  the pack is to contain the new entries.
- **Concurrent game runs.** Do not run the script while the game is appending `shader-answers.bin` /
  `shader-programs.bin`: the script rewrites `shader-answers.bin` atomically from a snapshot, so records appended
  between its read and write are lost. Stop the game first. (The readers tolerate a torn tail in either file.)
- **Stale records.** An answers file can contain variants whose programs are not in the current RomFS (2 of the 1,018
  here). They are reported as "without a program" and skipped; the merge keeps them, so the warm list will name them
  once. A `--prune-records` option would drop them from the answers file.
- **The compute clamp.** `sm86_control_parse` replaces a control size below `0xe0` or above `CONTROL_BOUND` (`0x400`)
  with `0xe0`. Compute programs seen in play carry `+0xd4` values such as `0x768` and `0x1970`, so their code hash
  covers the first `0xe0` control bytes only. The script mirrors this; do not "fix" it to use the raw field without
  changing the runtime and `KEY_VERSION`.

## Open questions

- Which variants a fresh install actually meets on the title screen, file select, and the opening cinematics (the
  recording here came from gameplay), and whether the baseline pass covers the title cinematic's neural upscaler
  kernels without a recorded list.
- Whether the task/mesh programs need variants beyond the default answers (their answers are currently zero; the
  job's mesh fields come from the control section).
- Whether a future translator change should also invalidate `shader-prepared.bin` (it names input ids, which are
  translator-independent, so it survives; the stage libraries keyed to the old SPIR-V do not).

## Provenance

The NVN2 archive walk, BFSHA/BNSH/SARC parsing, and the `sm86_lower_main` batch driver are `tools/shader_census.py` and
`tools/sm86_lower_main.c` (MIT; from totk-pc-recomp / MacNativeGames' shader tooling). The key layout, the cache file
formats, the use masks, the stored inputs, and the record files are `runtime/gfx_shader.c`; the job protocol and the
reflection are `tools/xlate/Program.cs`. This note and `tools/prepare_all_shaders.py` are new for this port. No game
data is embedded in either.
