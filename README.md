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
| `1`…`5`           | View · Paint · Fill · Rect · Objects |
| wheel             | zoom, toward whatever is under the pointer |
| middle drag       | move the view, from any tool |
| left click / drag | in **View** (the default): click selects an entity or door, drag moves the view. Otherwise paint, flood fill, drag a rectangle, or move an object |
| right click       | pick the block under the cursor |
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

### Objects

Entities, doors and spawn points are drawn on the map. Clicking one selects it in the
View tool too, so you can inspect things without leaving the tool you navigate with; the
**Objects** tool is what lets you drag them. Either way, every field is editable in the
panel on the right. Each layer can
be hidden from the toolbar, and gex2 doors draw a dashed line to where they lead.

Nothing about those records is written into the editor. The panel is generated from the
disassembly's own `tools/map_formats.json` — the file `render_map_asm.py` uses to
generate the annotated `.asm` the ROM is built from — so every field it documents gets a
row, in record order, and a field with an enum gets a dropdown of the actual constants
from `constants.asm`. Document a byte there and it appears here.

Positions are world coordinates rather than cells, so an object sits exactly where it
sits. gex2 stores entity positions in pixels and door positions in blocks; gex3 stores
everything in pixels and keeps one entity list per *level*, with the map id inside each
record — the editor shows only the records belonging to the map you are editing. All of
that is declared in the schema's `editor` block, which is the one editor-specific thing
added to a disassembly and is inert to its build.

Saving rewrites only the records that changed; the rest of the file is byte-identical.

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

## gex2's alt blockset

Every gex2 map cell carries a flag saying whether it takes the primary blockset or the
alternate one, and an alt block does not simply use the map's tileset: tile ids below
$24 come from one of a handful of 36-tile *secondary* tilesets, chosen per block by
`secondary_tileset_for_block_<channel>.bin`. Byte 0 of that file is the first block id
it covers and the bytes after it are one selector per block; 0 means no substitution,
otherwise selector - 1 picks a tileset **by position in sorted filename order** - the
numbers in those filenames are not the selector.

Each secondary tileset brings its own palette-id table. The Media Dimension screens go
further: a screen named `..._<channel>_screen` has a 16-byte
`<channel>_television_palette.bin` beside it holding two palettes, which stand in for
the last two of the map's own eight. That is exactly what those tilesets' palette ids
say - they use only 6 and 7.

The map's palette is per LEVEL, read from `.data_0b_5665_LevelBgPalettePointerTable`,
not per channel: the bonus Kung Fu Theater level uses a different one from the rest of
its channel.

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
