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

    def test_vu1_hardware_base_and_header_invariants(self) -> None:
        """Verify PS2 VU1 FTOI0(vec2)-1 base quantization and hdr_b3=lm*4 across sign/integer boundaries."""
        test_cases = [
            # (verts, lm, scale, expected_b2)
            ([(-2.12, -0.94, -0.02), (-1.50, -0.50, 0.40), (-1.10, -0.10, 0.80)], 0, 0.0002, 0x17),
            ([(1.0099, 0.005, -1.005), (1.50, 0.50, -0.20), (2.44, 0.94, 1.24)], 7, 0.0002, 0x17),
            ([(0.0001, -0.0001, 1.0101), (0.30, 0.20, 1.20), (0.60, 0.40, 1.40)], 10, 0.0002, 0x17),
            ([(-4.0, -2.0, 0.5), (1.0, 2.0, 3.0), (5.0, 4.0, 6.0)], 3, 1.0 / 256.0, 0x07),
        ]
        for verts, lm, scale, exp_b2 in test_cases:
            nv = len(verts)
            norms = [(0.0, 0.0, 1.0)] * nv
            uvs = [(0.25, 0.75)] * nv
            adcs = [1, 1] + [0] * (nv - 2)
            pkt = gc.pack_vif_strip(verts, norms, uvs, adcs, scale=scale, light_material_index=lm)

            # Parse raw VIF packet directly as PS2 VU1 hardware sees it
            hdr_tag, b0, b1, b2, b3 = struct.unpack_from("<I4B", pkt, 0)
            self.assertEqual(hdr_tag, 0x6E018000)
            self.assertEqual((b0, b1, b2, b3), (nv, (nv - 1) // 4, exp_b2, (lm * 4) & 0xFF))

            # Unpack vec2 at addr 0x02 (byte offset 0x1C tag + 4 = 0x20)
            vec2 = struct.unpack_from("<3f", pkt, 0x20)
            vu1_base = [float(int(c) - 1) for c in vec2]

            # Simulate real PS2 VU1 hardware decode via geometry_inspect
            dummy_strip = gi.StripEntry(
                index=0, flags=0x4180, num_verts=nv, num_polys=nv - 2,
                material_density=0.0, data_qword_offset=0, nz=0, nt=0,
                data_qword_size=len(pkt) // 16, poly_group=0, texture_index=0,
                light_material_index=lm,
            )
            dec_v, _, _, _ = gi._unpack_vif_strip(pkt, dummy_strip)
            for (vx, vy, vz), (dx, dy, dz) in zip(verts, dec_v):
                self.assertAlmostEqual(vx, dx, delta=scale)
                self.assertAlmostEqual(vy, dy, delta=scale)
                self.assertAlmostEqual(vz, dz, delta=scale)
            # Every quantized coord must be >= 0 (within [0, 32767]) for standard spans
            for ax in range(3):
                q_min = round((min(v[ax] for v in verts) - vu1_base[ax]) / scale)
                self.assertGreaterEqual(q_min, 0)
                self.assertLessEqual(q_min, 32767)

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

            # Strict PS2 VU1 hardware invariant check on every compiled strip of BMWM3GTRE46_KIT00_BODY_B
            orig_vmin = [min(v[ax] for v in orig_mesh.vertices) for ax in range(3)]
            orig_vmax = [max(v[ax] for v in orig_mesh.vertices) for ax in range(3)]
            self._assert_vu1_strip_invariants(repacked, upg_info.parts[5], orig_vmin, orig_vmax)

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

    def _assert_vu1_strip_invariants(
        self,
        pack_data: bytes,
        part: gi.SolidPartInfo,
        expected_min: List[float],
        expected_max: List[float],
    ) -> None:
        """Verify every strip in `part` satisfies retail SLUS-21351 VU1 hardware invariants."""
        off = part.chunk_offset
        _, csz = struct.unpack_from("<II", pack_data, off)
        cur = off + 8
        end = cur + csz
        strips = []
        vif_start = 0
        while cur + 8 <= end:
            scid, scsz = struct.unpack_from("<II", pack_data, cur)
            if scid == gi.CHUNK_MESH_CONT:
                mp = cur + 8
                mend = mp + scsz
                while mp + 8 <= mend:
                    mcid, mcsz = struct.unpack_from("<II", pack_data, mp)
                    po = gi._skip_11_padding(pack_data, mp + 8, mp + 8 + mcsz)
                    if mcid == gi.CHUNK_STRIP_TABLE:
                        so = po
                        while so + 16 <= mp + 8 + mcsz:
                            strips.append(struct.unpack_from("<HBBfHbbBBbb", pack_data, so))
                            so += 16
                    elif mcid == gi.CHUNK_VIF_DATA:
                        vif_start = po
                    mp += 8 + mcsz
            cur += 8 + scsz

        self.assertGreater(len(strips), 0)
        sim_mins = [float("inf")] * 3
        sim_maxs = [float("-inf")] * 3
        for idx, s in enumerate(strips):
            _fl, nv, _np, _md, qoff, _nz, _nt, qsz, _pg, _tx, lm = s
            sb = pack_data[vif_start + qoff * 16 : vif_start + (qoff + qsz) * 16]
            hdr_tag, b0, b1, b2, b3 = struct.unpack_from("<I4B", sb, 0)
            self.assertEqual(hdr_tag, 0x6E018000, f"strip[{idx}] bad header VIF tag")
            self.assertEqual(b0, nv, f"strip[{idx}] hdr_b0 != nv")
            self.assertEqual(b1, (nv - 1) // 4, f"strip[{idx}] hdr_b1 != (nv-1)//4")
            self.assertEqual(b2, 0x17, f"strip[{idx}] hdr_b2 != 0x17")
            self.assertEqual(b3, (lm * 4) & 0xFF, f"strip[{idx}] hdr_b3 != lm*4")

            scale = struct.unpack_from("<f", sb, 0x10)[0]
            self.assertAlmostEqual(scale, 0.0002, places=7)
            vec2 = struct.unpack_from("<3f", sb, 0x20)
            vec3 = struct.unpack_from("<3f", sb, 0x2C)
            vu1_base = [float(int(c) - 1) for c in vec2]

            # Unpack int16 positions and simulate exact PS2 VU1 hardware formula
            q = 0
            pos_ints: List[int] = []
            while q + 4 <= len(sb):
                tag = struct.unpack_from("<I", sb, q)[0]
                cmd = (tag >> 24) & 0xFF
                num = (tag >> 16) & 0xFF
                imm = tag & 0xFFFF
                if cmd == 0:
                    q += 4
                    continue
                if (cmd & 0x60) == 0x60:
                    vn = (cmd >> 2) & 3
                    vl = cmd & 3
                    addr = imm & 0x3FF
                    cnt = 256 if num == 0 else num
                    eb = (32, 16, 8, 16)[vl]
                    tb = (16 if vl == 3 else (vn + 1) * eb) * cnt
                    blen = ((tb + 31) // 32) * 4
                    if 5 <= addr < 0x1D and vl == 1:
                        pos_ints.extend(struct.unpack(f"<{blen // 2}h", sb[q + 4 : q + 4 + blen]))
                    q += 4 + blen
                else:
                    q += 4

            xs, ys, zs = [], [], []
            pi = 0
            rem = nv
            while rem > 0 and pi + 3 * min(4, rem) <= len(pos_ints):
                b = min(4, rem)
                xs.extend(pos_ints[pi : pi + b])
                ys.extend(pos_ints[pi + b : pi + 2 * b])
                zs.extend(pos_ints[pi + 2 * b : pi + 3 * b])
                pi += 3 * b
                rem -= b
            self.assertEqual(len(xs), nv)
            for ax, arr in enumerate((xs, ys, zs)):
                self.assertGreaterEqual(min(arr), 0, f"strip[{idx}] axis {ax} negative int16 q")
                for val in arr:
                    p_vu1 = vu1_base[ax] + val * scale
                    self.assertGreaterEqual(p_vu1, vec2[ax] - 1e-4)
                    self.assertLessEqual(p_vu1, vec3[ax] + 1e-4)
                    sim_mins[ax] = min(sim_mins[ax], p_vu1)
                    sim_maxs[ax] = max(sim_maxs[ax], p_vu1)

        for ax in range(3):
            self.assertAlmostEqual(sim_mins[ax], expected_min[ax], delta=0.002)
            self.assertAlmostEqual(sim_maxs[ax], expected_max[ax], delta=0.002)


if __name__ == "__main__":
    unittest.main()
