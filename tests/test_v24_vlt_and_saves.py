#!/usr/bin/env python3
"""v2.4 tests: measured authentic VLT recipe, PINE telemetry comparison, and .ps2 save transferability.

Offline tests run on Linux + macOS with NFSMW_ISO=/nonexistent (synthetic ELF/disc/memcard fixtures only).
Live tests read the untouched retail SLUS-21351 ISO strictly read-only and skip cleanly when it is absent.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tools"))

import rockport_cli  # noqa: E402
import verify_v24_vlt_and_saves as v24  # noqa: E402

CNF = b"BOOT2 = cdrom0:\\SLUS_213.51;1\r\nVER = 1.01\r\nVMODE = NTSC"


def _find_local_iso() -> Optional[Path]:
    env_iso = os.environ.get("NFSMW_ISO")
    if env_iso is not None:
        p = Path(env_iso).expanduser()
        return p if p.is_file() else None
    p = Path.home() / "Documents" / "PS2Games" / "Need for Speed - Most Wanted - Black Edition.iso"
    return p if p.is_file() else None


def _synthetic_elf() -> bytes:
    body = bytearray(b"\x7fELF" + b"\x11" * 4092)
    for pat in v24.SAVE_ANCHORS.values():
        body += pat + b"\x00" * 8
    return bytes(body + b"\x22" * 2048)


def _disc(cnf: bytes = CNF, elf: Optional[bytes] = None, source: str = "synthetic") -> dict:
    return {"source": source, "system_cnf_raw": cnf, "system_cnf": v24.parse_system_cnf(cnf), "elf": elf or _synthetic_elf()}


def _tel(t: float, heading: float, steer: float, speed: float = 60.0, **extra) -> dict:
    rec = {"timestamp": t, "flow_state_name": "RACING", "player_present": True, "speed_mph": speed,
           "heading_deg": heading, "wrong_way": False, "obstacle_count": 2, "stuck_recoveries": 0,
           "action": {"steer_cmd": steer, "heading_error_deg": 3.0}}
    rec.update(extra)
    return rec


class TestMeasuredVltAudit(unittest.TestCase):
    def setUp(self) -> None:
        self.recipe = json.loads(v24.MEASURED_RECIPE.read_text(encoding="utf-8"))

    def test_measured_recipe_schema_and_budget(self) -> None:
        self.assertEqual(self.recipe["target"]["game"], "SLUS-21351")
        for p in self.recipe["patches"]:
            for req in ("class", "node", "field", "expected_orig", "new_value", "expected_offset", "note"):
                self.assertIn(req, p)
        rep = v24.audit_recipe(self.recipe)
        self.assertEqual(rep["verdict"], "authentic")
        self.assertEqual(rep["in_scope_patches"], len(self.recipe["patches"]))
        self.assertLessEqual(rep["max_abs_rel_delta"], 0.10 + 1e-9)

    def test_measured_recipe_avoids_roster_keys_and_offsets(self) -> None:
        roster = json.loads((REPO_ROOT / "patches" / "vlt" / "full_roster_handling.json").read_text(encoding="utf-8"))
        r_keys = {(p["class"], p["node"], p["field"], p.get("member")) for p in roster["patches"]}
        r_offs = {int(p["expected_offset"], 16) for p in roster["patches"]}
        for p in self.recipe["patches"]:
            self.assertNotIn((p["class"], p["node"], p["field"], p.get("member")), r_keys)
            self.assertNotIn(int(p["expected_offset"], 16), r_offs)

    def test_audit_flags_over_budget_and_ignores_camera(self) -> None:
        recipe = {"id": "loud", "patches": [
            {"class": "tires", "node": "x", "field": "STEERING", "expected_orig": 1.0, "new_value": 1.2},
            {"class": "pursuitlevels", "node": "heat_01", "field": "NumPatrolCars", "expected_orig": 1, "new_value": 2},
            {"class": "tires", "node": "y", "field": "YAW_SPEED", "expected_orig": 0.3, "new_value": 0.31},
            {"class": "camerainfo", "node": "close", "field": "FOV", "expected_orig": 78.0, "new_value": 94.0},
        ]}
        rep = v24.audit_recipe(recipe)
        self.assertEqual(rep["verdict"], "exceeds_budget")
        self.assertEqual(rep["in_scope_patches"], 3)
        self.assertEqual([o["key"] for o in rep["worst_offenders"]], ["pursuitlevels/heat_01/NumPatrolCars/", "tires/x/STEERING/"])
        cam_only = v24.audit_recipe({"patches": recipe["patches"][3:]})
        self.assertEqual(cam_only["verdict"], "not_applicable")

    def test_audit_covers_every_repo_recipe(self) -> None:
        reports = {r["file"]: r for r in v24.audit_vlt_dir()}
        self.assertEqual(set(reports), {p.name for p in (REPO_ROOT / "patches" / "vlt").glob("*.json")})
        self.assertEqual(reports["authentic_measured_v24.json"]["verdict"], "authentic")

    def test_authentic_profile_validates_and_uses_only_measured_recipe(self) -> None:
        report = rockport_cli.collect_profiles(repo_root=REPO_ROOT)
        entry = {p["profile_id"]: p for p in report["profiles"]}["rockport2026_authentic_vlt"]
        self.assertTrue(entry["valid"], entry["errors"])
        prof = json.loads(v24.MEASURED_PROFILE.read_text(encoding="utf-8"))
        self.assertEqual([r["recipe"] for r in prof["vlt_recipes"]], ["patches/vlt/authentic_measured_v24.json"])


class TestTelemetryCompare(unittest.TestCase):
    def test_summary_skips_frontend_and_handles_heading_wrap(self) -> None:
        recs = [{"flow_state_name": "IN_FRONTEND", "speed_mph": 0.0}]
        recs += [_tel(i * 0.05, (359.0 + i) % 360.0, 0.1, heat=2, cop_count=3) for i in range(30)]
        s = v24.summarize_telemetry(recs)
        self.assertEqual(s["samples"], 30)
        self.assertAlmostEqual(s["yaw_rate_std_dps"], 0.0, places=6)  # constant 20 deg/s across the 359->0 wrap
        self.assertEqual((s["heat_max"], s["cop_count_mean"]), (2.0, 3.0))
        self.assertEqual(s["pursuit_metrics_source"], "heat/cop_count fields")

    def test_compare_identical_runs_pass(self) -> None:
        run = [_tel(i * 0.05, i * 0.5, 0.05 * (i % 3)) for i in range(40)]
        rep = v24.compare_telemetry(run, run)
        self.assertEqual(rep["verdict"], "pass")
        self.assertEqual(rep["delta"]["mean_speed_mph"], 0.0)

    def test_compare_flags_steering_instability_and_speed_drop(self) -> None:
        before = [_tel(i * 0.05, i * 0.5 + (i % 2) * 0.2, 0.05 * (i % 3)) for i in range(40)]
        after = [_tel(i * 0.05, (i % 2) * 8.0, 0.8 * (-1) ** i, speed=40.0) for i in range(40)]
        rep = v24.compare_telemetry(before, after)
        self.assertEqual(rep["verdict"], "regressed")
        joined = " ".join(rep["regressions"])
        for key in ("steer_jitter", "yaw_rate_std_dps", "mean_speed_mph"):
            self.assertIn(key, joined)
        self.assertIn("insufficient", " ".join(v24.compare_telemetry(before[:5], after[:5])["regressions"]))

    def test_cli_compare_telemetry_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            b, a = Path(td) / "before.jsonl", Path(td) / "after.jsonl"
            lines = "\n".join(json.dumps(_tel(i * 0.05, i * 0.4, 0.02)) for i in range(25)) + "\n{bad json\n"
            b.write_text(lines, encoding="utf-8")
            a.write_text(lines, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                rc = v24.main(["compare-telemetry", str(b), str(a)])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue())["before"]["samples"], 25)


class TestSaveTransferOffline(unittest.TestCase):
    def test_parse_system_cnf(self) -> None:
        cnf = v24.parse_system_cnf(CNF)
        self.assertEqual((cnf["serial"], cnf["elf_name"], cnf["vmode"]), ("SLUS-21351", "SLUS_213.51", "NTSC"))
        self.assertIsNone(v24.parse_system_cnf(b"BOOT2 = cdrom0:\\FOO.ELF;1")["serial"])

    def test_save_identity_extraction(self) -> None:
        ident = v24.extract_save_identity(_synthetic_elf())
        self.assertTrue(ident["all_anchors_present"])
        self.assertEqual(ident["save_dir_prefix"], "BASLUS-21351")
        self.assertIsNone(v24.extract_save_identity(b"\x00" * 64)["save_dir_prefix"])

    def test_identical_identity_passes_even_when_elf_crc_changes(self) -> None:
        elf = bytearray(_synthetic_elf())
        elf[8:12] = b"\x01\x02\x03\x04"  # ELF patch outside the save-identity span
        rep = v24.compare_disc_identities(_disc(), _disc(elf=bytes(elf), source="modded"))
        self.assertEqual(rep["status"], "pass", rep["checks"])
        self.assertTrue(rep["elf_crc_changed"])
        self.assertEqual(rep["sandbox_memcard"]["resolved"]["modded"], ["BASLUS-21351SANDBOX"])

    def test_changed_system_cnf_or_save_span_fails(self) -> None:
        bad_cnf = v24.compare_disc_identities(_disc(), _disc(cnf=CNF.replace(b"1.01", b"1.02")))
        self.assertFalse(bad_cnf["checks"]["system_cnf_byte_identical"])
        self.assertEqual(bad_cnf["status"], "fail")
        elf = bytearray(_synthetic_elf())
        off = elf.find(b"nfwMW_New.ico")
        elf[off:off + 3] = b"XXX"
        bad_elf = v24.compare_disc_identities(_disc(), _disc(elf=bytes(elf)))
        self.assertFalse(bad_elf["checks"]["save_identity_span_identical"])
        self.assertEqual(bad_elf["status"], "fail")
        patch = [{"id": "p", "writes": [{"file_offset": f"0x{off:08x}"}]}]
        touched = v24.compare_disc_identities(_disc(), _disc(), patch)
        self.assertFalse(touched["checks"]["no_elf_patch_inside_save_span"])

    def test_sandbox_memcard_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            card = Path(td) / "sandbox.ps2"
            payload = os.urandom(5000)  # spans multiple clusters to exercise the FAT chain
            sha = v24.build_sandbox_memcard(card, "BASLUS-21351TEST", {"icon.sys": v24._icon_sys("NFS MW"), "DATA": payload})
            self.assertEqual(card.stat().st_size, 8192 * 2 * 528)
            info = v24.read_memcard_saves(card)
            self.assertEqual((info["magic"], info["version"], info["page_len"]), ("Sony PS2 Memory Card Format ", "1.2.0.0", 512))
            self.assertEqual(info["saves"]["BASLUS-21351TEST"]["DATA"], payload)
            self.assertEqual(info["saves"]["BASLUS-21351TEST"]["icon.sys"][:4], b"PS2D")
            self.assertEqual(hashlib.sha256(card.read_bytes()).hexdigest(), sha)

    def test_memcard_writer_refuses_paths_outside_sandbox(self) -> None:
        target = Path("/nonexistent_v24_pcsx2/memcards/Mcd001.ps2")
        with self.assertRaises(PermissionError):
            v24.build_sandbox_memcard(target, "BASLUS-21351X", {"a": b"x"})
        self.assertFalse(target.exists())

    def test_cli_requires_iso_for_live_commands(self) -> None:
        old = os.environ.get("NFSMW_ISO")
        os.environ["NFSMW_ISO"] = "/nonexistent"
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(v24.main(["verify-saves"]), 2)
        finally:
            if old is None:
                os.environ.pop("NFSMW_ISO", None)
            else:
                os.environ["NFSMW_ISO"] = old


ISO_PATH = _find_local_iso()


@unittest.skipUnless(ISO_PATH is not None, "Local SLUS-21351 ISO not present; skipping live v2.4 checks")
class TestLiveRetailIso(unittest.TestCase):
    def test_measured_recipe_against_retail_attributes(self) -> None:
        assert ISO_PATH is not None
        rep = v24.verify_vlt_against_iso(ISO_PATH)
        self.assertEqual(rep["status"], "pass", rep)
        self.assertEqual(rep["patches_applied"], 9)
        self.assertTrue(rep["only_declared_bytes_changed"])

    def test_save_identity_retail_vs_modded_profiles(self) -> None:
        assert ISO_PATH is not None
        stat_before = (ISO_PATH.stat().st_size, ISO_PATH.stat().st_mtime_ns)
        for prof in ("rockport2026_authentic_vlt", "rockport2026_v0.5", "rockport2026_sandbox"):
            rep = v24.verify_save_transfer(ISO_PATH, profile=REPO_ROOT / "patches" / "profiles" / f"{prof}.json")
            self.assertEqual(rep["status"], "pass", (prof, rep["checks"]))
            self.assertEqual(rep["retail"]["elf_pcsx2_crc"], "0518D274")
            self.assertTrue(rep["elf_crc_changed"])
            self.assertEqual(rep["retail"]["save"]["save_dir_prefix"], "BASLUS-21351")
        self.assertEqual((ISO_PATH.stat().st_size, ISO_PATH.stat().st_mtime_ns), stat_before)


if __name__ == "__main__":
    unittest.main()
