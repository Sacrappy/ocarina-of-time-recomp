# Mesh and task shaders

Hand-off for the work that draws the geometry the Kokiri Forest's colour pass sends through mesh shaders. Written
2026-11-06, before any code for it beyond diagnostics; update the Status section as it moves.

## Symptom and evidence

On the level route (`python3 tools/run.py --seconds 150 --replay build/logs/a-spam.txt --skip-movies --env
OOT_SKIP_CS=1a62084a --shot ...`) most of the scene is flat black or white. A frame dump (`--dump 900`,
`build/logs/dump-361`, `docs/phase3.md` evening status) shows:

- pass 136, a depth prepass: 217 ordinary draws, colour masked, covering the whole view;
- pass 168, the colour pass (depth EQUAL, no write): its ordinary draws render correctly (textured bark and moss),
  but most of the scene, the ground among it, is missing.

The missing part is drawn with mesh shaders, which are stubs:

- `nvnCommandBufferDrawMeshTasksIndirect` is called thousands of times a run, `DrawMeshTasks` too. Both were stubs;
  since 2026-11-07 they are recorded as `OP_DRAW_MESH` (`nvn_api_cmd.c`) and the replay counts them and notes them in
  frame dumps (`vk_exec_draw.c op_draw_mesh`) without drawing them: about 54 a level frame.
- The game's programs come in four stage sets by NVN2's stage byte (control +0x56; `nvn_api_obj.c prog_set_shaders`
  logs each set once): vertex + fragment (0 1), compute (5), mesh + fragment (7 1) and task + mesh + fragment
  (6 7 1).
- `OOT_MESH_DUMP=<dir>` (a Windows path, e.g. from `wslpath -w build/logs/mesh`) writes the control block (0x400
  bytes) and code of every stage of the first 12 mesh programs as `<k>_s<stage>.ctl/.code`. The first 12 are mesh +
  fragment (no task stage among them); `tools/sm86_dis.py <file>.code` lists them.

## What the programs contain

Over the 12 captured mesh stages (320-2200 instructions each), only two opcodes are new to the lowering; the rest is
the instruction set the vertex, fragment and compute programs already use:

- `0x923` ISBERD, once per program, at the start (reads the stage buffer: the mesh group's index, the payload);
- `0x927` ISBEWR, about 800 times (writes the stage buffer: the outputs), e.g. `ISBEWR [R7 + 0x2001c]`.

All 12 stop in `sm86_lower` at the first ISBERD/ISBEWR ("opcode"). Bank 4 constants appear too (`c[4][0x5c]`).

## Decoded from our captures (2026-11-07)

`tools/sm86_dis.py` prints both ops by these fields (12 mesh stages, `build/logs/mesh`).

**ISBERD** (0x923): `Rd` at 16-23, address register at 24-31, byte offset `w1 >> 8`, the space in bits 75-78 as for
ISBEWR below. Task programs and stand-alone mesh programs read their workgroup index with `ISBERD.ATTR Rd, [RZ + 0]`
once at the start; the mesh programs of task pipelines read `ISBERD.TASK [RZ + 0]` and the payload the task wrote
(`[RZ + 0x10]`, `[RZ + 0x14]`, `[Rn + 0x18]`, `[Rn + 0x418]`, `[Rn + 0x818]`).

**ISBEWR** (0x927): address register at 24-31; word 1 holds the data register in bits 0-7 and the byte offset above
them (`w1 >> 8`); bits 75-78 select what is written; bit 79 is set in every capture.

| bits 75-78 | written | count in the captures | example |
|---|---|---|---|
| 0xf | a vertex output component (attribute space) | 716 | `ISBEWR.ATTR [R7 + 0x280], R6` |
| 0x1 | a 32-bit value into the primitive map | 12 (one per program) | `@!P4 ISBEWR.MAP.32 [RZ + 0x3], R11`: the primitive count |
| 0x0 | a byte into the primitive map | 72 | `ISBEWR.MAP.U8 [R4 + 0x4..0x6], R3 R21 R7`: one triangle's vertex indices |
| 0x7 | the task's output (task programs only) | 76 over 12 task stages | `ISBEWR.TASK [RZ + 0x4], R0` before the last EXIT: the mesh workgroup count; `[RZ + 0x10]`, `[RZ + 0x14]` and arrays `[R16 + 0x18 / 0x418 / 0x818]` (0x400 apart): the payload |

Task programs (12 captured in `build/logs/mesh-task`, `OOT_MESH_DUMP_STAGE=6`; 650-1280 instructions) use only
ISBERD.ATTR and ISBEWR.TASK besides the ordinary instruction set.

**Vertex outputs**: one 0x80-byte line per output component (32 vertices of 4 bytes; the address register is the
vertex's byte offset in its line, so `vertex * 4` for the first 32, a working assumption the culling arithmetic of
`03_s7` does not contradict; confirmed for groups in `01_s7`, below), lines in output-map order: line k is the k-th enabled component of the program's
output map. Checked on all 12: the map is the code header's (a Maxwell VTG shader program header, the 0x80 bytes
before the first instruction) output map from byte 0x34: 0x34 system values A, **0x35 system values B (0xf0 =
position xyzw, lines 0-3)**, then a nibble per generic vector from 0x36 (low nibble first). `03_s7`: position +
vector 5 .xyz = 7 lines, lines 0-6 written. `01_s7`: position + vectors 0.x 1.x 3.xyz 4.xyz 5.x 6.xyz 7.xyz 10.xyz
11.xyz = 25 lines, lines 0-24 written. Vertices come in groups of 32, each group's lines after the previous
group's: `01_s7` computes its address register as `(v >> 5) * 0xc80 + (v & 31) * 4` (SHF by 5, IMAD by 0xc80 = 25
lines x 0x80). So component line k of vertex v is at `(v >> 5) * lines * 0x80 + k * 0x80 + (v & 31) * 4`.

**Primitive map**: the primitive count is the value of the MAP.32 write at byte offset 3 (one lane writes it, under
a guard; whether only its low byte lands there does not matter to an emulation, which takes the value). Triangle p's
three U8 vertex indices are at bytes `4 + 3p` to `6 + 3p` (`03_s7`: `LEA R4 = (R4 << 1) + R4` = 3p, then
`[R4 + 4..6]`).

**Task and mesh control sections** (compared over the 24 captured stages, 12 stand-alone mesh, 12 task + mesh;
"checked" means it agrees with the code of every capture):

| offset | values | meaning |
|---|---|---|
| +0x38, +0x40, +0x44 | | code end and size, as for the other stages |
| +0x4c | 0x28-0xff | registers used (guess) |
| +0x50 | 0, 0x200, 0x800, 0x1a00 | **local memory of the 32-thread workgroup**, checked: non-zero exactly in the programs with LDL/STL, and 32 x (their highest offset + its width): 32 x 0xd0 = 0x1a00, 32 x 0x40 = 0x800, 32 x 0x10 = 0x200 |
| +0x58 bits 0-15 | 0x20 in all | threads per workgroup (32; guess, the vertex groups are 32 wide) |
| +0x58 bits 16-23 | 0x80 in all | unknown |
| +0x58 bits 24-31 | 0x00 (stand-alone mesh), 0x04, 0x0c (task pipelines; the same in a pipeline's task and mesh stage) | **the task payload size, 1 KiB per 4** (0x04: the task writes up to 0x1c; 0x0c: up to 0x81c), from two values |
| +0x5c | 0x03000000 in all | output topology: triangles (guess) |
| +0x60 | mesh stages 0x00400080, 0x00400060, 0x00200060, 0x00600060; task stages 0 | maximum vertices (bits 0-15: 128 or 96) and primitives (bits 16-31: 32, 64 or 96), guess: zero exactly in task stages |
| +0xe0-+0xf0 | non-zero in the three largest stand-alone mesh programs | unknown |

## Layout, from Lichii's notes (GPL-3.0: behaviour only, write our own code)

`/mnt/c/dev/Lichii/NX2_NOTES.md`, row 89 (the same scheme in another Switch 2 title):

- Task program: `ISBERD.ATTR.SKEW [RZ]` is the workgroup index; it writes the payload with `ISBEWR.O.ATTR` at
  `rank * stride + 0x10` and the number of mesh workgroups to launch at `[0x4]`.
- Mesh program: input `[0]` is its workgroup index, the payload from 0x10. Outputs: vertices in groups of 32, one
  128-byte line per output component in the mesh header's output-map order, then the per-primitive groups; the
  primitive map has the count at `ISBEWR.O.32 [0x3]` and U8 vertex indices from 4. Addresses are linear bytes.
- On the GPU side, while SET_MESH_CONTROL (method 0x114c) is on, the task program sits in the vertex slot, the mesh
  program in the tessellation slot, and a header-only pass-through geometry program supplies the per-primitive output
  map. The draw count is the number of task workgroups.
- Their approach: VK_EXT_mesh_shader; ISBE reads and writes kept as a pseudo instruction through the lowering, an
  emulated stage buffer (task payload / workgroup memory), and every EXIT branching to one epilogue that emits
  EmitMeshTasks (task) or SetMeshOutputs plus the copy of the buffer into the mesh outputs (mesh).

To confirm against our captures before relying on it: the field meanings of `ISBEWR` (address register, the
immediate's offset and mode bits) and where the control section declares the output count, the maximum vertices
and primitives and the output map of a mesh program.

## Plan

1. **Decode** ISBERD/ISBEWR on the captured programs (`tools/sm86_dis.py`; Mesa NAK has no encoder for them, so the
   fields come from the captures and the notes above) and the mesh control section fields (max vertices,
   primitives, workgroup size, output map). Capture task programs too (raise the dump's limit or filter on stage 6).
2. **Lowering** (`runtime/sm86_lower.c`): keep ISBERD/ISBEWR as a pseudo instruction the translator understands
   (a translator patch decodes it), or lower them to shared-memory loads and stores into an emulated stage buffer
   (workgroup memory sized from the control section), with every EXIT jumping to an epilogue marker.
3. **Translator** (a patch series in `tools/xlate/patches`): task and mesh execution models in Ryujinx's SPIR-V back
   end (`TaskEXT`, `MeshEXT`, `SPV_EXT_mesh_shader`): the payload, the epilogue (EmitMeshTasksEXT;
   SetMeshOutputsEXT plus copying positions, attributes and primitive indices out of the emulated buffer), local
   size from the control section. The fragment stage stays as it is (it reads the mesh's attributes as varyings).
4. **Host**: mesh pipelines (no vertex input; task and mesh stages; `VK_EXT_mesh_shader` enabled in
   `vk_device.c` and checked in `vk_caps.c`), the reflection of task and mesh shaders in `gfx_shader.c`, and the
   draws: record `DrawMeshTasks*` as replay ops (`nvn_api_cmd.c`) and replay them as `vkCmdDrawMeshTasks*EXT`.
5. **Check** with the level route's screenshot and a frame dump: pass 168 should have the ground.

An alternative to 3 and 4 is running task and mesh programs as compute that writes vertex and index buffers, then
an indirect draw with a pass-through vertex shader. It avoids a new execution model in the translator, but needs a
chain of indirect dispatches and draws per call and buffers sized for the worst case; VK_EXT_mesh_shader is on both
target GPUs (RTX 4070, RX 9070), so the native way is the recommendation.

## Status

The task list with its status is `docs/mesh-tracker.md`.

- 2026-11-06: findings above; diagnostics `OOT_MESH_DUMP`, the stub call counts and the stage-set log in place.
