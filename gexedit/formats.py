"""Byte-level structures shared by both games.

Pure functions over bytes: no GUI, no file system, no game-specific branching beyond
what the caller asks for. Everything here has an inverse, because the editor has to
write these files back out byte for byte.

The GBC facts that matter:

  * A tile is 8x8 pixels and 4 shades. The .png beside each tileset stores those
    shades as grey levels 0/85/170/255, so shade = 3 - (grey >> 6).
  * A palette is 4 colours, 2 bytes each, 15-bit little-endian with red in the low
    bits. 8 bytes per palette.
  * A block (metatile) is a square of tiles. gex3 uses 2x2 (16 px); gex2 uses 4x4
    (32 px), which is why SPAWN_UNITS_PER_BLOCK is $20 there.

Where the two games genuinely differ - block size, how a block stores its tiles, and
where a tile's palette comes from - the difference is a parameter, not a branch buried
in the renderer.
"""

from dataclasses import dataclass, field


TILE_PX = 8


# --------------------------------------------------------------------- palettes

def expand5(v):
    """5-bit channel to 8-bit, by bit replication: (v << 3) | (v >> 2).

    NOT `v << 3`, which is what tools/map_editor/gex-map-editor.html does. That caps
    white at 248 and leaves every colour a little dark; rgbgfx replicates the high bits
    so 31 reaches a true 255, and matching it is what makes this renderer agree with
    tools/map2png (and with the ROM as an emulator shows it).
    """
    return (v << 3) | (v >> 2)


def parse_palettes(data):
    """8 bytes per palette, 4 colours of 15-bit BGR555 little-endian."""
    out = []
    for i in range(0, len(data) - 7, 8):
        colours = []
        for j in range(0, 8, 2):
            v = data[i + j] | (data[i + j + 1] << 8)
            colours.append((expand5(v & 0x1F),
                            expand5((v >> 5) & 0x1F),
                            expand5((v >> 10) & 0x1F)))
        out.append(colours)
    return out


def serialize_palettes(palettes):
    out = bytearray()
    for pal in palettes:
        for r, g, b in pal:
            v = (r >> 3) & 0x1F | (((g >> 3) & 0x1F) << 5) | (((b >> 3) & 0x1F) << 10)
            out += bytes((v & 0xFF, (v >> 8) & 0xFF))
    return bytes(out)


# ----------------------------------------------------------------------- blocks

@dataclass
class Block:
    """One metatile: a tile id per cell, plus a GBC attribute byte per cell.

    gex2 has no per-cell attributes at all, so its attrs are synthesised as zero and
    the palette is looked up per tile id instead. Keeping one Block type for both games
    is what lets the renderer stay game-agnostic.
    """
    tiles: list = field(default_factory=list)
    attrs: list = field(default_factory=list)

    def palette_of(self, cell, palette_ids=None):
        if palette_ids is not None:                 # gex2: palette follows the tile
            tile = self.tiles[cell]
            return palette_ids[tile] & 0x07 if tile < len(palette_ids) else 0
        return self.attrs[cell] & 0x07              # gex3: palette is in the attribute

    def flips(self, cell):
        a = self.attrs[cell]
        return bool(a & 0x20), bool(a & 0x40)


def parse_blockset_attr(data, cells=4):
    """gex3 layout: per block, `cells` tile low-bytes then `cells` attribute bytes.

    Attribute bit 3 is the tile bank, so it contributes $100 to the tile id; bits 0-2
    are the palette, bit 5 flips X and bit 6 flips Y.
    """
    stride = cells * 2
    blocks = []
    for i in range(0, len(data) - stride + 1, stride):
        attrs = list(data[i + cells:i + stride])
        tiles = [data[i + t] + (0x100 if attrs[t] & 0x08 else 0) for t in range(cells)]
        blocks.append(Block(tiles, attrs))
    return blocks


def serialize_blockset_attr(blocks, cells=4):
    out = bytearray()
    for b in blocks:
        out += bytes(t & 0xFF for t in b.tiles[:cells])
        attrs = []
        for t in range(cells):
            a = b.attrs[t]
            a = (a | 0x08) if b.tiles[t] >= 0x100 else (a & ~0x08)
            attrs.append(a & 0xFF)
        out += bytes(attrs)
    return bytes(out)


def parse_blockset_planar(data, cells=16, count=256, stride=0x100):
    """gex2 layout: PLANAR, not one block after another.

    Cell k of block b lives at b + k * stride, so the 4096 bytes are sixteen 256-byte
    planes - all sixteen blocks' first cells, then all their second cells, and so on.
    The ROM reads it this way because call_00_169f_BlockPatch_WriteTiles walks a block
    one sub-row at a time and steps its pointer by a whole plane between cells, which
    costs an `add` rather than a multiply.

    gex2 blocks carry no attribute bytes at all; a tile's palette comes from the
    per-channel palette_ids table instead.
    """
    blocks = []
    for b in range(count):
        tiles = [data[b + k * stride] if b + k * stride < len(data) else 0
                 for k in range(cells)]
        blocks.append(Block(tiles, [0] * cells))
    return blocks


def serialize_blockset_planar(blocks, cells=16, count=256, stride=0x100):
    out = bytearray(cells * stride)
    for b, blk in enumerate(blocks[:count]):
        for k in range(cells):
            out[b + k * stride] = blk.tiles[k] & 0xFF
    return bytes(out)


# ------------------------------------------------------------------- block maps

def parse_blockmap8(data, width, height):
    """gex2: one byte per cell."""
    return [data[i] if i < len(data) else 0 for i in range(width * height)]


def parse_blockmap16(low, high, width, height):
    """gex3: the id is split across _map.bin and _map_extended.bin."""
    out = []
    for i in range(width * height):
        lo = low[i] if i < len(low) else 0
        hi = high[i] if high and i < len(high) else 0
        out.append(lo + (hi << 8))
    return out


def serialize_blockmap8(cells):
    return bytes(c & 0xFF for c in cells)


def serialize_blockmap16(cells):
    return bytes(c & 0xFF for c in cells), bytes((c >> 8) & 0xFF for c in cells)


# ------------------------------------------------------------------- collision

def parse_collision_blockset(data, cells=4):
    """One byte per cell, no attributes - a collision id per quarter of a block."""
    return [list(data[i:i + cells]) for i in range(0, len(data) - cells + 1, cells)]


def serialize_collision_blockset(blocks, cells=4):
    out = bytearray()
    for b in blocks:
        out += bytes((b + [0] * cells)[:cells])
    return bytes(out)
