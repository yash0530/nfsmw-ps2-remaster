#!/usr/bin/env python3
"""export_pbr_scene.py — Clean-room Y-up PBR scene & lighting exporter for Native Metal.

Extracts and assembles the BMW M3 GTR Showcase Scene directly from an untouched
PS2 SLUS-21351 ISO (opened strictly read-only 'rb', supporting NFSMW_ISO env var):
  - BMWM3GTRE46_KIT00_BODY_B (+ windows, headlights, headlight lenses, brakelights, interior)
  - 4x ENKEI_STYLE01_17_25_B wheels positioned & oriented via body wheel-arch anchors
  - Front & rear brake rotors/calipers positioned inside each wheel hub
  - SPOILER_STYLE01_B rear wing (exported & registered; auto-hidden if body already
    contains the integrated ALMS GT wing unless --spoiler force is specified)
  - Converted cleanly from PS2 right-handed Z-up (+X fwd, +Y left, +Z up) to
    Metal Y-up right-handed meters (+X right, +Y up, +Z forward, tires resting at Y=0)
  - Lighting presets & LightMaterial PBR hints loaded from patches/lighting/*.json

Also supports `--synthetic` mode to generate a sleek procedural multi-part sports car
(body + cabin glass + 4 wheels + brake discs + rear GT wing) without requiring an ISO,
for offline CI and unit tests.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from geometry_inspect import DecodedMesh, SolidPartInfo, parse_geometry_pack  # noqa: E402
from jdlz import jdlz_decompress  # noqa: E402
from lighting_ps2 import BCHUNK_LIGHT_MATERIALS, iter_top_level_bchunks, parse_light_materials  # noqa: E402
from zdir_tool import PS2NFSMWArchive, bstringhash  # noqa: E402

DEFAULT_SOURCE_ISO = (
    Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"
)

# Known SLUS-21351 LightMaterial hashes (resolved against GLOBALB.LZC when ISO is present,
# with built-in fallback hashes so offline/pre-extracted packs still resolve cleanly).
BUILTIN_LIGHT_MATERIAL_NAMES: Dict[str, str] = {
    "0xd6d6080a": "CARBODY",
    "0x54949afd": "CHROME",
    "0x12c9453c": "MOLDINGS",
    "0x0fedee40": "DULLPLASTIC",
    "0x52bf4c34": "BOTTOM",
    "0x010cb64a": "PLAINNOTHING",
    "0x2e65e067": "ALUMINUM",
    "0xb57ab98e": "CARBONFIBER2",
    "0x4c95c6f8": "LICENSEPLATE",
    "0xdcd01662": "DIABLOHP",
    "0x02a05578": "DECAL",
    "0x22719fa9": "MAGSILVER",
    "0x78743ca1": "RUBBER",
    "0x471a1dca": "WINDSHIELD",
    "0x3ed70c43": "WINDOWMASK",
    "0xb51b3acf": "HEADLIGHTREFLECTOR",
    "0xa6348ee3": "HEADLIGHTGLASS",
    "0x05bc3a3c": "BRAKELIGHT",
    "0x2787edab": "INTERIOR",
    "0xf0a43b67": "BRAKEDISC",
    "0x1fbd2378": "CALIPERDECAL",
}

# Material PBR definitions shared with Metal shader uniform pipeline
PBR_MATERIALS: Dict[str, Dict[str, Any]] = {
    "car_paint_hero": {
        "kind": "car_paint",
        "base_color": [0.56, 0.60, 0.66],
        "secondary_color": [0.025, 0.14, 0.50],
        "metallic": 0.85,
        "roughness": 0.20,
        "clearcoat": 1.0,
        "clearcoat_roughness": 0.03,
        "ior": 1.5,
        "emissive": [0.0, 0.0, 0.0],
        "alpha": 1.0,
        "livery_enabled": True,
    },
    "chrome_trim": {
        "kind": "metal",
        "base_color": [0.94, 0.95, 0.97],
        "metallic": 1.0,
        "roughness": 0.055,
        "clearcoat": 0.0,
        "clearcoat_roughness": 0.04,
        "ior": 1.5,
        "emissive": [0.0, 0.0, 0.0],
        "alpha": 1.0,
    },
    "brushed_aluminum": {
        "kind": "metal",
        "base_color": [0.78, 0.80, 0.83],
        "metallic": 0.94,
        "roughness": 0.22,
        "clearcoat": 0.15,
        "clearcoat_roughness": 0.08,
        "ior": 1.5,
        "emissive": [0.0, 0.0, 0.0],
        "alpha": 1.0,
    },
    "alloy_rim": {
        "kind": "metal",
        "base_color": [0.84, 0.86, 0.90],
        "metallic": 0.96,
        "roughness": 0.14,
        "clearcoat": 0.45,
        "clearcoat_roughness": 0.05,
        "ior": 1.5,
        "emissive": [0.0, 0.0, 0.0],
        "alpha": 1.0,
    },
    "matte_trim": {
        "kind": "dielectric",
        "base_color": [0.05, 0.055, 0.06],
        "metallic": 0.0,
        "roughness": 0.62,
        "clearcoat": 0.0,
        "clearcoat_roughness": 0.2,
        "ior": 1.45,
        "emissive": [0.0, 0.0, 0.0],
        "alpha": 1.0,
    },
    "carbon_fiber": {
        "kind": "carbon_fiber",
        "base_color": [0.075, 0.08, 0.088],
        "metallic": 0.08,
        "roughness": 0.28,
        "clearcoat": 1.0,
        "clearcoat_roughness": 0.045,
        "ior": 1.5,
        "emissive": [0.0, 0.0, 0.0],
        "alpha": 1.0,
    },
    "rubber_tire": {
        "kind": "rubber",
        "base_color": [0.032, 0.033, 0.036],
        "metallic": 0.0,
        "roughness": 0.82,
        "clearcoat": 0.0,
        "clearcoat_roughness": 0.5,
        "ior": 1.45,
        "emissive": [0.0, 0.0, 0.0],
        "alpha": 1.0,
    },
    "smoked_glass": {
        "kind": "glass",
        "base_color": [0.018, 0.024, 0.036],
        "metallic": 0.02,
        "roughness": 0.03,
        "clearcoat": 1.0,
        "clearcoat_roughness": 0.02,
        "ior": 1.52,
        "emissive": [0.0, 0.0, 0.0],
        "alpha": 0.34,
    },
    "headlight_glass": {
        "kind": "glass",
        "base_color": [0.85, 0.90, 0.98],
        "metallic": 0.02,
        "roughness": 0.03,
        "clearcoat": 1.0,
        "clearcoat_roughness": 0.02,
        "ior": 1.52,
        "emissive": [0.35, 0.42, 0.55],
        "alpha": 0.20,
    },
    "headlight_emissive": {
        "kind": "emissive_headlight",
        "base_color": [0.95, 0.97, 1.0],
        "metallic": 0.85,
        "roughness": 0.08,
        "clearcoat": 1.0,
        "clearcoat_roughness": 0.02,
        "ior": 1.5,
        "emissive": [5.5, 6.2, 7.6],
        "alpha": 1.0,
    },
    "taillight_emissive": {
        "kind": "emissive_taillight",
        "base_color": [0.85, 0.06, 0.03],
        "metallic": 0.05,
        "roughness": 0.12,
        "clearcoat": 1.0,
        "clearcoat_roughness": 0.03,
        "ior": 1.5,
        "emissive": [5.2, 0.45, 0.12],
        "alpha": 1.0,
    },
    "interior_dark": {
        "kind": "dielectric",
        "base_color": [0.06, 0.062, 0.068],
        "metallic": 0.02,
        "roughness": 0.72,
        "clearcoat": 0.0,
        "clearcoat_roughness": 0.4,
        "ior": 1.45,
        "emissive": [0.0, 0.0, 0.0],
        "alpha": 1.0,
    },
    "brake_rotor": {
        "kind": "metal",
        "base_color": [0.62, 0.64, 0.68],
        "metallic": 0.90,
        "roughness": 0.28,
        "clearcoat": 0.0,
        "clearcoat_roughness": 0.1,
        "ior": 1.5,
        "emissive": [0.0, 0.0, 0.0],
        "alpha": 1.0,
    },
    "brake_caliper": {
        "kind": "car_paint",
        "base_color": [0.82, 0.12, 0.08],
        "secondary_color": [0.82, 0.12, 0.08],
        "metallic": 0.55,
        "roughness": 0.24,
        "clearcoat": 0.9,
        "clearcoat_roughness": 0.05,
        "ior": 1.5,
        "emissive": [0.0, 0.0, 0.0],
        "alpha": 1.0,
        "livery_enabled": False,
    },
    "license_plate": {
        "kind": "dielectric",
        "base_color": [0.16, 0.19, 0.24],
        "metallic": 0.15,
        "roughness": 0.38,
        "clearcoat": 0.3,
        "clearcoat_roughness": 0.1,
        "ior": 1.5,
        "emissive": [0.0, 0.0, 0.0],
        "alpha": 1.0,
    },
}


# ---------------------------------------------------------------------------
# Coordinate Conversion & Mesh Transform Helpers
# ---------------------------------------------------------------------------


def ps2_vec_to_yup(v: Tuple[float, float, float], ground_z_ps2: float = 0.0) -> Tuple[float, float, float]:
    """Convert PS2 right-handed Z-up (+X fwd, +Y left, +Z up) to Y-up right-handed meters.

    Mapping: (x_yup, y_yup, z_yup) = (-y_ps2, z_ps2 - ground_z_ps2, x_ps2).
    Determinant = +1 (proper rotation), preserving outward normals and CCW winding.
    """
    return (-v[1], v[2] - ground_z_ps2, v[0])


def ps2_norm_to_yup(n: Tuple[float, float, float]) -> Tuple[float, float, float]:
    """Rotate normal vector from PS2 Z-up into Y-up right-handed space."""
    nx, ny, nz = -n[1], n[2], n[0]
    mag = math.sqrt(nx * nx + ny * ny + nz * nz)
    if mag > 1e-6:
        return (nx / mag, ny / mag, nz / mag)
    return (0.0, 1.0, 0.0)


def transform_ps2_mesh_to_yup(
    mesh: DecodedMesh,
    offset_ps2: Tuple[float, float, float] = (0.0, 0.0, 0.0),
    rotate_z_180_ps2: bool = False,
    scale: float = 1.0,
    ground_z_ps2: float = 0.0,
) -> DecodedMesh:
    """Apply optional 180-deg Z rotation + uniform scale + PS2 offset, then convert to Y-up."""
    ox, oy, oz = offset_ps2
    new_verts: List[Tuple[float, float, float]] = []
    new_norms: List[Tuple[float, float, float]] = []
    for (vx, vy, vz), (nx, ny, nz) in zip(mesh.vertices, mesh.normals):
        if rotate_z_180_ps2:
            vx, vy = -vx, -vy
            nx, ny = -nx, -ny
        px = vx * scale + ox
        py = vy * scale + oy
        pz = vz * scale + oz
        new_verts.append(ps2_vec_to_yup((px, py, pz), ground_z_ps2=ground_z_ps2))
        new_norms.append(ps2_norm_to_yup((nx, ny, nz)))
    return DecodedMesh(
        vertices=new_verts,
        normals=new_norms,
        uvs=list(mesh.uvs),
        triangles=list(mesh.triangles),
    )


def map_ps2_material_name(part_name: str, mat_name: str) -> str:
    """Translate PS2 part name + eLightMaterial name into a clean 2026 PBR material key."""
    pn = part_name.upper()
    mn = mat_name.upper()

    if "HEADLIGHT_GLASS" in pn or mn == "HEADLIGHTGLASS":
        return "headlight_glass"
    if "HEADLIGHT" in pn:
        if mn in ("CHROME", "MOLDINGS"):
            return "chrome_trim" if mn == "CHROME" else "matte_trim"
        return "headlight_emissive"
    if "BRAKELIGHT" in pn or mn == "BRAKELIGHT":
        if mn == "MOLDINGS":
            return "matte_trim"
        return "taillight_emissive"
    if mn == "WINDSHIELD" or ("WINDOW" in pn and mn != "WINDOWMASK"):
        return "smoked_glass"
    if mn == "WINDOWMASK":
        return "matte_trim"
    if mn == "INTERIOR" or "INTERIOR" in pn:
        return "interior_dark"
    if mn == "BRAKEDISC":
        return "brake_rotor"
    if mn == "CALIPERDECAL":
        return "brake_caliper"
    if mn in ("CARBODY", "DIABLOHP", "DECAL"):
        return "car_paint_hero"
    if mn == "CHROME":
        return "chrome_trim"
    if mn == "ALUMINUM":
        return "brushed_aluminum"
    if mn == "MAGSILVER":
        return "alloy_rim"
    if mn in ("CARBONFIBER", "CARBONFIBER2") or "_CF_" in pn:
        return "carbon_fiber"
    if mn == "RUBBER" or "TIRE" in pn:
        return "rubber_tire"
    if mn == "LICENSEPLATE":
        return "license_plate"
    if mn in ("MOLDINGS", "DULLPLASTIC", "BOTTOM", "PLAINNOTHING"):
        return "matte_trim"
    if "SPOILER" in pn:
        return "carbon_fiber"
    return "car_paint_hero"


def write_pbr_obj_mesh(
    out_path: Path,
    part_name: str,
    mesh: DecodedMesh,
    part_info: Optional[SolidPartInfo],
    mat_lookup: Dict[str, str],
    mtl_filename: str = "materials.mtl",
) -> Tuple[Path, Dict[str, int]]:
    """Write a Y-up DecodedMesh to Wavefront .obj with semantic `usemtl` material groups."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = [
        f"# NFSMW Rockport 2026 PBR Scene Mesh (Y-up right-handed meters): {part_name}",
        f"# Vertices: {len(mesh.vertices)}, Triangles: {len(mesh.triangles)}",
        f"mtllib {mtl_filename}",
        f"o {part_name}",
    ]
    for x, y, z in mesh.vertices:
        lines.append(f"v {x:.6f} {y:.6f} {z:.6f}")
    for u, v in mesh.uvs:
        lines.append(f"vt {u:.6f} {v:.6f}")
    for nx, ny, nz in mesh.normals:
        lines.append(f"vn {nx:.6f} {ny:.6f} {nz:.6f}")

    # Resolve per-material-index PBR names
    mat_hashes = part_info.light_material_hashes if part_info else []
    idx_to_pbr: Dict[int, str] = {}
    for m_idx, mh in enumerate(mat_hashes):
        ps2_mat = mat_lookup.get(mh.lower(), mh)
        idx_to_pbr[m_idx] = map_ps2_material_name(part_name, ps2_mat)

    # Sort triangles by material so OBJ has clean contiguous `usemtl` groups
    sorted_tris = sorted(mesh.triangles, key=lambda t: t[4])
    mat_counts: Dict[str, int] = {}
    cur_mtl: Optional[str] = None
    for i0, i1, i2, _tx, lm in sorted_tris:
        if lm in idx_to_pbr:
            mtl = idx_to_pbr[lm]
        elif 0 <= lm < len(mat_hashes):
            mtl = map_ps2_material_name(part_name, mat_hashes[lm])
        else:
            mtl = map_ps2_material_name(part_name, "")
        mat_counts[mtl] = mat_counts.get(mtl, 0) + 1
        if mtl != cur_mtl:
            cur_mtl = mtl
            lines.append(f"g {part_name}_{mtl}")
            lines.append(f"usemtl {mtl}")
        a, b, c = i0 + 1, i1 + 1, i2 + 1
        lines.append(f"f {a}/{a}/{a} {b}/{b}/{b} {c}/{c}/{c}")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path, mat_counts


def write_mtl_library(out_path: Path) -> Path:
    """Write a companion Wavefront .mtl library for external inspection tools."""
    lines = ["# NFSMW Rockport 2026 PBR Material Library"]
    for name, props in PBR_MATERIALS.items():
        r, g, b = props["base_color"]
        er, eg, eb = props["emissive"]
        alpha = props.get("alpha", 1.0)
        lines.append(f"\nnewmtl {name}")
        lines.append(f"Kd {r:.4f} {g:.4f} {b:.4f}")
        lines.append(f"Ke {er:.4f} {eg:.4f} {eb:.4f}")
        lines.append(f"Pm {props['metallic']:.4f}")
        lines.append(f"Pr {props['roughness']:.4f}")
        lines.append(f"d {alpha:.4f}")
        lines.append("illum 2")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Lighting Preset Loader (from patches/lighting/*.json)
# ---------------------------------------------------------------------------


def _normalize3(v: Sequence[float]) -> List[float]:
    mag = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    if mag < 1e-6:
        return [0.4, 0.7, 0.5]
    return [round(v[0] / mag, 6), round(v[1] / mag, 6), round(v[2] / mag, 6)]


def load_lighting_presets(repo_root: Path) -> Dict[str, Dict[str, Any]]:
    """Build Golden Hour, Daylight, and Night Rain presets grounded in patches/lighting/*.json."""
    lighting_dir = repo_root / "patches" / "lighting"

    def _extract_patch_profile(json_path: Path, target_sun: str) -> Dict[str, Any]:
        if not json_path.is_file():
            return {}
        doc = json.loads(json_path.read_text(encoding="utf-8"))
        out: Dict[str, Any] = {"source": str(json_path.relative_to(repo_root)), "layers": {}, "materials": {}}
        for p in doc.get("patches", []):
            if p.get("section") == "SunInfos" and p.get("profile") == target_sun:
                if p.get("field") == "Position":
                    out["sun_pos_ps2"] = p["new_value"]
                elif p.get("field") == "SunLayers":
                    idx = int(p.get("index", 0))
                    out["layers"].setdefault(idx, {})[p["member"]] = p["new_value"]
            elif p.get("section") == "LightMaterials":
                mat = p.get("material", "")
                out["materials"].setdefault(mat, {})[p["field"]] = p["new_value"]
        return out

    gh_data = _extract_patch_profile(lighting_dir / "rockport2026_golden_hour.json", "SUNSET")
    dl_data = _extract_patch_profile(lighting_dir / "rockport2026_daylight.json", "MIDDAY")

    # Convert PS2 sun elevation from patches/lighting/*.json and orient showcase sun azimuth
    # toward the distant Rockport horizon (-X, -Z) so the sun disc, corona, and wet-asphalt
    # specular path frame the front-3/4 BMW M3 GTR showcase cleanly.
    gh_ps2 = gh_data.get("sun_pos_ps2", [5200.0, -1400.0, 1150.0])
    gh_horiz = math.hypot(gh_ps2[0], gh_ps2[1])
    gh_elev = math.atan2(gh_ps2[2], max(gh_horiz, 1.0))
    gh_dir = _normalize3([-math.cos(gh_elev) * 0.72, math.sin(gh_elev) * 0.92, -math.cos(gh_elev) * 0.69])

    dl_ps2 = dl_data.get("sun_pos_ps2", [5300.0, -3350.0, 3850.0])
    dl_horiz = math.hypot(dl_ps2[0], dl_ps2[1])
    dl_elev = math.atan2(dl_ps2[2], max(dl_horiz, 1.0))
    dl_dir = _normalize3([-math.cos(dl_elev) * 0.48, math.sin(dl_elev), -math.cos(dl_elev) * 0.88])

    gh_l0 = gh_data.get("layers", {}).get(0, {})
    gh_l1 = gh_data.get("layers", {}).get(1, {})
    gh_col0 = gh_l0.get("Colour", [255, 236, 178, 128])
    gh_col1 = gh_l1.get("Colour", [255, 178, 86, 128])

    dl_l0 = dl_data.get("layers", {}).get(0, {})
    dl_l1 = dl_data.get("layers", {}).get(1, {})
    dl_col0 = dl_l0.get("Colour", [255, 252, 244, 128])
    dl_col1 = dl_l1.get("Colour", [175, 215, 255, 128])

    return {
        "golden_hour": {
            "name": "Rockport 2026 Golden Hour",
            "source_patch": gh_data.get("source", "patches/lighting/rockport2026_golden_hour.json"),
            "sun_direction": gh_dir,
            "sun_color": [round(gh_col0[0] / 255.0 * 1.10, 4), round(gh_col0[1] / 255.0 * 0.85, 4), round(gh_col0[2] / 255.0 * 0.62, 4)],
            "sun_intensity": round(float(gh_l0.get("IntensityScale", 1.35)) * 3.1, 3),
            "corona_color": [round(gh_col1[0] / 255.0 * 0.85, 4), round(gh_col1[1] / 255.0 * 0.68, 4), round(gh_col1[2] / 255.0 * 0.52, 4)],
            "sky_zenith": [0.045, 0.11, 0.30],
            "sky_horizon": [0.68, 0.32, 0.13],
            "sky_ground": [0.025, 0.022, 0.025],
            "ibl_intensity": 0.92,
            "wetness": 0.68,
            "fog_density": 0.011,
            "exposure": 1.02,
            "bloom_threshold": 1.35,
            "bloom_intensity": 0.42,
            "headlights_on": 0.95,
        },
        "daylight": {
            "name": "Rockport 2026 Crisp Daylight",
            "source_patch": dl_data.get("source", "patches/lighting/rockport2026_daylight.json"),
            "sun_direction": dl_dir,
            "sun_color": [round(dl_col0[0] / 255.0, 4), round(dl_col0[1] / 255.0, 4), round(dl_col0[2] / 255.0, 4)],
            "sun_intensity": round(float(dl_l0.get("IntensityScale", 1.15)) * 3.4, 3),
            "corona_color": [round(dl_col1[0] / 255.0 * 0.65, 4), round(dl_col1[1] / 255.0 * 0.72, 4), round(dl_col1[2] / 255.0 * 0.82, 4)],
            "sky_zenith": [0.06, 0.22, 0.64],
            "sky_horizon": [0.40, 0.58, 0.82],
            "sky_ground": [0.035, 0.040, 0.048],
            "ibl_intensity": 0.95,
            "wetness": 0.45,
            "fog_density": 0.007,
            "exposure": 0.96,
            "bloom_threshold": 1.55,
            "bloom_intensity": 0.28,
            "headlights_on": 0.55,
        },
        "night_rain": {
            "name": "Rockport 2026 Midnight Rain",
            "source_patch": "procedural_night_rain",
            "sun_direction": _normalize3([-0.45, 0.38, 0.80]),
            "sun_color": [0.48, 0.64, 1.0],
            "sun_intensity": 2.1,
            "corona_color": [0.18, 0.36, 0.82],
            "sky_zenith": [0.008, 0.015, 0.042],
            "sky_horizon": [0.055, 0.088, 0.165],
            "sky_ground": [0.010, 0.012, 0.018],
            "ibl_intensity": 0.88,
            "wetness": 0.92,
            "fog_density": 0.013,
            "exposure": 1.16,
            "bloom_threshold": 1.05,
            "bloom_intensity": 0.62,
            "headlights_on": 1.45,
        },
    }


# ---------------------------------------------------------------------------
# BMW M3 GTR ISO Extraction & Assembly
# ---------------------------------------------------------------------------


def _compute_wheel_arch_anchors(body_mesh: DecodedMesh, body_part: SolidPartInfo) -> Dict[str, float]:
    """Derive exact front/rear axle X, wheel Z, and track Y from BMW body geometry + AABB."""
    bmin = body_part.aabb_min
    bmax = body_part.aabb_max
    half_width = max(abs(bmin[1]), abs(bmax[1]))

    def _find_arch(x0: float, x1: float, fallback_x: float) -> Tuple[float, float]:
        lip = [
            (x, z)
            for x, y, z in body_mesh.vertices
            if x0 <= x <= x1 and y >= 0.88 * half_width and 0.22 <= z <= 0.59
        ]
        if len(lip) >= 6:
            min_x = min(p[0] for p in lip)
            max_x = max(p[0] for p in lip)
            top_z = max(p[1] for p in lip)
            return ((min_x + max_x) * 0.5, top_z)
        return (fallback_x, 0.57)

    length = bmax[0] - bmin[0]
    front_x, front_top_z = _find_arch(bmin[0] + 0.72 * length, bmax[0] - 0.06 * length, bmin[0] + 0.812 * length)
    rear_x, rear_top_z = _find_arch(bmin[0] + 0.08 * length, bmin[0] + 0.34 * length, bmin[0] + 0.214 * length)
    arch_top_z = min(front_top_z, rear_top_z)
    return {
        "front_x": front_x,
        "rear_x": rear_x,
        "arch_top_z": arch_top_z,
        "half_width": half_width,
        "length": length,
    }


def _body_has_builtin_wing(body_mesh: DecodedMesh, body_part: SolidPartInfo) -> bool:
    """Detect whether the extracted body mesh already integrates a high-mount GT wing."""
    x_thresh = body_part.aabb_min[0] + 0.14 * (body_part.aabb_max[0] - body_part.aabb_min[0])
    z_thresh = body_part.aabb_max[2] * 0.82
    wing_verts = sum(1 for x, _y, z in body_mesh.vertices if x < x_thresh and z > z_thresh)
    return wing_verts >= 30


def export_iso_scene(
    iso_path: Path,
    out_dir: Path,
    spoiler_mode: str = "auto",
) -> Dict[str, Any]:
    """Extract BMW M3 GTR showcase parts from untouched ISO (strictly read-only 'rb')."""
    if not iso_path.is_file():
        raise FileNotFoundError(f"ISO not found: {iso_path}")

    arc = PS2NFSMWArchive(iso_path)

    def _read_vpath(vpath: str) -> bytes:
        target = vpath.upper()
        ent = next((e for e in arc.entries if (e.resolved_path or "").upper() == target), None)
        if ent is None:
            raise KeyError(f"Archive entry not found: {vpath}")
        return arc.read_entry(ent)

    # Resolve LightMaterial hash -> name lookup from GLOBALB.LZC
    mat_lookup: Dict[str, str] = dict(BUILTIN_LIGHT_MATERIAL_NAMES)
    try:
        gb_raw = jdlz_decompress(_read_vpath("GLOBAL\\GLOBALB.LZC"))
        chunks = iter_top_level_bchunks(gb_raw)
        for lm in parse_light_materials(gb_raw, chunks):
            mat_lookup[f"0x{bstringhash(lm.name):08x}"] = lm.name
            mat_lookup[f"0x{lm.name_hash:08x}"] = lm.name
    except Exception:
        pass

    car_pack_bytes = _read_vpath("CARS\\BMWM3GTRE46\\GEOMETRY.BIN")
    wheel_pack_bytes = _read_vpath("CARS\\WHEELS\\GEOMETRY.BIN")
    spoiler_pack_bytes = _read_vpath("CARS\\SPOILER\\GEOMETRY.BIN")

    car_info, _ = parse_geometry_pack(car_pack_bytes, path="CARS\\BMWM3GTRE46\\GEOMETRY.BIN")
    wheel_info, _ = parse_geometry_pack(wheel_pack_bytes, path="CARS\\WHEELS\\GEOMETRY.BIN")
    spoiler_info, _ = parse_geometry_pack(spoiler_pack_bytes, path="CARS\\SPOILER\\GEOMETRY.BIN")

    wanted_car_parts = [
        "BMWM3GTRE46_KIT00_BODY_B",
        "BMWM3GTRE46_KIT00_INTERIOR_B",
        "BMWM3GTRE46_KIT00_FRONT_WINDOW_B",
        "BMWM3GTRE46_KIT00_REAR_WINDOW_B",
        "BMWM3GTRE46_KIT00_FRONT_LEFT_WINDOW_B",
        "BMWM3GTRE46_KIT00_FRONT_RIGHT_WINDOW_B",
        "BMWM3GTRE46_KIT00_REAR_LEFT_WINDOW_B",
        "BMWM3GTRE46_KIT00_REAR_RIGHT_WINDOW_B",
        "BMWM3GTRE46_KIT00_LEFT_HEADLIGHT_B",
        "BMWM3GTRE46_KIT00_RIGHT_HEADLIGHT_B",
        "BMWM3GTRE46_KIT00_LEFT_HEADLIGHT_GLASS_B",
        "BMWM3GTRE46_KIT00_RIGHT_HEADLIGHT_GLASS_B",
        "BMWM3GTRE46_KIT00_LEFT_BRAKELIGHT_B",
        "BMWM3GTRE46_KIT00_RIGHT_BRAKELIGHT_B",
        "BMWM3GTRE46_KIT00_FRONT_BRAKE_B",
        "BMWM3GTRE46_KIT00_REAR_BRAKE_B",
    ]
    car_idx_by_name = {p.name: p.index for p in car_info.parts}
    decode_car_indices = [car_idx_by_name[n] for n in wanted_car_parts if n in car_idx_by_name]
    _, car_meshes = parse_geometry_pack(car_pack_bytes, decode_parts=decode_car_indices)

    wheel_part = next(p for p in wheel_info.parts if p.name == "ENKEI_STYLE01_17_25_B")
    _, wheel_meshes = parse_geometry_pack(wheel_pack_bytes, decode_parts=[wheel_part.index])
    wheel_mesh = wheel_meshes[wheel_part.index]

    spoiler_part = next(
        (p for p in spoiler_info.parts if p.name == "SPOILER_STYLE01_B"),
        spoiler_info.parts[0],
    )
    _, spoiler_meshes = parse_geometry_pack(spoiler_pack_bytes, decode_parts=[spoiler_part.index])
    spoiler_mesh = spoiler_meshes[spoiler_part.index]

    body_idx = car_idx_by_name["BMWM3GTRE46_KIT00_BODY_B"]
    body_part = car_info.parts[body_idx]
    body_mesh = car_meshes[body_idx]

    anchors = _compute_wheel_arch_anchors(body_mesh, body_part)
    wheel_r = max(abs(wheel_part.aabb_min[2]), abs(wheel_part.aabb_max[2]))
    # Compute wheel scale from arch clearance so tire fits tightly inside E46 GTR widebody arches
    arch_clearance = 0.044
    wheel_scale = 1.0
    wheel_z = anchors["arch_top_z"] - wheel_r * wheel_scale - arch_clearance
    wheel_min_z = min(v[2] for v in wheel_mesh.vertices) * wheel_scale
    ground_z_ps2 = wheel_z + wheel_min_z

    # ENKEI_STYLE01_17_25_B outer rim face is at local y_min (~ -0.017) facing -Y
    outer_face_y = anchors["half_width"] * 0.975
    wheel_y_offset = outer_face_y + wheel_part.aabb_min[1] * wheel_scale

    meshes_dir = out_dir / "meshes"
    meshes_dir.mkdir(parents=True, exist_ok=True)
    write_mtl_library(meshes_dir / "materials.mtl")

    scene_parts: List[Dict[str, Any]] = []
    total_verts = 0
    total_tris = 0

    def _emit_part(
        obj_name: str,
        mesh: DecodedMesh,
        pinfo: Optional[SolidPartInfo],
        role: str,
        transparent: bool = False,
        visible: bool = True,
    ) -> None:
        nonlocal total_verts, total_tris
        obj_rel = f"meshes/{obj_name}.obj"
        _, mat_counts = write_pbr_obj_mesh(meshes_dir / f"{obj_name}.obj", obj_name, mesh, pinfo, mat_lookup)
        total_verts += len(mesh.vertices)
        total_tris += len(mesh.triangles)
        scene_parts.append(
            {
                "name": obj_name,
                "obj": obj_rel,
                "role": role,
                "transparent": transparent,
                "visible": visible,
                "vertex_count": len(mesh.vertices),
                "triangle_count": len(mesh.triangles),
                "materials": mat_counts,
            }
        )

    # 1. Body + fixed trim/glass/lights/interior
    for pname in wanted_car_parts:
        if pname not in car_idx_by_name or "BRAKE_B" in pname:
            continue
        pidx = car_idx_by_name[pname]
        pinfo = car_info.parts[pidx]
        ymesh = transform_ps2_mesh_to_yup(car_meshes[pidx], ground_z_ps2=ground_z_ps2)
        is_glass = "WINDOW" in pname or "HEADLIGHT_GLASS" in pname
        role = "glass" if is_glass else ("lights" if ("HEADLIGHT" in pname or "BRAKELIGHT" in pname) else "body")
        _emit_part(pname, ymesh, pinfo, role=role, transparent=is_glass, visible=True)

    # 2. 4x ENKEI_STYLE01_17_25_B wheels + brake rotors/calipers at each wheel hub
    wheel_specs = [
        ("WHEEL_FL", (anchors["front_x"], +wheel_y_offset, wheel_z), True, "BMWM3GTRE46_KIT00_FRONT_BRAKE_B"),
        ("WHEEL_FR", (anchors["front_x"], -wheel_y_offset, wheel_z), False, "BMWM3GTRE46_KIT00_FRONT_BRAKE_B"),
        ("WHEEL_RL", (anchors["rear_x"], +wheel_y_offset, wheel_z), True, "BMWM3GTRE46_KIT00_REAR_BRAKE_B"),
        ("WHEEL_RR", (anchors["rear_x"], -wheel_y_offset, wheel_z), False, "BMWM3GTRE46_KIT00_REAR_BRAKE_B"),
    ]
    for wname, pos_ps2, rot180, brake_part_name in wheel_specs:
        wmesh_yup = transform_ps2_mesh_to_yup(
            wheel_mesh,
            offset_ps2=pos_ps2,
            rotate_z_180_ps2=rot180,
            scale=wheel_scale,
            ground_z_ps2=ground_z_ps2,
        )
        _emit_part(f"ENKEI_STYLE01_17_25_B_{wname}", wmesh_yup, wheel_part, role="wheel", visible=True)
        if brake_part_name in car_idx_by_name:
            bidx = car_idx_by_name[brake_part_name]
            binfo = car_info.parts[bidx]
            # Place brake disc slightly inboard behind the spokes
            inboard = -0.045 if pos_ps2[1] > 0 else +0.045
            bpos = (pos_ps2[0], pos_ps2[1] + inboard, pos_ps2[2])
            bmesh_yup = transform_ps2_mesh_to_yup(
                car_meshes[bidx],
                offset_ps2=bpos,
                rotate_z_180_ps2=rot180,
                scale=1.0,
                ground_z_ps2=ground_z_ps2,
            )
            _emit_part(f"{brake_part_name}_{wname}", bmesh_yup, binfo, role="brake", visible=True)

    # 3. Rear Wing SPOILER_STYLE01_B
    has_builtin_wing = _body_has_builtin_wing(body_mesh, body_part)
    spoiler_visible = (spoiler_mode == "force") or (spoiler_mode == "auto" and not has_builtin_wing)
    trunk_x = body_part.aabb_min[0] + 0.24
    trunk_z = 0.875
    spoiler_yup = transform_ps2_mesh_to_yup(
        spoiler_mesh,
        offset_ps2=(trunk_x, 0.0, trunk_z),
        ground_z_ps2=ground_z_ps2,
    )
    _emit_part("SPOILER_STYLE01_B", spoiler_yup, spoiler_part, role="spoiler", visible=spoiler_visible)

    return _build_scene_manifest(
        mode="iso_bmwm3gtre46",
        car_name="BMW M3 GTR E46 (SLUS-21351)",
        parts=scene_parts,
        total_verts=total_verts,
        total_tris=total_tris,
        anchors=anchors,
        wheel_z_ps2=wheel_z,
        ground_z_ps2=ground_z_ps2,
        spoiler_visible=spoiler_visible,
        has_builtin_wing=has_builtin_wing,
    )


# ---------------------------------------------------------------------------
# Synthetic Procedural Sports Car Generator (for CI & Offline Tests)
# ---------------------------------------------------------------------------


def _make_box_mesh(
    cx: float, cy: float, cz: float, sx: float, sy: float, sz: float, mat_idx: int = 0
) -> DecodedMesh:
    """Create a closed Y-up right-handed box mesh with outward normals."""
    hx, hy, hz = sx * 0.5, sy * 0.5, sz * 0.5
    faces = [
        # (normal, 4 corners CCW from outside)
        ((0, 0, 1), [(-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz)]),
        ((0, 0, -1), [(hx, -hy, -hz), (-hx, -hy, -hz), (-hx, hy, -hz), (hx, hy, -hz)]),
        ((1, 0, 0), [(hx, -hy, hz), (hx, -hy, -hz), (hx, hy, -hz), (hx, hy, hz)]),
        ((-1, 0, 0), [(-hx, -hy, -hz), (-hx, -hy, hz), (-hx, hy, hz), (-hx, hy, -hz)]),
        ((0, 1, 0), [(-hx, hy, hz), (hx, hy, hz), (hx, hy, -hz), (-hx, hy, -hz)]),
        ((0, -1, 0), [(-hx, -hy, -hz), (hx, -hy, -hz), (hx, -hy, hz), (-hx, -hy, hz)]),
    ]
    verts: List[Tuple[float, float, float]] = []
    norms: List[Tuple[float, float, float]] = []
    uvs: List[Tuple[float, float]] = []
    tris: List[Tuple[int, int, int, int, int]] = []
    for n, corners in faces:
        base = len(verts)
        for i, (px, py, pz) in enumerate(corners):
            verts.append((cx + px, cy + py, cz + pz))
            norms.append(n)
            uvs.append((float(i in (1, 2)), float(i >= 2)))
        tris.append((base, base + 1, base + 2, 0, mat_idx))
        tris.append((base, base + 2, base + 3, 0, mat_idx))
    return DecodedMesh(vertices=verts, normals=norms, uvs=uvs, triangles=tris)


def _make_cylinder_wheel(cx: float, cy: float, cz: float, radius: float, width: float, segs: int = 28) -> DecodedMesh:
    """Create a sleek procedural Y-up wheel (outer rim alloy_rim + rubber_tire)."""
    verts: List[Tuple[float, float, float]] = []
    norms: List[Tuple[float, float, float]] = []
    uvs: List[Tuple[float, float]] = []
    tris: List[Tuple[int, int, int, int, int]] = []

    hw = width * 0.5
    # Tire tread ring (mat_idx = 0 -> rubber_tire)
    for i in range(segs):
        a0 = 2.0 * math.pi * i / segs
        a1 = 2.0 * math.pi * (i + 1) / segs
        y0, z0 = math.sin(a0), math.cos(a0)
        y1, z1 = math.sin(a1), math.cos(a1)
        b = len(verts)
        for x_off, yy, zz in [(-hw, y0, z0), (hw, y0, z0), (hw, y1, z1), (-hw, y1, z1)]:
            verts.append((cx + x_off, cy + radius * yy, cz + radius * zz))
            norms.append((0.0, yy, zz))
            uvs.append((0.5 + 0.5 * yy, 0.5 + 0.5 * zz))
        tris.append((b, b + 1, b + 2, 0, 0))
        tris.append((b, b + 2, b + 3, 0, 0))

    # Sidewall + alloy rim caps (mat_idx = 1 -> alloy_rim on inner spokes, 0 -> tire outer ring)
    rim_r = radius * 0.68
    for side in (-1.0, 1.0):
        x_cap = cx + side * hw
        nx = side
        c_idx = len(verts)
        verts.append((x_cap, cy, cz))
        norms.append((nx, 0.0, 0.0))
        uvs.append((0.5, 0.5))
        for i in range(segs):
            a0 = 2.0 * math.pi * i / segs
            a1 = 2.0 * math.pi * (i + 1) / segs
            y0, z0 = math.sin(a0), math.cos(a0)
            y1, z1 = math.sin(a1), math.cos(a1)
            b = len(verts)
            verts.append((x_cap, cy + rim_r * y0, cz + rim_r * z0))
            verts.append((x_cap, cy + rim_r * y1, cz + rim_r * z1))
            verts.append((x_cap, cy + radius * y0, cz + radius * z0))
            verts.append((x_cap, cy + radius * y1, cz + radius * z1))
            for _ in range(4):
                norms.append((nx, 0.0, 0.0))
                uvs.append((0.5, 0.5))
            if side > 0:
                tris.append((c_idx, b, b + 1, 0, 1))
                tris.append((b, b + 2, b + 3, 0, 0))
                tris.append((b, b + 3, b + 1, 0, 0))
            else:
                tris.append((c_idx, b + 1, b, 0, 1))
                tris.append((b + 1, b + 3, b + 2, 0, 0))
                tris.append((b + 1, b + 2, b, 0, 0))

    return DecodedMesh(vertices=verts, normals=norms, uvs=uvs, triangles=tris)


def _merge_meshes(meshes: Sequence[DecodedMesh]) -> DecodedMesh:
    verts: List[Tuple[float, float, float]] = []
    norms: List[Tuple[float, float, float]] = []
    uvs: List[Tuple[float, float]] = []
    tris: List[Tuple[int, int, int, int, int]] = []
    for m in meshes:
        off = len(verts)
        verts.extend(m.vertices)
        norms.extend(m.normals)
        uvs.extend(m.uvs)
        for i0, i1, i2, tx, lm in m.triangles:
            tris.append((i0 + off, i1 + off, i2 + off, tx, lm))
    return DecodedMesh(vertices=verts, normals=norms, uvs=uvs, triangles=tris)


def export_synthetic_scene(out_dir: Path) -> Dict[str, Any]:
    """Generate a sleek multi-part procedural sports car Y-up PBR scene for CI/offline tests."""
    meshes_dir = out_dir / "meshes"
    meshes_dir.mkdir(parents=True, exist_ok=True)
    write_mtl_library(meshes_dir / "materials.mtl")

    # Body: lower chassis + flared fenders + hood + cabin deck (mat 0=car_paint_hero, 1=matte_trim, 2=chrome_trim)
    body = _merge_meshes(
        [
            _make_box_mesh(0.0, 0.44, 0.05, 1.84, 0.48, 4.35, mat_idx=0),
            _make_box_mesh(0.0, 0.20, 2.24, 1.86, 0.14, 0.22, mat_idx=1),  # front splitter
            _make_box_mesh(0.0, 0.22, -2.14, 1.80, 0.16, 0.16, mat_idx=1),  # rear diffuser
            _make_box_mesh(0.0, 0.92, -0.18, 1.42, 0.48, 1.85, mat_idx=0),  # roof/pillars
            _make_box_mesh(0.0, 0.56, 2.24, 0.72, 0.14, 0.06, mat_idx=2),  # kidney grille chrome
        ]
    )
    glass = _make_box_mesh(0.0, 0.93, -0.16, 1.44, 0.36, 1.72, mat_idx=0)
    headlights = _merge_meshes(
        [
            _make_box_mesh(-0.66, 0.52, 2.23, 0.32, 0.12, 0.06, mat_idx=0),
            _make_box_mesh(+0.66, 0.52, 2.23, 0.32, 0.12, 0.06, mat_idx=0),
        ]
    )
    taillights = _merge_meshes(
        [
            _make_box_mesh(-0.66, 0.64, -2.13, 0.38, 0.12, 0.06, mat_idx=0),
            _make_box_mesh(+0.66, 0.64, -2.13, 0.38, 0.12, 0.06, mat_idx=0),
        ]
    )
    spoiler = _merge_meshes(
        [
            _make_box_mesh(0.0, 1.06, -1.96, 1.54, 0.05, 0.28, mat_idx=0),  # carbon wing blade
            _make_box_mesh(-0.48, 0.86, -1.92, 0.04, 0.38, 0.14, mat_idx=1),
            _make_box_mesh(+0.48, 0.86, -1.92, 0.04, 0.38, 0.14, mat_idx=1),
        ]
    )

    body_info = SolidPartInfo(
        index=0, name="SYNTH_BODY", name_hash="0x00000001", lod="B", version=1, flags=0,
        header_polys=0, num_bones=0, num_textures=0, num_light_materials=3, num_markers=0,
        aabb_min=[-2.15, -0.93, 0.14], aabb_max=[2.26, 0.93, 1.16], volume=1.0, density=1.0,
        texture_hashes=[], light_material_hashes=["0xd6d6080a", "0x12c9453c", "0x54949afd"],
        plat_version=0, strip_flags=0, strip_count=0, strip_data_bytes=0,
        strip_vertex_total=len(body.vertices), strip_poly_total=len(body.triangles), chunk_offset=0,
    )
    glass_info = dataclasses.replace(body_info, name="SYNTH_WINDOW", light_material_hashes=["0x471a1dca"])
    hl_info = dataclasses.replace(body_info, name="SYNTH_HEADLIGHT", light_material_hashes=["0xb51b3acf"])
    tl_info = dataclasses.replace(body_info, name="SYNTH_BRAKELIGHT", light_material_hashes=["0x05bc3a3c"])
    sp_info = dataclasses.replace(body_info, name="SPOILER_STYLE01_B", light_material_hashes=["0xb57ab98e", "0x2e65e067"])
    wh_info = dataclasses.replace(body_info, name="ENKEI_STYLE01_17_25_B", light_material_hashes=["0x78743ca1", "0x22719fa9"])

    scene_parts: List[Dict[str, Any]] = []
    total_verts = 0
    total_tris = 0

    def _emit(name: str, m: DecodedMesh, info: SolidPartInfo, role: str, transparent: bool = False) -> None:
        nonlocal total_verts, total_tris
        _, mc = write_pbr_obj_mesh(meshes_dir / f"{name}.obj", name, m, info, BUILTIN_LIGHT_MATERIAL_NAMES)
        total_verts += len(m.vertices)
        total_tris += len(m.triangles)
        scene_parts.append(
            {
                "name": name,
                "obj": f"meshes/{name}.obj",
                "role": role,
                "transparent": transparent,
                "visible": True,
                "vertex_count": len(m.vertices),
                "triangle_count": len(m.triangles),
                "materials": mc,
            }
        )

    _emit("SYNTH_CAR_BODY_B", body, body_info, "body")
    _emit("SYNTH_CAR_WINDOW_B", glass, glass_info, "glass", transparent=True)
    _emit("SYNTH_CAR_HEADLIGHT_B", headlights, hl_info, "lights")
    _emit("SYNTH_CAR_BRAKELIGHT_B", taillights, tl_info, "lights")
    _emit("SPOILER_STYLE01_B", spoiler, sp_info, "spoiler")

    wheel_radius = 0.34
    for wname, (wx, wz) in [
        ("WHEEL_FL", (-0.82, 1.38)),
        ("WHEEL_FR", (+0.82, 1.38)),
        ("WHEEL_RL", (-0.82, -1.34)),
        ("WHEEL_RR", (+0.82, -1.34)),
    ]:
        wm = _make_cylinder_wheel(wx, wheel_radius, wz, wheel_radius, 0.25)
        _emit(f"ENKEI_STYLE01_17_25_B_{wname}", wm, wh_info, "wheel")

    return _build_scene_manifest(
        mode="synthetic",
        car_name="Rockport 2026 Synthetic GT Coupe",
        parts=scene_parts,
        total_verts=total_verts,
        total_tris=total_tris,
        anchors={"front_x": 1.38, "rear_x": -1.34, "arch_top_z": 0.60, "half_width": 0.93, "length": 4.41},
        wheel_z_ps2=wheel_radius,
        ground_z_ps2=0.0,
        spoiler_visible=True,
        has_builtin_wing=False,
    )


def _build_scene_manifest(
    mode: str,
    car_name: str,
    parts: List[Dict[str, Any]],
    total_verts: int,
    total_tris: int,
    anchors: Dict[str, float],
    wheel_z_ps2: float,
    ground_z_ps2: float,
    spoiler_visible: bool,
    has_builtin_wing: bool,
) -> Dict[str, Any]:
    """Assemble the complete scene.json payload consumed by RockportMetalPBR."""
    asphalt_tex = REPO_ROOT / "build" / "cc0_baked" / "asphalt_02_wet_4k.png"
    carbon_tex = REPO_ROOT / "build" / "cc0_baked" / "carbon_fiber_4k.png"
    return {
        "schema_version": 1,
        "coordinate_system": "yup_right_handed_meters",
        "mode": mode,
        "car_name": car_name,
        "summary": {
            "part_count": len(parts),
            "total_vertices": total_verts,
            "total_triangles": total_tris,
            "spoiler_visible": spoiler_visible,
            "body_has_builtin_wing": has_builtin_wing,
            "wheel_arch_anchors_ps2": {k: round(v, 4) for k, v in anchors.items()},
            "wheel_center_z_ps2": round(wheel_z_ps2, 4),
            "ground_z_ps2": round(ground_z_ps2, 4),
        },
        "camera": {
            "target": [0.0, 0.54, 0.14],
            "distance": 4.95,
            "azimuth_deg": 40.0,
            "elevation_deg": 9.5,
            "fov_deg": 38.0,
        },
        "textures": {
            "asphalt_wet": str(asphalt_tex.relative_to(REPO_ROOT)) if asphalt_tex.is_file() else "",
            "carbon_fiber": str(carbon_tex.relative_to(REPO_ROOT)) if carbon_tex.is_file() else "",
        },
        "materials": PBR_MATERIALS,
        "lighting_presets": load_lighting_presets(REPO_ROOT),
        "parts": parts,
    }


def resolve_iso_path(cli_iso: Optional[str]) -> Path:
    if cli_iso:
        return Path(cli_iso).expanduser()
    env_iso = os.environ.get("NFSMW_ISO", "").strip()
    if env_iso:
        return Path(env_iso).expanduser()
    return DEFAULT_SOURCE_ISO


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Export Y-up PBR showcase scene & lighting JSON for Native Metal.")
    parser.add_argument("--iso", type=str, default=None, help="Path to untouched PS2 NFSMW ISO (or set NFSMW_ISO).")
    parser.add_argument(
        "--out",
        type=str,
        default="build/metal_wave3/scene",
        help="Output scene directory (default: build/metal_wave3/scene).",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Generate a synthetic procedural GT coupe scene without requiring an ISO.",
    )
    parser.add_argument(
        "--spoiler",
        choices=("auto", "force", "off"),
        default="auto",
        help="Spoiler visibility mode (auto hides external SPOILER_STYLE01_B when body integrates GT wing).",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = REPO_ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.synthetic:
        manifest = export_synthetic_scene(out_dir)
    else:
        iso_path = resolve_iso_path(args.iso)
        manifest = export_iso_scene(iso_path, out_dir, spoiler_mode=args.spoiler)

    scene_json_path = out_dir / "scene.json"
    scene_json_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        f"Exported {manifest['mode']} scene -> {scene_json_path} "
        f"({manifest['summary']['part_count']} parts, "
        f"{manifest['summary']['total_vertices']} verts, "
        f"{manifest['summary']['total_triangles']} tris)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
