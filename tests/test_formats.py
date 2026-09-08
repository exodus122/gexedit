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


class TestLayerResolution(unittest.TestCase):
    def test_generated_asm_resolves_to_its_bin(self):
        """gex3 INCLUDEs generated .asm for its entity lists; the bytes are the .bin."""
        p = _project(GEX3)
        path = p.maps[0].layer("entity_list")
        self.assertTrue(path.endswith(".bin"), path)


if __name__ == "__main__":
    unittest.main()
