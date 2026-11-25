# PS2 NFSMW (`SLUS-21351`) Audio Modding & Clean-Room Synthesis Pipeline

Clean-room specification and tooling documentation for **Path 1: PS2 Max-Ceiling Audio** in *Need for Speed: Most Wanted - Black Edition* (`SLUS-21351`), implemented in [`tools/audio_patcher.py`](../../tools/audio_patcher.py) with CC0 asset manifest [`assets_cc0/audio_manifest.json`](../../assets_cc0/audio_manifest.json).

> **Safety & Legal Policy:**
> - Never modify or open the retail ISO for writing; all reads use `'rb'` via `os.environ.get("NFSMW_ISO")` or `Path.home() / "Documents/PS2Games/..."`.
> - All replacement audio cues are **100% original procedural CC0-1.0 waveforms** synthesized in Python (`synth-cc0`). Never bundle or reference copyrighted audio.
> - All temporary `.wav`, `.abk`, `.gin`, `.bin`, and diff JSON outputs belong exclusively inside gitignored `build/audio_wave3/`.

---

## 1. Sony PS2 SPU-ADPCM (VAG / EA `BNKl` SPU-RAM) Codec

Inside `ABKC` + `BNKl` v5 sound banks, hardware SPU2 waveform slots (`codec_id=None`, `data_offset >= bnkl_header_size`; **1,719** of **2,350** active slots across **301** `ABKC` banks in `SLUS-21351`) use standard **16-byte Sony PS2 SPU-ADPCM (VAG)** blocks representing **28 mono PCM samples** per block (`4 bits/sample`, `3.5:1` compression vs 16-bit PCM).

### 16-Byte Block Layout
| Offset | Size | Field | Meaning |
| :---: | :---: | :--- | :--- |
| `0x00` | 1 B | `predict_shift` | Upper nibble `(ps >> 4) & 0xF` = predictor `0..4`; lower nibble `ps & 0xF` = shift factor `0..12` |
| `0x01` | 1 B | `flags` | SPU2 hardware loop/end state flags |
| `0x02` | 14 B | `nibbles[14]` | 28 signed 4-bit residuals (`low nibble` first, `high nibble` second) |

### 5-Predictor IIR Coefficients (`SPU_FILTER_FIXED`)
Each sample $x[n]$ is reconstructed from previous decoded samples $s_1, s_2$ and signed 4-bit residual $q \in [-8, 7]$ as:

$$x[n] = \left(\frac{q \ll 12}{2^{\text{shift}}}\right) + \frac{k_0 \cdot s_1 + k_1 \cdot s_2}{64}$$

| Predictor | $k_0 / 64$ | $k_1 / 64$ | Typical Match |
| :---: | :---: | :---: | :--- |
| `0` | `0` | `0` | Direct DPCM / silence / unpredicted transients |
| `1` | `60 / 64` | `0` | First-order low-pass / decaying envelopes |
| `2` | `115 / 64` | `-52 / 64` | Second-order resonant / tonal harmonics |
| `3` | `98 / 64` | `-55 / 64` | Mid-band / mechanical transients |
| `4` | `122 / 64` | `-60 / 64` | Smooth low-frequency fundamentals |

The encoder in `tools/audio_patcher.py` runs closed-loop IIR quantization search over all 5 predictors and candidate shift exponents per 28-sample frame to minimize squared reconstruction error.

### SPU2 Hardware Loop Flags
- `0x00`: Normal body block (one-shot)
- `0x01`: One-shot sample end + mute (observed on retail `FE_COMMON_MB.ABK`, `GEAR_SML_BASE.ABK`)
- `0x02`: Loop body block
- `0x03`: Loop end + jump to loop start
- `0x06`: Loop start + loop repeat (sets SPU2 `LSA` register)
- `0x07`: Single-block loop (`0x01 | 0x02 | 0x04`) / VAG end marker

Retail `ABKC` SPU2 samples also prepend **one 16-byte all-zero lead-in block** (`lead_in_zero_block=True`, `28` silent samples) before block 1 (`0x00` or `0x06`).

---

## 2. `ABKC` / `BNKl` v5 Relocation & Sample Patching

Replacing a sample inside an `ABKC` bank requires updating both the `BNKl` payload and the outer `ABKC` relocation pointers, verified across **301 / 301** retail banks (`0` invariant failures):

1. **Slot `PT\x05\x00` TLV Header:** Updates `0x82` (`channels`), `0x84` (`sample_rate`), `0x85` (`num_samples`), `0x86`/`0x87` (`loop_start`/`loop_length` when looped), `0x88` (`data_offset`), and `0x89` (right-channel `data_offset` when stereo).
2. **`BNKl` Header & Payload:**
   - Rebuilds `rel_offsets[slot_count]` and pads `bnkl_header_size` (`+0x08`) to 16 bytes.
   - Shifts `data_offset` (`0x88` / `0x89`) on all untouched SPU-RAM slots (`data_offset >= old_bnkl_header_size`) by `hdr_delta + payload_delta` (only for slots strictly after a non-aliased spliced slot).
   - If the replaced slot's original `data_offset` is **aliased** by another slot (`19` cases across retail banks), appends the replacement sample to the end of `bnkl_payload` so sibling slots remain bit-for-bit untouched.
3. **Outer `ABKC` Chunk & `t3` String Relocation:**
   - Pads the end of `BNKl` payload with `0x00` to a **128-byte boundary** (`t1_off`, verified across all 301 retail `ABKC` banks).
   - Updates `total_size` (`+0x14`), `bnkl_chunk_size` (`+0x24 = new_t1 - bnkl_offset`), `t1_off` (`+0x30`), `t2_off` (`+0x34`), and `t3_off` (`+0x38`).
   - Leaves `t1` and `t2` entries untouched (`100%` of `t1`/`t2` pointers reference `[0x00 .. bnkl_offset)`), and relocates every 12-byte `t3` cue descriptor's string pointer (`t3 + 4 + i * 12 + 4`) by `+tail_delta`.

---

## 3. `Gnsu` v20 Engine Synth Tuning & Hero Car Map

Across all **160** `Gnsu` v20 files in `SLUS-21351`, the granule payload consists of a `4`-byte `(total_samples - 1)` prefix followed by `ceil(total_samples / 32) * 19` bytes of EA-XAS frames (`granule_data_bytes == ((total_samples + 31) // 32) * 19 + 4` for `160 / 160` entries).

`patch-gnsu` preserves the EA-XAS granule payload verbatim while tuning:
- **`min_rpm` (`+0x08`) & `max_rpm` (`+0x0C`)**: Expands redline RPM bounds without premature synth clamping.
- **`sample_rate` (`+0x1C`)**: Scales playback rate (`rate_scale`) to shift exhaust pitch/presence (`+3%..+4.5%` brighter upper-harmonic bite on high-revving V8/V10s; `-4.5%` deeper V8 rumble on Camaro SS).
- **`rpm_table` (`+0x20`, `num_rpm_steps + 1` `uint32`s)**: Applies a normalized power curve $u' = u^\gamma$ (`curve_gamma`) across the `51`-point sample lookup table while strictly preserving endpoints and monotonicity (`accel` ascending / `decel` descending).

### Verified Hero Car VLT -> `Gnsu` Presets (`GLOBAL\ATTRIBUTES.BIN`)

| Preset | Vehicle (`pvehicle`) | `engineaudio` | Target `Gnsu` Path | Stock -> Tuned RPM | Rate Scale & `gamma` |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `bmw_m3_gtr` | `bmwm3gtre46` | `tvr_cerb` | `SOUND\ENGINE\GIN_TVR_CERBERA.GIN` (`0x0306f074`) | `1239.5..7784.1` -> `1200..8350` | `1.045x` (`25080 Hz`), $\gamma=0.92$ |
| `carrera_gt` | `carreragt` | `por_car_gt` | `SOUND\ENGINE\GIN_POR_CARRERA_GT.GIN` | `1208.0..8204.9` -> `1150..8450` | `1.035x` (`33120 Hz`), $\gamma=0.90$ |
| `gallardo` | `gallardo` | `lam_gallardo` | `SOUND\ENGINE\GIN_GALLARDO.GIN` | `1530.6..8248.4` -> `1350..8500` | `1.030x` (`32960 Hz`), $\gamma=0.93$ |
| `camaro_ss` | `camaro` | `for_mus_b` | `SOUND\ENGINE\GIN_MUSTSAL_RAMP_V2_CD.GIN` | `1116.8..5154.0` -> `950..6400` | `0.955x` (`30560 Hz`), $\gamma=1.06$ |

---

## 4. Procedural CC0-1.0 Audio Assets (`synth-cc0`)

Running `python3 tools/audio_patcher.py synth-cc0 --verify-iso` synthesizes 6 original CC0-1.0 WAVs into `build/audio_wave3/cc0_wav/` and validates SPU-ADPCM round-trip SNR & live ISO patching:

| Asset | Rate | Samples | SPU-ADPCM Size | Measured SNR | Target `ABKC` Bank & Slot |
| :--- | :---: | :---: | :---: | :---: | :--- |
| `ui_click.wav` | `32000 Hz` | `1,440` | `832 B` | `32.55 dB` | `SOUND\GLOBAL\FE_COMMON_MB.ABK` (slot `1`) |
| `ui_confirm.wav` | `32000 Hz` | `5,760` | `3,296 B` | `42.74 dB` | `SOUND\GLOBAL\FE_COMMON_MB.ABK` (slot `2`) |
| `ui_back.wav` | `32000 Hz` | `4,480` | `2,560 B` | `51.59 dB` | `SOUND\GLOBAL\FE_COMMON_MB.ABK` (slot `3`) |
| `turbo_bov_flutter.wav` | `24000 Hz` | `12,480` | `7,136 B` | `30.84 dB` | `SOUND\TURBO\TURBO_MED_2_MB.ABK` (slot `2`) |
| `gearshift_transient.wav` | `24000 Hz` | `3,840` | `2,208 B` | `32.82 dB` | `SOUND\SHIFTING\GEAR_SML_BASE.ABK` (slot `1`) |
| `straight_cut_whine_loop.wav` | `32000 Hz` | `17,920` (`640*28`) | `10,240 B` | `40.60 dB` | `SOUND\ENGINE\CAR_WHINE_00.ABK` (slot `1`, looped) |

---

## 5. CLI Usage Examples

```bash
# 1. Generate all 6 CC0-1.0 procedural WAVs + verify ABKC & Gnsu round-trips from untouched ISO
python3 tools/audio_patcher.py synth-cc0 --verify-iso --out-dir build/audio_wave3

# 2. Encode & decode standalone WAV <-> PS2 SPU-ADPCM
python3 tools/audio_patcher.py spu-encode --in-wav build/audio_wave3/cc0_wav/ui_confirm.wav --out build/audio_wave3/ui_confirm.vag
python3 tools/audio_patcher.py spu-decode --in-bin build/audio_wave3/ui_confirm.vag --out-wav build/audio_wave3/ui_confirm_dec.wav --sample-rate 32000

# 3. Tune a hero car Gnsu synth (dry-run diff or write patched .GIN + inspect)
python3 tools/audio_patcher.py patch-gnsu --preset bmw_m3_gtr --dry-run --json
python3 tools/audio_patcher.py patch-gnsu --preset camaro_ss --out build/audio_wave3/camaro_ss_patched.GIN
python3 tools/audio_inspect.py build/audio_wave3/camaro_ss_patched.GIN

# 4. Patch an ABKC bank slot with a CC0 WAV and verify with audio_inspect.py
python3 tools/audio_patcher.py patch-abkc \
  --virtual-path 'SOUND\SHIFTING\GEAR_SML_BASE.ABK' \
  --slot 1 \
  --wav build/audio_wave3/cc0_wav/gearshift_transient.wav \
  --out build/audio_wave3/GEAR_SML_BASE_patched.ABK
python3 tools/audio_inspect.py build/audio_wave3/GEAR_SML_BASE_patched.ABK --samples
```
