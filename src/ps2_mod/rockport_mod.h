#ifndef ROCKPORT_MOD_H
#define ROCKPORT_MOD_H

#include <stdint.h>

/*
 * Verified SLUS_213.51 (NTSC-U Black Edition, CRC 0518D274) EE addresses & symbols.
 * Every address below was directly verified against retail SLUS_213.51 and
 * build/symbols_slus21351.txt.
 */

/* Verified Code Cave Regions inside SLUS_213.51 .text (0x00000000, 0 xrefs) */
#define ROCKPORT_CAVE_A_ADDR      0x003037A4u
#define ROCKPORT_CAVE_A_SIZE      124u
#define ROCKPORT_CAVE_B_ADDR      0x003E4E84u
#define ROCKPORT_CAVE_B_SIZE      36u

/* Verified Hook Sites in SLUS_213.51 .text */
#define SLUS_MAIN_LOOP_BP_ADDR    0x003285B8u /* MainLoopBreakpoint__Fv (jr $ra; nop) */
#define SLUS_CALC_H_RET_ADDR      0x001FC464u /* CalculateH__FUsP5eView epilogue */
#define SLUS_SET_SUN_RET_ADDR     0x001FADB4u /* SetCurrentSunInfo__FUi epilogue */

/* Verified Engine Functions */
typedef float (*fn_CalculateH)(uint16_t fov_angle, void *view);
typedef void  (*fn_SetCurrentSunInfo)(uint32_t sun_hash);
typedef void  (*fn_UpdateAllScreenEFX)(void);
typedef void  (*fn_eDisplayFrame)(void);
typedef int   (*fn_scePadRead)(int port, int slot, uint8_t *rdata);

#define slus_CalculateH           ((fn_CalculateH)0x001FC3E8u)
#define slus_SetCurrentSunInfo    ((fn_SetCurrentSunInfo)0x001FACC0u)
#define slus_UpdateAllScreenEFX   ((fn_UpdateAllScreenEFX)0x003DE740u)
#define slus_eDisplayFrame        ((fn_eDisplayFrame)0x0020B2E8u)
#define slus_scePadRead           ((fn_scePadRead)0x00494700u)

/* Verified Static Engine Globals (.data) */
typedef struct {
    float x;
    float y;
    float z;
    float w;
} RockportVec4;

/* Unused 4-byte EA debug symbol `debuggy` (0 xrefs in retail SLUS_213.51) */
#define g_RockportTelemetryCounter (*(volatile uint32_t *)0x0054B3BCu)

/* Active 3D sun direction vector written by SetCurrentSunInfo__FUi */
#define g_WorldLightDirection      (*(volatile RockportVec4 *)0x00547480u)

/* 60 Hz VBlank counter incremented by PSX2VBlank__Fi -> 0x002085A0 */
#define g_VBlankCounter            (*(volatile uint32_t *)0x0054B5ECu)

/* Visual & Aspect Globals (verified in patches/elf_patches.json) */
#define g_AspectRatio3dx_16_9      (*(volatile float *)0x0054BA38u)
#define g_FrustumCullWidth_16_9    (*(volatile float *)0x0054E174u)
#define g_SmearEnable              (*(volatile uint32_t *)0x0055014Cu)
#define g_SmearNumSubStages        (*(volatile uint32_t *)0x00550150u)
#define g_SkipSepiaTint            (*(volatile uint32_t *)0x00550D58u)
#define g_SkipBloomPass            (*(volatile uint32_t *)0x00550D5Cu)

/* Section placement helpers for linkfile.ld */
#define ROCKPORT_CAVE_A __attribute__((section(".text.rockport_cave_a"), used, noinline))
#define ROCKPORT_CAVE_B __attribute__((section(".text.rockport_cave_b"), used, noinline))

void ROCKPORT_CAVE_A rockport_heartbeat_hook(void);
float ROCKPORT_CAVE_A rockport_fov_multiplier_hook(float focal_h);
void ROCKPORT_CAVE_B rockport_sun_high_noon_hook(RockportVec4 *light_dir);

/*
 * Future Extension Stubs (require live PCSX2 memory/pad-buffer validation
 * before shipping binary patches in elf_caves.json):
 *
 * 1) DualShock 2 Pad Button-Combo Toggle:
 *    RealInput::DeviceImp calls scePadRead(port, slot, stack_buf) at 0x00492858
 *    and packs buttons into `sh $v1, 0xc($s0)` where $s0 is the heap-allocated
 *    DeviceImp instance. Hook 0x004928A0 with a cave that mirrors `$v1` to a
 *    verified unused word (e.g., 0x0054B3B8 VUmemEnd) before branching on
 *    L3+R3 or D-Pad combos.
 *
 * 2) In-Game Telemetry HUD Banner:
 *    Hook Debug_ShowScreenMessage__FPCcf (0x002D9268) or FEngHud::Update to
 *    render g_RockportTelemetryCounter / current FOV scale on screen.
 */

#endif /* ROCKPORT_MOD_H */
