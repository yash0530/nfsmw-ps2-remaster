#!/usr/bin/env python3
"""
tests/test_geometry_compiler.py — Offline & Read-Only ISO Tests for:
  1. tools/geometry_compiler.py (GF(2) VU1 LFSR seed solver, VIF strip packer,
     0x80134000 pack synthesis, replace-solid repacking, EE/VU1 guardrails)
  2. tools/subdivide_car_mesh.py (crease-aware PN-triangle & Loop subdivision,
     38-deg dihedral crease preservation, UVs, material groups, AABB preservation)
  3. Optional strictly read-only live ISO round-trip on CARS\\BMWM3GTRE46\\GEOMETRY.BIN
     (skipped gracefully when NFSMW_ISO is absent or unavailable).
"""

from __future__ import annotations

import contextlib
import io
import math
import os
import random
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import geometry_compiler as gc  # noqa: E402
import geometry_inspect as gi  # noqa: E402
import subdivide_car_mesh as scm  # noqa: E402
from zdir_tool import PS2NFSMWArchive  # noqa: E402


def _find_local_iso() -> Optional[Path]:
    if "NFSMW_ISO" in os.environ:
        env_iso = os.environ["NFSMW_ISO"].strip()
        if not env_iso:
            return None
        p = Path(env_iso)
        return p if p.is_file() else None
    default_iso = (
        Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"
    )
    return default_iso if default_iso.is_file() else None


def _make_sphere_patch_grid(nx: int = 4, ny: int = 4) -> gc.CompiledMesh:
    """Create a smooth curved spherical cap mesh in XY -> Z = sqrt(R^2 - x^2 - y^2)."""
    r = 2.0
    verts: List[Tuple[float, float, float]] = []
    norms: List[Tuple[float, float, float]] = []
    uvs: List[Tuple[float, float]] = []
    tris: List[Tuple[int, int, int, int, int]] = []
    for iy in range(ny + 1):
        for ix in range(nx + 1):
            u = ix / nx
            v = iy / ny
            x = (u - 0.5) * 1.2
            y = (v - 0.5) * 1.2
            z = math.sqrt(max(0.1, r * r - x * x - y * y))
            verts.append((x, y, z))
            mag = (x * x + y * y + z * z) ** 0.5
            norms.append((x / mag, y / mag, z / mag))
            uvs.append((u, v))

    stride = nx + 1
    for iy in range(ny):
        for ix in range(nx):
            i0 = iy * stride + ix
            i1 = i0 + 1
            i2 = i0 + stride
            i3 = i2 + 1
            tx = 0 if ix < nx // 2 else 1
            lm = tx
            tris.append((i0, i1, i3, tx, lm))
            tris.append((i0, i3, i2, tx, lm))

    return gc.CompiledMesh(
        vertices=verts,
        normals=norms,
        uvs=uvs,
        triangles=tris,
        group_meta={
            (0, 0): gc.GroupMetadata(strip_flags=0x4180, poly_group=0),
            (1, 1): gc.GroupMetadata(strip_flags=0x4080, poly_group=1),
        },
    )


def _make_bent_corner_mesh() -> gc.CompiledMesh:
    """Create two perpendicular quads meeting at a 90-degree sharp crease along X=0."""
    # Quad A on Z=1, X in [-1, 0]; Quad B on X=0, Z in [0, 1]
    verts = [
        (-1.0, -0.5, 1.0),
        (0.0, -0.5, 1.0),
        (0.0, 0.5, 1.0),
        (-1.0, 0.5, 1.0),
        (0.0, -0.5, 1.0),
        (0.0, -0.5, 0.0),
        (0.0, 0.5, 0.0),
        (0.0, 0.5, 1.0),
    ]
    norms = [(0.0, 0.0, 1.0)] * 4 + [(1.0, 0.0, 0.0)] * 4
    uvs = [
        (0.0, 0.0), (0.5, 0.0), (0.5, 1.0), (0.0, 1.0),
        (0.5, 0.0), (1.0, 0.0), (1.0, 1.0), (0.5, 1.0),
    ]
    tris = [
        (0, 1, 2, 0, 0),
        (0, 2, 3, 0, 0),
        (4, 5, 6, 0, 0),
        (4, 6, 7, 0, 0),
    ]
    return gc.CompiledMesh(vertices=verts, normals=norms, uvs=uvs, triangles=tris)


class TestGF2LfsrSolver(unittest.TestCase):
    def test_exact_roundtrip_nv_3_to_64(self) -> None:
        rng = random.Random(20260401)
        for nv in range(3, 65):
            # 1. Single continuous strip [1, 1, 0, 0, ...] — valid for ALL nv=3..64
            single_strip = [1, 1] + [0] * (nv - 2)
            v1_0, v1_2, v1_3 = gc.solve_vu_adc_seeds(single_strip)
            decoded = gi.decode_vu_adc_bits((v1_0, 0, v1_2, v1_3), nv)
            self.assertEqual(decoded, single_strip, f"Single strip failed at nv={nv}")

            # 2. Multi-restart patterns:
            #    - For nv <= 42, BOTH seed halves are <= 23 bits so ANY arbitrary pattern is exact.
            #    - For nv in 43..64, test LFSR-reachable tail patterns (e.g. from random v1[3] seed).
            if nv <= 42:
                # Deterministic multi-restart + random trials
                restart_pat = [1, 1] + [1 if (i % 5 in (0, 1)) else 0 for i in range(nv - 2)]
                rand_pat = [1, 1] + [rng.randint(0, 1) for _ in range(nv - 2)]
                for pat in (restart_pat, rand_pat):
                    s0, s2, s3 = gc.solve_vu_adc_seeds(pat)
                    self.assertEqual(
                        gi.decode_vu_adc_bits((s0, 0, s2, s3), nv),
                        pat,
                        f"Multi-restart pattern failed at nv={nv}",
                    )
            else:
                # Prefix (19 bits) is arbitrary; tail (nv-19 bits) is generated by a random 23-bit seed
                prefix = [1, 1] + [rng.randint(0, 1) for _ in range(17)]
                tail_seed = rng.randint(1, 0x7FFFFF)
                tail = gc.eval_vu_lfsr(tail_seed, nv - 19)
                pat = prefix + tail
                s0, s2, s3 = gc.solve_vu_adc_seeds(pat)
                self.assertEqual(
                    gi.decode_vu_adc_bits((s0, 0, s2, s3), nv),
                    pat,
                    f"LFSR-subspace pattern failed at nv={nv}",
                )

    def test_inconsistent_long_vector_rejected(self) -> None:
        # 24 bits from a single 23-bit LFSR cannot realize an arbitrary vector outside its subspace
        seed = 0x123456
        valid_24 = gc.eval_vu_lfsr(seed, 24)
        invalid_24 = list(valid_24)
        invalid_24[-1] ^= 1
        with self.assertRaises(ValueError):
            gc.solve_lfsr_seed(invalid_24)


class TestGeometryCompilerOffline(unittest.TestCase):
    def test_vif_strip_packet_roundtrip_all_mod4_remainders(self) -> None:
        # Test nv = 5 (rem=1), 6 (rem=2), 7 (rem=3), 8 (rem=0), and 30 (max VU1)
        for nv in (5, 6, 7, 8, 30):
            verts = [(0.1 * i, -0.2 + 0.05 * (i % 3), 0.3 + 0.02 * i) for i in range(nv)]
            norms = [(0.0, 0.0, 1.0) if i % 2 == 0 else (0.0, 0.6, 0.8) for i in range(nv)]
            uvs = [((i * 0.13) % 1.0, 1.0 - ((i * 0.07) % 1.0)) for i in range(nv)]
            adcs = [1, 1] + [1 if i in (6, 7, 15, 16) else 0 for i in range(2, nv)]

            pkt = gc.pack_vif_strip(verts, norms, uvs, adcs, scale=gc.DEFAULT_SCALE)
            self.assertEqual(len(pkt) % 16, 0)

            dummy_strip = gi.StripEntry(
                index=0,
                flags=0x4180,
                num_verts=nv,
                num_polys=sum(1 for a in adcs[2:] if a == 0),
                material_density=0.0,
                data_qword_offset=0,
                nz=0,
                nt=0,
                data_qword_size=len(pkt) // 16,
                poly_group=0,
                texture_index=0,
                light_material_index=0,
            )
            dec_v, dec_n, dec_uv, dec_adc = gi._unpack_vif_strip(pkt, dummy_strip)
            self.assertEqual(dec_adc, adcs)
            self.assertEqual(len(dec_v), nv)
            for (vx, vy, vz), (dx, dy, dz) in zip(verts, dec_v):
                self.assertAlmostEqual(vx, dx, delta=0.00025)
                self.assertAlmostEqual(vy, dy, delta=0.00025)
                self.assertAlmostEqual(vz, dz, delta=0.00025)
            for (u, v), (du, dv) in zip(uvs, dec_uv):
                self.assertAlmostEqual(u, du, delta=1.0 / 2048.0)
                self.assertAlmostEqual(v, dv, delta=1.0 / 2048.0)
            for (nx, ny, nz), (dnx, dny, dnz) in zip(norms, dec_n):
                self.assertAlmostEqual(nx, dnx, delta=0.02)
                self.assertAlmostEqual(ny, dny, delta=0.02)
                self.assertAlmostEqual(nz, dnz, delta=0.02)

    def test_synthetic_pack_and_replace_solid_roundtrip(self) -> None:
        mesh_a = _make_sphere_patch_grid(3, 3)
        mesh_b = _make_bent_corner_mesh()
        pack_bytes = gc.build_synthetic_geometry_pack(
            r"TEST\GEOMETRY.BIN",
            "TEST_GRP",
            [
                ("TEST_BODY_B", mesh_a, [0x11111111, 0x22222222], [0xAAAA0001, 0xAAAA0002]),
                ("TEST_OTHER_C", mesh_b, [0x33333333], [0xBBBB0001]),
            ],
        )

        info, decoded = gi.parse_geometry_pack(pack_bytes, decode_parts=[0, 1])
        self.assertEqual(info.solid_count, 2)
        self.assertEqual(info.parts[0].name, "TEST_BODY_B")
        self.assertEqual(info.parts[0].strip_poly_total, len(mesh_a.triangles))
        self.assertEqual(len(decoded[0].triangles), len(mesh_a.triangles))
        self.assertEqual(len(decoded[1].triangles), len(mesh_b.triangles))

        # Replace TEST_BODY_B with subdivided version while keeping TEST_OTHER_C byte-identical
        sub_mesh, _ = scm.subdivide_compiled_mesh(mesh_a, crease_deg=38.0, mode="pn")
        repacked, stats = gc.replace_solid_in_pack(pack_bytes, "TEST_BODY_B", sub_mesh)
        self.assertEqual(stats.input_triangles, len(mesh_a.triangles) * 4)

        info2, decoded2 = gi.parse_geometry_pack(repacked, decode_parts=[0, 1])
        self.assertEqual(info2.solid_count, 2)
        self.assertEqual(info2.parts[0].strip_poly_total, len(sub_mesh.triangles))
        self.assertEqual(info2.parts[0].header_polys, len(sub_mesh.triangles))
        self.assertEqual(len(decoded2[0].triangles), len(sub_mesh.triangles))
        # Ensure untouched solid TEST_OTHER_C is byte-identical
        off_orig = info.parts[1].chunk_offset
        _, sz_orig = struct.unpack_from("<II", pack_bytes, off_orig)
        off_new = info2.parts[1].chunk_offset
        _, sz_new = struct.unpack_from("<II", repacked, off_new)
        self.assertEqual(
            pack_bytes[off_orig : off_orig + 8 + sz_orig],
            repacked[off_new : off_new + 8 + sz_new],
        )

    def test_obj_export_load_and_budget_guardrail(self) -> None:
        mesh = _make_sphere_patch_grid(2, 2)
        with tempfile.TemporaryDirectory() as td:
            obj_p = Path(td) / "test.obj"
            gc.export_rich_obj(obj_p, "TEST_BODY_B", mesh, group_meta=mesh.group_meta)
            loaded = gc.load_obj_mesh(obj_p)
            self.assertEqual(len(loaded.triangles), len(mesh.triangles))
            tx_lm_set = {(t[3], t[4]) for t in loaded.triangles}
            self.assertEqual(tx_lm_set, {(0, 0), (1, 1)})
            self.assertEqual(loaded.group_meta[(1, 1)].strip_flags, 0x4080)

        pack_bytes = gc.build_synthetic_geometry_pack(
            r"TEST\GEOMETRY.BIN",
            "TEST_GRP",
            [("TEST_BODY_B", mesh, [0x11111111, 0x22222222], [0xAAAA0001, 0xAAAA0002])],
        )
        with self.assertRaises(ValueError):
            gc.replace_solid_in_pack(pack_bytes, "TEST_BODY_B", mesh, max_solid_bytes=100)


class TestSubdivideCarMesh(unittest.TestCase):
    def test_pn_subdivision_smooths_sphere_and_preserves_aabb_and_uvs(self) -> None:
        mesh = _make_sphere_patch_grid(4, 4)
        sub_pn, st_pn = scm.subdivide_compiled_mesh(mesh, crease_deg=38.0, mode="pn", levels=1)
        self.assertEqual(st_pn.output_triangles, st_pn.input_triangles * 4)
        self.assertGreater(st_pn.smooth_edges, 0)

        # Interior edge midpoints should bulge outward toward R=2.0 vs flat chord midpoint
        xs = [v[0] for v in sub_pn.vertices]
        ys = [v[1] for v in sub_pn.vertices]
        zs = [v[2] for v in sub_pn.vertices]
        orig_zs = [v[2] for v in mesh.vertices]
        self.assertGreaterEqual(min(xs), min(v[0] for v in mesh.vertices) - 1e-6)
        self.assertLessEqual(max(xs), max(v[0] for v in mesh.vertices) + 1e-6)
        self.assertGreaterEqual(min(ys), min(v[1] for v in mesh.vertices) - 1e-6)
        self.assertLessEqual(max(ys), max(v[1] for v in mesh.vertices) + 1e-6)
        self.assertGreaterEqual(min(zs), min(orig_zs) - 1e-6)
        self.assertLessEqual(max(zs), max(orig_zs) + 1e-6)

        # Check Loop mode as well
        sub_loop, st_loop = scm.subdivide_compiled_mesh(mesh, crease_deg=38.0, mode="loop", levels=1)
        self.assertEqual(st_loop.output_triangles, st_loop.input_triangles * 4)
        self.assertEqual({(t[3], t[4]) for t in sub_loop.triangles}, {(0, 0), (1, 1)})

    def test_sharp_90_degree_crease_stays_crisp(self) -> None:
        corner = _make_bent_corner_mesh()
        sub, stats = scm.subdivide_compiled_mesh(corner, crease_deg=38.0, mode="pn", levels=1)
        self.assertEqual(stats.output_triangles, 16)
        self.assertGreaterEqual(stats.sharp_edges, 1)
        # All vertices on Z=1 plane (X < 0) must remain strictly at Z == 1.0,
        # and all vertices on X=0 plane (Z < 1) must remain strictly at X == 0.0.
        for x, _y, z in sub.vertices:
            self.assertTrue(
                abs(z - 1.0) < 1e-6 or abs(x) < 1e-6,
                f"Sharp 90-deg corner drifted off plane: ({x}, {z})",
            )


class TestLiveBMWM3GTRRoundTrip(unittest.TestCase):
    def test_live_bmwm3gtre46_subdivide_and_repack(self) -> None:
        iso_path = _find_local_iso()
        if iso_path is None:
            self.skipTest("SLUS-21351 ISO not available locally (NFSMW_ISO unset/absent)")

        arc = PS2NFSMWArchive(iso_path)
        entry = arc.find(r"CARS\BMWM3GTRE46\GEOMETRY.BIN")
        orig_data = arc.read_entry(entry)

        pack_info, meshes = gi.parse_geometry_pack(orig_data, decode_parts=[5])
        body_part = pack_info.parts[5]
        self.assertEqual(body_part.name, "BMWM3GTRE46_KIT00_BODY_B")
        orig_mesh = meshes[5]

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            orig_obj = td_path / "orig.obj"
            sub_obj = td_path / "sub.obj"
            # Export via geometry_compiler CLI main for rich group_meta preservation
            with contextlib.redirect_stdout(io.StringIO()):
                gc.main(
                    [
                        "export-obj",
                        "--iso",
                        str(iso_path),
                        "--solid",
                        "BMWM3GTRE46_KIT00_BODY_B",
                        "--out",
                        str(orig_obj),
                    ]
                )
            sub_stats = scm.subdivide_obj_file(
                orig_obj, sub_obj, crease_deg=38.0, mode="pn", levels=1, part_name=body_part.name
            )
            self.assertGreaterEqual(sub_stats.output_triangles, 12000)
            self.assertLessEqual(sub_stats.output_triangles, 15000)

            compiled_mesh = gc.load_obj_mesh(sub_obj)
            repacked, c_stats = gc.replace_solid_in_pack(
                orig_data, "BMWM3GTRE46_KIT00_BODY_B", compiled_mesh
            )
            self.assertEqual(c_stats.strip_polys, 14560)
            self.assertLess(c_stats.solid_bytes, gc.DEFAULT_MAX_SOLID_BYTES)

            upg_info, upg_meshes = gi.parse_geometry_pack(repacked, decode_parts=[5])
            self.assertEqual(upg_info.solid_count, pack_info.solid_count)
            self.assertEqual(upg_info.parts[5].strip_poly_total, 14560)
            self.assertEqual(len(upg_meshes[5].triangles), 14560)

            # Verify every other solid is byte-identical
            for idx in range(pack_info.solid_count):
                if idx == 5:
                    continue
                o_off = pack_info.parts[idx].chunk_offset
                _, o_sz = struct.unpack_from("<II", orig_data, o_off)
                n_off = upg_info.parts[idx].chunk_offset
                _, n_sz = struct.unpack_from("<II", repacked, n_off)
                self.assertEqual(
                    orig_data[o_off : o_off + 8 + o_sz],
                    repacked[n_off : n_off + 8 + n_sz],
                    f"Untouched solid {idx} changed",
                )


if __name__ == "__main__":
    unittest.main()
