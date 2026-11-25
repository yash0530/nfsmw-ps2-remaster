# `src/ps2_mod/` — NFSMW: Rockport 2026 `ps2sdk` MIPS R5900 Mod Blueprint

This directory provides a clean-room **`ps2dev/ps2sdk` EE C mod framework** targeting verified retail `SLUS_213.51` (*Need for Speed: Most Wanted — Black Edition*, NTSC-U, CRC `0518D274`) symbols and code caves.

## Files

| File | Purpose |
| :--- | :--- |
| [`rockport_mod.h`](rockport_mod.h) | Verified `SLUS_213.51` addresses, globals, function prototypes, and future stub specs |
| [`rockport_mod.c`](rockport_mod.c) | C implementations for the frame heartbeat, FOV multiplier, and high-noon sun caves |
| [`linkfile.ld`](linkfile.ld) | GNU `ld` script placing caves into verified zero-filled `.text` regions (`0x003037A4`, `0x003E4E84`) |
| [`Makefile`](Makefile) | Builds `rockport_mod.elf` + raw cave `.bin` blobs strictly into `build/caves_wave3/ps2_mod/` |
| [`Dockerfile.ps2sdk`](Dockerfile.ps2sdk) | Reproducible `ps2dev/ps2dev:latest` container build environment |

## Building

### Option A: Zero-Dependency Python Cave Assembler & Verifier (Always Available)

You do **not** need `ps2dev` installed to assemble, verify, and emit `.pnach` or ELF cave patches. Run from the repository root:

```bash
python3 tools/mips_cave_builder.py verify patches/elf_caves.json
python3 tools/mips_cave_builder.py emit-pnach patches/elf_caves.json --out build/caves_wave3/SLUS-21351_caves.pnach
```

### Option B: Docker `ps2dev/ps2sdk` C Compilation

```bash
docker build -f src/ps2_mod/Dockerfile.ps2sdk -t rockport-ps2sdk .
docker run --rm -v "$PWD:/workspace" -w /workspace/src/ps2_mod rockport-ps2sdk make all disasm
```

### Option C: Local `ps2dev` Toolchain (`mips64r5900el-ps2-elf-gcc` or `ee-gcc`)

```bash
make -C src/ps2_mod all disasm
```

## ABI & Safety Notes

- `SLUS_213.51` was compiled by EA Black Box with Sony EE GCC using `-mabi=eabi -mgp64` (`.mdebug.eabi64`), saving 64-bit GPRs (`sd`/`ld`) on 16-byte aligned stacks.
- **Do not** hook mid-function or epilogue sites with raw C functions unless the calling convention and scratch registers match! For mid-epilogue hooks (`CalculateH` @ `0x001FC464`, `SetCurrentSunInfo` @ `0x001FADB4`), [`patches/elf_caves.json`](../../patches/elf_caves.json) uses hand-audited MIPS R5900 trampolines verified through `tools/elf_disasm.py`.
- All build outputs live strictly inside gitignored `build/caves_wave3/ps2_mod/`.
