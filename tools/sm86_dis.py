"""A field-level listing of an SM86 (Ampere) shader program from the census: one line per 16-byte instruction with its
offset, predicate, opcode name and form, the register fields and the raw words. Not a disassembler: it names the
opcode (Mesa NAK's sm70_encode.rs, MIT, is the reference for the numbers) and prints the operand fields every ALU
instruction shares (NAK encode_alu_base: dst 16-23, src0 24-31, src1 32-39 or a 32-bit immediate or a constant buffer
word, src2 64-71, predicate 12-15, uniform forms with bit 91) so a pattern can be read without nvdisasm.

    python3 tools/sm86_dis.py <key or .code file> [--stage cs] [--from OFFSET] [--count N]

A key names build/v131072/shaders/code/<key>.code and its control section gives the stage and the code end.
"""
import argparse
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

NAMES = {
    0x002: 'MOV', 0x005: 'CS2R', 0x006: 'VOTE', 0x007: 'SEL', 0x008: 'FSEL', 0x009: 'FMNMX', 0x00a: 'FSET',
    0x00b: 'FSETP', 0x00c: 'ISETP', 0x010: 'IADD3', 0x011: 'LEA', 0x012: 'LOP3', 0x013: 'IABS', 0x016: 'PRMT',
    0x017: 'IMNMX', 0x019: 'SHF', 0x01a: 'SGXT', 0x01b: 'BMSK', 0x01c: 'PLOP3', 0x020: 'FMUL', 0x021: 'FADD',
    0x022: 'FSWZADD', 0x023: 'FFMA', 0x024: 'IMAD', 0x025: 'IMAD.WIDE', 0x026: 'IDP', 0x027: 'IMAD.HI',
    0x028: 'DMUL', 0x029: 'DADD', 0x02a: 'DSETP', 0x02b: 'DFMA', 0x030: 'HADD2', 0x031: 'HFMA2', 0x032: 'HMUL2',
    0x033: 'HSET2', 0x034: 'HSETP2', 0x03e: 'F2FP', 0x040: 'HMNMX2', 0x043: 'FCHK', 0x045: 'I2FP',
    0x082: 'UMOV', 0x085: 'UIMNMX', 0x086: 'VOTEU', 0x087: 'USEL', 0x08c: 'UISETP', 0x090: 'UIADD3', 0x091: 'ULEA',
    0x092: 'ULOP3', 0x096: 'UPRMT', 0x099: 'USHF', 0x09a: 'USGXT', 0x09b: 'UBMSK', 0x09c: 'UPLOP3', 0x0a4: 'UIMAD',
    0x0a5: 'UIMAD.WIDE', 0x0b8: 'ULDCG', 0x0b9: 'ULDC', 0x0ba: 'UF2FP', 0x0bd: 'UFLO', 0x0be: 'UBREV', 0x0bf: 'UPOPC',
    0x0c2: 'UMOV.R', 0x0ca: 'R2UR?',
    0x100: 'FLO', 0x101: 'BREV', 0x104: 'F2F', 0x105: 'F2I', 0x106: 'I2F', 0x107: 'FRND', 0x108: 'MUFU',
    0x109: 'POPC', 0x110: 'F2F64', 0x111: 'F2I64', 0x112: 'I2F64', 0x113: 'FRND64', 0x118: 'NOP', 0x119: 'S2R',
    0x11a: 'DEPBAR', 0x11d: 'BAR', 0x120: 'AL2P', 0x121: 'ALD', 0x122: 'AST', 0x123: 'ISBERD', 0x124: 'OUT',
    0x125: 'PIXLD', 0x126: 'IPA', 0x127: 'ISBEWR', 0x141: 'BSYNC', 0x142: 'BREAK', 0x144: 'CALL', 0x145: 'BSSY',
    0x147: 'BRA', 0x148: 'WARPSYNC', 0x149: 'BRX', 0x14d: 'EXIT', 0x150: 'RET', 0x155: 'BMOV', 0x156: 'BMOV',
    0x158: 'BRXU', 0x15b: 'KILL', 0x15c: 'BPT', 0x15d: 'NANOSLEEP', 0x160: 'TEX', 0x161: 'TEX.B', 0x163: 'TLD4',
    0x164: 'TLD4.B', 0x166: 'TLD', 0x167: 'TLD.B', 0x169: 'TMML', 0x16a: 'TMML.B', 0x16c: 'TXD', 0x16d: 'TXD.B',
    0x16f: 'TXQ', 0x170: 'TXQ.B', 0x181: 'LD', 0x182: 'LDC', 0x183: 'LDL', 0x184: 'LDS', 0x186: 'ST', 0x187: 'STL',
    0x188: 'STS', 0x189: 'SHFL', 0x18c: 'ATOMS', 0x18d: 'ATOM', 0x18e: 'RED', 0x18f: 'CCTL', 0x192: 'MEMBAR',
    0x194: 'SUATOM', 0x196: 'SUATOM', 0x197: 'SULD', 0x198: 'SULD', 0x19a: 'SULD', 0x19b: 'SUST', 0x19c: 'SUST',
    0x19e: 'SUST', 0x1a0: 'SUATOM', 0x1a1: 'MATCH', 0x1a3: 'ATOM', 0x1a6: 'ATOM', 0x1a8: 'ATOMG', 0x1a9: 'ATOMG',
    0x1ac: 'LDCG', 0x1ad: 'LDTRAM', 0x1c2: 'R2UR', 0x1c3: 'S2UR', 0x1c4: 'REDUX',
}
FORMS = {1: 'R', 2: 'xI', 3: 'xC', 4: 'I', 5: 'C', 6: 'U', 7: 'xU'}   # x: the wide operand is the third source


def bits(words, offset, count):
    v = words[offset // 32] | (words[offset // 32 + 1] << 32 if offset // 32 + 1 < 4 else 0)
    return (v >> (offset % 32)) & ((1 << count) - 1)


# The stage buffer ops of task and mesh programs (docs/mesh.md), as read from captured programs (2026-11-07; not in
# Mesa NAK's encoder): Rd (ISBERD) at 16-23, the address register at 24-31, the byte offset in w1 >> 8, ISBEWR's data
# register in w1 & 0xff, the space by bits 75-78: 0xf ATTR (the workgroup index at 0; vertex outputs, a 0x80-byte
# line per component), 7 TASK (the task's output: the mesh workgroup count at 4, the payload from 0x10), 1 MAP.32 (the
# primitive count at 3), 0 MAP.U8 (vertex indices from 4). The names are working ones.
ISBE_SPACE = {0xf: 'ATTR', 0x7: 'TASK', 0x1: 'MAP.32', 0x0: 'MAP.U8'}


def isbe(w, base):
    ra = bits(w, 24, 8)
    addr = 'RZ' if ra == 255 else f'R{ra}'
    space = ISBE_SPACE.get(bits(w, 75, 4), f'space{bits(w, 75, 4):x}')
    if base == 0x123:
        return f'ISBERD.{space:<6} R{bits(w, 16, 8)}, [{addr} + {w[1] >> 8:#x}]'
    return f'ISBEWR.{space:<6} [{addr} + {w[1] >> 8:#x}], R{w[1] & 0xff}  b79 {bits(w, 79, 1)}'


def listing(code, start, end, origin=0):
    lines = []
    for o in range(start, end - (end - start) % 16, 16):
        w = struct.unpack_from('<4I', code, o)
        op = w[0] & 0xfff
        name = NAMES.get(op & 0x1ff, f'op{op & 0x1ff:03x}')
        form = FORMS.get(op >> 9, str(op >> 9))
        pred = bits(w, 12, 4)
        p = '      ' if pred == 7 else f'@{"!" if pred & 8 else ""}P{pred & 7} '.ljust(6)
        rd, ra, rb, rc = bits(w, 16, 8), bits(w, 24, 8), bits(w, 32, 8), bits(w, 64, 8)
        imm = w[1]
        cb = f'c[{bits(w, 54, 5)}][{bits(w, 38, 16):#x}]'
        src1 = f'{imm:#x}' if form in ('I', 'xI') else cb if form in ('C', 'xC') else f'UR{bits(w, 32, 6)}' if form in ('U', 'xU') else f'R{rb}'
        text = f'{name:<10} {form:<2} R{rd:<3} R{ra:<3} {src1:<14} R{rc:<3}'
        if op & 0x1ff in (0x123, 0x127):
            text = isbe(w, op & 0x1ff).ljust(len(text))
        lines.append(f'{origin + o:06x} {p}{text} {w[0]:08x} {w[1]:08x} {w[2]:08x} {w[3]:08x}')
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('program', help='census key or .code path')
    ap.add_argument('--version', default='v131072')
    ap.add_argument('--stage', help='cs for a bare code file of a compute program (no header)')
    ap.add_argument('--from', dest='start', type=lambda s: int(s, 0), help='byte offset to start at')
    ap.add_argument('--count', type=int, help='instructions to print')
    args = ap.parse_args()
    path = Path(args.program)
    header, end = 0x80, None
    if not path.is_file():
        base = ROOT / 'build' / args.version / 'shaders'
        path = base / 'code' / f'{args.program}.code'
        ctl = (base / 'ctl' / f'{args.program}.ctl').read_bytes()
        end = struct.unpack_from('<I', ctl, 0x38)[0]
        header = 0 if ctl[0x56] == 5 else 0x80
        c1_len, c1_off = struct.unpack_from('<II', ctl, 0x3c)
        print(f'stage {ctl[0x56]}, code end {end:#x}, c1 at {c1_off:#x} ({c1_len} bytes)')
    elif args.stage == 'cs':
        header = 0
    code = path.read_bytes()
    end = end or len(code)
    start = args.start if args.start is not None else header
    if args.count:
        end = min(end, start + 16 * args.count)
    print('\n'.join(listing(code, start, end)))
    if not path.is_file() or end < len(code):
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
