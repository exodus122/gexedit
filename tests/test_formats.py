"""Round-trip and ground-truth tests. Run with: python3 -m unittest discover tests

The format tests are self-contained. The rendering tests need the disassembly repos
beside this one and are skipped when they are not there, so a fresh clone still passes.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gexedit import formats, objects                           # noqa: E402
from gexedit.project import Project, ProjectError              # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEX2 = os.path.join(os.path.dirname(HERE), "gex2gbc")
GEX3 = os.path.join(os.path.dirname(HERE), "gex3gbc")


class TestPalettes(unittest.TestCase):
    def test_expand5_is_bit_replication(self):
        # not v << 3: 31 must reach a true 255, the way rgbgfx expands it
        self.assertEqual(formats.expand5(0), 0)
        self.assertEqual(formats.expand5(31), 255)
        self.assertEqual(formats.expand5(23), 189)

    def test_round_trip(self):
        raw = bytes(range(64))
        self.assertEqual(formats.serialize_palettes(formats.parse_palettes(raw)), raw)


class TestBlocksets(unittest.TestCase):
    def test_attr_round_trip(self):
        raw = bytes((i * 7 + 3) & 0xFF for i in range(8 * 32))
        blocks = formats.parse_blockset_attr(raw, 4)
        self.assertEqual(formats.serialize_blockset_attr(blocks, 4), raw)

    def test_attr_bank_bit_lifts_tile_id(self):
        raw = bytes([5, 0, 0, 0, 0x08, 0, 0, 0])
        self.assertEqual(formats.parse_blockset_attr(raw, 4)[0].tiles[0], 0x105)

    def test_planar_layout(self):
        # cell k of block b lives at b + k * stride, not b * 16 + k
        raw = bytearray(16 * 256)
        raw[3 + 2 * 256] = 0x42                       # block 3, cell 2
        blocks = formats.parse_blockset_planar(bytes(raw), 16, 256, 256)
        self.assertEqual(blocks[3].tiles[2], 0x42)
        self.assertEqual(blocks[3].tiles[0], 0)

    def test_planar_round_trip(self):
        raw = bytes((i * 11) & 0xFF for i in range(16 * 256))
        blocks = formats.parse_blockset_planar(raw, 16, 256, 256)
        self.assertEqual(formats.serialize_blockset_planar(blocks, 16, 256, 256), raw)


class TestBlockmaps(unittest.TestCase):
    def test_split16(self):
        cells = formats.parse_blockmap16(bytes([1, 2]), bytes([0, 1]), 2, 1)
        self.assertEqual(cells, [1, 0x102])
        lo, hi = formats.serialize_blockmap16(cells)
        self.assertEqual((lo, hi), (bytes([1, 2]), bytes([0, 1])))


def _project(path):
    if not os.path.isdir(path):
        raise unittest.SkipTest("%s not found beside this repo" % path)
    return Project(path)


class TestProjects(unittest.TestCase):
    def test_gex2_detected(self):
        p = _project(GEX2)
        self.assertEqual(p.game, "gex2")
        self.assertEqual(len(p.maps), 31)
        self.assertEqual(p.block_tiles, 4)

    def test_gex3_detected(self):
        p = _project(GEX3)
        self.assertEqual(p.game, "gex3")
        self.assertEqual(len(p.maps), 61)
        self.assertEqual(p.block_tiles, 2)

    def test_every_layer_resolves(self):
        for path in (GEX2, GEX3):
            p = _project(path)
            missing = [(m.name, role) for m in p.maps
                       for role, v in m.layers.items()
                       if role != "channel" and not os.path.exists(v)]
            self.assertEqual(missing, [], "%s: unresolved layers" % p.game)


class TestRenderMatchesMap2png(unittest.TestCase):
    """The gex2 renderer is checked against tools/map2png, which is known good."""

    def test_blocks_match(self):
        import numpy as np
        from PIL import Image
        from gexedit.render import MapView

        p = _project(GEX2)
        sheet = os.path.join(GEX2, "tools", "map2png", "blockset_images",
                             "ToonTV_OutOfToon_blockset.png")
        if not os.path.exists(sheet):
            raise unittest.SkipTest("map2png reference images not generated")
        ref = Image.open(sheet).convert("RGB")
        view = MapView(p, next(m for m in p.maps
                               if m.name == "MAP_TOON_TV_OUT_OF_TOON"))
        for b in range(256):
            x, y = (b % 16) * 32, (b // 16) * 32
            theirs = np.asarray(ref.crop((x, y, x + 32, y + 32)), dtype=int)
            mine = np.asarray(view.renderer.block(b), dtype=int)
            self.assertEqual(int(np.abs(theirs - mine).sum()), 0, "block $%02x" % b)


class TestObjectLayers(unittest.TestCase):
    """Objects are parsed straight from each disassembly's own map_formats.json."""

    def _layers(self, path, map_name):
        p = _project(path)
        info = next(m for m in p.maps if m.name == map_name)
        return p, info, objects.layers_for(p, info)

    def test_gex2_layers_present(self):
        _p, _i, layers = self._layers(GEX2, "MAP_MEDIA_DIMENSION")
        self.assertIn("entity_list", layers)
        self.assertIn("doors", layers)

    def test_gex3_layers_present(self):
        _p, _i, layers = self._layers(GEX3, "MAP_GEX_CAVE1")
        self.assertIn("entity_list", layers)
        self.assertIn("doors", layers)

    def test_round_trip_is_byte_exact(self):
        for path, name in ((GEX2, "MAP_MEDIA_DIMENSION"), (GEX3, "MAP_GEX_CAVE1")):
            _p, _i, layers = self._layers(path, name)
            for role, layer in layers.items():
                with open(layer.path, "rb") as f:
                    raw = f.read()
                self.assertEqual(layer.serialize(), raw,
                                 "%s %s did not round-trip" % (name, role))

    def test_moving_an_object_only_changes_its_own_bytes(self):
        _p, info, layers = self._layers(GEX3, "MAP_GEX_CAVE1")
        layer = layers["entity_list"]
        with open(layer.path, "rb") as f:
            before = f.read()
        rec = layer.on_map(info.id)[0]
        layer.set_world_xy(rec, 100, 200)
        after = layer.serialize()
        self.assertEqual(len(before), len(after))
        differing = {i // layer.size for i in range(len(before)) if before[i] != after[i]}
        self.assertEqual(differing, {rec.index})

    def test_gex3_entity_list_is_filtered_by_map(self):
        p = _project(GEX3)
        one = next(m for m in p.maps if m.name == "MAP_GEX_CAVE1")
        two = next(m for m in p.maps if m.name == "MAP_GEX_CAVE2")
        a = objects.layers_for(p, one)["entity_list"]
        b = objects.layers_for(p, two)["entity_list"]
        # same file, different maps: the shared list must be split by map_id
        self.assertEqual(a.path, b.path)
        self.assertNotEqual([r.index for r in a.on_map(one.id)],
                            [r.index for r in b.on_map(two.id)])

    def test_entity_names_resolve(self):
        p = _project(GEX3)
        info = next(m for m in p.maps if m.name == "MAP_GEX_CAVE1")
        layer = objects.layers_for(p, info)["entity_list"]
        labels = [layer.label(r, p.enums) for r in layer.on_map(info.id)]
        self.assertTrue(all(l.startswith("ENTITY_") for l in labels), labels)


class TestGex2AltBlockset(unittest.TestCase):
    """gex2's alt blockset substitutes tiles from small per-channel secondary tilesets.

    Checked against tools/map2png, which is known good for the primary blockset. The one
    place they disagree is documented in test_media_dimension_is_the_only_difference.
    """

    REGION = (0, 0, 48, 32)

    def _refs(self):
        import re
        src_path = os.path.join(GEX2, "tools", "map2png", "map2png.py")
        if not os.path.exists(src_path):
            raise unittest.SkipTest("map2png not present")
        with open(src_path) as f:
            src = f.read()
        names = re.findall(r'"([^"]+)"',
                           re.search(r"level_names = \[(.*?)\]", src, re.S).group(1))
        return names, os.path.join(GEX2, "tools", "map2png", "map_images")

    def _diff_cells(self, project, info, ref_png):
        import numpy as np
        from PIL import Image
        from gexedit.render import MapView
        Image.MAX_IMAGE_PIXELS = None
        mine = MapView(project, info).render(region=self.REGION).convert("RGB")
        ref = Image.open(ref_png).convert("RGB").crop((0, 0, mine.width, mine.height))
        d = (np.abs(np.asarray(ref, dtype=np.int16)
                    - np.asarray(mine, dtype=np.int16)).sum(axis=2) != 0)
        return {(cx, cy)
                for cx in range(self.REGION[2]) for cy in range(self.REGION[3])
                if d[cy * 32:(cy + 1) * 32, cx * 32:(cx + 1) * 32].any()}

    def test_secondary_tilesets_load(self):
        p = _project(GEX2)
        from gexedit.render import MapView
        info = next(m for m in p.maps if m.name == "MAP_TOON_TV_OUT_OF_TOON")
        sec = MapView(p, info).alt_renderer.secondary
        self.assertTrue(sec.sets, "no secondary tilesets loaded")
        self.assertLess(sec.start, 0x100)

    def test_palette_comes_from_the_level_table(self):
        """Not from the channel: the bonus Kung Fu level uses a different palette."""
        p = _project(GEX2)
        info = next(m for m in p.maps
                    if m.name == "MAP_KUNG_FU_THEATER_LIZARD_IN_A_CHINA_SHOP")
        self.assertTrue(info.layer("palette").endswith("palette_kung_fu_theater2.bin"),
                        info.layer("palette"))

    def test_matches_map2png_everywhere_but_the_tv_screens(self):
        p = _project(GEX2)
        names, ref_dir = self._refs()
        if not os.path.isdir(ref_dir):
            raise unittest.SkipTest("map2png reference images not generated")
        checked = 0
        for info in p.maps:
            ref = os.path.join(ref_dir, names[info.id] + "_map.png")
            if not os.path.exists(ref):
                continue
            checked += 1
            bad = self._diff_cells(p, info, ref)
            if info.name == "MAP_MEDIA_DIMENSION":
                # the only disagreement: map2png gives every television screen the
                # first of its channel's two television palettes, so tiles whose
                # palette id is 7 come out in palette 6's colours there
                self.assertLessEqual(len(bad), 8, "Media Dimension drifted")
                continue
            self.assertEqual(bad, set(), "%s differs from map2png" % info.name)
        self.assertGreater(checked, 15)


class TestDoorPairs(unittest.TestCase):
    """gex2's two-way doors are a pair of records that reverse each other.

    Moving one end has to bring the other's destination with it, or the trip back
    silently breaks - and the editor draws a line to nowhere.
    """

    def _doors(self):
        p = _project(GEX2)
        info = next(m for m in p.maps if m.name == "MAP_TOON_TV_OUT_OF_TOON")
        layers = objects.layers_for(p, info)
        if "doors" not in layers:
            raise unittest.SkipTest("no door layer")
        return layers["doors"]

    def test_pairing_comes_from_the_schema(self):
        pair = self._doors().pairing
        self.assertEqual(pair["kind"], "reverse_pairs")
        self.assertEqual(pair["from"], ["from_x", "from_y"])
        self.assertEqual(pair["to"], ["to_x", "to_y"])

    def test_partner_follows_a_move(self):
        layer = self._doors()
        rec = layer.records[0]
        partners = layer.partners(rec)
        self.assertTrue(partners, "expected door 0 to have a reverse partner")
        moved = layer.set_world_xy(rec, 40 * 32, 20 * 32)
        self.assertEqual([q.index for q in moved], [q.index for q in partners])
        here = (rec["from_x"], rec["from_y"])
        for q in partners:
            self.assertEqual((q["to_x"], q["to_y"]), here)

    def test_partner_can_be_left_alone(self):
        layer = self._doors()
        rec, partner = layer.records[0], layer.records[3]
        before = (partner["to_x"], partner["to_y"])
        layer.set_world_xy(rec, 10 * 32, 10 * 32, sync_partners=False)
        self.assertEqual((partner["to_x"], partner["to_y"]), before)

    def test_a_one_way_door_has_no_partner(self):
        layer = self._doors()
        oneway = [r for r in layer.records if not layer.partners(r)]
        self.assertTrue(oneway, "expected at least one one-way door")
        for r in oneway:
            self.assertEqual(layer.set_world_xy(r, 32, 32), [])

    def test_repeated_moves_keep_tracking(self):
        """A drag is many small moves; the partner must follow every one."""
        layer = self._doors()
        rec = layer.records[0]
        for step in range(5):
            layer.set_world_xy(rec, (10 + step) * 32, (10 + step) * 32)
        here = (rec["from_x"], rec["from_y"])
        self.assertEqual([(q["to_x"], q["to_y"]) for q in layer.records
                          if q.index == 3], [here])

    def test_entities_are_unaffected(self):
        """Only records the schema declares reversible get this treatment."""
        p = _project(GEX2)
        info = next(m for m in p.maps if m.name == "MAP_TOON_TV_OUT_OF_TOON")
        ents = objects.layers_for(p, info)["entity_list"]
        self.assertIsNone(ents.pairing)
        self.assertEqual(ents.set_world_xy(ents.records[0], 64, 64), [])


class TestLayerResolution(unittest.TestCase):
    def test_generated_asm_resolves_to_its_bin(self):
        """gex3 INCLUDEs generated .asm for its entity lists; the bytes are the .bin."""
        p = _project(GEX3)
        path = p.maps[0].layer("entity_list")
        self.assertTrue(path.endswith(".bin"), path)


if __name__ == "__main__":
    unittest.main()
