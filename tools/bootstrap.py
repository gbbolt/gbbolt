"""One-time bootstrap: trace the ROM and create the initial disassembly in src/.

    python tools/bootstrap.py [--force] [--coverage FILE]

Runs tools/trace.py to produce <rom>.sym (code/data map), then mgbdis
(from ../mgbdis next to the project, or $GBBOLT_MGBDIS) with the vector labels it normally hard-codes removed - the
tracer knows where code really is, and mgbdis' fixed 8-byte vector blocks
would cut code that runs on past a vector in half (Tetris' serial handler).

src/ is hand-edited after this, so the script refuses to overwrite it.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build  # noqa: E402
ROOT = build.ROOT                                   # the game's project folder (with game.json)
MGBDIS = os.environ.get('GBBOLT_MGBDIS') or os.path.join(ROOT, '..', 'mgbdis', 'mgbdis.py')
ROM = build.BASEROM
SRC = build.SRC


def main():
    os.makedirs(SRC, exist_ok=True)
    if os.path.exists(os.path.join(SRC, 'bank_000.asm')) and '--force' not in sys.argv:
        sys.exit('src/ already exists and is hand-edited; pass --force to regenerate it')
    subprocess.check_call([sys.executable, os.path.join(HERE, 'trace.py'), ROM] +
                          [a for a in sys.argv[1:] if a != '--force'])     # e.g. --coverage FILE

    source = open(MGBDIS, encoding='utf-8').read()
    marker = 'gbc_symbols = ['
    patch = ("default_symbols = [s for s in default_symbols\n"
             "                   if int(s.split()[0].split(':')[1], 16) >= 0x104]\n")
    source = source.replace(marker, patch + marker, 1)
    tiny = ['--tiny'] if os.path.getsize(ROM) <= 0x8000 else []
    sys.argv = ['mgbdis.py', ROM] + tiny + ['--output-dir', SRC, '--hli', 'hli',
                '--ldh_a8', 'ldh_ffa8', '--indent-tabs', '--overwrite']
    sys.path.insert(0, os.path.dirname(MGBDIS))
    exec(compile(source, MGBDIS, 'exec'), {'__name__': '__main__', '__file__': MGBDIS})
    postprocess()


def postprocess_banked(tables):
    """Banked ROMs: every table the tracer found (<rom>.tables.json: "bank:addr" -> entry names) as `dw`."""
    names = {}
    for line in open(os.path.splitext(ROM)[0] + '.sym'):
        if not line.startswith(';') and ' .' not in line:
            loc, name = line.split()
            names[name] = loc
    for f in sorted(os.listdir(SRC)):
        if not f.startswith('bank_'):
            continue
        path = os.path.join(SRC, f)
        lines = open(path).read().split('\n')
        out, i = [], 0
        while i < len(lines):
            out.append(lines[i])
            name = lines[i].rstrip(':') if lines[i].endswith(':') and not lines[i].startswith(('\t', ' ')) else None
            entries = tables.get(names.get(name)) if name else None
            i += 1
            if not entries:
                continue
            data = []
            while len(data) < 2 * len(entries) and i < len(lines) and lines[i].strip().startswith('db '):
                data += [int(v.strip()[1:], 16) for v in lines[i].strip()[3:].split(',')]
                i += 1
            n = min(len(entries), len(data) // 2)
            out += ['\tdw {}'.format(e) for e in entries[:n]]
            if len(data) > 2 * n:
                out.append('\tdb ' + ', '.join('${:02x}'.format(b) for b in data[2 * n:]))
        open(path, 'w', newline='\n').write('\n'.join(out))


def fix_undefined():
    """Labels mgbdis did not place (code read at two alignments): refer to them by address instead, and
    say so in a comment, until the ROM links."""
    import re
    import build as b
    addr = {}
    for line in open(os.path.splitext(ROM)[0] + '.sym'):
        if not line.startswith(';') and ' .' not in line:
            loc, name = line.split()
            addr[name] = '$' + loc.split(':')[1]
    for _ in range(20):
        ok, msg = b.build(quiet=True)
        print(msg.split('\n')[0])
        if ok:
            return
        bad = re.findall(r"Undefined symbol `([^`]+)`\s+at (\S+?)\((\d+)\)", msg)
        if not bad:
            print(msg[:2000])
            return
        for sym, f, ln in bad:
            path = os.path.join(SRC, os.path.basename(f))
            lines = open(path).read().split('\n')
            k = int(ln) - 1
            if sym in addr and sym in lines[k] and ';' not in lines[k]:
                lines[k] = re.sub(r'\b' + re.escape(sym) + r'\b', addr[sym], lines[k]) + \
                    ' ; {} (not an instruction start in this disassembly)'.format(sym)
            open(path, 'w', newline='\n').write('\n'.join(lines))


def postprocess():
    """Jump tables as `dw Label`, and an (empty) ram.inc for named variables."""
    tj = os.path.splitext(ROM)[0] + '.tables.json'
    if os.path.getsize(ROM) > 0x8000 and os.path.exists(tj):
        import json
        postprocess_banked(json.load(open(tj)))
        finish()
        fix_undefined()
        return
    finish_single()


def finish_single():
    labels = {}
    for line in open(os.path.splitext(ROM)[0] + '.sym'):
        if line.startswith(';') or ' .' in line:
            continue
        loc, name = line.split()
        labels[int(loc.split(':')[1], 16)] = name

    path = os.path.join(SRC, 'bank_000.asm')
    lines = open(path).read().split('\n')
    out, i = [], 0
    while i < len(lines):
        out.append(lines[i])
        if lines[i].startswith('JumpTable_'):
            i += 1
            data = []
            while i < len(lines) and lines[i].strip().startswith('db '):
                data += [int(v.strip()[1:], 16) for v in lines[i].strip()[3:].split(',')]
                i += 1
            for k in range(0, len(data), 2):
                w = data[k] | (data[k + 1] << 8)
                out.append('\tdw {}'.format(labels.get(w, '${:04x}'.format(w))))
            continue
        i += 1
    open(path, 'w', newline='\n').write('\n'.join(out))
    finish()


def finish():
    open(os.path.join(SRC, 'ram.inc'), 'w', newline='\n').write(
        '; Named RAM / IO addresses.\n'
        '; Format: DEF name EQU $addr ;@ type description\n'
        ';   type: u8 | u16 (little endian) | u8[N] | code[N] | const\n'
        '; Lines without ";@" are plain constants.\n\n')
    game = os.path.join(SRC, 'game.asm')
    text = open(game).read().replace('INCLUDE "hardware.inc"',
                                     'INCLUDE "hardware.inc"\nINCLUDE "ram.inc"')
    open(game, 'w', newline='\n').write(text)


if __name__ == '__main__':
    main()
