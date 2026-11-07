// Written for ocarina-of-time-recomp (MIT, Copyright (c) 2026 Jeff Aigner).
// SM86 (Ampere, the Switch 2's T239) shader programs lowered to Maxwell (SM5x) programs the vendored Ryujinx
// translator decodes. See sm86_lower.c for the layout of both and the provenance of the encodings.
#pragma once
#include <stdint.h>

typedef struct {
    uint8_t *code;             // Maxwell program: graphics: the 0x50-byte program header, then instructions in
                               // groups of three behind a (zero) scheduling word; compute: the groups alone
    uint32_t code_size;
    uint8_t *c1;               // the jump-table constants (constant bank 1) with their targets moved to the
                               // lowered program; the translator reads BRX targets from here
    uint32_t c1_len;
    uint32_t error_offset;     // byte offset of the SM86 instruction that could not be lowered, from the start
                               // of the code block (header included)
    char error[96];            // empty when the lowering succeeded
} Sm86Lowered;

// code and control are the NVN2 program as the game hands them to nvnProgramSetShaders (and as the archives hold
// them): control is the NVN2 control section (magic 0x98761234; its fields are read here), code the code block
// the control describes (graphics: 0x80-byte header, instructions, padding, the jump tables). Returns 1 and fills
// out on success; 0 with out->error set otherwise. Free with sm86_lower_free either way.
int sm86_lower(const uint8_t *code, uint32_t code_size, const uint8_t *control, uint32_t control_size, Sm86Lowered *out);
// Set by the host before any lowering (gfx_shader_set_tensor): 1 lowers an unconditional HMMA.16816.F16 of a compute
// program whose workgroup is whole subgroups to the translator's MMA marker (tools/xlate patch 0016), which becomes a
// cooperative matrix multiply on the tensor cores; 0 emulates it with shuffles and half FMAs.
extern int sm86_lower_tensor_cores;
// sm86_lower with the tensor-core choice made by the caller (tensor 0 or 1 instead of sm86_lower_tensor_cores).
int sm86_lower_ex(const uint8_t *code, uint32_t code_size, const uint8_t *control, uint32_t control_size, Sm86Lowered *out,
                  int tensor);
void sm86_lower_free(Sm86Lowered *out);

// The control section's fields the host needs (offsets measured over the game's archives, tools/shader_census.py).
typedef struct {
    uint32_t size;             // the control section's own size (+0xd4; the archives repeat it at +0x30)
    uint32_t code_end;         // end of the instructions in the code block (+0x38), header included
    uint32_t c1_off, c1_len;   // the jump-table constants in the code block (+0x40, +0x3c)
    uint32_t code_size;        // the whole code block (+0x44)
    uint32_t stage;            // NVN's enum at byte +0x56: 0 vertex, 1 fragment, 2 geometry, 3 tess control,
                               // 4 tess eval, 5 compute, 6 task, 7 mesh
    uint32_t local[3];         // compute: the workgroup size (+0x58)
} Sm86Control;
// 0 when the magic or the bounds are wrong.
int sm86_control_parse(const uint8_t *control, uint32_t control_size, Sm86Control *c);
#define SM86_CONTROL_MAGIC 0x98761234u
#define SM86_CONTROL_MIN 0xe0u    // the fixed part; the jump-table records follow it
#define SM86_HEADER 0x80u         // the program header before a graphics stage's first instruction
// Task and mesh programs (docs/mesh.md): two pseudo instructions in Maxwell encodings no Maxwell instruction uses (top
// byte 0x6c and 0x6d; Ryujinx's InstTable has nothing there), which a translator patch decodes.
// SM86_ISBE_OP: the stage buffer, ISBERD or ISBEWR: bits 0-7 the register read into or written, 8-15 the address
// register, 16-19 the guard, 20-43 the byte offset, 44-47 the space (0xf ATTR, 7 TASK, 1 MAP.32, 0 MAP.U8), 48 a write.
// SM86_MESH_EPILOGUE: where every EXIT of the program branches (sm86_lower.c lower_exit), before the final EXIT: the
// translator emits the stage's end there (EmitMeshTasks for a task program, SetMeshOutputs and the output copy for a
// mesh program).
#define SM86_ISBE_OP 0x6c00000000000000ull
#define SM86_MESH_EPILOGUE 0x6d00000000070000ull
