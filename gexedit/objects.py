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

    def __init__(self, role, asset, path, data, block_px, constants=None):
        self.role = role
        self.asset = asset
        self.path = path
        self.block_px = block_px
        self.constants = constants or {}
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
        """World units per stored unit.

        Records are stored in whatever grid the game indexes them by: pixels for gex2
        entities, blocks for gex2 doors, and 16-pixel cells for collectibles in both -
        which is a block in gex3 but half of one in gex2, so it has to be its own number
        rather than derived from the block size.
        """
        if self.editor.get("scale"):
            return int(self.editor["scale"])
        return self.block_px if self.editor.get("units") == "blocks" else 1

    def _offsets(self):
        """Where inside its cell a record actually sits, in world units.

        A gex2 door is stored as a block, but the game does not put the player at that
        block's corner: call_0b_4efe_Map_SetSpawnPosition lands them at
        block * SPAWN_UNITS_PER_BLOCK + SPAWN_DOOR_X_OFFSET across and
        + SPAWN_DOOR_Y_OFFSET down - the right edge, halfway down. Drawing the marker at
        the corner puts it a whole block from where the door really is.

        The offsets are named in the schema and resolved from the game's own constants,
        so retuning one in the disassembly moves the markers with it.
        """
        return (self.constants.get(self.editor.get("x_offset"), 0),
                self.constants.get(self.editor.get("y_offset"), 0))

    def anchor_note(self):
        ox, oy = self._offsets()
        return "+%d,+%d into the block" % (ox, oy) if (ox or oy) else ""

    def world_xy(self, rec):
        s = self._scale()
        ox, oy = self._offsets()
        return rec[self.editor["x"]] * s + ox, rec[self.editor["y"]] * s + oy

    def set_world_xy(self, rec, px, py, sync_partners=True):
        """Move one record, and by default bring its reverse partners with it.

        gex2 doors are one-directional, and a two-way door is a pair of records that
        are each other's exact reverse. Moving one end without the other does not just
        leave a stale line on screen - it silently breaks the trip back, which is the
        kind of edit you would not notice until you played it. Records that pointed at
        this one's old position are repointed at its new one.

        Returns the partner records it changed, so the caller can say so.
        """
        s = self._scale()
        ox, oy = self._offsets()
        moved = []
        if sync_partners:
            moved = self.partners(rec)
        rec[self.editor["x"]] = max(0, (int(px) - ox) // s)
        rec[self.editor["y"]] = max(0, (int(py) - oy) // s)
        here = (rec[self.editor["x"]], rec[self.editor["y"]])
        for q in moved:
            q[self.pairing["to"][0]], q[self.pairing["to"][1]] = here
        self.dirty = True
        return moved

    @property
    def pairing(self):
        """The from/to field pair, when the schema declares these records reversible.

        Reuses the `annotate` block the disassembly already carries for generating the
        "<-> #n" / "one-way" notes, rather than teaching the editor about doors.
        """
        ann = self.asset.get("annotate") or {}
        return ann if ann.get("kind") == "reverse_pairs" else None

    def partners(self, rec):
        """Records whose destination is this record's current source."""
        pair = self.pairing
        if not pair:
            return []
        here = tuple(rec[f] for f in pair["from"])
        return [q for q in self.records
                if q is not rec and tuple(q[f] for f in pair["to"]) == here]

    def link_xy(self, rec):
        """Some records point somewhere else - gex2 doors carry their destination."""
        lx, ly = self.editor.get("link_x"), self.editor.get("link_y")
        if not (lx and ly) or lx not in rec:
            return None
        s = self._scale()
        ox, oy = self._offsets()
        return rec[lx] * s + ox, rec[ly] * s + oy

    def on_map(self, map_id):
        """Records belonging to one map.

        gex3 keeps one entity list per level with the map id inside each record, so a
        list has to be filtered; gex2's lists are already per level, which is per map.
        """
        field = self.editor.get("map_field")
        if not field:
            return list(self.records)
        return [r for r in self.records if r.get(field) == map_id]

    # ----------------------------------------------------------------- editing
    def new_record(self, px, py, map_id=None, template=None):
        """Append a record at a world position and return it.

        APPENDED, never inserted: gex2's wD000_EntityFlags is indexed by an entry's
        position in its list, so putting a record in the middle renumbers the saved
        state of everything after it. Appending leaves existing indices alone.

        A template - normally the selected record - supplies the non-positional fields,
        so adding an object next to one like it is a click rather than a form to fill in.
        """
        values = {f["name"]: 0 for f in self.fields}
        if template is not None:
            values.update({k: v for k, v in template.items()})
        rec = Record(len(self.records), values)
        self.records.append(rec)
        field = self.editor.get("map_field")
        if field and map_id is not None:
            rec[field] = map_id
        self.set_world_xy(rec, px, py, sync_partners=False)
        self.dirty = True
        return rec

    def delete(self, rec):
        """Remove a record and renumber the rest. Returns the indices that shifted."""
        if rec not in self.records:
            return []
        self.records.remove(rec)
        shifted = []
        for i, q in enumerate(self.records):
            if q.index != i:
                shifted.append((q.index, i))
                q.index = i
        self.dirty = True
        return shifted

    def is_last(self, rec):
        return bool(self.records) and self.records[-1] is rec

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
            out[role] = ObjectLayer(role, asset, path, data, project.block_px,
                                    project.constants)
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
