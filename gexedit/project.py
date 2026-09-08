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
                    m = re.match(r"^([A-Za-z_][\w]*):", line)
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
    """Turn an INCBIN path into a real file, preferring an editable source.

    A path under .gfx/ is a build artifact rgbgfx makes from a PNG; the editor wants
    the PNG, since that is what a person can actually change.
    """
    if incpath.startswith(".gfx/"):
        png = os.path.join(root, "src", "gfx", incpath[len(".gfx/"):])
        png = os.path.splitext(png)[0] + ".png"
        if os.path.exists(png):
            return png
    direct = os.path.join(root, "src", incpath)
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
            maps.append(MapInfo(ident, mapname, level, w, h, layers))
        return maps

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
            bsc      = pick(r"BANK\((blockset_collision_\w+)\)")
            tileset  = pick(r"\bdw\s+(tileset_\w+)")
            altflags = pick(r"BANK\((alt_blockset_flags\w*)\)")
            altmask  = pick(r"db\s+\$00,\s*(ALT_BLOCKSET_\w+)")
            layers = {}
            for role, label in (("blockmap", blockmap), ("blockset_collision", bsc),
                                ("tileset", tileset), ("alt_blockset_flags", altflags)):
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
            maps.append(MapInfo(ident, mapname, None, geo["width"], geo["height"],
                                layers, {"alt_mask": altmask}))
        return maps
