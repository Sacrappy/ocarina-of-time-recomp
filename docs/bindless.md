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

- 2026-11-06: findings above; nothing implemented.
