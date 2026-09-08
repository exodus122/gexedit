"""Open a disassembly repo and work out what maps it contains.

Nothing here hardcodes a filename pattern. Both games already keep a per-map manifest
in their source - gex3's `.data_03_XXXX_MapData_<Map>` records name every layer with a
`farpointer` and carry the map's geometry in a `map_geometry` macro, gex2's
`.data_00_2ebf_MapData` records name theirs with `BANK(label)` - so the editor reads
those records and resolves each label to the file it INCBINs. Rename a file or repoint
a map in the source and the editor follows, which is the same property the generated
.asm has.

Which game a repo IS comes from the profiles in gexedit/profiles/. They live here
rather than in the game repos because they describe the games' data formats, which is
the editor's business; a repo that wants to override one can drop a .gexedit.json at
its root.
"""

import glob
import json
import os
import re

PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles")


def load_profiles():
    """Every game this editor knows how to open, by name."""
    out = {}
    for path in sorted(glob.glob(os.path.join(PROFILE_DIR, "*.json"))):
        with open(path) as f:
            prof = json.load(f)
        out[prof.get("game", os.path.splitext(os.path.basename(path))[0])] = prof
    return out


class ProjectError(Exception):
    pass


# ------------------------------------------------------------- label resolution

def scan_labels(root):
    """label -> path of the file it INCBINs or INCLUDEs, for every global label."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(os.path.join(root, "src")):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if not fn.endswith(".asm"):
                continue
            path = os.path.join(dirpath, fn)
            pending = None
            with open(path, errors="replace") as f:
                for line in f:
                    m = re.match(r"^\.?([A-Za-z_][\w]*):", line)
                    if m:
                        pending = m.group(1)
                        continue
                    m = re.match(r'\s+INC(?:BIN|LUDE)\s+"([^"]+)"', line)
                    if m and pending:
                        out[pending] = m.group(1)
                    if line.strip() and not line.startswith((" ", "\t", ";")):
                        pending = None
    return out


def resolve(root, incpath):
    """Turn an INCBIN/INCLUDE path into the file that is actually the source of truth.

    Two indirections matter:

    * A path under .gfx/ is a build artifact rgbgfx makes from a PNG. The editor wants
      the PNG, because that is the file a person can change.
    * An INCLUDEd .asm with a same-stem .bin beside it is a GENERATED view of that .bin
      (see render_map_asm.py in either disassembly). The .bin holds the bytes; the .asm
      is regenerated from it by `make maps-docs`. So the editor edits the .bin and never
      the .asm - writing the .asm would be overwritten by the next build.
    """
    if incpath.startswith(".gfx/"):
        png = os.path.join(root, "src", "gfx", incpath[len(".gfx/"):])
        png = os.path.splitext(png)[0] + ".png"
        if os.path.exists(png):
            return png
    direct = os.path.join(root, "src", incpath)
    if direct.endswith(".asm"):
        binary = direct[:-4] + ".bin"
        if os.path.exists(binary):
            return binary
    return direct


# --------------------------------------------------------------------- map info

class MapInfo:
    def __init__(self, ident, name, level, width, height, layers, extra=None):
        self.id = ident
        self.name = name
        self.level = level
        self.width = width
        self.height = height
        self.layers = layers          # role -> absolute path (may not exist)
        self.extra = extra or {}

    def __repr__(self):
        return "<MapInfo %02x %s %dx%d>" % (self.id, self.name, self.width, self.height)

    def layer(self, role):
        p = self.layers.get(role)
        return p if p and os.path.exists(p) else None

    def read(self, role):
        p = self.layer(role)
        if not p:
            return None
        with open(p, "rb") as f:
            return f.read()


# ------------------------------------------------------------------ the project

class Project:
    def __init__(self, root, game=None):
        self.root = os.path.abspath(root)
        self.profile = self._pick_profile(game)
        schema = os.path.join(self.root, "tools", "map_formats.json")
        self.record_schema = None
        if os.path.exists(schema):
            with open(schema) as f:
                self.record_schema = json.load(f)
        self.labels = scan_labels(self.root)
        self.maps = self._load_maps()

    @property
    def constants(self):
        """Every DEF NAME EQU $xx in the repo's constants.asm, by name."""
        if getattr(self, "_consts", None) is not None:
            return self._consts
        table = {}
        path = os.path.join(self.root, "src", "constants", "constants.asm")
        if os.path.exists(path):
            with open(path, errors="replace") as f:
                for ln in f:
                    m = re.match(r"^DEF\s+([A-Z0-9_]+)\s+EQU\s+\$([0-9a-fA-F]+)", ln)
                    if m:
                        table.setdefault(m.group(1), int(m.group(2), 16))
        self._consts = table
        return table

    @property
    def enums(self):
        """name -> {value: CONSTANT}, scraped the way render_map_asm.py scrapes them.

        The schema names each enum by the span of DEF lines it occupies in
        constants.asm, because those files reuse a prefix like ENTITY_ for several
        unrelated enums whose values would otherwise collide.
        """
        if getattr(self, "_enums", None) is not None:
            return self._enums
        out = {}
        schema = self.record_schema or {}
        path = os.path.join(self.root, "src", "constants", "constants.asm")
        if os.path.exists(path):
            with open(path, errors="replace") as f:
                lines = f.read().splitlines()
            pat = re.compile(r"^DEF\s+([A-Z0-9_]+)\s+EQU\s+\$([0-9a-fA-F]+)")
            for name, spec in schema.get("enums", {}).items():
                start = end = None
                for i, ln in enumerate(lines):
                    m = pat.match(ln)
                    if not m:
                        continue
                    if m.group(1) == spec["from"]:
                        start = i
                    if m.group(1) == spec["to"]:
                        end = i
                if start is None or end is None or end < start:
                    continue
                table = {}
                for ln in lines[start:end + 1]:
                    m = pat.match(ln)
                    if m:
                        table.setdefault(int(m.group(2), 16), m.group(1))
                out[name] = table
        self._enums = out
        return out

    # -- which game is this? ----------------------------------------------
    def _pick_profile(self, game=None):
        """A repo is identified by the manifest file its profile names.

        gex2's map table is in bank00_map_init_data.asm and gex3's is in
        bank03_map_init_data.asm, so the first profile whose manifest source actually
        exists is the right one. An explicit --game wins, and a .gexedit.json at the
        repo root wins over both - either a whole profile, or just {"game": "gex2"}.
        """
        profiles = load_profiles()
        if not profiles:
            raise ProjectError("no game profiles found in %s" % PROFILE_DIR)

        override = os.path.join(self.root, ".gexedit.json")
        if os.path.exists(override):
            with open(override) as f:
                data = json.load(f)
            if set(data) == {"game"}:
                game = data["game"]
            else:
                return data

        if game:
            if game not in profiles:
                raise ProjectError("unknown game %r - known: %s"
                                   % (game, ", ".join(sorted(profiles))))
            return profiles[game]

        matches = [p for p in profiles.values()
                   if os.path.exists(os.path.join(self.root, p["manifest"]["source"]))]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ProjectError(
                "%s does not look like a gex disassembly - none of %s is present.\n"
                "Pass --game if it is one under a layout I do not recognise."
                % (self.root, ", ".join(sorted(p["manifest"]["source"]
                                               for p in profiles.values()))))
        raise ProjectError("%s matches more than one game (%s) - pass --game"
                           % (self.root, ", ".join(sorted(p["game"] for p in matches))))

    # -- convenience ------------------------------------------------------
    @property
    def game(self):
        return self.profile["game"]

    @property
    def block_tiles(self):
        return self.profile["block_tiles"]

    @property
    def block_px(self):
        return self.profile["block_tiles"] * 8

    def _src(self, rel):
        return os.path.join(self.root, rel)

    # -- manifest ---------------------------------------------------------
    def _load_maps(self):
        kind = self.profile["manifest"]["kind"]
        if kind == "gex3_mapdata":
            return self._load_gex3()
        if kind == "gex2_mapdata":
            return self._load_gex2()
        raise ProjectError("unknown manifest kind %r" % kind)

    def _load_gex3(self):
        man = self.profile["manifest"]
        path = self._src(man["source"])
        if not os.path.exists(path):
            raise ProjectError("missing %s" % man["source"])
        with open(path, errors="replace") as f:
            text = f.read().splitlines()

        # the pointer table gives the map order and the MAP_* name of each entry
        order = []
        for ln in text:
            m = re.match(r"\s+dw\s+(\.data_03_[0-9a-f]+_MapData_\w+)\s*;\s*(MAP_\w+)", ln)
            if m:
                order.append((m.group(1), m.group(2)))

        # then each record names its layers and its geometry
        recs, cur = {}, None
        for ln in text:
            m = re.match(r"^(\.data_03_[0-9a-f]+_MapData_\w+):", ln)
            if m:
                cur = m.group(1)
                recs[cur] = {"ptrs": [], "geometry": None}
                continue
            if cur is None:
                continue
            m = re.match(r"\s+farpointer\s+(\w+)", ln)
            if m:
                recs[cur]["ptrs"].append(m.group(1))
                continue
            m = re.match(r"\s+map_geometry\s+(\d+)\s*,\s*(\d+)\s*,\s*(\w+)", ln)
            if m:
                recs[cur]["geometry"] = (int(m.group(1)), int(m.group(2)), m.group(3))
                continue
            if ln.strip() and not ln.startswith((" ", "\t", ";")):
                cur = None

        roles = man["layers"]
        maps = []
        for ident, (label, mapname) in enumerate(order):
            rec = recs.get(label)
            if rec is None or rec["geometry"] is None:
                continue
            w, h, level = rec["geometry"]
            layers = {}
            for role, ptr in zip(roles, rec["ptrs"]):
                inc = self.labels.get(ptr)
                if inc:
                    layers[role] = resolve(self.root, inc)
            # the tileset PNG sits beside the .bin the ROM actually uses
            if "tileset" in layers:
                png = os.path.splitext(layers["tileset"])[0] + ".png"
                if os.path.exists(png):
                    layers["tileset_png"] = png
            doors = self._table_asset("doors", ident)
            if doors:
                layers["doors"] = doors
            maps.append(MapInfo(ident, mapname, level, w, h, layers))
        return maps

    def _labelled_table(self, spec, index):
        """Entry `index` of one named `dw` pointer table, resolved to a file.

        Used where guessing would go wrong: gex2 has several `dw .palette_*` tables and
        the one that matters is the level BG table in bank0B, not the collectible
        palettes in bank03 - so the profile names the label rather than the editor
        pattern-matching for it.
        """
        if not spec:
            return None
        path = self._src(spec["source"])
        if not os.path.exists(path):
            return None
        with open(path, errors="replace") as f:
            lines = f.read().splitlines()
        labels, seen = [], False
        for ln in lines:
            if not seen:
                if re.match(r"^\.?%s:" % re.escape(spec["label"]), ln):
                    seen = True
                continue
            m = re.match(r"\s+dw\s+\.?([A-Za-z_]\w*)", ln)
            if m:
                labels.append(m.group(1))
                continue
            if ln.strip() and not ln.startswith((" ", "\t", ";")):
                break
        if index >= len(labels):
            return None
        inc = self.labels.get(labels[index])
        return resolve(self.root, inc) if inc else None

    def _table_asset(self, role, index):
        table = self._pointer_tables().get(role)
        if not table or index >= len(table):
            return None
        return table[index]

    def _level_asset(self, level_id, prefix):
        """gex2's per-level lists, found through the pointer tables that index them.

        Every one of those tables is `dw <label>` in level order, so entry N of the
        entity-list table names level N's list - which beats guessing a filename from
        the level's name, and follows a repointing the way everything else here does.
        """
        return self._table_asset(prefix, level_id)

    def _pointer_tables(self):
        """role -> [file per level], scraped from the `dw` tables in src/code.

        A run of `dw <label>` lines is taken to be a per-level pointer table when every
        label in it resolves to a file whose name starts with the same known prefix.
        Going by the resolved FILE rather than the label's spelling is what makes this
        survive gex2 calling a table entry `.data_0b_5030_Doors_MediaDimension` while
        the file beside it is `doors_media_dimension.bin`.
        """
        if getattr(self, "_ptr_cache", None) is not None:
            return self._ptr_cache
        # matched as substrings of the file name, which covers gex2's
        # entity_list_out_of_toon.bin and gex3's GexCave_entity_list.bin alike
        wanted = ("entity_list", "collectible_list", "doors", "spawns")
        found = {}

        def consider(run):
            # a null entry ($0000) means "this map has none", and must keep its slot
            real = [lbl for lbl in run if lbl is not None]
            if len(real) <= 4:
                return
            paths = [self.labels.get(lbl) if lbl else None for lbl in run]
            if not all(paths[i] for i, lbl in enumerate(run) if lbl):
                return
            names = [os.path.basename(q) for q in paths if q]
            for role in wanted:
                if role in found:
                    continue
                if all(role in n for n in names):
                    found[role] = [resolve(self.root, q) if q else None for q in paths]

        for dirpath, _dirs, files in os.walk(os.path.join(self.root, "src", "code")):
            for fn in sorted(files):
                if not fn.endswith(".asm"):
                    continue
                with open(os.path.join(dirpath, fn), errors="replace") as f:
                    run = []
                    for line in f:
                        m = re.match(r"\s+dw\s+\.?([A-Za-z_]\w*)\s*(?:;.*)?$", line)
                        if m:
                            run.append(m.group(1))
                            continue
                        if re.match(r"\s+dw\s+\$0+\s*(?:;.*)?$", line):
                            run.append(None)
                            continue
                        consider(run)
                        run = []
                    consider(run)
        self._ptr_cache = found
        return found

    def _load_gex2(self):
        man = self.profile["manifest"]
        path = self._src(man["source"])
        if not os.path.exists(path):
            raise ProjectError("missing %s" % man["source"])
        with open(path, errors="replace") as f:
            text = f.read()
        geo = self.profile["geometry"]

        # one record per map: a comment naming it, then BANK(...)/dw references
        chunks = re.split(r"\n\s*;\s*\$([0-9a-f]{2})\s+(MAP_\w+)\s*\n", text)
        maps = []
        for i in range(1, len(chunks) - 1, 3):
            ident, mapname, body = int(chunks[i], 16), chunks[i + 1], chunks[i + 2]
            body = body.split("\n\n")[0]
            def pick(pat):
                m = re.search(pat, body)
                return m.group(1) if m else None
            blockmap = pick(r"BANK\((blockmap_\w+)\)")
            bs       = pick(r"BANK\((blockset_\w+)\)")
            tileset  = pick(r"\bdw\s+(tileset_\w+)")
            altflags = pick(r"BANK\((alt_blockset_flags\w*)\)")
            altmask  = pick(r"db\s+\$00,\s*(ALT_BLOCKSET_\w+)")
            # the record names only the first of the bank's four regions; the other
            # three are its siblings, laid out after it by the blockset_bank macro
            roles = [("blockmap", blockmap), ("tileset", tileset),
                     ("alt_blockset_flags", altflags)]
            if bs:
                ch = bs[len("blockset_"):]
                roles += [("blockset", bs), ("alt_blockset", "alt_blockset_" + ch),
                          ("blockset_tile_types", "blockset_tile_types_" + ch),
                          ("alt_blockset_tile_types", "alt_blockset_tile_types_" + ch)]
            layers = {}
            for role, label in roles:
                inc = self.labels.get(label) if label else None
                if inc:
                    layers[role] = resolve(self.root, inc)
            if "tileset" in layers and layers["tileset"].endswith(".png"):
                layers["tileset_png"] = layers["tileset"]
            # gex2 keeps one palette and one palette-id table per channel, named after
            # the tileset rather than the map
            if tileset:
                channel = tileset[len("tileset_"):]
                for role, sub in (("palette", "palettes/palette_%s.bin"),
                                  ("palette_ids", "palette_ids/palette_ids_%s.bin")):
                    p = self._src("src/gfx/tilesets/" + sub % channel)
                    if os.path.exists(p):
                        layers[role] = p
                layers.setdefault("channel", channel)
                # the alt blockset draws some of its tiles from a per-channel set of
                # small secondary tilesets, chosen per block
                sec = self._src("src/data/maps/%s/secondary_tileset_for_block_%s.bin"
                                % (channel, channel))
                if os.path.exists(sec):
                    layers["secondary_tileset_for_block"] = sec
                folder = self._src("src/gfx/secondary_tilesets/%s" % channel)
                if os.path.isdir(folder):
                    layers["secondary_tilesets"] = folder
            # The BG palette is NOT the channel's: .data_0b_5665_LevelBgPalettePointerTable
            # picks one per level, and the bonus Kung Fu Theater level takes a different
            # one from the rest of its channel. Read the table rather than assume.
            pal = self._labelled_table(man.get("palette_table"), ident)
            if pal:
                layers["palette"] = pal

            # gex2's object lists are per level and named after the level, not the
            # channel, and nothing in the MapData record points at them - so find them
            # by the label the code INCLUDEs, which is keyed on the level name
            for role in ("entity_list", "collectible_list", "doors"):
                inc = self._level_asset(ident, role)
                if inc:
                    layers[role] = inc
            maps.append(MapInfo(ident, mapname, None, geo["width"], geo["height"],
                                layers, {"alt_mask": altmask}))
        return maps
