#!/usr/bin/env python3
"""Cross-platform offline + live-ISO tests for v2.7 3D World Lighting & Geometric AO Baker."""

from __future__ import annotations

import contextlib
import io
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Sequence, Tuple

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import bake_world_3d_lighting as b3d  # noqa: E402
import geometry_compiler as gc  # noqa: E402
import geometry_inspect as gi  # noqa: E402
from zdir_tool import PS2NFSMWArchive  # noqa: E402

DEFAULT_ISO = Path(os.environ.get("NFSMW_ISO", Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"))


def _chunk(cid: int, payload: bytes) -> bytes:
    return struct.pack("<II", cid, len(payload)) + payload


def _vif_tag(vn: int, vl: int, addr: int, cnt: int, usn: int = 0) -> int:
    return ((0x60 | ((vn & 3) << 2) | (vl & 3)) << 24) | ((cnt & 0xFF) << 16) | (addr & 0x3FF) | ((usn & 1) << 14) | 0x8000


def _pack_strip(
    verts: Sequence[Tuple[float, float, float]], rgba: Sequence[Tuple[int, int, int, int]], adcs: Sequence[int], *, f32: bool = False
) -> bytes:
    nv = len(verts)
    v1_0, v1_2, v1_3 = gc.solve_vu_adc_seeds(adcs)
    scale = 0.00390625
    scale_u32 = struct.unpack("<I", struct.pack("<f", scale))[0]
    bx, by, bz = (float(int(min(v[a] for v in verts)) - 1) for a in range(3))
    out = bytearray()
    out += struct.pack("<I4B", _vif_tag(3, 2, 0x000, 1, 0), nv, max(0, (nv - 1) // 4), 13, 252)
    out += struct.pack("<I4I", _vif_tag(3, 0, 0x001, 1, 1), v1_0, scale_u32, v1_2, v1_3)
    out += struct.pack("<I6f", _vif_tag(2, 0, 0x002, 2, 1), bx + 1.0, by + 1.0, bz + 1.0, 0.0, 0.0, 0.0)
    addr, idx = 0x005, 0
    while idx < nv:
        b = min(4, nv - idx)
        blk = verts[idx : idx + b]
        if f32:
            vals = [v[0] for v in blk] + [v[1] for v in blk] + [v[2] for v in blk]
            out += struct.pack("<I", _vif_tag(b - 1, 0, addr, 3, 1)) + struct.pack(f"<{len(vals)}f", *vals)
        else:
            vals_i = [int(round((v[0] - bx) / scale)) for v in blk] + [int(round((v[1] - by) / scale)) for v in blk] + [int(round((v[2] - bz) / scale)) for v in blk]
            raw = struct.pack(f"<{len(vals_i)}h", *vals_i)
            out += struct.pack("<I", _vif_tag(b - 1, 1, addr, 3, 0)) + raw + b"\x00" * ((4 - (len(raw) & 3)) & 3)
        addr, idx = addr + 3, idx + b
    out += struct.pack("<I", _vif_tag(3, 2, 0x02D, nv, 1)) + b"".join(bytes(c) for c in rgba)
    return bytes(out + b"\x00" * ((16 - (len(out) & 15)) & 15))


def _quad(p0, p1, p2, p3, colors, *, f32: bool = False) -> Tuple[int, int, bytes]:
    return 4, 2, _pack_strip([p0, p1, p2, p3], colors, [1, 1, 0, 0], f32=f32)


def _solid(name: str, strips_spec: Sequence[Tuple[int, int, bytes]], aabb) -> bytes:
    hdr = bytearray(0xA0)
    struct.pack_into("<3f", hdr, 0x20, *aabb[0])
    struct.pack_into("<3f", hdr, 0x30, *aabb[1])
    hb = bytes(hdr) + name.encode("ascii") + b"\x00"
    hb += b"\x00" * ((4 - (len(hb) & 3)) & 3)
    vif, tbl = bytearray(), bytearray()
    for nv, np_c, sb in strips_spec:
        tbl += struct.pack("<HBBfHbbBBbb", 0, nv, np_c, 1.0, len(vif) // 16, 0, 0, len(sb) // 16, 0, 0, -1)
        vif += sb
    plat = struct.pack("<HHII", 32, 0x4000, len(strips_spec), len(vif))
    mesh = _chunk(gi.CHUNK_PLAT_INFO, plat) + _chunk(gi.CHUNK_STRIP_TABLE, bytes(tbl)) + _chunk(gi.CHUNK_VIF_DATA, bytes(vif))
    return _chunk(gi.CHUNK_SOLID_PACK, _chunk(gi.CHUNK_ESOLID_HDR, hb) + _chunk(gi.CHUNK_MESH_CONT, mesh))


def _build_synthetic_world() -> Tuple[bytes, bytes]:
    c_mid, c_road, c_em = (140, 140, 140, 127), (150, 150, 150, 63), (245, 240, 230, 127)
    bldg = _solid("TRN_CT_BLDG_A1", [
        _quad((310, 2400, 0), (310, 2410, 0), (310, 2400, 10), (310, 2410, 10), [c_mid, c_mid, c_mid, c_em]),
        _quad((300, 2410, 0), (300, 2400, 0), (300, 2410, 10), (300, 2400, 10), [c_mid] * 4),
        _quad((300, 2400, 0), (310, 2400, 0), (300, 2400, 10), (310, 2400, 10), [c_mid] * 4),
        _quad((310, 2410, 0), (300, 2410, 0), (310, 2410, 10), (300, 2410, 10), [c_mid] * 4),
        _quad((300, 2400, 10), (310, 2400, 10), (300, 2410, 10), (310, 2410, 10), [c_mid] * 4),
    ], ((300, 2400, 0), (310, 2410, 10)))
    road = _solid("TRN_CT_ROAD_A1", [
        _quad((310, 2400, 0), (311, 2400, 0), (310, 2410, 0), (311, 2410, 0), [c_road] * 4),
        _quad((320, 2400, 0), (326, 2400, 0), (320, 2406, 0), (326, 2406, 0), [(150, 150, 150, 31)] * 4),
        _quad((340, 2400, 0), (346, 2400, 0), (340, 2406, 0), (346, 2406, 0), [(150, 150, 150, 95)] * 4),
    ], ((310, 2400, 0), (346, 2410, 0.1)))
    bridge = _solid("TRN_CT_BRIDGE_A1", [
        _quad((320, 2406, 5.5), (326, 2406, 5.5), (320, 2400, 5.5), (326, 2400, 5.5), [c_mid] * 4),
        _quad((320, 2400, 6.0), (326, 2400, 6.0), (320, 2406, 6.0), (326, 2406, 6.0), [c_mid] * 4),
    ], ((320, 2400, 5.5), (326, 2406, 6.0)))
    sky = _solid("SKY_DOME_01", [_quad((0, 0, 100), (10, 0, 100), (0, 10, 100), (10, 10, 100), [(200, 210, 230, 127)] * 4)], ((0, 0, 100), (10, 10, 100)))
    shd = _solid("SHD_CT_ROAD_A1", [_quad((310, 2400, 0.05), (314, 2400, 0.05), (310, 2404, 0.05), (314, 2404, 0.05), [(82, 89, 100, 127)] * 4)], ((310, 2400, 0), (314, 2404, 0.1)))
    tower = _solid("XB_CITYMODERN01_1C_00", [_quad((5, -5, 0), (5, 5, 0), (5, -5, 40), (5, 5, 40), [c_mid] * 4, f32=True)], ((-5, -5, 0), (5, 5, 40)))

    p_a = _chunk(gi.CHUNK_GEOM_PACK, bldg + road + bridge + sky + shd + tower)
    p_b = _chunk(gi.CHUNK_GEOM_PACK, _solid("TRN_TC_ROAD_B1", [_quad((600, -50, 0), (610, -50, 0), (600, -40, 0), (610, -40, 0), [c_road] * 4)], ((600, -50, 0), (610, -40, 0.1))))
    sec_a = p_a + b"\x00" * ((16 - (len(p_a) & 15)) & 15)
    sec_b = p_b + b"\x00" * ((16 - (len(p_b) & 15)) & 15)

    def _rec(name: str, num: int, off: int, sz: int, cx: float, cy: float) -> bytes:
        r = bytearray(92)
        r[: len(name)] = name.encode("ascii")
        struct.pack_into("<h", r, 8, num)
        struct.pack_into("<II", r, 20, off, sz)
        struct.pack_into("<3f", r, 40, cx, cy, 100.0)
        return bytes(r)

    l2ra = _chunk(b3d.CHUNK_TRACK_SECTIONS, _rec("A41", 141, 0, len(sec_a), 320.0, 2405.0) + _rec("B41", 241, len(sec_a), len(sec_b), 605.0, -45.0))
    return l2ra, sec_a + sec_b


class TestWorld3DLightingOffline(unittest.TestCase):
    def test_synthetic_world_3d_lighting_and_ao(self) -> None:
        l2ra, stream = _build_synthetic_world()
        patched, rep = b3d.bake_streaml2ra_bytes(l2ra, stream, district="all")
        assert patched is not None
        self.assertEqual(len(patched), len(stream))
        self.assertEqual(rep["alpha_bytes_modified"], 0)
        self.assertEqual((rep["solids_baked"], rep["solids_skipped"]), (5, 2))

        by_b = {s.name: s for s in b3d.extract_section_solids(memoryview(stream), 0, len(stream))[0]}
        by_a = {s.name: s for s in b3d.extract_section_solids(memoryview(patched), 0, len(patched))[0]}

        # Sunlit +X wall > shadow -X wall (warm sunlit tint vs cool slate shadow) & base z=0 darker than top z=10
        ba = by_a["TRN_CT_BLDG_A1"]
        sun_top, shd_top, sun_base, em_v = ba.strips[0].rgba[2], ba.strips[1].rgba[2], ba.strips[0].rgba[0], ba.strips[0].rgba[3]
        self.assertGreater(int(sun_top[0]), int(shd_top[0]) + 35)
        self.assertGreater(int(sun_top[0]) - int(sun_top[2]), 0)
        self.assertGreater(int(shd_top[2]) - int(shd_top[0]), 0)
        self.assertLess(int(sun_base[0]), int(sun_top[0]) - 15)
        self.assertGreaterEqual(int(em_v[0]), 242)

        # Overpass underside shades cleanly (never crushed black); road under overpass & at curb darkens vs open sky road
        bridge_r = int(by_a["TRN_CT_BRIDGE_A1"].strips[0].rgba[0, 0])
        self.assertTrue(90 < bridge_r < 124, f"bridge underside R={bridge_r}")
        rd = by_a["TRN_CT_ROAD_A1"]
        self.assertLess(float(rd.strips[1].rgba[:, 0].mean()), float(rd.strips[2].rgba[:, 0].mean()) - 20.0)
        self.assertLess(float(rd.strips[0].rgba[:, 0].mean()), float(rd.strips[2].rgba[:, 0].mean()) - 10.0)

        # Bit-exact alpha across all vertices, V4-32 tower ground gradient, and untouched SKY/SHD chunks
        for name, sb in by_b.items():
            for st_b, st_a in zip(sb.strips, by_a[name].strips):
                np.testing.assert_array_equal(st_b.rgba[:, 3], st_a.rgba[:, 3])
        tw = by_a["XB_CITYMODERN01_1C_00"].strips[0]
        self.assertLess(int(tw.rgba[0, 0]), int(tw.rgba[2, 0]) - 10)
        for tok in (b"SKY_DOME_01", b"SHD_CT_ROAD_A1"):
            i = stream.index(tok)
            self.assertEqual(stream[i : i + 200], patched[i : i + 200])

    def test_concave_vs_convex_corner_ao(self) -> None:
        pts = np.array([[0, 0, 0], [0, 2, 0], [0, 0, 2], [2, 0, 0], [2, 0, 2]], dtype=np.float32)
        tris = np.array([[0, 1, 2], [0, 2, 3], [3, 2, 4]], dtype=np.int32)
        rgba = np.full((5, 4), [140, 140, 140, 127], dtype=np.uint8)
        cfg = b3d.BakeConfig(sun_boost=0.0, shadow_darken=0.0, underside_darken=0.0, ground_ao_strength=0.0)
        c_in, _ = b3d.compute_3d_vertex_colors(pts, tris, rgba, config=cfg, is_local_solid=True, aabb_min_z=-10.0)
        c_out, _ = b3d.compute_3d_vertex_colors(pts, tris[:, ::-1], rgba, config=cfg, is_local_solid=True, aabb_min_z=-10.0)
        self.assertLess(int(c_in[0, 0]), int(c_out[0, 0]))
        self.assertEqual((int(c_in[0, 3]), int(c_out[0, 3])), (127, 127))

    def test_district_filter_dry_run_and_cli(self) -> None:
        l2ra, stream = _build_synthetic_world()
        b41 = next(s for s in b3d.parse_l2ra_sections(l2ra) if s.name == "B41")
        p_rw, rep_rw = b3d.bake_streaml2ra_bytes(l2ra, stream, district="rosewood")
        assert p_rw is not None
        self.assertEqual(rep_rw["sections_selected"], 1)
        self.assertEqual(p_rw[b41.file_offset : b41.file_offset + b41.size], stream[b41.file_offset : b41.file_offset + b41.size])
        self.assertIsNone(b3d.bake_streaml2ra_bytes(l2ra, stream, dry_run=True)[0])
        self.assertEqual(b3d.bake_streaml2ra_bytes(l2ra, stream, config=b3d.BakeConfig(strength=0.0))[0], stream)
        with tempfile.TemporaryDirectory() as td:
            p_l2, p_st, p_out = Path(td) / "L2RA.BUN", Path(td) / "STREAML2RA.BUN", Path(td) / "OUT.BUN"
            p_l2.write_bytes(l2ra)
            p_st.write_bytes(stream)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(b3d.main(["--l2ra", str(p_l2), "--stream", str(p_st), "--output", str(p_out), "--json"]), 0)
            self.assertEqual(len(p_out.read_bytes()), len(stream))
            self.assertTrue((Path(td) / "bake_report.json").is_file())


@unittest.skipUnless(DEFAULT_ISO.is_file(), "Retail SLUS-21351 ISO not present (CI offline mode)")
class TestWorld3DLightingLiveISO(unittest.TestCase):
    def test_live_rosewood_a41_roundtrip(self) -> None:
        arc = PS2NFSMWArchive(DEFAULT_ISO)
        l2ra, stream = arc.read_entry(arc.find(r"TRACKS\L2RA.BUN")), arc.read_entry(arc.find(r"TRACKS\STREAML2RA.BUN"))
        a41 = next(s for s in b3d.parse_l2ra_sections(l2ra) if s.name == "A41")
        patched, rep = b3d.bake_streaml2ra_bytes(l2ra, stream, district="rosewood", section_names=["A41"])
        assert patched is not None
        self.assertEqual((len(patched), rep["alpha_bytes_modified"]), (len(stream), 0))
        self.assertGreater(rep["vertices_baked"], 500)
        self.assertGreater(rep["sunlit_mean_delta"], rep["shadow_mean_delta"])

        s_o = stream[a41.file_offset : a41.file_offset + a41.size]
        s_n = patched[a41.file_offset : a41.file_offset + a41.size]
        self.assertNotEqual(s_o, s_n)
        p, checked = 0, 0
        while p + 8 <= len(s_n):
            cid, csz = struct.unpack_from("<II", s_n, p)
            if cid == gi.CHUNK_GEOM_PACK:
                io, _ = gi.parse_geometry_pack(s_o[p : p + 8 + csz], path="A41_o")
                in_, _ = gi.parse_geometry_pack(s_n[p : p + 8 + csz], path="A41_n")
                self.assertEqual((io.solid_count, io.total_strips, io.total_vertices, io.total_polys), (in_.solid_count, in_.total_strips, in_.total_vertices, in_.total_polys))
                checked += 1
            p += 8 + csz
        self.assertGreater(checked, 0)


if __name__ == "__main__":
    unittest.main()
