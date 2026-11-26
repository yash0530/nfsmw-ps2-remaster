#!/usr/bin/env python3
"""v2.4 verifier: measured authentic VLT handling/pursuit + .ps2 save transferability (SLUS-21351).

Subcommands (all read-only against ISOs; opened 'rb'):
  audit-vlt          Authenticity budget audit of every patches/vlt/*.json handling & pursuit delta.
  verify-vlt         Dry-run a recipe against the untouched retail GLOBAL\\ATTRIBUTES.BIN and prove
                     only the declared 4-byte fields change.
  compare-telemetry  Compare two PINE autopilot JSONL logs (before/after): speed, steering stability,
                     pursuit/heat (when logged) and traffic-density proxy.
  verify-saves       Prove SYSTEM.CNF disc identity + ELF memory-card save identity (BASLUS-21351) are
                     byte-identical between the retail ISO and a modded ISO (or a profile's ELF patches
                     applied in memory), then round-trip a synthetic sandbox .ps2 card.
  all                audit-vlt + verify-vlt + verify-saves, writing a JSON report.

Never touches real memory cards, global PCSX2 settings, or the source ISO, and never launches PCSX2.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tools"))

from iso_inspect import SECTOR_SIZE, compute_pcsx2_elf_crc, parse_iso9660  # noqa: E402

SERIAL = "SLUS-21351"
ELF_NAME = "SLUS_213.51"
SAVE_DIR_PREFIX = "BA" + SERIAL  # PS2 NTSC-U save folder convention ("BA" + serial)
MEASURED_RECIPE = REPO_ROOT / "patches" / "vlt" / "authentic_measured_v24.json"
MEASURED_PROFILE = REPO_ROOT / "patches" / "profiles" / "rockport2026_authentic_vlt.json"
DEFAULT_BUDGET = {"handling_max_rel_delta": 0.05, "pursuit_max_rel_delta": 0.10}
HANDLING_CLASSES = {"tires", "brakes", "chassis", "world", "nos", "engine", "transmission", "induction", "rigidbodyspecs"}
PURSUIT_CLASSES = {"pursuitlevels", "pursuitsupport", "pursuitescalation", "trafficpattern", "aivehicle", "copspawn"}

# Literal strings the retail ELF uses to build/identify its memory-card saves (verified in SLUS_213.51):
# region-ID "SLUS_21351", the "BA" NTSC-U prefix that follows the per-region table, the MC_SaveInfo tag,
# the save icon file name, the "PS2D" icon.sys magic and the "NFS MW%s" save-title format.
SAVE_ANCHORS: Dict[str, bytes] = {
    "region_id": b"\x00SLUS_21351\x00",
    "us_region_prefix": b"\x00BA\x00\x00\x00\x00\x00\x00MC_SaveInfo\x00",
    "icon_name": b"\x00nfwMW_New.ico\x00",
    "icon_sys_magic": b"\x00PS2D\x00",
    "save_title_fmt": b"\x00NFS MW%s\x00",
}


# ----------------------------------------------------------------------------- VLT authenticity audit
def _scope(cls: str) -> str:
    c = cls.lower()
    return "handling" if c in HANDLING_CLASSES else "pursuit" if c in PURSUIT_CLASSES else "out_of_scope"


def _rel_delta(orig: Any, new: Any) -> Optional[float]:
    if isinstance(orig, (list, dict)) or isinstance(new, (list, dict)) or isinstance(orig, bool):
        return None
    o, n = float(orig), float(new)
    return abs(n - o) if o == 0 else (n - o) / abs(o)


def audit_recipe(recipe: dict, budget: Optional[Dict[str, float]] = None) -> dict:
    """Check every handling/pursuit delta against a relative-change budget (camera/visual = out of scope)."""
    b = dict(DEFAULT_BUDGET, **(budget or recipe.get("authenticity_budget") or {}))
    limits = {"handling": b["handling_max_rel_delta"], "pursuit": b["pursuit_max_rel_delta"]}
    rows, over = [], []
    for p in recipe.get("patches", []):
        scope = _scope(p["class"])
        rd = _rel_delta(p["expected_orig"], p["new_value"])
        if scope == "out_of_scope" or rd is None:
            continue
        key = "/".join(str(x) for x in (p["class"], p["node"], p["field"], p.get("member", p.get("index", ""))))
        row = {"key": key, "scope": scope, "orig": p["expected_orig"], "new": p["new_value"], "rel_delta": round(rd, 4)}
        rows.append(row)
        if abs(rd) > limits[scope] + 1e-9:
            over.append(row)
    over.sort(key=lambda r: -abs(r["rel_delta"]))
    in_scope = len(rows)
    return {
        "recipe_id": recipe.get("id"),
        "in_scope_patches": in_scope,
        "over_budget_count": len(over),
        "max_abs_rel_delta": max((abs(r["rel_delta"]) for r in rows), default=0.0),
        "verdict": "not_applicable" if in_scope == 0 else ("authentic" if not over else "exceeds_budget"),
        "worst_offenders": over[:5],
        "budget": limits,
    }


def audit_vlt_dir(vlt_dir: Path = REPO_ROOT / "patches" / "vlt") -> List[dict]:
    out = []
    for path in sorted(vlt_dir.glob("*.json")):
        rep = audit_recipe(json.loads(path.read_text(encoding="utf-8")), budget=DEFAULT_BUDGET)
        rep["file"] = path.name
        out.append(rep)
    return out


def verify_vlt_against_iso(iso: Path, recipe_path: Path = MEASURED_RECIPE) -> dict:
    """Apply a recipe in memory to the retail ATTRIBUTES.BIN and prove only the declared bytes change."""
    import vlt_ps2

    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    raw, _ = vlt_ps2._load_vpak_source(iso, recipe["target"]["virtual_path"])
    patched, log = vlt_ps2.apply_vlt_recipes(raw, [recipe])
    allowed = set()
    for e in log:
        off = int(e["file_offset"], 16)
        allowed.update(range(off, off + int(e["size"])))
    changed = [i for i in range(len(raw)) if raw[i] != patched[i]] if len(raw) == len(patched) else None
    offsets_match = all(int(e["file_offset"], 16) == int(p["expected_offset"], 16) for e, p in zip(log, recipe["patches"]))
    ok = changed is not None and set(changed) <= allowed and len(log) == len(recipe["patches"]) and offsets_match
    return {
        "recipe_id": recipe.get("id"),
        "attributes_bin_sha256": hashlib.sha256(raw).hexdigest(),
        "patched_sha256": hashlib.sha256(patched).hexdigest(),
        "patches_applied": len(log),
        "declared_offsets_match": offsets_match,
        "changed_bytes": len(changed) if changed is not None else None,
        "only_declared_bytes_changed": ok,
        "changes": [{"target": e["target"], "orig": e["orig_value"], "new": e["new_value"]} for e in log],
        "status": "pass" if ok else "fail",
    }


# ----------------------------------------------------------------------------- PINE telemetry comparison
def load_jsonl(path: Path) -> List[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _pct(vals: List[float], q: float) -> float:
    s = sorted(vals)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


def summarize_telemetry(records: Sequence[dict]) -> dict:
    """Driving-only summary of a run_closed_loop_autopilot JSONL (records outside RACING are skipped)."""
    drive = [r for r in records if r.get("flow_state_name", "RACING") == "RACING" and r.get("player_present", True)]
    speeds = [float(r["speed_mph"]) for r in drive if r.get("speed_mph") is not None]
    steer = [float(r["action"]["steer_cmd"]) for r in drive if isinstance(r.get("action"), dict) and "steer_cmd" in r["action"]]
    herr = [abs(float(r["action"]["heading_error_deg"])) for r in drive if isinstance(r.get("action"), dict) and "heading_error_deg" in r["action"]]
    yaw_rates = []
    for a, b in zip(drive, drive[1:]):
        if a.get("heading_deg") is None or b.get("heading_deg") is None:
            continue
        dt = float(b.get("timestamp", 0)) - float(a.get("timestamp", 0))
        if 0.0 < dt <= 1.0:
            dh = (float(b["heading_deg"]) - float(a["heading_deg"]) + 180.0) % 360.0 - 180.0
            yaw_rates.append(dh / dt)
    heat = [float(r["heat"]) for r in drive if isinstance(r.get("heat"), (int, float))]
    cops = [float(r["cop_count"]) for r in drive if isinstance(r.get("cop_count"), (int, float))]
    obst = [float(r["obstacle_count"]) for r in drive if isinstance(r.get("obstacle_count"), (int, float))]

    def mean(v: List[float]) -> Optional[float]:
        return round(statistics.fmean(v), 4) if v else None

    return {
        "samples": len(drive),
        "mean_speed_mph": mean(speeds),
        "p95_speed_mph": round(_pct(speeds, 0.95), 3) if speeds else None,
        "max_speed_mph": round(max(speeds), 3) if speeds else None,
        "steer_jitter": mean([abs(b - a) for a, b in zip(steer, steer[1:])]),
        "yaw_rate_std_dps": round(statistics.pstdev(yaw_rates), 4) if len(yaw_rates) > 1 else None,
        "mean_abs_heading_error_deg": mean(herr),
        "wrong_way_frac": mean([1.0 if r.get("wrong_way") else 0.0 for r in drive]),
        "stuck_recoveries": int(drive[-1].get("stuck_recoveries", 0)) if drive else 0,
        "heat_max": max(heat) if heat else None,
        "heat_mean": mean(heat),
        "cop_count_mean": mean(cops),
        "traffic_density_proxy": mean(obst),
        "pursuit_metrics_source": "heat/cop_count fields" if (heat or cops) else "not logged (obstacle_count proxy only)",
    }


def compare_telemetry(before: Sequence[dict], after: Sequence[dict], max_stability_regression: float = 0.20,
                      max_speed_drop: float = 0.10, min_samples: int = 20) -> dict:
    """Before/after comparison with regression gates. Autopilot runs are noisy: gates are deliberately loose."""
    b, a = summarize_telemetry(before), summarize_telemetry(after)
    deltas: Dict[str, Optional[float]] = {}
    for k, v in b.items():
        if isinstance(v, (int, float)) and isinstance(a.get(k), (int, float)) and not isinstance(v, bool):
            deltas[k] = round(a[k] - v, 4)
    regressions = []
    if b["samples"] < min_samples or a["samples"] < min_samples:
        regressions.append(f"insufficient RACING samples (before={b['samples']}, after={a['samples']}, need {min_samples})")
    for k in ("yaw_rate_std_dps", "steer_jitter"):
        if b.get(k) and a.get(k) is not None and a[k] > b[k] * (1.0 + max_stability_regression):
            regressions.append(f"{k} worsened {b[k]} -> {a[k]} (>{max_stability_regression:.0%})")
    if b.get("mean_speed_mph") and a.get("mean_speed_mph") is not None and a["mean_speed_mph"] < b["mean_speed_mph"] * (1.0 - max_speed_drop):
        regressions.append(f"mean_speed_mph dropped {b['mean_speed_mph']} -> {a['mean_speed_mph']} (>{max_speed_drop:.0%})")
    if a["stuck_recoveries"] > b["stuck_recoveries"] + 1:
        regressions.append(f"stuck_recoveries rose {b['stuck_recoveries']} -> {a['stuck_recoveries']}")
    return {"before": b, "after": a, "delta": deltas, "regressions": regressions,
            "verdict": "pass" if not regressions else "regressed"}


# ----------------------------------------------------------------------------- Disc + ELF save identity
def parse_system_cnf(raw: bytes) -> dict:
    text = raw.decode("ascii", errors="replace")
    kv = {k.strip().upper(): v.strip() for k, _, v in (ln.partition("=") for ln in text.splitlines()) if _}
    m = re.match(r"cdrom0:\\+([A-Z]{4})_(\d{3})\.(\d{2});1$", kv.get("BOOT2", ""), re.I)
    serial = f"{m.group(1).upper()}-{m.group(2)}{m.group(3)}" if m else None
    return {"boot2": kv.get("BOOT2"), "ver": kv.get("VER"), "vmode": kv.get("VMODE"), "serial": serial,
            "elf_name": f"{m.group(1).upper()}_{m.group(2)}.{m.group(3)}" if m else None}


def extract_save_identity(elf: bytes) -> dict:
    anchors = {name: (elf.find(pat) + 1 if elf.find(pat) >= 0 else None) for name, pat in SAVE_ANCHORS.items()}
    found = {k: v for k, v in anchors.items() if v is not None}
    span = (min(found.values()), max(v + len(SAVE_ANCHORS[k]) - 1 for k, v in found.items())) if found else (0, 0)
    region = SAVE_ANCHORS["region_id"].strip(b"\x00").decode() if anchors["region_id"] is not None else None
    return {
        "anchors": {k: (f"0x{v:08x}" if v is not None else None) for k, v in anchors.items()},
        "all_anchors_present": len(found) == len(SAVE_ANCHORS),
        "save_dir_prefix": ("BA" + region.replace("_", "-")) if region and anchors["us_region_prefix"] else None,
        "span": [f"0x{span[0]:08x}", f"0x{span[1]:08x}"],
        "span_sha256": hashlib.sha256(elf[span[0]:span[1]]).hexdigest(),
    }


def read_disc_identity(iso: Path) -> dict:
    """Read SYSTEM.CNF + boot ELF from an ISO strictly read-only ('rb')."""
    with iso.open("rb") as f:
        _, _, entries = parse_iso9660(f)
        by_path = {e["path"].upper(): e for e in entries if not e["is_dir"]}
        cnf_e = by_path["SYSTEM.CNF"]
        f.seek(cnf_e["lba"] * SECTOR_SIZE)
        cnf_raw = f.read(cnf_e["size"])
        cnf = parse_system_cnf(cnf_raw)
        elf_e = by_path[(cnf["elf_name"] or ELF_NAME).upper()]
        f.seek(elf_e["lba"] * SECTOR_SIZE)
        elf = f.read(elf_e["size"])
    return {"source": "iso", "system_cnf_raw": cnf_raw, "system_cnf": cnf, "elf": elf}


def apply_profile_in_memory(disc: dict, profile_path: Path) -> Tuple[dict, List[dict]]:
    """Model a modded build without writing an ISO: build_modded_iso copies SYSTEM.CNF verbatim and
    only rewrites ELF words named by the profile's ELF patch spec, so apply exactly those in memory."""
    import build_modded_iso

    profile = build_modded_iso.load_build_profile(profile_path)
    ep = profile.get("elf_patches") or {}
    spec = json.loads((REPO_ROOT / ep.get("spec", "patches/elf_patches.json")).read_text(encoding="utf-8"))
    elf = bytearray(disc["elf"])
    applied = build_modded_iso.apply_elf_patches(elf, spec, ep.get("include_ids"), bool(ep.get("include_optional")))
    return dict(disc, source=f"profile:{profile.get('profile_id')}", elf=bytes(elf)), applied


def _identity_summary(disc: dict) -> dict:
    return {"source": disc["source"], "system_cnf": disc["system_cnf"],
            "system_cnf_sha256": hashlib.sha256(disc["system_cnf_raw"]).hexdigest(),
            "elf_pcsx2_crc": f"{compute_pcsx2_elf_crc(disc['elf']):08X}", "save": extract_save_identity(disc["elf"])}


# ----------------------------------------------------------------------------- Synthetic sandbox .ps2 card
# PS2 8 MB card as written by PCSX2 file memcards: 16384 pages x (512 data + 16 spare), 2-page clusters.
PAGE, SPARE, PPC, CLUSTERS, ALLOC_OFF, IFC_CLUSTER = 512, 16, 2, 8192, 41, 8
CLUSTER = PAGE * PPC
SB = struct.Struct("<28s12sHHHHLLLLLL8x128s128sbb")
DIRENT = struct.Struct("<HHL8sLL8sL28s32s416s")
MODE_DIR, MODE_FILE, FAT_EOC, FAT_FREE = 0x8427, 0x8497, 0xFFFFFFFF, 0x7FFFFFFF
TOD = b"\x00\x00\x00\x0c\x1a\x0b\xea\x07"  # 2026-11-26 12:00:00 (sec, min, hour, day, month, year)


def _assert_sandbox_path(path: Path) -> None:
    rp = path.resolve()
    roots = [Path(tempfile.gettempdir()).resolve(), (REPO_ROOT / "build").resolve()]
    if not any(rp == r or r in rp.parents for r in roots):
        raise PermissionError(f"Refusing memcard path outside tempdir/build sandbox: {path.name}")


def _cl_off(abs_cluster: int, page: int = 0) -> int:
    return (abs_cluster * PPC + page) * (PAGE + SPARE)


def _write_cluster(card: bytearray, abs_cluster: int, data: bytes) -> None:
    data = data.ljust(CLUSTER, b"\x00")
    for p in range(PPC):
        card[_cl_off(abs_cluster, p):_cl_off(abs_cluster, p) + PAGE] = data[p * PAGE:(p + 1) * PAGE]


def _read_cluster(card: bytes, abs_cluster: int) -> bytes:
    return b"".join(card[_cl_off(abs_cluster, p):_cl_off(abs_cluster, p) + PAGE] for p in range(PPC))


def _dirent(mode: int, length: int, cluster: int, name: str) -> bytes:
    return DIRENT.pack(mode, 0, length, TOD, cluster, 0, TOD, 0, b"", name.encode("ascii"), b"")


def build_sandbox_memcard(path: Path, save_dir: str, files: Dict[str, bytes]) -> str:
    """Write a minimal formatted 8 MB .ps2 card holding one save directory. Sandbox-only (tempdir/build)."""
    _assert_sandbox_path(path)
    card = bytearray(b"\xff" * (CLUSTERS * PPC * (PAGE + SPARE)))
    fat = [FAT_FREE] * (CLUSTERS - ALLOC_OFF)
    nxt = [0]

    def alloc(data: bytes) -> int:
        n = max(1, math.ceil(len(data) / CLUSTER))
        first = nxt[0]
        for i in range(n):
            c = first + i
            fat[c] = (0x80000000 | (c + 1)) if i < n - 1 else FAT_EOC
            _write_cluster(card, ALLOC_OFF + c, data[i * CLUSTER:(i + 1) * CLUSTER])
        nxt[0] += n
        return first

    root_c = 0
    nxt[0] = 2  # reserve 2 clusters (4 entries) for the root directory
    file_ents = [_dirent(MODE_FILE, len(d), alloc(d), n) for n, d in files.items()]
    save_ents = [_dirent(MODE_DIR, 2 + len(files), 0, "."), _dirent(MODE_DIR, 0, 0, "..")] + file_ents
    save_c = alloc(b"".join(save_ents))
    root = [_dirent(MODE_DIR, 3, root_c, "."), _dirent(MODE_DIR, 0, 0, ".."), _dirent(MODE_DIR, len(save_ents), save_c, save_dir)]
    fat[0], fat[1] = 0x80000001, FAT_EOC
    _write_cluster(card, ALLOC_OFF + 0, root[0] + root[1])
    _write_cluster(card, ALLOC_OFF + 1, root[2])
    fat_raw = b"".join(struct.pack("<L", v) for v in fat)
    n_fat = math.ceil(len(fat_raw) / CLUSTER)
    fat_clusters = [IFC_CLUSTER + 1 + i for i in range(n_fat)]
    for i, c in enumerate(fat_clusters):
        _write_cluster(card, c, fat_raw[i * CLUSTER:(i + 1) * CLUSTER])
    _write_cluster(card, IFC_CLUSTER, b"".join(struct.pack("<L", c) for c in fat_clusters))
    ifc = struct.pack("<32L", IFC_CLUSTER, *([0] * 31))
    sb = SB.pack(b"Sony PS2 Memory Card Format ", b"1.2.0.0\x00\x00\x00\x00\x00", PAGE, PPC, 16, 0xFF00, CLUSTERS,
                 ALLOC_OFF, CLUSTERS - 57, root_c, 1023, 1022, ifc, b"\xff" * 128, 2, 0x52)
    card[0:PAGE] = sb.ljust(PAGE, b"\xff")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(card))
    return hashlib.sha256(card).hexdigest()


def read_memcard_saves(path: Path) -> dict:
    """Parse superblock + FAT + root directory (read-only) and return save dirs with their files."""
    card = path.read_bytes()
    f = SB.unpack_from(card, 0)
    if f[0] != b"Sony PS2 Memory Card Format ":
        raise ValueError("not a PS2 memory card image")
    alloc_off, root_c = f[7], f[9]
    ifc0 = struct.unpack_from("<32L", f[12])[0]
    fat_clusters = [c for c in struct.unpack("<256L", _read_cluster(card, ifc0)) if c]
    fat = list(struct.unpack(f"<{len(fat_clusters) * 256}L", b"".join(_read_cluster(card, c) for c in fat_clusters)))

    def chain(c: int) -> bytes:
        out, seen = [], set()
        while c not in seen and c < len(fat):
            seen.add(c)
            out.append(_read_cluster(card, alloc_off + c))
            if fat[c] == FAT_EOC or not fat[c] & 0x80000000:
                break
            c = fat[c] & 0x7FFFFFFF
        return b"".join(out)

    def entries(c: int) -> List[tuple]:
        raw = chain(c)
        count = DIRENT.unpack_from(raw, 0)[2]
        return [DIRENT.unpack_from(raw, i * DIRENT.size) for i in range(min(count, len(raw) // DIRENT.size))]

    saves = {}
    for e in entries(root_c)[2:]:
        if e[0] & 0x20:
            name = e[9].rstrip(b"\x00").decode("ascii", "replace")
            saves[name] = {s[9].rstrip(b"\x00").decode("ascii", "replace"): chain(s[4])[: s[2]]
                           for s in entries(e[4])[2:] if s[0] & 0x10}
    return {"magic": f[0].decode(), "version": f[1].rstrip(b"\x00").decode(), "page_len": f[2],
            "card_flags": f[15] & 0xFF, "saves": saves}


def _icon_sys(title: str) -> bytes:
    buf = bytearray(964)
    buf[0:4] = b"PS2D"
    t = title.encode("ascii")[:66]
    buf[0xC0:0xC0 + len(t)] = t
    return bytes(buf)


# ----------------------------------------------------------------------------- Save transfer verification
def verify_save_transfer(retail_iso: Path, modded_iso: Optional[Path] = None, profile: Optional[Path] = None,
                         sandbox_dir: Optional[Path] = None) -> dict:
    retail = read_disc_identity(retail_iso)
    if modded_iso is not None:
        modded, applied = read_disc_identity(modded_iso), []
        modded["source"] = "modded_iso"
    else:
        modded, applied = apply_profile_in_memory(retail, profile or MEASURED_PROFILE)
    return compare_disc_identities(retail, modded, applied, sandbox_dir)


def compare_disc_identities(retail: dict, modded: dict, applied_elf_patches: Sequence[dict] = (),
                            sandbox_dir: Optional[Path] = None) -> dict:
    r, m = _identity_summary(retail), _identity_summary(modded)
    lo, hi = (int(x, 16) for x in r["save"]["span"])
    touched = [w["file_offset"] for p in applied_elf_patches for w in p["writes"] if lo <= int(w["file_offset"], 16) < hi]
    checks = {
        "system_cnf_byte_identical": retail["system_cnf_raw"] == modded["system_cnf_raw"],
        "disc_serial_is_slus_21351": r["system_cnf"]["serial"] == SERIAL == m["system_cnf"]["serial"],
        "boot_elf_name_unchanged": r["system_cnf"]["elf_name"] == m["system_cnf"]["elf_name"] == ELF_NAME,
        "save_anchors_present": r["save"]["all_anchors_present"] and m["save"]["all_anchors_present"],
        "save_anchor_offsets_identical": r["save"]["anchors"] == m["save"]["anchors"],
        "save_identity_span_identical": r["save"]["span_sha256"] == m["save"]["span_sha256"],
        "save_dir_prefix_identical": r["save"]["save_dir_prefix"] == m["save"]["save_dir_prefix"] == SAVE_DIR_PREFIX,
        "no_elf_patch_inside_save_span": not touched,
    }
    with tempfile.TemporaryDirectory(prefix="nfsmw_v24_mcd_", dir=str(sandbox_dir) if sandbox_dir else None) as td:
        card_path = Path(td) / "sandbox_Mcd_v24.ps2"
        save_name = r["save"]["save_dir_prefix"] + "SANDBOX" if r["save"]["save_dir_prefix"] else "INVALID"
        payload = b"SYNTHETIC-NFSMW-SAVE-V24" * 64
        sha_written = build_sandbox_memcard(card_path, save_name, {"icon.sys": _icon_sys("NFS MW Sandbox"), "NFSMWSAV": payload})
        card = read_memcard_saves(card_path)
        resolved = {src: sorted(n for n in card["saves"] if s["save"]["save_dir_prefix"] and n.startswith(s["save"]["save_dir_prefix"]))
                    for src, s in (("retail", r), ("modded", m))}
        files = card["saves"].get(save_name, {})
        checks["sandbox_card_header_valid"] = card["magic"] == "Sony PS2 Memory Card Format " and card["page_len"] == PAGE
        checks["sandbox_save_resolved_by_both"] = resolved["retail"] == resolved["modded"] == [save_name]
        checks["sandbox_save_roundtrip_intact"] = files.get("NFSMWSAV") == payload and files.get("icon.sys", b"")[:4] == b"PS2D"
        checks["sandbox_card_unmodified_by_reads"] = hashlib.sha256(card_path.read_bytes()).hexdigest() == sha_written
    return {
        "retail": r, "modded": m, "elf_crc_changed": r["elf_pcsx2_crc"] != m["elf_pcsx2_crc"],
        "applied_elf_patches": [p["id"] for p in applied_elf_patches], "elf_writes_inside_save_span": touched,
        "sandbox_memcard": {"save_dir": save_name, "resolved": resolved, "card_flags": f"0x{card['card_flags']:02X}",
                            "note": "temporary synthetic card (deleted); ECC spare bytes not generated"},
        "checks": checks, "status": "pass" if all(checks.values()) else "fail",
    }


# ----------------------------------------------------------------------------- CLI
def _default_iso() -> Optional[Path]:
    env = os.environ.get("NFSMW_ISO")
    p = Path(env).expanduser() if env else Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"
    return p if p.is_file() else None


def _emit(obj: Any, out: Optional[Path]) -> None:
    text = json.dumps(obj, indent=2, default=lambda b: b.hex() if isinstance(b, bytes) else str(b))
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    print(text)


LIVE_CAPTURE_HINT = (
    "Live A/B telemetry (optional; only when no other PCSX2 is running - check `pgrep -iq pcsx2`):\n"
    "  python3 tools/race_flywheel.py run-race --pine-autopilot --iso <retail.iso> --iter-name v24_before\n"
    "  python3 tools/race_flywheel.py run-race --pine-autopilot --iso build/NFSMW_BlackEdition_Rockport2026_AuthenticVLT.iso --iter-name v24_after\n"
    "  python3 tools/verify_v24_vlt_and_saves.py compare-telemetry <before>/pine_telemetry.jsonl <after>/pine_telemetry.jsonl"
)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("audit-vlt").add_argument("--out", type=Path)
    for name in ("verify-vlt", "verify-saves", "all"):
        sp = sub.add_parser(name)
        sp.add_argument("--iso", type=Path, default=None, help="Untouched retail SLUS-21351 ISO (read-only)")
        sp.add_argument("--recipe", type=Path, default=MEASURED_RECIPE)
        sp.add_argument("--modded-iso", type=Path, default=None, help="Built modded ISO (read-only)")
        sp.add_argument("--profile", type=Path, default=MEASURED_PROFILE, help="Profile modelled in memory if no --modded-iso")
        sp.add_argument("--out", type=Path)
    ct = sub.add_parser("compare-telemetry")
    ct.add_argument("before", type=Path)
    ct.add_argument("after", type=Path)
    ct.add_argument("--out", type=Path)
    args = ap.parse_args(argv)

    if args.cmd == "audit-vlt":
        _emit(audit_vlt_dir(), args.out)
        return 0
    if args.cmd == "compare-telemetry":
        rep = compare_telemetry(load_jsonl(args.before), load_jsonl(args.after))
        _emit(rep, args.out)
        return 0 if rep["verdict"] == "pass" else 1
    iso = args.iso or _default_iso()
    if iso is None or not iso.is_file():
        print("error: retail ISO not found (pass --iso or set NFSMW_ISO)", file=sys.stderr)
        return 2
    report: Dict[str, Any] = {}
    if args.cmd in ("verify-vlt", "all"):
        report["vlt_iso"] = verify_vlt_against_iso(iso, args.recipe)
    if args.cmd == "all":
        report["vlt_audit"] = audit_vlt_dir()
        report["live_telemetry"] = LIVE_CAPTURE_HINT
    if args.cmd in ("verify-saves", "all"):
        report["saves"] = verify_save_transfer(iso, args.modded_iso, args.profile)
    _emit(report, args.out)
    ok = report.get("vlt_iso", {}).get("status", "pass") == "pass" and report.get("saves", {}).get("status", "pass") == "pass"
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
