#!/usr/bin/env python3
"""
pcsx2_pine_driver.py — PCSX2 v2.6.3 PINE IPC Telemetry Reader & Closed-Loop Driving Autopilot
for Need for Speed: Most Wanted - Black Edition (PS2, SLUS-21351).

Reads live PS2 Emotion Engine (EE) RAM at 20-60 Hz over PCSX2's official PINE Unix-domain
socket (sub-millisecond batched reads) instead of driving from screenshots:
  - GameFlowState (`TheGameFlowManager + 0x20` @ 0x00573B50) & Sim::State (`*(0x005B2E74) + 0x34`)
  - Player Vehicle (`IPlayer::mList[PLAYER_LOCAL]` -> LocalPlayer -> ISimable/PVehicle -> RigidBody):
      * 3D world position (x, y, z)
      * 3D linear velocity (vx, vy, vz) & scalar speed (m/s, mph)
      * Orientation basis vectors: right (rx, ry, rz), up (ux, uy, uz), forward (fx, fy, fz)
      * Gear (R/N/1..8), Engine RPM, NOS capacity (`EngineRacer`)
  - Live Road Navigation (`AIVehicleHuman::mDriveToNav` -> `WRoadNav`):
      * Road centerline position (`+0xB0`), forward tangent vector (`+0xE0`), segment index (`+0x9A`)
      * Wrong-way indicator (`AIVehicleHuman + 0x9A0`)
  - Active Race Next Checkpoint (`GRaceStatus::fObj` @ `*(0x00570DFC)` -> `mNextCheckpoint + 0x58`)
  - Live Obstacle Vehicles (`IVehicle::mList[VEHICLE_ALL]` @ 0x00574B40 -> all non-player cars)

SAFETY:
  - Read-only PINE IPC telemetry by default (never patches game state during autopilot).
  - Never modifies global PCSX2.ini or real memory cards.
  - Never hardcodes personal paths.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import socket
import struct
import subprocess
import sys
import time
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# PCSX2 v2.6.3 PINE IPC Wire Protocol Constants (verified from PCSX2 binary)
# ---------------------------------------------------------------------------

DEFAULT_PINE_SLOT = 28011
MAX_IPC_REQUEST_SIZE = 650_000
MAX_IPC_RESPONSE_SIZE = 450_000

IPC_OK = 0x00
IPC_FAIL = 0xFF


class PineOpcode(IntEnum):
    MSG_READ8 = 0
    MSG_READ16 = 1
    MSG_READ32 = 2
    MSG_READ64 = 3
    MSG_WRITE8 = 4
    MSG_WRITE16 = 5
    MSG_WRITE32 = 6
    MSG_WRITE64 = 7
    MSG_VERSION = 8
    MSG_STATUS = 9
    MSG_GAME_TITLE = 10
    MSG_GAME_ID = 11
    MSG_GAME_UUID = 12
    MSG_GAME_CRC = 13
    MSG_SAVE_STATE = 14
    MSG_LOAD_STATE = 15


class EmuStatus(IntEnum):
    RUNNING = 0
    PAUSED = 1
    SHUTDOWN = 2


_READ_OPCODE_SIZES: Dict[int, int] = {
    PineOpcode.MSG_READ8: 1,
    PineOpcode.MSG_READ16: 2,
    PineOpcode.MSG_READ32: 4,
    PineOpcode.MSG_READ64: 8,
}

# ---------------------------------------------------------------------------
# Verified SLUS_213.51 (NFSMW Black Edition NTSC-U) EE RAM Telemetry Map
# Every address & struct offset below is verified against SLUS_213.51 disassembly
# and build/symbols_slus21351.txt.
# ---------------------------------------------------------------------------

EE_RAM_MIN = 0x00100000
EE_RAM_MAX = 0x02000000  # 32 MiB PS2 Emotion Engine main RAM

# GameFlowManager (0x00573B30; SetState__15GameFlowManager13GameFlowState writes +0x20)
ADDR_GAMEFLOW_MANAGER = 0x00573B30
OFF_GAMEFLOW_STATE = 0x20
ADDR_GAMEFLOW_STATE = ADDR_GAMEFLOW_MANAGER + OFF_GAMEFLOW_STATE  # 0x00573B50 (int32)

# Sim::System pointer (GetState__3Simv @ 0x00394858 reads *(0x005B2E74) + 0x34)
ADDR_SIM_SYSTEM_PTR = 0x005B2E74
OFF_SIM_TIMESTEP = 0x24  # float32
OFF_SIM_STATE = 0x34  # int32 (4 == SIM_STATE_PAUSED)
SIM_STATE_PAUSED = 4

# WorldLoopCounter (symbols: WorldLoopCounter = 0x574304; size:0x4)
ADDR_WORLD_LOOP_COUNTER = 0x00574304

# UTL::Collections::ListableSet<IPlayer, 8, ePlayerList, 3> (First__...IPlayer @ 0x00137F10)
# Base 0x005754E0, stride 0x30 (48 bytes). ePlayerList: 0=ALL, 1=PLAYER_LOCAL, 2=REMOTE.
ADDR_IPLAYER_LIST_BASE = 0x005754E0
IPLAYER_LIST_STRIDE = 0x30
PLAYER_LIST_ALL = 0
PLAYER_LIST_LOCAL = 1
ADDR_IPLAYER_LOCAL_LIST = ADDR_IPLAYER_LIST_BASE + PLAYER_LIST_LOCAL * IPLAYER_LIST_STRIDE  # 0x00575510
OFF_LIST_ARRAY_PTR = 0x00  # IPlayer** / IVehicle**
OFF_LIST_COUNT = 0x08  # int32

# UTL::Collections::ListableSet<IVehicle, 10, eVehicleList, 10> (Count__...IVehicle @ 0x00136EF0)
# Base 0x00574B40, stride 0x38 (56 bytes). eVehicleList: 0=ALL, 1=PLAYERS, 2=AI, 3=AIRACERS, 4=AICOPS, 5=AITRAFFIC.
ADDR_IVEHICLE_LIST_BASE = 0x00574B40
IVEHICLE_LIST_STRIDE = 0x38
VEHICLE_LIST_ALL = 0
VEHICLE_LIST_PLAYERS = 1
ADDR_IVEHICLE_ALL_LIST = ADDR_IVEHICLE_LIST_BASE + VEHICLE_LIST_ALL * IVEHICLE_LIST_STRIDE  # 0x00574B40
ADDR_IVEHICLE_PLAYERS_LIST = ADDR_IVEHICLE_LIST_BASE + VEHICLE_LIST_PLAYERS * IVEHICLE_LIST_STRIDE  # 0x00574B78

# Multiple-inheritance COM interface -> concrete object `this` deltas (verified from SLUS_213.51 vtables)
DELTA_IPLAYER_TO_LOCALPLAYER = -0xB0  # LocalPlayer IPlayer vtable @ 0x004FC620
OFF_LOCALPLAYER_SIMABLE = 0xA0  # GetSimable__C11LocalPlayer @ 0x003A61B0: lw $v0, 0xa0($a0)
# Shortcut: *(u32*)(IPlayer* - 0xB0 + 0xA0) == *(u32*)(IPlayer* - 0x10)
DELTA_ISIMABLE_TO_PVEHICLE = -0x50  # PVehicle::GetSimable @ 0x00349140: addiu $v0, $a0, 0x50
DELTA_IVEHICLE_TO_PVEHICLE = -0x130  # PVehicle IVehicle vtable @ 0x004F14C4 / 0x004F15CC

# PVehicle fields (verified from GetPosition/GetSpeed/OnBehaviorChange__8PVehicle @ 0x0032C928..0x0032C93C)
OFF_PVEHICLE_RIGIDBODY = 0xE0  # IRigidBody* (GetRigidBody__13PhysicsObject @ 0x00349838)
OFF_PVEHICLE_IENGINE = 0x1B4  # IEngine* (OnBehaviorChange__8PVehicle @ 0x0032C93C)
OFF_PVEHICLE_ITRANSMISSION = 0x1BC  # ITransmission* (OnBehaviorChange__8PVehicle @ 0x0032C928)
OFF_PVEHICLE_IAIVEHICLE = 0x1C0  # IVehicleAI* (GetAIVehiclePtr__C8PVehicle @ 0x00348FC0)
OFF_PVEHICLE_SPEED_MPS = 0x1DC  # float32 (GetSpeed__C8PVehicle @ 0x00349008)
OFF_PVEHICLE_ABS_SPEED_MPS = 0x1E0  # float32 (GetAbsoluteSpeed__C8PVehicle @ 0x00349018)
OFF_PVEHICLE_SPEEDOMETER_MPS = 0x1E4  # float32 (GetSpeedometer__C8PVehicle @ 0x00349010)
OFF_PVEHICLE_SLIP_ANGLE = 0x1EC  # float32 (GetSlipAngle__C8PVehicle @ 0x00348FB8)
OFF_PVEHICLE_DRIVER_CLASS = 0x20C  # int32 (GetDriverClass__C8PVehicle @ 0x003490F8)

# IRigidBody* -> RigidBody* -> RigidBody::State* (verified from RigidBody getters @ 0x00382230..0x00382630)
DELTA_IRIGIDBODY_TO_RIGIDBODY = -0x80  # IRigidBody vtable @ 0x004F0E48
OFF_RIGIDBODY_DATA_HOLDER = 0x120  # lw $v1, 0x120($a0); lw $v0, 0x0($v1) -> RigidBody::State*
OFF_RBSTATE_QUAT = 0x00  # 4 x float32 (qx, qy, qz, qw)
OFF_RBSTATE_POS = 0x10  # 3 x float32 (x, y, z) — UMath::Vector3 (Y-up, XZ horizontal)
OFF_RBSTATE_LINVEL = 0x20  # 3 x float32 (vx, vy, vz)
OFF_RBSTATE_ANGVEL = 0x30  # 3 x float32 (wx, wy, wz)
OFF_RBSTATE_CONTACTS = 0x6C  # int8 ground contact point count
OFF_RBSTATE_RIGHT = 0x80  # 3 x float32 (rx, ry, rz) — GetRightVector__C9RigidBody
OFF_RBSTATE_UP = 0x90  # 3 x float32 (ux, uy, uz) — GetUpVector__C9RigidBody
OFF_RBSTATE_FORWARD = 0xA0  # 3 x float32 (fx, fy, fz) — GetForwardVector__C9RigidBody

# EngineRacer (verified from GetRPM/GetGear/GetNOSCapacity/IsNOSEngaged @ 0x0037F488..0x0037F6A0)
DELTA_IENGINE_TO_ENGINERACER = -0xA0  # vtable @ 0x004F5B10
DELTA_ITRANS_TO_ENGINERACER = -0x90  # vtable @ 0x004F5B98
OFF_ENGINERACER_GEAR = 0x104  # int32: 0=R, 1=N, 2=1st, 3=2nd, ..., 9=8th
OFF_ENGINERACER_NOS = 0x178  # float32 [0.0, 1.0] (GetNOSCapacity__C11EngineRacer @ 0x0037F660)
OFF_ENGINERACER_NOS_ENGAGED = 0x180  # float32 >= 1.0 when active (IsNOSEngaged__C11EngineRacer @ 0x0037F630)
OFF_ENGINERACER_RPM = 0x200  # float32 (GetRPM__C11EngineRacer @ 0x0037F6A0)

# AIVehicle / AIVehicleHuman -> WRoadNav (verified from GetDriveToNav/UpdateWrongWay/ResetVehicleToRoadNav)
DELTA_IAIVEHICLE_TO_AIVEHICLE = -0x90  # vtable @ 0x004C3A1C
OFF_AIVEHICLE_DRIVE_TO_NAV = 0xD0  # WRoadNav* (GetDriveToNav__9AIVehicle @ 0x00142C10)
OFF_AIVEHICLEHUMAN_WRONG_WAY = 0x9A0  # int32 bool (IsFacingWrongWay__C14AIVehicleHuman @ 0x00140C70)

# WRoadNav fields (verified from UpdateWrongWay @ 0x00117DC8..0x00117E08 & ResetVehicleToRoadNav @ 0x00119CBC)
OFF_ROADNAV_VALID = 0x4C  # int32
OFF_ROADNAV_SEG_IND = 0x9A  # int16
OFF_ROADNAV_POS = 0xB0  # 3 x float32 (x, y, z) centerline position
OFF_ROADNAV_FORWARD = 0xE0  # 3 x float32 (fx, fy, fz) road forward tangent

# GRaceStatus::fObj (verified from UpdateWrongWay @ 0x00117CFC & SetNextCheckpointPos @ 0x002AF760)
ADDR_GRACESTATUS_PTR = 0x00570DFC
OFF_GRACESTATUS_RACER_COUNT = 0x1C30  # int32
OFF_GRACESTATUS_RACE_STATE = 0x1C38  # int32 (1 == active race)
OFF_GRACESTATUS_NEXT_CHECKPOINT = 0x1C9C  # GTrigger*
OFF_GTRIGGER_POS = 0x58  # 3 x float32 (GetPosition__8GTrigger @ 0x002AB600)

MPS_TO_MPH = 2.2369362920544


class GameFlowState(IntEnum):
    NONE = 0
    LOADING_FRONTEND = 1
    UNLOADING_FRONTEND = 2
    IN_FRONTEND = 3
    LOADING_REGION = 4
    LOADING_TRACK = 5
    RACING = 6
    UNLOADING_TRACK = 7
    UNLOADING_REGION = 8

    @classmethod
    def label(cls, value: int) -> str:
        try:
            return cls(value).name
        except ValueError:
            return f"UNKNOWN_{value}"


def gear_to_label(gear: Optional[int]) -> str:
    if gear is None:
        return "?"
    if gear == 0:
        return "R"
    if gear == 1:
        return "N"
    if 2 <= gear <= 9:
        return str(gear - 1)
    return f"G{gear}"


def is_valid_ee_ptr(addr: int) -> bool:
    return EE_RAM_MIN <= addr < EE_RAM_MAX and (addr & 0x3) == 0


# ---------------------------------------------------------------------------
# PINE IPC Exceptions & Socket Discovery
# ---------------------------------------------------------------------------


class PineError(RuntimeError):
    """Base error for PINE IPC failures."""


class PineConnectionError(PineError):
    """Raised when the PINE Unix domain socket cannot be reached or closes."""


class PineProtocolError(PineError):
    """Raised when PCSX2 returns IPC_FAIL or a malformed response."""


def candidate_pine_socket_paths(
    slot: int = DEFAULT_PINE_SLOT,
    explicit_path: Optional[str | Path] = None,
) -> List[Path]:
    """Return ordered candidate PINE socket paths matching PCSX2 v2.6.3 PINEServer::Initialize.

    In PCSX2 v2.6.3 macOS (`PINEServer::Initialize(int)`):
      - Base dir is `getenv("TMPDIR")` if set, else `"/tmp"`.
      - Base name is `<dir>/pcsx2.sock`.
      - When `slot != 28011`, PCSX2 appends `f".{slot}"`.
    """
    candidates: List[Path] = []
    if explicit_path is not None and str(explicit_path).strip():
        candidates.append(Path(explicit_path).expanduser())
        return candidates

    suffix = "" if slot == DEFAULT_PINE_SLOT else f".{slot}"
    fname = f"pcsx2.sock{suffix}"

    seen: set[str] = set()
    for env_key in ("TMPDIR", "XDG_RUNTIME_DIR"):
        val = os.environ.get(env_key, "").strip()
        if val:
            p = Path(os.path.normpath(os.path.join(val, fname)))
            s = str(p)
            if s not in seen:
                seen.add(s)
                candidates.append(p)

    fallback = Path("/tmp") / fname
    if str(fallback) not in seen:
        candidates.append(fallback)
    return candidates


def discover_pine_socket(
    slot: int = DEFAULT_PINE_SLOT,
    explicit_path: Optional[str | Path] = None,
) -> Path:
    """Return the first existing PINE socket path, or the primary expected path if none exist yet."""
    cands = candidate_pine_socket_paths(slot=slot, explicit_path=explicit_path)
    for p in cands:
        if p.exists():
            return p
    return cands[0]


# ---------------------------------------------------------------------------
# Pure-Python PINE IPC Client (with single-packet batched multi-reads)
# ---------------------------------------------------------------------------


class PineClient:
    """High-speed synchronous PCSX2 PINE IPC client over Unix domain sockets."""

    def __init__(
        self,
        socket_path: Optional[str | Path] = None,
        *,
        slot: int = DEFAULT_PINE_SLOT,
        timeout: float = 1.0,
    ) -> None:
        self.slot = int(slot)
        self.explicit_path = Path(socket_path).expanduser() if socket_path is not None else None
        self.timeout = float(timeout)
        self._sock: Optional[socket.socket] = None
        self.connected_path: Optional[Path] = None

    def __enter__(self) -> "PineClient":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @property
    def is_connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> Path:
        """Connect to the PCSX2 PINE Unix socket (trying discovered paths in order)."""
        self.close()
        last_exc: Optional[Exception] = None
        cands = candidate_pine_socket_paths(slot=self.slot, explicit_path=self.explicit_path)
        for path in cands:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            try:
                s.connect(str(path))
                self._sock = s
                self.connected_path = path
                return path
            except OSError as exc:
                s.close()
                last_exc = exc
        tried = ", ".join(str(p) for p in cands)
        raise PineConnectionError(f"Could not connect to PCSX2 PINE socket (tried: {tried}): {last_exc}")

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            self.connected_path = None

    def _recv_exact(self, n: int) -> bytes:
        if self._sock is None:
            raise PineConnectionError("PINE client is not connected")
        chunks: List[bytes] = []
        remaining = n
        while remaining > 0:
            try:
                chunk = self._sock.recv(remaining)
            except OSError as exc:
                self.close()
                raise PineConnectionError(f"PINE socket recv error: {exc}") from exc
            if not chunk:
                self.close()
                raise PineConnectionError("PINE socket closed by PCSX2")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _send_and_recv(self, payload_body: bytes) -> bytes:
        """Send one framed PINE request (`<I total_len` + body) and return response body (after status byte)."""
        if self._sock is None:
            self.connect()
        assert self._sock is not None
        total_len = 4 + len(payload_body)
        if total_len > MAX_IPC_REQUEST_SIZE:
            raise PineProtocolError(f"PINE request too large ({total_len} > {MAX_IPC_REQUEST_SIZE})")
        packet = struct.pack("<I", total_len) + payload_body
        try:
            self._sock.sendall(packet)
        except OSError as exc:
            self.close()
            raise PineConnectionError(f"PINE socket send error: {exc}") from exc

        hdr = self._recv_exact(5)
        resp_len, status = struct.unpack("<IB", hdr)
        if resp_len < 5 or resp_len > MAX_IPC_RESPONSE_SIZE:
            raise PineProtocolError(f"Invalid PINE response length: {resp_len}")
        body = self._recv_exact(resp_len - 5) if resp_len > 5 else b""
        if status != IPC_OK:
            raise PineProtocolError(f"PINE server returned failure status 0x{status:02X}")
        return body

    # --- Metadata / VM status commands ---

    def _read_string_cmd(self, opcode: PineOpcode) -> str:
        body = self._send_and_recv(struct.pack("<B", int(opcode)))
        if len(body) < 4:
            raise PineProtocolError(f"Short string response for opcode {opcode.name}: {len(body)} bytes")
        str_len = struct.unpack("<I", body[:4])[0]
        raw = body[4 : 4 + str_len]
        return raw.rstrip(b"\x00").decode("utf-8", errors="replace")

    def version(self) -> str:
        return self._read_string_cmd(PineOpcode.MSG_VERSION)

    def status(self) -> EmuStatus:
        body = self._send_and_recv(struct.pack("<B", int(PineOpcode.MSG_STATUS)))
        if len(body) < 4:
            raise PineProtocolError(f"Short MsgStatus response: {len(body)} bytes")
        return EmuStatus(struct.unpack("<I", body[:4])[0])

    def game_title(self) -> str:
        return self._read_string_cmd(PineOpcode.MSG_GAME_TITLE)

    def game_id(self) -> str:
        return self._read_string_cmd(PineOpcode.MSG_GAME_ID)

    def game_crc(self) -> int:
        body = self._send_and_recv(struct.pack("<B", int(PineOpcode.MSG_GAME_CRC)))
        if len(body) < 4:
            raise PineProtocolError(f"Short MsgGameCRC response: {len(body)} bytes")
        return struct.unpack("<I", body[:4])[0]

    # --- Single & batched EE RAM reads ---

    def read_batch_raw(self, ops: Sequence[Tuple[PineOpcode, int]]) -> List[bytes]:
        """Execute multiple MsgRead8/16/32/64 commands in ONE PINE IPC round-trip."""
        if not ops:
            return []
        req_parts: List[bytes] = []
        expected_sizes: List[int] = []
        for op, addr in ops:
            sz = _READ_OPCODE_SIZES.get(int(op))
            if sz is None:
                raise ValueError(f"Unsupported batch read opcode: {op!r}")
            req_parts.append(struct.pack("<BI", int(op), addr & 0xFFFFFFFF))
            expected_sizes.append(sz)

        body = self._send_and_recv(b"".join(req_parts))
        total_expected = sum(expected_sizes)
        if len(body) != total_expected:
            raise PineProtocolError(
                f"Batched PINE response length mismatch: got {len(body)}, expected {total_expected}"
            )
        out: List[bytes] = []
        off = 0
        for sz in expected_sizes:
            out.append(body[off : off + sz])
            off += sz
        return out

    def read_u8(self, addr: int) -> int:
        return self.read_batch_raw([(PineOpcode.MSG_READ8, addr)])[0][0]

    def read_u16(self, addr: int) -> int:
        return struct.unpack("<H", self.read_batch_raw([(PineOpcode.MSG_READ16, addr)])[0])[0]

    def read_u32(self, addr: int) -> int:
        return struct.unpack("<I", self.read_batch_raw([(PineOpcode.MSG_READ32, addr)])[0])[0]

    def read_s32(self, addr: int) -> int:
        return struct.unpack("<i", self.read_batch_raw([(PineOpcode.MSG_READ32, addr)])[0])[0]

    def read_u64(self, addr: int) -> int:
        return struct.unpack("<Q", self.read_batch_raw([(PineOpcode.MSG_READ64, addr)])[0])[0]

    def read_f32(self, addr: int) -> float:
        return struct.unpack("<f", self.read_batch_raw([(PineOpcode.MSG_READ32, addr)])[0])[0]

    def read_bytes(self, addr: int, size: int) -> bytes:
        """Read an arbitrary contiguous byte range in a single batched PINE packet using 64/32/16/8 reads."""
        if size <= 0:
            return b""
        ops: List[Tuple[PineOpcode, int]] = []
        cur = addr
        rem = size
        while rem >= 8:
            ops.append((PineOpcode.MSG_READ64, cur))
            cur += 8
            rem -= 8
        while rem >= 4:
            ops.append((PineOpcode.MSG_READ32, cur))
            cur += 4
            rem -= 4
        while rem >= 2:
            ops.append((PineOpcode.MSG_READ16, cur))
            cur += 2
            rem -= 2
        if rem == 1:
            ops.append((PineOpcode.MSG_READ8, cur))
        return b"".join(self.read_batch_raw(ops))


# ---------------------------------------------------------------------------
# Telemetry Data Structures & SLUS-21351 EE RAM Reader
# ---------------------------------------------------------------------------

Vec3 = Tuple[float, float, float]


@dataclasses.dataclass
class ObstacleVehicle:
    pvehicle_addr: int
    pos: Vec3
    vel: Vec3
    dist_m: float
    forward_dist_m: float
    lateral_offset_m: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pvehicle": f"0x{self.pvehicle_addr:08X}",
            "pos": [round(v, 2) for v in self.pos],
            "vel": [round(v, 2) for v in self.vel],
            "dist_m": round(self.dist_m, 2),
            "forward_dist_m": round(self.forward_dist_m, 2),
            "lateral_offset_m": round(self.lateral_offset_m, 2),
        }


@dataclasses.dataclass
class VehicleTelemetry:
    timestamp: float
    flow_state: int
    flow_state_name: str
    sim_state: int
    paused: bool
    world_loop_counter: int
    player_count: int
    vehicle_count: int
    player_present: bool
    pvehicle_addr: int = 0
    pos: Vec3 = (0.0, 0.0, 0.0)
    vel: Vec3 = (0.0, 0.0, 0.0)
    right: Vec3 = (1.0, 0.0, 0.0)
    up: Vec3 = (0.0, 1.0, 0.0)
    forward: Vec3 = (0.0, 0.0, 1.0)
    speed_mps: float = 0.0
    abs_speed_mps: float = 0.0
    speedometer_mps: float = 0.0
    speed_mph: float = 0.0
    forward_speed_mps: float = 0.0
    slip_angle: float = 0.0
    heading_deg: float = 0.0
    gear: Optional[int] = None
    gear_label: str = "?"
    rpm: Optional[float] = None
    nos: Optional[float] = None
    nos_engaged: bool = False
    ground_contacts: int = 0
    wrong_way: bool = False
    road_nav_valid: bool = False
    road_segment: Optional[int] = None
    road_pos: Optional[Vec3] = None
    road_forward: Optional[Vec3] = None
    road_tangent_deg: Optional[float] = None
    race_active: bool = False
    next_checkpoint_pos: Optional[Vec3] = None
    obstacles: List[ObstacleVehicle] = dataclasses.field(default_factory=list)
    read_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": round(self.timestamp, 4),
            "flow_state": self.flow_state,
            "flow_state_name": self.flow_state_name,
            "sim_state": self.sim_state,
            "paused": self.paused,
            "world_loop_counter": self.world_loop_counter,
            "player_count": self.player_count,
            "vehicle_count": self.vehicle_count,
            "player_present": self.player_present,
            "pvehicle": f"0x{self.pvehicle_addr:08X}" if self.pvehicle_addr else None,
            "pos": [round(v, 3) for v in self.pos],
            "vel": [round(v, 3) for v in self.vel],
            "forward": [round(v, 4) for v in self.forward],
            "right": [round(v, 4) for v in self.right],
            "speed_mps": round(self.speed_mps, 3),
            "abs_speed_mps": round(self.abs_speed_mps, 3),
            "speed_mph": round(self.speed_mph, 2),
            "forward_speed_mps": round(self.forward_speed_mps, 3),
            "heading_deg": round(self.heading_deg, 2),
            "gear": self.gear,
            "gear_label": self.gear_label,
            "rpm": round(self.rpm, 1) if self.rpm is not None else None,
            "nos": round(self.nos, 3) if self.nos is not None else None,
            "nos_engaged": self.nos_engaged,
            "ground_contacts": self.ground_contacts,
            "wrong_way": self.wrong_way,
            "road_nav_valid": self.road_nav_valid,
            "road_segment": self.road_segment,
            "road_pos": [round(v, 3) for v in self.road_pos] if self.road_pos else None,
            "road_forward": [round(v, 4) for v in self.road_forward] if self.road_forward else None,
            "road_tangent_deg": round(self.road_tangent_deg, 2) if self.road_tangent_deg is not None else None,
            "race_active": self.race_active,
            "next_checkpoint_pos": (
                [round(v, 3) for v in self.next_checkpoint_pos] if self.next_checkpoint_pos else None
            ),
            "obstacle_count": len(self.obstacles),
            "nearest_obstacle": self.obstacles[0].to_dict() if self.obstacles else None,
            "read_ms": round(self.read_ms, 3),
        }


def _finite_vec3(v: Vec3) -> bool:
    return all(math.isfinite(x) and abs(x) < 1e7 for x in v)


def _norm_xz(v: Vec3) -> Tuple[float, float]:
    mag = math.hypot(v[0], v[2])
    if mag < 1e-6:
        return (0.0, 0.0)
    return (v[0] / mag, v[2] / mag)


def _heading_deg_xz(v: Vec3) -> float:
    return math.degrees(math.atan2(v[0], v[2]))


class NfsmwTelemetryReader:
    """Batched EE RAM telemetry reader for NFSMW Black Edition (SLUS-21351)."""

    def __init__(self, pine: PineClient, *, max_obstacles: int = 12) -> None:
        self.pine = pine
        self.max_obstacles = max(0, int(max_obstacles))

    def read_flow_state(self) -> Tuple[int, str]:
        raw = self.pine.read_s32(ADDR_GAMEFLOW_STATE)
        return raw, GameFlowState.label(raw)

    def read_telemetry(self) -> VehicleTelemetry:
        t0 = time.monotonic()

        # Batch 1: Core global pointers & list headers (10 u32 reads in 1 packet)
        b1 = self.pine.read_batch_raw(
            [
                (PineOpcode.MSG_READ32, ADDR_GAMEFLOW_STATE),
                (PineOpcode.MSG_READ32, ADDR_SIM_SYSTEM_PTR),
                (PineOpcode.MSG_READ32, ADDR_WORLD_LOOP_COUNTER),
                (PineOpcode.MSG_READ32, ADDR_IPLAYER_LOCAL_LIST + OFF_LIST_ARRAY_PTR),
                (PineOpcode.MSG_READ32, ADDR_IPLAYER_LOCAL_LIST + OFF_LIST_COUNT),
                (PineOpcode.MSG_READ32, ADDR_IVEHICLE_PLAYERS_LIST + OFF_LIST_ARRAY_PTR),
                (PineOpcode.MSG_READ32, ADDR_IVEHICLE_PLAYERS_LIST + OFF_LIST_COUNT),
                (PineOpcode.MSG_READ32, ADDR_IVEHICLE_ALL_LIST + OFF_LIST_ARRAY_PTR),
                (PineOpcode.MSG_READ32, ADDR_IVEHICLE_ALL_LIST + OFF_LIST_COUNT),
                (PineOpcode.MSG_READ32, ADDR_GRACESTATUS_PTR),
            ]
        )
        flow_state = struct.unpack("<i", b1[0])[0]
        sim_sys_ptr = struct.unpack("<I", b1[1])[0]
        world_loop = struct.unpack("<I", b1[2])[0]
        iplayer_arr = struct.unpack("<I", b1[3])[0]
        iplayer_cnt = struct.unpack("<I", b1[4])[0]
        veh_players_arr = struct.unpack("<I", b1[5])[0]
        veh_players_cnt = struct.unpack("<I", b1[6])[0]
        veh_all_arr = struct.unpack("<I", b1[7])[0]
        veh_all_cnt = struct.unpack("<I", b1[8])[0]
        race_status_ptr = struct.unpack("<I", b1[9])[0]

        if iplayer_cnt > 16:
            iplayer_cnt = 0
        if veh_players_cnt > 16:
            veh_players_cnt = 0
        if veh_all_cnt > 64:
            veh_all_cnt = 0

        tel = VehicleTelemetry(
            timestamp=time.time(),
            flow_state=flow_state,
            flow_state_name=GameFlowState.label(flow_state),
            sim_state=0,
            paused=False,
            world_loop_counter=world_loop,
            player_count=iplayer_cnt,
            vehicle_count=veh_all_cnt,
            player_present=False,
        )

        # Batch 2: Sim::State, IPlayer[0], IVehicle[PLAYERS][0], GRaceStatus fields, IVehicle[ALL][0..N]
        ops2: List[Tuple[PineOpcode, int]] = []
        idx_sim = idx_iplayer = idx_veh_player = idx_race_state = idx_next_cp = -1
        if is_valid_ee_ptr(sim_sys_ptr):
            idx_sim = len(ops2)
            ops2.append((PineOpcode.MSG_READ32, sim_sys_ptr + OFF_SIM_STATE))
        if iplayer_cnt > 0 and is_valid_ee_ptr(iplayer_arr):
            idx_iplayer = len(ops2)
            ops2.append((PineOpcode.MSG_READ32, iplayer_arr))
        if veh_players_cnt > 0 and is_valid_ee_ptr(veh_players_arr):
            idx_veh_player = len(ops2)
            ops2.append((PineOpcode.MSG_READ32, veh_players_arr))
        if is_valid_ee_ptr(race_status_ptr):
            idx_race_state = len(ops2)
            ops2.append((PineOpcode.MSG_READ32, race_status_ptr + OFF_GRACESTATUS_RACE_STATE))
            idx_next_cp = len(ops2)
            ops2.append((PineOpcode.MSG_READ32, race_status_ptr + OFF_GRACESTATUS_NEXT_CHECKPOINT))

        obs_list_n = min(veh_all_cnt, self.max_obstacles + 1) if is_valid_ee_ptr(veh_all_arr) else 0
        idx_veh_all_start = len(ops2)
        for i in range(obs_list_n):
            ops2.append((PineOpcode.MSG_READ32, veh_all_arr + i * 4))

        b2 = self.pine.read_batch_raw(ops2) if ops2 else []
        if idx_sim >= 0:
            tel.sim_state = struct.unpack("<i", b2[idx_sim])[0]
            tel.paused = tel.sim_state == SIM_STATE_PAUSED

        iplayer_ptr = struct.unpack("<I", b2[idx_iplayer])[0] if idx_iplayer >= 0 else 0
        iveh_player_ptr = struct.unpack("<I", b2[idx_veh_player])[0] if idx_veh_player >= 0 else 0
        if idx_race_state >= 0:
            tel.race_active = struct.unpack("<i", b2[idx_race_state])[0] == 1
        next_cp_ptr = struct.unpack("<I", b2[idx_next_cp])[0] if idx_next_cp >= 0 else 0

        all_iveh_ptrs: List[int] = []
        for i in range(obs_list_n):
            ptr = struct.unpack("<I", b2[idx_veh_all_start + i])[0]
            if is_valid_ee_ptr(ptr):
                all_iveh_ptrs.append(ptr)

        # Resolve player PVehicle pointer (primary: IPlayer -> LocalPlayer -> ISimable -> PVehicle;
        # fallback: IVehicle::mList[VEHICLE_PLAYERS][0] - 0x130)
        pvehicle_ptr = 0
        if is_valid_ee_ptr(iplayer_ptr):
            local_player_ptr = iplayer_ptr + DELTA_IPLAYER_TO_LOCALPLAYER
            if is_valid_ee_ptr(local_player_ptr):
                isimable_ptr = self.pine.read_u32(local_player_ptr + OFF_LOCALPLAYER_SIMABLE)
                if is_valid_ee_ptr(isimable_ptr):
                    cand = isimable_ptr + DELTA_ISIMABLE_TO_PVEHICLE
                    if is_valid_ee_ptr(cand):
                        pvehicle_ptr = cand
        if pvehicle_ptr == 0 and is_valid_ee_ptr(iveh_player_ptr):
            cand = iveh_player_ptr + DELTA_IVEHICLE_TO_PVEHICLE
            if is_valid_ee_ptr(cand):
                pvehicle_ptr = cand

        if pvehicle_ptr == 0:
            tel.read_ms = (time.monotonic() - t0) * 1000.0
            return tel

        tel.pvehicle_addr = pvehicle_ptr

        # Collect non-player obstacle PVehicle addresses
        obs_pvehicles: List[int] = []
        for iveh in all_iveh_ptrs:
            pv = iveh + DELTA_IVEHICLE_TO_PVEHICLE
            if pv != pvehicle_ptr and is_valid_ee_ptr(pv):
                obs_pvehicles.append(pv)
                if len(obs_pvehicles) >= self.max_obstacles:
                    break

        # Batch 3: Player PVehicle fields & sub-interfaces + Next Checkpoint pos + Obstacle RigidBody ptrs
        ops3: List[Tuple[PineOpcode, int]] = [
            (PineOpcode.MSG_READ32, pvehicle_ptr + OFF_PVEHICLE_RIGIDBODY),
            (PineOpcode.MSG_READ32, pvehicle_ptr + OFF_PVEHICLE_IENGINE),
            (PineOpcode.MSG_READ32, pvehicle_ptr + OFF_PVEHICLE_IAIVEHICLE),
            (PineOpcode.MSG_READ32, pvehicle_ptr + OFF_PVEHICLE_ITRANSMISSION),
            (PineOpcode.MSG_READ32, pvehicle_ptr + OFF_PVEHICLE_SPEED_MPS),
            (PineOpcode.MSG_READ32, pvehicle_ptr + OFF_PVEHICLE_ABS_SPEED_MPS),
            (PineOpcode.MSG_READ32, pvehicle_ptr + OFF_PVEHICLE_SPEEDOMETER_MPS),
            (PineOpcode.MSG_READ32, pvehicle_ptr + OFF_PVEHICLE_SLIP_ANGLE),
        ]
        idx_cp_pos = -1
        if is_valid_ee_ptr(next_cp_ptr):
            idx_cp_pos = len(ops3)
            ops3.append((PineOpcode.MSG_READ64, next_cp_ptr + OFF_GTRIGGER_POS))
            ops3.append((PineOpcode.MSG_READ32, next_cp_ptr + OFF_GTRIGGER_POS + 8))
        idx_obs_rb = len(ops3)
        for pv in obs_pvehicles:
            ops3.append((PineOpcode.MSG_READ32, pv + OFF_PVEHICLE_RIGIDBODY))

        b3 = self.pine.read_batch_raw(ops3)
        irb_ptr = struct.unpack("<I", b3[0])[0]
        iengine_ptr = struct.unpack("<I", b3[1])[0]
        iaiveh_ptr = struct.unpack("<I", b3[2])[0]
        itrans_ptr = struct.unpack("<I", b3[3])[0]
        tel.speed_mps = struct.unpack("<f", b3[4])[0]
        tel.abs_speed_mps = struct.unpack("<f", b3[5])[0]
        tel.speedometer_mps = struct.unpack("<f", b3[6])[0]
        tel.slip_angle = struct.unpack("<f", b3[7])[0]

        if idx_cp_pos >= 0:
            cp_raw = b3[idx_cp_pos] + b3[idx_cp_pos + 1]
            cp_vec = struct.unpack("<3f", cp_raw)
            if _finite_vec3(cp_vec):
                tel.next_checkpoint_pos = cp_vec

        obs_irbs: List[Tuple[int, int]] = []
        for i, pv in enumerate(obs_pvehicles):
            o_irb = struct.unpack("<I", b3[idx_obs_rb + i])[0]
            if is_valid_ee_ptr(o_irb):
                obs_irbs.append((pv, o_irb))

        # Batch 4: Player rb_holder, EngineRacer RPM/NOS/NOS_engaged/Gear, AIVehicle mDriveToNav & wrong_way, obstacle rb_holders
        ops4: List[Tuple[PineOpcode, int]] = []
        idx_rb_holder = idx_rpm = idx_nos = idx_nos_eng = idx_gear = idx_nav = idx_ww = -1
        if is_valid_ee_ptr(irb_ptr):
            rb_ptr = irb_ptr + DELTA_IRIGIDBODY_TO_RIGIDBODY
            if is_valid_ee_ptr(rb_ptr):
                idx_rb_holder = len(ops4)
                ops4.append((PineOpcode.MSG_READ32, rb_ptr + OFF_RIGIDBODY_DATA_HOLDER))
        if is_valid_ee_ptr(iengine_ptr):
            eng_ptr = iengine_ptr + DELTA_IENGINE_TO_ENGINERACER
            if is_valid_ee_ptr(eng_ptr):
                idx_rpm = len(ops4)
                ops4.append((PineOpcode.MSG_READ32, eng_ptr + OFF_ENGINERACER_RPM))
                idx_nos = len(ops4)
                ops4.append((PineOpcode.MSG_READ32, eng_ptr + OFF_ENGINERACER_NOS))
                idx_nos_eng = len(ops4)
                ops4.append((PineOpcode.MSG_READ32, eng_ptr + OFF_ENGINERACER_NOS_ENGAGED))
        if is_valid_ee_ptr(itrans_ptr):
            tr_ptr = itrans_ptr + DELTA_ITRANS_TO_ENGINERACER
            if is_valid_ee_ptr(tr_ptr):
                idx_gear = len(ops4)
                ops4.append((PineOpcode.MSG_READ32, tr_ptr + OFF_ENGINERACER_GEAR))
        if is_valid_ee_ptr(iaiveh_ptr):
            ai_ptr = iaiveh_ptr + DELTA_IAIVEHICLE_TO_AIVEHICLE
            if is_valid_ee_ptr(ai_ptr):
                idx_nav = len(ops4)
                ops4.append((PineOpcode.MSG_READ32, ai_ptr + OFF_AIVEHICLE_DRIVE_TO_NAV))
                idx_ww = len(ops4)
                ops4.append((PineOpcode.MSG_READ32, ai_ptr + OFF_AIVEHICLEHUMAN_WRONG_WAY))

        idx_obs_holders = len(ops4)
        valid_obs_irbs: List[Tuple[int, int]] = []
        for pv, o_irb in obs_irbs:
            o_rb = o_irb + DELTA_IRIGIDBODY_TO_RIGIDBODY
            if is_valid_ee_ptr(o_rb):
                valid_obs_irbs.append((pv, o_rb))
                ops4.append((PineOpcode.MSG_READ32, o_rb + OFF_RIGIDBODY_DATA_HOLDER))

        b4 = self.pine.read_batch_raw(ops4) if ops4 else []
        rb_holder_ptr = struct.unpack("<I", b4[idx_rb_holder])[0] if idx_rb_holder >= 0 else 0
        if idx_rpm >= 0:
            rpm_val = struct.unpack("<f", b4[idx_rpm])[0]
            if math.isfinite(rpm_val) and 0.0 <= rpm_val <= 25000.0:
                tel.rpm = rpm_val
        if idx_nos >= 0:
            nos_val = struct.unpack("<f", b4[idx_nos])[0]
            if math.isfinite(nos_val) and -0.1 <= nos_val <= 2.0:
                tel.nos = max(0.0, min(1.0, nos_val))
        if idx_nos_eng >= 0:
            nos_eng_val = struct.unpack("<f", b4[idx_nos_eng])[0]
            if math.isfinite(nos_eng_val) and nos_eng_val >= 1.0:
                tel.nos_engaged = True
        if idx_gear >= 0:
            g_val = struct.unpack("<i", b4[idx_gear])[0]
            if 0 <= g_val <= 10:
                tel.gear = g_val
                tel.gear_label = gear_to_label(g_val)
        nav_ptr = struct.unpack("<I", b4[idx_nav])[0] if idx_nav >= 0 else 0
        if idx_ww >= 0:
            tel.wrong_way = struct.unpack("<i", b4[idx_ww])[0] != 0

        obs_holders: List[Tuple[int, int]] = []
        for i, (pv, _) in enumerate(valid_obs_irbs):
            h = struct.unpack("<I", b4[idx_obs_holders + i])[0]
            if is_valid_ee_ptr(h):
                obs_holders.append((pv, h))

        # Batch 5: Player rb_state ptr, WRoadNav fields, obstacle rb_state ptrs
        ops5: List[Tuple[PineOpcode, int]] = []
        idx_rb_state = idx_nav_valid = idx_nav_seg = idx_nav_pos = idx_nav_fwd = -1
        if is_valid_ee_ptr(rb_holder_ptr):
            idx_rb_state = len(ops5)
            ops5.append((PineOpcode.MSG_READ32, rb_holder_ptr))
        if is_valid_ee_ptr(nav_ptr):
            idx_nav_valid = len(ops5)
            ops5.append((PineOpcode.MSG_READ32, nav_ptr + OFF_ROADNAV_VALID))
            idx_nav_seg = len(ops5)
            ops5.append((PineOpcode.MSG_READ16, nav_ptr + OFF_ROADNAV_SEG_IND))
            idx_nav_pos = len(ops5)
            ops5.append((PineOpcode.MSG_READ64, nav_ptr + OFF_ROADNAV_POS))
            ops5.append((PineOpcode.MSG_READ32, nav_ptr + OFF_ROADNAV_POS + 8))
            idx_nav_fwd = len(ops5)
            ops5.append((PineOpcode.MSG_READ64, nav_ptr + OFF_ROADNAV_FORWARD))
            ops5.append((PineOpcode.MSG_READ32, nav_ptr + OFF_ROADNAV_FORWARD + 8))

        idx_obs_states = len(ops5)
        for _, h in obs_holders:
            ops5.append((PineOpcode.MSG_READ32, h))

        b5 = self.pine.read_batch_raw(ops5) if ops5 else []
        rb_state_ptr = struct.unpack("<I", b5[idx_rb_state])[0] if idx_rb_state >= 0 else 0
        if idx_nav_valid >= 0:
            nav_valid = struct.unpack("<i", b5[idx_nav_valid])[0] != 0
            seg_ind = struct.unpack("<h", b5[idx_nav_seg])[0]
            npos = struct.unpack("<3f", b5[idx_nav_pos] + b5[idx_nav_pos + 1])
            nfwd = struct.unpack("<3f", b5[idx_nav_fwd] + b5[idx_nav_fwd + 1])
            fwd_mag = math.hypot(nfwd[0], nfwd[2])
            if nav_valid and _finite_vec3(npos) and _finite_vec3(nfwd) and fwd_mag > 0.1:
                tel.road_nav_valid = True
                tel.road_segment = int(seg_ind)
                tel.road_pos = npos
                tel.road_forward = (nfwd[0] / fwd_mag, nfwd[1], nfwd[2] / fwd_mag)
                tel.road_tangent_deg = _heading_deg_xz(tel.road_forward)

        obs_states: List[Tuple[int, int]] = []
        for i, (pv, _) in enumerate(obs_holders):
            st = struct.unpack("<I", b5[idx_obs_states + i])[0]
            if is_valid_ee_ptr(st):
                obs_states.append((pv, st))

        if not is_valid_ee_ptr(rb_state_ptr):
            tel.read_ms = (time.monotonic() - t0) * 1000.0
            return tel

        # Batch 6: Player RigidBody::State (pos, linvel, contacts, right, up, forward) + obstacle pos/vel
        # All packed into 64-bit reads for minimal packet size!
        ops6: List[Tuple[PineOpcode, int]] = [
            # pos (0x10..0x1B) + vel (0x20..0x2B)
            (PineOpcode.MSG_READ64, rb_state_ptr + OFF_RBSTATE_POS),
            (PineOpcode.MSG_READ32, rb_state_ptr + OFF_RBSTATE_POS + 8),
            (PineOpcode.MSG_READ64, rb_state_ptr + OFF_RBSTATE_LINVEL),
            (PineOpcode.MSG_READ32, rb_state_ptr + OFF_RBSTATE_LINVEL + 8),
            (PineOpcode.MSG_READ8, rb_state_ptr + OFF_RBSTATE_CONTACTS),
            # right (0x80..0x8B), up (0x90..0x9B), forward (0xA0..0xAB)
            (PineOpcode.MSG_READ64, rb_state_ptr + OFF_RBSTATE_RIGHT),
            (PineOpcode.MSG_READ32, rb_state_ptr + OFF_RBSTATE_RIGHT + 8),
            (PineOpcode.MSG_READ64, rb_state_ptr + OFF_RBSTATE_UP),
            (PineOpcode.MSG_READ32, rb_state_ptr + OFF_RBSTATE_UP + 8),
            (PineOpcode.MSG_READ64, rb_state_ptr + OFF_RBSTATE_FORWARD),
            (PineOpcode.MSG_READ32, rb_state_ptr + OFF_RBSTATE_FORWARD + 8),
        ]
        idx_obs_kin = len(ops6)
        for _, st in obs_states:
            ops6.append((PineOpcode.MSG_READ64, st + OFF_RBSTATE_POS))
            ops6.append((PineOpcode.MSG_READ32, st + OFF_RBSTATE_POS + 8))
            ops6.append((PineOpcode.MSG_READ64, st + OFF_RBSTATE_LINVEL))
            ops6.append((PineOpcode.MSG_READ32, st + OFF_RBSTATE_LINVEL + 8))

        b6 = self.pine.read_batch_raw(ops6)
        pos = struct.unpack("<3f", b6[0] + b6[1])
        vel = struct.unpack("<3f", b6[2] + b6[3])
        contacts = struct.unpack("<b", b6[4])[0]
        right = struct.unpack("<3f", b6[5] + b6[6])
        up = struct.unpack("<3f", b6[7] + b6[8])
        forward = struct.unpack("<3f", b6[9] + b6[10])

        if _finite_vec3(pos) and _finite_vec3(vel) and _finite_vec3(forward):
            tel.player_present = True
            tel.pos = pos
            tel.vel = vel
            tel.ground_contacts = int(contacts)
            tel.right = right if _finite_vec3(right) else (1.0, 0.0, 0.0)
            tel.up = up if _finite_vec3(up) else (0.0, 1.0, 0.0)
            tel.forward = forward
            vel_speed = math.sqrt(vel[0] ** 2 + vel[1] ** 2 + vel[2] ** 2)
            if not math.isfinite(tel.abs_speed_mps) or abs(tel.abs_speed_mps) > 300.0:
                tel.abs_speed_mps = vel_speed
            if not math.isfinite(tel.speed_mps) or abs(tel.speed_mps) > 300.0:
                tel.speed_mps = vel_speed
            tel.speed_mph = abs(tel.abs_speed_mps) * MPS_TO_MPH
            tel.forward_speed_mps = vel[0] * forward[0] + vel[1] * forward[1] + vel[2] * forward[2]
            tel.heading_deg = _heading_deg_xz(forward)
            # Defensive guard: if RPM reads 0 while moving > 10 mph, EngineRacer read was stale/invalid;
            # clear tel.nos so a bad 0.0 read never silently locks out NOS activation.
            if tel.rpm == 0.0 and tel.speed_mph > 10.0:
                tel.nos = None

            # Decode nearby obstacle vehicles in player's local frame (forward, right)
            obstacles: List[ObstacleVehicle] = []
            for i, (pv, _) in enumerate(obs_states):
                base = idx_obs_kin + i * 4
                opos = struct.unpack("<3f", b6[base] + b6[base + 1])
                ovel = struct.unpack("<3f", b6[base + 2] + b6[base + 3])
                if not (_finite_vec3(opos) and _finite_vec3(ovel)):
                    continue
                dx = opos[0] - pos[0]
                dy = opos[1] - pos[1]
                dz = opos[2] - pos[2]
                if abs(dy) > 8.0:
                    continue
                dist = math.hypot(dx, dz)
                if dist < 0.2 or dist > 150.0:
                    continue
                fwd_d = dx * forward[0] + dz * forward[2]
                lat_d = dx * tel.right[0] + dz * tel.right[2]
                obstacles.append(
                    ObstacleVehicle(
                        pvehicle_addr=pv,
                        pos=opos,
                        vel=ovel,
                        dist_m=dist,
                        forward_dist_m=fwd_d,
                        lateral_offset_m=lat_d,
                    )
                )
            obstacles.sort(key=lambda o: o.dist_m)
            tel.obstacles = obstacles

        tel.read_ms = (time.monotonic() - t0) * 1000.0
        return tel


# ---------------------------------------------------------------------------
# Input Sink Abstraction & Persistent macOS key_driver Session
# ---------------------------------------------------------------------------


class InputSink:
    """Abstract real-time keyboard state sink for closed-loop driving."""

    def set_keys(self, keys: Iterable[str]) -> None:
        raise NotImplementedError

    def tap(self, keys: str, ms: int = 120) -> None:
        raise NotImplementedError

    def trigger_f8(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class NullInputSink(InputSink):
    """Recording no-op input sink for dry runs and unit tests."""

    def __init__(self) -> None:
        self.history: List[Tuple[str, Any]] = []
        self.current_keys: Tuple[str, ...] = ()

    def set_keys(self, keys: Iterable[str]) -> None:
        normalized = tuple(sorted({k.strip().lower() for k in keys if k.strip()}))
        if normalized != self.current_keys:
            self.current_keys = normalized
            self.history.append(("set", normalized))

    def tap(self, keys: str, ms: int = 120) -> None:
        self.history.append(("tap", (keys, ms)))

    def trigger_f8(self) -> None:
        self.history.append(("f8", None))


class KeyDriverSession(InputSink):
    """Persistent `key_driver --pid <pid> --stdin` session for zero-jitter 20 Hz driving."""

    def __init__(
        self,
        key_driver_bin: Path,
        pid: int,
        *,
        tap_mode: str = "auto",
        focus_wait_ms: int = 120,
    ) -> None:
        self.key_driver_bin = Path(key_driver_bin)
        self.pid = int(pid)
        self.tap_mode = tap_mode
        self.focus_wait_ms = int(focus_wait_ms)
        self._proc: Optional[subprocess.Popen[str]] = None
        self._current_keys: Tuple[str, ...] = ()

    def start(self, ready_timeout: float = 5.0) -> str:
        if not self.key_driver_bin.is_file():
            raise FileNotFoundError(f"key_driver binary not found: {self.key_driver_bin}")
        cmd = [
            str(self.key_driver_bin),
            "--pid",
            str(self.pid),
            "--tap-mode",
            self.tap_mode,
            "--focus-wait-ms",
            str(self.focus_wait_ms),
            "--stdin",
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert self._proc.stdout is not None
        deadline = time.monotonic() + ready_timeout
        line = ""
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                err = self._proc.stderr.read() if self._proc.stderr else ""
                raise RuntimeError(f"key_driver exited early (rc={self._proc.returncode}): {err.strip()}")
            line = self._proc.stdout.readline().strip()
            if line.startswith("READY"):
                return line
        self.close()
        raise TimeoutError("Timed out waiting for key_driver --stdin READY banner")

    def _send(self, line: str) -> None:
        if self._proc is None or self._proc.poll() is not None or self._proc.stdin is None:
            return
        try:
            self._proc.stdin.write(line + "\n")
            self._proc.stdin.flush()
        except BrokenPipeError:
            pass

    def set_keys(self, keys: Iterable[str]) -> None:
        normalized = tuple(sorted({k.strip().lower() for k in keys if k.strip()}))
        if normalized == self._current_keys:
            return
        self._current_keys = normalized
        if not normalized:
            self._send("release")
        else:
            self._send("set " + "+".join(normalized))

    def tap(self, keys: str, ms: int = 120) -> None:
        self._send(f"tap {keys.strip().lower()} {int(ms)}")

    def trigger_f8(self) -> None:
        self._send("f8")

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            self._send("quit")
            self._proc.wait(timeout=2.0)
        except Exception:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1.5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None
        self._current_keys = ()


# ---------------------------------------------------------------------------
# Closed-Loop Driving Autopilot (Road-Tangent PD+PWM + Obstacle Avoidance + Stuck Recovery)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class AutopilotConfig:
    hz: float = 20.0
    kp_steer: float = 1.65
    kd_steer: float = 0.30
    center_gain: float = 0.055
    checkpoint_blend: float = 0.25
    steer_deadband_base: float = 0.10
    steer_deadband_highspeed: float = 0.16
    stuck_speed_mps: float = 1.5
    stuck_timeout_sec: float = 0.65
    impact_drop_mps: float = 8.0
    reverse_duration_sec: float = 1.10
    turnout_duration_sec: float = 0.45
    wrong_way_timeout_sec: float = 1.00
    obstacle_lookahead_m: float = 32.0
    obstacle_corridor_m: float = 3.6
    obstacle_avoid_gain: float = 1.35
    enable_nos: bool = True
    nos_min_speed_mph: float = 40.0
    nos_max_err_deg: float = 12.0
    nos_max_road_offset_m: float = 4.5
    nos_burst_sec: float = 1.2
    nos_abort_err_deg: float = 18.0
    nos_abort_level: float = 0.10
    high_offset_caution_m: float = 6.0
    tangent_jump_caution_deg: float = 35.0
    caution_coast_speed_mph: float = 45.0
    caution_brake_speed_mph: float = 62.0
    caution_hold_sec: float = 0.45
    obstacle_dodge_cooldown_sec: float = 3.0
    verify_reverse_demo: bool = True
    reverse_demo_min_distance_m: float = 40.0
    reverse_demo_max_road_offset_m: float = 4.0
    reverse_demo_hold_sec: float = 1.8
    reverse_demo_min_rev_sec: float = 0.8
    reverse_demo_target_mph: float = 15.0
    reverse_demo_max_total_sec: float = 4.8
    launch_grace_sec: float = 2.2
    countdown_grace_sec: float = 4.5
    # Verified against retail GLOBAL/ATTRIBUTES.BIN VLT controller/drive + drive_analog (PS2 Config 1):
    #   Cross  (k) = GAMEACTION_GAS
    #   Square (j) = GAMEACTION_BRAKE (brake & reverse)
    #   L1     (q) = GAMEACTION_NOS
    #   R1     (e) = GAMEACTION_HANDBRAKE   (NEVER hold during straightaways/sprints!)
    #   Circle (l) = GAMEACTION_GAMEBREAKER (Speedbreaker — NOT NOS!)
    #   ALX_L  (a) = GAMEACTION_STEERLEFT,  ALX_R (d) = GAMEACTION_STEERRIGHT
    throttle_key: str = "k"
    brake_reverse_key: str = "j"
    steer_left_key: str = "a"
    steer_right_key: str = "d"
    nos_key: str = "q"


@dataclasses.dataclass
class ControlDecision:
    mode: str  # WAIT_RACE | DRIVE | STUCK_REVERSE | STUCK_TURN_OUT | WRONG_WAY_UTURN
    keys: Tuple[str, ...]
    steer_cmd: float  # [-1.0 (left), +1.0 (right)]
    heading_error_deg: float
    obstacle_bias: float
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "keys": list(self.keys),
            "steer_cmd": round(self.steer_cmd, 4),
            "heading_error_deg": round(self.heading_error_deg, 2),
            "obstacle_bias": round(self.obstacle_bias, 4),
            "reason": self.reason,
        }


class DrivingAutopilot:
    """20 Hz closed-loop steering, throttle, obstacle avoidance, and stuck-recovery controller."""

    def __init__(self, config: Optional[AutopilotConfig] = None) -> None:
        self.cfg = config or AutopilotConfig()
        self.mode: str = "DRIVE"
        self._mode_timer: float = 0.0
        self._stuck_timer: float = 0.0
        self._wrong_way_timer: float = 0.0
        self._reverse_steer_sign: float = -1.0
        self._prev_err_rad: float = 0.0
        self._prev_speed_mps: float = 0.0
        self._prev_pos: Optional[Vec3] = None
        self._pwm_accum: float = 0.0
        self._racing_elapsed_sec: float = 0.0
        self._launch_grace_timer: float = 0.0
        self._nos_latch_timer: float = 0.0
        self._caution_timer: float = 0.0
        self._prev_road_tangent_deg: Optional[float] = None
        self._active_dodge_obs: set[int] = set()
        self._dodge_obs_last_episode: Dict[int, float] = {}
        self._reverse_demo_done: bool = False
        self._reverse_demo_rev_timer: float = 0.0

        # Run telemetry aggregates
        self.ticks: int = 0
        self.racing_ticks: int = 0
        self.distance_traveled_m: float = 0.0
        self.max_speed_mph: float = 0.0
        self.max_reverse_speed_mph: float = 0.0
        self._speed_mph_sum: float = 0.0
        self.stuck_recoveries: int = 0
        self.wrong_way_recoveries: int = 0
        self.reverse_verified: bool = False
        self.reverse_demo_completed: bool = False
        self.nos_activated_ticks: int = 0
        self.nos_engaged_ticks: int = 0
        self.nos_consumed: bool = False
        self._prev_nos: Optional[float] = None
        self.obstacles_avoided: int = 0
        self.flow_states_seen: List[str] = []

    def _compute_desired_dir_xz(self, tel: VehicleTelemetry) -> Tuple[Tuple[float, float], str]:
        """Fuse WRoadNav road centerline+tangent and GRaceStatus next checkpoint into a unit XZ vector."""
        des_x, des_z = 0.0, 0.0
        source = "heading"

        if tel.road_nav_valid and tel.road_forward is not None:
            rfx, rfz = _norm_xz(tel.road_forward)
            des_x, des_z = rfx, rfz
            source = "road_nav"
            if tel.road_pos is not None:
                # Lookahead point along road centerline pulls car smoothly back to center of road
                lookahead = max(10.0, min(35.0, abs(tel.abs_speed_mps) * 0.65))
                tx = tel.road_pos[0] + rfx * lookahead - tel.pos[0]
                tz = tel.road_pos[2] + rfz * lookahead - tel.pos[2]
                tmag = math.hypot(tx, tz)
                if tmag > 1.0:
                    # Blend centerline lookahead with pure tangent so far-off-road snaps back gently
                    des_x = 0.65 * rfx + 0.35 * (tx / tmag)
                    des_z = 0.65 * rfz + 0.35 * (tz / tmag)

            if tel.next_checkpoint_pos is not None:
                cx = tel.next_checkpoint_pos[0] - tel.pos[0]
                cz = tel.next_checkpoint_pos[2] - tel.pos[2]
                cdist = math.hypot(cx, cz)
                if 2.0 < cdist < 250.0:
                    cnx, cnz = cx / cdist, cz / cdist
                    # Only blend checkpoint if roughly ahead of road tangent (avoid pulling backward)
                    if (cnx * rfx + cnz * rfz) > -0.2:
                        w = self.cfg.checkpoint_blend
                        des_x = (1.0 - w) * des_x + w * cnx
                        des_z = (1.0 - w) * des_z + w * cnz
                        source = "road_nav+checkpoint"
        elif tel.next_checkpoint_pos is not None:
            cx = tel.next_checkpoint_pos[0] - tel.pos[0]
            cz = tel.next_checkpoint_pos[2] - tel.pos[2]
            cdist = math.hypot(cx, cz)
            if cdist > 1.0:
                des_x, des_z = cx / cdist, cz / cdist
                source = "checkpoint"
        else:
            des_x, des_z = _norm_xz(tel.forward)

        mag = math.hypot(des_x, des_z)
        if mag < 1e-6:
            return _norm_xz(tel.forward), "fallback_forward"
        return (des_x / mag, des_z / mag), source

    def _compute_obstacle_bias(self, tel: VehicleTelemetry) -> Tuple[float, bool, set[int]]:
        """Compute repulsive lateral steering bias [-1, +1] and active obstacle vehicle addresses."""
        bias = 0.0
        dodging = False
        active_addrs: set[int] = set()
        for obs in tel.obstacles:
            if not (1.5 < obs.forward_dist_m < self.cfg.obstacle_lookahead_m):
                continue
            if abs(obs.lateral_offset_m) >= self.cfg.obstacle_corridor_m:
                continue
            dodging = True
            active_addrs.add(obs.pvehicle_addr)
            # If obstacle is slightly to our right (lat >= 0), steer left (-); if on our left (lat < 0), steer right (+)
            away_sign = -1.0 if obs.lateral_offset_m >= 0.0 else 1.0
            lat_factor = (self.cfg.obstacle_corridor_m - abs(obs.lateral_offset_m)) / self.cfg.obstacle_corridor_m
            dist_factor = min(1.0, 12.0 / max(obs.forward_dist_m, 4.0))
            bias += away_sign * self.cfg.obstacle_avoid_gain * lat_factor * dist_factor
        return max(-0.95, min(0.95, bias)), dodging, active_addrs

    def _pwm_steer_key(self, steer_cmd: float, speed_mph: float) -> Optional[str]:
        """Convert continuous steer_cmd in [-1, 1] into digital left/right/none via sigma-delta PWM."""
        deadband = (
            self.cfg.steer_deadband_highspeed
            if speed_mph > 75.0
            else self.cfg.steer_deadband_base
        )
        if abs(steer_cmd) < deadband:
            self._pwm_accum *= 0.5
            return None
        if abs(steer_cmd) >= 0.72:
            self._pwm_accum = 0.0
            return self.cfg.steer_right_key if steer_cmd > 0.0 else self.cfg.steer_left_key

        # Sigma-delta modulator gives smooth fractional steering duty cycle at 20 Hz
        self._pwm_accum += steer_cmd
        if self._pwm_accum >= 0.5:
            self._pwm_accum -= 1.0
            return self.cfg.steer_right_key
        if self._pwm_accum <= -0.5:
            self._pwm_accum += 1.0
            return self.cfg.steer_left_key
        return None

    def step(self, tel: VehicleTelemetry, dt: Optional[float] = None) -> ControlDecision:
        dt = float(dt) if dt is not None and dt > 0 else (1.0 / self.cfg.hz)
        self.ticks += 1
        if tel.flow_state_name not in self.flow_states_seen:
            self.flow_states_seen.append(tel.flow_state_name)

        if tel.flow_state != GameFlowState.RACING or tel.paused or not tel.player_present:
            self._stuck_timer = 0.0
            self._wrong_way_timer = 0.0
            self._prev_pos = None
            return ControlDecision(
                mode="WAIT_RACE",
                keys=(),
                steer_cmd=0.0,
                heading_error_deg=0.0,
                obstacle_bias=0.0,
                reason=f"flow={tel.flow_state_name} paused={tel.paused} player={tel.player_present}",
            )

        self.racing_ticks += 1
        self._racing_elapsed_sec += dt
        if self._launch_grace_timer > 0.0:
            self._launch_grace_timer = max(0.0, self._launch_grace_timer - dt)
        if self._nos_latch_timer > 0.0:
            self._nos_latch_timer = max(0.0, self._nos_latch_timer - dt)
        if self._caution_timer > 0.0:
            self._caution_timer = max(0.0, self._caution_timer - dt)

        if self._prev_pos is not None:
            step_dist = math.hypot(tel.pos[0] - self._prev_pos[0], tel.pos[2] - self._prev_pos[2])
            if step_dist < 40.0:  # ignore teleports/respawns
                self.distance_traveled_m += step_dist
        self._prev_pos = tel.pos

        if tel.speed_mph > self.max_speed_mph:
            self.max_speed_mph = tel.speed_mph
        self._speed_mph_sum += tel.speed_mph

        rev_mph = max(0.0, -tel.forward_speed_mps * MPS_TO_MPH)
        if rev_mph > self.max_reverse_speed_mph:
            self.max_reverse_speed_mph = rev_mph
        if tel.forward_speed_mps < -0.5 or tel.gear == 0:
            self.reverse_verified = True
        if tel.nos_engaged:
            self.nos_engaged_ticks += 1
        if tel.nos is not None:
            if self._prev_nos is not None and (self._prev_nos - tel.nos) > 0.005:
                self.nos_consumed = True
            self._prev_nos = tel.nos

        # Compute car basis in XZ
        fx, fz = _norm_xz(tel.forward)
        rx, rz = _norm_xz(tel.right)
        if math.hypot(rx, rz) < 0.5:
            rx, rz = -fz, fx

        (des_x, des_z), target_src = self._compute_desired_dir_xz(tel)
        fwd_dot = fx * des_x + fz * des_z
        right_dot = rx * des_x + rz * des_z
        err_rad = math.atan2(right_dot, fwd_dot)  # >0: target is to right -> steer right
        err_deg = math.degrees(err_rad)

        # Lateral offset from road centerline (meters) & abrupt road-tangent jump detection
        road_offset_m = 0.0
        tangent_jump_deg = 0.0
        if tel.road_nav_valid and tel.road_pos is not None:
            road_offset_m = abs(
                (tel.road_pos[0] - tel.pos[0]) * rx + (tel.road_pos[2] - tel.pos[2]) * rz
            )
        if tel.road_nav_valid and tel.road_tangent_deg is not None:
            if self._prev_road_tangent_deg is not None:
                diff = abs((tel.road_tangent_deg - self._prev_road_tangent_deg + 180.0) % 360.0 - 180.0)
                tangent_jump_deg = diff
            self._prev_road_tangent_deg = tel.road_tangent_deg
        else:
            self._prev_road_tangent_deg = None

        if (
            (tel.road_nav_valid and road_offset_m > self.cfg.high_offset_caution_m)
            or tangent_jump_deg > self.cfg.tangent_jump_caution_deg
        ):
            self._caution_timer = max(self._caution_timer, self.cfg.caution_hold_sec)

        # Derivative term for yaw damping
        d_err = (err_rad - self._prev_err_rad) / max(dt, 1e-3)
        d_err = max(-4.0, min(4.0, d_err))
        self._prev_err_rad = err_rad

        # Rising-edge unique obstacle-dodge episode counting (per vehicle pointer + cooldown)
        obs_bias, dodging, active_obs_addrs = self._compute_obstacle_bias(tel)
        for addr in active_obs_addrs:
            if addr not in self._active_dodge_obs:
                last_t = self._dodge_obs_last_episode.get(addr, -1e9)
                if (self._racing_elapsed_sec - last_t) >= self.cfg.obstacle_dodge_cooldown_sec:
                    self.obstacles_avoided += 1
                    self._dodge_obs_last_episode[addr] = self._racing_elapsed_sec
        self._active_dodge_obs = active_obs_addrs

        speed_drop = self._prev_speed_mps - tel.abs_speed_mps
        self._prev_speed_mps = tel.abs_speed_mps

        # --- Open-Road Reverse Verification Maneuver (brake -> R gear 15 mph -> rocket launch) ---
        if self.mode == "REVERSE_DEMO":
            self._mode_timer += dt
            in_reverse = (tel.gear == 0) or (tel.forward_speed_mps <= 0.4)
            if in_reverse:
                self._reverse_demo_rev_timer += dt
            reached_target = (
                tel.gear == 0
                and rev_mph >= self.cfg.reverse_demo_target_mph
                and self._reverse_demo_rev_timer >= self.cfg.reverse_demo_min_rev_sec
            )
            hold_done = self._reverse_demo_rev_timer >= self.cfg.reverse_demo_hold_sec
            timed_out = self._mode_timer >= self.cfg.reverse_demo_max_total_sec

            if reached_target or hold_done or timed_out:
                self.mode = "DRIVE"
                self._mode_timer = 0.0
                self._stuck_timer = 0.0
                self._reverse_demo_done = True
                self.reverse_demo_completed = True
                self._launch_grace_timer = self.cfg.launch_grace_sec
                launch_keys = [self.cfg.throttle_key]
                if self.cfg.enable_nos and (tel.nos is None or tel.nos > self.cfg.nos_abort_level):
                    launch_keys.append(self.cfg.nos_key)
                    self._nos_latch_timer = self.cfg.nos_burst_sec
                    self.nos_activated_ticks += 1
                return ControlDecision(
                    mode="DRIVE",
                    keys=tuple(launch_keys),
                    steer_cmd=0.0,
                    heading_error_deg=err_deg,
                    obstacle_bias=obs_bias,
                    reason=(
                        f"reverse_demo_complete rev_mph={self.max_reverse_speed_mph:.1f} "
                        f"gear={tel.gear_label} -> launch"
                    ),
                )

            return ControlDecision(
                mode="REVERSE_DEMO",
                keys=(self.cfg.brake_reverse_key,),
                steer_cmd=0.0,
                heading_error_deg=err_deg,
                obstacle_bias=0.0,
                reason=(
                    f"reverse_demo t={self._mode_timer:.2f}s rev_t={self._reverse_demo_rev_timer:.2f}s "
                    f"gear={tel.gear_label} rev_mph={rev_mph:.1f}"
                ),
            )

        # --- Recovery State Machine ---
        if self.mode == "STUCK_REVERSE":
            self._mode_timer += dt
            keys = [self.cfg.brake_reverse_key]
            # Counter-steer while reversing so car's nose swings toward desired direction
            steer_key = self.cfg.steer_right_key if self._reverse_steer_sign > 0 else self.cfg.steer_left_key
            keys.append(steer_key)
            if self._mode_timer >= self.cfg.reverse_duration_sec:
                self.mode = "STUCK_TURN_OUT"
                self._mode_timer = 0.0
            return ControlDecision(
                mode="STUCK_REVERSE",
                keys=tuple(keys),
                steer_cmd=self._reverse_steer_sign,
                heading_error_deg=err_deg,
                obstacle_bias=obs_bias,
                reason=f"reversing t={self._mode_timer:.2f}s fwd_v={tel.forward_speed_mps:.1f}",
            )

        if self.mode == "STUCK_TURN_OUT":
            self._mode_timer += dt
            turn_sign = 1.0 if err_rad >= 0 else -1.0
            steer_key = self.cfg.steer_right_key if turn_sign > 0 else self.cfg.steer_left_key
            keys = [self.cfg.throttle_key, steer_key]
            if self._mode_timer >= self.cfg.turnout_duration_sec or abs(err_deg) < 18.0:
                self.mode = "DRIVE"
                self._mode_timer = 0.0
                self._stuck_timer = 0.0
                self._launch_grace_timer = 1.0
            return ControlDecision(
                mode="STUCK_TURN_OUT",
                keys=tuple(keys),
                steer_cmd=turn_sign,
                heading_error_deg=err_deg,
                obstacle_bias=obs_bias,
                reason=f"turnout t={self._mode_timer:.2f}s err={err_deg:+.1f}deg",
            )

        if self.mode == "WRONG_WAY_UTURN":
            self._mode_timer += dt
            keys = [self.cfg.brake_reverse_key, self.cfg.steer_right_key]
            if fwd_dot > 0.35 or self._mode_timer >= 1.8:
                self.mode = "STUCK_TURN_OUT"
                self._mode_timer = 0.0
                self._wrong_way_timer = 0.0
            return ControlDecision(
                mode="WRONG_WAY_UTURN",
                keys=tuple(keys),
                steer_cmd=1.0,
                heading_error_deg=err_deg,
                obstacle_bias=obs_bias,
                reason=f"uturn fwd_dot={fwd_dot:+.2f}",
            )

        # --- Normal DRIVE mode checks ---
        # 0. Trigger open-road Reverse (R gear) verification maneuver once past min distance on clear road
        if (
            self.cfg.verify_reverse_demo
            and not self._reverse_demo_done
            and self.distance_traveled_m >= self.cfg.reverse_demo_min_distance_m
            and not dodging
            and abs(err_deg) < 25.0
            and (not tel.road_nav_valid or road_offset_m < self.cfg.reverse_demo_max_road_offset_m)
        ):
            self.mode = "REVERSE_DEMO"
            self._mode_timer = 0.0
            self._reverse_demo_rev_timer = 0.0
            return ControlDecision(
                mode="REVERSE_DEMO",
                keys=(self.cfg.brake_reverse_key,),
                steer_cmd=0.0,
                heading_error_deg=err_deg,
                obstacle_bias=0.0,
                reason=f"start_reverse_demo dist={self.distance_traveled_m:.1f}m offset={road_offset_m:.1f}m",
            )

        # 1. Wrong-way check
        is_wrong_way = tel.wrong_way or (tel.road_nav_valid and fwd_dot < -0.45)
        if is_wrong_way:
            self._wrong_way_timer += dt
            if self._wrong_way_timer >= self.cfg.wrong_way_timeout_sec:
                self.mode = "WRONG_WAY_UTURN"
                self._mode_timer = 0.0
                self.wrong_way_recoveries += 1
                return ControlDecision(
                    mode="WRONG_WAY_UTURN",
                    keys=(self.cfg.brake_reverse_key, self.cfg.steer_right_key),
                    steer_cmd=1.0,
                    heading_error_deg=err_deg,
                    obstacle_bias=obs_bias,
                    reason="triggered wrong-way recovery",
                )
        else:
            self._wrong_way_timer = 0.0

        # 2. Wall-impact / stuck detection (suppressed during start countdown & post-reverse launch grace)
        stuck_armed = (
            self._launch_grace_timer <= 0.0
            and (self.max_speed_mph > 5.0 or self._racing_elapsed_sec >= self.cfg.countdown_grace_sec)
        )
        hard_impact = stuck_armed and speed_drop >= self.cfg.impact_drop_mps and tel.abs_speed_mps < 2.5
        if stuck_armed and tel.abs_speed_mps < self.cfg.stuck_speed_mps:
            self._stuck_timer += dt
        else:
            self._stuck_timer = max(0.0, self._stuck_timer - dt * 2.0)

        if stuck_armed and (self._stuck_timer >= self.cfg.stuck_timeout_sec or hard_impact):
            self.mode = "STUCK_REVERSE"
            self._mode_timer = 0.0
            self._stuck_timer = 0.0
            self.stuck_recoveries += 1
            # Counter-steer opposite the desired heading so backing up rotates the nose toward the road
            self._reverse_steer_sign = -1.0 if err_rad >= 0.0 else 1.0
            steer_key = self.cfg.steer_right_key if self._reverse_steer_sign > 0 else self.cfg.steer_left_key
            return ControlDecision(
                mode="STUCK_REVERSE",
                keys=(self.cfg.brake_reverse_key, steer_key),
                steer_cmd=self._reverse_steer_sign,
                heading_error_deg=err_deg,
                obstacle_bias=obs_bias,
                reason="hard_wall_impact" if hard_impact else "stuck_low_speed",
            )

        # 3. PD steering + obstacle avoidance fusion + high-offset/sharp-turn speed modulation
        caution_active = self._caution_timer > 0.0
        steer_gain = 1.25 if (tel.road_nav_valid and road_offset_m > self.cfg.high_offset_caution_m) else 1.0
        raw_steer = steer_gain * (self.cfg.kp_steer * err_rad + self.cfg.kd_steer * d_err) + obs_bias
        steer_cmd = max(-1.0, min(1.0, raw_steer))
        steer_key = self._pwm_steer_key(steer_cmd, tel.speed_mph)

        keys_out: List[str] = []
        # Modulate speed on sharp hairpins, abrupt road-tangent jumps (>35 deg), or wide off-centerline (>6m)
        if abs(err_deg) > 55.0 and tel.speed_mph > 65.0:
            keys_out.append(self.cfg.brake_reverse_key)
            self._nos_latch_timer = 0.0
        elif caution_active and tel.speed_mph > self.cfg.caution_brake_speed_mph:
            keys_out.append(self.cfg.brake_reverse_key)
            self._nos_latch_timer = 0.0
        elif caution_active and tel.speed_mph > self.cfg.caution_coast_speed_mph:
            # Coast (no throttle, no brake) with crisp steering back onto boulevard centerline
            self._nos_latch_timer = 0.0
        else:
            keys_out.append(self.cfg.throttle_key)

        if steer_key is not None:
            keys_out.append(steer_key)

        # Smart NOS (L1 -> 'q') with 1.2s continuous burst latch on aligned boulevard straightaways
        nos_can_hold = (
            self.cfg.enable_nos
            and not dodging
            and not caution_active
            and abs(err_deg) <= self.cfg.nos_abort_err_deg
            and (tel.nos is None or tel.nos > self.cfg.nos_abort_level)
        )
        nos_trigger = (
            nos_can_hold
            and abs(err_deg) <= self.cfg.nos_max_err_deg
            and tel.speed_mph >= self.cfg.nos_min_speed_mph
            and (not tel.road_nav_valid or road_offset_m < self.cfg.nos_max_road_offset_m)
        )
        if not nos_can_hold:
            self._nos_latch_timer = 0.0
        elif nos_trigger:
            if self._nos_latch_timer <= 0.0:
                self._nos_latch_timer = self.cfg.nos_burst_sec

        if nos_can_hold and self._nos_latch_timer > 0.0:
            keys_out.append(self.cfg.nos_key)
            self.nos_activated_ticks += 1

        return ControlDecision(
            mode="DRIVE",
            keys=tuple(keys_out),
            steer_cmd=steer_cmd,
            heading_error_deg=err_deg,
            obstacle_bias=obs_bias,
            reason=(
                f"src={target_src} err={err_deg:+.1f}deg off={road_offset_m:.1f}m "
                f"caution={caution_active} dodge={dodging}"
            ),
        )

    def summary(self, elapsed_sec: float) -> Dict[str, Any]:
        avg_mph = (self._speed_mph_sum / self.racing_ticks) if self.racing_ticks > 0 else 0.0
        return {
            "ticks": self.ticks,
            "racing_ticks": self.racing_ticks,
            "elapsed_sec": round(elapsed_sec, 2),
            "distance_traveled_m": round(self.distance_traveled_m, 2),
            "max_speed_mph": round(self.max_speed_mph, 2),
            "max_reverse_speed_mph": round(self.max_reverse_speed_mph, 2),
            "avg_speed_mph": round(avg_mph, 2),
            "stuck_recoveries": self.stuck_recoveries,
            "wrong_way_recoveries": self.wrong_way_recoveries,
            "reverse_verified": self.reverse_verified,
            "reverse_demo_completed": self.reverse_demo_completed,
            "nos_activated_ticks": self.nos_activated_ticks,
            "nos_engaged_ticks": self.nos_engaged_ticks,
            "nos_consumed": self.nos_consumed,
            "obstacles_avoided": self.obstacles_avoided,
            "flow_states_seen": list(self.flow_states_seen),
        }


# ---------------------------------------------------------------------------
# High-Level Wait & Closed-Loop Runner Helpers
# ---------------------------------------------------------------------------


def wait_for_gameflow_state(
    reader: NfsmwTelemetryReader,
    target_states: Sequence[int],
    *,
    timeout_sec: float = 30.0,
    poll_interval: float = 0.1,
    on_poll: Optional[Callable[[int, str], None]] = None,
) -> Tuple[bool, int, str, float]:
    """Poll TheGameFlowManager.CurrentGameFlowState via PINE until it matches `target_states`."""
    t0 = time.monotonic()
    last_state = -1
    last_name = "UNKNOWN"
    target_set = set(target_states)
    while (time.monotonic() - t0) < timeout_sec:
        try:
            st, name = reader.read_flow_state()
            last_state, last_name = st, name
            if on_poll is not None:
                on_poll(st, name)
            if st in target_set:
                return True, st, name, round(time.monotonic() - t0, 3)
        except PineError:
            pass
        time.sleep(poll_interval)
    return False, last_state, last_name, round(time.monotonic() - t0, 3)


# ---------------------------------------------------------------------------
# Deterministic GameFlowState-Aware Frontend Menu Navigator
# ---------------------------------------------------------------------------

# Non-blocking (elapsed_sec, key, tap_ms, label) schedules executed ONLY while
# TheGameFlowManager.CurrentGameFlowState == IN_FRONTEND (3). Immediately halts
# the instant flow_state transitions to LOADING_REGION (4) / LOADING_TRACK (5) / RACING (6).
_MENU_SCHEDULES: Dict[str, Tuple[Tuple[float, str, int, str], ...]] = {
    # Boot -> Memory Card OK -> Main Menu -> Career -> Safe House -> Resume Free Roam -> Yes | [No] -> Left -> Cross
    "freeroam": (
        (2.0, "k", 250, "dismiss_title_or_memcard"),
        (5.0, "k", 250, "select_career"),
        (7.5, "k", 250, "resume_career_safehouse"),
        (13.0, "k", 250, "select_resume_freeroam"),
        (14.6, "left", 250, "dialog_move_no_to_yes"),
        (15.1, "k", 250, "dialog_confirm_yes"),
    ),
    # Boot -> Memory Card OK -> Main Menu -> Right (Challenge Series) -> Enter -> Event #1 -> Confirm Yes
    "challenge": (
        (2.0, "k", 250, "dismiss_title_or_memcard"),
        (5.0, "right", 250, "select_challenge_series"),
        (5.8, "k", 250, "enter_challenge_series"),
        (8.2, "k", 250, "select_tollbooth_event"),
        (10.2, "k", 250, "start_challenge_event"),
        (11.8, "left", 250, "dialog_move_no_to_yes"),
        (12.3, "k", 250, "dialog_confirm_yes"),
    ),
    # Boot -> Main Menu -> Right x2 (Quick Race) -> Circuit -> first track -> car -> options (3 opponents) -> Start.
    # Not yet live-calibrated: tools/benchmark_4car_race.py --menu-keys overrides it, and its gate rejects any
    # run where PINE does not see >= 3 AI racers (so a mis-navigation can never pass as a 4-car race).
    "quickrace": (
        (2.0, "k", 250, "dismiss_title_or_memcard"),
        (5.0, "right", 250, "select_challenge_series"),
        (5.8, "right", 250, "select_quick_race"),
        (6.6, "k", 250, "enter_quick_race"),
        (8.6, "k", 250, "select_circuit"),
        (10.6, "k", 250, "select_first_track"),
        (12.6, "k", 250, "select_car"),
        (14.6, "k", 250, "accept_race_options"),
        (16.2, "k", 250, "start_race"),
    ),
}


class FrontendMenuNavigator:
    """State-gated deterministic menu navigator for NFSMW PS2 IN_FRONTEND screens."""

    def __init__(self, mode: str = "freeroam") -> None:
        norm = mode.strip().lower()
        if norm not in _MENU_SCHEDULES:
            raise ValueError(f"Unsupported menu_mode {mode!r}; expected one of {sorted(_MENU_SCHEDULES)}")
        self.mode = norm
        self._schedule = _MENU_SCHEDULES[norm]
        self._entered_t: Optional[float] = None
        self._step_idx: int = 0
        self._retry_cycle: int = 0
        self._retry_substep: int = 0
        self._retry_start_sec: float = self._schedule[-1][0] + 2.9
        self.steps_sent: List[str] = []

    def reset(self) -> None:
        self._entered_t = None
        self._step_idx = 0
        self._retry_cycle = 0
        self._retry_substep = 0

    def step(self, now: float, flow_state: int, sink: InputSink) -> Optional[ControlDecision]:
        """Fire the next due menu tap if flow_state == IN_FRONTEND, else return None."""
        if flow_state != GameFlowState.IN_FRONTEND:
            if self._entered_t is not None:
                self.reset()
            return None

        if self._entered_t is None:
            self._entered_t = now
        elapsed = now - self._entered_t

        # 1. Execute primary deterministic schedule
        if self._step_idx < len(self._schedule):
            due_sec, key, tap_ms, label = self._schedule[self._step_idx]
            if elapsed >= due_sec:
                self._step_idx += 1
                sink.set_keys(())
                sink.tap(key, tap_ms)
                self.steps_sent.append(f"{label}:{key}")
                return ControlDecision(
                    mode="MENU_ADVANCE",
                    keys=(key,),
                    steer_cmd=0.0,
                    heading_error_deg=0.0,
                    obstacle_bias=0.0,
                    reason=f"menu[{self.mode}] t={elapsed:.1f}s {label} (tap {key})",
                )
            return ControlDecision(
                mode="MENU_WAIT",
                keys=(),
                steer_cmd=0.0,
                heading_error_deg=0.0,
                obstacle_bias=0.0,
                reason=f"menu[{self.mode}] waiting for {label} at t={due_sec:.1f}s (cur={elapsed:.1f}s)",
            )

        # 2. Retry cycle if still IN_FRONTEND past schedule end: k -> (+1.5s) left -> (+0.4s) k every 4.0s
        retry_pattern = (
            (0.0, "k", 250, "retry_open_or_confirm"),
            (1.5, "left", 250, "retry_move_no_to_yes"),
            (1.9, "k", 250, "retry_confirm_yes"),
        )
        cycle_base = self._retry_start_sec + self._retry_cycle * 4.0
        sub_off, key, tap_ms, label = retry_pattern[self._retry_substep]
        if elapsed >= (cycle_base + sub_off):
            self._retry_substep += 1
            if self._retry_substep >= len(retry_pattern):
                self._retry_substep = 0
                self._retry_cycle += 1
            sink.set_keys(())
            sink.tap(key, tap_ms)
            self.steps_sent.append(f"{label}:{key}")
            return ControlDecision(
                mode="MENU_ADVANCE",
                keys=(key,),
                steer_cmd=0.0,
                heading_error_deg=0.0,
                obstacle_bias=0.0,
                reason=f"menu[{self.mode}] retry#{self._retry_cycle} {label} (tap {key})",
            )

        return ControlDecision(
            mode="MENU_WAIT",
            keys=(),
            steer_cmd=0.0,
            heading_error_deg=0.0,
            obstacle_bias=0.0,
            reason=f"menu[{self.mode}] retry wait cur={elapsed:.1f}s",
        )


def run_closed_loop_autopilot(
    reader: NfsmwTelemetryReader,
    sink: InputSink,
    *,
    duration_sec: float = 30.0,
    config: Optional[AutopilotConfig] = None,
    telemetry_jsonl_path: Optional[Path] = None,
    f8_interval_sec: float = 0.0,
    wait_for_racing_timeout: float = 0.0,
    menu_auto_advance: bool = False,
    menu_mode: str = "freeroam",
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    """Run the 20 Hz closed-loop driving loop and write per-tick telemetry JSONL."""
    cfg = config or AutopilotConfig()
    pilot = DrivingAutopilot(cfg)
    menu_nav = FrontendMenuNavigator(mode=menu_mode) if menu_auto_advance else None
    tick_period = 1.0 / max(cfg.hz, 1.0)

    log_fp = None
    if telemetry_jsonl_path is not None:
        telemetry_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        log_fp = telemetry_jsonl_path.open("w", encoding="utf-8")

    t_start = clock()
    last_tick_t = t_start
    last_f8_t = t_start
    frontend_entered_t: Optional[float] = None
    frontend_f8_taken = False
    race_started_t: Optional[float] = None
    errors: List[str] = []

    try:
        while True:
            now = clock()
            # Duration counts from race start if waiting for racing, else from loop start
            if race_started_t is not None:
                if (now - race_started_t) >= duration_sec:
                    break
            else:
                if wait_for_racing_timeout > 0.0:
                    if (now - t_start) >= wait_for_racing_timeout:
                        break
                elif (now - t_start) >= duration_sec:
                    break

            dt = max(0.005, min(0.25, now - last_tick_t))
            last_tick_t = now

            try:
                tel = reader.read_telemetry()
            except PineError as exc:
                errors.append(str(exc))
                if len(errors) >= 10:
                    break
                sleeper(tick_period)
                continue

            if tel.flow_state == GameFlowState.IN_FRONTEND and frontend_entered_t is None:
                frontend_entered_t = now

            if tel.flow_state == GameFlowState.RACING and tel.player_present and race_started_t is None:
                race_started_t = now
                # Delay first gameplay F8 snap until t_race >= 2.0s (past black fade-in transition!)
                if f8_interval_sec > 0.0:
                    last_f8_t = now - f8_interval_sec + 2.0

            # Smart GameFlowState-aware menu advance (navigates IN_FRONTEND -> Yes | [No] -> RACING!)
            menu_decision = menu_nav.step(now, tel.flow_state, sink) if menu_nav is not None else None
            if menu_decision is not None:
                pilot.ticks += 1
                if tel.flow_state_name not in pilot.flow_states_seen:
                    pilot.flow_states_seen.append(tel.flow_state_name)
                decision = menu_decision
            else:
                decision = pilot.step(tel, dt=dt)
                sink.set_keys(decision.keys)

            # Capture 1 clean 4K F8 snap of the Main Menu BMW M3 GTR (~1.4s after Memory Card OK, before Career)
            if (
                f8_interval_sec > 0.0
                and not frontend_f8_taken
                and tel.flow_state == GameFlowState.IN_FRONTEND
                and frontend_entered_t is not None
                and (now - frontend_entered_t) >= 3.4
            ):
                sink.trigger_f8()
                frontend_f8_taken = True

            if (
                f8_interval_sec > 0.0
                and race_started_t is not None
                and tel.flow_state == GameFlowState.RACING
                and (now - last_f8_t) >= f8_interval_sec
            ):
                sink.trigger_f8()
                last_f8_t = now

            if log_fp is not None:
                rec = tel.to_dict()
                rec["action"] = decision.to_dict()
                rec["distance_traveled_m"] = round(pilot.distance_traveled_m, 2)
                rec["max_speed_mph"] = round(pilot.max_speed_mph, 2)
                rec["stuck_recoveries"] = pilot.stuck_recoveries
                rec["reverse_verified"] = pilot.reverse_verified
                log_fp.write(json.dumps(rec) + "\n")

            spent = clock() - now
            rem = tick_period - spent
            if rem > 0.001:
                sleeper(rem)
    finally:
        try:
            sink.set_keys(())
        except Exception:
            pass
        if log_fp is not None:
            log_fp.close()

    summary = pilot.summary(elapsed_sec=clock() - t_start)
    summary["race_started"] = race_started_t is not None
    if menu_nav is not None:
        summary["menu_mode"] = menu_nav.mode
        summary["menu_steps_sent"] = list(menu_nav.steps_sent)
    summary["errors"] = errors[-5:]
    if telemetry_jsonl_path is not None:
        summary["telemetry_jsonl"] = str(telemetry_jsonl_path)
    return summary


# ---------------------------------------------------------------------------
# CLI Interface (`probe`, `wait-state`, `autopilot`)
# ---------------------------------------------------------------------------


def _format_probe_line(tel: VehicleTelemetry) -> str:
    x, y, z = tel.pos
    road_str = (
        f"road={tel.road_tangent_deg:+.1f}deg(seg={tel.road_segment})"
        if tel.road_nav_valid and tel.road_tangent_deg is not None
        else "road=N/A"
    )
    cp_str = (
        f"cp=({tel.next_checkpoint_pos[0]:.0f},{tel.next_checkpoint_pos[2]:.0f})"
        if tel.next_checkpoint_pos
        else "cp=N/A"
    )
    rpm_str = f"{tel.rpm:.0f}" if tel.rpm is not None else "?"
    return (
        f"[{tel.flow_state_name:17s}] "
        f"pos=({x:7.1f},{y:5.1f},{z:7.1f}) "
        f"spd={tel.speed_mph:5.1f}mph hdg={tel.heading_deg:+6.1f}deg "
        f"gear={tel.gear_label} rpm={rpm_str} {road_str} {cp_str} "
        f"obs={len(tel.obstacles)} ww={int(tel.wrong_way)} ({tel.read_ms:.2f}ms)"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="PCSX2 v2.6.3 PINE IPC telemetry reader & closed-loop driving autopilot (SLUS-21351)"
    )
    parser.add_argument("--socket", type=Path, default=None, help="Explicit PINE Unix socket path")
    parser.add_argument("--slot", type=int, default=DEFAULT_PINE_SLOT, help="PCSX2 PINESlot (default: 28011)")
    parser.add_argument("--timeout", type=float, default=1.0, help="Socket timeout in seconds")

    sub = parser.add_subparsers(dest="command", required=True)

    # probe
    p_probe = sub.add_parser("probe", help="Connect to live PCSX2 PINE socket and print telemetry")
    p_probe.add_argument("--watch", action="store_true", help="Continuously stream telemetry")
    p_probe.add_argument("--hz", type=float, default=5.0, help="Refresh rate for --watch (default: 5 Hz)")
    p_probe.add_argument("--count", type=int, default=0, help="Stop after N samples (0=unlimited in --watch)")
    p_probe.add_argument("--json", action="store_true", help="Emit JSON output")

    # wait-state
    p_wait = sub.add_parser("wait-state", help="Wait until TheGameFlowManager reaches a target state")
    p_wait.add_argument(
        "--state",
        type=str,
        default="RACING",
        help="Target GameFlowState name or int (e.g. IN_FRONTEND, RACING, 6)",
    )
    p_wait.add_argument("--wait-timeout", type=float, default=30.0)
    p_wait.add_argument("--json", action="store_true")

    # autopilot
    p_auto = sub.add_parser("autopilot", help="Run 20 Hz closed-loop PINE-driven autopilot")
    p_auto.add_argument("--pid", type=int, default=None, help="PCSX2 process ID for macOS key_driver")
    p_auto.add_argument(
        "--key-driver",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "build" / "flywheel" / "key_driver",
        help="Path to compiled macOS key_driver binary",
    )
    p_auto.add_argument("--tap-mode", choices=["auto", "hid", "session", "pid"], default="auto")
    p_auto.add_argument("--duration", type=float, default=30.0, help="Driving duration in seconds")
    p_auto.add_argument("--hz", type=float, default=20.0, help="Control loop frequency (default: 20 Hz)")
    p_auto.add_argument("--telemetry-out", type=Path, default=None, help="Output path for telemetry.jsonl")
    p_auto.add_argument("--summary-out", type=Path, default=None, help="Output path for summary JSON")
    p_auto.add_argument("--f8-interval", type=float, default=0.0, help="Optional F8 snapshot interval (s)")
    p_auto.add_argument("--wait-racing-timeout", type=float, default=0.0, help="Wait up to N sec for RACING")
    p_auto.add_argument("--menu-auto-advance", action="store_true", help="Auto-navigate IN_FRONTEND menus into race")
    p_auto.add_argument(
        "--menu-mode",
        choices=sorted(_MENU_SCHEDULES),
        default="freeroam",
        help="IN_FRONTEND route: 'freeroam' (Career Safe House), 'challenge' (Challenge Series) or 'quickrace' (4-car Circuit)",
    )
    p_auto.add_argument("--no-nos", action="store_true", help="Disable automatic NOS activation")
    p_auto.add_argument(
        "--no-verify-reverse",
        action="store_true",
        help="Disable open-road Reverse (R gear) verification maneuver once past 40m",
    )
    p_auto.add_argument("--dry-run", action="store_true", help="Read telemetry & compute decisions without sending keys")
    p_auto.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    with PineClient(args.socket, slot=args.slot, timeout=args.timeout) as pine:
        reader = NfsmwTelemetryReader(pine)

        if args.command == "probe":
            meta: Dict[str, Any] = {"socket": str(pine.connected_path)}
            try:
                meta["version"] = pine.version()
                meta["emu_status"] = pine.status().name
                meta["game_id"] = pine.game_id()
                meta["game_crc"] = f"{pine.game_crc():08X}"
            except PineError as exc:
                meta["meta_error"] = str(exc)

            if not args.watch:
                tel = reader.read_telemetry()
                if args.json:
                    print(json.dumps({"meta": meta, "telemetry": tel.to_dict()}, indent=2))
                else:
                    print(f"PINE connected: {meta}")
                    print(_format_probe_line(tel))
                return 0

            period = 1.0 / max(args.hz, 0.5)
            n = 0
            try:
                while args.count <= 0 or n < args.count:
                    tel = reader.read_telemetry()
                    n += 1
                    if args.json:
                        print(json.dumps(tel.to_dict()), flush=True)
                    else:
                        print(_format_probe_line(tel), flush=True)
                    time.sleep(period)
            except KeyboardInterrupt:
                pass
            return 0

        if args.command == "wait-state":
            raw_state = args.state.strip().upper()
            try:
                target = int(raw_state)
            except ValueError:
                target = int(GameFlowState[raw_state])
            ok, st, name, elapsed = wait_for_gameflow_state(
                reader,
                [target],
                timeout_sec=args.wait_timeout,
            )
            payload = {"ok": ok, "target": target, "state": st, "state_name": name, "elapsed_sec": elapsed}
            if args.json:
                print(json.dumps(payload, indent=2))
            else:
                print(f"wait-state ok={ok} state={name}({st}) elapsed={elapsed}s")
            return 0 if ok else 1

        if args.command == "autopilot":
            cfg = AutopilotConfig(
                hz=args.hz,
                enable_nos=not args.no_nos,
                verify_reverse_demo=not args.no_verify_reverse,
            )
            sink: InputSink
            if args.dry_run or args.pid is None:
                sink = NullInputSink()
            else:
                drv = KeyDriverSession(args.key_driver, pid=args.pid, tap_mode=args.tap_mode)
                drv.start()
                sink = drv
            try:
                summary = run_closed_loop_autopilot(
                    reader,
                    sink,
                    duration_sec=args.duration,
                    config=cfg,
                    telemetry_jsonl_path=args.telemetry_out,
                    f8_interval_sec=args.f8_interval,
                    wait_for_racing_timeout=args.wait_racing_timeout,
                    menu_auto_advance=args.menu_auto_advance,
                    menu_mode=args.menu_mode,
                )
            finally:
                sink.close()

            if args.summary_out is not None:
                args.summary_out.parent.mkdir(parents=True, exist_ok=True)
                args.summary_out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            if args.json:
                print(json.dumps(summary, indent=2))
            else:
                print(
                    f"Autopilot done: ticks={summary['ticks']} dist={summary['distance_traveled_m']}m "
                    f"max={summary['max_speed_mph']}mph avg={summary['avg_speed_mph']}mph "
                    f"recoveries={summary['stuck_recoveries']} reverse_ok={summary['reverse_verified']}"
                )
            return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
