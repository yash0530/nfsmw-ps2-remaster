#!/usr/bin/env python3
"""Offline unit tests for tools/pcsx2_pine_driver.py (with mock Unix-socket PINE server)."""

from __future__ import annotations

import json
import math
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Dict, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import pcsx2_pine_driver as pine_drv  # noqa: E402
import race_flywheel  # noqa: E402


class MockPineServer:
    """In-process Unix-domain-socket mock implementing PCSX2 v2.6.3 PINE wire protocol."""

    def __init__(self, sock_path: Path) -> None:
        self.sock_path = sock_path
        self.mem: bytearray = bytearray(0x02000000)  # 32 MiB PS2 EE RAM image
        self.status: int = int(pine_drv.EmuStatus.RUNNING)
        self.version: str = "PCSX2 v2.6.3"
        self.game_title: str = "Need for Speed: Most Wanted"
        self.game_id: str = "SLUS-21351"
        self.game_crc: int = 0x2DAD2310
        self.request_count: int = 0
        self._stop = threading.Event()
        self._srv: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def write_u8(self, addr: int, val: int) -> None:
        self.mem[addr] = val & 0xFF

    def write_i16(self, addr: int, val: int) -> None:
        struct.pack_into("<h", self.mem, addr, val)

    def write_u32(self, addr: int, val: int) -> None:
        struct.pack_into("<I", self.mem, addr, val)

    def write_i32(self, addr: int, val: int) -> None:
        struct.pack_into("<i", self.mem, addr, val)

    def write_f32(self, addr: int, val: float) -> None:
        struct.pack_into("<f", self.mem, addr, val)

    def write_vec3(self, addr: int, vec: Tuple[float, float, float]) -> None:
        struct.pack_into("<3f", self.mem, addr, vec[0], vec[1], vec[2])

    def start(self) -> None:
        if self.sock_path.exists():
            self.sock_path.unlink()
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(self.sock_path))
        self._srv.listen(4)
        self._srv.settimeout(0.1)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
        if self.sock_path.exists():
            try:
                self.sock_path.unlink()
            except OSError:
                pass

    def _recv_exact(self, conn: socket.socket, n: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = conn.recv(n - len(buf))
            except socket.timeout:
                if self._stop.is_set():
                    return None
                continue
            except OSError:
                return None
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def _handle_packet(self, body: bytes) -> bytes:
        self.request_count += 1
        out = bytearray()
        pos = 0
        ok = True
        while pos < len(body):
            op = body[pos]
            pos += 1
            if op in (
                pine_drv.PineOpcode.MSG_READ8,
                pine_drv.PineOpcode.MSG_READ16,
                pine_drv.PineOpcode.MSG_READ32,
                pine_drv.PineOpcode.MSG_READ64,
            ):
                if pos + 4 > len(body):
                    ok = False
                    break
                addr = struct.unpack_from("<I", body, pos)[0]
                pos += 4
                sz = {0: 1, 1: 2, 2: 4, 3: 8}[op]
                if addr + sz > len(self.mem):
                    ok = False
                    break
                out.extend(self.mem[addr : addr + sz])
            elif op == pine_drv.PineOpcode.MSG_VERSION:
                raw = self.version.encode("utf-8") + b"\x00"
                out.extend(struct.pack("<I", len(raw)) + raw)
            elif op == pine_drv.PineOpcode.MSG_STATUS:
                out.extend(struct.pack("<I", self.status))
            elif op == pine_drv.PineOpcode.MSG_GAME_TITLE:
                raw = self.game_title.encode("utf-8") + b"\x00"
                out.extend(struct.pack("<I", len(raw)) + raw)
            elif op == pine_drv.PineOpcode.MSG_GAME_ID:
                raw = self.game_id.encode("utf-8") + b"\x00"
                out.extend(struct.pack("<I", len(raw)) + raw)
            elif op == pine_drv.PineOpcode.MSG_GAME_CRC:
                out.extend(struct.pack("<I", self.game_crc))
            else:
                ok = False
                break

        status_byte = pine_drv.IPC_OK if ok else pine_drv.IPC_FAIL
        payload = bytes(out) if ok else b""
        return struct.pack("<IB", 5 + len(payload), status_byte) + payload

    def _loop(self) -> None:
        assert self._srv is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(0.2)
            with conn:
                while not self._stop.is_set():
                    hdr = self._recv_exact(conn, 4)
                    if hdr is None:
                        break
                    total_len = struct.unpack("<I", hdr)[0]
                    if total_len < 4:
                        break
                    body = self._recv_exact(conn, total_len - 4)
                    if body is None:
                        break
                    resp = self._handle_packet(body)
                    try:
                        conn.sendall(resp)
                    except OSError:
                        break


def populate_mock_nfsmw_scene(
    srv: MockPineServer,
    *,
    flow_state: int = int(pine_drv.GameFlowState.RACING),
    pos: Tuple[float, float, float] = (100.0, 2.0, 200.0),
    vel: Tuple[float, float, float] = (0.0, 0.0, 25.0),
    forward: Tuple[float, float, float] = (0.0, 0.0, 1.0),
    right: Tuple[float, float, float] = (1.0, 0.0, 0.0),
    speed_mps: float = 25.0,
    gear: int = 4,  # 3rd gear (0=R, 1=N, 2=1st, 3=2nd, 4=3rd)
    rpm: float = 5400.0,
    nos: float = 0.75,
    road_pos: Tuple[float, float, float] = (100.0, 2.0, 210.0),
    road_fwd: Tuple[float, float, float] = (0.0, 0.0, 1.0),
    wrong_way: int = 0,
    checkpoint_pos: Tuple[float, float, float] = (100.0, 2.0, 320.0),
    obstacle_pos: Tuple[float, float, float] | None = (100.5, 2.0, 218.0),
) -> Dict[str, int]:
    """Populate realistic SLUS-21351 pointer chains in mock 32 MiB EE RAM."""
    # Fixed scratch addresses inside valid EE RAM [0x00800000..0x01800000)
    sim_sys = 0x00801000
    iplayer_arr = 0x00802000
    local_player = 0x00810000
    iplayer_ptr = local_player + 0xB0  # DELTA_IPLAYER_TO_LOCALPLAYER = -0xB0
    pvehicle = 0x00820000
    isimable = pvehicle + 0x50  # DELTA_ISIMABLE_TO_PVEHICLE = -0x50
    iveh_player = pvehicle + 0x130  # DELTA_IVEHICLE_TO_PVEHICLE = -0x130
    rigidbody = 0x00830000
    irigidbody = rigidbody + 0x80  # DELTA_IRIGIDBODY_TO_RIGIDBODY = -0x80
    rb_holder = 0x00831000
    rb_state = 0x00832000
    engine_racer = 0x00840000
    iengine = engine_racer + 0xA0
    itrans = engine_racer + 0x90
    aivehicle = 0x00850000
    iaivehicle = aivehicle + 0x90
    roadnav = 0x00860000
    race_status = 0x00870000
    next_cp = 0x00880000
    veh_all_arr = 0x00890000

    # GameFlowManager & Sim::System
    srv.write_i32(pine_drv.ADDR_GAMEFLOW_STATE, flow_state)
    srv.write_u32(pine_drv.ADDR_SIM_SYSTEM_PTR, sim_sys)
    srv.write_i32(sim_sys + pine_drv.OFF_SIM_STATE, 1)  # active, not paused (4)
    srv.write_u32(pine_drv.ADDR_WORLD_LOOP_COUNTER, 12345)

    # IPlayer::mList[PLAYER_LOCAL]
    srv.write_u32(pine_drv.ADDR_IPLAYER_LOCAL_LIST + pine_drv.OFF_LIST_ARRAY_PTR, iplayer_arr)
    srv.write_u32(pine_drv.ADDR_IPLAYER_LOCAL_LIST + pine_drv.OFF_LIST_COUNT, 1)
    srv.write_u32(iplayer_arr, iplayer_ptr)
    srv.write_u32(local_player + pine_drv.OFF_LOCALPLAYER_SIMABLE, isimable)

    # Player PVehicle
    srv.write_u32(pvehicle + pine_drv.OFF_PVEHICLE_RIGIDBODY, irigidbody)
    srv.write_u32(pvehicle + pine_drv.OFF_PVEHICLE_IENGINE, iengine)
    srv.write_u32(pvehicle + pine_drv.OFF_PVEHICLE_ITRANSMISSION, itrans)
    srv.write_u32(pvehicle + pine_drv.OFF_PVEHICLE_IAIVEHICLE, iaivehicle)
    srv.write_f32(pvehicle + pine_drv.OFF_PVEHICLE_SPEED_MPS, speed_mps)
    srv.write_f32(pvehicle + pine_drv.OFF_PVEHICLE_ABS_SPEED_MPS, abs(speed_mps))
    srv.write_f32(pvehicle + pine_drv.OFF_PVEHICLE_SPEEDOMETER_MPS, abs(speed_mps))

    # RigidBody -> rb_holder -> rb_state
    srv.write_u32(rigidbody + pine_drv.OFF_RIGIDBODY_DATA_HOLDER, rb_holder)
    srv.write_u32(rb_holder, rb_state)
    srv.write_vec3(rb_state + pine_drv.OFF_RBSTATE_POS, pos)
    srv.write_vec3(rb_state + pine_drv.OFF_RBSTATE_LINVEL, vel)
    srv.write_u8(rb_state + pine_drv.OFF_RBSTATE_CONTACTS, 4)
    srv.write_vec3(rb_state + pine_drv.OFF_RBSTATE_RIGHT, right)
    srv.write_vec3(rb_state + pine_drv.OFF_RBSTATE_UP, (0.0, 1.0, 0.0))
    srv.write_vec3(rb_state + pine_drv.OFF_RBSTATE_FORWARD, forward)

    # EngineRacer
    srv.write_i32(engine_racer + pine_drv.OFF_ENGINERACER_GEAR, gear)
    srv.write_f32(engine_racer + pine_drv.OFF_ENGINERACER_RPM, rpm)
    srv.write_f32(engine_racer + pine_drv.OFF_ENGINERACER_NOS, nos)
    srv.write_f32(engine_racer + pine_drv.OFF_ENGINERACER_NOS_ENGAGED, 1.0 if nos > 0.0 else 0.0)

    # AIVehicleHuman -> WRoadNav
    srv.write_u32(aivehicle + pine_drv.OFF_AIVEHICLE_DRIVE_TO_NAV, roadnav)
    srv.write_i32(aivehicle + pine_drv.OFF_AIVEHICLEHUMAN_WRONG_WAY, wrong_way)
    srv.write_i32(roadnav + pine_drv.OFF_ROADNAV_VALID, 1)
    srv.write_i16(roadnav + pine_drv.OFF_ROADNAV_SEG_IND, 42)
    srv.write_vec3(roadnav + pine_drv.OFF_ROADNAV_POS, road_pos)
    srv.write_vec3(roadnav + pine_drv.OFF_ROADNAV_FORWARD, road_fwd)

    # GRaceStatus -> next checkpoint GTrigger
    srv.write_u32(pine_drv.ADDR_GRACESTATUS_PTR, race_status)
    srv.write_i32(race_status + pine_drv.OFF_GRACESTATUS_RACE_STATE, 1)
    srv.write_u32(race_status + pine_drv.OFF_GRACESTATUS_NEXT_CHECKPOINT, next_cp)
    srv.write_vec3(next_cp + pine_drv.OFF_GTRIGGER_POS, checkpoint_pos)

    # IVehicle::mList[VEHICLE_ALL]: player + optional obstacle car
    srv.write_u32(pine_drv.ADDR_IVEHICLE_ALL_LIST + pine_drv.OFF_LIST_ARRAY_PTR, veh_all_arr)
    srv.write_u32(veh_all_arr, iveh_player)
    if obstacle_pos is not None:
        obs_pv = 0x00900000
        obs_iveh = obs_pv + 0x130
        obs_rb = 0x00910000
        obs_irb = obs_rb + 0x80
        obs_holder = 0x00911000
        obs_state = 0x00912000
        srv.write_u32(pine_drv.ADDR_IVEHICLE_ALL_LIST + pine_drv.OFF_LIST_COUNT, 2)
        srv.write_u32(veh_all_arr + 4, obs_iveh)
        srv.write_u32(obs_pv + pine_drv.OFF_PVEHICLE_RIGIDBODY, obs_irb)
        srv.write_u32(obs_rb + pine_drv.OFF_RIGIDBODY_DATA_HOLDER, obs_holder)
        srv.write_u32(obs_holder, obs_state)
        srv.write_vec3(obs_state + pine_drv.OFF_RBSTATE_POS, obstacle_pos)
        srv.write_vec3(obs_state + pine_drv.OFF_RBSTATE_LINVEL, (0.0, 0.0, 12.0))
    else:
        srv.write_u32(pine_drv.ADDR_IVEHICLE_ALL_LIST + pine_drv.OFF_LIST_COUNT, 1)

    return {
        "pvehicle": pvehicle,
        "rb_state": rb_state,
        "engine_racer": engine_racer,
        "roadnav": roadnav,
    }


class TestPineProtocolAndTelemetry(unittest.TestCase):
    def setUp(self) -> None:
        # Keep Unix socket path short (< 104 bytes on macOS)
        self._tmpdir = tempfile.TemporaryDirectory(dir="/tmp", prefix="pine_t_")
        self.sock_path = Path(self._tmpdir.name) / "pcsx2.sock"
        self.srv = MockPineServer(self.sock_path)
        self.srv.start()

    def tearDown(self) -> None:
        self.srv.stop()
        self._tmpdir.cleanup()

    def test_socket_discovery_matches_pcsx2_v263(self) -> None:
        old_tmpdir = os.environ.get("TMPDIR")
        try:
            os.environ["TMPDIR"] = self._tmpdir.name + "/"
            default_cands = pine_drv.candidate_pine_socket_paths(slot=28011)
            self.assertEqual(default_cands[0], Path(self._tmpdir.name) / "pcsx2.sock")
            self.assertEqual(pine_drv.discover_pine_socket(slot=28011), self.sock_path)

            custom_cands = pine_drv.candidate_pine_socket_paths(slot=28099)
            self.assertEqual(custom_cands[0], Path(self._tmpdir.name) / "pcsx2.sock.28099")
            self.assertEqual(custom_cands[-1], Path("/tmp/pcsx2.sock.28099"))
        finally:
            if old_tmpdir is None:
                os.environ.pop("TMPDIR", None)
            else:
                os.environ["TMPDIR"] = old_tmpdir

    def test_metadata_and_batched_reads(self) -> None:
        self.srv.write_u8(0x00100000, 0xAB)
        self.srv.write_i16(0x00100002, 0x1234)
        self.srv.write_u32(0x00100004, 0xDEADBEEF)
        self.srv.write_f32(0x00100008, 123.5)
        self.srv.write_f32(0x0010000C, -45.25)

        with pine_drv.PineClient(self.sock_path) as client:
            self.assertEqual(client.version(), "PCSX2 v2.6.3")
            self.assertEqual(client.status(), pine_drv.EmuStatus.RUNNING)
            self.assertEqual(client.game_title(), "Need for Speed: Most Wanted")
            self.assertEqual(client.game_id(), "SLUS-21351")
            self.assertEqual(client.game_crc(), 0x2DAD2310)

            before_reqs = self.srv.request_count
            raw_list = client.read_batch_raw(
                [
                    (pine_drv.PineOpcode.MSG_READ8, 0x00100000),
                    (pine_drv.PineOpcode.MSG_READ16, 0x00100002),
                    (pine_drv.PineOpcode.MSG_READ32, 0x00100004),
                    (pine_drv.PineOpcode.MSG_READ64, 0x00100008),
                ]
            )
            # All 4 reads executed in ONE single PINE packet!
            self.assertEqual(self.srv.request_count, before_reqs + 1)
            self.assertEqual(raw_list[0][0], 0xAB)
            self.assertEqual(struct.unpack("<H", raw_list[1])[0], 0x1234)
            self.assertEqual(struct.unpack("<I", raw_list[2])[0], 0xDEADBEEF)
            f1, f2 = struct.unpack("<2f", raw_list[3])
            self.assertAlmostEqual(f1, 123.5, places=4)
            self.assertAlmostEqual(f2, -45.25, places=4)

    def test_full_slus21351_telemetry_decode(self) -> None:
        populate_mock_nfsmw_scene(
            self.srv,
            pos=(120.5, 3.25, -450.0),
            vel=(0.0, 0.0, 30.0),
            forward=(0.0, 0.0, 1.0),
            right=(1.0, 0.0, 0.0),
            speed_mps=30.0,
            gear=5,
            rpm=6250.0,
            nos=0.6,
            road_pos=(122.0, 3.25, -440.0),
            road_fwd=(0.1, 0.0, 0.995),
            checkpoint_pos=(135.0, 3.25, -350.0),
            obstacle_pos=(121.0, 3.25, -432.0),
        )

        with pine_drv.PineClient(self.sock_path) as client:
            reader = pine_drv.NfsmwTelemetryReader(client)
            tel = reader.read_telemetry()

        self.assertEqual(tel.flow_state, pine_drv.GameFlowState.RACING)
        self.assertEqual(tel.flow_state_name, "RACING")
        self.assertFalse(tel.paused)
        self.assertTrue(tel.player_present)
        self.assertAlmostEqual(tel.pos[0], 120.5, places=3)
        self.assertAlmostEqual(tel.pos[2], -450.0, places=3)
        self.assertAlmostEqual(tel.speed_mph, 30.0 * pine_drv.MPS_TO_MPH, places=2)
        self.assertEqual(tel.gear, 5)
        self.assertEqual(tel.gear_label, "4")
        self.assertEqual(pine_drv.OFF_PVEHICLE_IENGINE, 0x1B4)
        self.assertEqual(pine_drv.OFF_PVEHICLE_ITRANSMISSION, 0x1BC)
        self.assertEqual(pine_drv.OFF_ENGINERACER_NOS_ENGAGED, 0x180)
        self.assertAlmostEqual(tel.rpm or 0.0, 6250.0, places=1)
        self.assertAlmostEqual(tel.nos or 0.0, 0.6, places=2)
        self.assertTrue(tel.nos_engaged)
        self.assertEqual(tel.ground_contacts, 4)

        # Verify defensive guard: rpm == 0.0 while moving > 10 mph clears tel.nos to None
        populate_mock_nfsmw_scene(self.srv, speed_mps=25.0, rpm=0.0, nos=0.0)
        with pine_drv.PineClient(self.sock_path) as client:
            tel_guard = pine_drv.NfsmwTelemetryReader(client).read_telemetry()
        self.assertEqual(tel_guard.rpm, 0.0)
        self.assertIsNone(tel_guard.nos)
        self.assertTrue(tel.road_nav_valid)
        self.assertEqual(tel.road_segment, 42)
        self.assertIsNotNone(tel.next_checkpoint_pos)
        self.assertAlmostEqual(tel.next_checkpoint_pos[2], -350.0, places=2)
        self.assertEqual(len(tel.obstacles), 1)
        self.assertAlmostEqual(tel.obstacles[0].forward_dist_m, 18.0, places=2)
        self.assertAlmostEqual(tel.obstacles[0].lateral_offset_m, 0.5, places=2)

    def test_wait_for_gameflow_state(self) -> None:
        self.srv.write_i32(pine_drv.ADDR_GAMEFLOW_STATE, int(pine_drv.GameFlowState.LOADING_TRACK))

        def transition() -> None:
            threading.Event().wait(0.12)
            self.srv.write_i32(pine_drv.ADDR_GAMEFLOW_STATE, int(pine_drv.GameFlowState.RACING))

        t = threading.Thread(target=transition, daemon=True)
        t.start()
        with pine_drv.PineClient(self.sock_path) as client:
            reader = pine_drv.NfsmwTelemetryReader(client)
            ok, st, name, elapsed = pine_drv.wait_for_gameflow_state(
                reader,
                [int(pine_drv.GameFlowState.RACING)],
                timeout_sec=2.0,
                poll_interval=0.02,
            )
        t.join(timeout=1.0)
        self.assertTrue(ok)
        self.assertEqual(st, int(pine_drv.GameFlowState.RACING))
        self.assertEqual(name, "RACING")
        self.assertLess(elapsed, 1.5)


class TestAutopilotControllerAndRecovery(unittest.TestCase):
    def _make_tel(
        self,
        *,
        pos: Tuple[float, float, float] = (0.0, 0.0, 0.0),
        vel: Tuple[float, float, float] = (0.0, 0.0, 25.0),
        forward: Tuple[float, float, float] = (0.0, 0.0, 1.0),
        right: Tuple[float, float, float] = (1.0, 0.0, 0.0),
        speed_mps: float = 25.0,
        road_pos: Tuple[float, float, float] = (0.0, 0.0, 10.0),
        road_fwd: Tuple[float, float, float] = (0.0, 0.0, 1.0),
        gear: int = 3,
        wrong_way: bool = False,
        obstacles: list[pine_drv.ObstacleVehicle] | None = None,
    ) -> pine_drv.VehicleTelemetry:
        fwd_spd = vel[0] * forward[0] + vel[1] * forward[1] + vel[2] * forward[2]
        return pine_drv.VehicleTelemetry(
            timestamp=1.0,
            flow_state=int(pine_drv.GameFlowState.RACING),
            flow_state_name="RACING",
            sim_state=1,
            paused=False,
            world_loop_counter=100,
            player_count=1,
            vehicle_count=1,
            player_present=True,
            pvehicle_addr=0x00820000,
            pos=pos,
            vel=vel,
            right=right,
            up=(0.0, 1.0, 0.0),
            forward=forward,
            speed_mps=speed_mps,
            abs_speed_mps=abs(speed_mps),
            speedometer_mps=abs(speed_mps),
            speed_mph=abs(speed_mps) * pine_drv.MPS_TO_MPH,
            forward_speed_mps=fwd_spd,
            gear=gear,
            gear_label=pine_drv.gear_to_label(gear),
            rpm=5000.0,
            nos=0.8,
            wrong_way=wrong_way,
            road_nav_valid=True,
            road_segment=10,
            road_pos=road_pos,
            road_forward=road_fwd,
            road_tangent_deg=math.degrees(math.atan2(road_fwd[0], road_fwd[2])),
            obstacles=obstacles or [],
        )

    def test_road_tangent_steering_sign_right_and_left(self) -> None:
        pilot = pine_drv.DrivingAutopilot()
        # Road curves right (+X while car faces +Z, right=+X, within normal lane width)
        d_right = pilot.step(
            self._make_tel(road_pos=(3.0, 0.0, 15.0), road_fwd=(0.707, 0.0, 0.707)),
            dt=0.05,
        )
        self.assertEqual(d_right.mode, "DRIVE")
        self.assertGreater(d_right.steer_cmd, 0.5)
        self.assertIn("k", d_right.keys)
        self.assertIn("d", d_right.keys)

        # Road curves left (-X while car faces +Z, right=+X, within normal lane width)
        pilot_l = pine_drv.DrivingAutopilot()
        d_left = pilot_l.step(
            self._make_tel(road_pos=(-3.0, 0.0, 15.0), road_fwd=(-0.707, 0.0, 0.707)),
            dt=0.05,
        )
        self.assertEqual(d_left.mode, "DRIVE")
        self.assertLess(d_left.steer_cmd, -0.5)
        self.assertIn("k", d_left.keys)
        self.assertIn("a", d_left.keys)

    def test_straightaway_engages_nos_and_dodges_obstacle(self) -> None:
        pilot = pine_drv.DrivingAutopilot()
        # Clear straightaway at ~56 mph -> throttle ('k'=Cross) + NOS ('q'=L1), NEVER handbrake ('e'=R1) or speedbreaker ('l'=Circle)
        d_clear = pilot.step(self._make_tel(speed_mps=25.0), dt=0.05)
        self.assertIn("k", d_clear.keys)
        self.assertIn("q", d_clear.keys)
        self.assertNotIn("e", d_clear.keys)
        self.assertNotIn("l", d_clear.keys)
        self.assertNotIn("a", d_clear.keys)
        self.assertNotIn("d", d_clear.keys)
        self.assertEqual(pilot.nos_activated_ticks, 1)

        # NOS 1.2s burst latch keeps 'q' held even if heading drifts to 15 deg (< 18 deg abort)
        d_latch = pilot.step(
            self._make_tel(
                speed_mps=28.0,
                road_pos=(0.0, 0.0, 10.0),
                road_fwd=(math.sin(math.radians(15.0)), 0.0, math.cos(math.radians(15.0))),
            ),
            dt=0.05,
        )
        self.assertIn("q", d_latch.keys)
        self.assertEqual(pilot.nos_activated_ticks, 2)

        # Traffic car 10m ahead slightly to our right (+0.4m) -> dodge left ('a') & abort NOS;
        # Rising-edge unique obstacle counter counts 1 episode across multiple consecutive ticks!
        obs = pine_drv.ObstacleVehicle(
            pvehicle_addr=0x00900000,
            pos=(0.4, 0.0, 10.0),
            vel=(0.0, 0.0, 12.0),
            dist_m=10.0,
            forward_dist_m=10.0,
            lateral_offset_m=0.4,
        )
        for _ in range(6):
            d_dodge = pilot.step(self._make_tel(speed_mps=25.0, obstacles=[obs]), dt=0.05)
        self.assertLess(d_dodge.obstacle_bias, -0.5)
        self.assertIn("a", d_dodge.keys)
        self.assertNotIn("q", d_dodge.keys)
        self.assertEqual(pilot.obstacles_avoided, 1)

        # A distinct second obstacle vehicle increments unique counter to 2
        obs2 = pine_drv.ObstacleVehicle(
            pvehicle_addr=0x00900500,
            pos=(-0.5, 0.0, 12.0),
            vel=(0.0, 0.0, 10.0),
            dist_m=12.0,
            forward_dist_m=12.0,
            lateral_offset_m=-0.5,
        )
        pilot.step(self._make_tel(speed_mps=25.0, obstacles=[obs2]), dt=0.05)
        self.assertEqual(pilot.obstacles_avoided, 2)

    def test_high_offset_and_tangent_jump_speed_modulation(self) -> None:
        pilot = pine_drv.DrivingAutopilot(pine_drv.AutopilotConfig(verify_reverse_demo=False))
        # Establish normal heading at 74 mph (~33 m/s), road straight ahead
        pilot.step(self._make_tel(speed_mps=33.0, road_pos=(0.0, 0.0, 10.0), road_fwd=(0.0, 0.0, 1.0)), dt=0.05)
        # Suddenly road centerline is 8m off to the left (e.g. narrow Police Station booth) at 74 mph
        d_off = pilot.step(
            self._make_tel(speed_mps=33.0, road_pos=(-8.0, 0.0, 10.0), road_fwd=(0.0, 0.0, 1.0)),
            dt=0.05,
        )
        self.assertIn("j", d_off.keys)  # Modulates speed (>62 mph) instead of full-throttle smash
        self.assertNotIn("k", d_off.keys)
        self.assertNotIn("q", d_off.keys)

        # At 52 mph (~23 m/s) with abrupt 45 deg road tangent jump -> coasts (no 'k', no 'j', no 'q') while steering
        pilot2 = pine_drv.DrivingAutopilot(pine_drv.AutopilotConfig(verify_reverse_demo=False))
        pilot2.step(self._make_tel(speed_mps=23.0, road_pos=(0.0, 0.0, 10.0), road_fwd=(0.0, 0.0, 1.0)), dt=0.05)
        d_jump = pilot2.step(
            self._make_tel(speed_mps=23.0, road_pos=(0.0, 0.0, 10.0), road_fwd=(0.707, 0.0, 0.707)),
            dt=0.05,
        )
        self.assertNotIn("k", d_jump.keys)
        self.assertNotIn("j", d_jump.keys)
        self.assertNotIn("q", d_jump.keys)
        self.assertIn("d", d_jump.keys)

    def test_f8_timing_captures_main_menu_and_skips_black_race_fade_in(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="pine_f8_") as td:
            sock_path = Path(td) / "pcsx2.sock"
            srv = MockPineServer(sock_path)
            srv.start()
            try:
                populate_mock_nfsmw_scene(srv, flow_state=int(pine_drv.GameFlowState.IN_FRONTEND), speed_mps=20.0)
                sink = pine_drv.NullInputSink()
                sim_t = [0.0]
                f8_times: list[tuple[float, int]] = []

                orig_f8 = sink.trigger_f8

                def record_f8() -> None:
                    cur_flow = struct.unpack_from("<i", srv.mem, pine_drv.ADDR_GAMEFLOW_STATE)[0]
                    f8_times.append((round(sim_t[0], 2), cur_flow))
                    orig_f8()

                sink.trigger_f8 = record_f8  # type: ignore[method-assign]

                def fake_clock() -> float:
                    return sim_t[0]

                def fake_sleep(dt: float) -> None:
                    sim_t[0] += dt
                    # Transition from IN_FRONTEND -> RACING at t=4.0s
                    if sim_t[0] >= 4.0:
                        srv.write_i32(pine_drv.ADDR_GAMEFLOW_STATE, int(pine_drv.GameFlowState.RACING))

                with pine_drv.PineClient(sock_path) as client:
                    reader = pine_drv.NfsmwTelemetryReader(client)
                    pine_drv.run_closed_loop_autopilot(
                        reader,
                        sink,
                        duration_sec=3.0,
                        config=pine_drv.AutopilotConfig(hz=10.0, verify_reverse_demo=False),
                        f8_interval_sec=4.0,
                        wait_for_racing_timeout=10.0,
                        menu_auto_advance=True,
                        clock=fake_clock,
                        sleeper=fake_sleep,
                    )

                # Exactly 1 F8 snap inside IN_FRONTEND around t=3.4s (Main Menu BMW),
                # NO F8 snap at t_race=0.0s (t=4.0s black fade-in), and first race snap at t_race>=2.0s (t>=6.0s)!
                fe_snaps = [t for t, fl in f8_times if fl == int(pine_drv.GameFlowState.IN_FRONTEND)]
                race_snaps = [t for t, fl in f8_times if fl == int(pine_drv.GameFlowState.RACING)]
                self.assertEqual(len(fe_snaps), 1)
                self.assertGreaterEqual(fe_snaps[0], 3.3)
                self.assertEqual(len(race_snaps), 1)
                self.assertGreaterEqual(race_snaps[0], 5.95)
            finally:
                srv.stop()

    def test_stuck_wall_reverse_and_recover_state_machine(self) -> None:
        pilot = pine_drv.DrivingAutopilot(pine_drv.AutopilotConfig(verify_reverse_demo=False))
        # First drive briefly (>5 mph) so stuck-detection is armed (simulating past start countdown)
        pilot.step(self._make_tel(vel=(0.0, 0.0, 10.0), speed_mps=10.0), dt=0.05)
        # Drive stuck at 0.3 m/s against a right wall for 0.70s (14 ticks @ 20 Hz)
        last_dec = None
        for _ in range(15):
            last_dec = pilot.step(
                self._make_tel(
                    vel=(0.0, 0.0, 0.3),
                    speed_mps=0.3,
                    road_pos=(-6.0, 0.0, 10.0),
                    road_fwd=(-0.4, 0.0, 0.916),
                ),
                dt=0.05,
            )
            if last_dec.mode == "STUCK_REVERSE":
                break

        self.assertIsNotNone(last_dec)
        assert last_dec is not None
        self.assertEqual(last_dec.mode, "STUCK_REVERSE")
        self.assertEqual(pilot.stuck_recoveries, 1)
        self.assertIn("j", last_dec.keys)  # Reverse engaged
        # Desired road is to the LEFT (-X), so reversing counter-steers RIGHT ('d') to swing nose left
        self.assertIn("d", last_dec.keys)

        # Simulate reversing telemetry (negative forward speed & gear R=0)
        modes_seen = []
        for _ in range(30):
            dec = pilot.step(
                self._make_tel(
                    vel=(0.0, 0.0, -4.0),
                    speed_mps=-4.0,
                    gear=0,
                    road_pos=(-2.0, 0.0, 12.0),
                    road_fwd=(0.0, 0.0, 1.0),
                ),
                dt=0.05,
            )
            modes_seen.append(dec.mode)

        self.assertTrue(pilot.reverse_verified)
        self.assertIn("STUCK_TURN_OUT", modes_seen)
        self.assertEqual(modes_seen[-1], "DRIVE")

    def test_frontend_menu_navigator_freeroam_and_challenge(self) -> None:
        # 1. Freeroam schedule must send Left + Cross to move Safe House dialog [No] -> [Yes]
        nav_fr = pine_drv.FrontendMenuNavigator(mode="freeroam")
        sink_fr = pine_drv.NullInputSink()
        for t_step in range(0, 240):  # 0.0s .. 23.9s in 0.1s increments
            nav_fr.step(t_step * 0.1, int(pine_drv.GameFlowState.IN_FRONTEND), sink_fr)
        taps_fr = [payload[0] for kind, payload in sink_fr.history if kind == "tap"]
        # First 4 taps are 'k' (Title/Memcard -> Career -> Safe House -> Resume Free Roam), then 'left' -> 'k' (Yes!)
        self.assertEqual(taps_fr[:6], ["k", "k", "k", "k", "left", "k"])
        # Retry cycle after 18s sends k -> left -> k
        self.assertEqual(taps_fr[6:9], ["k", "left", "k"])
        # Immediately halts once flow transitions to LOADING_REGION
        self.assertIsNone(nav_fr.step(25.0, int(pine_drv.GameFlowState.LOADING_REGION), sink_fr))

        # 2. Challenge schedule sends 'k' then 'right' (Challenge Series) -> 'k' -> 'k' -> 'k' -> 'left' -> 'k'
        nav_ch = pine_drv.FrontendMenuNavigator(mode="challenge")
        sink_ch = pine_drv.NullInputSink()
        for t_step in range(0, 140):  # 0.0s .. 13.9s
            nav_ch.step(t_step * 0.1, int(pine_drv.GameFlowState.IN_FRONTEND), sink_ch)
        taps_ch = [payload[0] for kind, payload in sink_ch.history if kind == "tap"]
        self.assertEqual(taps_ch[:7], ["k", "right", "k", "k", "k", "left", "k"])

    def test_open_road_reverse_verification_maneuver_and_launch(self) -> None:
        pilot = pine_drv.DrivingAutopilot(
            pine_drv.AutopilotConfig(verify_reverse_demo=True, reverse_demo_min_distance_m=40.0)
        )
        # Advance 45m on clean open road (road_offset = 0m)
        z = 0.0
        triggered = False
        for _ in range(45):
            dec = pilot.step(
                self._make_tel(pos=(0.0, 0.0, z), vel=(0.0, 0.0, 25.0), speed_mps=25.0, road_pos=(0.0, 0.0, z + 10.0)),
                dt=0.05,
            )
            z += 1.25
            if dec.mode == "REVERSE_DEMO":
                triggered = True
                self.assertEqual(dec.keys, ("j",))
                break
        self.assertTrue(triggered)
        self.assertGreaterEqual(pilot.distance_traveled_m, 40.0)

        # Simulate braking to 0 then reversing in R gear (gear=0) at >15 mph (-7.2 m/s = 16.1 mph)
        launched = False
        for i in range(45):
            fwd_v = max(-7.2, 10.0 - i * 1.0)
            gear = 0 if fwd_v <= 0.0 else 2
            dec = pilot.step(
                self._make_tel(
                    pos=(0.0, 0.0, z),
                    vel=(0.0, 0.0, fwd_v),
                    speed_mps=abs(fwd_v),
                    gear=gear,
                    road_pos=(0.0, 0.0, z + 10.0),
                ),
                dt=0.05,
            )
            if dec.mode == "DRIVE":
                launched = True
                self.assertIn("k", dec.keys)
                self.assertIn("q", dec.keys)  # Launches back forward with Cross + L1 NOS!
                break

        self.assertTrue(launched)
        self.assertTrue(pilot.reverse_verified)
        self.assertTrue(pilot.reverse_demo_completed)
        self.assertGreater(pilot.max_reverse_speed_mph, 15.0)
        # Stuck detection must NOT false-trigger during post-reverse launch grace at 0.5 m/s
        for _ in range(15):
            d_grace = pilot.step(
                self._make_tel(pos=(0.0, 0.0, z), vel=(0.0, 0.0, 0.5), speed_mps=0.5, gear=2, road_pos=(0.0, 0.0, z + 10.0)),
                dt=0.05,
            )
            self.assertEqual(d_grace.mode, "DRIVE")
        self.assertEqual(pilot.stuck_recoveries, 0)

    def test_end_to_end_closed_loop_with_mock_pine_and_jsonl(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="pine_e2e_") as td:
            sock_path = Path(td) / "pcsx2.sock"
            jsonl_path = Path(td) / "telemetry.jsonl"
            srv = MockPineServer(sock_path)
            srv.start()
            try:
                refs = populate_mock_nfsmw_scene(srv, pos=(0.0, 1.0, 0.0), speed_mps=20.0)
                sink = pine_drv.NullInputSink()
                sim_t = [0.0]

                def fake_clock() -> float:
                    return sim_t[0]

                def fake_sleep(dt: float) -> None:
                    sim_t[0] += dt
                    # Advance mock car forward along Z each tick
                    cur_z = struct.unpack_from("<f", srv.mem, refs["rb_state"] + pine_drv.OFF_RBSTATE_POS + 8)[0]
                    srv.write_vec3(refs["rb_state"] + pine_drv.OFF_RBSTATE_POS, (0.0, 1.0, cur_z + 20.0 * dt))

                with pine_drv.PineClient(sock_path) as client:
                    reader = pine_drv.NfsmwTelemetryReader(client)
                    summary = pine_drv.run_closed_loop_autopilot(
                        reader,
                        sink,
                        duration_sec=0.5,
                        config=pine_drv.AutopilotConfig(hz=20.0),
                        telemetry_jsonl_path=jsonl_path,
                        clock=fake_clock,
                        sleeper=fake_sleep,
                    )

                self.assertGreaterEqual(summary["ticks"], 8)
                self.assertGreater(summary["distance_traveled_m"], 5.0)
                self.assertGreater(summary["max_speed_mph"], 40.0)
                self.assertTrue(jsonl_path.is_file())
                lines = [json.loads(l) for l in jsonl_path.read_text().splitlines() if l.strip()]
                self.assertEqual(len(lines), summary["ticks"])
                self.assertEqual(lines[0]["flow_state_name"], "RACING")
                self.assertIn("action", lines[0])
            finally:
                srv.stop()

    def test_race_flywheel_sandbox_enables_pine_without_touching_global_ini(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            data_dir = base / "global_pcsx2"
            (data_dir / "inis").mkdir(parents=True)
            (data_dir / "memcards").mkdir(parents=True)
            (data_dir / "patches").mkdir(parents=True)
            (data_dir / "bios").mkdir(parents=True)
            (data_dir / "resources").mkdir(parents=True)

            global_ini = data_dir / "inis" / "PCSX2.ini"
            orig_ini_text = "[UI]\nConfirmShutdown = true\n\n[EmuCore]\nEnablePINE = false\nPINESlot = 28011\n"
            global_ini.write_text(orig_ini_text, encoding="utf-8")

            flywheel_dir = base / "flywheel"
            iter_dir = flywheel_dir / "runs" / "iter_pine_test"
            preflight = {
                "iso": {"serial": "SLUS-21351", "elf_crc": "2DAD2310"},
                "global_pcsx2_ini": {"bios_dir": str(data_dir / "bios")},
            }
            info = race_flywheel.prepare_flywheel_sandbox(
                flywheel_dir=flywheel_dir,
                iter_dir=iter_dir,
                data_dir=data_dir,
                preflight=preflight,
                enable_pine=True,
                pine_slot=28042,
            )

            self.assertTrue(info["enable_pine"])
            self.assertEqual(info["pine_slot"], 28042)
            # Global INI must remain completely untouched
            self.assertEqual(global_ini.read_text(encoding="utf-8"), orig_ini_text)
            # Sandbox INI must have EnablePINE = true and PINESlot = 28042 under [EmuCore]
            sandbox_text = Path(info["sandbox_ini"]).read_text(encoding="utf-8")
            self.assertIn("EnablePINE = true", sandbox_text)
            self.assertIn("PINESlot = 28042", sandbox_text)


if __name__ == "__main__":
    unittest.main()
