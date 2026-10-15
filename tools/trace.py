"""Recursive code tracer for a 32 KiB (single bank) Game Boy ROM.

Finds which bytes are code by following control flow from the entry points,
including jump tables: a table of addresses right after an `rst` (game.json
"jump_table_rst") or a call to a dispatcher routine ("jump_table_calls"). Writes an mgbdis .sym file that marks code and data blocks
and names the discovered entry points.

    python tools/trace.py tetris.gb  ->  tetris.sym
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import build  # noqa: E402
from sm83 import decode  # noqa: E402

JUMPTABLE_RST = build.GAME['jump_table_rst']   # `rst $xx` + table of addresses, or None
# routines that are called with a table of addresses right after the call (game.json:
# "jump_table_calls": ["0x0229"]) - they pop it as their return address and jump through it
JUMPTABLE_TARGETS = ({JUMPTABLE_RST} if JUMPTABLE_RST is not None else set()) |     {int(a, 0) for a in build.GAME.get('jump_table_calls', [])}
ENTRY_POINTS = {
    0x0100: 'Boot',
    0x0000: 'RST_00',
    0x0008: 'RST_08',
    0x0028: 'RST_28',
    0x0040: 'VBlankInterrupt',
    0x0048: 'LCDCInterrupt',
    0x0050: 'TimerOverflowInterrupt',
    0x0058: 'SerialTransferCompleteInterrupt',
}
# code nothing refers to, but that is clearly code (game.json: "trace_entries": {"0x0153": "Name"})
ENTRY_POINTS.update({int(a, 0): n for a, n in build.GAME.get('trace_entries', {}).items()})


DATA_OPS = {0x10, 0x08, 0xF8, 0xE8, 0xD3, 0xDB, 0xDD, 0xE3, 0xE4, 0xEB, 0xEC, 0xED, 0xF4, 0xFC, 0xFD}
COND_OPS = {0x20: 0, 0x28: 1, 0x30: 2, 0x38: 3, 0xC2: 0, 0xCA: 1, 0xD2: 2, 0xDA: 3, 0xC0: 0, 0xC8: 1, 0xD0: 2, 0xD8: 3}


def opposite_pair(prev_op, op):
    """`jp z, X` followed by `ret nz` (or any conditional jump / return on the opposite flag): one of the
    two is always taken, so the code ends there."""
    return prev_op in COND_OPS and op in COND_OPS and COND_OPS[prev_op] ^ 1 == COND_OPS[op]


def trace(rom, table_limits):
    """One tracing pass. table_limits: {table_addr: max_entries or None}."""
    size = len(rom)
    code = {}            # addr -> Insn
    calls = set()        # call/rst targets
    jumps = set()        # jump targets
    tables = {}          # table_addr -> [targets]
    work = list(ENTRY_POINTS)
    seen = set()
    while work:
        pc = work.pop()
        while 0 <= pc < size and pc not in seen:
            insn = decode(rom, pc)
            if insn.kind == 'invalid':
                break
            seen.add(pc)
            code[pc] = insn
            nxt = pc + insn.length
            k = insn.kind
            if k in ('jump', 'cjump'):
                jumps.add(insn.target)
                work.append(insn.target)
            elif k in ('call', 'ccall', 'rst'):
                calls.add(insn.target)
                work.append(insn.target)
                if insn.target in JUMPTABLE_TARGETS:
                    t = nxt
                    limit = table_limits.get(t)
                    entries = []
                    a = t
                    while a + 1 < size and (limit is None or len(entries) < limit):
                        w = rom[a] | (rom[a + 1] << 8)
                        if not (0x0040 <= w < 0x8000) or 0x0104 <= w < 0x0150:
                            break
                        entries.append(w)
                        a += 2
                    tables[t] = entries
                    for w in entries:
                        jumps.add(w)
                        work.append(w)
                    break
            if k in ('jump', 'ret', 'jphl', 'stop'):
                break
            pc = nxt
    return code, calls, jumps, tables


def code_bytes(code):
    s = set()
    for a, i in code.items():
        s.update(range(a, a + i.length))
    return s


def run(rom):
    limits = {}
    for _ in range(50):
        code, calls, jumps, tables = trace(rom, limits)
        cb = code_bytes(code)
        starts = set(code)
        changed = False
        for t, entries in tables.items():
            # a table ends where code (or another table) begins
            n = 0
            for i in range(len(entries)):
                a = t + 2 * i
                if a in cb or a + 1 in cb or a in starts:
                    break
                if i and any(a == t2 for t2 in tables if t2 != t):
                    break
                n += 1
            if limits.get(t) != n:
                limits[t] = n
                changed = True
        if not changed:
            break
    return code, calls, jumps, tables


# --- banked ROMs ---------------------------------------------------------------
# Places are linear addresses (offsets in the ROM file). Code in bank n sees bank 0 at $0000-$3FFF and
# itself at $4000-$7FFF, so its jumps into $4000-$7FFF stay in bank n; a jump from bank 0 into
# $4000-$7FFF depends on the bank switched in at run time and is not followed. Code in other banks is
# reached through the far call (game.json "farcall": {"rst": "0x10", "table": "0x4001"}: `ld hl, $BBII`
# + `rst $10` calls entry II of the table of addresses at BB:table; every entry of every bank's table is
# traced), and through the addresses the game was seen running (--coverage FILE: one linear address in
# hex per line, from running the game in an emulator).

def banked_view(rom, bank, cache={}):
    v = cache.get(bank)
    if v is None:
        v = bytearray(0x10000)
        v[0:0x4000] = rom[0:0x4000]
        o = bank * 0x4000
        v[0x4000:0x8000] = rom[o:o + 0x4000].ljust(0x4000, b'\xff')
        cache[bank] = v
    return v


def lin_of(bank, addr):
    if addr < 0x4000:
        return addr
    if addr < 0x8000 and bank:
        return bank * 0x4000 + addr - 0x4000
    return None


def far_tables(rom, table):
    """bank -> [linear entry addresses] of the far-call table at the start of each bank (a bank has one
    when its first byte is its own number and the words after it point into the bank)."""
    out = {}
    for bank in range(1, len(rom) // 0x4000):
        o = bank * 0x4000
        if rom[o] != bank:
            continue
        entries, a, lowest = [], table, 0x8000
        while a + 1 < 0x8000 and a < lowest:
            w = rom[o + a - 0x4000] | rom[o + a - 0x3FFF] << 8
            if not table + 2 <= w < 0x8000:
                break
            entries.append(w)
            lowest = min(lowest, w)
            a += 2
        if entries:
            out[bank] = [lin_of(bank, w) for w in entries]
    return out


def trace_banked(rom, table_limits, extra):
    far = build.GAME.get('farcall') or {}
    far_rst = int(far['rst'], 0) if 'rst' in far else None
    far_table = int(far.get('table', '0x4001'), 0)
    ftables = far_tables(rom, far_table) if far_rst is not None else {}
    size = len(rom)
    nbanks = size // 0x4000
    code, calls, jumps, tables, unresolved = {}, set(), set(), {}, set()
    work = [(a, None) for a in list(ENTRY_POINTS) + list(extra)]
    table_entries = trace_banked.table_entries = set()
    # tables of addresses whose entries each live in the bank a parallel table of bank numbers names
    # (game.json "bank_tables": [{"table": "0x3955", "banks": "0xC000", "count": 232}], linear addresses)
    for bt in build.GAME.get('bank_tables', []):
        t, b = int(bt['table'], 0), int(bt['banks'], 0)
        for i in range(int(bt['count'])):
            w = rom[t + 2 * i] | rom[t + 2 * i + 1] << 8
            if 0x4000 <= w < 0x8000 and 0 < rom[b + i] < nbanks:
                jumps.add(lin_of(rom[b + i], w))
                table_entries.add(lin_of(rom[b + i], w))
                work.append((lin_of(rom[b + i], w), None))
    far_used = set()                                   # (bank, entry) the code calls
    seen = set()
    # MBC bank writes (game.json "bank_writes": true): bank-0 code that does `ld a, N` + `ld [$2000-$3FFF], a`
    # (or calls a bank-0 routine that starts with that store) has bank N at $4000-$7FFF until the next
    # switch, so its jumps and calls there are followed into bank N.
    track = build.GAME.get('bank_writes')
    covered = set(extra)
    resolved = set()
    switchers = {}                                     # bank-0 routine -> it switches to A (True)

    def is_switcher(t):
        if t not in switchers:
            i = decode(rom, t) if t < 0x4000 else None
            switchers[t] = bool(i and rom[t] == 0xEA and 0x20 <= rom[t + 2] < 0x40)
        return switchers[t]

    while work:
        lin, cur = work.pop()
        a_val = hl_val = prev_op = None
        # one linear run is kept only when it ends like code: running into an invalid opcode, or one that code
        # never has (`stop`, `ld hl, sp+n`, ...) unless the game was seen running it, drops the run back to its
        # last call (a routine that never returns) - the run read data.
        trusted = lin in covered
        r_seen, r_code, r_work, r_calls, r_jumps, r_tables, r_unres, r_res, r_far = [], {}, [], [], [], {}, [], [], []
        last_call = 0
        snap, snap_tables = (0, 0, 0, 0, 0, 0), {}
        ok = True
        while lin is not None and 0 <= lin < size and (lin, cur if lin < 0x4000 else None) not in seen                 and (lin, cur if lin < 0x4000 else None) not in r_seen:
            bank = lin // 0x4000
            view = banked_view(rom, bank if bank else (cur or 1))
            pc = lin if bank == 0 else 0x4000 + (lin & 0x3FFF)
            insn = decode(view, pc)
            if insn.kind == 'invalid' or pc + insn.length > (0x4000 if bank == 0 else 0x8000) or                     (view[pc] in DATA_OPS or view[pc] == 0x76 and bank) and lin not in covered:
                ok = trusted
                break
            r_seen.append((lin, cur if lin < 0x4000 else None))
            r_code[lin] = insn
            nxt = lin + insn.length
            k = insn.kind
            op = view[pc]
            if track and bank == 0:
                if op == 0xEA and 0x20 <= view[pc + 2] < 0x40:
                    cur = a_val if a_val is not None and 0 < a_val < nbanks else None
                elif k in ('call', 'ccall') and insn.target < 0x4000 and is_switcher(insn.target):
                    cur = a_val if a_val is not None and 0 < a_val < nbanks else None
                if op == 0x36 and hl_val is not None and 0x2000 <= hl_val < 0x4000:   # ld hl, $2100 / ld [hl], N
                    n = view[pc + 1] & (0x1F if nbanks <= 32 else 0xFF) or 1
                    cur = n if n < nbanks else None
                hl_val = view[pc + 1] | view[pc + 2] << 8 if op == 0x21 else hl_val if op in (0x36, 0x3E, 0xEA) else None
                if op == 0x3E:
                    a_val = view[pc + 1] & (0x1F if nbanks <= 32 else 0xFF) or 1
                elif op not in (0xEA, 0xE0, 0x77, 0x22, 0x32, 0x12, 0x02, 0xF5, 0xC5, 0xD5, 0xE5, 0x01, 0x11, 0x21,
                                0x06, 0x0E, 0x16, 0x1E, 0x26, 0x2E, 0x47, 0x4F, 0x57, 0x5F, 0x67, 0x6F):
                    a_val = None
            if k in ('jump', 'cjump', 'call', 'ccall', 'rst'):
                t = lin_of(bank, insn.target)
                if t is None and bank == 0 and cur and 0x4000 <= insn.target < 0x8000:
                    t = lin_of(cur, insn.target)
                    r_res.append(lin)
                if t is None:
                    if insn.target < 0x8000:
                        r_unres.append((lin, insn.target))
                else:
                    (r_jumps if k in ('jump', 'cjump') else r_calls).append(t)
                    r_work.append((t, cur if bank == 0 and k in ('jump', 'cjump') else None))
            if k == 'rst' and insn.target == far_rst and view[pc - 3] == 0x21 and (lin - 3 in code or lin - 3 in r_code):
                fb, fi = view[pc - 1], view[pc - 2]                  # ld hl, $BBII just before
                ft = ftables.get(fb, [])
                if fi < len(ft):
                    r_far.append((fb, fi))
                    r_calls.append(ft[fi])
                    r_work.append((ft[fi], None))
            if k in ('call', 'ccall', 'rst') and insn.target in JUMPTABLE_TARGETS:
                limit = table_limits.get(nxt)
                entries, a = [], nxt
                while a + 1 < size and (limit is None or len(entries) < limit):
                    ca = pc + (a - lin)                                  # CPU address of the entry
                    w = view[ca] | view[ca + 1] << 8 if ca + 1 < 0x8000 else None
                    if w is None or not (0x0040 <= w < 0x8000) or 0x0104 <= w < 0x0150:
                        break
                    t = lin_of(bank or cur or 0, w)
                    if t is None or entries and (w < 0x4000) != (entries[0] < 0x4000):
                        break                            # a table points into one bank: home or its own
                    entries.append(t)
                    a += 2
                r_tables[nxt] = entries
                for t in entries:
                    r_jumps.append(t)
                    r_work.append((t, cur if bank == 0 else None))
                break
            if k in ('jump', 'ret', 'jphl', 'stop') or opposite_pair(prev_op, op):
                break
            prev_op = op
            if k in ('call', 'ccall', 'rst'):
                last_call = len(r_seen)
                snap = (len(r_work), len(r_calls), len(r_jumps), len(r_unres), len(r_res), len(r_far))
                snap_tables = dict(r_tables)
            lin = nxt
        if not ok:                                   # keep the run up to its last call, with what that found
            r_seen, (nw, nc, nj, nu, nr, nf), r_tables = r_seen[:last_call], snap, snap_tables
            keep = {a for a, _ in r_seen}
            r_code = {a: i for a, i in r_code.items() if a in keep}
            r_work, r_calls, r_jumps, r_unres, r_res, r_far = (r_work[:nw], r_calls[:nc], r_jumps[:nj],
                                                               r_unres[:nu], r_res[:nr], r_far[:nf])
        seen.update(r_seen)
        code.update(r_code)
        work.extend(r_work)
        calls.update(r_calls)
        jumps.update(r_jumps)
        tables.update(r_tables)
        unresolved.update(r_unres)
        resolved.update(r_res)
        far_used.update(r_far)
    trace_banked.far_used = far_used
    unresolved = {u for u in unresolved if u[0] not in resolved}    # a path that knew the bank followed it
    return code, calls, jumps, tables, unresolved, ftables


RARE_OPS = {0x00, 0x40, 0x49, 0x52, 0x5B, 0x64, 0x6D, 0x7F, 0x76, 0x10, 0xFF, 0x08, 0xE8, 0xF8, 0xF9,
            0x37, 0x3F, 0x27, 0x2F, 0xD9}


def looks_like_code(rom, lin, known, limit=300):
    """Does the code from `lin` (a far-table entry nothing calls by number) look like a routine? No invalid
    or rare opcodes (nop, ld b,b, halt, ...: common in data, rare in code), not mostly register moves (the
    high bytes of a pointer table), it ends (ret / jp), and its calls into bank 0 land on routines already
    found."""
    bank = lin // 0x4000
    view = banked_view(rom, bank)
    work, seen, rare, moves, n, ends = [0x4000 + (lin & 0x3FFF)], set(), 0, 0, 0, False
    while work and n < limit:
        pc = work.pop()
        while 0x4000 <= pc < 0x8000 and pc not in seen:
            insn = decode(view, pc)
            if insn.kind == 'invalid' or bank * 0x4000 + pc - 0x4000 in known.get('data', ()):
                return False
            seen.add(pc)
            n += 1
            rare += view[pc] in RARE_OPS
            moves += 0x40 <= view[pc] < 0x80          # ld r, r': a table of pointers into $4000-$7FFF reads so
            k = insn.kind
            if k in ('jump', 'cjump', 'call', 'ccall', 'rst') and insn.target is not None:
                t = insn.target
                if t < 0x4000 and k != 'rst' and t not in known['starts']:
                    return False
                if 0x4000 <= t < 0x8000:
                    work.append(t)
            if k in ('ret', 'jump', 'jphl'):
                ends = True
            if k in ('jump', 'ret', 'jphl', 'stop'):
                break
            pc += insn.length
    return ends and n >= 2 and rare * 12 <= n and moves * 5 <= n * 2


def run_banked(rom, extra):
    extra = list(extra)
    for _ in range(10):                            # far-table entries that look like code join the trace
        code, calls, jumps, tables, unresolved, ftables = run_banked_once(rom, extra)
        known = {'starts': set(code)}
        used = {b for b, _ in trace_banked.far_used}
        new = [e for b in used for e in ftables[b] if e not in code and looks_like_code(rom, e, known)]
        if not new:
            break
        extra += new
        run_banked.guessed = getattr(run_banked, 'guessed', set()) | set(new)
    return code, calls, jumps, tables, unresolved, ftables


def run_banked_once(rom, extra):
    limits = {}
    for _ in range(50):
        code, calls, jumps, tables, unresolved, ftables = trace_banked(rom, limits, extra)
        cb = code_bytes(code)
        starts = set(code)
        changed = False
        for t, entries in tables.items():
            n = 0
            for i in range(len(entries)):
                a = t + 2 * i
                if a in cb or a + 1 in cb or a in starts:
                    break
                if i and any(a == t2 for t2 in tables if t2 != t):
                    break
                n += 1
            if limits.get(t) != n:
                limits[t] = n
                changed = True
        if not changed:
            break
    return code, calls, jumps, tables, unresolved, ftables


def main_banked(rom_path, rom, extra):
    code, calls, jumps, tables, unresolved, ftables = run_banked(rom, extra)
    cb = code_bytes(code)
    size = len(rom)
    loc = lambda a: '{:02x}:{:04x}'.format(a // 0x4000, a if a < 0x4000 else 0x4000 + (a & 0x3FFF))  # noqa: E731
    tag = lambda a: ('{:04X}'.format(a) if a < 0x4000 else  # noqa: E731
                     '{:02X}_{:04X}'.format(a // 0x4000, 0x4000 + (a & 0x3FFF)))
    labels = dict(ENTRY_POINTS)
    data_blocks = {}
    used_banks = {b for b, _ in trace_banked.far_used}
    for bank, entries in ftables.items():
        if bank not in used_banks:
            continue
        o = bank * 0x4000
        labels.setdefault(o, 'BankNumber_{:02X}'.format(bank))
        labels.setdefault(o + 1, 'FarTable_{:02X}'.format(bank))
        data_blocks[o] = 1
        data_blocks[o + 1] = 2 * len(entries)
        for e in entries:                          # entries that are not code: data the bank hands out
            if e not in code:
                labels.setdefault(e, 'Data_' + tag(e))
    for t in tables:
        labels.setdefault(t, 'JumpTable_' + tag(t))
    for c in calls | getattr(run_banked, 'guessed', set()) | trace_banked.table_entries:
        labels.setdefault(c, 'Call_' + tag(c))
    for t in jumps:
        if t in cb and t not in labels:
            pass                                   # mgbdis names jump targets itself
    for e in tables.values():
        for w in e:
            labels.setdefault(w, 'Jump_' + tag(w))
    for bt in build.GAME.get('bank_tables', []):          # the entries' banks come from their bank table
        t, b = int(bt['table'], 0), int(bt['banks'], 0)
        ws = [rom[t + 2 * i] | rom[t + 2 * i + 1] << 8 for i in range(int(bt['count']))]
        tables[t] = [lin_of(rom[b + i], w) if 0x4000 <= w < 0x8000 and 0 < rom[b + i] < size // 0x4000
                     else '${:04x}'.format(w) for i, w in enumerate(ws)]
        labels.setdefault(t, 'JumpTable_' + tag(t))
    # each table is its own data block, and tools/bootstrap.py writes it as `dw` lines (<rom>.tables.json)
    table_json = {}
    for t, e in tables.items():
        if e and t not in cb:
            data_blocks[t] = 2 * len(e)
            table_json[loc(t)] = [w if isinstance(w, str) else labels.get(w, '${:04x}'.format(
                w if w < 0x4000 else 0x4000 + (w & 0x3FFF))) for w in e]
    open(os.path.splitext(rom_path)[0] + '.tables.json', 'w').write(json.dumps(table_json, indent=0))
    starts = set(code)
    overlaps = sorted(a for a, i in code.items() for x in range(a + 1, a + i.length) if x in starts)
    if overlaps:
        print('{} instructions overlap others (code read at two alignments), e.g. {}'.format(
            len(overlaps), ' '.join(loc(a) for a in overlaps[:12])))
    out = [';; generated by tools/trace.py - code/data map for mgbdis']
    for bank in range(size // 0x4000):
        base = bank * 0x4000
        a = base
        while a < base + 0x4000:
            if a in data_blocks:
                out.append('{} .data:{:x}'.format(loc(a), data_blocks[a]))
                a += data_blocks[a]
                continue
            is_code = a in cb
            b = a
            while b < base + 0x4000 and (b in cb) == is_code and (b == a or b not in data_blocks):
                b += 1
            out.append('{} .{}:{:x}'.format(loc(a), 'code' if is_code else 'data', b - a))
            a = b
    for a in sorted(labels):
        out.append('{} {}'.format(loc(a), labels[a]))
    open(os.path.splitext(rom_path)[0] + '.sym', 'w').write('\n'.join(out) + '\n')
    per_bank = {}
    for a in cb:
        per_bank[a // 0x4000] = per_bank.get(a // 0x4000, 0) + 1
    print(json.dumps({'code_bytes': len(cb), 'functions': len(calls), 'tables': len(tables),
                      'far_tables': len(used_banks), 'far_entries': len(trace_banked.far_used), 'unresolved_far_jumps': len(unresolved)}))
    for bank in sorted(used_banks):
        missing = [i for i in range(len(ftables[bank])) if (bank, i) not in trace_banked.far_used
                   and ftables[bank][i] not in code]
        if missing:
            print('bank {:02X}: far entries never called: {}'.format(bank, missing))
    print('code bytes per bank:', ' '.join('{:02X}:{}'.format(b, n) for b, n in sorted(per_bank.items())))


def main():
    rom_path = sys.argv[1]
    rom = open(rom_path, 'rb').read()
    if len(rom) > 0x8000:
        extra = []
        if '--coverage' in sys.argv:
            extra = [int(x, 16) for x in open(sys.argv[sys.argv.index('--coverage') + 1]).read().split()]
        return main_banked(rom_path, rom, extra)
    code, calls, jumps, tables = run(rom)
    cb = code_bytes(code)
    size = len(rom)

    # contiguous code / data ranges
    blocks = []
    a = 0
    while a < size:
        is_code = a in cb
        b = a
        while b < size and (b in cb) == is_code:
            b += 1
        blocks.append((a, b - a, 'code' if is_code else 'data'))
        a = b

    table_bytes = set()
    for t, e in tables.items():
        table_bytes.update(range(t, t + 2 * len(e)))

    labels = dict(ENTRY_POINTS)
    for t in tables:
        labels.setdefault(t, 'JumpTable_{:04X}'.format(t))
    for c in calls:
        labels.setdefault(c, 'Call_{:04X}'.format(c))
    for e in tables.values():
        for w in e:
            labels.setdefault(w, 'Jump_{:04X}'.format(w))

    out = [';; generated by tools/trace.py - code/data map for mgbdis']
    for a, n, kind in blocks:
        out.append('00:{:04x} .{}:{:x}'.format(a, kind, n))
    for a in sorted(labels):
        out.append('00:{:04x} {}'.format(a, labels[a]))
    sym_path = os.path.splitext(rom_path)[0] + '.sym'
    open(sym_path, 'w').write('\n'.join(out) + '\n')

    info = {
        'code_bytes': len(cb),
        'data_bytes': size - len(cb),
        'functions': len(calls),
        'tables': {'{:04X}'.format(t): ['{:04X}'.format(w) for w in e] for t, e in tables.items()},
    }
    print(json.dumps({k: v for k, v in info.items() if k != 'tables'}))
    for t, e in sorted(tables.items()):
        print('table ${:04X}: {} entries'.format(t, len(e)))


if __name__ == '__main__':
    main()
