"""Object layers - entities, doors, spawns - read straight from each game's schema.

The disassemblies already describe these records in tools/map_formats.json: it is what
render_map_asm.py uses to generate the annotated .asm they assemble. The editor reads
the same file rather than keeping a second definition, so a field named there is a field
the editor can show, and nothing here knows what an entity record looks like.

Each asset gains one editor-only block in that schema saying which fields are the
position and what units they are in. That is the only editor-specific thing added to a
disassembly, and it is inert to the build.
"""


class Record(dict):
    """One object. Field values by name, plus the slot it occupies in the file."""

    def __init__(self, index, values):
        super().__init__(values)
        self.index = index


class ObjectLayer:
    """One list of records: parsed, editable, and writable back byte for byte."""

    def __init__(self, role, asset, path, data, block_px):
        self.role = role
        self.asset = asset
        self.path = path
        self.block_px = block_px
        self.editor = asset.get("editor") or {}
        self.size = asset["record_size"]
        self.fields = [f for line in asset["lines"] for f in line["fields"]]
        self.records = []
        self.trailing = b""
        self.dirty = False
        self._parse(data)

    # ---------------------------------------------------------------- parsing
    def _parse(self, data):
        term = self.asset.get("terminator")
        end = len(data)
        if term is not None:
            for i in range(0, len(data) - self.size + 1, self.size):
                if data[i] == term["value"]:
                    end = i
                    break
            else:
                if len(data) % self.size and data[-1] == term["value"]:
                    end = len(data) - 1
        end -= end % self.size
        self.trailing = data[end:]
        for n, base in enumerate(range(0, end, self.size)):
            self.records.append(Record(n, {
                f["name"]: self._read(data, base, f) for f in self.fields}))

    @staticmethod
    def _read(data, base, field):
        at = base + field["at"]
        if field["type"] == "u16":
            return data[at] | (data[at + 1] << 8)
        return data[at]

    def serialize(self):
        out = bytearray(len(self.records) * self.size)
        for n, rec in enumerate(self.records):
            base = n * self.size
            for f in self.fields:
                v = int(rec.get(f["name"], 0))
                at = base + f["at"]
                if f["type"] == "u16":
                    out[at] = v & 0xFF
                    out[at + 1] = (v >> 8) & 0xFF
                else:
                    out[at] = v & 0xFF
        return bytes(out) + self.trailing

    def save(self):
        with open(self.path, "wb") as f:
            f.write(self.serialize())
        self.dirty = False
        return self.path

    # --------------------------------------------------------------- geometry
    @property
    def placeable(self):
        return bool(self.editor.get("x") and self.editor.get("y"))

    def _scale(self):
        return self.block_px if self.editor.get("units") == "blocks" else 1

    def world_xy(self, rec):
        s = self._scale()
        return rec[self.editor["x"]] * s, rec[self.editor["y"]] * s

    def set_world_xy(self, rec, px, py):
        s = self._scale()
        rec[self.editor["x"]] = max(0, int(px) // s)
        rec[self.editor["y"]] = max(0, int(py) // s)
        self.dirty = True

    def link_xy(self, rec):
        """Some records point somewhere else - gex2 doors carry their destination."""
        lx, ly = self.editor.get("link_x"), self.editor.get("link_y")
        if not (lx and ly) or lx not in rec:
            return None
        s = self._scale()
        return rec[lx] * s, rec[ly] * s

    def on_map(self, map_id):
        """Records belonging to one map.

        gex3 keeps one entity list per level with the map id inside each record, so a
        list has to be filtered; gex2's lists are already per level, which is per map.
        """
        field = self.editor.get("map_field")
        if not field:
            return list(self.records)
        return [r for r in self.records if r.get(field) == map_id]

    # ------------------------------------------------------------------ labels
    def label(self, rec, enums=None):
        name = self.editor.get("label")
        if not name or name not in rec:
            return "#%d" % rec.index
        value = rec[name]
        field = next((f for f in self.fields if f["name"] == name), None)
        if field and enums and field.get("enum") in enums:
            pretty = enums[field["enum"]].get(value)
            if pretty:
                return pretty
        return "%s $%02x" % (name, value)


def layers_for(project, info):
    """Every placeable object layer this map has, keyed by role."""
    schema = project.record_schema
    if not schema:
        return {}
    out = {}
    for role in ("entity_list", "doors", "spawns", "collectible_list"):
        path = info.layer(role)
        if not path:
            continue
        asset = _asset_for(schema, path)
        if asset is None or not (asset.get("editor") or {}).get("x"):
            continue
        with open(path, "rb") as f:
            data = f.read()
        try:
            out[role] = ObjectLayer(role, asset, path, data, project.block_px)
        except (IndexError, KeyError):
            continue
    return out


def _asset_for(schema, path):
    """Match a file to the schema asset whose glob describes it."""
    import fnmatch
    import os
    name = os.path.basename(path)
    for asset in schema.get("assets", {}).values():
        globs = asset["glob"]
        for g in ([globs] if isinstance(globs, str) else globs):
            if fnmatch.fnmatch(name, os.path.basename(g)):
                return asset
    return None
