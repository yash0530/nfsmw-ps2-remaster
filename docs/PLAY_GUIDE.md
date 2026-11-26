# NFSMW: Rockport 2026 — Player's Guide (PS2 `SLUS-21351` in PCSX2 v2.6.3 on macOS)

Rockport 2026 is a set of **non-destructive patches for the real PlayStation 2 *Need for Speed: Most Wanted – Black Edition*** (NTSC-U, `SLUS-21351`, 2005), played inside **PCSX2 v2.6.3**. It is not a remake: the original ELF, physics, career, FMVs and memory-card saves are the game you own. Every tool reads your ISO read-only and writes a separate modded copy under `build/`.

Status of each milestone lives in [PROGRESS.md](PROGRESS.md); engineering plans are in [ROADMAP.md](ROADMAP.md).

---

## 1. What you need

| Item | Notes |
| :--- | :--- |
| Mac (Apple Silicon recommended) | PCSX2 Metal renderer; 4K internal resolution needs a recent M-series GPU |
| PCSX2 **v2.6.3** | Plus a PS2 BIOS dumped from your own console |
| Your own disc image | *NFSMW Black Edition* NTSC-U, `SLUS-21351`, stock ELF CRC `0518D274` |
| Python 3 (3.11+ recommended) | Pillow + NumPy only for the texture tools; Rust/`cargo` only for the BC7 encoder |
| PS5 DualSense (optional) | USB-C or Bluetooth |

```bash
export NFSMW_ISO="/path/to/Need for Speed - Most Wanted - Black Edition.iso"
```

No EA or Sony files are distributed here. See [ASSETS_LICENSE.md](../ASSETS_LICENSE.md).

## 2. Pick a build profile

| Profile | Gameplay | Visuals |
| :--- | :--- | :--- |
| `rockport2026_purist_hd` | 100% stock | 16:9, brown filter + smear removed, high-LOD cars, 5-face reflections, 3x draw distance, 60 Hz ScreenEFX |
| `rockport2026_authentic_vlt` | Stock feel + measured VLT polish (§6) | Same as Purist HD |
| `rockport2026_v0.5` | Large handling/pursuit/camera changes (over the §6 authenticity budget) | Adds wet roads, Forza-style lighting, optional hero-car meshes |
| `rockport2026_sandbox` | Unlocks all cars/parts, skips intros, no AI catch-up, plus the modern handling/pursuit VLT | Purist HD set |

```bash
python3 tools/rockport_cli.py profiles          # validate + list every profile
python3 tools/rockport_cli.py build --profile rockport2026_authentic_vlt --source "$NFSMW_ISO" \
  --install-pcsx2 --renderer metal --upscale 6 --anisotropy 16 --ee-cycle-rate 2
```

The modded ISO lands in `build/`. `--install-pcsx2` writes only per-game files (`gamesettings/SLUS-21351_<CRC>.ini` and `patches/SLUS-21351_<CRC>.pnach`) and never touches your global `PCSX2.ini` or memory cards.

**Prefer the untouched retail ISO?** Run `make setup-pcsx2`, then enable sections of [`patches/pnach/SLUS-21351_0518D274.pnach`](../patches/pnach/SLUS-21351_0518D274.pnach) under *Game Properties → Patches*. Enable only one aspect-ratio section at a time.

## 3. 4K Metal profile (per game)

Generate or inspect the `v2.1` per-game 4K/32-bit Metal profile with `tools/pcsx2_profile_2026.py` (which writes only `~/Library/Application Support/PCSX2/gamesettings/SLUS-21351_<CRC>.ini` and never touches your global `PCSX2.ini` or memory cards):

```bash
python3 tools/pcsx2_profile_2026.py --crc 0518D274 --scale 6 --anisotropy 16 --ee-cycle-rate 3
python3 tools/benchmark_pcsx2_live.py --iso "$NFSMW_ISO"   # isolated-sandbox PINE FPS + MP4 + 5-PNG A/B proof
```

| Setting | Value |
| :--- | :--- |
| Renderer | Metal (`Renderer = 17`) |
| Internal resolution | 6x native (4K) |
| Dithering | Force 32bit (`dithering_ps2 = 3`, removes PS2 dither noise at 4K) |
| Anisotropic filtering | 16x, with mipmapping |
| Aspect ratio | 16:9 (paired with the matching widescreen patch) |
| Hardware fixes | Official PCSX2 GameDB SLUS-21351 fixes (Half Pixel Offset, Native Scaling, CLUT) |
| Texture replacements | Load + async + precache |
| EE cycle rate | 300% (`--ee-cycle-rate 3`, or 180% `--ee-cycle-rate 2`; drop to 130% `1` if audio crackles) |

## 4. PS5 DualSense setup

1. Pair it in *System Settings → Bluetooth*: hold **PS + Create** until the light bar flashes. Or plug in over USB-C.
2. In PCSX2, go to *Settings → Controllers*. Keep the **SDL** input source enabled. Enable **DualShock 4 / DualSense Enhanced Mode** if your build shows it.
3. Select *Controller Port 1 → DualShock 2 → Automatic Mapping* and choose the SDL entry for your DualSense.
4. L2/R2 are analog and map to the PS2's pressure-sensitive buttons. Set *Analog Deadzone* to about 5–10% so the stick doesn't drift.
5. PCSX2 drives standard rumble only. Adaptive triggers and haptics are not used.

## 5. HD textures, 4K HUD and opt-in hero cars

All texture and geometry tools read your untouched retail ISO strictly read-only (`'rb'`) and write outputs under `build/`:

- **Faithful HD world, car & global textures (`v2.2`):** build all 5,859 BC7 DDS replacements offline directly from your ISO using the pure-math PS2 GS VRAM + `XXH3_64bits` hasher (no manual in-game dumping required; UI/fonts/loading icons stay cleanly separated):
  ```bash
  python3 tools/build_pcsx2_texture_pack.py --iso "$NFSMW_ISO" --out build/pcsx2_textures_v22
  ```
- **4K HUD, fonts, menus & loading icons (`v2.3`):** build 677 single-mip 4x/8x BC7 DDS replacements with raw PS2 GS `[0..128]` alpha clamping and zero overlap with the `v2.2` pack (6,536 combined DDS):
  ```bash
  python3 tools/build_hud_ui_pack.py --iso "$NFSMW_ISO" --out build/pcsx2_hud_v23
  ```
  Copy (or symlink) the `.dds` files from both `replacements/` folders into `~/Library/Application Support/PCSX2/textures/SLUS-21351/replacements/`.
- **Opt-in hero car VU1 smoothing (`v2.3`):** strictly opt-in per-car smoothing with wheel-hub, AABB, damage-morph, and EE pack-size guards (`BMWM3GTRE46`, `CARRERAGT`, `GALLARDO`, `MUSTANGGT`):
  ```bash
  python3 tools/optin_car_geometry.py --hero-preset --iso "$NFSMW_ISO" --out build/optin_cars_v23
  ```
  Pass any promoted `GEOMETRY.BIN` files to `tools/build_modded_iso.py` via `--zz-override 'CARS\<CAR>\GEOMETRY.BIN=build/optin_cars_v23/CARS/<CAR>/GEOMETRY.BIN'`. Without explicit flags, stock car geometry is untouched.

## 6. Measured authentic VLT handling & pursuit (`authentic_measured_v24`)

The goal is the 2005 feel: crisp but not twitchy, with pursuits that stay fair. Each handling value moves at most ±5% from stock and each pursuit value at most ±10%. Grip, brakes, NOS, traffic and patrol counts are unchanged.

| Field | Stock → v2.4 |
| :--- | :--- |
| Analog steering curve (`world/CONTROLLER_CURVE`) | 1.25 → 1.20 (slightly more linear on modern sticks) |
| BMW M3 GTR steering / yaw speed | 0.90 → 0.94 / 0.25 → 0.26 |
| Prologue M3 GTR yaw speed · Corvette C6 yaw speed | 0.26 → 0.27 · 0.30 → 0.31 |
| Cop spawn interval, Heat 1 / 2 / 4 | 10 → 9.5 s / 10 → 9.5 s / 5 → 4.75 s |
| Roadblock chance, Heat 3 | 25% → 27.5% |

```bash
python3 tools/verify_v24_vlt_and_saves.py audit-vlt                 # budget audit of every patches/vlt recipe
python3 tools/verify_v24_vlt_and_saves.py verify-vlt --iso "$NFSMW_ISO"   # dry-run on retail ATTRIBUTES.BIN
```

The audit also reports that the older `modern_handling_tuning`, `full_roster_handling` and `pursuit_and_traffic` recipes exceed this budget. For example, they raise the hero BMW's yaw speed by 36% and double Heat 1–3 patrol cars. Use them for a deliberately different feel, not for authenticity.

To measure a change in practice, record PINE autopilot telemetry on the retail ISO and on the modded ISO, then compare the two runs. Only do this when no other PCSX2 instance is running:

```bash
python3 tools/race_flywheel.py run-race --pine-autopilot --iso "$NFSMW_ISO" --iter-name v24_before
python3 tools/race_flywheel.py run-race --pine-autopilot --iso build/NFSMW_BlackEdition_Rockport2026_AuthenticVLT.iso --iter-name v24_after
python3 tools/verify_v24_vlt_and_saves.py compare-telemetry <before>/pine_telemetry.jsonl <after>/pine_telemetry.jsonl
```

The comparison reports speed, steering jitter, yaw-rate stability and wrong-way time. It reports heat and cop counts only when the log contains them; otherwise it uses obstacle count as a rough traffic proxy. Autopilot runs are noisy, so treat a single run as a smoke test, not a benchmark.

## 7. Saves carry over between retail and modded ISOs

Modded builds leave `SYSTEM.CNF` (`BOOT2 = cdrom0:\SLUS_213.51;1`) byte-identical, so PCSX2 still sees serial `SLUS-21351`. They also leave the ELF strings the game uses to name its saves (`SLUS_21351`, the `BA` region prefix, `MC_SaveInfo`, `nfwMW_New.ico`, `NFS MW%s`) untouched. In-game saves in your `BASLUS-21351…` folder on `Mcd001.ps2` therefore load from either ISO.

```bash
python3 tools/verify_v24_vlt_and_saves.py verify-saves --iso "$NFSMW_ISO" \
  --profile patches/profiles/rockport2026_authentic_vlt.json      # or --modded-iso build/<your>.iso
```

The verifier compares the retail disc with the modded build. If you pass `--profile`, it models the build by applying that profile's ELF patches in memory. It then round-trips a synthetic save on a temporary sandbox card and deletes it. It never opens your real memory cards.

- **Save states (`.p2s`) do not transfer** between the retail and modded ISOs because the ELF differs. Use in-game saves.
- The modded ISO has its own CRC, so it gets its own per-game settings file. Pass both CRCs to `setup_pcsx2_macos.py --crcs` if you switch back and forth.
- Back up `~/Library/Application Support/PCSX2/memcards/Mcd001.ps2` before trying any mod.

## 8. Check your checkout

```bash
make verify                                   # py_compile, hygiene audit, .pnach lint, profiles, tests
NFSMW_ISO=/nonexistent python3 -m unittest discover -s tests -v   # offline suite (what CI runs)
```

## 9. Credits

- [`dbalatoni13/nfsmw`](https://github.com/dbalatoni13/nfsmw): the `SLUS-21351` decompilation, whose symbol names we use to identify and cross-check engine functions and globals.
- `.pnach` visual fixes by **fobes** and **Steelmax / PCSX2 community**, independently re-verified against `SLUS_213.51`.
- The **PCSX2 team**, for the emulator, the PINE IPC protocol, the Metal renderer and texture replacement.
