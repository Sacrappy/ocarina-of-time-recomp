#!/usr/bin/env python3
# New for ocarina-of-time-recomp (MIT).
"""Translate every shader variant the game ships to SPIR-V ahead of time, offline, into the runtime's own shader-cache.

This is the "no translator at play" path: it discovers the NVN2 programs in the user's extracted RomFS
(tools/shader_census.py's inventory), lowers them with runtime/sm86_lower.c (tools/sm86_lower_main.c), sends the same
JSON jobs the runtime would send to build/xlate/oot_xlate.exe in batch mode, and writes the result into the layout
runtime/gfx_shader.c reads:

    <cache>/<cache key>.spv, <cache key>.res.json     the translation (raw translator output)
    <cache>/<use name>.use                            the code hash's use mask, so the runtime computes the cache key
    <cache>/in/<input id>.in                          the stored inputs, for gfx_shader_load_input as a fallback
    <records>/shader-answers.bin                      the variants, for gfx_shader_warm_list (priority 0)
    <records>/shader-prepared.bin                     the cached variants, for --warm-shaders (priority 1)

With those files in place the runtime's gfx_shader_get finds the mask and the key on disk and never starts a translator;
gfx_shader_load_input and the boot prewarm load the same entries by input id.

The key derivation is a Python mirror of runtime/gfx_shader.c: build_code_hash, pack_answers, mask_answers,
key_of_input and use_name, with the same KEY_VERSION/IN_VERSION/format constants. The job layout is
runtime/gfx_shader.c build_request and tools/xlate/Program.cs. Provenance: the NVN2 archive walk is
tools/shader_census.py's (MIT, from totk-pc-recomp/MacNativeGames); the lowering driver is tools/sm86_lower_main.c.

Everything written stays under the caller's build/profile directories (game-derived); no game data enters the tree.
Runs with Python 3.14 (compression.zstd), like the extraction tools: .venv-py314.

Examples (from the repository root):

    # every recorded variant + one default-answer variant of every program, into build/v131072/shader-cache
    python tools/prepare_all_shaders.py

    # a quick, partial check: 8 recorded variants, no census baseline
    python tools/prepare_all_shaders.py --no-baseline --limit 8

    # the identity the running game uses: read host options/tensor cores from its newest log
    python tools/prepare_all_shaders.py --log build/v131072/logs/save-test2.log

    # verify only: no translator, check every recorded variant is in the cache
    python tools/prepare_all_shaders.py --verify-only

    # a packaged build elsewhere (--romfs is the extracted RomFS, romfs.bin + index.json)
    python tools/prepare_all_shaders.py --build E:\\oot --out E:\\oot\\shader-cache --records E:\\oot \\
        --xlate E:\\oot\\oot_xlate.exe --romfs E:\\oot-extract\\romfs --log E:\\oot\\logs\\oot.log
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

# ---- constants mirrored from runtime/gfx_shader.c (bump both sides together)
CONTROL_MAGIC = b'\x34\x12\x76\x98'                       # 0x98761234, sm86_lower.h
CONTROL_BOUND = 0x400
KEY_VERSION = 17                                          # gfx_shader.c: in every cache key
IN_VERSION = 2                                            # gfx_shader.c: in every input id and .in file
USE_MAGIC, USE_VERSION = 0x4d554b54, 2                    # "TKUM", .use; version 2: sampler format query
IN_MAGIC = 0x49534b54                                     # "TKSI", .in files
ANS_MAGIC, ANS_VERSION, ANS_PACKED, ANS_REC = b'TOTKSANS', 1, 147, 216
PRG_MAGIC, PRG_VERSION = b'TOTKSPRG', 2
PRP_MAGIC, PRP_VERSION, PRP_REC = b'TOTKSPRP', 1, 36
PRG_HEAD = 12 + 32
ANSWERS_STRUCT = 152                                      # sizeof(GfxShaderAnswers) on x86-64 (padding included)

# GFX_STAGE_* (runtime/gfx.h)
VS, TCS, TES, GS, FS, CS, TS, MS = range(8)
STAGE_NAME = {VS: 'vs', FS: 'ps', CS: 'cs', TS: 'ts', MS: 'ms'}
NVN_TO_GFX = {0: VS, 1: FS, 2: GS, 3: TCS, 4: TES, 5: CS, 6: TS, 7: MS}
# gfx_shader.c tex_names / fmt_names
TEX_NAMES = [None, '1d', '1darray', 'buffer', '2d', '2darray', '2dms', '2dmsarray', '3d', 'cube', 'cubearray']
FMT_NAMES = [None, 'R8Unorm', 'R8Snorm', 'R8Uint', 'R8Sint', 'R16Float', 'R16Unorm', 'R16Snorm', 'R16Uint',
             'R16Sint', 'R32Float', 'R32Uint', 'R32Sint', 'R8G8Unorm', 'R8G8Snorm', 'R8G8Uint', 'R8G8Sint',
             'R16G16Float', 'R16G16Unorm', 'R16G16Snorm', 'R16G16Uint', 'R16G16Sint', 'R32G32Float', 'R32G32Uint',
             'R32G32Sint', 'R8G8B8A8Unorm', 'R8G8B8A8Snorm', 'R8G8B8A8Uint', 'R8G8B8A8Sint', 'R16G16B16A16Float',
             'R16G16B16A16Unorm', 'R16G16B16A16Snorm', 'R16G16B16A16Uint', 'R16G16B16A16Sint',
             'R32G32B32A32Float', 'R32G32B32A32Uint', 'R32G32B32A32Sint', 'R10G10B10A2Unorm', 'R10G10B10A2Uint',
             'R11G11B10Float']
DEFAULT_CS_SHARED = 0xc000                               # runtime/vk_exec_draw.c answers_for: every compute program


def log(msg):
    print(msg, flush=True)


def u16(b, o):
    return struct.unpack_from('<H', b, o)[0]


def u32(b, o):
    return struct.unpack_from('<I', b, o)[0]


def sha256(b):
    return hashlib.sha256(b).digest()


def hex32(b):
    return b.hex()


def u64(b, o):
    return struct.unpack_from('<Q', b, o)[0]


# ---- the read-only cache pack (<cache>/shader-cache.pack, tools/pack_shader_cache.py)
# "TKSPACK1", u32 count, u32 0, then 48-byte entries {name[32], kind, size, offset} sorted by (kind, name), then data.
# The runtime reads the pack first and the loose files only for a key it does not hold (gfx_shader.c pack_find).
PACK_SPV, PACK_RES, PACK_USE, PACK_IN = 1, 2, 3, 4
_packs = {}


class Pack:
    def __init__(self, path):
        self.f = open(path, 'rb')
        self.ix = {}
        head = self.f.read(16)
        if len(head) != 16 or head[:8] != b'TKSPACK1':
            raise ValueError('not a shader cache pack')
        n = u32(head, 8)
        if n > 1 << 24:
            raise ValueError('pack index too large')
        self.f.seek(16)
        ix = self.f.read(n * 48)
        if len(ix) != n * 48:
            raise ValueError('short pack index')
        for i in range(n):
            e = ix[i * 48:(i + 1) * 48]
            self.ix[(u32(e, 32), e[:32])] = (u64(e, 40), u32(e, 36))

    def read(self, kind, name):
        e = self.ix.get((kind, name))
        if not e or not e[1]:
            return None
        self.f.seek(e[0])
        b = self.f.read(e[1])
        return b if len(b) == e[1] else None

    def has(self, kind, name):
        return (kind, name) in self.ix


def pack_of(cache):
    cache = Path(cache).resolve()
    if cache not in _packs:
        p = cache / 'shader-cache.pack'
        try:
            _packs[cache] = Pack(p) if p.is_file() else None
        except (OSError, ValueError):
            _packs[cache] = None
    return _packs[cache]


def write_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.aot-tmp')
    tmp.write_bytes(data)
    os.replace(tmp, path)


def host_path(p):
    """The path as the Windows translator sees it under WSL (tools/shader_census.py host_path)."""
    s = str(p)
    m = re.match(r'^/mnt/([a-z])/(.*)$', s)
    return f'{m.group(1).upper()}:\\{m.group(2).replace("/", "\\")}' if m else s


# ---- the control section and the program bytes (runtime/sm86_lower.c sm86_control_parse reads the same fields)

def parse_control(control):
    if len(control) < 0xe0 or control[:4] != CONTROL_MAGIC:
        raise ValueError('not an NVN2 control section')
    size = u32(control, 0xd4)
    # sm86_control_parse: a size below the fixed part or beyond CONTROL_BOUND (or beyond the bytes we hold) is
    # replaced by the fixed part. Compute programs seen in play carry values above it (0x768, 0x1970), and the
    # runtime hashes/clamps to 0xe0; the archives' graphics programs carry their real size.
    if size < 0xe0 or size > CONTROL_BOUND or size > len(control):
        size = 0xe0
    ctl = {
        'size': size,
        'code_end': u32(control, 0x38),
        'c1_len': u32(control, 0x3c),
        'c1_off': u32(control, 0x40),
        'code_size': u32(control, 0x44),
        'stage': control[0x56],
        'local': [u32(control, 0x58 + 4 * k) for k in range(3)],
        'mesh_threads': u16(control, 0x58),
        'payload': control[0x5f] * 1024,
        'max_vertices': u32(control, 0x60) & 0xffff,
        'max_primitives': u32(control, 0x60) >> 16,
    }
    if not 0 < ctl['code_end'] <= ctl['c1_off'] <= ctl['c1_off'] + ctl['c1_len'] <= ctl['code_size']:
        raise ValueError(f'invalid code bounds {ctl}')
    return ctl


def stage_of(control):
    if len(control) < 0xe0:
        raise ValueError('not an NVN2 control section')
    nvn = control[0x56]
    if nvn not in NVN_TO_GFX:
        raise ValueError(f'unknown NVN stage {nvn}')
    return NVN_TO_GFX[nvn]


# ---- key derivation (runtime/gfx_shader.c build_code_hash, pack_answers, mask_answers, key_of_input, use_name)

def code_hash(stage, control, code):
    """sha256 over stage, the control section's own size and bytes, code_size and the code block."""
    return sha256(struct.pack('<II', stage, len(control)) + control +
                  struct.pack('<Q', len(code)) + code)


def pack_answers_fields(tex, img, fmt, attr, fout, local, shared, depth):
    """The 147-byte fixed layout of pack_answers (no struct padding)."""
    o = bytearray(ANS_PACKED)
    o[0:32] = tex
    o[32:64] = img
    o[64:96] = fmt
    o[96:128] = attr
    o[128:136] = fout
    for k in range(3):
        struct.pack_into('<H', o, 136 + 2 * k, local[k] & 0xffff)
    struct.pack_into('<I', o, 142, shared)
    o[146] = depth & 0xff
    return bytes(o)


def default_answers(stage, ctl):
    """The answers vk_exec_draw.c answers_for gives a stage with no recorded variant: zero, except the workgroup
    size of a compute program and of a task/mesh program (the control section's +0x58) and, for compute only,
    the shared-memory size (0xc000 for every kernel)."""
    if stage not in (CS, TS, MS):
        return pack_answers_fields(bytes(32), bytes(32), bytes(32), bytes(32), bytes(8), (0, 0, 0), 0, 0)
    local = tuple(v or 1 for v in ctl['local'])
    return pack_answers_fields(bytes(32), bytes(32), bytes(32), bytes(32), bytes(8), local,
                               DEFAULT_CS_SHARED if stage == CS else 0, 0)


def mask_answers(a, mask):
    """pack_answers of the answers with everything outside the use mask zeroed (mask_answers)."""
    slots, attrs, fouts = mask
    o = bytearray(ANS_PACKED)
    for i in range(32):
        if slots >> i & 1:
            o[i] = a[i]
            o[32 + i] = a[32 + i]
            o[64 + i] = a[64 + i]
        if attrs >> i & 1:
            o[96 + i] = a[96 + i]
    for i in range(8):
        if fouts >> i & 1:
            o[128 + i] = a[128 + i]
    o[136:142] = a[136:142]       # local
    o[142:146] = a[142:146]       # shared_bytes
    o[146] = 0                    # depth_minus_w: mask_answers leaves it zero
    return bytes(o)


def input_id(ch, a, mask):
    return sha256(b'TKSI' + struct.pack('<I', IN_VERSION) + ch + mask_answers(a, mask))


def key_of_input(iid, identity):
    return sha256(b'TKSK' + struct.pack('<I', KEY_VERSION) + identity + iid)


def provisional_key(ch, a, identity):
    """build_key with no mask: full answers (runtime/gfx_shader.c "TKSP")."""
    return sha256(b'TKSP' + struct.pack('<I', KEY_VERSION) + identity + ch + a)


def use_name(ch, identity):
    return sha256(b'TKSU' + identity + ch)


def answers_struct(a):
    """The 152-byte in-memory GfxShaderAnswers in .in files (padding between local and shared_bytes included)."""
    o = bytearray(ANSWERS_STRUCT)
    o[0:142] = a[0:142]
    struct.pack_into('<I', o, 144, u32(a, 142))
    o[148] = a[146]
    return bytes(o)


# ---- SPIR-V reflection (runtime/gfx_shader.c reflect_spirv, the parts the use mask needs)

def spirv_complete(spv):
    if len(spv) < 20 or len(spv) % 4 or spv[:4] != b'\x03\x02\x23\x07':
        return False
    w = struct.unpack('<%dI' % (len(spv) // 4), spv)
    i = 5
    while i < len(w):
        n = w[i] >> 16
        if not n:
            return False
        i += n
    return i == len(w)


def spirv_io(spv):
    """(input Locations mask, output Locations mask) as reflect_spirv builds them."""
    w = struct.unpack('<%dI' % (len(spv) // 4), spv)
    n, bound = len(w), min(w[3], 1 << 20)
    loc = [0] * bound
    packed = bytearray(bound)
    ttype = bytearray(bound)
    tcomps = bytearray(bound)
    pointee = [0] * bound
    cval = [0] * bound
    alen = [0] * bound
    in_mask = out_mask = 0
    i = 5
    while i < n:
        op, ln = w[i] & 0xffff, w[i] >> 16
        if not ln or i + ln > n:
            break
        a = w[i + 1:i + ln]
        rid = a[0] if ln > 1 else 0
        if op == 71 and ln >= 4 and rid < bound:                 # OpDecorate
            if a[1] == 30:                                       # Location
                loc[rid] = a[2] + 1
            elif a[1] == 31:                                     # Component
                packed[rid] = 1
        elif op == 21 and ln >= 4 and rid < bound:               # OpTypeInt
            ttype[rid] = 1 if a[2] else 2
            tcomps[rid] = 1
        elif op == 22 and ln >= 3 and rid < bound:               # OpTypeFloat
            ttype[rid] = 0
            tcomps[rid] = 1
        elif op == 23 and ln >= 4 and rid < bound and a[1] < bound:   # OpTypeVector
            ttype[rid] = ttype[a[1]]
            tcomps[rid] = a[2] & 0xff
        elif op == 43 and ln >= 4 and a[1] < bound:              # OpConstant
            cval[a[1]] = a[2]
        elif op == 28 and ln >= 4 and rid < bound and a[1] < bound:   # OpTypeArray
            ttype[rid] = ttype[a[1]]
            tcomps[rid] = tcomps[a[1]]
            alen[rid] = cval[a[2]] if a[2] < bound else 0
        elif op == 32 and ln >= 4 and rid < bound:               # OpTypePointer
            pointee[rid] = a[2]
        elif op == 59 and ln >= 4:                               # OpVariable (result type, result id, storage)
            typ, var, sc = a[0], a[1], a[2]
            if sc in (1, 3) and var < bound and typ < bound and 0 < loc[var] <= 32:
                l = loc[var] - 1
                t = pointee[typ] if pointee[typ] < bound else 0
                span = alen[t] if alen[t] else 1
                for k in range(span):
                    if l + k < 32:
                        if sc == 1:
                            in_mask |= 1 << (l + k)
                        else:
                            out_mask |= 1 << (l + k)
        i += ln
    return in_mask, out_mask


def reflection_valid(res):
    if b'\0' in res:
        return False
    s = res.strip()
    if len(s) < 2 or s[:1] != b'{' or s[-1:] != b'}':
        return False
    if b'"stage"' not in s:
        return False
    return any(x in s for x in (b'"stage": "vs"', b'"stage": "ps"', b'"stage": "cs"',
                                b'"stage":"vs"', b'"stage":"ps"', b'"stage":"cs"',
                                b'"stage": "ts"', b'"stage": "ms"', b'"stage":"ts"', b'"stage":"ms"'))


def use_mask_of(res, spv, stage):
    """The UseMask the runtime derives from a translation (gfx_shader.c use_of)."""
    obj = json.loads(res)
    slots = 0
    for s in obj.get('tex_queried', []) + obj.get('img_queried', []):
        if 0 <= s < 32:
            slots |= 1 << s
    in_mask, out_mask = spirv_io(spv)
    attrs = in_mask if stage == VS else 0
    fouts = (out_mask & 0xff) if stage == FS else 0
    return slots, attrs, fouts


# ---- translator identity (runtime/gfx_shader.c xlate_identity, gfx_shader_set_host, gfx_shader_set_tensor)

def base_identity(xlate, override):
    if override:
        return bytes.fromhex(override)
    p = Path(xlate)
    idp = p.with_suffix('.id')
    if idp.is_file():
        text = idp.read_text(encoding='ascii', errors='replace').strip()
        if len(text) == 64:
            try:
                return bytes.fromhex(text)
            except ValueError:
                pass
    try:
        return sha256(p.read_bytes())
    except OSError:
        raise SystemExit(f'no translator identity: {xlate} and {idp} are missing (pass --xlate-id)')


def mixed_identity(xlate, override, f64, sbuf, subgroup, tensor):
    ident = base_identity(xlate, override)
    if not (f64 and sbuf == 0 and subgroup == (32, 32, 32)):
        text = f'TKHO f64={1 if f64 else 0} sbuf={sbuf} sg={subgroup[0]},{subgroup[1]},{subgroup[2]}'
        ident = sha256(ident + text.encode())
    if tensor:
        ident = sha256(ident + b'TKTC tensor=1')
    return ident


def identity_from_log(path):
    """Host options and tensor cores from a run log. The log prints only 8 hex digits of the identity, but the
    options are printed in full: 'gfx_shader: host options f64=1 sbuf=16 sg=32,32,32' and 'HMMA runs on ...'."""
    text = Path(path).read_text(encoding='utf-8', errors='replace')
    opts = {'f64': None, 'sbuf': None, 'sg': None, 'tensor': False, 'prefix': None}
    for m in re.finditer(r'gfx_shader: host options f64=(\d+) sbuf=(\d+) sg=(\d+),(\d+),(\d+)', text):
        opts = {'f64': int(m.group(1)), 'sbuf': int(m.group(2)),
                'sg': (int(m.group(3)), int(m.group(4)), int(m.group(5))), 'tensor': False,
                'prefix': opts['prefix']}
    if 'HMMA runs on cooperative matrices' in text:
        opts['tensor'] = True
    m = None
    for m in re.finditer(r'gfx_shader: translator identity ([0-9a-f]{8})', text):
        pass
    if m:
        opts['prefix'] = m.group(1)
    return opts


# ---- programs from the RomFS (tools/shader_census.py inventory) and from shader-programs.bin

class Program:
    __slots__ = ('code_hash', 'stage', 'control', 'code', 'ctl', 'local', 'source')

    def __init__(self, stage, control, code, local, source, ch=None):
        self.stage = stage
        self.ctl = parse_control(control)
        self.control = control[:self.ctl['size']]
        self.code = code[:self.ctl['code_size']]
        self.local = local
        self.source = source
        self.code_hash = code_hash(stage, self.control, self.code)
        if ch is not None and ch != self.code_hash:
            raise ValueError('record does not hash to its stored code hash')


def load_recorded_programs(path):
    """shader-programs.bin: stage, length, control size, code hash, control, code (gfx_shader.c prg_walk).
    A torn record at the end (the game appends these while it runs) is ignored, not fatal."""
    b = Path(path).read_bytes()
    if b[:8] != PRG_MAGIC:
        raise ValueError(f'{path}: not a shader programs file')
    ver, rec = struct.unpack_from('<II', b, 8)
    if ver != PRG_VERSION:
        raise ValueError(f'{path}: programs version {ver}, this tool reads {PRG_VERSION}')
    out, o = {}, 16
    while len(b) - o >= PRG_HEAD:
        st, ln, cs = struct.unpack_from('<III', b, o)
        h = b[o + 12:o + 44]
        if ln > 64 << 20 or not 0xe0 <= cs <= CONTROL_BOUND or o + PRG_HEAD + ln + cs > len(b):
            break
        control = b[o + 44:o + 44 + cs]
        code = b[o + 44 + cs:o + 44 + cs + ln]
        try:
            p = Program(st, control, code, tuple(parse_control(control)['local']), path, ch=h)
        except ValueError:
            break
        out[p.code_hash] = p
        o += PRG_HEAD + ln + cs
    return out


def load_inventory(romfs):
    """Every unique program in the extracted RomFS, keyed by the runtime's code hash."""
    import shader_census as sc       # needs Python 3.14 (compression.zstd); imported only when the RomFS is read
    t0 = time.monotonic()
    shaders, visited = sc.inventory(Path(romfs))
    out = {}
    for key, s in shaders.items():
        control, code = s['control'], s['code']
        try:
            stage = stage_of(control)
            p = Program(stage, control, code, tuple(parse_control(control)['local']), s.get('source', key))
        except ValueError:
            continue
        out[p.code_hash] = p
    log(f'census: {len(out)} programs from {len(visited)} files in {time.monotonic() - t0:.1f} s')
    return out


def load_census_dir(census, programs):
    """A census directory (programs.json + ctl|control/ + code/) as a fallback program source."""
    ctl_dir = census / 'control' if (census / 'control').is_dir() else census / 'ctl'
    code_dir = census / 'code'
    table = json.loads((census / 'programs.json').read_text(encoding='utf-8'))
    added = 0
    for key, e in table.items():
        stage_name = e.get('stage')
        if stage_name not in ('vs', 'ps', 'cs', 'ts', 'ms'):
            continue
        cp, kp = ctl_dir / f'{key}.ctl', code_dir / f'{key}.code'
        if not cp.is_file() or not kp.is_file():
            continue
        control, code = cp.read_bytes(), kp.read_bytes()
        try:
            stage = stage_of(control)
            p = Program(stage, control, code, tuple(parse_control(control)['local']), key)
        except ValueError:
            continue
        if p.code_hash not in programs:
            programs[p.code_hash] = p
            added += 1
    return added


# ---- answer records (shader-answers.bin and seed/list files)

class Variant:
    __slots__ = ('stage', 'code_hash', 'iid', 'answers', 'source')

    def __init__(self, stage, ch, iid, answers, source):
        self.stage = stage
        self.code_hash = ch
        self.iid = iid                      # recorded input id, or None for a seed/default variant
        self.answers = answers              # 147-byte pack_answers layout
        self.source = source


def load_answers_file(path):
    b = Path(path).read_bytes()
    if b[:8] != ANS_MAGIC:
        raise ValueError(f'{path}: not a shader answers file')
    ver, rec = struct.unpack_from('<II', b, 8)
    if ver != ANS_VERSION or rec != ANS_REC:
        raise ValueError(f'{path}: answers version {ver}, record size {rec}; this tool reads {ANS_VERSION}/{ANS_REC}')
    out = []
    for o in range(16, len(b) - ANS_REC + 1, ANS_REC):
        r = b[o:o + ANS_REC]
        stage = u32(r, 0)
        iid = r[36:68]
        out.append(Variant(stage, r[4:36], None if iid == bytes(32) else iid, r[68:68 + ANS_PACKED], str(path)))
    return out


def read_answers_file(path):
    try:
        b = Path(path).read_bytes()
    except FileNotFoundError:
        return []
    if len(b) < 16 or b[:8] != ANS_MAGIC:
        return []
    ver, rec = struct.unpack_from('<II', b, 8)
    if ver != ANS_VERSION or rec != ANS_REC:
        return []
    return [b[o:o + ANS_REC] for o in range(16, len(b) - ANS_REC + 1, ANS_REC)]


def merge_answers(path, new_records):
    """Union by input id, keeping existing records (play may have appended some) and the first of duplicates."""
    recs = read_answers_file(path)
    seen = {r[36:68] for r in recs}
    added = 0
    for st, ch, iid, a, pad in new_records:
        if iid in seen:
            continue
        seen.add(iid)
        recs.append(struct.pack('<I', st) + ch + iid + a + bytes([pad]))
        added += 1
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.aot-tmp')
    with open(tmp, 'wb') as f:
        f.write(ANS_MAGIC + struct.pack('<II', ANS_VERSION, ANS_REC))
        for r in recs:
            f.write(r)
    os.replace(tmp, path)
    return added, len(recs)


def read_prepared(path):
    try:
        b = Path(path).read_bytes()
    except FileNotFoundError:
        return []
    if len(b) < 16 or b[:8] != PRP_MAGIC:
        return []
    ver, rec = struct.unpack_from('<II', b, 8)
    if ver != PRP_VERSION or rec != PRP_REC:
        return []
    return [b[o:o + PRP_REC] for o in range(16, len(b) - PRP_REC + 1, PRP_REC)]


def merge_prepared(path, entries):
    """entries: (stage, iid). The runtime's shader-prepared.bin, one record per input id."""
    recs = read_prepared(path)
    seen = {r[4:36] for r in recs}
    added = 0
    for st, iid in entries:
        if iid in seen:
            continue
        seen.add(iid)
        recs.append(bytes([st & 0xff, 1, 0, 0]) + iid)       # priority 1: prepared, not met in play
        added += 1
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.aot-tmp')
    with open(tmp, 'wb') as f:
        f.write(PRP_MAGIC + struct.pack('<II', PRP_VERSION, PRP_REC))
        for r in recs:
            f.write(r)
    os.replace(tmp, path)
    return added, len(recs)


# ---- lowering (runtime/sm86_lower.c through tools/sm86_lower_main.c)

LOWER_SOURCES = [ROOT / 'runtime' / 'sm86_lower.c', ROOT / 'tools' / 'sm86_lower_main.c']


def build_lowerer(work):
    work.mkdir(parents=True, exist_ok=True)
    exe = work / ('sm86_lower.exe' if os.name == 'nt' else 'sm86_lower')
    stamp = work / 'sm86_lower.sha256'
    h = sha256(b''.join(p.read_bytes() for p in LOWER_SOURCES)).hex()
    if exe.is_file() and stamp.is_file() and stamp.read_text() == h:
        return exe, h
    cc = [r'C:\msys64\ucrt64\bin\clang.exe'] if os.name == 'nt' else ['cc']
    cmd = cc + ['-std=c11', '-O2', '-Wall', '-Wextra', '-I', str(ROOT / 'runtime'), '-o', str(exe)] + \
        [str(p) for p in LOWER_SOURCES]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f'building the lowering helper failed:\n{r.stderr}')
    work.mkdir(parents=True, exist_ok=True)
    stamp.write_text(h)
    return exe, h


def lower_programs(work, programs, tensor):
    """Lower the given programs in one batch; returns ({code hash: (code path, c1)}, {code hash: message})."""
    exe, source_stamp = build_lowerer(work)
    stamp = source_stamp + (':tensor=1' if tensor else ':tensor=0')
    prog_dir, low_dir = work / 'programs', work / 'lowered'
    prog_dir.mkdir(parents=True, exist_ok=True)
    low_dir.mkdir(parents=True, exist_ok=True)
    stamp_file = low_dir / 'stamp.txt'
    if stamp_file.is_file() and stamp_file.read_text() != stamp:
        for p in low_dir.glob('*'):
            if p.name != 'stamp.txt':
                p.unlink()
    stamp_file.write_text(stamp)
    lines, pending = [], {}
    low_fail = {}
    for ch, p in programs.items():
        h = hex32(ch)
        oc, o1 = low_dir / f'{h}.code', low_dir / f'{h}.c1'
        if oc.is_file() and o1.is_file():
            pending[ch] = (oc, o1)
            continue
        cp, kp = prog_dir / f'{h}.ctl', prog_dir / f'{h}.code'
        if not cp.is_file() or cp.read_bytes() != p.control:
            write_atomic(cp, p.control)
        if not kp.is_file() or kp.read_bytes() != p.code:
            write_atomic(kp, p.code)
        lines.append(f'{h} {STAGE_NAME.get(p.stage, "?")} {cp} {kp} {oc} {o1}')
    if lines:
        env = dict(os.environ)
        env['SM86_TENSOR'] = '1' if tensor else '0'
        r = subprocess.run([str(exe), '--batch'], input='\n'.join(lines) + '\n', capture_output=True, text=True,
                           env=env)
        for line in r.stdout.splitlines():
            parts = line.split(' ', 3)
            if parts[0] == 'ok':
                h = bytes.fromhex(parts[1])
                pending[h] = (low_dir / f'{parts[1]}.code', low_dir / f'{parts[1]}.c1')
            elif parts[0] == 'fail' and len(parts) == 4:
                try:
                    low_fail[bytes.fromhex(parts[1])] = f'{parts[3]} (at +{parts[2]})'
                except ValueError:
                    pass
        if r.returncode:
            log(f'lowering helper rc={r.returncode}: {r.stderr.strip()[:400]}')
    ok, failures = {}, {}
    for ch in programs:
        h = hex32(ch)
        oc, o1 = pending.get(ch, (low_dir / f'{h}.code', low_dir / f'{h}.c1'))
        if oc.is_file() and o1.is_file():
            ok[ch] = (oc, o1)
        else:
            failures[ch] = low_fail.get(ch, 'not lowered')
    return ok, failures


# ---- cache reading and writing (runtime/gfx_shader.c disk_load / use_path / in_path)

def read_mask_file(cache, ch, identity):
    name = use_name(ch, identity)
    p = pack_of(cache)
    d = p.read(PACK_USE, name) if p else None
    if d is None:
        path = cache / (hex32(name) + '.use')
        d = path.read_bytes() if path.is_file() else None
    if not d or len(d) != 20 or u32(d, 0) != USE_MAGIC or u32(d, 4) != USE_VERSION:
        return None
    return u32(d, 8), u32(d, 12), u32(d, 16)


def loose_pair_valid(cache, key):
    h = hex32(key)
    spv_p, res_p = cache / f'{h}.spv', cache / f'{h}.res.json'
    if not spv_p.is_file() or not res_p.is_file():
        return False
    return spirv_complete(spv_p.read_bytes()) and reflection_valid(res_p.read_bytes())


def cache_entry_valid(cache, key):
    p = pack_of(cache)
    if p:
        spv, res = p.read(PACK_SPV, key), p.read(PACK_RES, key)
        if spv and res and spirv_complete(spv) and reflection_valid(res):
            return True
    return loose_pair_valid(cache, key)


def write_in_file(cache, iid, stage, answers, control, code, ctl_size):
    hdr = struct.pack('<6I', IN_MAGIC, IN_VERSION, stage, len(code), ctl_size, ANSWERS_STRUCT)
    path = in_path(cache, iid)
    if not in_valid(cache, iid):
        write_atomic(path, hdr + answers_struct(answers) + control[:ctl_size] + code)


def write_cache_entry(cache, key, ch, identity, iid, stage, answers, mask, control, code, ctl_size, spv, res):
    h = hex32(key)
    write_atomic(cache / f'{h}.spv', spv)
    write_atomic(cache / f'{h}.res.json', res)
    # The .use file is the lookup that turns a code hash into the final key; write it after the pair.
    write_atomic(cache / (hex32(use_name(ch, identity)) + '.use'),
                 struct.pack('<5I', USE_MAGIC, USE_VERSION, mask[0], mask[1], mask[2]))
    write_in_file(cache, iid, stage, answers, control, code, ctl_size)


def in_valid(cache, iid, ch=None):
    """The .in file's current 24-byte header and body (gfx_shader.c in_load); optionally its code hash."""
    p = pack_of(cache)
    d = p.read(PACK_IN, iid) if p else None
    if d is None:
        q = in_path(cache, iid)
        d = q.read_bytes() if q.is_file() else None
    if not d or len(d) < 24:
        return False
    magic, ver, stage, length, ctl_size, asz = struct.unpack_from('<6I', d, 0)
    if magic != IN_MAGIC or ver != IN_VERSION or asz != ANSWERS_STRUCT:
        return False
    if not 0xe0 <= ctl_size <= CONTROL_BOUND or len(d) != 24 + asz + ctl_size + length:
        return False
    control = d[24 + asz:24 + asz + ctl_size]
    code = d[24 + asz + ctl_size:]
    try:
        ctl = parse_control(control)
    except ValueError:
        return False
    if ctl['size'] > ctl_size or ctl['code_size'] > length:
        return False
    return ch is None or code_hash(stage, control[:ctl['size']], code[:ctl['code_size']]) == ch


def in_path(cache, iid):
    return Path(cache) / 'in' / (hex32(iid) + '.in')


def in_present(cache, iid, ch=None):
    return in_valid(cache, iid, ch)


# ---- job building (runtime/gfx_shader.c build_request)

def build_job(key, stage, c1, a, ctl, code_path, code, f64, sbuf, subgroup):
    job = {'key': hex32(key), 'stage': STAGE_NAME[stage], 'debug': False,
           'code': host_path(code_path), 'c1': c1.hex()}
    tex, img, attr, fout = {}, {}, {}, {}
    for i in range(32):
        t = a[i] & 0xf
        if t and t < len(TEX_NAMES):
            tex[str(i)] = TEX_NAMES[t] + ('_uint' if a[i] & 0x10 else '_sint' if a[i] & 0x20 else '')
        t = a[32 + i]
        if t and t < len(TEX_NAMES):
            f = FMT_NAMES[a[64 + i]] if 0 < a[64 + i] < len(FMT_NAMES) else 'R8G8B8A8Unorm'
            img[str(i)] = {'type': TEX_NAMES[t], 'fmt': f}
    for i in range(32):
        if a[96 + i]:
            attr[str(i)] = 'sint' if a[96 + i] == 1 else 'uint'
    for i in range(8):
        if a[128 + i]:
            fout[str(i)] = 'sint' if a[128 + i] == 1 else 'uint'
    job.update(tex=tex, img=img, attr=attr, fout=fout, shared=u32(a, 142))
    if stage == CS:
        local = [u16(a, 136 + 2 * k) for k in range(3)]
        if not any(local):
            local = [v or 1 for v in ctl['local']]
        job['local'] = local
    if stage in (TS, MS):
        job.update(local=[ctl['mesh_threads'] or 32, 1, 1], maxv=ctl['max_vertices'],
                   maxp=ctl['max_primitives'], payload=ctl['payload'],
                   omap=code[0x34:0x34 + 16].hex())
    if not f64:
        job['softfloat64'] = True
    if sbuf:
        job['sbuf_align'] = sbuf
    si = 0 if stage == VS else 2 if stage == CS else 1
    if subgroup[si] != 32:
        job['subgroup'] = subgroup[si]
    return job


# ---- translation

def translate_jobs(xlate, work, jobs, parallel, retries):
    """Run oot_xlate.exe's batch mode over the jobs on `parallel` processes. Returns the directories the outputs are
    in and the set of keys that produced valid output."""
    out_dir = work / 'translated'
    out_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    if not jobs:
        return out_dir, results
    jobs = sorted(jobs, key=lambda j: len(j['c1']), reverse=True)
    n = max(1, min(parallel, len(jobs)))
    chunks = [jobs[i::n] for i in range(n)]
    procs = []
    for i, chunk in enumerate(chunks):
        p = out_dir / f'jobs-{i}.json'
        p.write_text(json.dumps(chunk))
        for j in chunk:                       # never pick up a stale file if the process dies
            for ext in ('.spv', '.res.json'):
                q = out_dir / (j['key'] + ext)
                if q.exists():
                    q.unlink()
        err = open(out_dir / f'jobs-{i}.stderr.log', 'w', encoding='utf-8', errors='replace')
        procs.append((subprocess.Popen([str(xlate), str(p), str(out_dir)], stdout=subprocess.PIPE,
                                       stderr=err, text=True), err, chunk))
    for proc, err, chunk in procs:
        proc.wait()
        err.close()
    for attempt in range(retries + 1):
        missing = [j for j in jobs if j['key'] not in results]
        for j in missing:
            spv_p = out_dir / (j['key'] + '.spv')
            res_p = out_dir / (j['key'] + '.res.json')
            if not spv_p.is_file() or not res_p.is_file():
                continue
            spv, res = spv_p.read_bytes(), res_p.read_bytes()
            if spirv_complete(spv) and reflection_valid(res):
                results[j['key']] = (spv, res)
        if attempt < retries and len(results) < len(jobs):
            missing = [j for j in jobs if j['key'] not in results]
            log(f'translator: {len(missing)} jobs produced no valid output; retrying them')
            p = out_dir / 'jobs-retry.json'
            p.write_text(json.dumps(missing))
            for j in missing:
                for ext in ('.spv', '.res.json'):
                    q = out_dir / (j['key'] + ext)
                    if q.exists():
                        q.unlink()
            r = subprocess.run([str(xlate), str(p), str(out_dir)], capture_output=True, text=True)
            if r.returncode:
                log(f'translator retry rc={r.returncode}: {r.stderr.strip()[:400]}')
    return out_dir, results


# ---- verification

def collect_use_masks(cache):
    """Every use mask in a cache (loose .use files and the pack's PACK_USE entries), for --self-test."""
    masks = set()
    for p in Path(cache).glob('*.use'):
        d = p.read_bytes()
        if len(d) == 20 and u32(d, 0) == USE_MAGIC and u32(d, 4) == USE_VERSION:
            masks.add((u32(d, 8), u32(d, 12), u32(d, 16)))
    p = pack_of(cache)
    if p:
        for (kind, name) in p.ix:
            if kind != PACK_USE:
                continue
            d = p.read(kind, name)
            if d and len(d) == 20 and u32(d, 0) == USE_MAGIC and u32(d, 4) == USE_VERSION:
                masks.add((u32(d, 8), u32(d, 12), u32(d, 16)))
    return masks


def self_test(cache, variants, limit):
    """Reproduce recorded input ids from the cache's own use masks: the key math against the runtime's output."""
    masks = collect_use_masks(cache)
    if not masks:
        log('self-test: no .use files in the cache; nothing to compare against')
        return 0, 0
    vs = [v for v in variants if v.iid is not None][:limit or None]
    ok = bad = 0
    for v in vs:
        if any(input_id(v.code_hash, v.answers, m) == v.iid for m in masks):
            ok += 1
        else:
            bad += 1
    log(f'self-test: {ok} of {ok + bad} recorded input ids reproduced from {len(masks)} use masks'
        + (f'; {bad} differ (a record from another build, or a mask the cache no longer holds)' if bad else ''))
    return ok, bad


def find_spirv_val(explicit):
    if explicit:
        return explicit if Path(explicit).is_file() else None
    for name in ('spirv-val', 'spirv-val.exe'):
        p = shutil.which(name)
        if p:
            return Path(p)
    sdk = os.environ.get('VULKAN_SDK')
    cands = []
    if sdk:
        cands.append(Path(sdk) / 'Bin' / 'spirv-val.exe')
    cands += sorted(Path(r'C:\VulkanSDK').glob('*/Bin/spirv-val.exe'), reverse=True)
    for c in cands:
        if c.is_file():
            return c
    return None


# ---- planning

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--version', default='v131072', help='build/extracted/<version> (default v131072)')
    ap.add_argument('--romfs', type=Path, help='extracted RomFS (default build/extracted/<version>/romfs)')
    ap.add_argument('--build', type=Path, help='the oot.exe directory (default build/<version>)')
    ap.add_argument('--out', type=Path, help='shader cache directory (default <build>/shader-cache)')
    ap.add_argument('--records', type=Path, help='where shader-answers.bin/shader-prepared.bin live (default <build>)')
    ap.add_argument('--work', type=Path, help='scratch and lowering cache (default <build>/shader-aot)')
    ap.add_argument('--xlate', type=Path, help='the translator (default build/xlate/oot_xlate.exe)')
    ap.add_argument('--answers', type=Path, action='append', default=[],
                    help='an answers file to translate (repeatable; default <records>/shader-answers.bin)')
    ap.add_argument('--no-recorded', action='store_true', help='do not read <records>/shader-answers.bin')
    ap.add_argument('--no-baseline', action='store_true', help="do not add a default-answer variant of every program")
    ap.add_argument('--mesh', choices=('on', 'off'), default='on',
                    help='include task/mesh programs (default on; they need a translator built with patch 0021)')
    ap.add_argument('--stage', action='append', choices=('vs', 'ps', 'cs', 'ts', 'ms'), help='only these stages')
    ap.add_argument('--limit', type=int, help='translate at most N variants (for a smoke test)')
    ap.add_argument('--jobs', type=int, default=min(8, os.cpu_count() or 4), help='translator processes (default 8)')
    ap.add_argument('--retries', type=int, default=1, help='retries for jobs without valid output (default 1)')
    ap.add_argument('--xlate-id', help='64 hex digits: override the translator identity (OOT_XLATE_ID must match)')
    ap.add_argument('--sbuf-align', type=int, help='minStorageBufferOffsetAlignment answer (default 0)')
    ap.add_argument('--no-float64', action='store_true', help='the device has no shaderFloat64')
    ap.add_argument('--subgroup', help='translator subgroup sizes VS,FS,CS (default 32,32,32)')
    ap.add_argument('--tensor', choices=('on', 'off'), help='SM86 HMMA on cooperative matrices (default off)')
    ap.add_argument('--log', type=Path, help='a run log to take host options/tensor cores from (default: newest)')
    ap.add_argument('--no-log', action='store_true', help='do not read a run log for the identity options')
    ap.add_argument('--print-identity', action='store_true', help='print the identity and exit')
    ap.add_argument('--verify', action='store_true', help='after writing, verify every variant is loadable')
    ap.add_argument('--verify-only', action='store_true', help='verify without translating')
    ap.add_argument('--self-test', action='store_true',
                    help='check the key math against the existing cache and answers records, then continue')
    ap.add_argument('--spirv-val', metavar='PATH', help='spirv-val to use (default: PATH/Vulkan SDK)')
    ap.add_argument('--validate-spv', choices=('auto', 'off', 'sample', 'all'), default='auto',
                    help='spirv-val before an entry enters the cache (auto: all when the tool is found)')
    ap.add_argument('--spirv-val-sample', type=int, default=32, help='files for --validate-spv=sample')
    ap.add_argument('--write-programs', action='store_true',
                    help='also merge shader-programs.bin (large; only --prepare-shaders needs it)')
    ap.add_argument('--strict', action='store_true', help='exit 1 when any planned variant failed')
    ap.add_argument('--dry-run', action='store_true', help='plan and print, write nothing')
    ap.add_argument('--allow-repo-output', action='store_true', help='allow writing outside build/ inside the repo')
    a = ap.parse_args()
    if a.xlate_id and not re.fullmatch(r'[0-9a-fA-F]{64}', a.xlate_id):
        ap.error('--xlate-id needs exactly 64 hex digits')
    if a.subgroup:
        try:
            parts = [int(x) for x in a.subgroup.split(',')]
            if len(parts) != 3:
                raise ValueError
        except ValueError:
            ap.error('--subgroup needs three comma-separated sizes: VS,FS,CS')
    a.jobs = max(1, a.jobs)
    if a.limit is not None and a.limit < 0:
        ap.error('--limit must not be negative')

    build = (a.build or ROOT / 'build' / a.version).resolve()
    out = (a.out or build / 'shader-cache').resolve()
    records = (a.records or build).resolve()
    work = (a.work or build / 'shader-aot').resolve()
    romfs = (a.romfs or ROOT / 'build' / 'extracted' / a.version / 'romfs').resolve()
    xlate = (a.xlate or ROOT / 'build' / 'xlate' / 'oot_xlate.exe').resolve()

    # The repository stays free of game-derived data: only build/ inside the tree may receive output.
    for name, p in (('--out', out), ('--records', records), ('--work', work)):
        try:
            rel = p.relative_to(ROOT)
        except ValueError:
            continue
        if rel.parts[:1] != ('build',) and not a.allow_repo_output:
            raise SystemExit(f'{name} {p} is inside the repository but not under build/; '
                             f'pass --allow-repo-output if that is deliberate')

    if not xlate.is_file() and not (a.verify_only or a.print_identity):
        raise SystemExit(f'missing translator {xlate} (python tools/build_xlate.py)')

    # Identity: explicit flags win over a run log, which wins over the defaults (Float64, exact storage offsets, 32).
    opts = {'f64': None, 'sbuf': None, 'sg': None, 'tensor': None, 'prefix': None, 'log': None}
    log_path = a.log
    if not log_path and not a.no_log:
        cands = []
        for d in (records / 'logs', ROOT / 'build' / 'logs', build / 'logs'):
            if d.is_dir():
                cands += list(d.glob('*.log'))
        cands = sorted(cands, key=lambda p: p.stat().st_mtime, reverse=True)
        for p in cands[:4]:
            try:
                o = identity_from_log(p)
            except OSError:
                continue
            if o['prefix'] or o['sbuf'] is not None or o['tensor']:
                opts, log_path = o, p
                break
    elif log_path:
        opts = identity_from_log(log_path)
    if opts['prefix']:
        opts['log'] = log_path
    f64 = not a.no_float64 if a.no_float64 else (opts['f64'] if opts['f64'] is not None else 1)
    sbuf = a.sbuf_align if a.sbuf_align is not None else (opts['sbuf'] or 0)
    subgroup = tuple(int(x) for x in a.subgroup.split(',')) if a.subgroup else (opts['sg'] or (32, 32, 32))
    tensor = (a.tensor == 'on') if a.tensor else bool(opts['tensor'])
    base = base_identity(xlate, a.xlate_id)
    identity = mixed_identity(xlate, a.xlate_id, f64, sbuf, subgroup, tensor)
    log(f'identity: {hex32(identity)}  (f64={int(f64)} sbuf={sbuf} sg={subgroup[0]},{subgroup[1]},{subgroup[2]} '
        f'tensor={int(tensor)}' + (f'; options from {opts["log"]}' if opts.get('log') else '') + ')')
    if opts['prefix'] and not a.xlate_id and hex32(base)[:8] != opts['prefix']:
        log(f'identity: note the log printed {opts["prefix"]}; the translator was rebuilt since that run '
            f'(or the log is from another host). The host options above are still applied to the current id.')
    if a.print_identity:
        return 0
    if not xlate.is_file() and not a.verify_only:
        raise SystemExit(f'missing translator {xlate}')

    t0 = time.monotonic()

    # Programs: the recorded shader-programs.bin first (exact bytes the game asked for), the RomFS census for the rest.
    programs = {}
    recorded_path = records / 'shader-programs.bin'
    if recorded_path.is_file():
        records_progs = load_recorded_programs(recorded_path)
        programs.update(records_progs)
        log(f'recorded programs: {len(records_progs)} from {recorded_path}')
    census_dir = build / 'shaders'
    if romfs.is_dir():
        inv = load_inventory(romfs)
    elif (census_dir / 'programs.json').is_file():
        inv = {}
        added = load_census_dir(census_dir, inv)
        log(f'census: {added} programs from {census_dir}')
    else:
        inv = {}
    for ch, p in inv.items():
        programs.setdefault(ch, p)
    if not programs:
        raise SystemExit(f'no programs: {romfs} and {census_dir}/programs.json are missing')

    # Variants: the recorded answer files, then a default-answer variant of every program.
    stages = set(a.stage) if a.stage else set(STAGE_NAME.values())
    if a.mesh == 'off':
        stages -= {'ts', 'ms'}
    variants = []
    sources = list(a.answers)
    if not a.no_recorded and (records / 'shader-answers.bin').is_file():
        sources.insert(0, records / 'shader-answers.bin')
    seen = set()
    recorded_variants = 0
    skipped_stage = 0
    for src in sources:
        for v in load_answers_file(src):
            if STAGE_NAME.get(v.stage) not in stages:
                skipped_stage += 1
                continue
            key = (v.stage, v.code_hash, v.answers)
            if key in seen:
                continue
            seen.add(key)
            variants.append(v)
            recorded_variants += 1
    log(f'answers: {recorded_variants} recorded variants from {len(sources)} file(s)'
        + (f' ({skipped_stage} outside the selected stages)' if skipped_stage else ''))

    baseline = 0
    if not a.no_baseline:
        have = {(v.stage, v.code_hash, v.answers) for v in variants}
        for ch, p in programs.items():
            if STAGE_NAME.get(p.stage) not in stages:
                continue
            ans = default_answers(p.stage, p.ctl)
            if (p.stage, ch, ans) in have:
                continue
            have.add((p.stage, ch, ans))
            variants.append(Variant(p.stage, ch, None, ans, 'census'))
            baseline += 1
    log(f'variants: {len(variants)} ({recorded_variants} recorded, {baseline} default-answer)')

    if a.self_test:
        self_test(out, variants, 0)

    if a.limit is not None:
        variants = variants[:a.limit]
        log(f'limit: {len(variants)} variants')

    # Resolve each variant's program, and split cached from to-translate.
    todo, cached, missing = [], [], []
    for v in variants:
        p = programs.get(v.code_hash)
        if not p:
            missing.append(v)
            continue
        mask = read_mask_file(out, v.code_hash, identity)
        if mask is not None:
            iid = input_id(v.code_hash, v.answers, mask)
            key = key_of_input(iid, identity)
            if cache_entry_valid(out, key):
                cached.append((v, p, mask, iid, key))
                continue
        todo.append((v, p))
    pre_cached = list(cached)

    # Lower only the programs that need a translation.
    need_lower = {}
    for v, p in todo:
        need_lower.setdefault(v.code_hash, p)
    log(f'cache: {len(cached)} variants already cached, {len(todo)} to translate, {len(missing)} without a program')
    lowered, lower_fail = ({}, {})
    if need_lower and not a.verify_only and not a.dry_run:
        log(f'lowering: {len(need_lower)} programs')
        lowered, lower_fail = lower_programs(work, need_lower, tensor)
        log(f'lowering: {len(lowered)} ok, {len(lower_fail)} failed')

    # Jobs for the lowered variants.
    jobs, meta = [], []
    for v, p in todo:
        if v.code_hash not in lowered:
            continue
        oc, o1 = lowered[v.code_hash]
        key = provisional_key(v.code_hash, v.answers, identity)
        jobs.append(build_job(key, v.stage, o1.read_bytes(), v.answers, p.ctl, oc, p.code,
                              f64, sbuf, subgroup))
        meta.append((key, v, p))
    # One job per provisional key (the runtime's build_key is a function of exactly these inputs).
    uniq, seen_key = [], set()
    for j, (key, v, p) in zip(jobs, meta):
        if key in seen_key:
            continue
        seen_key.add(key)
        uniq.append((j, v, p))
    jobs = [j for j, _, _ in uniq]
    meta = [(j['key'], v, p) for j, v, p in uniq]

    written = skipped = iid_mismatch = 0
    failures = []
    if jobs and not a.verify_only and not a.dry_run:
        log(f'translating: {len(jobs)} variants on {min(a.jobs, len(jobs))} translator process(es)')
        out_dir, results = translate_jobs(xlate, work, jobs, a.jobs, a.retries)
        # spirv-val before anything enters the cache: the runtime checks only the magic and the instruction chain, so a
        # module it accepted would fail the driver instead of being translated again on the next run.
        val_tool = find_spirv_val(a.spirv_val) if a.validate_spv != 'off' else None
        vres = {}
        if val_tool and results:
            keys = list(results)
            if a.validate_spv == 'sample':
                keys = keys[:a.spirv_val_sample]
            def one_val(k):
                r = subprocess.run([str(val_tool), str(out_dir / (k + '.spv'))], capture_output=True, text=True)
                return r.returncode == 0, ((r.stderr or r.stdout).strip().splitlines() or [''])[0]
            with ThreadPoolExecutor(max_workers=max(1, min(a.jobs, len(keys)))) as ex:
                vres = dict(zip(keys, ex.map(one_val, keys)))
            ok_n = sum(1 for k in keys if vres[k][0])
            log(f'spirv-val: {ok_n} ok, {len(keys) - ok_n} failed ({val_tool})'
                + (' [sample]' if a.validate_spv == 'sample' else ''))
            if a.validate_spv == 'sample' and len(results) > len(keys):
                log(f'spirv-val: {len(results) - len(keys)} modules not validated (--validate-spv=all validates everyone)')
        elif results:
            log('spirv-val: not found; the structural checks only')
        for hexkey, v, p in meta:
            got = results.get(hexkey)
            if not got:
                failures.append((v, p, 'translator produced no valid output'))
                continue
            if vres.get(hexkey) and not vres[hexkey][0]:
                failures.append((v, p, f'spirv-val rejected it: {vres[hexkey][1]}'))
                continue
            spv, res = got
            try:
                mask = use_mask_of(res, spv, v.stage)
            except Exception as e:
                failures.append((v, p, f'reflection: {e}'))
                continue
            iid = input_id(v.code_hash, v.answers, mask)
            key = key_of_input(iid, identity)
            if v.iid is not None and v.iid != iid:
                iid_mismatch += 1
                if iid_mismatch <= 8:
                    log(f'note: {hex32(v.code_hash)[:12]} {STAGE_NAME[v.stage]}: computed input id '
                        f'{hex32(iid)[:12]} differs from the recorded {hex32(v.iid)[:12]} (the translator changed '
                        f'which answers the program uses); the new id is the one the runtime will compute')
            # The existing .use (from a cached variant of the same program) must agree with this translation.
            old = read_mask_file(out, v.code_hash, identity)
            if old is not None and old != mask:
                log(f'note: {hex32(v.code_hash)[:12]}: use mask {old} of an earlier variant differs from {mask}; '
                    f'writing this one')
            write_cache_entry(out, key, v.code_hash, identity, iid, v.stage, v.answers, mask,
                              p.control, p.code, p.ctl['size'], spv, res)
            cached.append((v, p, mask, iid, key))
            written += 1
    # Write the missing .in files for variants that were already cached.
    if not a.verify_only and not a.dry_run:
        for v, p, mask, iid, key in pre_cached:
            if in_present(out, iid, v.code_hash):
                skipped += 1
                continue
            write_in_file(out, iid, v.stage, v.answers, p.control, p.code, p.ctl['size'])

    # Records the runtime reads: the warm list (answers) and the prepared variants (--warm-shaders).
    answer_recs, prepared, prog_recs = [], [], []
    if cached and not a.verify_only and not a.dry_run:
        for v, p, mask, iid, key in cached:
            pad = 0
            answer_recs.append((v.stage, v.code_hash, iid, v.answers, pad))
            prepared.append((v.stage, iid))
            if a.write_programs and (v.iid is None or v.source == 'census'):
                prog_recs.append((v.stage, p))
        ans_add, ans_n = merge_answers(records / 'shader-answers.bin', answer_recs)
        prp_add, prp_n = merge_prepared(records / 'shader-prepared.bin', prepared)
        log(f'records: shader-answers.bin +{ans_add} ({ans_n} total); shader-prepared.bin +{prp_add} ({prp_n} total)')
        if a.write_programs and prog_recs:
            path = records / 'shader-programs.bin'
            existing = {}
            valid = False
            if path.is_file():
                try:
                    existing = load_recorded_programs(path)
                    valid = True
                except (ValueError, OSError):
                    existing = {}
            body = bytearray()
            seen = set(existing)
            added = 0
            for st, p in prog_recs:
                if p.code_hash in seen:
                    continue
                seen.add(p.code_hash)
                body += struct.pack('<III', st, len(p.code), len(p.control)) + p.code_hash + p.control + p.code
                added += 1
            if body:
                with open(path, 'ab' if valid else 'wb') as f:
                    if not valid:
                        f.write(PRG_MAGIC + struct.pack('<II', PRG_VERSION, 0))
                    f.write(body)
            log(f'records: shader-programs.bin +{added} programs')

    # Verification of the final state.
    if a.verify or a.verify_only:
        bad = []
        cached_ids = {id(c[0]) for c in cached}
        for v, p, mask, iid, key in cached:
            if not cache_entry_valid(out, key):
                bad.append((v, key, 'missing or invalid .spv/.res.json'))
            elif read_mask_file(out, v.code_hash, identity) != mask:
                bad.append((v, key, 'use mask differs'))
            elif not in_present(out, iid, v.code_hash):
                bad.append((v, key, 'missing .in'))
        for v, p in todo:
            if id(v) in cached_ids:
                continue
            if v.code_hash in lower_fail:
                bad.append((v, None, 'lowering failed'))
            else:
                bad.append((v, None, 'not cached'))
        # spirv-val on the loose modules again: the runtime's checks are lighter than the driver's, so a cached module
        # can still be invalid (observed with a CooperativeMatrix capability change in a translator rebuild).
        val_tool = find_spirv_val(a.spirv_val) if a.validate_spv != 'off' else None
        if val_tool:
            loose = [(v, out / (hex32(key) + '.spv')) for v, p, mask, iid, key in cached
                     if (out / (hex32(key) + '.spv')).is_file()]
            def one_cached(item):
                v, path = item
                r = subprocess.run([str(val_tool), str(path)], capture_output=True, text=True)
                return v, r.returncode == 0, ((r.stderr or r.stdout).strip().splitlines() or [''])[0]
            with ThreadPoolExecutor(max_workers=max(1, min(a.jobs, len(loose)))) as ex:
                checked = list(ex.map(one_cached, loose))
            nbad = 0
            for v, good, err in checked:
                if not good:
                    nbad += 1
                    bad.append((v, None, f'cached module fails spirv-val: {err}'))
            log(f'verify: spirv-val {len(checked) - nbad} ok, {nbad} failed of {len(checked)} loose modules')
        log(f'verify: {len(cached)} variants checked, {len(bad)} problems')
        for v, key, why in bad[:20]:
            log(f'  {hex32(v.code_hash)[:12]} {STAGE_NAME.get(v.stage, v.stage)}: {why}')

    # A machine-readable record of what this run decided, for comparing two identities or machines.
    if not a.verify_only and not a.dry_run:
        manifest = {
            'identity': hex32(identity),
            'options': {'float64': bool(f64), 'sbuf_align': sbuf, 'subgroup': list(subgroup), 'tensor': bool(tensor)},
            'counts': {'programs': len(programs), 'variants': len(variants), 'cached': len(cached),
                       'written': written, 'missing_program': len(missing),
                       'lowering_failures': len(lower_fail), 'translation_failures': len(failures)},
            'variants': [
                {'stage': STAGE_NAME.get(v.stage, str(v.stage)), 'code_hash': hex32(v.code_hash),
                 'input_id': hex32(iid), 'key': hex32(key),
                 'mask': [mask[0], mask[1], mask[2]], 'source': v.source}
                for v, p, mask, iid, key in cached
            ],
        }
        write_atomic(work / 'manifest.json', json.dumps(manifest).encode('utf-8'))

    # Summary.
    by_stage = {}
    for v, p in todo:
        by_stage[STAGE_NAME.get(v.stage, str(v.stage))] = by_stage.get(STAGE_NAME.get(v.stage, str(v.stage)), 0) + 1
    log('')
    log(f'prepare_all_shaders: {len(programs)} programs, {len(variants)} variants; '
        f'{len(cached)} cached, {written} written, {skipped} already had an input, {len(missing)} without a program')
    if lower_fail:
        log(f'  lowering failures: {len(lower_fail)}')
        for ch, msg in list(lower_fail.items())[:8]:
            log(f'    {hex32(ch)[:12]} {STAGE_NAME.get(programs[ch].stage, "?" )}: {msg}')
    if failures:
        log(f'  translation failures: {len(failures)}')
        for v, p, why in failures[:10]:
            log(f'    {hex32(v.code_hash)[:12]} {STAGE_NAME.get(v.stage, v.stage)}: {why}')
    if iid_mismatch:
        log(f'  input ids differing from the recorded file: {iid_mismatch} (see the notes above)')
    log(f'  output: {out}')
    log(f'  records: {records}')
    log(f'  work: {work}')
    log(f'  elapsed: {time.monotonic() - t0:.1f} s')
    if a.strict and (failures or lower_fail or iid_mismatch):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
