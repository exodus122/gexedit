# gexedit

A map editor for the Gex Game Boy Color disassemblies —
[gex2gbc](../gex2gbc) (*Gex: Enter the Gecko*) and
[gex3gbc](../gex3gbc) (*Gex 3: Deep Cover Gecko*).

One editor, both games. Everything that differs between the two is declared in
`gexedit/profiles/*.json`; there is no game-specific code.

## Requirements

- Python 3.8+
- Pillow and numpy (`pip install -r requirements.txt`, or `pip install -e .`)
- Tk, for the GUI. It ships with the python.org Windows and macOS builds; on Debian or
  Ubuntu it is `apt install python3-tk`. The `--list` and `--render` paths need no
  display and no Tk.

## Using it

```sh
python3 -m gexedit ../gex2gbc                 # open the editor on a repo
python3 -m gexedit ../gex3gbc --list          # list the maps
python3 -m gexedit ../gex2gbc --render MAP_TOON_TV_OUT_OF_TOON -o out.png --scale 2
python3 -m gexedit --games                    # what it knows how to open
```

Installed (`pip install -e .`) the same thing is just `gexedit ../gex2gbc`.

### In the editor

Pick a map on the left, a block on the right, and paint. The map view renders only the
cells you can see, so a 128x128 gex2 map scrolls as smoothly as a small gex3 one.

| | |
|---|---|
| left click / drag | paint the selected block |
| right click       | pick the block under the cursor |
| wheel             | scroll · **ctrl+wheel** zoom |
| `+` / `-`         | zoom in / out |
| `f`               | fit the map to the window |
| `g`               | grid |
| `c`               | collision overlay |
| ctrl+Z / ctrl+Y   | undo / redo |
| ctrl+S            | save the map |
| ctrl+O            | open another repo |

Saving writes the blockmap back in whatever shape the game stores it — one flat plane
for gex2, `_map.bin` plus `_map_extended.bin` for gex3 — so the disassembly rebuilds
from it directly.

The collision overlay colours each sub-cell by its collision id rather than drawing a
collision tileset, since neither repo ships one and "these two blocks collide
differently" is the thing you actually want to see.

Which game a repo is gets detected from the map table its source contains, so there is
nothing to configure. `--game` forces it, and a `.gexedit.json` at a repo's root
overrides both — useful for a fork whose layout has moved.

## How it finds anything

Nothing here hardcodes a filename. Both disassemblies already keep a per-map manifest
in their source, and the editor reads it:

- **gex3** — each `.data_03_XXXX_MapData_<Map>` record names every layer with a
  `farpointer` and carries the map's size in a `map_geometry` macro.
- **gex2** — each record in `.data_00_2ebf_MapData` names its layers with `BANK(label)`.

Those labels are then resolved to the files they `INCBIN`, so renaming a file or
repointing a map in the disassembly is followed automatically. Where a layer resolves to
a build artifact under `.gfx/`, the editor opens the `.png` it is generated from
instead, because that is the file a person can actually edit.

## What differs between the games

Every structural choice, which is why this is data and not `if game == ...`:

|                | gex2                                   | gex3                                  |
|----------------|----------------------------------------|---------------------------------------|
| block          | 4×4 tiles (32 px)                      | 2×2 tiles (16 px)                     |
| blockset       | **planar** — cell *k* of block *b* at `b + k*0x100` | 8 bytes per block: 4 tile ids, 4 attribute bytes |
| collision      | a quadrant of the same 16 KiB bank     | its own file                          |
| palette        | per-channel table indexed by *tile id* | per-cell attribute bits               |
| block ids      | 8-bit, one flat plane                  | 16-bit, split across two files        |
| map size       | fixed 128×128                          | per map, from `map_geometry`          |

## Object records

Doors, spawns, entities and the rest are described by each disassembly's own
`tools/map_formats.json` — the same schema `render_map_asm.py` uses to generate the
annotated `.asm` those repos assemble. The editor reads it rather than keeping a second
definition of every record, so a field named there is a field the editor understands.

## Colour

Palettes are 15-bit BGR555. Channels expand to 8-bit by bit replication,
`(v << 3) | (v >> 2)`, so 31 reaches a true 255 — not `v << 3`, which caps white at 248
and leaves everything slightly dark. This matches `rgbgfx` and `tools/map2png`.

## Tests

```sh
python3 -m unittest discover tests
```

Format round-trips are self-contained. The rendering tests check the gex2 pipeline
against `gex2gbc/tools/map2png`'s known-good output block for block, and are skipped
when the disassembly repos are not beside this one.

## legacy/

`legacy/gex-map-editor.html` is the original browser-based editor this replaces. It
still runs, and it is where the tile, block and palette pipeline came from.
