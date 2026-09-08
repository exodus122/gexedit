"""Turn a map's layers into pixels.

A faithful port of the pipeline in tools/map_editor/gex-map-editor.html, generalised so
the same code renders gex2's 4x4-tile blocks with per-tile-id palettes and gex3's
2x2-tile blocks with per-cell attribute bytes.

No GUI imports: this module can render to a PNG from a script, which is how it gets
tested without a screen.
"""

import os
import re

import numpy as np
from PIL import Image

from . import formats


class Tileset:
    """The 8x8 tiles of one tileset, ready to be coloured by any palette.

    The .png stores four shades as grey 0/85/170/255. The ROM's shade 0 is the lightest,
    hence `3 - (grey >> 6)`.
    """

    def __init__(self, png_path):
        img = Image.open(png_path).convert("L")
        a = np.asarray(img, dtype=np.uint8)
        h, w = a.shape
        self.per_row = w // 8
        self.count = self.per_row * (h // 8)
        shades = 3 - np.minimum(3, a >> 6)
        # (count, 8, 8)
        self.shades = np.stack([
            shades[(i // self.per_row) * 8:(i // self.per_row) * 8 + 8,
                   (i % self.per_row) * 8:(i % self.per_row) * 8 + 8]
            for i in range(self.count)])

    def tile_rgb(self, index, palette):
        """One 8x8x3 RGB array. Out-of-range tiles render as transparent black rather
        than raising, because a half-edited blockset should still draw."""
        if index >= self.count:
            return np.zeros((8, 8, 3), dtype=np.uint8)
        lut = np.array(palette, dtype=np.uint8)          # (4, 3)
        return lut[self.shades[index]]


class SecondaryTilesets:
    """gex2's per-channel secondary tilesets, and which block uses which.

    A block in the ALT blockset can draw its low tile ids from one of these 36-tile sets
    instead of the map's own tileset. secondary_tileset_for_block_<channel>.bin says
    which: byte 0 is the first block id the table covers, and the bytes after it are one
    selector per block from there to $FF. A selector of 0 means "no substitution", and
    otherwise selector - 1 indexes the tilesets. Only tile ids below TILE_LIMIT are
    substituted; anything above still comes from the map's tileset.

    Each secondary tileset carries its own palette-id table, so a substituted tile also
    takes its colours from there rather than from the channel's palette_ids.
    """

    TILE_LIMIT = 0x24            # a secondary tileset is 6x6 tiles

    def __init__(self, table, folder, palette_count=8):
        self.palette_count = palette_count
        self.start = table[0] if table else 0x100
        self.selectors = table[1:] if table else b""
        self.sets = {}
        if not folder or not os.path.isdir(folder):
            return
        pal_dir = os.path.join(folder, "palette_ids")
        # The selector indexes these sets by POSITION in sorted filename order, not by
        # the number in the file name - circuit_central's run from image_00d_11 to _17
        # while its selectors are 1..6. A .png with no palette-id table beside it is
        # unusable and is skipped rather than occupying a slot, which is what keeps the
        # positions lined up.
        index = 0
        for name in sorted(os.listdir(folder)):
            if not name.lower().endswith(".png"):
                continue
            stem = os.path.splitext(name)[0]
            pal = os.path.join(pal_dir, stem + "_palette_ids.bin")
            if not os.path.exists(pal):
                continue
            with open(pal, "rb") as f:
                ids = f.read()
            self.sets[index] = (Tileset(os.path.join(folder, name)), ids,
                                self._television(folder, stem))
            index += 1

    @staticmethod
    def _television(folder, stem):
        """The Media Dimension TVs show other channels, in other channels' colours.

        A screen tileset is named ..._<channel>_screen, and beside it is a 16-byte
        <channel>_television_palette.bin - two palettes, which stand in for the LAST two
        of the map's own eight while those tiles are drawn. That is exactly what the
        screen tilesets' palette ids say: they use only 6 and 7, and each screen uses
        whichever of the two its channel needs.
        """
        if not stem.endswith("_screen"):
            return None
        # image_013_14_prehistory_channel_screen -> prehistory_channel
        m = re.match(r"^image_[0-9a-fA-F]+_\d+_(.+)$", stem[:-len("_screen")])
        if not m:
            return None
        channel = m.group(1)
        path = os.path.join(folder, "palettes", channel + "_television_palette.bin")
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return formats.parse_palettes(f.read())

    def palette_for(self, sub, pal_id, default):
        """The palette a substituted tile takes: a television one when it has them."""
        tv = sub[2]
        if not tv:
            return default
        offset = max(0, self.palette_count - len(tv))
        return tv[min(len(tv) - 1, max(0, pal_id - offset))]

    def for_block(self, block_id):
        """(tileset, palette_ids, television_palettes) for this block, or None."""
        i = block_id - self.start
        if i < 0 or i >= len(self.selectors):
            return None
        sel = self.selectors[i]
        return self.sets.get(sel - 1) if sel else None


class BlockRenderer:
    """Caches one RGB image per block id, since a map reuses a few hundred blocks
    thousands of times. This is the cache the HTML editor calls blockImageCache."""

    def __init__(self, tileset, blocks, palettes, cells_per_side, palette_ids=None,
                 secondary=None):
        self.tileset = tileset
        self.secondary = secondary
        self.blocks = blocks
        self.palettes = palettes or [[(0, 0, 0)] * 4]
        self.n = cells_per_side
        self.px = cells_per_side * 8
        self.palette_ids = palette_ids
        self._cache = {}

    def block(self, block_id):
        img = self._cache.get(block_id)
        if img is not None:
            return img
        out = np.zeros((self.px, self.px, 3), dtype=np.uint8)
        if 0 <= block_id < len(self.blocks):
            blk = self.blocks[block_id]
            sub = self.secondary.for_block(block_id) if self.secondary else None
            for cell in range(self.n * self.n):
                if cell >= len(blk.tiles):
                    break
                tile_id = blk.tiles[cell]
                tileset, ids, substituted = self.tileset, self.palette_ids, False
                if sub and tile_id < SecondaryTilesets.TILE_LIMIT:
                    tileset, ids, substituted = sub[0], sub[1], True
                pal_id = blk.palette_of(cell, ids)
                pal = self.palettes[pal_id % len(self.palettes)]
                if substituted:
                    pal = self.secondary.palette_for(sub, pal_id, pal)
                tile = tileset.tile_rgb(tile_id, pal)
                fx, fy = blk.flips(cell)
                if fx:
                    tile = tile[:, ::-1]
                if fy:
                    tile = tile[::-1, :]
                y = (cell // self.n) * 8
                x = (cell % self.n) * 8
                out[y:y + 8, x:x + 8] = tile
        self._cache[block_id] = out
        return out

    def invalidate(self, block_id=None):
        if block_id is None:
            self._cache.clear()
        else:
            self._cache.pop(block_id, None)


class MapView:
    """Everything needed to draw one map, loaded from a Project's MapInfo."""

    def __init__(self, project, info):
        self.project = project
        self.info = info
        p = project.profile
        self.cells = project.block_tiles
        self.block_px = project.block_px
        self.width = info.width
        self.height = info.height

        png = info.layer("tileset_png")
        if not png:
            raise ValueError("%s has no tileset PNG to render from" % info.name)
        self.tileset = Tileset(png)
        self.palettes = formats.parse_palettes(info.read("palette") or b"")

        palette_ids = None
        if p["palette_source"] == "per_channel_tile_ids":
            palette_ids = info.read("palette_ids")

        if p["blockset"] == "attr":
            self.blocks = formats.parse_blockset_attr(
                info.read("blockset") or b"", self.cells * self.cells)
            self.alt_blocks = None
            self.alt_plane = None
        else:
            bank = info.read("blockset_collision") or b""
            spec = p["blockset_bank"]
            size = spec["blocks"] * spec["block_bytes"]
            cut = lambda off: bank[off:off + size]
            self.blocks = formats.parse_blockset_planar(
                cut(spec["blockset"]), spec["block_bytes"], spec["blocks"],
                spec["plane_stride"])
            self.alt_blocks = formats.parse_blockset_planar(
                cut(spec["alt_blockset"]), spec["block_bytes"], spec["blocks"],
                spec["plane_stride"])
            self.alt_plane = info.read("alt_blockset_flags")

        if p["blockmap"] == "split16":
            self.cells_map = formats.parse_blockmap16(
                info.read("blockmap") or b"", info.read("blockmap_hi"),
                self.width, self.height)
        else:
            self.cells_map = formats.parse_blockmap8(
                info.read("blockmap") or b"", self.width, self.height)

        self.renderer = BlockRenderer(self.tileset, self.blocks, self.palettes,
                                      self.cells, palette_ids)
        self.alt_renderer = None
        if self.alt_blocks is not None:
            table = info.read("secondary_tileset_for_block") or b""
            secondary = SecondaryTilesets(table, info.layers.get("secondary_tilesets"),
                                          max(1, len(self.palettes)))
            self.alt_renderer = BlockRenderer(self.tileset, self.alt_blocks,
                                              self.palettes, self.cells, palette_ids,
                                              secondary=secondary)

        # collision: gex3 keeps a parallel grid plus its own tiny blockset, gex2 keeps
        # a quadrant of the same bank indexed by the very same block ids
        self.coll_cells = None
        self.coll_blocks = None
        if p["collision"] == "separate":
            raw = info.read("collision")
            if raw:
                self.coll_cells = list(raw)
            cb = info.read("collision_blockset")
            if cb:
                self.coll_blocks = formats.parse_collision_blockset(
                    cb, self.cells * self.cells)
        else:
            spec = p["blockset_bank"]
            bank = info.read("blockset_collision") or b""
            size = spec["blocks"] * spec["block_bytes"]
            self.coll_cells = self.cells_map
            self.coll_blocks = [b.tiles for b in formats.parse_blockset_planar(
                bank[spec["collision"]:spec["collision"] + size],
                spec["block_bytes"], spec["blocks"], spec["plane_stride"])]

        # which map cells take the alternate blockset
        self.alt_mask = 0
        name = (info.extra or {}).get("alt_mask")
        if name:
            self.alt_mask = project_constant(project.root, name) or 0

    # ------------------------------------------------------------------
    def block_at(self, cx, cy):
        return self.cells_map[cy * self.width + cx]

    def set_block(self, cx, cy, block_id):
        self.cells_map[cy * self.width + cx] = block_id

    def uses_alt(self, cx, cy):
        if not (self.alt_plane and self.alt_mask and self.alt_renderer):
            return False
        i = cy * self.width + cx
        return i < len(self.alt_plane) and bool(self.alt_plane[i] & self.alt_mask)

    def collision_at(self, cx, cy):
        """The collision ids under one map cell, one per sub-cell, or None."""
        if self.coll_cells is None or self.coll_blocks is None:
            return None
        i = cy * self.width + cx
        if i >= len(self.coll_cells):
            return None
        cid = self.coll_cells[i]
        if cid >= len(self.coll_blocks):
            return None
        return self.coll_blocks[cid]

    def render_collision(self, region=None, alpha=110):
        """A translucent overlay the caller composites over render()."""
        cx0, cy0, cw, ch = region or (0, 0, self.width, self.height)
        cw = min(cw, self.width - cx0)
        ch = min(ch, self.height - cy0)
        bp = self.block_px
        sub = bp // self.cells
        out = np.zeros((ch * bp, cw * bp, 4), dtype=np.uint8)
        for y in range(ch):
            for x in range(cw):
                ids = self.collision_at(cx0 + x, cy0 + y)
                if not ids:
                    continue
                for k, value in enumerate(ids[:self.cells * self.cells]):
                    colour = collision_colour(value)
                    if colour is None:
                        continue
                    py = y * bp + (k // self.cells) * sub
                    px = x * bp + (k % self.cells) * sub
                    out[py:py + sub, px:px + sub, 0:3] = colour
                    out[py:py + sub, px:px + sub, 3] = alpha
        return Image.fromarray(out, "RGBA")

    def render(self, region=None, scale=1, grid=False):
        """Render `region` = (cx, cy, cw, ch) in cells, or the whole map."""
        cx0, cy0, cw, ch = region or (0, 0, self.width, self.height)
        cw = min(cw, self.width - cx0)
        ch = min(ch, self.height - cy0)
        bp = self.block_px
        out = np.zeros((ch * bp, cw * bp, 3), dtype=np.uint8)
        for y in range(ch):
            for x in range(cw):
                gx, gy = cx0 + x, cy0 + y
                r = self.alt_renderer if self.uses_alt(gx, gy) else self.renderer
                out[y * bp:(y + 1) * bp, x * bp:(x + 1) * bp] = r.block(self.block_at(gx, gy))
        img = Image.fromarray(out, "RGB")
        if scale != 1:
            img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
        if grid:
            img = _draw_grid(img, bp * scale)
        return img


COLLISION_EMPTY = 0


def collision_colour(value):
    """A stable colour per collision id.

    There is no collision tileset to draw from in either repo, and a colour per id is
    more useful than tiles anyway: it makes "these two blocks collide differently"
    visible at a glance. Id 0 is empty and draws as nothing.
    """
    if value == COLLISION_EMPTY:
        return None
    h = (value * 2654435761) & 0xFFFFFFFF
    return ((h >> 16) & 0x7F | 0x80, (h >> 8) & 0x7F | 0x80, h & 0x7F | 0x80)


def _draw_grid(img, step):
    from PIL import ImageDraw
    d = ImageDraw.Draw(img, "RGBA")
    for x in range(0, img.width, step):
        d.line([(x, 0), (x, img.height)], fill=(255, 255, 255, 60))
    for y in range(0, img.height, step):
        d.line([(0, y), (img.width, y)], fill=(255, 255, 255, 60))
    return img


_const_cache = {}


def project_constant(root, name):
    """Look one DEF up in a repo's constants.asm - used for the alt-blockset mask."""
    import os, re
    table = _const_cache.get(root)
    if table is None:
        table = {}
        path = os.path.join(root, "src", "constants", "constants.asm")
        if os.path.exists(path):
            with open(path, errors="replace") as f:
                for ln in f:
                    m = re.match(r"^DEF\s+([A-Z0-9_]+)\s+EQU\s+\$([0-9a-fA-F]+)", ln)
                    if m:
                        table.setdefault(m.group(1), int(m.group(2), 16))
        _const_cache[root] = table
    return table.get(name)
