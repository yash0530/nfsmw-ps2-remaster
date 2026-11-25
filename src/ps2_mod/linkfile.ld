/*
 * linkfile.ld — GNU ld script targeting retail SLUS_213.51 (NTSC-U Black Edition)
 *
 * Maps Rockport 2026 C code caves directly into verified zero-filled padding
 * regions inside SLUS_213.51's PT_LOAD executable segment (0x00100000..0x005ABBDC),
 * and binds verified retail symbols via PROVIDE() so C code can call or
 * inspect game state without relocations.
 */

OUTPUT_FORMAT("elf32-littlemips", "elf32-bigmips", "elf32-littlemips")
OUTPUT_ARCH(mips:5900)

MEMORY
{
    /* Region A: 124 bytes immediately after Process__23Schedule_QuarterSimRateiUs */
    CAVE_A (rx) : ORIGIN = 0x003037A4, LENGTH = 0x0000007C

    /* Region B: 36 bytes immediately after OverrideMalloc__12TSMemoryPoolPviPCcii */
    CAVE_B (rx) : ORIGIN = 0x003E4E84, LENGTH = 0x00000024
}

/* Verified retail SLUS_213.51 symbols (from build/symbols_slus21351.txt) */
PROVIDE(CalculateH__FUsP5eView      = 0x001FC3E8);
PROVIDE(SetCurrentSunInfo__FUi      = 0x001FACC0);
PROVIDE(eDisplayFrame__Fv           = 0x0020B2E8);
PROVIDE(MainLoopBreakpoint__Fv      = 0x003285B8);
PROVIDE(UpdateAllScreenEFX__Fv      = 0x003DE740);
PROVIDE(scePadRead                  = 0x00494700);
PROVIDE(WorldLightDirectionVector   = 0x00547480);
PROVIDE(debuggy                     = 0x0054B3BC);
PROVIDE(VBlankCounter               = 0x0054B5EC);

SECTIONS
{
    .text.rockport_cave_a :
    {
        KEEP(*(.text.rockport_cave_a*))
    } > CAVE_A

    .text.rockport_cave_b :
    {
        KEEP(*(.text.rockport_cave_b*))
    } > CAVE_B

    /DISCARD/ :
    {
        *(.reginfo)
        *(.mdebug*)
        *(.pdr)
        *(.comment)
        *(.note*)
        *(.eh_frame*)
    }
}
