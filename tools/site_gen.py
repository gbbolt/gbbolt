"""Generate the gbbolt viewer: one self-contained HTML file with the data embedded."""
import base64
import datetime
import json
import os
import shutil
import re

import build
from pseudo import HELPER_DOCS, parse_signature
from sm83 import CPU, MBC

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, 'viewer.html')
OUT_DIR = os.path.join(build.OUT, 'site')

STATUS_CODE = {'verified': 'v', 'checked': 'k', 'failing': 'f', 'stale': 's', 'none': 'c'}


def unit_paths(project):
    """Virtual folder of every unit: `;@ path:` if given; a data block otherwise
    takes the folder most of its users are in (the unit before it if nobody
    refers to it)."""
    import bisect
    p, an = project.parsed, project.analysis
    paths = {u.name: u.path for u in p.units if u.path}
    if build.GAME.get('unit_labels') == 'global':
        # pret style: the source tree is already organised; a unit lives where its file does
        for u in p.units:
            if u.name not in paths:
                paths[u.name] = os.path.splitext(p.lines[u.first_line].file)[0]
        return paths
    data = sorted((u for u in p.units if u.kind == 'data' and u.name not in paths), key=lambda u: u.start)
    starts = [d.start for d in data]
    users = {u.name: {} for u in data}
    for code in p.units:
        if code.name not in paths or code.name not in an.refs:
            continue
        # RAM accesses plus every 16-bit immediate (ROM pointers like `ld de, MenuObjects`)
        addrs = set().union(*an.refs[code.name].values())
        addrs |= {i.imm for i in an.insns.get(code.name, []) if i.imm is not None and i.imm >= 0x150}
        hit = set()
        for a in addrs:
            k = bisect.bisect_right(starts, a) - 1
            if k >= 0 and a < data[k].end:
                hit.add(data[k].name)
        for name in hit:
            users[name][paths[code.name]] = users[name].get(paths[code.name], 0) + 1
    order = [u.name for u in p.units]
    for name in an.tables:                           # a jump table belongs with its dispatcher (the unit before it)
        i = order.index(name)
        if name in users and i and order[i - 1] in paths:
            users[name] = {paths[order[i - 1]]: 1}
    prev = ''
    for u in p.units:
        if u.name in users:
            c = users[u.name]
            paths[u.name] = max(sorted(c), key=lambda k: c[k]) if c else prev
        prev = paths.get(u.name, prev)
    return paths


def folder_list(paths):
    """[{path, desc}] in reading order: src/folders.txt first, then any others."""
    out, seen = [], set()
    fname = os.path.join(build.SRC, 'folders.txt')
    if os.path.exists(fname):
        for line in open(fname, encoding='utf-8'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            path, _, desc = line.partition('|')
            out.append({'path': path.strip(), 'desc': desc.strip()})
            seen.add(path.strip())
    for path in sorted(set(paths.values())):
        parts = path.split('/')
        for k in range(1, len(parts) + 1):
            sub = '/'.join(parts[:k])
            if sub and sub not in seen:
                out.append({'path': sub, 'desc': ''})
                seen.add(sub)
    return out


def insn_row(ln, rom):
    raw = rom[ln.addr:ln.addr + min(ln.size, 16)]
    hexs = ' '.join('{:02X}'.format(b) for b in raw) + (' …' if ln.size > 16 else '')
    return [ln.addr, hexs, 'i' if ln.kind == 'insn' else 'd', ln.text, ln.comment, ln.group]


def host_unit(project, line_index):
    if not hasattr(project, '_host'):
        project._host = {i: u.name for u in project.parsed.units for i in u.lines}
    return project._host.get(line_index)


def unit_data(project, u, res, edges_from, edges_to):
    lines = project.parsed.lines
    rom = project.rom
    out_lines = []
    prev_blank = False
    for i in u.lines:
        ln = lines[i]
        if ln.kind in ('header', 'pseudo'):
            continue
        if ln.kind == 'blank':
            if prev_blank or not out_lines:
                continue
            prev_blank = True
            out_lines.append([None, '', 'b', '', '', ln.group])
            continue
        prev_blank = False
        if ln.kind == 'label':
            out_lines.append([ln.addr, '', 'l', '.' + ln.label.split('.')[-1] if '.' in ln.label else ln.label,
                              '', ln.group])
        elif ln.kind == 'comment':
            out_lines.append([None, '', 'c', '', ln.comment, ln.group])
        elif ln.kind in ('insn', 'data'):
            out_lines.append(insn_row(ln, rom))
        elif ln.kind in ('directive', 'inactive') and ln.text:
            # assembler directives and macro / REPT bodies ('x'); IF branches not assembled ('n')
            out_lines.append([None, '', 'x' if ln.kind == 'directive' else 'n', ln.text, ln.comment, ln.group])
        if ln.xref:
            out_lines[-1] = out_lines[-1][:6] + [ln.xref[0]]   # belongs to a line of another unit
    while out_lines and out_lines[-1][2] == 'b':
        out_lines.pop()
    # instructions elsewhere that belong to this unit's lines (`;=@Unit.tag`), by host unit
    foreign = []
    for i in u.foreign:
        ln = lines[i]
        if ln.kind not in ('insn', 'data', 'label'):
            continue
        host = host_unit(project, i)
        if not foreign or foreign[-1]['host'] != host:
            foreign.append({'host': host, 'lines': []})
        row = insn_row(ln, rom) if ln.kind != 'label' else             [ln.addr, '', 'l', '.' + ln.label.split('.')[-1] if '.' in ln.label else ln.label, '', ln.xref[1]]
        row[5] = ln.xref[1]
        foreign[-1]['lines'].append(row)

    d = {
        'n': u.name, 's': u.start, 'e': u.end, 'k': u.kind,
        'st': (res.status if res.status != 'none' else 'checked') if u.annotated else 'none',
        'lines': out_lines,
        'file': os.path.relpath(os.path.join(build.SRC, lines[u.first_line].file), build.ROOT).replace(os.sep, '/'),
        'foreign': foreign,
        'out': sorted({(t, k) for t, k in edges_from.get(u.name, [])}),
        'in': sorted({(f, k) for f, k in edges_to.get(u.name, [])}),
    }
    if u.name in project.analysis.tables:
        d['table'] = project.analysis.tables[u.name]
    if u.asset:
        d['asset'] = u.asset['type']
    refs = project.analysis.refs.get(u.name)
    if refs:
        name = project.analysis.var_for
        d['refs'] = {k: sorted({name(a) or '${:04X}'.format(a) for a in v}) for k, v in refs.items()}
    if u.annotated:
        f = u.func
        d['def'] = f['def']
        d['doc'] = f['doc']
        d['hdr'] = {k: f[k] for k in ('reads', 'writes', 'clobbers', 'test')}
        d['groups'] = [g['pseudo'] for g in u.pseudo_groups]
        if f['def']:
            try:
                sig = parse_signature(f['def'])
                d['params'] = sig.params
                d['returns'] = sig.returns
            except SyntaxError:
                pass
        d['res'] = res.as_dict()
    return d


def resolve_value(project, v):
    v = v.strip()
    if v in project.syms:
        return project.syms[v]
    if v in project.parsed.consts:
        return project.parsed.consts[v]
    return int(v[1:], 16) if v.startswith('$') else int(v, 0)


def resolve_bgp():
    v = build.GAME.get('bgp')
    return int(str(v).replace('$', '0x'), 0) if v is not None else None


def resolve_obp():
    """game.json "obp": [OBP0, OBP1], the sprite palettes the game sets (the viewer's default is $E4, $C4)."""
    v = build.GAME.get('obp')
    return [int(str(x).replace('$', '0x'), 0) for x in v] if v else None


def run_loader(project, spec):
    """Run a tile-loading routine in the SM83 interpreter; return VRAM tile data ($8000-$97FF).

    spec: 'Routine' or 'Routine(hl=Label,bc=$1000)'; several joined with '+' run one
    after the other on the same VRAM (a base tileset, then what a screen loads over it).
    An argument that is not a register sets a byte of RAM first: 'wFoo=3' or '$FF94=$1E'.
    Banked games: the routine's own bank is switched in (for a bank-0 routine, the bank of a ROM label
    passed in a register, e.g. CopyData(hl=SomeTiles,de=$9000,bc=$200)), and its bank writes switch banks.
    """
    mem = bytearray(0x10000)
    mem[0:0x8000] = project.rom[0:0x8000]
    mem[0xFF44] = 0x91                 # rLY: in VBlank, for loaders that switch the LCD off first
    mbc = MBC(bytes(project.rom), mem) if build.BANKED else None
    regs = ('a', 'b', 'c', 'd', 'e', 'h', 'l', 'f', 'af', 'bc', 'de', 'hl')
    for step in spec.split('+'):
        m = re.match(r'^(\w+)(?:\((.*)\))?$', step)
        name, args = m.group(1), m.group(2) or ''
        bank = build.SYM_BANK.get(name, 0) if project.syms.get(name, 0) >= 0x4000 else 0
        cpu = CPU(mem, mbc)
        for arg in filter(None, args.split(',')):
            reg, _, val = arg.strip().partition('=')
            v = resolve_value(project, val)
            if reg in regs:
                if not bank and val.strip() in project.syms and project.syms[val.strip()] >= 0x4000:
                    bank = build.SYM_BANK.get(val.strip(), 0)
                if len(reg) == 2:
                    cpu.setp(reg, v)
                else:
                    setattr(cpu, reg, v & 0xFF)
            else:                          # a RAM byte
                v_addr = project.parsed.vars[reg]['addr'] if reg in project.parsed.vars else resolve_value(project, reg)
                mem[v_addr] = v & 0xFF
        if mbc is not None and bank:
            mbc.map(bank)
        cpu.sp = build.GAME['test_memory']['stack_top']
        cpu.call(project.syms[name], max_steps=2000000)
    return bytes(mem[0x8000:0x9800])


def render_sprites(project, count):
    """Draw every sprite id with the game's own routine (game.json "sprites"): write the
    object record, set the variables, call the routine, read the OAM entries it wrote."""
    spec = build.GAME.get('sprites')
    if not spec:
        return [[] for _ in range(count)]
    syms, v = project.syms, project.parsed.vars

    def addr(name):
        if isinstance(name, int):
            return name
        if name in v:
            return v[name]['addr']
        return syms[name] if name in syms else int(name, 0)
    num = lambda x: int(x, 0) if isinstance(x, str) else x   # noqa: E731
    oam = addr(spec['oam'])
    out = []
    for sid in range(count):
        mem = bytearray(0x10000)
        mem[0:0x8000] = project.rom[0:0x8000]
        rec = [sid if x == 'id' else num(x) for x in spec.get('record', [])]
        at = addr(spec['record_at'])
        mem[at:at + 16] = bytes(rec + [0] * (16 - len(rec)))
        for name, val in spec.get('set', {}).items():
            mem[addr(name)] = num(val) & 0xFF
        cpu = CPU(mem)
        cpu.sp = build.GAME['test_memory']['stack_top']
        if spec.get('hl'):
            cpu.setp('hl', addr(spec['hl']))
        try:
            cpu.call(syms[spec['routine']], max_steps=200000)
        except Exception:  # noqa: BLE001 - a broken entry just renders empty
            out.append([])
            continue
        if spec.get('oam_end'):
            hi, lo = spec['oam_end']
            end = (mem[addr(hi)] << 8) | mem[addr(lo)]
        else:
            end = oam + 0xA0
        out.append([list(mem[a:a + 4]) for a in range(oam, min(end, oam + 0xA0), 4)])
    return out


def screen_strings(project, rom, a, params):
    """`strings`: a BG map address (word), tiles, then `next` ($FE) and another address, ..., `end` ($FF).
    Returns ({(row, col): tile}, end address)."""
    nxt, stop = resolve_value(project, params.get('next', '$FE')), resolve_value(project, params.get('end', '$FF'))
    cells = {}
    dest = rom[a] | rom[a + 1] << 8
    a += 2
    while True:
        b = rom[a]
        a += 1
        if b == stop:
            return cells, a
        if b == nxt:
            dest = rom[a] | rom[a + 1] << 8
            a += 2
            continue
        cells[((dest & 0x3FF) >> 5, dest & 0x1F)] = b
        dest += 1


def screen_rle(project, rom, a, params):
    """`rlemap`: a BG map address (word), then runs: $80 tile count = count tiles counting up from tile;
    n < $80 = the next byte n times; $80 | n = n bytes as they are; 0 ends."""
    cells = {}
    dest = rom[a] | rom[a + 1] << 8
    a += 2
    out = []
    while rom[a]:
        n = rom[a]
        if n == 0x80:
            t, k = rom[a + 1], rom[a + 2]
            out += [(t + i) & 0xFF for i in range(k)]
            a += 3
        elif n < 0x80:
            out += [rom[a + 1]] * n
            a += 2
        else:
            out += list(rom[a + 1:a + 1 + (n & 0x7F)])
            a += 1 + (n & 0x7F)
    for i, t in enumerate(out):
        d = dest + i
        cells[((d & 0x3FF) >> 5, d & 0x1F)] = t
    return cells, a + 1


def screen_tilemap(project, cells, params, start, end):
    """The rectangle of BG map cells that strings / RLE data cover, as a tilemap asset."""
    blank = resolve_value(project, params.get('blank', '$7F'))
    if not cells:
        cells = {(0, 0): blank}
    r0, r1 = min(r for r, _ in cells), max(r for r, _ in cells)
    c0, c1 = min(c for _, c in cells), max(c for _, c in cells)
    if params.get('screen'):                     # the whole 20 x 18 screen
        r0, c0, r1, c1 = 0, 0, max(r1, 17), max(c1, 19)
    w, h = c1 - c0 + 1, r1 - r0 + 1
    grid = [cells.get((r, c), blank) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]
    return {'type': 'tilemap', 'params': dict(params, width=str(w), height=str(h)),
            'bytes': base64.b64encode(bytes(grid)).decode(), 'length': end - start}


def collect_assets(project):
    assets, tilesets = [], {}
    rom = project.rom
    users = {}       # address -> code units that load it as an immediate (ld hl/de, Label)
    for uname, insns in project.analysis.insns.items():
        for i in insns:
            if i.imm is not None and i.length == 3:
                users.setdefault(i.imm, set()).add(uname)
    for u in project.parsed.units:
        a = u.asset
        if not a:
            continue
        params = {k: v for k, v in a.items() if k not in ('type', 'doc')}
        if a['type'] in ('tilemap', 'rows', 'strings', 'rlemap'):     # BG tile numbers: 8000 = unsigned from $8000, 8800 = signed around $9000 (LCDC bit 4)
            params.setdefault('addressing', str(build.GAME.get('tile_addressing', '8000')))
        if 'range' in params:
            lo, hi = params['range'].split('-')
            start, end = resolve_value(project, lo), resolve_value(project, hi) + 1
        else:
            start = u.start
            end = start + (resolve_value(project, params['length']) if 'length' in params else u.end - u.start)
            if a['type'] == 'tilemap' and 'length' not in params:
                end = start + int(params.get('width', 20)) * int(params.get('height', 18))
            if a['type'] == 'oam' and 'length' not in params:    # (y, x, tile, attr) entries copied up to an end byte
                stop = resolve_value(project, params.get('end', '$FD'))
                end = rom.index(stop, start) + 1     # the copy loops stop at that byte anywhere
            if a['type'] == 'rows' and 'length' not in params:   # tile rows split by a newline byte, up to an end byte
                end = rom.index(resolve_value(project, params.get('end', '$FD')), start) + 1
        item = {'name': u.name, 'type': a['type'], 'params': params, 'doc': a['doc'],
                'start': start, 'length': end - start,
                'bytes': base64.b64encode(bytes(rom[start:end])).decode(),
                'users': sorted(users.get(u.start, ()))}
        if a['type'] == 'rows':          # shown as a small tilemap: one row per line, padded with the blank tile
            data, blank = bytes(rom[start:end - 1]), resolve_value(project, params.get('blank', '$FE'))
            if 'newline' in params:
                rows = [list(r) for r in data.split(bytes([resolve_value(project, params['newline'])]))]
            else:                         # fixed-width rows (the whole run as one row without a width)
                n = int(params.get('width', 0)) or len(data)
                rows = [list(data[i:i + n]) for i in range(0, len(data), n)]
            width = int(params.get('width', 0)) or max(len(r) for r in rows)
            grid = [r + [blank] * (width - len(r)) for r in rows]
            item['type'] = 'tilemap'
            item['params'] = dict(params, width=str(width), height=str(len(grid)))
            item['bytes'] = base64.b64encode(bytes(sum(grid, []))).decode()
            item['length'] = end - start
        if a['type'] in ('strings', 'rlemap'):     # drawn at BG map addresses: shown as the screen area they cover
            cells, end = (screen_strings if a['type'] == 'strings' else screen_rle)(project, rom, start, params)
            item.update(screen_tilemap(project, cells, params, start, end))
        if 'tiles' in params:
            keys = params['tiles'].split('|')
            for key in keys:
                if key not in tilesets:
                    try:
                        tilesets[key] = base64.b64encode(run_loader(project, key)).decode()
                    except Exception as e:  # noqa: BLE001 - report, keep going
                        item['error'] = 'could not run {}: {}'.format(key, e)
            item['tileset'] = keys[0]
            item['tilesets'] = keys
        if a['type'] == 'oam':
            # (y, x, tile, attributes) entries. frame=N: N bytes per frame (several pictures); signed: the y / x
            # are signed offsets from an origin (sprite lists drawn around an object); tall: 8x16 sprites.
            # Entries of four $FF bytes are unused places and left out.
            raw = rom[start:end] if 'length' in params else rom[start:end - 1]    # without the end byte
            ents = [list(raw[i:i + 4]) for i in range(0, len(raw) - 3, 4)]
            if 'signed' in params:
                ents = [[e[0] - 256 if e[0] >= 128 else e[0], e[1] - 256 if e[1] >= 128 else e[1]] + e[2:] for e in ents]
            per = max(1, resolve_value(project, params['frame']) // 4) if params.get('frame') else max(1, len(ents))
            item['sprites'] = [[e for e in ents[i:i + per] if e != [255, 255, 255, 255] and e != [-1, -1, 255, 255]]
                               for i in range(0, len(ents), per)] or [[]]
            if 'tall' in params:
                item['tall'] = 1
        if a['type'] == 'sprites':
            item['sprites'] = render_sprites(project, resolve_value(project, params.get('count', '1')))
        assets.append(item)
    return assets, tilesets


# What each sound is, from where the game requests it (see the annotated callers)
DMG = [(0xE0, 0xF8, 0xD0), (0x88, 0xC0, 0x70), (0x34, 0x68, 0x56), (0x08, 0x18, 0x20)]


def write_png(path, width, height, rgb_rows):
    """A plain RGB PNG (no dependencies)."""
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b''.join(b'\x00' + bytes(row) for row in rgb_rows)
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b'')
    open(path, 'wb').write(png)


def collect_macros(project):
    """Every macro the source defines: name (lower case) -> {n: name, b: body lines, d: what
    its comments say, f: file}. The viewer explains and expands macro lines with it."""
    import measure
    texts = []
    for f in sorted(measure.prelude_files()):
        texts.append((f, open(os.path.join(build.SRC, f), encoding='utf-8', errors='replace').read().split('\n')))
    by_file = {}
    for ln in project.parsed.lines:
        by_file.setdefault(ln.file, []).append(ln.raw)
    texts += sorted(by_file.items())
    out = {}
    for fname, lines in texts:
        i = 0
        while i < len(lines):
            code = measure.code_part(lines[i]).strip()
            m = re.match(r'^MACRO\??\s+(\w+)', code, re.I) or re.match(r'^(\w+):?\s+MACRO\b', code, re.I)
            if not m:
                i += 1
                continue
            doc, j = [], i - 1
            while j >= 0 and lines[j].strip().startswith(';') and not lines[j].strip().startswith(';@'):
                doc.insert(0, lines[j].strip().lstrip(';').strip())
                j -= 1
            tail = lines[i].split(';', 1)
            if len(tail) > 1 and tail[1].strip():
                doc.append(tail[1].strip())
            body, depth, i = [], 1, i + 1
            while i < len(lines) and depth:
                c = measure.code_part(lines[i]).strip()
                if re.match(r'^MACRO\b', c, re.I):
                    depth += 1
                elif re.match(r'^ENDM\b', c, re.I):
                    depth -= 1
                    if not depth:
                        break
                body.append(lines[i].rstrip().replace('\t', '    '))
                i += 1
            while body and body[0].strip().startswith(';'):          # leading comments describe it
                doc.append(body.pop(0).strip().lstrip(';').strip())
            out.setdefault(m.group(1).lower(), {'n': m.group(1), 'b': body, 'd': ' '.join(d for d in doc if d), 'f': fname})
            i += 1
    return out


def data_views(project, paths):
    """Views a game plugin gives its data blocks (game.json "views": a Python file whose
    `views(ctx)` returns {unit name: view}), e.g. the text a text block prints."""
    path = build.GAME.get('views')
    if not path:
        return {}
    import importlib.util
    import types
    spec = importlib.util.spec_from_file_location('game_views', os.path.join(build.ROOT, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    p = project.parsed
    units = []
    for u in p.units:
        units.append(types.SimpleNamespace(
            name=u.name, start=u.start, end=u.end, kind=u.kind, path=paths.get(u.name, ''),
            lines=[(p.lines[i].macro or (p.lines[i].text.split() or [''])[0].lower(), p.lines[i].text, p.lines[i].addr, p.lines[i].size)
                   for i in u.lines if p.lines[i].kind in ('insn', 'data')]))
    by_start = {}
    for u in units:
        by_start.setdefault(u.start, u.name)
    rev = {}
    for name, a in project.syms.items():
        if '.' not in name and not name.startswith('__gb_'):
            rev.setdefault((build.SYM_BANK.get(name, 0), a), name)
    ctx = types.SimpleNamespace(
        rom=project.rom, units=units, unit_named={u.name: u for u in units}, unit_starting=by_start.get,
        syms=project.syms, bank=build.SYM_BANK, linear=build.linear, fmt_rom=build.fmt_rom,
        charmap=build.read_charmap(), consts=p.consts, vars=p.vars,
        label=lambda bank, addr: rev.get((bank, addr)) or rev.get((0, addr)))
    return mod.views(ctx)


HEAVY = ('lines', 'res', 'groups', 'view', 'foreign', 'doc', 'hdr', 'refs')   # what only the Code page and the Book need
ASSET_HEAVY = ('pixels', 'sections', 'doc', 'marks')     # what only drawing an asset needs


def rle(s):
    """'aaab' -> [['a', 3], ['b', 1]] (the ROM map: one letter per byte, in long runs)."""
    out = []
    for ch in s:
        if out and out[-1][0] == ch:
            out[-1][1] += 1
        else:
            out.append([ch, 1])
    return out


def write_asset_chunk(assets):
    """The pixels / sections / marks of every asset into gen/assets.js, loaded when an asset is drawn."""
    body = {}
    for a in assets:
        heavy = {k: a.pop(k) for k in ASSET_HEAVY if k in a}
        if heavy:
            body[a['name']] = heavy
            a['lazy'] = 1
    os.makedirs(os.path.join(OUT_DIR, 'gen'), exist_ok=True)
    with open(os.path.join(OUT_DIR, 'gen', 'assets.js'), 'w', encoding='utf-8', newline='\n') as f:
        f.write('GBA({});\n'.format(json.dumps(body, separators=(',', ':'))))


def write_chunks(units):
    """Move the bulky per-unit fields out of the page into gen/c/<n>.js, one file per folder, so the page
    starts with a small index and the viewer loads a folder's bodies when it shows them (a <script> tag,
    which also works for a page opened from disk). u['c'] = the chunk number."""
    import shutil
    root = os.path.join(OUT_DIR, 'gen', 'c')
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    groups = {}
    for u in units:
        body = {k: u.pop(k) for k in HEAVY if k in u}
        if body:
            groups.setdefault(u.get('p') or '', []).append((u, body))
    for i, (folder, members) in enumerate(sorted(groups.items())):
        chunk = {}
        for u, body in members:
            u['c'] = i
            chunk[u['n']] = body
        with open(os.path.join(root, '{}.js'.format(i)), 'w', encoding='utf-8', newline='\n') as f:
            f.write('GBC({},{});\n'.format(i, json.dumps(chunk, separators=(',', ':'))))


def read_intro():
    """src/intro.md: the Book's optional chapter 0, a short story from power-on to the first
    level told through the code (Markdown; `Name` links to a function, variable, asset or folder)."""
    path = os.path.join(build.SRC, 'intro.md')
    return open(path, encoding='utf-8').read() if os.path.exists(path) else None


def check_intro(data):
    """Warn about `Name`s and [text](target)s in intro.md that the viewer can't link."""
    if not data.get('intro'):
        return
    known = ({u['n'] for u in data['units']} | {v['name'] for v in data['vars']}
             | {a['name'] for a in data['assets']} | {f['path'] for f in data['folders']})
    for m in re.finditer(r'`([^`]+)`|\]\(([^)\s]+)\)', data['intro']):
        name = m.group(1) or m.group(2)
        if m.group(2) and re.match(r'(https?:|#/)', name):
            continue
        # `code` that doesn't look like a name ($9C, rst $30, x + 1) is meant as code
        looks_like_name = re.fullmatch(r'[A-Za-z_][\w/]*', name) and not re.fullmatch(r'[a-z]+', name)   # not `rst`, `call`
        if name not in known and (m.group(2) or looks_like_name):
            print('intro.md: no function, variable, asset or folder named', name)


def write_thumbnail(assets, tilesets, path, scale=2):
    """The tilemap asset named in game.json "thumbnail" as a PNG in DMG colours (for the hub),
    or a .png under out/site/ (e.g. a poster an asset plugin wrote into gen/)."""
    name = build.GAME.get('thumbnail')
    if name and name.endswith('.png'):          # a picture an asset plugin wrote (out/site/gen/...)
        src = os.path.join(os.path.dirname(path), name)
        if not os.path.exists(src):
            return False
        shutil.copyfile(src, path)
        return True
    a = next((x for x in assets if x['name'] == name and x['type'] == 'tilemap'), None)
    if not a or a.get('tileset') not in tilesets:
        return False
    tmap = base64.b64decode(a['bytes'])
    tiles = base64.b64decode(tilesets[a['tileset']])
    w, h = int(a['params'].get('width', 20)), int(a['params'].get('height', 18))
    signed = a['params'].get('addressing') == '8800'
    rows = []
    for py in range(h * 8):
        row = []
        for px in range(w * 8):
            tile = tmap[(py // 8) * w + px // 8]
            off = (tile + 256 if signed and tile < 128 else tile) * 16 + (py % 8) * 2
            lo, hi = tiles[off], tiles[off + 1]
            bit = 7 - px % 8
            row.extend(DMG[((hi >> bit) & 1) << 1 | ((lo >> bit) & 1)] * scale)
        for _ in range(scale):
            rows.append([c for rgb in zip(row[0::3], row[1::3], row[2::3]) for c in rgb])
    write_png(path, w * 8 * scale, h * 8 * scale, rows)
    return True


def other_games():
    """The games of the hub this site is built for ($GBBOLT_GAMES: a games.json), for
    the selector in the viewer's header. Empty for a site built on its own."""
    path = os.environ.get('GBBOLT_GAMES')
    if not path or not os.path.exists(path):
        return []
    return [{'id': g['id'], 'title': g['title']} for g in json.load(open(path, encoding='utf-8'))]


def collect_audio():
    """The rendered sounds (out/site/audio/index.json) with names and labels from src/sound.json."""
    path = os.path.join(OUT_DIR, 'audio', 'index.json')
    if not os.path.exists(path):
        return {'items': [], 'kinds': {}}
    cfg_path = os.path.join(build.SRC, 'sound.json')
    cfg = json.load(open(cfg_path, encoding='utf-8')) if os.path.exists(cfg_path) else {}
    names = cfg.get('names', {})
    items = json.load(open(path))
    for it in items:
        name, used = names.get('{} {}'.format(it['kind'], it['id']), ['{} {}'.format(it['kind'], it['id']), ''])
        it['name'], it['usedBy'] = name, used
    kinds = {k: v.get('label', k) for k, v in cfg.get('kinds', {}).items()}
    return {'items': items, 'kinds': kinds}


def generate(project):
    p = project.parsed
    results = project.results
    edges_from, edges_to = {}, {}
    for a, b, k in project.analysis.edges:
        edges_from.setdefault(a, []).append((b, k))
        edges_to.setdefault(b, []).append((a, k))

    from verify import Result
    for u in p.units:                       # `page` after edits: units the last verify hasn't seen
        if u.name not in results:
            results[u.name] = Result(u)
    units = [unit_data(project, u, results[u.name], edges_from, edges_to) for u in p.units]
    paths = unit_paths(project)
    for d in units:
        d['p'] = paths.get(d['n'], '')
    views = data_views(project, paths)
    for d in units:
        if d['n'] in views:
            d['view'] = views[d['n']]

    rommap = ['d'] * len(project.rom)
    for u in p.units:
        if u.kind != 'code':
            continue
        st = results[u.name].status
        c = (STATUS_CODE[st] if st != 'none' else 'k') if u.annotated else 'c'   # annotated, not verified yet: checked
        for a in range(u.start, min(u.end, len(rommap))):
            rommap[a] = c
    # bytes inside code units that are data (db lines) stay 'd'
    for ln in p.lines:
        if ln.kind == 'data' and ln.addr is not None:
            for a in range(ln.addr, min(ln.addr + ln.size, len(rommap))):
                rommap[a] = 'd'

    banks = None
    if build.BANKED:
        # banked games: bytes outside every section are free space; the banks with their sections
        sections = [s for s in build.read_map() if s[0] in ('ROM0', 'ROMX') and s[3] > s[2]]
        inside = bytearray(len(project.rom))
        for kind, bank, start, end, name in sections:
            lo = build.linear(bank, start)
            inside[lo:lo + end - start] = b'' * (end - start)
        for a, used in enumerate(inside):
            if not used:
                rommap[a] = 'e'
        banks = [{'b': b, 'sections': [[name, build.linear(bank, start), build.linear(bank, start) + end - start]
                                       for kind, bank, start, end, name in sections if bank == b]}
                 for b in range(len(project.rom) // 0x4000)]

    # memory references for the RAM map (static scan of every code unit)
    an = project.analysis
    access = {}
    for uname, refs in an.refs.items():
        for kind, addrs in refs.items():
            for a in addrs:
                access.setdefault(a, {'reads': set(), 'writes': set(), 'ptrs': set()})[kind].add(uname)
    # plus what annotation headers declare (accesses through pointers the static scan misses)
    for u in p.units:
        if not u.annotated:
            continue
        for kind in ('reads', 'writes'):
            for name in u.func[kind]:
                v = p.vars.get(name)
                if v:
                    access.setdefault(v['addr'], {'reads': set(), 'writes': set(), 'ptrs': set()})[kind].add(u.name)

    variables = []
    named = set()
    for v in p.vars.values():
        span = range(v['addr'], v['addr'] + max(1, v['size']))
        named.update(span)
        acc = {'reads': set(), 'writes': set(), 'ptrs': set()}
        for a in span:
            for k in acc:
                acc[k] |= access.get(a, {}).get(k, set())
        if v['type'] == 'io' and not any(acc.values()):
            continue
        variables.append({'name': v['name'], 'addr': v['addr'], 'type': v['type'], 'size': v['size'],
                          'desc': v['desc'], 'reads': sorted(acc['reads']), 'writes': sorted(acc['writes']),
                          'ptrs': sorted(acc['ptrs'])})
        if 0xA000 <= v['addr'] < 0xC000 and v.get('bank'):
            variables[-1]['bank'] = v['bank']             # SRAM bank (the save file)
    variables.sort(key=lambda v: (v.get('bank', 0) if 0xA000 <= v['addr'] < 0xC000 else 0, v['addr']))
    unnamed = []
    # pointers only (no access through them by name), inside a memory region the
    # source names (`WORK_RAM0 + $FFF` etc.: start of a downward fill)
    regions = [(p.consts[c], size) for c, size in (('WORK_RAM0', 0x1000), ('WORK_RAM1', 0x1000),
                                                 ('OAM_START', 0x100), ('HRAM_START', 0x7F))
               if c in p.consts]
    for a in sorted(access):
        if a in named or a < 0xA000 or 0xFF00 <= a < 0xFF80 or a == 0xFFFF:
            continue
        acc = access[a]
        if not acc['reads'] and not acc['writes'] and any(s <= a < s + n for s, n in regions):
            continue
        unnamed.append({'addr': a, 'reads': sorted(acc['reads']), 'writes': sorted(acc['writes']),
                        'ptrs': sorted(acc['ptrs'])})

    consts = {k: {'value': v, 'doc': p.constdoc.get(k, '')} for k, v in p.consts.items()}
    assets, tilesets = collect_assets(project)
    import assets as plugin_assets          # the game's own asset plugins (<game>/assets/*.py)
    assets += plugin_assets.collect(project, OUT_DIR)
    by_name = {d['n']: d for d in units}
    for a in assets:                        # a plugin asset that draws a data block ('unit': its label)
        d = by_name.get(a.get('unit'))
        if d is not None and 'asset' not in d:
            d['asset'] = a['type']
            if a['name'] != d['n']:
                d['an'] = a['name']
    builtin = {a['name']: a for a in assets if not a.get('generated')}
    for a in assets:                        # per-tile palettes a plugin found for tile sheets: {asset: [palette | None]}
        for n, pals in (a.pop('tilePalettes', None) or {}).items():
            if n in builtin:
                builtin[n]['tilePalettes'] = pals

    data = {
        'title': build.GAME.get('title') or build.rom_title(project.rom),
        'gameId': build.GAME.get('id'),
        'games': other_games(),
        'game': {'entry': build.GAME.get('entry'), 'start': build.GAME.get('start'), 'bgp': resolve_bgp(), 'obp': resolve_obp(), 'graphRoot': build.GAME.get('graph_root'),
                 'jumpRst': build.GAME.get('jump_table_rst'),
                 'sprites': (build.GAME.get('sprites') or {}).get('routine')},
        'generated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
        'build': {'ok': project.build_ok, 'msg': project.build_msg, 'sha1': build.sha1(build.BUILT)},
        'units': units,
        'rommap': rle(''.join(rommap)),
        'banked': bool(build.BANKED),
        'macros': collect_macros(project),
        'banks': banks,
        'vars': variables,
        'unnamed': unnamed,
        'folders': folder_list(paths),
        'intro': read_intro(),
        'consts': consts,
        'helpers': HELPER_DOCS,
        'assets': assets,
        'audio': collect_audio(),
        'tilesets': tilesets,
        'globalChecksum': sum(b for i, b in enumerate(project.rom) if i not in (0x14E, 0x14F)) & 0xFFFF,
        'verifySeconds': round(project.verify_seconds, 1),
    }
    check_intro(data)
    write_chunks(data['units'])
    thumb_assets = [dict(a) for a in data['assets']]       # the thumbnail below needs the pixels
    write_asset_chunk(data['assets'])
    html = open(TEMPLATE, encoding='utf-8').read()
    payload = json.dumps(data, separators=(',', ':')).replace('</', '<\\/')
    html = html.replace('/*__GBBOLT_DATA__*/null', payload)
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, 'index.html')
    open(path, 'w', encoding='utf-8', newline='\n').write(html)
    # a short summary for the hub page that lists all games
    funcs = [u for u in units if u['k'] == 'code']
    thumb = write_thumbnail(thumb_assets, tilesets, os.path.join(OUT_DIR, 'thumb.png'))
    summary = {'id': build.GAME.get('id'), 'title': data['title'], 'sha1': data['build']['sha1'],
               'thumbnail': 'thumb.png' if thumb else None,
               'publisher': build.GAME.get('publisher'), 'year': build.GAME.get('year'),
               'functions': len(funcs), 'annotated': sum(1 for u in funcs if u.get('def')),
               'verified': sum(1 for u in funcs if u.get('st') == 'verified'),
               'checked': sum(1 for u in funcs if u.get('st') == 'checked'),
               'sounds': len(data['audio']['items']), 'assets': len(data['assets']),
               'generated': data['generated']}
    json.dump(summary, open(os.path.join(OUT_DIR, 'summary.json'), 'w', encoding='utf-8'), indent=1)
    return path
