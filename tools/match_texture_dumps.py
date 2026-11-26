#!/usr/bin/env python3
"""Perceptual dHash/pHash + color-histogram matcher for PCSX2 texture dumps.

Matches unknown PCSX2 texture dumps (``<TexHash>-<ClutHash>.png`` or ``<TexHash>.png``)
against extracted PS2 TPK PNGs from ``tools/tpk_extract.py`` using zero extra
dependencies beyond NumPy, Pillow, and the Python standard library.

Also resolves matched TPK texture names against ``assets_cc0/material_pack_rules.json``
and stages matching 4K CC0 baked surfaces and procedural HUD kit PNGs ready for
``python3 tools/texture_pipeline.py encode-dds --strict-pcsx2-names``.

Subcommands:
  match   Match dump PNGs against extracted PS2 TPK textures + material rules
  deploy  Stage matching replacement PNGs named ``<TexHash>-<ClutHash>.png``

Usage:
  python3 tools/match_texture_dumps.py match \\
    --dumps path/to/pcsx2/dumps \\
    --tpk-dir build/tpk_extracted \\
    --rules assets_cc0/material_pack_rules.json \\
    --out build/matched_hashes.json

  python3 tools/match_texture_dumps.py deploy \\
    --matches build/matched_hashes.json \\
    --hud-dir build/hud_pack \\
    --baked-dir build/cc0_baked \\
    --out build/staged_replacements
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import fnmatch
from functools import lru_cache
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

PCSX2_HASH_RE = re.compile(
    r"^(?P<tex>[0-9a-fA-F]{8,16})(?:-(?P<part2>[0-9a-fA-F]{8,16}))?(?:-(?P<flags>[0-9a-fA-F]{8}))?$"
)

# Byte popcount lookup table for vectorized 64-bit Hamming distance without
# requiring external C extensions.
_POPCOUNT_LUT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


@dataclass
class TextureSignature:
    """Compact perceptual and color signature for a 2D RGBA image."""

    width: int
    height: int
    dhash: int
    phash: int
    color_hist: np.ndarray  # shape=(32,), unit L2 norm float32
    alpha_mean: float
    alpha_nonzero_ratio: float


def _effective_luma_image(img: Image.Image) -> Image.Image:
    """Build grayscale perceptual image combining premultiplied luma + alpha.

    Pure-alpha PS2 HUD masks (where RGB is nearly uniform black/white and
    the silhouette is carried in Alpha) retain strong edge gradients cleanly
    alongside opaque diffuse world textures.
    """
    rgba = np.asarray(img.convert("RGBA"), dtype=np.float32)
    r = rgba[:, :, 0]
    g = rgba[:, :, 1]
    b = rgba[:, :, 2]
    a = rgba[:, :, 3]
    a_norm = a / 255.0
    rgb_luma = (0.299 * r + 0.587 * g + 0.114 * b) * a_norm
    eff = np.clip(0.75 * rgb_luma + 0.25 * a, 0.0, 255.0).astype(np.uint8)
    return Image.fromarray(eff)


@lru_cache(maxsize=8)
def _dct_matrix(n: int) -> np.ndarray:
    """Orthonormal 1D Type-II DCT basis matrix of size ``n x n`` (pure NumPy)."""
    k = np.arange(n, dtype=np.float64)[:, None]
    i = np.arange(n, dtype=np.float64)[None, :]
    mat = np.cos(np.pi * k * (2.0 * i + 1.0) / (2.0 * n))
    mat[0, :] *= np.sqrt(1.0 / n)
    mat[1:, :] *= np.sqrt(2.0 / n)
    return mat


def compute_dhash(img: Image.Image, hash_size: int = 8) -> int:
    """Compute a 64-bit horizontal difference perceptual hash (dHash)."""
    gray = _effective_luma_image(img).resize(
        (hash_size + 1, hash_size), Image.Resampling.LANCZOS
    )
    arr = np.asarray(gray, dtype=np.int16)
    bits = (arr[:, 1:] > arr[:, :-1]).reshape(-1)
    val = 0
    for bit in bits:
        val = (val << 1) | int(bool(bit))
    return val


def compute_phash(img: Image.Image, hash_size: int = 8, highfreq_factor: int = 4) -> int:
    """Compute a 64-bit 2D-DCT perceptual hash (pHash) without SciPy."""
    img_size = hash_size * highfreq_factor
    gray = _effective_luma_image(img).resize((img_size, img_size), Image.Resampling.LANCZOS)
    pixels = np.asarray(gray, dtype=np.float64)
    dct = _dct_matrix(img_size)
    coeff = dct @ pixels @ dct.T
    low_freq = coeff[:hash_size, :hash_size].copy()
    # Exclude DC term when computing median so uniform brightness shifts cancel out.
    ac_flat = low_freq.reshape(-1)[1:]
    median = float(np.median(ac_flat)) if len(ac_flat) > 0 else 0.0
    bits = (low_freq > median).reshape(-1)
    val = 0
    for bit in bits:
        val = (val << 1) | int(bool(bit))
    return val


def compute_color_hist(img: Image.Image, bins: int = 8) -> np.ndarray:
    """Compute normalized 32-bin RGBA marginal histogram (L2 unit norm float32)."""
    rgba = np.asarray(img.convert("RGBA"), dtype=np.uint8)
    parts = []
    for ch in range(4):
        hist, _ = np.histogram(rgba[:, :, ch], bins=bins, range=(0, 256))
        parts.append(hist.astype(np.float32))
    vec = np.concatenate(parts, axis=0)
    norm = float(np.linalg.norm(vec))
    if norm > 0.0:
        vec /= norm
    return vec


def compute_signature(img: Image.Image) -> TextureSignature:
    """Compute full perceptual + histogram signature for an RGBA image."""
    rgba_img = img.convert("RGBA")
    alpha = np.asarray(rgba_img, dtype=np.uint8)[:, :, 3]
    return TextureSignature(
        width=rgba_img.width,
        height=rgba_img.height,
        dhash=compute_dhash(rgba_img),
        phash=compute_phash(rgba_img),
        color_hist=compute_color_hist(rgba_img),
        alpha_mean=float(alpha.mean() / 255.0),
        alpha_nonzero_ratio=float((alpha > 16).mean()),
    )


def hamming_distance(a: int, b: int) -> int:
    """Compute bit Hamming distance between two 64-bit integer hashes."""
    return bin((a ^ b) & 0xFFFFFFFFFFFFFFFF).count("1")


def parse_pcsx2_stem_full(stem: str) -> Tuple[Optional[str], Optional[str], Optional[str], bool]:
    """Parse PCSX2 v2.x texture stems into ``(tex_hash, clut_hash, flags_hex, is_valid)``.

    Supports all PCSX2 dump/replacement stem forms:
      - 3-part PCSX2 v2.6.3 paletted: ``<TexHash>-<ClutHash>-<Flags>`` (e.g. 14..16 hex - 14..16 hex - 8 hex)
      - 2-part PCSX2 v2.6.3 direct RGBA (no CLUT): ``<TexHash>-<Flags>`` (12..16 hex - 8 hex)
      - 2-part legacy paletted: ``<TexHash>-<ClutHash>``
      - 1-part legacy direct: ``<TexHash>``
    """
    m = PCSX2_HASH_RE.match(stem.strip())
    if not m:
        return None, None, None, False
    tex = m.group("tex").lower()
    part2 = m.group("part2").lower() if m.group("part2") else None
    flags = m.group("flags").lower() if m.group("flags") else None
    if flags is not None:
        return tex, part2, flags, True
    if part2 is not None:
        if len(tex) >= 12 and len(part2) == 8:
            return tex, None, part2, True
        return tex, part2, None, True
    return tex, None, None, True


def parse_pcsx2_stem(stem: str) -> Tuple[Optional[str], Optional[str], bool]:
    """Parse PCSX2 stem into ``(tex_hash, clut_hash, is_valid)`` (backwards-compatible)."""
    tex, clut, _flags, valid = parse_pcsx2_stem_full(stem)
    return tex, clut, valid


def load_rules(rules_path: Optional[Path]) -> Dict[str, Any]:
    """Load material pack rules JSON cleanly (returns empty rule set if missing)."""
    if rules_path is None or not rules_path.is_file():
        return {"default_theme": "motorsport2026", "matching_defaults": {}, "rules": []}
    data = json.loads(rules_path.read_text(encoding="utf-8"))
    rules = list(data.get("rules", []))
    rules.sort(key=lambda r: int(r.get("priority", 0)), reverse=True)
    data["rules"] = rules
    return data


def match_material_rule(
    tex_name: str,
    rules_cfg: Dict[str, Any],
    theme: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve the highest-priority material rule matching ``tex_name``."""
    upper_name = tex_name.strip().upper()
    if not upper_name:
        return None
    active_theme = theme or rules_cfg.get("default_theme", "motorsport2026")
    for rule in rules_cfg.get("rules", []):
        patterns = rule.get("name_patterns", [])
        for pat in patterns:
            if fnmatch.fnmatchcase(upper_name, str(pat).strip().upper()):
                asset_rel = str(rule.get("asset", "")).replace("{theme}", active_theme)
                return {
                    "rule_id": rule["id"],
                    "category": rule.get("category", "unknown"),
                    "source_type": rule.get("source_type", "cc0_baked"),
                    "asset": asset_rel,
                    "preserve_aspect": bool(rule.get("preserve_aspect", False)),
                    "pot_align": bool(rule.get("pot_align", True)),
                    "matched_pattern": pat,
                }
    return None


def load_tpk_database(tpk_dir: Path) -> List[Dict[str, Any]]:
    """Load extracted TPK entries + compute perceptual signatures from ``tpk_dir``.

    Reads ``index.json`` / nested ``manifest.json`` when present, and falls back to
    discovering all ``*.png`` files recursively so standalone test fixtures also work.
    """
    records_by_path: Dict[Path, Dict[str, Any]] = {}

    index_path = tpk_dir / "index.json"
    if index_path.is_file():
        idx_data = json.loads(index_path.read_text(encoding="utf-8"))
        for item in idx_data.get("textures", []):
            p_raw = item.get("png_path")
            if p_raw:
                p = Path(p_raw)
                if not p.is_absolute() and not p.is_file():
                    p = tpk_dir / p_raw
                if p.is_file():
                    records_by_path[p.resolve()] = dict(item)

    for manifest in sorted(tpk_dir.glob("*/manifest.json")):
        mdata = json.loads(manifest.read_text(encoding="utf-8"))
        for item in mdata.get("textures", []):
            p_raw = item.get("png_path")
            p = Path(p_raw) if p_raw else None
            if p is not None and not p.is_file():
                candidate = manifest.parent / p.name
                if candidate.is_file():
                    p = candidate
            if p is not None and p.is_file():
                records_by_path.setdefault(p.resolve(), dict(item))

    # Fallback / supplemental scan for unindexed PNGs inside tpk_dir
    for png in sorted(tpk_dir.rglob("*.png")):
        resolved = png.resolve()
        if resolved not in records_by_path:
            stem = png.stem
            # Strip leading index prefix `002_` and trailing hash `_a1b2c3d4` if present
            cleaned = re.sub(r"^\d+_", "", stem)
            cleaned = re.sub(r"_[0-9a-fA-F]{8}$", "", cleaned)
            records_by_path[resolved] = {
                "archive": png.parent.name,
                "name": cleaned or stem,
                "name_hash": None,
                "width": None,
                "height": None,
                "ps2_psm_name": None,
                "png_path": str(png),
            }

    entries: List[Dict[str, Any]] = []
    for resolved_path, meta in records_by_path.items():
        with Image.open(resolved_path) as im:
            sig = compute_signature(im)
        entries.append(
            {
                "archive": meta.get("archive", resolved_path.parent.name),
                "name": meta.get("name", resolved_path.stem),
                "name_hash": meta.get("name_hash"),
                "ps2_psm_name": meta.get("ps2_psm_name"),
                "png_path": str(resolved_path),
                "sig": sig,
            }
        )
    return entries


def _vector_hamming_distances(query_hash: int, db_hashes_u64: np.ndarray) -> np.ndarray:
    """Fast NumPy vectorized 64-bit Hamming distance against ``db_hashes_u64``."""
    xor_arr = np.bitwise_xor(db_hashes_u64, np.uint64(query_hash & 0xFFFFFFFFFFFFFFFF))
    bytes_view = xor_arr.view(np.uint8).reshape(-1, 8)
    return _POPCOUNT_LUT[bytes_view].sum(axis=1).astype(np.int32)


def score_dump_against_db(
    dump_sig: TextureSignature,
    db_entries: Sequence[Dict[str, Any]],
    db_dhashes: np.ndarray,
    db_phashes: np.ndarray,
    db_hists: np.ndarray,
    weights: Optional[Dict[str, float]] = None,
    ignore_aspect: bool = False,
    top_k: int = 3,
) -> List[Dict[str, Any]]:
    """Rank extracted TPK entries against a single dump texture signature."""
    if not db_entries:
        return []

    w_cfg = weights or {}
    w_d = float(w_cfg.get("dhash", 0.45))
    w_p = float(w_cfg.get("phash", 0.35))
    w_c = float(w_cfg.get("color_hist", 0.20))
    w_total = max(1e-6, w_d + w_p + w_c)
    w_d, w_p, w_c = w_d / w_total, w_p / w_total, w_c / w_total

    d_dists = _vector_hamming_distances(dump_sig.dhash, db_dhashes)
    p_dists = _vector_hamming_distances(dump_sig.phash, db_phashes)
    d_sims = 1.0 - (d_dists.astype(np.float64) / 64.0)
    p_sims = 1.0 - (p_dists.astype(np.float64) / 64.0)
    hist_sims = np.clip(db_hists @ dump_sig.color_hist, 0.0, 1.0).astype(np.float64)

    scores = w_d * d_sims + w_p * p_sims + w_c * hist_sims

    if not ignore_aspect:
        dump_ar = dump_sig.width / max(1.0, float(dump_sig.height))
        tpk_ws = np.array([e["sig"].width for e in db_entries], dtype=np.float64)
        tpk_hs = np.array([e["sig"].height for e in db_entries], dtype=np.float64)
        tpk_ars = tpk_ws / np.maximum(1.0, tpk_hs)
        ratios = np.maximum(dump_ar / tpk_ars, tpk_ars / dump_ar)
        ar_penalty = np.clip((ratios - 1.25) * 0.12, 0.0, 0.25)
        scores = np.clip(scores - ar_penalty, 0.0, 1.0)

    k = min(max(1, top_k), len(db_entries))
    top_indices = np.argsort(-scores)[:k]

    results: List[Dict[str, Any]] = []
    for idx in top_indices:
        entry = db_entries[int(idx)]
        tsig: TextureSignature = entry["sig"]
        results.append(
            {
                "tpk_name": entry["name"],
                "tpk_archive": entry["archive"],
                "tpk_hash": entry["name_hash"],
                "tpk_psm": entry["ps2_psm_name"],
                "tpk_size": [tsig.width, tsig.height],
                "tpk_png": entry["png_path"],
                "score": round(float(scores[int(idx)]), 4),
                "dhash_hamming": int(d_dists[int(idx)]),
                "phash_hamming": int(p_dists[int(idx)]),
                "hist_similarity": round(float(hist_sims[int(idx)]), 4),
            }
        )
    return results


def match_dumps_directory(
    dumps_dir: Path,
    tpk_dir: Path,
    rules_path: Optional[Path] = None,
    out_path: Optional[Path] = None,
    min_score: Optional[float] = None,
    max_hamming: Optional[int] = None,
    theme: Optional[str] = None,
    ignore_aspect: bool = False,
    top_k: int = 3,
) -> Dict[str, Any]:
    """Match all PNG dumps under ``dumps_dir`` against ``tpk_dir`` + material rules."""
    rules_cfg = load_rules(rules_path)
    defaults = rules_cfg.get("matching_defaults", {})
    eff_min_score = float(min_score if min_score is not None else defaults.get("min_score", 0.72))
    eff_max_hamming = int(
        max_hamming if max_hamming is not None else defaults.get("max_dhash_hamming", 18)
    )
    weights = defaults.get("weights", {"dhash": 0.45, "phash": 0.35, "color_hist": 0.20})
    active_theme = theme or rules_cfg.get("default_theme", "motorsport2026")

    db_entries = load_tpk_database(tpk_dir)
    if db_entries:
        db_dhashes = np.array([e["sig"].dhash & 0xFFFFFFFFFFFFFFFF for e in db_entries], dtype=np.uint64)
        db_phashes = np.array([e["sig"].phash & 0xFFFFFFFFFFFFFFFF for e in db_entries], dtype=np.uint64)
        db_hists = np.stack([e["sig"].color_hist for e in db_entries], axis=0).astype(np.float32)
    else:
        db_dhashes = np.zeros((0,), dtype=np.uint64)
        db_phashes = np.zeros((0,), dtype=np.uint64)
        db_hists = np.zeros((0, 32), dtype=np.float32)

    dump_files = sorted(dumps_dir.rglob("*.png")) if dumps_dir.exists() else []
    matches: List[Dict[str, Any]] = []
    matched_count = 0
    rule_hit_count = 0

    for dpath in dump_files:
        tex_hash, clut_hash, flags_hex, valid_pcsx2 = parse_pcsx2_stem_full(dpath.stem)
        with Image.open(dpath) as im:
            dsig = compute_signature(im)

        candidates = score_dump_against_db(
            dsig,
            db_entries,
            db_dhashes,
            db_phashes,
            db_hists,
            weights=weights,
            ignore_aspect=ignore_aspect,
            top_k=top_k,
        )
        best = candidates[0] if candidates else None
        is_match = (
            best is not None
            and best["score"] >= eff_min_score
            and best["dhash_hamming"] <= eff_max_hamming
        )
        resolved_rule = None
        if is_match and best is not None:
            matched_count += 1
            resolved_rule = match_material_rule(best["tpk_name"], rules_cfg, theme=active_theme)
            if resolved_rule is not None:
                rule_hit_count += 1

        matches.append(
            {
                "dump_path": str(dpath.resolve()),
                "dump_name": dpath.name,
                "pcsx2_stem": dpath.stem.lower() if valid_pcsx2 else None,
                "tex_hash": tex_hash,
                "clut_hash": clut_hash,
                "flags": flags_hex,
                "is_valid_pcsx2_name": valid_pcsx2,
                "dump_size": [dsig.width, dsig.height],
                "dump_dhash": f"{dsig.dhash:016x}",
                "dump_phash": f"{dsig.phash:016x}",
                "matched": is_match,
                "best_match": best,
                "rule": resolved_rule,
                "top_candidates": candidates,
            }
        )

    report = {
        "schema_version": "1.0",
        "dumps_dir": str(dumps_dir),
        "tpk_dir": str(tpk_dir),
        "rules_path": str(rules_path) if rules_path else None,
        "theme": active_theme,
        "tpk_database_size": len(db_entries),
        "dump_count": len(dump_files),
        "matched_count": matched_count,
        "rule_matched_count": rule_hit_count,
        "thresholds": {
            "min_score": eff_min_score,
            "max_hamming": eff_max_hamming,
        },
        "matches": matches,
    }

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    return report


def _round_div4(val: int, min_val: int = 4, max_val: int = 4096) -> int:
    clamped = max(min_val, min(max_val, val))
    return max(4, (clamped + 3) // 4 * 4)


def _stage_replacement_image(
    src_path: Path,
    dump_size: Tuple[int, int],
    source_type: str,
    preserve_aspect: bool,
    max_size: int = 4096,
) -> Image.Image:
    """Prepare a replacement PNG matching PCSX2 aspect-ratio & BC7 mod-4 rules."""
    dump_w, dump_h = max(4, int(dump_size[0])), max(4, int(dump_size[1]))
    target_ar = dump_w / dump_h

    with Image.open(src_path) as im_raw:
        src = im_raw.convert("RGBA")

    src_w, src_h = src.size
    src_ar = src_w / max(1, src_h)
    ar_diff = abs(src_ar - target_ar) / max(src_ar, target_ar)

    # Exact aspect or near-identical aspect ratio: keep native high-res up to max_size.
    if ar_diff <= 0.05 or preserve_aspect:
        if max(src_w, src_h) <= max_size and (src_w % 4 == 0) and (src_h % 4 == 0):
            return src
        scale = min(1.0, max_size / max(src_w, src_h))
        tw = _round_div4(int(round(src_w * scale)), max_val=max_size)
        th = _round_div4(int(round(src_h * scale)), max_val=max_size)
        return src.resize((tw, th), Image.Resampling.LANCZOS)

    # For tileable CC0 baked materials, center-crop to exact dump aspect ratio
    # so asphalt / concrete / carbon grain keeps true isotropic texel scale.
    if source_type == "cc0_baked":
        if target_ar >= src_ar:
            crop_w = src_w
            crop_h = max(4, int(round(src_w / target_ar)))
        else:
            crop_h = src_h
            crop_w = max(4, int(round(src_h * target_ar)))
        left = max(0, (src_w - crop_w) // 2)
        top = max(0, (src_h - crop_h) // 2)
        cropped = src.crop((left, top, left + crop_w, top + crop_h))
        tw = _round_div4(min(crop_w, max_size), max_val=max_size)
        th = _round_div4(int(round(tw / target_ar)), max_val=max_size)
        if cropped.size != (tw, th):
            return cropped.resize((tw, th), Image.Resampling.LANCZOS)
        return cropped

    # For HUD sprites with different container aspect ratio, scale cleanly to match dump aspect.
    long_edge = min(max_size, max(src_w, src_h))
    if target_ar >= 1.0:
        tw = _round_div4(long_edge, max_val=max_size)
        th = _round_div4(int(round(tw / target_ar)), max_val=max_size)
    else:
        th = _round_div4(long_edge, max_val=max_size)
        tw = _round_div4(int(round(th * target_ar)), max_val=max_size)
    return src.resize((tw, th), Image.Resampling.LANCZOS)


def deploy_matches(
    matches_path: Path,
    hud_dir: Path,
    baked_dir: Path,
    out_dir: Path,
    max_size: int = 4096,
    allow_non_pcsx2_names: bool = False,
) -> Dict[str, Any]:
    """Stage matched high-res replacement PNGs ready for ``texture_pipeline.py encode-dds``."""
    data = json.loads(matches_path.read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)

    staged: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for item in data.get("matches", []):
        if not item.get("matched") or not item.get("rule"):
            skipped.append({"dump": item.get("dump_name"), "reason": "unmatched_or_no_rule"})
            continue

        valid_pcsx2 = bool(item.get("is_valid_pcsx2_name"))
        if not valid_pcsx2 and not allow_non_pcsx2_names:
            skipped.append({"dump": item.get("dump_name"), "reason": "non_pcsx2_hash_filename"})
            continue

        rule = item["rule"]
        src_type = rule.get("source_type", "cc0_baked")
        base_dir = hud_dir if src_type == "hud_pack" else baked_dir
        src_file = base_dir / str(rule["asset"])
        if not src_file.is_file():
            skipped.append(
                {
                    "dump": item.get("dump_name"),
                    "reason": f"missing_source_asset:{src_file}",
                }
            )
            continue

        stem = item.get("pcsx2_stem") or Path(item["dump_name"]).stem
        dest_file = out_dir / f"{stem}.png"
        dump_size = tuple(item.get("dump_size") or [512, 512])
        staged_img = _stage_replacement_image(
            src_path=src_file,
            dump_size=(int(dump_size[0]), int(dump_size[1])),
            source_type=src_type,
            preserve_aspect=bool(rule.get("preserve_aspect", False)),
            max_size=max_size,
        )
        staged_img.save(dest_file, format="PNG")
        staged.append(
            {
                "pcsx2_stem": stem,
                "staged_png": str(dest_file),
                "staged_size": list(staged_img.size),
                "source_asset": str(src_file),
                "rule_id": rule["rule_id"],
                "category": rule["category"],
                "matched_tpk_name": item["best_match"]["tpk_name"],
                "score": item["best_match"]["score"],
            }
        )

    manifest = {
        "schema_version": "1.0",
        "matches_source": str(matches_path),
        "out_dir": str(out_dir),
        "staged_count": len(staged),
        "skipped_count": len(skipped),
        "staged": staged,
        "skipped": skipped,
    }
    (out_dir / "deploy_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Perceptual dHash/pHash matcher & 4K CC0 replacement stager for PCSX2 texture dumps."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_match = sub.add_parser("match", help="Match PCSX2 dump PNGs against extracted PS2 TPK PNGs.")
    p_match.add_argument("--dumps", type=Path, required=True, help="Directory of PCSX2 dumped PNGs.")
    p_match.add_argument(
        "--tpk-dir",
        type=Path,
        default=Path("build/tpk_extracted"),
        help="Directory of extracted PS2 TPK PNGs/manifests.",
    )
    p_match.add_argument(
        "--rules",
        type=Path,
        default=Path("assets_cc0/material_pack_rules.json"),
        help="Material pack rules JSON.",
    )
    p_match.add_argument(
        "--out",
        type=Path,
        default=Path("build/matched_hashes.json"),
        help="Output matched hashes JSON path.",
    )
    p_match.add_argument("--min-score", type=float, help="Minimum composite similarity score (0..1).")
    p_match.add_argument("--max-hamming", type=int, help="Maximum dHash Hamming distance (0..64).")
    p_match.add_argument("--theme", help="HUD kit theme override (e.g. motorsport2026, rockport_neon).")
    p_match.add_argument("--ignore-aspect", action="store_true", help="Disable aspect-ratio mismatch penalty.")
    p_match.add_argument("--top-k", type=int, default=3, help="Number of top candidates to record per dump.")
    p_match.add_argument("--json", action="store_true", help="Print compact summary JSON to stdout.")

    p_deploy = sub.add_parser("deploy", help="Stage matched replacement PNGs for encode-dds.")
    p_deploy.add_argument(
        "--matches",
        type=Path,
        default=Path("build/matched_hashes.json"),
        help="Input matched_hashes.json from `match`.",
    )
    p_deploy.add_argument("--hud-dir", type=Path, default=Path("build/hud_pack"), help="HUD pack directory.")
    p_deploy.add_argument("--baked-dir", type=Path, default=Path("build/cc0_baked"), help="Baked CC0 directory.")
    p_deploy.add_argument(
        "--out",
        type=Path,
        default=Path("build/staged_replacements"),
        help="Output directory for staged PCSX2-named PNGs.",
    )
    p_deploy.add_argument("--max-size", type=int, default=4096, help="Maximum long edge in pixels (default 4096).")
    p_deploy.add_argument(
        "--allow-non-pcsx2-names",
        action="store_true",
        help="Also stage dumps whose filenames do not match <TexHash>-<ClutHash>.",
    )
    p_deploy.add_argument("--json", action="store_true", help="Print compact summary JSON to stdout.")

    args = parser.parse_args(argv)

    if args.command == "match":
        report = match_dumps_directory(
            dumps_dir=args.dumps,
            tpk_dir=args.tpk_dir,
            rules_path=args.rules,
            out_path=args.out,
            min_score=args.min_score,
            max_hamming=args.max_hamming,
            theme=args.theme,
            ignore_aspect=args.ignore_aspect,
            top_k=args.top_k,
        )
        if args.json:
            print(
                json.dumps(
                    {
                        "dump_count": report["dump_count"],
                        "tpk_database_size": report["tpk_database_size"],
                        "matched_count": report["matched_count"],
                        "rule_matched_count": report["rule_matched_count"],
                        "out": str(args.out),
                    },
                    indent=2,
                )
            )
        else:
            print(
                f"Matched {report['matched_count']}/{report['dump_count']} dumps "
                f"({report['rule_matched_count']} mapped to replacement rules) "
                f"against {report['tpk_database_size']} PS2 TPK textures -> {args.out}"
            )
        return 0

    if args.command == "deploy":
        manifest = deploy_matches(
            matches_path=args.matches,
            hud_dir=args.hud_dir,
            baked_dir=args.baked_dir,
            out_dir=args.out,
            max_size=args.max_size,
            allow_non_pcsx2_names=args.allow_non_pcsx2_names,
        )
        if args.json:
            print(
                json.dumps(
                    {
                        "staged_count": manifest["staged_count"],
                        "skipped_count": manifest["skipped_count"],
                        "out_dir": manifest["out_dir"],
                    },
                    indent=2,
                )
            )
        else:
            print(
                f"Staged {manifest['staged_count']} replacement PNG(s) "
                f"(skipped {manifest['skipped_count']}) -> {args.out}"
            )
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
