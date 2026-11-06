# Bindless textures (pool-indexed sampling)

Hand-off for the work that makes the Kokiri Forest materials sample their real textures. Written 2026-11-06, before
any code for it; update the Status section as it moves.

## Symptom

On the level route (`python3 tools/run.py --seconds 150 --replay build/logs/a-spam.txt --skip-movies --env
OOT_SKIP_CS=1a62084a --shot build/logs/level.png`) the scene reaches the screen, but most geometry is black or flat:
the materials sample the host's placeholder texture instead of their own. (`OOT_SKIP_CS=1a62084a` leaves out the
volumetric fog, which writes NaN; see `docs/phase3.md`.)

## What the shaders do

The materials do not take their textures from bound slots. They load a handle from a constant buffer and sample
through it: a handle is `(sampler id << 20) | texture id`. Those ids index the texture and sampler pools that the
game registers with `nvnTexturePoolInitialize` and `nvnSamplerPoolInitialize`.

Ryujinx 1.3.2 (the vendored translator) already deals with this. When it cannot resolve the handle to a bound slot,
`BindlessElimination.GenerateBindlessAccess` (Ryujinx.Graphics.Shader, Translation/Optimizations) rewrites the
sample into an indexed access to arrays that stand for the pools:

- texture index = `min(handle & 0xfffff, texturePoolLength - 1)`
- sampler index = `min(handle >> 20, samplerPoolLength - 1)`, into a separate sampler array

It does this only when the handle comes from a cbuf, an Input or a storage-buffer load (`IsBindlessAccessAllowed`),
and only when the host supports separate samplers (`QueryHostSupportsSeparateSampler`).

The pool lengths come from `QueryTextureArrayLengthFromPool` and `QuerySamplerArrayLengthFromPool`, with a minimum of
`BindlessToArray.MinimumArrayLength`. `tools/xlate/Program.cs` answers 32 for both, so every texture id above 31
is clamped to 31.

The rewritten resources are declared with handle `0x40000000`, which is
`TextureHandle.PackOffsets(0, 0, TextureHandleType.Direct)` (type 4 in bits 28-31), plus `cbuf_slot` 0 and
`"array": 32`. The reflection prints them as "slot -1 (c0+0)".

Measured in `build/v131072/shader-cache` (`*.res.json`): 249 programs have these arrays.

| what | `type` (SamplerType) | separate | programs |
|---|---|---|---|
| sampler array | 0 (None) | no | 249 |
| 2D texture array | 3 (Texture2D) | yes | 172 |
| 2D-array texture array | 259 (Texture2D, Array) | yes | 184 |
| 2D shadow texture array | 1027 (Texture2D, Shadow) | yes | 96 |

So a program has one sampler array plus one texture array per dimension it samples. There are no cube, 3D or
integer variants in the corpus yet; recount after more of the level is reached.

## What the host does today

`runtime/vk_exec_draw.c` `bind_resources` fills an array binding element by element. With `cbuf_slot == 0` and
slot -1 it calls `cbuf_handle`:

- In a graphics stage that returns 0, so the placeholder is bound.
- In a compute stage it reads NVN2 bank-0 words at offset 0 + 8k (`nvn2_c0_slot`), which is wrong for a
  pool array.

Either way, element k never becomes texture id k of the bound pool. `runtime/vk_pipeline.c` `add_stage` already gives
the layout binding the array's descriptor count, so the arrays exist in the layout. They are just filled wrongly, and
they are too short.

Pools on the host:

- `runtime/nvn_api_obj.c`: `nvn_tpool_slot(pool, id)` and `nvn_spool_slot(pool, id)` map a pool's id to a global
  slot, with `1 << 20` slots per pool.
- `RegisterTexture` / `RegisterSampler` go to `gfx_backend()->texture_register` / `sampler_register`.
- Lookups (`runtime/vk.h`): `vk_texture_by_id_locked(id, view, has_view)`, `vk_sampler_by_id_locked(id)`,
  `vk_texture_slot_token`.
- Change counters: `vk_texture_slots_gen` and `vk_sampler_gen`.

## Plan

1. **Measure the pools.** `NVN_LOG("TexturePool n=%d")` and `NVN_LOG("SamplerPool n=%d")` in `nvn_api_obj.c` print
   the sizes. They are not in a plain run's log; find the trace switch that enables `NVN_LOG` (probably `OOT_TRACE`)
   and confirm with `--expect 'TexturePool n='`. Also log the highest texture id registered by the time the level is
   up, which is the real bound for the arrays.
2. **Make the lengths real.** Have `Program.cs` answer the measured pool lengths, either as a constant or from a
   job field the host passes (`_job`, as the other queries do). Rounding up to a power of two keeps the cache keys
   stable. A changed answer changes the SPIR-V, so rebuild the translator (`tools/build_xlate.py`) and bump
   `KEY_VERSION` in `runtime/gfx_shader.c`.
3. **Mark pool arrays in the reflection.** Recognise them by handle `0x40000000` with cbuf_slot 0, either by a flag
   in `gfx_shader.c` or by comparing in `vk_exec_draw.c`. The current `cbuf_handle` path must not treat them as
   bank-0 or uniform-block handles.
4. **Fill element k from the pool.**
   - First, a simple version: in `bind_resources`, element k = `vk_texture_by_id_locked(nvn_tpool_slot(bound
     texture pool, k))`, and the sampler array's element k = `vk_sampler_by_id_locked(nvn_spool_slot(bound sampler
     pool, k))`. Use the placeholder of the right view type for empty ids (`dummy_index(res_view_type(...))`) and
     for ids whose view type does not match the array's (a 2D-array texture in the 2D array).
     This is correct but writes thousands of descriptors per draw. Measure it before optimising.
   - Then the fast version: one persistent descriptor set per pool and view type, with update-after-bind and
     partially-bound bindings (descriptor indexing: `runtimeDescriptorArray`, `descriptorBindingPartiallyBound`,
     `descriptorBindingSampledImageUpdateAfterBind`, `shaderSampledImageArrayNonUniformIndexing`). Rewrite only
     the elements whose slot changed since the last frame, using the gen counters (or a dirty list kept by
     `texture_register`). The pipeline layout puts the pool arrays in their own set, so a draw binds it with no
     writes. That means the pool bindings move out of set 0, either in `vk_pipeline.c` (renumbering the module's
     decorations, as it already does for bindings) or in the translator's binding allocator.
   - Check `vk_device.c` / `vk_caps.c` for the descriptor-indexing features and limits
     (`maxPerStageDescriptorUpdateAfterBindSampledImages`); none are enabled today.
5. **Non-uniform indexing.** The index comes from data, so it can differ across a subgroup. Ryujinx's
   `Instructions.cs` `MarkNonUniform` decorates the access only when `HostCapabilities.SupportsShaderNonUniformIndexing`
   is set. Make sure `Program.cs` sets it, or the result is undefined on divergent handles (the kind of artifact that
   shows up as tile-edge seams).
6. **The bound pool.** Find which pools are bound at a draw (`nvnCommandBufferSetTexturePool` and
   `nvnCommandBufferSetSamplerPool`). Then check that the replay records it per draw, not once.

## Checking it

- Follow the project skill `verify-shader-change` after steps 2 and 5: they change the SPIR-V.
- Use the level-route screenshot above, scored with `tools/shot_diff.py`. The materials should show texture
  detail instead of flat black. Compare with a screenshot from before the change.
- Take a frame dump on the level (`--dump <frame>`). `vk_dump.c` records the handle of each texture binding
  (`vk_exec_res_handle`), so extend it to list the pool ids a pool array resolved to.
- Watch the frame rate (`OOT_TRACE=perf`). The level runs at about 17 fps before this work, and the simple
  version of step 4 may make it slower.

## Open questions

- The pool sizes, and whether one texture pool stays bound for the whole level.
- Whether some materials index past what `IsBindlessAccessAllowed` accepts (a handle computed in registers). Those
  stay unresolved and need another approach. Look for translator warnings in the level's translations.
- Whether NVN2 compute kernels sample through pools as well. If they do, the bank-0 `cbuf_handle` path needs the
  same exclusion.

## Status

- 2026-11-06 (morning): findings above; nothing implemented.
- 2026-11-06 (afternoon): pool-indexed sampling implemented; the level's pool samples reach their textures, but the
  scene stays mostly black for another reason (below). Not committed yet.

### What was built

- **Pool sizes** (plan step 1, from an `OOT_TRACE=gfx` log): one texture pool of 33068 descriptors, one sampler pool
  of 4096. That ruled out the plan's "simple version" (about 100,000 descriptor writes per program per draw).
- **Translator** (`tools/xlate/Program.cs`): pool arrays of 65536 textures and 4096 samplers (`TexturePoolLength`,
  `SamplerPoolLength`); such an array takes one binding number (the accessor advanced `_binding` by the array length,
  so the bindings after a pool array passed 65535 and wrapped in the host's 16-bit fields: "uses binding 135181",
  then a device loss); the translator's own `Log` messages go into the `.res.json` notes. No translation of the level
  route logged "Failed to find handle source" (1254 translations), so every pool access resolved to the arrays.
- **Reflection** (`runtime/gfx_shader.c` `add_res`, `move_pool_arrays`, `add_pool_feedback`): a resource with handle
  `0x40000000` in cbuf 0 is `bindless`, and `pool` names its binding in the pool set (`gfx.h` `GFX_POOL_*`: samplers 0,
  one binding per view type 1-7, 2D shadow shares 2D). At load the module's decorations move those variables to set
  `GFX_POOL_SET` (2) in place, and every access chain into a pool texture array gets a store of 1 into a feedback
  buffer at binding 8 (`OOT_POOL_SPV_DUMP=1` writes the first patched modules out; all pass `spirv-val`).
  Pool-indexed images, buffer textures and multisampled arrays are logged and keep the placeholder (none met yet).
- **Layouts** (`runtime/vk_pipeline.c` `pool_layout_init`): one pool set layout, update-after-bind and partially bound,
  at set 2 of every pipeline layout (an empty set fills set 1 where a layout has one set of its own); the pool arrays
  stay out of the per-draw binding lists (`Bindings.pool`, `VkPipeInfo.pool_mask`). Needs the descriptor-indexing
  features and limits checked there; without them pool-indexing pipelines fail.
- **Replay** (`runtime/vk_exec_draw.c` "pool sets"): one persistent set per bound pool pair, bound at set 2 before a
  draw or dispatch that indexes it. Only ids the GPU sampled get their texture: each submission that bound a set
  copies its device-local feedback buffer into a host-cached one at its end (`vk_exec_pool_feedback`), and
  `pool_sync` reads it every 30 ms; a sampled id joins the wanted list and is resolved from then on (a token compare
  per wanted id when slots change, the per-submit storage check for CPU-written textures). Unsampled ids hold the
  stand-in of their view type.
- `OOT_POOL_SKIP=1` (diagnostic): pipelines that index the pools fail, so their draws are skipped.

### Found on the way

- **Every live texture does not fit.** Resolving all 33068 ids created an image for each live texture (images are made
  on first lookup, ASTC decodes to RGBA8): 11.5-12 GB of the 4070's 10.7 GB budget. Hence the GPU feedback.
- **Stale pool entries.** The pool keeps descriptors of finalized textures, some over storage that is no longer
  mapped. Uploading those faulted in `tex_bl_copy` (reads of 0x5864xxxxx): `vk_texture.c` `upload` now checks the
  storage is committed (`storage_readable`) and uploads zeros otherwise. Resolving a finalized texture brought it back
  as an entry that is never released, which under VRAM pressure dropped released entries, which came back the same
  way: the pool path uses `vk_texture_live_by_id_locked`, which answers only for storage a live texture object has
  (`nvn_tex_live_at`).
- **Feedback reads.** Reading the feedback buffer in place (host-visible VRAM through the BAR) took about 20 ms per
  sync; the copy into host-cached memory brought the pool sets' CPU time to about 300 ms over the 150 s route, most
  of it the first fill of the bindings.
- **NonUniform is off.** With `QueryHostSupportsShaderNonUniformIndexing` on, the level route lost the device in runs
  345 and 346 (346 with `OOT_POOL_SKIP=1`, so not through the pool arrays) and hung the GPU in 338 and 342, each time
  with 43 instruction pointers inside one small code range (warps stuck in one shader); runs 335 and 336 were clean.
  Off, runs 350 and 352 were clean. Ryujinx decorates the access chain but not the load from it; whether that is what
  the driver trips on is not known. Divergent pool indices are undefined without the decoration, so watch for seams.

### Where the level is

- Measured on the level route (`--replay build/logs/a-spam.txt --skip-movies --env OOT_SKIP_CS=1a62084a --trace perf
  --shot`): only **14 pool ids** are ever sampled, and the change in the picture is the sky (a gradient with a warm
  horizon instead of flat white: `build/logs/level-before.png`, run 348, against `build/logs/level-pool.png`, run 352;
  both score as pictures, noise 0.26 and 0.18). The geometry is black in both. So the black materials are not the pool
  arrays. The same route logs 64 distinct programs whose SM86 lowering fails ("no general register free for
  scratch", "RED address", "opcode", "PLOP3 table", "uniform datapath opcode", "TLD mode", ...), whose draws and
  dispatches are skipped: that is the next thing to look at (`docs/phase3.md`).
- The route runs at 0.6-0.9 fps once the level is up, GPU-bound (0.4-1.2 s of GPU time per frame), before this
  change as well (run 348, committed code): not a regression, but the "about 17 fps" above was not reproduced.
- The title is unaffected: grass loop at 30 s scores 0.53 (`build/logs/check-pool.png`, run 351).

### Left open

- Turning NonUniform back on safely (decorate the load too, or find the shader that hangs).
- Textures are never given back once sampled (the feedback is never cleared); fine for 14 ids, not for a large world.
- A pool texture that is also an attachment of the open pass (the feedback-loop scan sees bound slots only), and the
  depth-without-compare point sampler that bound slots get (`tex_info_lookup`).
- `--selftest` fails "MapPhysicalMemory beyond the pool is not LimitReached", with and without this change.
