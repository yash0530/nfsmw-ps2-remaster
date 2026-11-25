# PS2 MIPS R5900 Code Injection, Code Caves & `ps2sdk` Mod Framework (`SLUS_213.51`)

> **Project:** NFSMW: Rockport 2026  
> **Target Binary:** `SLUS_213.51` — *Need for Speed: Most Wanted — Black Edition* (PS2 NTSC-U `v1.01`, CRC `0518D274`, SHA-1 `60289f3f8b643822a302720f9649ddc4ba55034b`)  
> **Toolchain:** [`tools/mips_cave_builder.py`](../../tools/mips_cave_builder.py) + [`tools/elf_disasm.py`](../../tools/elf_disasm.py) + [`src/ps2_mod/`](../../src/ps2_mod/README.md)  
> **Cave Manifest:** [`patches/elf_caves.json`](../../patches/elf_caves.json)

Every address, original word, register assignment, and branch delay slot documented here was verified directly by read-only disassembly of unmodified `SLUS_213.51`.

---

## 1. `SLUS_213.51` Execution Model & ABI Rules

| Property | Value |
| :--- | :--- |
| `PT_LOAD` segment | `p_vaddr = 0x00100000`, `p_offset = 0x00001000`, `p_filesz = 0x004ABBDC`, `p_memsz = 0x004D6ED0`, `PF_R \| PF_W \| PF_X` |
| `.text` code range | `0x00100000` – `0x004BE880` |
| `.data` static globals | `0x00536780` – `0x005ABBDC` |
| Compiler / ABI | Sony EE GCC (`-mabi=eabi -mgp64`, `.mdebug.eabi64`): 64-bit `sd`/`ld` register saves, 16-byte stack alignment |
| Caller-saved GPRs | `$at`, `$v0-$v1`, `$a0-$a3`, `$t0-$t9` |
| Callee-saved GPRs | `$s0-$s7`, `$gp`, `$sp`, `$fp`, `$ra` |
| Caller-saved FPRs | `$f0-$f19` (single-precision FPU; `$f20-$f31` callee-saved) |

### Critical MIPS R5900 Trampoline Rules

1. **Branch & Jump Delay Slots Always Execute:** Every `j`, `jal`, `jr`, `beq`, `bne`, `b` executes the instruction immediately following it (`PC + 4`) before transferring control.
   - **Prologue hooks** (`MainLoopBreakpoint__Fv` @ `0x003285B8`) replace `jr $ra; nop` cleanly with `j cave; nop` (leaving `0x003285BC` untouched as `nop`).
   - **Epilogue hooks** (`CalculateH` @ `0x001FC464`, `SetCurrentSunInfo` @ `0x001FADB4`) keep the existing delay-slot instruction (`addiu $sp, $sp, 0x40` / `sw $zero, 0xc($v1)`) intact at `hook + 4`, patching **only a single word** (`jr $ra` -> `j cave`)!
2. **256 MiB Jump Region:** `j`/`jal` encode a 26-bit word index within the current 256 MiB window (`(PC + 4) & 0xF0000000`). Since `.text` spans `0x00100000..0x004BE880`, all caves inside `.text` are directly reachable via single-instruction `j` without clobbering `$ra` or `$v0-$v1`.
3. **Zero Overlap with `patches/elf_patches.json`:** `tools/mips_cave_builder.py` enforces strict non-collision across both `patches/elf_caves.json` and `patches/elf_patches.json`.

---

## 2. Verified `.text` Code Cave Audit (and False-Positive Pitfalls)

Scanning `.text` for contiguous `0x00000000` (`nop`) runs reveals many zero runs that are **NOT** dead padding—they are **active busy-wait NOP slides inside hardware polling loops**:

| Address Range | Size | Verdict | Reason (Verified via Disassembly) |
| :--- | :---: | :--- | :--- |
| `0x002083FC..0x002084FC` | 256 B | **UNSAFE (DO NOT USE)** | Inside `eWaitUntilRenderingDone__Fv`: `0x002083A4: bne $v0,$zero,0x002083FC` branches directly into these 64 NOPs as a VIF1/VSync bus-yield delay loop! |
| `0x002181B8..0x00218200` | 72 B | **UNSAFE (DO NOT USE)** | Inside `FastSync__4DMACPV10DmaChannelUi`: falls straight through `0x002181B4` to `0x00218200: bc0t` when DMA channel bit `0x100` is active! |
| `0x004A3664..0x004A47D4` | 16 B each | **UNSAFE (DO NOT USE)** | Sony `sceIpuSync` / `sceCd` hardware polling loop NOP delays. |
| `0x0038FCA4`, `0x003A6FAC` | 4 B | **UNSAFE (DELAY SLOT)** | Delay slot (`nop`) of `jr $ra` at `0x0038FCA0` / `0x003A6FA8`; unused padding starts at `+4` (`0x0038FCA8`, `0x003A6FB0`). |

### Verified Safe Code Cave Regions (`0x00000000`, 0 Xrefs, 0 Branch Targets)

Every byte in the regions below sits strictly **after** an unconditional jump/return's delay slot and **before** the next valid function/entry, with **zero** code xrefs, **zero** branch/jump targets, and **zero** `.rodata`/`.data` pointer references:

| Cave Region | VAddr Range | Usable Size | Preceding Terminator | Next Symbol / Entry |
| :--- | :--- | :---: | :--- | :--- |
| **Region A** (`cave_region_a_schedule_pad`) | `0x003037A4` – `0x0030381C` | **124 B** (31 words) | `Process__23Schedule_QuarterSimRateiUs` (`j 0x00303660; andi $a2,$a2,0xffff`) | `0x00303820` (`GetIOModule` atexit dtor) |
| **Region B** (`cave_region_b_tsmempool_pad`) | `0x003E4E84` – `0x003E4EA4` | **36 B** (9 words) | `OverrideMalloc__12TSMemoryPool` (`jr $ra; addiu $sp,$sp,0x20`) | `0x003E4EA8` (`j 0x003D77E8`) |
| **Region C** (reserve) | `0x00300864` – `0x00300884` | **36 B** (9 words) | `InitializeDeviceScalar` (`jr $ra; addiu $sp,$sp,0x50`) | `0x00300888` |
| **Region D** (reserve) | `0x001F0CB4` – `0x001F0CD4` | **36 B** (9 words) | `FreeMemory__12AudioMemBasePv` (`j 0x001A2A48; addiu $a0,$a0,-0x2298`) | `0x001F0CD8` (`GetMaxCapacity`) |

---

## 3. Verified Caves Shipped in `patches/elf_caves.json`

All three caves are `enabled_by_default: false` so users/profiles opt in explicitly, and every write is verified against unmodified `SLUS_213.51`:

### 3.1 `rockport_frame_heartbeat` (MainLoop Frame Telemetry Counter)

- **Hook Site:** `MainLoopBreakpoint__Fv` (`0x003285B8`: `0x03E00008 jr $ra` -> `0x080C0DE9 j 0x003037A4`), called once per iteration from `MainLoop` (`0x00313374: jal 0x003285B8`). Delay slot `0x003285BC` stays `nop`.
- **Telemetry Word:** `debuggy` (`0x0054B3BC`, `size=4`, verified **0** code xrefs and **0** data pointers in `SLUS_213.51`), reset from `0x0000027A` to `0x00000000` at boot.
- **Cave (`0x003037A4..0x003037B4`, 5 words / 20 bytes in Region A):**

```mips
lui   $t0, 0x0055          ; hi(0x0054B3BC)
lw    $t1, -0x4C44($t0)    ; read counter @ 0x0054B3BC
addiu $t1, $t1, 1          ; increment frame counter
jr    $ra                  ; return to MainLoop @ 0x0031337C
sw    $t1, -0x4C44($t0)    ; delay slot: write back to 0x0054B3BC
```

### 3.2 `runtime_fov_multiplier_cave` (Dynamic +13.6% 3D Camera FOV Multiplier)

- **Hook Site:** `CalculateH__FUsP5eView` epilogue (`0x001FC464`: `0x03E00008 jr $ra` -> `0x080C0DF0 j 0x003037C0`). Both exit paths of `CalculateH` flow through `0x001FC464` with focal length `H` in `$f0`, and `0x001FC468` (`addiu $sp, $sp, 0x40`) stays intact in the delay slot. Both callers (`CreateViewMatricies` @ `0x0020660C`, `OccludedFlare::RenderAll` @ `0x00387DD0`) immediately consume `$f0` and overwrite `$at`/`$f1`.
- **Cave (`0x003037C0..0x003037D0`, 5 words / 20 bytes in Region A):**

```mips
lui   $at, 0x3F61
ori   $at, $at, 0x47AE     ; 0.88f -> widens 3D FOV by ~13.6% without touching 2D HUD
mtc1  $at, $f1
jr    $ra
mul.s $f0, $f0, $f1        ; delay slot: $f0 = H * 0.88f
```

### 3.3 `sun_high_noon_override` (Runtime Sun Direction Override)

- **Hook Site:** `SetCurrentSunInfo__FUi` epilogue (`0x001FADB4`: `0x03E00008 jr $ra` -> `0x080F93A1 j 0x003E4E84`). At `0x001FADB4`, `$v1` already holds `0x00547480` (`WorldLightDirectionVector`), and delay slot `0x001FADB8` (`sw $zero, 0xc($v1)`) stays intact.
- **Cave (`0x003E4E84..0x003E4EA0`, 8 words / 32 bytes in Region B):**

```mips
lui   $t0, 0xBEB5
ori   $t0, $t0, 0x04F3     ; -0.353553f (-sqrt(1/8))
sw    $t0, 0x0($v1)        ; WorldLightDirectionVector.x
sw    $t0, 0x4($v1)        ; WorldLightDirectionVector.y
lui   $t0, 0x3F5D
ori   $t0, $t0, 0xB3D7     ; +0.8660254f (+sqrt(3/4)) -> |v|^2 = 1.000
jr    $ra
sw    $t0, 0x8($v1)        ; delay slot: WorldLightDirectionVector.z
```

---

## 4. Using `tools/mips_cave_builder.py` & `src/ps2_mod/`

```bash
# Assemble a MIPS R5900 snippet & round-trip verify through elf_disasm.py
python3 tools/mips_cave_builder.py asm --base 0x003037A4 "lui \$t0, 0x0055"

# Verify elf_caves.json against elf_disasm, elf_patches.json, and live retail ISO
python3 tools/mips_cave_builder.py verify patches/elf_caves.json

# Also compatible with tools/elf_disasm.py verify-patches
python3 tools/elf_disasm.py --iso /path/to/SLUS-21351.iso verify-patches patches/elf_caves.json

# Emit and lint PCSX2 extended .pnach into gitignored build/caves_wave3/
python3 tools/mips_cave_builder.py emit-pnach patches/elf_caves.json --out build/caves_wave3/SLUS-21351_caves.pnach
```

---

## 5. Future Hooks Requiring Live Runtime Verification (Not Shipped)

- **DualShock 2 Pad Button-Combo Gates (`D0`/`E0` `.pnach` or In-Cave Toggle):**  
  Disassembly of `RealInput::DeviceImp` (`0x00492858..0x004928A0`) shows `scePadRead(port, slot, stack_buf)` packs the 16-bit inverted button mask (`nor $v1, $v1, $a0`) and stores it to `sh $v1, 0xc($s0)`, where `$s0` is a heap-allocated `DeviceImp*` (not a fixed static global). Before shipping a button-combo `.pnach` (`D0xxxxxx` / `E0xxxxxx`) or pad-gated cave, hook `0x004928A0` in a local test build to mirror `$v1` to a fixed static word (e.g., `0x0054B3B8` `VUmemEnd`) and confirm port 0 vs port 1 ordering in PCSX2.
- **In-Game Telemetry Text Banner:**  
  `Debug_ShowScreenMessage__FPCcf` (`0x002D9268`) exists in `SLUS_213.51` and calls `IPlayer` / `Find__Q43UTL3COM6Object6_IListPv`; requires runtime verification of whether the retail HUD strips debug text objects before hooking.
