# Mesh and task shaders: work tracker

The task list for drawing the geometry OoT sends through mesh shaders. `docs/mesh.md` has the evidence, the stage
buffer layout and the reasoning behind the plan; this file is what to do, in order, and how to tell each piece is
done. Update a task's status and add its commit when it lands; add a dated line under "Log" for anything learned
on the way that the next session needs.

Status values: `todo`, `doing`, `done`, `blocked` (say on what), `dropped` (say why).

## Goal and how to check it

The Kokiri Forest's colour pass draws its mesh-shader geometry: on the level route
(`python3 tools/run.py --seconds 150 --replay build/logs/a-spam.txt --skip-movies --env OOT_SKIP_CS=1a62084a --shot
build/logs/level-mesh.png`) the ground and the rest of the scene are textured instead of flat black and white, and a
frame dump (`--dump 900`) shows them in pass 168's target. The title (grass loop at 30 s) still scores as a picture
with `tools/shot_diff.py`, and the frame rate is reported only next to such a screenshot.

Baseline to compare with: `build/logs/level-lower2.png` (run 359) and `build/logs/dump-361` (pass 168).

## Decisions taken

- Native `VK_EXT_mesh_shader`, not a compute emulation (`docs/mesh.md`, "Plan"). Revisit only if task M5 shows the
  translator side cannot be done in reasonable patches.
- Lichii (GPL-3.0) is read for behaviour only; no code is copied from it. Facts taken from its notes are cited as
  `NX2_NOTES.md` row 89.
- First version: monolithic mesh pipelines (no graphics pipeline library), task and mesh shaders kept out of the
  pipeline-key prewarm until they work.
- B2 (2026-11-07): ISBERD/ISBEWR pass through the lowering as one pseudo instruction, `SM86_ISBE_OP` (top byte 0x6c,
  every field kept: register, address register, offset, space, read or write; `runtime/sm86_lower.h`), and every EXIT
  branches to `SM86_MESH_EPILOGUE` (0x6d). Not shared-memory ops: what a space means depends on the stage (a TASK
  read at 0 is the mesh workgroup index, a TASK write at 4 the mesh count, an ATTR read at 0 the workgroup index), and
  only the translator's expansion (C1-C3) knows the stage and the output variables.

## Tasks

### Phase A: know the formats (no code that changes what the game shows)

| ID | Task | Depends | Status | Done when |
|---|---|---|---|---|
| A1 | Capture task programs too: `OOT_MESH_DUMP` takes the first 12 programs with a task or mesh stage, and all 12 captured were mesh + fragment. Add a filter (`OOT_MESH_DUMP_STAGE=6`) or a higher limit, keyed by program so repeats are not dumped. | - | done (2026-11-07) | At least three task + mesh + fragment programs in `build/logs/mesh`. |
| A2 | Decode ISBEWR (0x927) and ISBERD (0x923): the address register, the immediate (offset and mode bits: the captures show values such as 0x2001c, 0x28006, 0x3000b), the data register, width, predicate. Teach `tools/sm86_dis.py` to print them. | - | done (2026-11-07) | The disassembler names every ISBE field; each field is explained in `docs/mesh.md` with an example from a capture. |
| A3 | Map the mesh output layout: which ISBE offsets hold positions, attributes, primitive indices and the vertex/primitive counts, checked against the notes (vertices in groups of 32, one 128-byte line per component, count at 0x3, U8 indices from 4). Do it on one small capture (`03_s7`, 320 instructions) by hand. | A2 | done (2026-11-07) | A table in `docs/mesh.md` from ISBE offset to output, which accounts for every ISBEWR of `03_s7`. |
| A4 | Decode the mesh and task control sections: workgroup size, maximum vertices and primitives, primitive type, output map (per-vertex and per-primitive), payload size, and the bank-4 constants (`c[4][0x5c]`). Compare the 12 captures' control blocks against each other. | - (A1 for the task fields) | done (2026-11-07): local memory and payload size checked; maxima, workgroup size and topology from the values only, to be confirmed by the first probe (D4) | `tools/kernel.py stats`-style output for a mesh program prints those fields, and they agree with A3 for every capture. |
| A5 | Find the NVN2 entry points' arguments: `DrawMeshTasks` (a capture shows x1 0, x2 0xc, x3 0x400000), `DrawMeshTasksIndirect` (x1 an address, x2 1, x3 the same address), and the Multi/3D variants the game calls (counts from the stub log), plus any state they read (SET_MESH_CONTROL-style state in NVN2's API, task workgroup counts). | - | done (2026-11-07; the indirect record's layout left to D3) | A comment block per entry point in `nvn_api_cmd.c` (where they will live) with the meaning of each argument, checked on several calls. |
| A6 | Count what is at stake per frame: mesh draws per pass in the level frame, so a dump can show which passes they land in. Extend the frame dump to list stubbed mesh draws (as it lists draws skipped for a missing shader). | - | done (2026-11-07) | `passes.txt` of a level dump shows the mesh draws in their passes. |

### Phase B: lowering

| ID | Task | Depends | Status | Done when |
|---|---|---|---|---|
| B1 | Accept task (6) and mesh (7) stages in `runtime/sm86_lower.c`: header handling as for graphics stages, the stage in `SM86` control parsing, and in `tools/sm86_lower_main.c`. | A4 | done (2026-11-07): nothing to change, `sm86_control_parse` takes stages up to 7 with the graphics header | The 12 captures get past the start of the program (they fail at the first ISBE instruction, not on the stage). |
| B2 | Lower ISBERD/ISBEWR. Choose one: (a) a pseudo instruction the translator decodes (a reserved Maxwell encoding carrying the fields), or (b) shared-memory loads and stores into an emulated stage buffer at a fixed shared-memory base. Record the choice and why under "Decisions taken". | A2, A3 | done (2026-11-07), choice (a): 34 of the 36 captured stages lower; 2 fail on register pressure ("no register pair dead over a uniform pair's span", `mesh-task/10_s7`, `11_s7`) | All captured task and mesh programs lower (the sweep script in the 2026-11-06 session: `sm86_lower` over every `build/logs/mesh/*_s[67]`). |
| B3 | Route every EXIT of a task or mesh program to one epilogue marker the translator expands (EmitMeshTasks for task, SetMeshOutputs plus the output copy for mesh). | B2 | done (2026-11-07): every EXIT of a task or mesh stage branches to `SM86_MESH_EPILOGUE` before the final EXIT | The lowered programs end in the marker on every path; `tools/kernel.py lower` output checked on two captures. |
| B4 | Store task and mesh inputs in the shader cache like the other stages (`.in` files, `tools/kernel.py` list/stats/lower for them), so later work is offline. | B1 | todo | `tools/kernel.py list` shows task and mesh programs from a game run. |

### Phase C: translator (patches in `tools/xlate/patches`, made with `tools/xlate_patch.py`)

| ID | Task | Depends | Status | Done when |
|---|---|---|---|---|
| C1 | Add task and mesh stages to Ryujinx's `ShaderStage` and the translator's stage plumbing (`TranslatorContext`, `ShaderDefinitions`, attribute handling), and to `tools/xlate/Program.cs` job fields (stage `ts`/`ms`, workgroup size, max vertices/primitives, output map). | B3 | done (2026-11-08): patch 0021; decoded as compute after the graphics header, emitted as `TaskEXT`/`MeshEXT`; the job carries `local`, `maxv`, `maxp`, `payload` and the 16 output-map bytes | A task and a mesh program translate without exceptions. 11 of the 12 captured mesh programs lower and translate; `11_s7` still fails the lowering (register pressure). |
| C2 | SPIR-V back end: `TaskEXT`/`MeshEXT` execution models, `SPV_EXT_mesh_shader`, `MeshShadingEXT` capability, `OutputVertices`/`OutputPrimitivesEXT`/`OutputTrianglesEXT` and `LocalSize`, the task payload (`TaskPayloadWorkgroupEXT`), per-vertex and per-primitive output arrays, `PrimitiveTriangleIndicesEXT`. | C1 | done (2026-11-08): the modules are SPIR-V 1.4 (the extension requires it), so mesh modules use `StorageBuffer` + `Block` instead of `Uniform` + `BufferBlock` and list every global in the entry point's interface | `spirv-val --target-env vulkan1.3` passes on all 11 captured mesh programs (`build/logs/meshxlate`). |
| C3 | The epilogue: for task, `OpEmitMeshTasksEXT` with the count and payload from the emulated buffer; for mesh, `OpSetMeshOutputsEXT` with the counts, then the copy of positions, attributes and indices from the buffer into the output arrays (layout from A3). | C2, B3 | doing (2026-11-08): the epilogue is emitted as IR (barrier, count byte, one load per output line per vertex group, the primitive loop, `EmitMeshTasks` for a task stage); needs D4 to prove it draws | One mesh program run on hand-made inputs (`--probe-cs`-style probe for mesh, see D4) produces the expected triangles. |
| C4 | Fragment shaders paired with mesh programs: their inputs come from the mesh's per-vertex outputs (and per-primitive ones, `PerPrimitiveEXT`); check the location mapping against the mesh output map. | C2 | doing (2026-11-08): mesh outputs are per-vertex `vec4[maxV]` arrays at the output map's Locations, so the fragment stage's inputs match without a per-pipeline patch; a pipeline builds (D2), the pairing still needs E1's picture | The fragment shaders of the captures validate linked with their mesh shader (a pipeline builds, D2). |

### Phase D: host

| ID | Task | Depends | Status | Done when |
|---|---|---|---|---|
| D1 | Enable `VK_EXT_mesh_shader` (`taskShader`, `meshShader`) in `runtime/vk_device.c`, its limits in `runtime/vk_caps.c` (`maxMeshOutputVertices`, `maxMeshOutputPrimitives`, workgroup limits), logged at boot; mesh stages off cleanly where missing. | - | done (2026-11-07): `vk: mesh shaders on` on the RTX 4070 (256 vertices and primitives, 128 invocations, 16 KiB payload) | The boot log names the feature and limits on the RTX 4070. |
| D2 | Reflection and pipelines: task/mesh stages in `gfx.h` (`GFX_STAGE_*`), `gfx_shader.c` (stage from the control section, translation jobs), `vk_pipeline.c` (a mesh pipeline: task + mesh + fragment, no vertex input or input assembly, its descriptor layout from the stages like the others, the pool set included). | C2, D1 | done (2026-11-08): `GFX_STAGE_TS`/`GFX_STAGE_MS`, the request's `local`/`maxv`/`maxp`/`payload`/`omap`, `VkPipeState.ts`/`.mesh`, `create_mesh_graphics` (monolithic, no GPL) and `pipe_miss_mesh` | Mesh pipelines build for the captured programs (the level run builds them; see E1). |
| D3 | Record and replay the draws: `DrawMeshTasks*` from stubs to recorded ops (`nvn_api_cmd.c`, `nvn_obj.h` payloads, `vk_exec.c` walk), replayed as `vkCmdDrawMeshTasksEXT` / `...IndirectEXT` / `...IndirectCountEXT` with the bound program's mesh pipeline and the same resource binding path as draws (`bind_resources`). | A5, D2 | doing (2026-11-08): `op_draw_mesh` replays `vkCmdDrawMeshTasksEXT` and `...IndirectEXT` (stride 16) through `prepare_mesh`; the commands are recorder packets (`rec_draw_mesh_tasks*`) so they run after the pass, the pipeline bind and the dynamic state; a program bound with only task/mesh stages keeps its own program; a mesh pipeline uses a dynamic state list without the vertex input/topology/restart states | The draws replay (`vk_exec: mesh draw ... replayed` in the log), but the device is lost (`vkQueueSubmit2 ... -4`) within about half a second after the first mesh draws; the dumped frame is black. `OOT_MESH=0` keeps them as stubs for an A/B. |
| D4 | A probe for one mesh program in isolation (as `--probe-cs` for compute): fixed inputs, one draw into an offscreen target, read back. | D3 | todo | The probe draws the expected shape for a hand-made task count and payload. |
| D5 | Frame dump support: mesh draws in `passes.txt` with their program, counts and bound resources (`vk_dump.c`). | D3 | todo | The level dump's pass 168 lists its mesh draws with their resources. |

### Phase E: in the game

| ID | Task | Depends | Status | Done when |
|---|---|---|---|---|
| E1 | First light: the level route with mesh draws on. Fix what breaks (counts, layouts, resource bindings), comparing pass 168 before and after. | C3, C4, D3 | todo | The goal's screenshot check passes. |
| E2 | Task programs and indirect counts: the Multi*/IndirectCount variants, task amplification. | E1 | todo | No mesh draw left as a stub on the level route (stub counts in the log are zero for the mesh entry points). |
| E3 | Performance: GPU time of the mesh passes (`OOT_TRACE=perf`), pipeline build time, the prewarm and the disk cache for mesh pipelines. | E1 | todo | Frame rate on the level reported next to a passing screenshot, mesh pipelines in the prewarm. |
| E4 | Clean up: diagnostics that stay are documented in `CLAUDE.md`; `docs/mesh.md` Status updated; the switches added on the way are in `runtime/cfg.c`'s list. | E1 | todo | `python tools/check_content.py --staged` passes and the docs match the code. |

## Design for phase C (2026-11-07, before any code)

Measured: `Spv.Generator/spirv.cs` (vendored) has only the NV mesh enums (TaskNV 5267, MeshNV 5268); the EXT ones
from the Vulkan SDK's `Include/spirv-headers/spirv.h`: ExecutionModel TaskEXT 5364, MeshEXT 5365; ExecutionMode
OutputVertices 26, OutputPrimitivesEXT 5270, OutputTrianglesEXT 5298; StorageClass TaskPayloadWorkgroupEXT 5402;
Decoration PerPrimitiveEXT 5271; BuiltIn PrimitiveTriangleIndicesEXT 5296, CullPrimitiveEXT 5299; Capability
MeshShadingEXT 5283; OpEmitMeshTasksEXT 5294, OpSetMeshOutputsEXT 5295 (no result, no result type). Ryujinx refers
to stages in 29 files of `Ryujinx.Graphics.Shader` (139 uses; `ShaderStage.Count` sizes per-stage arrays) and already
declares per-vertex output arrays for tessellation control (`Declarations.cs`, `gl_out[]` sized by
`ThreadsPerInputPrimitive`), the nearest model for mesh outputs indexed by vertex.

The plan, smallest first:

1. **Translate task and mesh stages as compute programs** in the front end: workgroup and thread ids, local memory,
   shared memory and barriers then work as they do. The lowering emits them without the graphics header (as compute,
   `l.graphics` 0), and the job passes what the header carried: the stage (`ts` / `ms`), the output map (the
   components of the code header's VTG map from byte 0x34), the maximum vertices and primitives (control +0x60), the
   payload size (control +0x58, top byte), the workgroup size (32).
2. **Decode the two pseudo instructions** (top byte 0x6c, 0x6d; `runtime/sm86_lower.h`) in `InstTable.cs` and emit IR:
   ATTR and MAP writes to a shared-memory stage buffer (ATTR at 0, MAP after the ATTR size: maxV rounded up to 32 x
   lines x 4 bytes; MAP.32 at 3 = the count word, MAP.U8 = byte stores); a TASK write at 4 = the count word, from 0x10
   = the payload (shared too, copied at the end); an ATTR read at 0 or a TASK read at 0 = the workgroup id's x; a TASK
   read from 0x10 = the task payload variable.
3. **The epilogue in IR** (where 0x6d is decoded): a workgroup barrier; mesh: count = the count word,
   SetMeshOutputs(maxV, count), then for v = tid + 32 j (j unrolled to maxV / 32) each component line k into its
   output (position for lines 0-3 when the map has it, else the user output's location and component) at index v,
   and for p = tid + 32 j below count the three index bytes into the primitive indices at p; task: copy the payload
   words into the payload variable, EmitMeshTasks(count, 1, 1). Ryujinx's own structurizer handles the loops and ifs.
4. **SPIR-V**: the execution model, modes (OutputVertices, OutputPrimitivesEXT, OutputTrianglesEXT, LocalSize), the
   capability and extension, the per-vertex output arrays sized maxV, `gl_PrimitiveTriangleIndicesEXT[maxP]`, the
   payload variable, the two new instructions. Spv.Generator gets the enums and two instruction builders.

Checks along the way: `tools/kernel.py xlate` and `spirv-val --target-env vulkan1.3` on the captured programs (with
B4 or a direct job file), then a mesh pipeline (D2) with the paired fragment shader.

## Open questions

- Is the stage buffer layout of this game the same as the one in Lichii's notes? (A3 answers it.)
- Do the fragment shaders of mesh programs read per-primitive inputs? (A4, C4.)
- How are the task programs' workgroup counts passed: the draw's arguments, or memory the GPU wrote (the indirect
  variants)? (A5.)
- Do the game's mesh programs use bank 4 for something the host must provide (`c[4][0x5c]` in every capture)? (A4.)

## Log

- 2026-11-06: tracker written. Evidence and diagnostics in place (`docs/mesh.md`); nothing of phases A-E started.
- 2026-11-07: A1, A2 done; A3, A4 under way. The vertex output lines follow the code header's VTG output map (from
  byte 0x34, position in 0x35), checked on all 12 mesh captures; task programs write their payload and the mesh count
  in a fifth space (bits 75-78 = 7) that the mesh programs read back with ISBERD.TASK. `tools/sm86_dis.py` decodes
  both ops; `OOT_MESH_DUMP_STAGE=6` captures task programs. A3 done: vertex v's line k at
  `(v >> 5) * lines * 0x80 + k * 0x80 + (v & 31) * 4`, triangle p's indices at bytes `4 + 3p`, the count written at 3.
  A5 done: DrawMeshTasks(cmd, first, count), DrawMeshTasksIndirect(cmd, address) with GPU-written 16-byte records.
- 2026-11-07: A6 done. The two entry points are recorded as OP_DRAW_MESH (no longer stubs); the replay counts them
  and a frame dump notes each with its program and pass (`build/logs/dump-375`, frame 900: 54 mesh draws, mostly
  indirect, in the 1600x1600 shadow pass and the 1920x1080 colour pass, a few direct ones in small passes). A note
  "outside a pass" belongs to the next render pass: the replay opens a pass at its first ordinary draw.
- 2026-11-07: A4 done, so phase A is: +0x50 is the workgroup's local memory (32 x per-thread), the top byte of
  +0x58 the task payload (1 KiB per 4), +0x60 presumably the vertex and primitive maxima. Next: B1 (accept stages 6
  and 7 in the lowering) and D1 (enable VK_EXT_mesh_shader), which do not depend on each other.
- 2026-11-07: D1, B1, B2, B3 done. The lowering emits the two pseudo instructions; UP2UR (uniform 0x883, the uniform
  predicates as bits) is lowered too, which five mesh stages of task pipelines use. No regression over the stored
  inputs (530) and the refused programs that lowered before (608). KEY_VERSION 17. Next: B4 (stored inputs of task
  and mesh programs) needs the stages translated on demand, so C1 comes first.

- 2026-11-08: C1, C2, D2 done and C3/C4/D3 first versions in place (translator patch 0021, `KEY_VERSION` untouched: the
  translator identity re-keys the cache). The captures are 12 mesh + fragment programs (the level's task pipelines show
  up as a 1 6 7 stage set at run time); 11 lower and translate, `11_s7` fails the lowering ("no general register dead
  over a uniform pair's span"). None of the captured mesh programs samples a texture: they read constant buffers and
  one storage buffer, so the c2 handle offsets of stages 6/7 did not have to be measured yet. Mesh modules are SPIR-V
  1.4, which forbids `BufferBlock`, needs every global in the entry point interface, and requires `spirv-val
  --target-env vulkan1.3` (not an older target). The per-stage arrays in `vk_exec.h` (`stage_prog`, `ubo`, `ssbo`,
  `tex`, `samp`, `img`) grew to 8 NVN stages; the program-to-stage map (`stagem`) stays at 6 and scans for 6/7.
- 2026-11-08 (later): the mesh draws replay but take the device down. Found and fixed on the way: the draw commands
  bypassed the recorder thread (they could run before their render pass and pipeline; the validation layer named it),
  the mesh pipeline listed dynamic states a mesh pipeline must not (PRIMITIVE_TOPOLOGY, PRIMITIVE_RESTART_ENABLE,
  VERTEX_INPUT_EXT), and the mesh module's `spirv_rebind` length was read from an out-parameter that does not exist
  (`spirv_rebind` keeps the length), so `vkCreateShaderModule` was called with 0 words and every mesh pipeline was
  skipped. Still open: the device loss after the first ~3 direct mesh draws. Candidates: `OpSetMeshOutputsEXT` in
  non-uniform control flow, per-vertex output ownership, a primitive index at or above the declared vertex count (now
  clamped), the task payload type of a task+mesh pair, or a descriptor bound for the wrong stage. The level's captured
  task pipelines are stages 1 6 7 (fragment, task, mesh); the ordinary (non-mesh) draws still render the frame black
  in these runs, so an `OOT_MESH=0` A/B is the next measurement.
- 2026-11-08 (evening): the bisect rules out the two cheapest explanations. `OOT_MESH_TASK=0` (drop the task stage)
  still loses the device on every run, and `OOT_MESH_ZERO=1` (the task/mesh stages read zeroed uniform/storage
  descriptors) does too, so it is neither the task shader nor a descriptor bound for the wrong stage: the mesh stage's
  own execution loses the GPU. The draws are tiny (3 workgroups), so it is a fault, not a timeout. A mesh shader's
  per-vertex block now holds Position only (no unwritten PointSize/ClipDistance/CullDistance members). Next: the D4
  probe, a hand-made minimal mesh pipeline drawn into an offscreen target, to iterate without the level; then bisect
  the epilogue (vertices only vs primitives only).

- 2026-11-08 (night): **the device loss was an infinite loop**. The epilogue's primitive loop was built with an IR
  local as its counter, and the AST optimizer dropped the increment (the generated SPIR-V loads the counter, never
  stores it), so every mesh workgroup whose primitive count was not zero spun until the GPU watchdog killed the
  context. Bisected with `OOT_MESH_EMPTY`: 1 (announce nothing) survives, 2 (vertices, no primitives) survives,
  3 (one fixed primitive) survives, 4 (the game's primitive count, fixed indices) and 5 (at most 8 primitives) both
  lose the device. The runtime's fault report names the faulting submission's part (`vk: device fault address N:
  instruction pointer (unknown) at ...`) and `OOT_LOG_MS=1` timestamps every log line (the loss comes seconds after
  the draw, the driver's timeout). Both epilogue loops (primitive indices, task payload) are now unrolled; the
  maximum counts bound the step count (126 primitives / 32 = 4 steps; 12 KiB payload / 32 = 96).

- 2026-11-08 (night, after the fix): the level route with mesh draws enabled **completes** (1269 presents, no device
  fault) and the shot set shows the Kokiri Forest with Link on the path, trees, grass, HUD and minimap (the known
  white overexposure is still there). E1's "textured ground instead of flat black" is therefore reachable; the next
  checks are an A/B against `OOT_MESH=0` at the same world window (does the mesh geometry actually change the frame?)
  and a frame dump of the colour pass (D5).
