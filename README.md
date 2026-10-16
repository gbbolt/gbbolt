# gbbolt

**Game Boy disassemblies with checked pseudo-code written next to the assembly**, and
a Godbolt-style viewer generated from them.

**Live: <https://gbbolt.lingora.org>** - the games built with gbbolt (Tetris so far).

The assembly stays the source of truth: annotations are ordinary asm comments, so
RGBDS ignores them and the ROM still rebuilds byte for byte. The tools read the
comments, check the pseudo-code against the real code in an emulator, and render
the result: code and pseudo-code side by side, a book, call graph, RAM map, the game's
graphics, and its music as a piano roll with mute / solo per channel.

This repository is the engine. Each game lives in its own repository (for example
[tetris-gbbolt](https://github.com/gbbolt/tetris-gbbolt)) and is listed in
[`games.json`](games.json); the site above is built from all of them.

## Using it on a game

A game project is a folder with a `game.json` and the disassembly in `src/`. Run the
tools from there (or set `GBBOLT_ROOT`):

```
cd tetris-gbbolt
python ../gbbolt/tools/gbbolt.py            # build, fixheaders, verify, stamp, site - one pass
python ../gbbolt/tools/gbbolt.py verify     # check annotations (verify Name1 Name2 ... for a subset)
python ../gbbolt/tools/gbbolt.py stamp      # record ;@ sig: for every passing annotation
python ../gbbolt/tools/audio.py             # render the music and sound effects
```

Then open `out/site/index.html`. Everything except the audio is in that one file.

Editing helpers (all rebuild the ROM and undo their change if it no longer matches):

```
tools/rename.py OLD NEW                          rename a label everywhere
tools/rename.py '$ffe1' hGameState u8 "text"     name a RAM address
tools/addlabel.py 415F Font1bpp "comment"        label inside a data block
tools/describe.py hLevel u8 "text"               change a variable's type / description
tools/gbbolt.py show Name1 Name2 ...             source + callers / callees, for annotating
tools/apply.py block.asm                         swap in rewritten (annotated) units
tools/regroup.py extract NAME... > block.asm     units as text, to rework the ;> lines
tools/regroup.py check block.asm                 only ;> / ;= lines changed?
tools/regroup.py stats [NAME...]                 instructions per pseudo-code group
tools/setpath.py folder/sub Name...              put units into a virtual folder
tools/songdata.py                                decode the song data (Tetris sound engine format)
```

`apply.py` takes files holding complete units (headers, label, body). Each replaces the
unit with the same label. A label that doesn't exist yet is inserted after the previous
unit, which splits it. `;! absorb Label` removes a fragment unit that the new version
takes over.

Requires Python 3.9+ with numpy, RGBDS (`../rgbds/` next to the game folder, `$GBBOLT_RGBDS`,
or the `PATH`) and ffmpeg for the audio.

## Annotation format

```asm
;@ def CopyBytes(src: hl, dest: de, count: bc)     <- header: a Python def, args bound to registers
;@ path: lib/memory                                 <- virtual folder
;@ Copies `count` bytes from src to dest.           <- free text = description
;@ clobbers: a, bc, de, hl                          <- optional: registers destroyed
;@ reads: hFoo        writes: wBar                  <- optional: memory touched directly
;@ test: count = rand(1, 0x100)                     <- setup for the differential test
;@ sig: 4f2a99c1                                    <- written by `gbbolt.py stamp`
CopyBytes::
;> copy(dest, src, count or 0x10000)                <- pseudo-code for the asm lines below it
	ld a, [hli]
	...
```

* Labels: `Name::` starts a unit (function or data block). `.name` is local to it;
  other units jump to it as `Unit.name`. `Name:` (one colon) names a fragment that
  sits at the end of one unit but belongs to another function.
* `;@` lines sit directly above the label. `;>` lines are pseudo-code. Each run of
  `;>` lines describes the instructions that follow it, up to the next `;>` or `;=`.
  That is what the viewer highlights when you hover. Aim for one Python statement per
  run of a few instructions.
* When the code for one line is not contiguous (an else branch placed further down, a
  shared exit, a loop's back-jump), tag the line and link the other runs to it:

  ```
  ;>@ack         hSerialTx = master_answer     <- `;>@name`: always a group of its own
  ...
  ;=@ack                                       <- the instructions below also belong to it
  .inStep
  	ld a, c
  	ldh [hSerialTx], a
  ```

  Tags are local to the function and may be referenced before they are defined. Code
  that physically sits inside another unit links with `;=@Function.name`; the owner's
  assembly pane then shows it under "elsewhere". Read top to bottom, the `;>` lines
  (tags removed) are the Python function.
* `;@ path: game/piece` puts a unit into a virtual folder, as if the game were a
  codebase with subfolders. The folders, their order and a one-line description each
  live in `src/folders.txt`. They drive the tree in the Code sidebar, the chapters of
  the Book and the folder filter of the call graph. A data block without a path takes
  the folder most of the code that refers to it lives in.
* Return values: `-> a`, `-> hl`, `-> carry`, `-> (hl, de)`.
* The pseudo-code is a small Python dialect, so it can be executed:
  * RAM names from `src/ram.inc` are variables: `hGameState = 3` writes memory.
  * Arrays index like lists (`wScore[0]`).
  * Constants and labels are numbers. `addr(name)` gives a variable's address.
  * `mem[a]` / `mem16[a]` access any address.
  * Annotated functions are called like Python functions.
  * Helpers: `copy`, `fill`, `bcd_read`, `bcd_write`, `bcd_to_int`, `lo`, `hi`, `u8`, `swap`,
    `forever`, plus hardware-only ones (`wait_ly`, `wait_hblank`, `goto`, `pop_return_address`,
    `set_rom_bank`, ...). The full list with descriptions is in `tools/pseudo.py`.
* `;@ test:` lines are Python run before each trial. They can call `rand(lo, hi)`,
  `rand_ram(n)`, `rand_bcd(nbytes)`, `fill_bcd(addr, n)`, `rng`, `mem`, `play(kind, n, frames)`
  (puts the sound engine into a realistic state), and can set RAM variables
  (`hGameState = 0`). `;@ test: skip <reason>` turns differential testing off.
* Variables in `src/ram.inc`: `DEF name EQU $addr ;@ type description` with type
  `u8`, `u16`, `u8[N]`, `code[N]` or `const`.

## Assets

Data blocks can be marked as assets so the viewer's **Assets** tab can show them:

```asm
;@ asset: tilemap width=20 height=18 tiles=LoadTitleTiles
;@ The title screen.                                <- free text = description
TitleScreenTilemap::
	db $8e, $8e, ...
```

| type | parameters | shows |
|---|---|---|
| `logo` | — | the 48-byte Nintendo logo as the boot ROM draws it |
| `header` | `range=$0100-$014F` | decoded cartridge header, checksums recomputed |
| `tiles` | `bpp=1\|2`, `length=`, `width=` (tiles per row, default 16), `tall` (8x16 sprite tiles: pairs one above the other, 8 pairs per row by default) | a tile sheet, hover for tile number / ROM address |
| `tilemap` | `width=`, `height=`, `tiles=Routine` or `tiles=Routine(hl=Label,bc=$1000)` | the screen exactly as the game shows it |
| `sprites` | `count=`, `tiles=...` | every sprite id, drawn by the game's own routine (`game.json` → `sprites`) |
| `oam` | `tiles=...`, `end=$FD` or `length=`, `frame=` (bytes per frame), `signed` (y / x are signed offsets), `tall` (8x16 sprites) | a sprite layout stored as OAM entries (y, x, tile, attributes); several frames side by side; sprite palettes from `game.json` → `obp` |
| `rows` | `tiles=...`, `width=` or `newline=`, `blank=` | a string of tiles (messages, text panels) |

For tilemaps, `tiles=` names the game's own tile-loading routine. The generator runs it
 (with the given registers) and uses the VRAM it leaves behind, so no tile layout has to be
described by hand. Several loaders can be chained with `+` (a base tile set, then what a screen loads
over it). `game.json` → `tile_addressing: "8800"` for games whose BG uses the signed tile numbers. In banked
games the loader runs with its own bank switched in and its bank writes switch banks.

### Asset plugins

Everything else a game wants to show lives in its own repository, in `assets/*.py`: each file
has a `build(ctx)` that returns assets made of a few display parts the viewer knows - `image`,
`video` (with lanes of events under it, e.g. the buttons held), `chart` (line, step, bar),
`table` (cells may link into the code or hold small images) and `tracks` (audio). `ctx` gives the
plugin the ROM and its names, a `GameRunner` (`tools/gamerun.py`) that runs the game's own code
frame by frame and renders the screen from VRAM / OAM, the tile loaders, and video and audio
writers (the game's sound engine, with RAM pokes and a different update rate). So a world map, a
table of monsters or a replay needs no change to the engine. Results are cached in
`out/site/gen/` and rebuilt only when the plugin, the ROM or the engine changes. See
`tools/assets.py` for the details and the Dr. Mario repository for examples.

## What is specific to a game

What the tools need to know about a particular game lives in its project, not in the tools:

| file | what |
|---|---|
| `game.json` | id, ROM file and its SHA1, title (default: from the cartridge header), main asm file, rgblink / rgbfix flags, the function the Code view opens with (`start`; default `MainLoop`, else the first function), the entry point and the call graph's default focus, the `rst` jump-table convention (if the game has one), extra entry points for the tracer, the memory the differential tests may use, how to draw a sprite with the game's own routine |
| `src/folders.txt` | the virtual folders and their descriptions |
| `src/intro.md` | optional: chapter 0 of the Book, a short story from power-on to the first level told through the code (below) |
| `src/sound.json` | how to drive the sound engine (below) |
| `src/ram.inc` | RAM names, types and descriptions |

The original ROM is optional: without it the build is checked against the `sha1` in
`game.json`, which is how the site is built in CI. Tools that edit the source find the
file a label lives in, so a disassembly split into several files works. Not supported
yet: bank switching (games bigger than 32 KB).

## The intro chapter

A game may tell its story from power-on to finishing the first level in `src/intro.md`.
The Book shows it as chapter 0, its `##` sections are listed in the table of contents,
and `#/book/intro` links to it. It is a small Markdown:

- `# Title` (the chapter's name), `## Section`, paragraphs, `- ` lists, `**bold**`, `*italic*`;
- `` `Name` `` links to a function, RAM variable, asset or folder of that name; anything
  else in backticks (`` `$9C` ``, `` `rst $30` ``) is shown as code;
- `[text](Name)` links text to the same targets, or to a URL.

The build warns about names it can't link. Keep it short and quick: what the code does,
in the order it happens, with a link on every routine worth a look.

## Sound and the Music view

`tools/audio.py` runs the game's own sound engine (init once, the
request, then the update routine once per frame) and feeds every sound register write
into an APU model. Per sound it writes the mix, one file per channel (for mute / solo)
and a timeline: the notes of each channel with frequency and volume frame by frame,
duty, envelope, sweep, waveform, noise settings, panning, who owned the channel (music
or an effect) and the pattern byte that played each note. Songs are rendered once
through and loop from their loop point.

`src/sound.json` says how to drive the engine: its entry points, the request and
"playing" bytes per kind of sound, the music channel structs (to find the pattern byte
behind a note and the song structure), which channels each kind of effect takes over,
the names of all sounds, and where the song data is.

The **Music** view shows each sound as a piano roll (notes fade with their envelope,
slides are slanted, effects that take over a channel are outlined in amber), with mixer
strips for the settings at the playhead, the pattern structure of the song, a tracker
table, and mute / solo per channel. Double-click a note to open its pattern byte in the
disassembly.

## What `verify` checks

| check | how |
|---|---|
| ROM matches | rebuild with RGBDS, compare the SHA1 with the original |
| names | every name in the pseudo-code must resolve (variable, label, helper, parameter) |
| header | declared parameters / clobbers / reads / writes vs. what the code really touches (static scan + observed during tests) |
| differential test | 64 random machine states: the original code runs on the tools' Game Boy CPU interpreter (`tools/sm83.py`), the pseudo-code in Python; all memory and the declared return registers must be identical |
| staleness | `;@ sig:` is a CRC of the function's bytes; it shows up as stale if they change |

Status per function: **verified** (differential test passed), **checked** (names and header OK, not runnable:
hardware access, never returns, or calls something without pseudo-code yet), **failing**, **stale**.
`--strict` makes `verify` exit with an error if anything fails (used in CI).

## The site

`tools/ci_build.py games.json _site` clones every game of `games.json`, builds it from
source, verifies it, renders its audio and writes its viewer to `_site/<id>/`; then
`tools/hub.py` writes the page that lists them. `.github/workflows/pages.yml` does this
on every push, every night and on demand, and deploys to GitHub Pages. Adding a game:
create its repository, add it to `games.json`.

## Layout

```
games.json          the games of the site
tools/gbbolt.py     build / verify / stamp / site
tools/asmparse.py   reads the asm + annotations, maps every line to address and bytes
tools/analyze.py    call graph, memory references
tools/pseudo.py     pseudo-code compiler / runtime
tools/verify.py     the checks above
tools/sm83.py       SM83 decoder and interpreter
tools/site_gen.py   writes out/site/index.html from tools/viewer.html
tools/audio.py      sound engine -> mix, per-channel tracks and timelines
tools/hub.py        the page listing all games
tools/ci_build.py   builds the whole site
tools/bootstrap.py  one-time: trace a ROM + run mgbdis to start a new disassembly
tools/trace.py      recursive code tracer (knows `rst` jump tables)
```

## License

MIT (see [LICENSE](LICENSE)) for gbbolt itself. The games are the property of their
publishers; no ROMs are part of this or any game repository.
