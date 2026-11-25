#include "rockport_mod.h"

/*
 * NFSMW: Rockport 2026 — ps2sdk C Code Cave Blueprint for SLUS_213.51
 *
 * Each function below compiles into a verified zero-filled cave inside
 * SLUS_213.51's executable PT_LOAD segment (placed via linkfile.ld).
 * For guaranteed register/stack safety when hooking mid-epilogue sites
 * (e.g. CalculateH / SetCurrentSunInfo), patches/elf_caves.json ships
 * hand-audited MIPS R5900 trampolines assembled & verified by
 * tools/mips_cave_builder.py; this C blueprint provides the high-level
 * ps2sdk toolchain reference and linkable ELF target.
 */

void ROCKPORT_CAVE_A rockport_heartbeat_hook(void) {
    /*
     * Hooked from MainLoopBreakpoint__Fv @ 0x003285B8 (called once per
     * MainLoop iteration @ 0x00313374). Increments unused EA debug global
     * `debuggy` (0x0054B3BC) without touching stack or callee-saved regs.
     */
    g_RockportTelemetryCounter = g_RockportTelemetryCounter + 1u;
}

float ROCKPORT_CAVE_A rockport_fov_multiplier_hook(float focal_h) {
    /*
     * Hooked at CalculateH__FUsP5eView epilogue @ 0x001FC464.
     * Multiplying perspective focal length H by 0.88f widens 3D FOV by ~13.6%
     * across all 3D views while leaving 2D HUD and AspectRatio3dx intact.
     */
    return focal_h * 0.88f;
}

void ROCKPORT_CAVE_B rockport_sun_high_noon_hook(RockportVec4 *light_dir) {
    /*
     * Hooked at SetCurrentSunInfo__FUi epilogue @ 0x001FADB4 ($v1 == 0x00547480).
     * Forces a unit-normalized high-afternoon solar vector:
     *   (-0.353553f, -0.353553f, +0.8660254f)  (|v|^2 = 0.125 + 0.125 + 0.75 = 1.0)
     */
    light_dir->x = -0.35355339f;
    light_dir->y = -0.35355339f;
    light_dir->z =  0.86602540f;
}
