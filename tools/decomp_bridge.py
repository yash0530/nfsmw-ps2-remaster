#!/usr/bin/env python3
"""
decomp_bridge.py — Path 2 Decomp & Native Recomp Bridge Generator for NFSMW: Rockport 2026.

Reads verified VLT recipes (`patches/vlt/*.json`), lighting recipes (`patches/lighting/*.json`),
and optionally `build/symbols_slus21351.txt` (with a built-in verified fallback table for
offline CI), and emits deterministic, self-contained C++20, C/Metal shared headers, and a
machine-readable PBR manifest into `--out-dir` (default: `build/decomp_bridge_wave3/`):

  1. `Rockport2026Config.hpp`  — C++20 `constexpr` VLT physics/camera/pursuit/visuallook deltas
                                 + verified `SLUS-21351` engine hook addresses
  2. `Rockport2026Lighting.h`  — Shared C / C++20 / Metal 3 ABI structs (`RockportSunUniforms`,
                                 `RockportVisualLookUniforms`, `RockportPBRMaterial`) and preset
                                 tables converted from `patches/lighting/*.json`
  3. `bridge_manifest.json`    — Machine-readable mapping of all 156 PS2 `LightMaterial` names &
                                 `bStringHash` values to 2026 PBR parameters (`roughness`,
                                 `metallic`, `clearcoat`, `clearcoat_roughness`, `ior`, `emissive`)

Pure Python 3 standard library (zero third-party dependencies).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 1
DEFAULT_PT_LOAD_VADDR = 0x00100000
DEFAULT_PT_LOAD_OFFSET = 0x00001000

# ---------------------------------------------------------------------------
# EA Black Box Hash Helpers (Pure Stdlib)
# ---------------------------------------------------------------------------


def bstringhash(text: str) -> int:
    """EA Black Box 32-bit bStringHash (`h = h * 33 + byte`)."""
    h = 0xFFFFFFFF
    for b in text.encode("ascii"):
        h = (h * 33 + b) & 0xFFFFFFFF
    return h


def vlt32_hash(text: str, initval: int = 0xABCDEF00) -> int:
    """EA Black Box VLT32 (Bob Jenkins lookup2 hash with init=0xABCDEF00)."""
    data = text.encode("ascii")
    length = len(data)
    a = b = 0x9E3779B9
    c = initval
    pos = 0

    def mix(x: int, y: int, z: int) -> Tuple[int, int, int]:
        x = (x - y - z) & 0xFFFFFFFF
        x ^= z >> 13
        y = (y - z - x) & 0xFFFFFFFF
        y ^= (x << 8) & 0xFFFFFFFF
        z = (z - x - y) & 0xFFFFFFFF
        z ^= y >> 13
        x = (x - y - z) & 0xFFFFFFFF
        x ^= z >> 12
        y = (y - z - x) & 0xFFFFFFFF
        y ^= (x << 16) & 0xFFFFFFFF
        z = (z - x - y) & 0xFFFFFFFF
        z ^= y >> 5
        x = (x - y - z) & 0xFFFFFFFF
        x ^= z >> 3
        y = (y - z - x) & 0xFFFFFFFF
        y ^= (x << 10) & 0xFFFFFFFF
        z = (z - x - y) & 0xFFFFFFFF
        z ^= y >> 15
        return x, y, z

    while length - pos >= 12:
        a = (a + int.from_bytes(data[pos : pos + 4], "little")) & 0xFFFFFFFF
        b = (b + int.from_bytes(data[pos + 4 : pos + 8], "little")) & 0xFFFFFFFF
        c = (c + int.from_bytes(data[pos + 8 : pos + 12], "little")) & 0xFFFFFFFF
        a, b, c = mix(a, b, c)
        pos += 12

    c = (c + length) & 0xFFFFFFFF
    rem = length - pos
    if rem >= 11:
        c = (c + (data[pos + 10] << 24)) & 0xFFFFFFFF
    if rem >= 10:
        c = (c + (data[pos + 9] << 16)) & 0xFFFFFFFF
    if rem >= 9:
        c = (c + (data[pos + 8] << 8)) & 0xFFFFFFFF
    if rem >= 8:
        b = (b + (data[pos + 7] << 24)) & 0xFFFFFFFF
    if rem >= 7:
        b = (b + (data[pos + 6] << 16)) & 0xFFFFFFFF
    if rem >= 6:
        b = (b + (data[pos + 5] << 8)) & 0xFFFFFFFF
    if rem >= 5:
        b = (b + data[pos + 4]) & 0xFFFFFFFF
    if rem >= 4:
        a = (a + (data[pos + 3] << 24)) & 0xFFFFFFFF
    if rem >= 3:
        a = (a + (data[pos + 2] << 16)) & 0xFFFFFFFF
    if rem >= 2:
        a = (a + (data[pos + 1] << 8)) & 0xFFFFFFFF
    if rem >= 1:
        a = (a + data[pos]) & 0xFFFFFFFF

    _, _, c = mix(a, b, c)
    return c


# ---------------------------------------------------------------------------
# Verified SLUS-21351 Hook Symbol Table (with offline built-in fallback)
# ---------------------------------------------------------------------------

# Tuple: (friendly_name, exact_mangled_symbol, fallback_vaddr, source_tu, role)
BUILTIN_SLUS21351_HOOKS: Tuple[Tuple[str, str, int, str, str], ...] = (
    ("eView_ctor", "__5eView", 0x001F84C0, "zEcstasy.cpp", "Constructs viewport & camera render view"),
    ("eView_SetupWorldLightContext", "SetupWorldLightContext__5eView", 0x001F89A8, "zEcstasy.cpp", "Binds primary/fill world light context"),
    ("eViewPlatInterface_RenderModel", "Render__18eViewPlatInterfaceP6eModelP8bMatrix4P13eLightContextUiT2", 0x00209810, "zEcstasy.cpp", "Platform model draw submission"),
    ("eViewPlatInterface_RenderPoly", "Render__18eViewPlatInterfaceP5ePolyP11TextureInfoP8bMatrix4if", 0x0020A280, "zEcstasy.cpp", "Platform quad/poly submission"),
    ("CreateViewMatricies", "CreateViewMatricies__FP5eViewP8bMatrix4T1T1T1T1fff", 0x00206518, "zEcstasy.cpp", "Projection & frustum culling matrices"),
    ("CalculateH", "CalculateH__FUsP5eView", 0x001FC3E8, "zEcstasy.cpp", "Perspective focal scale H"),
    ("eDisplayFrame", "eDisplayFrame__Fv", 0x0020B2E8, "zEcstasy.cpp", "Main frame submission & VBlank swap"),
    ("eModel_Init", "Init__6eModelUi", 0x001F3300, "zEcstasy.cpp", "Initializes eModel solid binding"),
    ("eModel_ConnectSolid", "ConnectSolid__6eModelP6eSolid", 0x001F33E8, "zEcstasy.cpp", "Attaches eSolid mesh pack to eModel"),
    ("eSolid_InternalLoader", "InternalLoaderSolidChunks__FP6bChunkP16eSolidListHeader", 0x001F4F38, "zEcstasy.cpp", "Parses 0x80134010 SolidPack chunks"),
    ("eSolidPlatInterface_LoaderPlatChunks", "LoaderPlatChunks__19eSolidPlatInterfaceP6bChunk", 0x002164D0, "zEcstasy.cpp", "Uploads PS2 VIF strip chunks"),
    ("epRenderStripsLit", "epRenderStripsLit__FP5eViewP6eSolidP8bMatrix4P13eLightContextUiT2", 0x002092C0, "zEcstasy.cpp", "Lit eSolid strip rendering pass"),
    ("epRenderStripsUnlit", "epRenderStripsUnlit__FP5eViewP6eSolidP8bMatrix4P13eLightContextUiT2", 0x00208E38, "zEcstasy.cpp", "Unlit eSolid strip rendering pass"),
    ("eGetRenderTarget", "eGetRenderTarget__Fi", 0x001FDAD8, "zEcstasy.cpp", "Fetches eRenderTarget slot"),
    ("eRenderTarget_UpdateGifTags", "UpdateGifTags__13eRenderTargetiiiiP17eRenderStageQueuei", 0x001FE000, "zEcstasy.cpp", "Propagates aspect scales to TARGET_PLAYER1"),
    ("MaybeChangeViewMode", "MaybeChangeViewMode__Fv", 0x002014A8, "zEcstasy.cpp", "Applies 4:3 / 16:9 / 21:9 aspect tables"),
    ("IsFaceEnabled", "IsFaceEnabled__Fi", 0x0020F598, "zEcstasy.cpp", "Controls 5-face car reflection cubemap rate"),
    ("CreateEnvMap", "CreateEnvMap__FiP13eRenderTargetT1", 0x0020F060, "zEcstasy.cpp", "Generates dynamic vehicle reflection probe"),
    ("IVisualTreatmentPlat_RenderMWVisualLook", "RenderMWVisualLook__20IVisualTreatmentPlatP5eViewP9eDmaChainT2", 0x0021A008, "zEcstasy.cpp", "Master post-processing & bloom dispatcher"),
    ("IVisualTreatmentPlat_ApplyColorBloom", "ApplyColorBloom__20IVisualTreatmentPlatP13eRenderTargetT1P9eDmaChain", 0x00219210, "zEcstasy.cpp", "Sepia tint & bloom pass"),
    ("IVisualTreatmentPlat_Radialblur", "Radialblur__20IVisualTreatmentPlatP13eRenderTargetP9eDmaChain", 0x00219A68, "zEcstasy.cpp", "High-speed / NOS radial blur pass"),
    ("IVisualTreatment_UpdateVisualLook", "UpdateVisualLook__16IVisualTreatment", 0x0040A6D0, "zWorld.cpp", "Evaluates VLT visuallook state"),
    ("eAttachFog", "eAttachFog__FP9eDmaChainUi13FogVolumeType", 0x00211220, "zEcstasy.cpp", "Atmospheric distance fog pass"),
    ("eAttachSmear2", "eAttachSmear2__FP9eDmaChainUif", 0x00213DA0, "zEcstasy.cpp", "PS2 accumulation motion-blur pass"),
    ("LoaderSun", "LoaderSun__FP6bChunk", 0x001FAC58, "zEcstasy.cpp", "Loads 0x00034202 SunInfos bChunk"),
    ("SetCurrentSunInfo_id", "SetCurrentSunInfo__FUi", 0x001FACC0, "zEcstasy.cpp", "Selects SunInfo profile by bStringHash"),
    ("SetCurrentSunInfo", "SetCurrentSunInfo__Fv", 0x001FADC0, "zEcstasy.cpp", "Applies MIDDAY/SUNSET SunInfo + WorldLightDirection"),
    ("StuffSun", "StuffSun__FP5eView", 0x00386A70, "zPlatform.cpp", "Uploads solar disc & flare sprites"),
    ("LoaderLights", "LoaderLights__FP6bChunk", 0x001F5E00, "zEcstasy.cpp", "Loads 0x00135200 eLightMaterial chunks"),
    ("elGetLightMaterial", "elGetLightMaterial__FUi", 0x001F8408, "zEcstasy.cpp", "Resolves eLightMaterial by name hash"),
    ("eLightMaterialPlatInterface_UpdatePlatInfo", "UpdatePlatInfo__27eLightMaterialPlatInterface", 0x0020E118, "zEcstasy.cpp", "Uploads GS/VU1 material constants"),
    ("CarRenderInfo_Render", "Render__13CarRenderInfoP5eViewPC8bVector3PC8bMatrix4P8bMatrix4T4T4Uiiif11CARPART_LODT11_", 0x003EC150, "zWorld.cpp", "Renders vehicle body/wheels & LOD selection"),
    ("CarRenderInfo_RenderPart", "RenderPart__13CarRenderInfoP5eViewP12CarPartModelP8bMatrix4P20eDynamicLightContextUi", 0x003F0688, "zWorld.cpp", "Submits single CarPartModel draw"),
    ("ScenerySectionHeader_DrawAScenery", "DrawAScenery__20ScenerySectionHeaderiP15SceneryCullInfoi", 0x003DAE88, "zTrack.cpp", "World scenery culling & LOD cutoff"),
    ("UpdateAllScreenEFX", "UpdateAllScreenEFX__Fv", 0x003DE740, "zTrack.cpp", "ScreenEffectDB update (1/30f -> 1/60f patch @ 0x003DE778)"),
    ("VideoSettings_Default", "Default__13VideoSettings", 0x0026D9F0, "zFe2.cpp", "Initializes default widescreen setting"),
    ("Scheduler_Init", "Init__9Schedulerf", 0x00302E80, "zMain.cpp", "Initializes 60 Hz fixed-step scheduler (1/60f)"),
    ("Scheduler_Run", "Run__9Schedulerb", 0x003030C0, "zMain.cpp", "Executes fixed-timestep simulation accumulator"),
    ("SimSystem_ctor", "__9SimSystem", 0x00390BF0, "zSim.cpp", "Constructs SimSystem inheriting 1/60f step"),
    ("SimSystem_UpdateFrame", "UpdateFrame__9SimSystem", 0x00393A88, "zSim.cpp", "Advances physics & Speedbreaker SimSpeed"),
    ("Sim_GetTimeStep", "GetTimeStep__3Simv", 0x00394918, "zSim.cpp", "Returns active simulation dt"),
    ("World_UpdateBody", "World_UpdateBody__FPQ23Sim6Packet", 0x0042E940, "zWorld.cpp", "Updates world rigid-body packet"),
    ("WorldLightDirectionVector", "WorldLightDirectionVector", 0x00547480, "zEcstasy.cpp", "ELF .data global world sun vector"),
    ("LightMaterialID", "LightMaterialID", 0x00546D38, "zEcstasy.cpp", "ELF .data active LightMaterial binding ID"),
)

# Verified SLUS_213.51 ELF .data world light & fog constants (from PS2_LIGHTING_SUNINFOS.md)
ELF_WORLD_LIGHT_DEFAULTS: Dict[str, Any] = {
    "world_light_direction_vaddr": 0x00547480,
    "primary_light_diffuse_intensity_vaddr": 0x00547490,
    "primary_light_diffuse_intensity": 6.5,
    "fill_light_diffuse_colour_vaddr": 0x005474A0,
    "fill_light_diffuse_colour": [0.87, 0.90, 1.00, 1.00],
    "fill_light_diffuse_intensity_vaddr": 0x005474B0,
    "fill_light_diffuse_intensity": 2.1,
    "fog_mode_infos_vaddr": 0x00550028,
    "base_weather_fog_vaddr": 0x00593E90,
    "base_weather_fog": 0.74,
    "base_weather_fog_start_vaddr": 0x00593E94,
    "base_weather_fog_start": 212.0,
}


# ---------------------------------------------------------------------------
# All 156 Verified Retail SLUS-21351 PS2 LightMaterials (Clean-Room Stock Table)
# Tuple: (name, diff_max_scale, diff_rgb, diff_alpha, spec_pow, spec_max_scale,
#         env_pow, env_max_scale, metallic_scale)
# ---------------------------------------------------------------------------

STOCK_PS2_LIGHT_MATERIALS: Tuple[
    Tuple[str, float, Tuple[float, float, float], float, float, float, float, float, float], ...
] = (
    ("ALUMINUM", 1.000, (0.700, 0.700, 0.700), 1.000, 1.690, 1.000, 0.132, 0.001, 0.000),
    ("BOTTOM", 0.840, (1.000, 1.000, 1.000), 1.000, 3.888, 0.000, 2.000, 0.000, 0.000),
    ("BRAKEDISC", 0.850, (1.000, 1.000, 1.000), 1.000, 1.500, 0.000, 0.000, 0.000, 0.000),
    ("BRAKELIGHT", 1.000, (1.000, 1.000, 1.000), 1.000, 1.760, 0.250, 0.200, 0.200, 0.000),
    ("BRAKELIGHTGLASS", 1.000, (1.000, 1.000, 1.000), 1.000, 2.500, 0.000, 0.400, 0.400, 0.000),
    ("BRAKES", 1.000, (0.300, 0.300, 0.300), 1.000, 1.908, 1.000, 1.620, 0.001, 0.000),
    ("CALIPER", 1.000, (1.000, 1.000, 1.000), 1.000, 4.500, 0.100, 0.000, 0.000, 0.000),
    ("CALIPERDECAL", 0.850, (1.000, 1.000, 1.000), 1.000, 1.265, 0.000, 2.000, 0.000, 0.000),
    ("CARBONFIBER", 0.700, (1.000, 1.000, 1.000), 1.000, 2.000, 0.200, 0.100, 0.100, 0.000),
    ("CARBONFIBER2", 0.700, (1.000, 1.000, 1.000), 1.000, 2.000, 0.200, 0.100, 0.100, 0.000),
    ("CHAR_CLOTH", 1.250, (1.000, 1.000, 1.000), 1.190, 0.650, 0.050, 0.000, 0.000, 0.000),
    ("CHAR_HAIR", 1.702, (0.800, 0.907, 0.870), 0.484, 2.932, 0.832, 0.000, 0.000, 0.000),
    ("CHAR_LATEX", 1.700, (0.600, 0.600, 0.600), 1.370, 5.500, 1.000, 1.000, 0.682, 0.000),
    ("CHAR_LEATHER", 1.600, (1.000, 1.000, 1.000), 1.500, 1.200, 0.095, 1.900, 0.335, 0.000),
    ("CHAR_PLASTIC", 1.010, (1.000, 1.000, 1.000), 1.000, 3.932, 1.010, 0.437, 0.001, 0.000),
    ("CHAR_RUBBER", 1.200, (1.000, 0.950, 0.900), 1.300, 1.000, 0.100, 0.000, 0.000, 0.000),
    ("CHAR_SKIN", 1.354, (1.000, 1.000, 1.000), 1.379, 3.500, 0.130, 0.000, 0.000, 0.000),
    ("CHROME", 1.000, (1.000, 1.000, 1.000), 1.000, 1.500, 0.000, 0.500, 2.000, 0.000),
    ("CLEARPLASTIC", 1.000, (1.000, 1.000, 1.000), 0.400, 2.500, 0.000, 0.400, 0.500, 0.000),
    ("CUSTOMPAINT_1", 0.200, (1.000, 1.000, 1.000), 1.000, 3.000, 1.000, 0.100, 0.100, 0.000),
    ("CUSTOMPAINT_10", 1.000, (0.000, 0.000, 2.000), 1.000, 1.000, 1.000, 0.100, 0.100, 0.000),
    ("CUSTOMPAINT_11", 0.500, (1.000, 1.000, 1.000), 1.000, 1.000, 0.000, 1.000, 2.000, 0.000),
    ("CUSTOMPAINT_12", 1.000, (1.000, 1.000, 1.000), 1.000, 0.300, 0.100, 1.000, 0.000, 0.000),
    ("CUSTOMPAINT_13", 0.500, (1.000, 1.000, 1.000), 1.000, 1.000, 0.000, 1.000, 2.000, 0.000),
    ("CUSTOMPAINT_14", 0.621, (0.553, 0.410, 0.410), 1.000, 4.096, 0.000, 0.500, 0.001, 0.000),
    ("CUSTOMPAINT_15", 0.997, (1.000, 1.000, 1.031), 1.256, 2.102, 2.000, 0.273, 0.000, 0.000),
    ("CUSTOMPAINT_16", 1.133, (1.420, 1.249, 0.683), 1.000, 5.816, 1.065, 0.423, 0.000, 0.000),
    ("CUSTOMPAINT_17", 1.000, (1.427, 1.420, 1.229), 1.000, 2.020, 0.867, 1.119, 0.000, 0.000),
    ("CUSTOMPAINT_18", 0.662, (1.003, 1.481, 1.000), 1.000, 4.014, 1.638, 0.500, 0.001, 0.000),
    ("CUSTOMPAINT_19", 0.737, (1.276, 0.662, 0.389), 1.000, 5.543, 1.386, 0.500, 0.000, 0.000),
    ("CUSTOMPAINT_2", 0.200, (1.000, 1.000, 1.000), 1.000, 3.000, 1.000, 0.100, 0.100, 0.000),
    ("CUSTOMPAINT_20", 1.331, (1.126, 1.515, 0.730), 0.143, 3.604, 0.573, 0.500, 0.001, 0.000),
    ("CUSTOMPAINT_3", 0.200, (1.000, 1.000, 1.000), 1.000, 3.000, 1.000, 0.100, 0.100, 0.000),
    ("CUSTOMPAINT_4", 1.000, (0.000, 1.000, 1.000), 1.000, 1.000, 1.000, 0.100, 0.100, 0.000),
    ("CUSTOMPAINT_5", 1.000, (1.000, 0.000, 1.000), 1.000, 1.000, 1.000, 0.100, 0.100, 0.000),
    ("CUSTOMPAINT_6", 0.200, (1.000, 1.000, 1.000), 1.000, 3.000, 1.000, 0.100, 0.100, 0.000),
    ("CUSTOMPAINT_7", 0.200, (1.000, 1.000, 1.000), 1.000, 3.000, 1.000, 0.100, 0.100, 0.000),
    ("CUSTOMPAINT_8", 0.200, (1.000, 1.000, 1.000), 1.000, 3.000, 1.000, 0.100, 0.100, 0.000),
    ("CUSTOMPAINT_9", 1.000, (1.000, 0.000, 1.000), 1.000, 1.000, 1.000, 0.100, 0.100, 0.000),
    ("DECAL", 1.200, (1.000, 1.000, 1.000), 1.000, 4.500, 0.100, 2.000, 0.000, 0.000),
    ("DEFAULT", 1.000, (1.000, 0.000, 1.000), 1.000, 2.563, 1.000, 2.000, 0.001, 0.000),
    ("DIABLOHP", 1.000, (1.000, 1.000, 1.000), 0.500, 1.250, 0.100, 0.000, 0.000, 0.000),
    ("DRIVER", 0.500, (1.000, 1.000, 1.000), 1.000, 4.000, 1.000, 0.000, 0.000, 0.000),
    ("DRIVERHEAD", 1.000, (1.000, 1.000, 1.000), 1.000, 3.500, 0.000, 0.100, 0.100, 0.000),
    ("DULLENGINE", 0.495, (1.000, 1.000, 1.000), 1.000, 5.834, 0.160, 0.072, 0.001, 0.000),
    ("DULLPLASTIC", 1.200, (1.000, 1.000, 1.000), 1.000, 1.000, 0.100, 0.000, 0.000, 0.000),
    ("ENGINE", 0.727, (1.000, 1.000, 1.000), 1.000, 2.520, 0.060, 0.000, 0.000, 0.000),
    ("EXHAUST_TIP", 0.000, (0.000, 0.000, 0.000), 1.000, 4.000, 1.000, 2.000, 0.001, 0.000),
    ("GOLDROTOR", 0.389, (1.000, 0.860, 0.246), 1.000, 2.337, 1.188, 0.419, 0.001, 0.000),
    ("GRILL", 1.045, (1.000, 1.000, 1.000), 1.340, 3.271, 0.550, 0.137, 0.000, 0.000),
    ("HEADLIGHT", 1.000, (1.000, 1.000, 1.000), 0.000, 1.700, 0.160, 0.000, 0.000, 0.000),
    ("HEADLIGHTGLASS", 1.000, (1.000, 1.000, 1.000), 0.350, 2.500, 0.000, 1.000, 3.000, 0.000),
    ("HEADLIGHTREFLECTOR", 2.000, (1.000, 1.000, 1.000), 1.000, 1.000, 0.200, 0.000, 0.000, 0.000),
    ("HOSES", 1.000, (1.000, 1.000, 1.000), 1.000, 3.500, 0.750, 1.500, 0.100, 1.000),
    ("INTERIOR", 1.000, (1.000, 1.000, 1.000), 1.000, 0.923, 0.069, 0.000, 0.000, 0.000),
    ("LICENSEPLATE", 1.000, (1.000, 1.000, 1.000), 1.000, 2.500, 0.100, 0.000, 0.000, 0.000),
    ("MAGCHROME", 0.500, (1.000, 1.000, 1.000), 1.000, 4.500, 0.000, 0.500, 2.000, 0.000),
    ("MAGGUNMETAL", 0.584, (1.004, 1.000, 1.000), 1.000, 4.345, 1.086, 0.899, 0.001, 0.000),
    ("MAGSILVER", 1.000, (1.000, 1.000, 1.000), 1.000, 2.000, 0.500, 0.500, 0.500, 1.000),
    ("MAGSILVERGLOSS", 1.000, (1.004, 1.000, 1.000), 1.000, 1.097, 1.000, 0.151, 0.001, 0.000),
    ("MATTEPLASTIC", 1.000, (1.000, 1.000, 1.000), 1.000, 2.130, 1.000, 0.373, 0.001, 0.000),
    ("MESH", 1.000, (1.000, 1.000, 1.000), 1.000, 4.000, 0.970, 0.000, 0.000, 0.000),
    ("METPAINTBLACK", 1.000, (1.000, 1.000, 1.000), 1.000, 3.000, 0.200, 0.200, 0.300, 1.000),
    ("METPAINTBLUE", 1.000, (1.000, 1.000, 1.000), 1.000, 3.000, 0.200, 0.200, 0.200, 1.000),
    ("METPAINTGOLD", 1.000, (1.000, 1.000, 1.000), 1.000, 3.000, 0.500, 0.150, 0.200, 1.000),
    ("METPAINTGRAY", 1.500, (1.000, 1.000, 1.000), 1.000, 8.000, 0.000, 0.150, 0.200, 1.000),
    ("METPAINTGREEN", 1.000, (1.000, 1.000, 1.000), 1.000, 3.000, 0.200, 0.200, 0.200, 1.000),
    ("METPAINTRED", 1.000, (1.000, 1.000, 1.000), 1.000, 3.000, 0.200, 0.200, 0.300, 1.000),
    ("METPAINTSILVER", 1.000, (1.000, 1.000, 1.000), 1.000, 3.000, 0.300, 0.150, 0.200, 1.000),
    ("METPAINTYELLOW", 1.000, (1.000, 1.000, 1.000), 1.000, 3.000, 0.200, 0.150, 0.200, 1.000),
    ("MIRROR", 1.000, (0.000, 0.000, 0.000), 1.000, 1.893, 1.000, 2.000, 0.001, 0.000),
    ("MOLDINGS", 1.000, (1.000, 1.000, 1.000), 1.000, 2.200, 0.200, 0.000, 0.000, 0.000),
    ("PEARL", 0.954, (1.933, 0.914, 0.906), 0.072, 4.105, 0.996, 0.150, 0.100, 1.000),
    ("PEARL1", 1.108, (0.856, 0.000, 0.122), 0.082, 4.015, 0.986, 0.150, 0.100, 1.000),
    ("PEARL2", 1.072, (0.809, 1.113, 1.019), 0.060, 1.408, 0.350, 0.150, 0.100, 1.000),
    ("PEARL3", 0.583, (0.683, 1.101, 0.000), 0.086, 3.367, 1.122, 0.150, 0.100, 1.000),
    ("PEARL4", 0.863, (1.416, 1.468, 0.000), 0.060, 1.842, 1.000, 0.150, 0.100, 1.000),
    ("PEARL5", 0.966, (0.978, 0.993, 2.000), 1.122, 2.763, 1.194, 0.150, 0.100, 1.000),
    ("PEARL6", 1.209, (1.116, 1.393, 0.037), 1.122, 1.750, 1.086, 0.150, 0.100, 1.000),
    ("PEARL7", 1.798, (0.142, 0.157, 0.951), 1.004, 1.918, 1.273, 0.150, 0.100, 1.000),
    ("PEARL8", 0.906, (0.000, 1.498, 0.000), 0.951, 3.540, 1.371, 0.150, 0.100, 1.000),
    ("PEARL9", 1.200, (1.461, 1.303, 0.255), 0.259, 4.225, 0.605, 0.150, 0.100, 1.000),
    ("PLAINNOTHING", 0.000, (0.000, 0.000, 0.000), 1.000, 0.000, 0.000, 0.000, 0.000, 0.000),
    ("PLASTICHUBCAP", 1.000, (1.000, 1.000, 1.000), 1.000, 2.130, 1.000, 0.373, 0.001, 0.000),
    ("RAD", 1.000, (1.000, 1.000, 1.000), 1.000, 4.000, 1.000, 0.000, 0.000, 0.000),
    ("REGPAINTBLACK", 1.000, (1.000, 1.000, 1.000), 1.000, 8.000, 0.000, 0.200, 0.300, 0.000),
    ("REGPAINTBLUE", 1.000, (1.000, 1.000, 1.000), 1.000, 6.000, 0.200, 0.200, 0.200, 0.000),
    ("REGPAINTGRAY", 1.000, (1.000, 1.000, 1.000), 1.000, 4.000, 0.200, 0.150, 0.200, 0.000),
    ("REGPAINTGREEN", 1.000, (1.000, 1.000, 1.000), 1.000, 8.000, 0.200, 0.150, 0.200, 0.000),
    ("REGPAINTORANGE", 1.000, (1.000, 1.000, 1.000), 1.000, 6.000, 0.000, 0.150, 0.200, 0.000),
    ("REGPAINTPINK", 1.000, (1.000, 1.000, 1.000), 1.000, 8.000, 0.200, 0.250, 0.200, 0.000),
    ("REGPAINTRED", 1.000, (1.000, 1.000, 1.000), 1.000, 5.000, 0.000, 0.200, 0.300, 0.000),
    ("REGPAINTWHITE", 1.000, (1.000, 1.000, 0.990), 1.000, 8.000, 0.200, 0.250, 0.300, 0.000),
    ("REGPAINTYELLOW", 1.000, (1.000, 1.000, 1.000), 1.000, 4.000, 0.000, 0.150, 0.200, 0.000),
    ("RIMPEARL", 0.949, (0.949, 0.949, 0.949), 1.000, 3.904, 0.996, 0.479, 0.001, 0.000),
    ("RIMPEARL1", 1.108, (0.856, 0.246, 0.122), 1.000, 4.015, 0.986, 0.524, 0.001, 0.000),
    ("RIMPEARL2", 1.072, (0.096, 1.031, 1.038), 1.000, 3.003, 0.532, 0.369, 0.001, 0.000),
    ("RIMPEARL3", 0.799, (1.010, 1.101, 0.000), 1.000, 5.488, 1.181, 1.024, 0.001, 0.000),
    ("RIMPEARL4", 0.863, (1.416, 1.468, 0.000), 1.000, 2.348, 1.000, 0.390, 0.001, 0.000),
    ("RIMPEARL5", 0.966, (1.474, 0.993, 2.000), 1.000, 2.763, 1.194, 0.315, 0.001, 0.000),
    ("RIMPEARL6", 0.840, (1.116, 1.393, 0.037), 1.000, 1.750, 1.086, 0.245, 0.001, 0.000),
    ("RIMPEARL7", 0.997, (0.142, 0.157, 0.951), 1.000, 1.918, 1.273, 0.315, 0.001, 0.000),
    ("RIMPEARL8", 0.906, (0.000, 1.498, 0.000), 1.000, 3.540, 1.371, 0.150, 0.001, 0.000),
    ("RIMPEARL9", 0.997, (1.461, 1.303, 0.255), 1.000, 4.225, 0.605, 0.614, 0.001, 0.000),
    ("ROAD", 1.000, (0.900, 0.570, 0.560), 1.000, 1.523, 1.000, 2.000, 0.001, 0.000),
    ("RUBBER", 1.000, (1.000, 0.950, 0.900), 1.000, 1.500, 0.100, 0.000, 0.000, 0.000),
    ("SHINYMOLDINGS", 1.004, (1.000, 1.000, 1.000), 1.004, 1.155, 0.152, 0.173, 0.001, 0.000),
    ("SHINYPLASTIC", 1.010, (1.000, 1.000, 1.000), 1.000, 3.932, 1.010, 0.437, 0.001, 0.000),
    ("TRAFFIC", 1.000, (1.000, 1.000, 1.000), 1.000, 2.500, 0.000, 0.100, 0.000, 0.000),
    ("TRAFFICWINDOWS", 1.000, (1.000, 1.000, 1.000), 0.300, 4.000, 0.000, 0.200, 0.100, 0.000),
    ("USER_CALIPERS", 1.018, (1.000, 1.000, 1.000), 0.289, 3.812, 0.000, 0.404, 0.001, 0.000),
    ("USER_EXHAUST", 1.000, (1.000, 1.000, 1.000), 0.724, 2.894, 1.000, 2.382, 0.001, 0.000),
    ("USER_EXHAUST2", 1.000, (1.000, 1.000, 1.000), 0.724, 2.894, 1.000, 2.382, 0.001, 0.000),
    ("USER_HOSES", 0.979, (0.510, 0.497, 1.324), 0.421, 0.993, 0.117, 0.414, 0.001, 0.000),
    ("USER_RIMS", 1.000, (1.000, 1.000, 1.000), 1.000, 2.000, 0.500, 0.500, 0.250, 1.000),
    ("USER_RIMS_CHROME", 1.000, (1.000, 1.000, 1.000), 1.000, 2.500, 0.500, 1.000, 1.500, 0.000),
    ("USER_RIMS_GLOSS", 0.997, (0.983, 1.000, 1.000), 1.000, 5.898, 0.901, 0.123, 0.001, 0.000),
    ("USER_RIMS_GUNMETAL", 1.000, (1.004, 1.000, 1.000), 1.000, 2.500, 0.200, 1.000, 0.001, 0.000),
    ("USER_RIMS_MAGSILVER", 0.494, (1.004, 1.000, 1.000), 1.000, 1.019, 1.206, 0.939, 0.001, 0.000),
    ("USER_RIMS_MET", 1.003, (1.000, 1.000, 1.000), 1.000, 7.208, 1.365, 0.939, 0.001, 0.000),
    ("USER_RIMS_SILVERGLOSS", 0.494, (1.004, 1.000, 1.000), 1.000, 1.019, 1.206, 0.939, 0.001, 0.000),
    ("USER_SPOILER", 0.908, (1.000, 1.000, 0.769), 0.179, 2.123, 0.569, 0.510, 0.001, 0.000),
    ("WHEELBLUR", 1.000, (0.900, 0.570, 0.560), 1.000, 1.523, 1.000, 2.000, 0.001, 0.000),
    ("WHITE", 1.000, (0.990, 0.980, 0.980), 1.000, 0.650, 1.000, 0.650, 0.001, 0.000),
    ("WINDOWMASK", 1.000, (1.000, 1.000, 1.000), 0.400, 2.500, 0.000, 1.000, 0.500, 0.000),
    ("WINDSHIELD", 1.000, (1.000, 1.000, 1.000), 0.400, 2.500, 0.000, 1.000, 0.500, 0.000),
    ("WINDSHIELD_TINT_L1_BLACK", 0.750, (1.000, 1.000, 1.000), 0.550, 2.500, 0.000, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L1_GREEN", 1.000, (0.500, 1.000, 0.500), 0.550, 2.500, 0.000, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L1_MBLACK", 0.500, (1.000, 1.000, 1.000), 0.650, 2.500, 0.000, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L1_MBLUE", 0.500, (0.500, 0.500, 1.000), 0.650, 2.500, 0.000, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L1_MGREEN", 0.500, (0.500, 1.000, 0.500), 0.650, 2.500, 0.000, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L1_MRED", 0.500, (1.000, 0.500, 0.500), 0.650, 2.500, 0.000, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L2_AQUA", 0.500, (0.000, 1.000, 1.000), 0.750, 2.500, 0.250, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L2_BLACK", 0.250, (1.000, 1.000, 1.000), 0.750, 2.500, 0.000, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L2_BLUE", 0.700, (0.000, 0.000, 1.000), 0.750, 2.500, 0.000, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L2_GOLD", 1.000, (1.000, 0.700, 0.000), 0.750, 2.500, 0.250, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L2_GRAPE", 1.000, (0.500, 0.000, 1.000), 0.750, 2.500, 0.000, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L2_GREEN", 0.250, (0.000, 1.000, 0.000), 0.750, 2.500, 0.000, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L2_LAVENDER", 1.000, (0.900, 0.200, 0.800), 0.750, 2.500, 0.250, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L2_LIME", 1.000, (0.000, 1.000, 0.250), 0.750, 2.500, 0.500, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L2_ORANGE", 0.750, (1.000, 0.500, 0.000), 0.750, 2.500, 0.250, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L2_PINK", 0.750, (1.000, 0.000, 0.500), 0.750, 2.500, 0.250, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L2_RED", 0.500, (1.000, 0.000, 0.000), 0.750, 2.500, 0.000, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L2_YELLOW", 1.000, (0.900, 1.000, 0.000), 0.750, 2.500, 0.000, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L3_AQUA", 1.550, (0.600, 1.100, 1.400), 0.750, 5.500, 1.000, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L3_BLACK", 0.250, (1.000, 1.000, 1.000), 0.750, 2.500, 0.000, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L3_BLUE", 0.250, (0.500, 0.500, 1.000), 0.750, 2.500, 0.250, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L3_GOLD", 1.000, (1.000, 1.000, 0.000), 0.750, 6.000, 0.500, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L3_GRAPE", 0.500, (0.400, 0.000, 1.000), 0.750, 4.000, 1.000, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L3_GREEN", 0.250, (0.500, 1.000, 0.500), 0.750, 2.500, 0.250, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L3_LAVENDER", 1.000, (0.300, 0.000, 1.000), 0.750, 5.500, 0.000, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L3_LIME", 1.000, (0.000, 1.000, 0.000), 0.750, 5.500, 0.600, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L3_ORANGE", 1.000, (1.000, 0.450, 0.000), 0.750, 5.000, 1.000, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L3_PINK", 0.500, (1.000, 0.000, 0.400), 0.750, 5.500, 1.300, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L3_RED", 0.500, (1.000, 0.000, 0.000), 0.750, 2.500, 0.250, 0.400, 0.500, 0.000),
    ("WINDSHIELD_TINT_L3_YELLOW", 1.000, (1.000, 1.000, 0.000), 0.750, 2.500, 0.250, 0.400, 0.500, 0.000),
)

PBR_CATEGORIES: Tuple[Tuple[str, int], ...] = (
    ("ROCKPORT_MAT_DEFAULT", 0),
    ("ROCKPORT_MAT_METALLIC_PAINT", 1),
    ("ROCKPORT_MAT_GLOSS_PAINT", 2),
    ("ROCKPORT_MAT_PEARL_PAINT", 3),
    ("ROCKPORT_MAT_CUSTOM_PAINT", 4),
    ("ROCKPORT_MAT_CHROME", 5),
    ("ROCKPORT_MAT_BRUSHED_METAL", 6),
    ("ROCKPORT_MAT_CARBON_FIBER", 7),
    ("ROCKPORT_MAT_GLASS", 8),
    ("ROCKPORT_MAT_EMISSIVE_LAMP", 9),
    ("ROCKPORT_MAT_RUBBER", 10),
    ("ROCKPORT_MAT_ROAD", 11),
    ("ROCKPORT_MAT_MATTE_PLASTIC", 12),
    ("ROCKPORT_MAT_CHARACTER", 13),
)
PBR_CATEGORY_ENUM = dict(PBR_CATEGORIES)


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class HookSymbol:
    friendly_name: str
    mangled_symbol: str
    vaddr: int
    elf_offset: int
    source_unit: str
    role: str
    resolved_from_file: bool


@dataclasses.dataclass(frozen=True)
class VltDelta:
    recipe_id: str
    class_name: str
    node_name: str
    field_name: str
    member_name: str
    index: int
    expected_offset: int
    value_kind: str  # "float", "int", "vec4"
    orig_value: Any
    new_value: Any

    @property
    def class_hash(self) -> int:
        return vlt32_hash(self.class_name)

    @property
    def node_hash(self) -> int:
        return vlt32_hash(self.node_name)

    @property
    def field_hash(self) -> int:
        return vlt32_hash(self.field_name)


# ---------------------------------------------------------------------------
# Symbol File Parser (Optional build/symbols_slus21351.txt)
# ---------------------------------------------------------------------------

_SYM_LINE_RE = re.compile(r"^\s*([A-Za-z_.$?@0-9]+)\s*=\s*0x([0-9A-Fa-f]+)\s*;")


def parse_symbol_map(path: Path) -> Dict[str, int]:
    """Parse a splat/decomp `symbols.txt` file into `{symbol_name: vaddr}`."""
    sym_map: Dict[str, int] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _SYM_LINE_RE.match(raw)
        if m:
            sym_map[m.group(1)] = int(m.group(2), 16)
    return sym_map


def resolve_hooks(symbols_path: Optional[Path]) -> Tuple[List[HookSymbol], str]:
    """Resolve verified `SLUS-21351` hook addresses from file if available, else fallback."""
    sym_map: Dict[str, int] = {}
    source_label = "builtin_verified_fallback"
    if symbols_path is not None and symbols_path.is_file():
        sym_map = parse_symbol_map(symbols_path)
        source_label = symbols_path.name

    hooks: List[HookSymbol] = []
    for friendly, mangled, fallback_vaddr, tu, role in BUILTIN_SLUS21351_HOOKS:
        if mangled in sym_map:
            vaddr = sym_map[mangled]
            resolved = True
        else:
            vaddr = fallback_vaddr
            resolved = False
        elf_off = vaddr - DEFAULT_PT_LOAD_VADDR + DEFAULT_PT_LOAD_OFFSET
        hooks.append(
            HookSymbol(
                friendly_name=friendly,
                mangled_symbol=mangled,
                vaddr=vaddr,
                elf_offset=elf_off,
                source_unit=tu,
                role=role,
                resolved_from_file=resolved,
            )
        )
    return hooks, source_label


# ---------------------------------------------------------------------------
# VLT & Lighting Recipe Loaders
# ---------------------------------------------------------------------------


def load_vlt_deltas(vlt_dir: Path) -> Dict[str, List[VltDelta]]:
    """Load all `patches/vlt/*.json` recipes cleanly in deterministic order."""
    grouped: Dict[str, List[VltDelta]] = {}
    for path in sorted(vlt_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        recipe_id = str(payload.get("id", path.stem))
        deltas: List[VltDelta] = []
        for entry in payload.get("patches", []):
            orig_val = entry["expected_orig"]
            new_val = entry["new_value"]
            if isinstance(new_val, list):
                if len(new_val) != 4:
                    raise ValueError(f"{path.name}: expected vec4 list, got {new_val!r}")
                kind = "vec4"
            elif isinstance(new_val, bool):
                kind = "int"
                orig_val = int(orig_val)
                new_val = int(new_val)
            elif isinstance(new_val, int) and isinstance(orig_val, int):
                kind = "int"
            else:
                kind = "float"
                orig_val = float(orig_val)
                new_val = float(new_val)

            off_raw = entry.get("expected_offset", "0x0")
            off = int(off_raw, 16) if isinstance(off_raw, str) else int(off_raw)
            deltas.append(
                VltDelta(
                    recipe_id=recipe_id,
                    class_name=str(entry["class"]),
                    node_name=str(entry["node"]),
                    field_name=str(entry["field"]),
                    member_name=str(entry.get("member", "")),
                    index=int(entry.get("index", 0)),
                    expected_offset=off,
                    value_kind=kind,
                    orig_value=orig_val,
                    new_value=new_val,
                )
            )
        grouped[recipe_id] = deltas
    return grouped


def load_lighting_recipes(lighting_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Load `patches/lighting/*.json` cleanly."""
    recipes: Dict[str, Dict[str, Any]] = {}
    for path in sorted(lighting_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        recipes[str(payload.get("id", path.stem))] = payload
    return recipes


# ---------------------------------------------------------------------------
# SunInfos & VisualLook -> Metal Uniform Conversion
# ---------------------------------------------------------------------------


def _ps2_pos_to_yup_dir(pos: Sequence[float]) -> List[float]:
    """Transform PS2 Z-up `(x, y, z)` sun vector to Metal Y-up unit direction `(x, z, -y)`."""
    mx, my, mz = float(pos[0]), float(pos[2]), -float(pos[1])
    mag = math.sqrt(mx * mx + my * my + mz * mz)
    if mag <= 1e-8:
        return [0.0, 1.0, 0.0]
    return [round(mx / mag, 6), round(my / mag, 6), round(mz / mag, 6)]


def _rgba8_to_linear(rgba: Sequence[int]) -> List[float]:
    """Convert PS2 8-bit sRGB `[R, G, B, A]` (`A=128 -> 1.0`) to normalized linear RGB."""
    out: List[float] = []
    for c in rgba[:3]:
        s = max(0.0, min(1.0, float(c) / 255.0))
        lin = s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4
        out.append(round(lin, 6))
    return out


def _extract_sun_preset(
    recipe: Dict[str, Any],
    profile_name: str,
    visuallook_deltas: Sequence[VltDelta],
    visuallook_node: str,
    preset_id: str,
    hdr_exposure: float,
    bloom_threshold: float,
) -> Dict[str, Any]:
    """Build exact `RockportSunUniforms` & `RockportVisualLookUniforms` from JSON recipes."""
    patches = recipe.get("patches", [])
    sun_pos = [5300.0, -3550.0, 3000.0]
    layers: Dict[int, Dict[str, Any]] = {
        0: {"IntensityScale": 1.0, "Size": 60.0, "Colour": [255, 200, 100, 128]},
        1: {"IntensityScale": 0.5, "Size": 320.0, "Colour": [130, 110, 25, 128]},
        2: {"IntensityScale": 0.7, "Size": 800.0, "Colour": [159, 122, 75, 128]},
    }
    mat_overrides: Dict[str, Dict[str, Any]] = {}

    for p in patches:
        sec = p.get("section")
        if sec == "SunInfos" and p.get("profile") == profile_name:
            field = p.get("field")
            if field == "Position":
                sun_pos = [float(v) for v in p["new_value"]]
            elif field == "SunLayers":
                idx = int(p["index"])
                member = str(p["member"])
                layers.setdefault(idx, {})[member] = p["new_value"]
        elif sec == "LightMaterials":
            mat_name = str(p["material"])
            mat_overrides.setdefault(mat_name, {})[str(p["field"])] = p["new_value"]

    vl_tint = [0.96, 0.94, 0.90, 1.0]
    vl_bloom = 0.18
    vl_desat = 0.06
    vl_black = 0.14
    for d in visuallook_deltas:
        if d.node_name != visuallook_node:
            continue
        if d.field_name == "ColourBloomTint":
            vl_tint = [float(x) for x in d.new_value]
        elif d.field_name == "ColourBloomIntensity":
            vl_bloom = float(d.new_value)
        elif d.field_name == "Desaturation":
            vl_desat = float(d.new_value)
        elif d.field_name == "BlackBloomIntensity":
            vl_black = float(d.new_value)

    sun_dir = _ps2_pos_to_yup_dir(sun_pos)
    c0 = _rgba8_to_linear(layers[0]["Colour"])
    c1 = _rgba8_to_linear(layers[1]["Colour"])
    c2 = _rgba8_to_linear(layers[2]["Colour"])

    return {
        "id": preset_id,
        "ps2_profile": profile_name,
        "ps2_sun_position": sun_pos,
        "sun_uniforms": {
            "sun_direction_intensity": [
                sun_dir[0],
                sun_dir[1],
                sun_dir[2],
                round(ELF_WORLD_LIGHT_DEFAULTS["primary_light_diffuse_intensity"] * float(layers[0]["IntensityScale"]), 4),
            ],
            "sun_color_size": [c0[0], c0[1], c0[2], round(float(layers[0]["Size"]), 4)],
            "corona_color_intensity": [c1[0], c1[1], c1[2], round(float(layers[1]["IntensityScale"]), 4)],
            "ray_color_intensity": [c2[0], c2[1], c2[2], round(float(layers[2]["IntensityScale"]), 4)],
            "fill_color_intensity": [
                ELF_WORLD_LIGHT_DEFAULTS["fill_light_diffuse_colour"][0],
                ELF_WORLD_LIGHT_DEFAULTS["fill_light_diffuse_colour"][1],
                ELF_WORLD_LIGHT_DEFAULTS["fill_light_diffuse_colour"][2],
                ELF_WORLD_LIGHT_DEFAULTS["fill_light_diffuse_intensity"],
            ],
        },
        "visuallook_uniforms": {
            "bloom_tint_intensity": [
                round(vl_tint[0], 4),
                round(vl_tint[1], 4),
                round(vl_tint[2], 4),
                round(vl_bloom, 4),
            ],
            "grading_params": [
                round(vl_desat, 4),
                round(vl_black, 4),
                round(hdr_exposure, 4),
                round(bloom_threshold, 4),
            ],
        },
        "material_overrides": mat_overrides,
    }


def build_lighting_presets(
    lighting_recipes: Dict[str, Dict[str, Any]],
    visuallook_deltas: Sequence[VltDelta],
) -> Dict[str, Dict[str, Any]]:
    """Build `daylight`, `golden_hour`, and `night_rain` Metal uniform presets."""
    daylight_recipe = lighting_recipes.get("rockport2026_daylight", {"patches": []})
    golden_recipe = lighting_recipes.get("rockport2026_golden_hour", {"patches": []})

    daylight = _extract_sun_preset(
        daylight_recipe,
        profile_name="MIDDAY",
        visuallook_deltas=visuallook_deltas,
        visuallook_node="default",
        preset_id="daylight",
        hdr_exposure=1.10,
        bloom_threshold=1.05,
    )
    golden_hour = _extract_sun_preset(
        golden_recipe,
        profile_name="SUNSET",
        visuallook_deltas=visuallook_deltas,
        visuallook_node="sunset",
        preset_id="golden_hour",
        hdr_exposure=1.15,
        bloom_threshold=0.95,
    )

    # Night/rain showcase preset aligned with `visuallook/uves` and wet asphalt shader mode
    uves_bloom = 0.24
    uves_desat = 0.10
    uves_black = 0.16
    for d in visuallook_deltas:
        if d.node_name == "uves":
            if d.field_name == "ColourBloomIntensity":
                uves_bloom = float(d.new_value)
            elif d.field_name == "Desaturation":
                uves_desat = float(d.new_value)
            elif d.field_name == "BlackBloomIntensity":
                uves_black = float(d.new_value)

    night_dir = _ps2_pos_to_yup_dir([-2800.0, 2200.0, 2400.0])
    night_rain = {
        "id": "night_rain",
        "ps2_profile": "PROTOTYPE",
        "ps2_sun_position": [-2800.0, 2200.0, 2400.0],
        "sun_uniforms": {
            "sun_direction_intensity": [night_dir[0], night_dir[1], night_dir[2], 2.25],
            "sun_color_size": [0.42, 0.56, 0.88, 36.0],
            "corona_color_intensity": [0.18, 0.34, 0.68, 0.40],
            "ray_color_intensity": [0.22, 0.38, 0.72, 0.25],
            "fill_color_intensity": [0.45, 0.58, 0.88, 1.35],
        },
        "visuallook_uniforms": {
            "bloom_tint_intensity": [0.76, 0.86, 1.00, round(uves_bloom, 4)],
            "grading_params": [round(uves_desat, 4), round(uves_black, 4), 1.25, 0.85],
        },
        "material_overrides": {},
    }

    return {
        "daylight": daylight,
        "golden_hour": golden_hour,
        "night_rain": night_rain,
    }


# ---------------------------------------------------------------------------
# PS2 LightMaterial -> 2026 Cook-Torrance PBR Translation
# ---------------------------------------------------------------------------


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def classify_material(name: str) -> str:
    """Map an EA `SLUS-21351` LightMaterial name to a canonical 2026 PBR category."""
    u = name.upper()
    if u in {"BRAKELIGHT", "HEADLIGHT", "HEADLIGHTREFLECTOR"}:
        return "ROCKPORT_MAT_EMISSIVE_LAMP"
    if u.startswith("WINDSHIELD") or u in {"WINDOWMASK", "TRAFFICWINDOWS", "CLEARPLASTIC", "BRAKELIGHTGLASS", "HEADLIGHTGLASS"}:
        return "ROCKPORT_MAT_GLASS"
    if u in {"CHROME", "MAGCHROME", "USER_RIMS_CHROME", "MIRROR"}:
        return "ROCKPORT_MAT_CHROME"
    if u in {"CARBONFIBER", "CARBONFIBER2"}:
        return "ROCKPORT_MAT_CARBON_FIBER"
    if u in {"ROAD"}:
        return "ROCKPORT_MAT_ROAD"
    if u in {"RUBBER", "WHEELBLUR"}:
        return "ROCKPORT_MAT_RUBBER"
    if u.startswith("CHAR_"):
        return "ROCKPORT_MAT_CHARACTER"
    if u.startswith("METPAINT"):
        return "ROCKPORT_MAT_METALLIC_PAINT"
    if u.startswith("PEARL") or u.startswith("RIMPEARL"):
        return "ROCKPORT_MAT_PEARL_PAINT"
    if u.startswith("REGPAINT"):
        return "ROCKPORT_MAT_GLOSS_PAINT"
    if u.startswith("CUSTOMPAINT"):
        return "ROCKPORT_MAT_CUSTOM_PAINT"
    if u in {
        "ALUMINUM",
        "BRAKEDISC",
        "CALIPER",
        "DULLENGINE",
        "ENGINE",
        "EXHAUST_TIP",
        "GOLDROTOR",
        "GRILL",
        "HOSES",
        "MAGGUNMETAL",
        "MAGSILVER",
        "MAGSILVERGLOSS",
        "RAD",
        "SHINYMOLDINGS",
        "USER_CALIPERS",
        "USER_EXHAUST",
        "USER_EXHAUST2",
        "USER_RIMS",
        "USER_RIMS_GLOSS",
        "USER_RIMS_GUNMETAL",
        "USER_RIMS_MAGSILVER",
        "USER_RIMS_MET",
        "USER_RIMS_SILVERGLOSS",
    }:
        return "ROCKPORT_MAT_BRUSHED_METAL"
    if u in {"BOTTOM", "BRAKES", "DRIVER", "DRIVERHEAD", "DULLPLASTIC", "INTERIOR", "MATTEPLASTIC", "MESH", "MOLDINGS", "PLASTICHUBCAP", "SHINYPLASTIC", "USER_HOSES"}:
        return "ROCKPORT_MAT_MATTE_PLASTIC"
    return "ROCKPORT_MAT_DEFAULT"


def translate_light_material_to_pbr(
    index: int,
    name: str,
    diff_scale: float,
    diff_rgb: Tuple[float, float, float],
    diff_alpha: float,
    spec_power: float,
    spec_scale: float,
    env_power: float,
    env_scale: float,
    metallic_scale: float,
    daylight_override: Optional[Dict[str, Any]] = None,
    golden_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Translate one PS2 `eLightMaterial` into a 2026 Cook-Torrance + Clearcoat PBR record."""
    # Apply daylight overrides as our neutral reference 2026 baseline where present
    eff_diff_scale = float(daylight_override.get("DiffuseMaxScale", diff_scale)) if daylight_override else diff_scale
    eff_diff_rgb = (
        tuple(float(v) for v in daylight_override["DiffuseMaxRGB"])
        if daylight_override and "DiffuseMaxRGB" in daylight_override
        else diff_rgb
    )
    eff_spec_pow = float(daylight_override.get("SpecularPower", spec_power)) if daylight_override else spec_power
    eff_spec_scale = float(daylight_override.get("SpecularMaxScale", spec_scale)) if daylight_override else spec_scale
    eff_env_scale = float(daylight_override.get("EnvmapMaxScale", env_scale)) if daylight_override else env_scale

    category = classify_material(name)
    n_eff = 2.0 + max(0.0, eff_spec_pow) * 18.0
    base_roughness = _clamp(math.sqrt(2.0 / (n_eff + 2.0)), 0.04, 0.96)

    # Normalize HDR-authored PS2 diffuse values cleanly into [0, 1] linear albedo
    albedo = [round(_clamp(c * _clamp(eff_diff_scale, 0.15, 1.35), 0.02, 1.0), 4) for c in eff_diff_rgb]
    alpha = round(_clamp(diff_alpha, 0.0, 1.0), 4)
    emissive = [0.0, 0.0, 0.0]
    clearcoat = 0.0
    clearcoat_roughness = 0.50
    ior = 1.50
    roughness = base_roughness
    metallic = _clamp(metallic_scale, 0.0, 1.0)
    flags = 0

    if category == "ROCKPORT_MAT_CHROME":
        roughness = _clamp(base_roughness * 0.25, 0.04, 0.10)
        metallic = 1.0
        clearcoat = 0.0
        clearcoat_roughness = 0.05
        ior = 2.50
        albedo = [0.95, 0.96, 0.98] if name.upper() == "MIRROR" else [round(_clamp(c, 0.78, 1.0), 4) for c in eff_diff_rgb]
    elif category == "ROCKPORT_MAT_GLASS":
        roughness = _clamp(base_roughness * 0.24, 0.04, 0.12)
        metallic = 0.0
        clearcoat = 0.90
        clearcoat_roughness = 0.04
        ior = 1.52
        alpha = round(_clamp(diff_alpha if diff_alpha < 0.99 else 0.42, 0.18, 0.85), 4)
        flags |= 0x2  # transparent
    elif category == "ROCKPORT_MAT_EMISSIVE_LAMP":
        roughness = 0.14
        metallic = 0.15
        clearcoat = 0.85
        clearcoat_roughness = 0.06
        ior = 1.50
        if name.upper() == "BRAKELIGHT":
            albedo = [0.85, 0.06, 0.04]
            emissive = [4.20, 0.35, 0.22]
        else:
            albedo = [0.92, 0.95, 1.00]
            emissive = [3.80, 4.10, 4.60]
        alpha = 1.0
        flags |= 0x4  # emissive
    elif category == "ROCKPORT_MAT_METALLIC_PAINT":
        roughness = _clamp(base_roughness * 1.45, 0.22, 0.34)
        metallic = 0.85
        clearcoat = 1.00
        clearcoat_roughness = 0.05
        ior = 1.50
        flags |= 0x1  # metallic micro-flake
    elif category == "ROCKPORT_MAT_PEARL_PAINT":
        roughness = _clamp(base_roughness * 1.35, 0.20, 0.34)
        metallic = 0.80 if name.upper().startswith("PEARL") else 0.72
        clearcoat = 1.00
        clearcoat_roughness = 0.05
        ior = 1.50
        flags |= 0x1
    elif category == "ROCKPORT_MAT_GLOSS_PAINT":
        roughness = _clamp(base_roughness * 1.55, 0.18, 0.28)
        metallic = 0.12
        clearcoat = 1.00
        clearcoat_roughness = 0.06
        ior = 1.50
    elif category == "ROCKPORT_MAT_CUSTOM_PAINT":
        roughness = _clamp(base_roughness * 1.30, 0.18, 0.36)
        metallic = 0.55 if eff_env_scale >= 0.5 else 0.18
        clearcoat = 0.95
        clearcoat_roughness = 0.07
        ior = 1.50
    elif category == "ROCKPORT_MAT_CARBON_FIBER":
        roughness = 0.26
        metallic = 0.10
        clearcoat = 0.80
        clearcoat_roughness = 0.10
        ior = 1.48
        albedo = [0.16, 0.17, 0.18]
    elif category == "ROCKPORT_MAT_BRUSHED_METAL":
        roughness = _clamp(base_roughness * 0.85, 0.16, 0.42)
        metallic = 0.92 if "RIMS" in name.upper() or name.upper() in {"ALUMINUM", "GOLDROTOR", "BRAKEDISC"} else 0.82
        clearcoat = 0.65 if "GLOSS" in name.upper() else 0.12
        clearcoat_roughness = 0.10 if clearcoat > 0.5 else 0.24
        ior = 2.20
    elif category == "ROCKPORT_MAT_ROAD":
        roughness = 0.62
        metallic = 0.0
        clearcoat = 0.0
        clearcoat_roughness = 0.50
        ior = 1.50
    elif category == "ROCKPORT_MAT_RUBBER":
        roughness = 0.82
        metallic = 0.0
        clearcoat = 0.0
        clearcoat_roughness = 0.65
        ior = 1.45
        albedo = [round(c * 0.18, 4) for c in eff_diff_rgb] if name.upper() == "RUBBER" else albedo
    elif category == "ROCKPORT_MAT_MATTE_PLASTIC":
        roughness = _clamp(base_roughness * 2.2, 0.50, 0.84)
        metallic = 0.05 if "SHINY" in name.upper() else 0.0
        clearcoat = 0.25 if "SHINY" in name.upper() else 0.0
        clearcoat_roughness = 0.22
        ior = 1.46
    elif category == "ROCKPORT_MAT_CHARACTER":
        roughness = 0.18 if "LATEX" in name.upper() else (0.36 if "LEATHER" in name.upper() else 0.68)
        metallic = 0.0
        clearcoat = 0.35 if "LATEX" in name.upper() else 0.0
        clearcoat_roughness = 0.18
        ior = 1.45
    else:
        roughness = _clamp(base_roughness * 1.4, 0.25, 0.72)
        metallic = _clamp(metallic_scale, 0.0, 1.0)
        clearcoat = 0.30 if eff_env_scale >= 0.2 else 0.0
        clearcoat_roughness = 0.18
        ior = 1.48

    if name.upper() == "PLAINNOTHING":
        albedo = [0.0, 0.0, 0.0]
        roughness = 0.96
        metallic = 0.0
        clearcoat = 0.0
        alpha = 0.0

    envmap_boost = round(_clamp(0.35 + eff_env_scale * 0.5 + eff_spec_scale * 0.2, 0.2, 2.5), 4)
    name_hash = bstringhash(name)

    return {
        "index": index,
        "name": name,
        "name_hash": f"0x{name_hash:08x}",
        "name_hash_u32": name_hash,
        "pbr_category": category,
        "pbr_category_id": PBR_CATEGORY_ENUM[category],
        "ps2_stock": {
            "DiffuseMaxScale": round(diff_scale, 4),
            "DiffuseMaxRGB": [round(x, 4) for x in diff_rgb],
            "DiffuseMaxA": round(diff_alpha, 4),
            "SpecularPower": round(spec_power, 4),
            "SpecularMaxScale": round(spec_scale, 4),
            "EnvmapPower": round(env_power, 4),
            "EnvmapMaxScale": round(env_scale, 4),
            "MetallicScale": round(metallic_scale, 4),
        },
        "daylight_overrides": daylight_override or {},
        "golden_hour_overrides": golden_override or {},
        "pbr": {
            "base_color": albedo,
            "alpha": alpha,
            "roughness": round(roughness, 4),
            "metallic": round(metallic, 4),
            "clearcoat": round(clearcoat, 4),
            "clearcoat_roughness": round(clearcoat_roughness, 4),
            "ior": round(ior, 4),
            "envmap_boost": envmap_boost,
            "emissive": [round(x, 4) for x in emissive],
            "flags": flags,
        },
    }


# ---------------------------------------------------------------------------
# Code & Manifest Emitters (Deterministic)
# ---------------------------------------------------------------------------


def _fmt_f(val: float) -> str:
    return f"{float(val):.6f}f"


def emit_config_hpp(
    vlt_grouped: Dict[str, List[VltDelta]],
    hooks: Sequence[HookSymbol],
    symbols_source: str,
) -> str:
    """Emit self-contained C++20 `Rockport2026Config.hpp`."""
    lines: List[str] = [
        "// GENERATED BY tools/decomp_bridge.py — DO NOT EDIT MANUALLY.",
        "// NFSMW: Rockport 2026 (SLUS-21351) VLT Tuning Deltas & Engine Hook Table.",
        "// License: CC0-1.0 / MIT (zero GPL-3.0 code).",
        "#pragma once",
        "",
        "#include <array>",
        "#include <cstdint>",
        "#include <string_view>",
        "",
        "namespace rockport2026::config {",
        "",
        "struct VltFloatDelta {",
        "    std::string_view class_name;",
        "    std::string_view node_name;",
        "    std::string_view field_name;",
        "    std::string_view member_name;",
        "    std::uint32_t class_hash;",
        "    std::uint32_t node_hash;",
        "    std::uint32_t field_hash;",
        "    std::uint32_t index;",
        "    std::uint32_t ps2_offset;",
        "    float orig_value;",
        "    float new_value;",
        "};",
        "",
        "struct VltIntDelta {",
        "    std::string_view class_name;",
        "    std::string_view node_name;",
        "    std::string_view field_name;",
        "    std::string_view member_name;",
        "    std::uint32_t class_hash;",
        "    std::uint32_t node_hash;",
        "    std::uint32_t field_hash;",
        "    std::uint32_t index;",
        "    std::uint32_t ps2_offset;",
        "    std::int32_t orig_value;",
        "    std::int32_t new_value;",
        "};",
        "",
        "struct VltVec4Delta {",
        "    std::string_view class_name;",
        "    std::string_view node_name;",
        "    std::string_view field_name;",
        "    std::uint32_t class_hash;",
        "    std::uint32_t node_hash;",
        "    std::uint32_t field_hash;",
        "    std::uint32_t ps2_offset;",
        "    std::array<float, 4> orig_value;",
        "    std::array<float, 4> new_value;",
        "};",
        "",
        "struct Slus21351Hook {",
        "    std::string_view friendly_name;",
        "    std::string_view mangled_symbol;",
        "    std::string_view source_unit;",
        "    std::uint32_t vaddr;",
        "    std::uint32_t elf_offset;",
        "};",
        "",
        "struct ElfWorldLightDefaults {",
        "    std::uint32_t world_light_direction_vaddr;",
        "    std::uint32_t primary_light_diffuse_intensity_vaddr;",
        "    float primary_light_diffuse_intensity;",
        "    std::uint32_t fill_light_diffuse_colour_vaddr;",
        "    std::array<float, 4> fill_light_diffuse_colour;",
        "    std::uint32_t fill_light_diffuse_intensity_vaddr;",
        "    float fill_light_diffuse_intensity;",
        "    std::uint32_t fog_mode_infos_vaddr;",
        "    std::uint32_t base_weather_fog_vaddr;",
        "    float base_weather_fog;",
        "    std::uint32_t base_weather_fog_start_vaddr;",
        "    float base_weather_fog_start;",
        "};",
        "",
        f'inline constexpr std::string_view kSymbolsSource = "{symbols_source}";',
        "inline constexpr std::uint32_t kSlus21351PtLoadVaddr = 0x00100000u;",
        "inline constexpr std::uint32_t kSlus21351PtLoadOffset = 0x00001000u;",
        "inline constexpr std::uint32_t kScreenEfxTimestepPatchVaddr = 0x003DE778u;",
        "inline constexpr std::uint32_t kScreenEfxTimestepOrigWord = 0x3C013D08u;",
        "inline constexpr std::uint32_t kScreenEfxTimestepPatchedWord = 0x3C013C88u;",
        "",
        "inline constexpr ElfWorldLightDefaults kElfWorldLightDefaults {",
        f"    0x{ELF_WORLD_LIGHT_DEFAULTS['world_light_direction_vaddr']:08X}u,",
        f"    0x{ELF_WORLD_LIGHT_DEFAULTS['primary_light_diffuse_intensity_vaddr']:08X}u,",
        f"    {_fmt_f(ELF_WORLD_LIGHT_DEFAULTS['primary_light_diffuse_intensity'])},",
        f"    0x{ELF_WORLD_LIGHT_DEFAULTS['fill_light_diffuse_colour_vaddr']:08X}u,",
        "    {"
        + ", ".join(_fmt_f(x) for x in ELF_WORLD_LIGHT_DEFAULTS["fill_light_diffuse_colour"])
        + "},",
        f"    0x{ELF_WORLD_LIGHT_DEFAULTS['fill_light_diffuse_intensity_vaddr']:08X}u,",
        f"    {_fmt_f(ELF_WORLD_LIGHT_DEFAULTS['fill_light_diffuse_intensity'])},",
        f"    0x{ELF_WORLD_LIGHT_DEFAULTS['fog_mode_infos_vaddr']:08X}u,",
        f"    0x{ELF_WORLD_LIGHT_DEFAULTS['base_weather_fog_vaddr']:08X}u,",
        f"    {_fmt_f(ELF_WORLD_LIGHT_DEFAULTS['base_weather_fog'])},",
        f"    0x{ELF_WORLD_LIGHT_DEFAULTS['base_weather_fog_start_vaddr']:08X}u,",
        f"    {_fmt_f(ELF_WORLD_LIGHT_DEFAULTS['base_weather_fog_start'])}",
        "};",
        "",
    ]

    def emit_float_array(var_name: str, items: Sequence[VltDelta]) -> None:
        lines.append(f"inline constexpr std::array<VltFloatDelta, {len(items)}> {var_name} {{{{")
        for d in items:
            lines.append(
                f'    {{"{d.class_name}", "{d.node_name}", "{d.field_name}", "{d.member_name}", '
                f"0x{d.class_hash:08X}u, 0x{d.node_hash:08X}u, 0x{d.field_hash:08X}u, "
                f"{d.index}u, 0x{d.expected_offset:08X}u, {_fmt_f(d.orig_value)}, {_fmt_f(d.new_value)}}},"
            )
        lines.append("}};")
        lines.append("")

    def emit_int_array(var_name: str, items: Sequence[VltDelta]) -> None:
        lines.append(f"inline constexpr std::array<VltIntDelta, {len(items)}> {var_name} {{{{")
        for d in items:
            lines.append(
                f'    {{"{d.class_name}", "{d.node_name}", "{d.field_name}", "{d.member_name}", '
                f"0x{d.class_hash:08X}u, 0x{d.node_hash:08X}u, 0x{d.field_hash:08X}u, "
                f"{d.index}u, 0x{d.expected_offset:08X}u, {int(d.orig_value)}, {int(d.new_value)}}},"
            )
        lines.append("}};")
        lines.append("")

    def emit_vec4_array(var_name: str, items: Sequence[VltDelta]) -> None:
        lines.append(f"inline constexpr std::array<VltVec4Delta, {len(items)}> {var_name} {{{{")
        for d in items:
            orig_s = ", ".join(_fmt_f(v) for v in d.orig_value)
            new_s = ", ".join(_fmt_f(v) for v in d.new_value)
            lines.append(
                f'    {{"{d.class_name}", "{d.node_name}", "{d.field_name}", '
                f"0x{d.class_hash:08X}u, 0x{d.node_hash:08X}u, 0x{d.field_hash:08X}u, "
                f"0x{d.expected_offset:08X}u, {{{orig_s}}}, {{{new_s}}}}},"
            )
        lines.append("}};")
        lines.append("")

    cam_floats = [d for d in vlt_grouped.get("modern_camera", []) if d.value_kind == "float"]
    hand_floats = [d for d in vlt_grouped.get("modern_handling_tuning", []) if d.value_kind == "float"]
    roster_floats = [d for d in vlt_grouped.get("full_roster_handling", []) if d.value_kind == "float"]
    pursuit_floats = [d for d in vlt_grouped.get("pursuit_and_traffic", []) if d.value_kind == "float"]
    pursuit_ints = [d for d in vlt_grouped.get("pursuit_and_traffic", []) if d.value_kind == "int"]
    vl_floats = [d for d in vlt_grouped.get("modern_visual_look", []) if d.value_kind == "float"]
    vl_vec4s = [d for d in vlt_grouped.get("modern_visual_look", []) if d.value_kind == "vec4"]

    emit_float_array("kCameraDeltas", cam_floats)
    emit_float_array("kHandlingDeltas", hand_floats)
    emit_float_array("kRosterHandlingDeltas", roster_floats)
    emit_float_array("kPursuitTrafficFloatDeltas", pursuit_floats)
    emit_int_array("kPursuitTrafficIntDeltas", pursuit_ints)
    emit_float_array("kVisualLookFloatDeltas", vl_floats)
    emit_vec4_array("kVisualLookVec4Deltas", vl_vec4s)

    total_vlt = (
        len(cam_floats)
        + len(hand_floats)
        + len(roster_floats)
        + len(pursuit_floats)
        + len(pursuit_ints)
        + len(vl_floats)
        + len(vl_vec4s)
    )
    lines.append(f"inline constexpr std::uint32_t kNumTotalVltDeltas = {total_vlt}u;")
    lines.append("")

    lines.append(f"inline constexpr std::array<Slus21351Hook, {len(hooks)}> kSlus21351Hooks {{{{")
    for h in hooks:
        lines.append(
            f'    {{"{h.friendly_name}", "{h.mangled_symbol}", "{h.source_unit}", '
            f"0x{h.vaddr:08X}u, 0x{h.elf_offset:08X}u}},"
        )
    lines.append("}};")
    lines.append("")
    lines.extend(
        [
            "[[nodiscard]] constexpr std::uint32_t find_hook_vaddr(std::string_view friendly_name) noexcept {",
            "    for (const auto& hook : kSlus21351Hooks) {",
            "        if (hook.friendly_name == friendly_name) {",
            "            return hook.vaddr;",
            "        }",
            "    }",
            "    return 0u;",
            "}",
            "",
            "} // namespace rockport2026::config",
            "",
        ]
    )
    return "\n".join(lines)


def emit_lighting_h(
    presets: Dict[str, Dict[str, Any]],
    pbr_materials: Sequence[Dict[str, Any]],
) -> str:
    """Emit shared C / C++20 / Metal 3 header `Rockport2026Lighting.h`."""
    lines: List[str] = [
        "/* GENERATED BY tools/decomp_bridge.py — DO NOT EDIT MANUALLY.",
        " * NFSMW: Rockport 2026 Shared C / C++20 / Metal 3 Lighting & PBR Material ABI.",
        " */",
        "#ifndef ROCKPORT2026_LIGHTING_H",
        "#define ROCKPORT2026_LIGHTING_H",
        "",
        "#if defined(__METAL_VERSION__)",
        "  #include <metal_stdlib>",
        "  #define ROCKPORT_FLOAT4 metal::float4",
        "  #define ROCKPORT_U32 uint32_t",
        "  #define ROCKPORT_CONST constant",
        "#else",
        "  #if defined(__cplusplus)",
        "    #include <cstdint>",
        "    #define ROCKPORT_U32 std::uint32_t",
        "    #define ROCKPORT_ALIGN16 alignas(16)",
        "    #define ROCKPORT_CONST inline constexpr",
        "  #else",
        "    #include <stdint.h>",
        "    #define ROCKPORT_U32 uint32_t",
        "    #define ROCKPORT_ALIGN16 _Alignas(16)",
        "    #if defined(__GNUC__) || defined(__clang__)",
        "      #define ROCKPORT_CONST __attribute__((unused)) static const",
        "    #else",
        "      #define ROCKPORT_CONST static const",
        "    #endif",
        "  #endif",
        "  typedef struct {",
        "      ROCKPORT_ALIGN16 float x;",
        "      float y;",
        "      float z;",
        "      float w;",
        "  } rockport_float4_t;",
        "  #define ROCKPORT_FLOAT4 rockport_float4_t",
        "#endif",
        "",
        "typedef enum RockportMaterialCategory {",
    ]
    for cat_name, cat_id in PBR_CATEGORIES:
        comma = "," if cat_id < len(PBR_CATEGORIES) - 1 else ""
        lines.append(f"    {cat_name} = {cat_id}{comma}")
    lines.extend(
        [
            "} RockportMaterialCategory;",
            "",
            "typedef struct RockportSunUniforms {",
            "    ROCKPORT_FLOAT4 sun_direction_intensity;",
            "    ROCKPORT_FLOAT4 sun_color_size;",
            "    ROCKPORT_FLOAT4 corona_color_intensity;",
            "    ROCKPORT_FLOAT4 ray_color_intensity;",
            "    ROCKPORT_FLOAT4 fill_color_intensity;",
            "} RockportSunUniforms;",
            "",
            "typedef struct RockportVisualLookUniforms {",
            "    ROCKPORT_FLOAT4 bloom_tint_intensity;",
            "    ROCKPORT_FLOAT4 grading_params;",
            "} RockportVisualLookUniforms;",
            "",
            "typedef struct RockportPBRMaterial {",
            "    ROCKPORT_U32 name_hash;",
            "    ROCKPORT_U32 pbr_category;",
            "    float roughness;",
            "    float metallic;",
            "    ROCKPORT_FLOAT4 base_color_alpha;",
            "    ROCKPORT_FLOAT4 clearcoat_ior_spec;",
            "    ROCKPORT_FLOAT4 emissive_flags;",
            "} RockportPBRMaterial;",
            "",
            "#if defined(__cplusplus) && !defined(__METAL_VERSION__)",
            "static_assert(sizeof(RockportSunUniforms) == 80, \"RockportSunUniforms must be 80 bytes\");",
            "static_assert(sizeof(RockportVisualLookUniforms) == 32, \"RockportVisualLookUniforms must be 32 bytes\");",
            "static_assert(sizeof(RockportPBRMaterial) == 64, \"RockportPBRMaterial must be 64 bytes\");",
            "#endif",
            "",
        ]
    )

    def _f4(vals: Sequence[float]) -> str:
        return "{" + ", ".join(_fmt_f(v) for v in vals) + "}"

    preset_map = (
        ("kRockportSunDaylight", "kRockportVisualLookDaylight", presets["daylight"]),
        ("kRockportSunGoldenHour", "kRockportVisualLookGoldenHour", presets["golden_hour"]),
        ("kRockportSunNightRain", "kRockportVisualLookNightRain", presets["night_rain"]),
    )
    for sun_var, vl_var, pdata in preset_map:
        su = pdata["sun_uniforms"]
        vu = pdata["visuallook_uniforms"]
        lines.extend(
            [
                f"ROCKPORT_CONST RockportSunUniforms {sun_var} = {{",
                f"    {_f4(su['sun_direction_intensity'])},",
                f"    {_f4(su['sun_color_size'])},",
                f"    {_f4(su['corona_color_intensity'])},",
                f"    {_f4(su['ray_color_intensity'])},",
                f"    {_f4(su['fill_color_intensity'])}",
                "};",
                "",
                f"ROCKPORT_CONST RockportVisualLookUniforms {vl_var} = {{",
                f"    {_f4(vu['bloom_tint_intensity'])},",
                f"    {_f4(vu['grading_params'])}",
                "};",
                "",
            ]
        )

    lines.append(f"#define ROCKPORT_PBR_MATERIAL_COUNT {len(pbr_materials)}u")
    lines.append(f"ROCKPORT_CONST RockportPBRMaterial kRockportPBRMaterials[{len(pbr_materials)}] = {{")
    for idx, mat in enumerate(pbr_materials):
        p = mat["pbr"]
        bc = p["base_color"] + [p["alpha"]]
        cc = [p["clearcoat"], p["clearcoat_roughness"], p["ior"], p["envmap_boost"]]
        em = p["emissive"] + [float(p["flags"])]
        comma = "," if idx < len(pbr_materials) - 1 else ""
        lines.append(
            f"    {{0x{mat['name_hash_u32']:08X}u, {mat['pbr_category_id']}u, "
            f"{_fmt_f(p['roughness'])}, {_fmt_f(p['metallic'])}, "
            f"{_f4(bc)}, {_f4(cc)}, {_f4(em)}}}{comma} /* {mat['name']} */"
        )
    lines.extend(
        [
            "};",
            "",
            "#endif /* ROCKPORT2026_LIGHTING_H */",
            "",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main Generator Driver
# ---------------------------------------------------------------------------


def generate_bridge(
    patches_dir: Path,
    out_dir: Path,
    symbols_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Generate `Rockport2026Config.hpp`, `Rockport2026Lighting.h`, and `bridge_manifest.json`."""
    vlt_dir = patches_dir / "vlt"
    lighting_dir = patches_dir / "lighting"

    vlt_grouped = load_vlt_deltas(vlt_dir)
    lighting_recipes = load_lighting_recipes(lighting_dir)
    hooks, symbols_source = resolve_hooks(symbols_path)

    visuallook_deltas = vlt_grouped.get("modern_visual_look", [])
    presets = build_lighting_presets(lighting_recipes, visuallook_deltas)

    day_mats = presets["daylight"].get("material_overrides", {})
    gold_mats = presets["golden_hour"].get("material_overrides", {})

    pbr_materials: List[Dict[str, Any]] = []
    for idx, row in enumerate(STOCK_PS2_LIGHT_MATERIALS):
        name, d_scale, d_rgb, d_alpha, s_pow, s_scale, e_pow, e_scale, m_scale = row
        pbr_materials.append(
            translate_light_material_to_pbr(
                index=idx,
                name=name,
                diff_scale=d_scale,
                diff_rgb=d_rgb,
                diff_alpha=d_alpha,
                spec_power=s_pow,
                spec_scale=s_scale,
                env_power=e_pow,
                env_scale=e_scale,
                metallic_scale=m_scale,
                daylight_override=day_mats.get(name),
                golden_override=gold_mats.get(name),
            )
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    config_hpp_path = out_dir / "Rockport2026Config.hpp"
    lighting_h_path = out_dir / "Rockport2026Lighting.h"
    manifest_path = out_dir / "bridge_manifest.json"

    config_hpp_path.write_text(emit_config_hpp(vlt_grouped, hooks, symbols_source), encoding="utf-8")
    lighting_h_path.write_text(emit_lighting_h(presets, pbr_materials), encoding="utf-8")

    manifest: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "project": "NFSMW: Rockport 2026",
        "target": {
            "game": "Need for Speed: Most Wanted - Black Edition",
            "serial": "SLUS-21351",
            "elf_name": "SLUS_213.51",
            "elf_sha1": "60289f3f8b643822a302720f9649ddc4ba55034b",
            "pcsx2_crc": "0518D274",
        },
        "symbols_source": symbols_source,
        "vlt_summary": {
            recipe_id: {
                "count": len(deltas),
                "classes": sorted({d.class_name for d in deltas}),
            }
            for recipe_id, deltas in sorted(vlt_grouped.items())
        },
        "total_vlt_deltas": sum(len(v) for v in vlt_grouped.values()),
        "elf_world_light_defaults": ELF_WORLD_LIGHT_DEFAULTS,
        "slus21351_hooks": [
            {
                "friendly_name": h.friendly_name,
                "mangled_symbol": h.mangled_symbol,
                "source_unit": h.source_unit,
                "role": h.role,
                "vaddr": f"0x{h.vaddr:08x}",
                "elf_offset": f"0x{h.elf_offset:08x}",
                "resolved_from_file": h.resolved_from_file,
            }
            for h in hooks
        ],
        "lighting_presets": presets,
        "pbr_categories": [{"name": n, "id": i} for n, i in PBR_CATEGORIES],
        "light_materials_count": len(pbr_materials),
        "light_materials": pbr_materials,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    return {
        "out_dir": str(out_dir),
        "files": [config_hpp_path.name, lighting_h_path.name, manifest_path.name],
        "symbols_source": symbols_source,
        "hooks_count": len(hooks),
        "hooks_resolved_from_file": sum(1 for h in hooks if h.resolved_from_file),
        "total_vlt_deltas": manifest["total_vlt_deltas"],
        "light_materials_count": len(pbr_materials),
        "presets": sorted(presets.keys()),
    }


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def main(argv: Optional[Sequence[str]] = None) -> int:
    root = _repo_root()
    parser = argparse.ArgumentParser(
        description="Generate Rockport 2026 Path 2 C++20/Metal bridge headers & PBR manifest."
    )
    parser.add_argument(
        "--patches-dir",
        type=Path,
        default=root / "patches",
        help="Path to patches directory containing vlt/ and lighting/ (default: patches/)",
    )
    parser.add_argument(
        "--symbols",
        type=Path,
        default=root / "build" / "symbols_slus21351.txt",
        help="Optional SLUS-21351 symbols.txt path (falls back cleanly when absent)",
    )
    parser.add_argument(
        "--no-symbols",
        action="store_true",
        help="Force built-in verified SLUS-21351 fallback symbol table (offline CI mode)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=root / "build" / "decomp_bridge_wave3",
        help="Output directory inside build/ (default: build/decomp_bridge_wave3/)",
    )
    parser.add_argument("--json", action="store_true", help="Print generation summary as JSON")
    args = parser.parse_args(argv)

    sym_path: Optional[Path] = None if args.no_symbols else args.symbols
    summary = generate_bridge(
        patches_dir=args.patches_dir,
        out_dir=args.out_dir,
        symbols_path=sym_path,
    )

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(
            f"[decomp_bridge] Exported {', '.join(summary['files'])} -> {summary['out_dir']} "
            f"(hooks={summary['hooks_count']}, file_resolved={summary['hooks_resolved_from_file']}, "
            f"vlt_deltas={summary['total_vlt_deltas']}, materials={summary['light_materials_count']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
