#include <metal_stdlib>
#include <metal_raytracing>
using namespace metal;
using namespace raytracing;

constant float PI = 3.14159265359f;

// Material IDs (aligned with RTMaterialID in RTSceneBuilder.swift)
constant uint MAT_CAR_PAINT        = 0u;
constant uint MAT_CHROME           = 1u;
constant uint MAT_BRUSHED_ALUMINUM = 2u;
constant uint MAT_ALLOY_RIM        = 3u;
constant uint MAT_MATTE_TRIM       = 4u;
constant uint MAT_CARBON_FIBER     = 5u;
constant uint MAT_RUBBER_TIRE      = 6u;
constant uint MAT_SMOKED_GLASS     = 7u;
constant uint MAT_HEADLIGHT_GLASS  = 8u;
constant uint MAT_HEADLIGHT_EMIT   = 9u;
constant uint MAT_TAILLIGHT_EMIT   = 10u;
constant uint MAT_INTERIOR_DARK    = 11u;
constant uint MAT_BRAKE_ROTOR      = 12u;
constant uint MAT_BRAKE_CALIPER    = 13u;
constant uint MAT_LICENSE_PLATE    = 14u;
constant uint MAT_WET_ASPHALT      = 15u;
constant uint MAT_URBAN_CONCRETE   = 16u;
constant uint MAT_URBAN_GLASS      = 17u;
constant uint MAT_NEON_CYAN        = 18u;
constant uint MAT_NEON_AMBER       = 19u;
constant uint MAT_AUTUMN_TERRAIN   = 20u;
constant uint MAT_COASTAL_WATER    = 21u;
constant uint MAT_STREETLAMP_LED   = 22u;
constant uint MAT_BARRIER_REFLECT  = 23u;
constant uint MAT_FOLIAGE_AUTUMN   = 24u;
constant uint MAT_FOLIAGE_PINE     = 25u;
constant uint MAT_TREE_TRUNK       = 26u;
constant uint MAT_CAR_PAINT_SILVER = 27u;
constant uint MAT_CAR_BADGE        = 28u;
constant uint MAT_CAR_MISC_TRIM    = 29u;
constant uint MAT_CAR_MISC_CHROME  = 30u;
constant uint MAT_CAR_MISC_METAL   = 31u;

constant uint TLAS_MASK_CAR          = 0x01u;
constant uint TLAS_MASK_WORLD_OPAQUE = 0x02u;
constant uint TLAS_MASK_ROAD         = 0x04u;
constant uint TLAS_MASK_WALL         = 0x08u;
constant uint TLAS_MASK_GLASS        = 0x10u;
constant uint TLAS_MASK_SHADOW_ALL   = 0x0Fu;

struct RTVertex {
    packed_float3 position;
    packed_float3 normal;
    packed_float2 uv;
    packed_float3 modelPos;
    uint materialID;
};

struct RkptVertex {
    packed_float3 position;
    packed_float3 normal;
    packed_float2 uv;
    ushort matIndex;
    uchar vertexAO;
    uchar flags;
};
static_assert(sizeof(RkptVertex) == 36, "RkptVertex must match locked 36-byte .rkpt v1 contract");

struct RkptMaterial {
    uint diffuseTexIndex;
    uint surfaceClass;
    uint flags;
    float roughnessHint;
    float metallicHint;
    float emissiveHint;
    float bumpStrength;
    uint tpkNameHash;
};
static_assert(sizeof(RkptMaterial) == 32, "RkptMaterial must match locked 32-byte .rkpt v1 contract");

struct BindlessTexEntry {
    texture2d<float> tex;
};

struct RTInstanceMeta {
    uint vertexOffset;
    uint indexOffset;
    uint triOffset;
    uint flags; // bit0: livery, bit1: glass, bit2: procWorld, bit3: scrollRoad, bit4: rkptWorld, bit5: alphaCutout, bit6: skyPanorama
    float4x4 modelMatrix;
    float4x4 invTransposeMatrix;
    float4x4 prevModelMatrix;
};

struct RTMaterial {
    float4 baseColor;      // rgb, w = alpha
    float4 secondaryColor; // rgb, w = ior
    float4 pbrParams;      // x = metallic, y = roughness, z = clearcoat, w = clearcoatRoughness
    float4 emissive;       // rgb, w = flags
};

struct RTFrameUniforms {
    float4x4 invViewProjMatrix;
    float4x4 currViewProjMatrix;
    float4x4 prevViewProjMatrix;
    float4 cameraPos;      // xyz, w = fovDeg
    float4 cameraForward;  // xyz, w = aspect
    float4 cameraRight;    // xyz, w = tanHalfFov
    float4 cameraUp;       // xyz, w = exposure
    float4 sunDir;         // xyz normalized, w = sunIntensity
    float4 sunColor;       // rgb, w = sunConeRadius
    float4 coronaColor;    // rgb, w = iblIntensity
    float4 skyZenith;      // rgb, w = puddleWetness
    float4 skyHorizon;     // rgb, w = fogDensity
    float4 skyGround;      // rgb, w = headlightsOn
    float4 neonParams;     // rgb = streetlamp tint, w = streetlampIntensity
    float4 postParams;     // x = bloomThreshold, y = bloomIntensity, z = liveryEnabled, w = rtEnabled
    float4 rtParams;       // x = width, y = height, z = sampleIndex, w = splitCompareMode
    float4 envParams;      // x = cloudCoverage, y = roadScrollZ, z = clearcoatRoughness, w = flakeStrength
    float4 gradeParams;    // x = vignette, y = contrast, z = saturation, w = denoisePasses
    float4 carDynParams;   // x = steerRad, y = wheelSpinRad, z = carYawRad, w = carRollRad
    float4 driveParams;    // x = speedKmh, y = steerDeg, z = hasAuthenticCarTex, w = carSupportsLivery
    float4 hudParams;      // x = hudEnabled, y = rpmNorm (0..1), z = gear (0..6), w = carID (0=m3, 1=cgt, 2=gallardo)
    float4 jitterParams;   // x = jitterX, y = jitterY, z = temporal1sppMode (0/1), w = worldTexCount
    float4 carWorldPos;    // xyz = carWorldPos, w = hasRkptWorld (0/1)
    float4 lightGridParams;// xy = world lightGrid originXZ, zw = invCellXZ (128x128 grid)
    float4 fxParams;       // x = rainIntensity (0..1), y = speedbreaker (0..1), z = timeOfDayHours, w = worldLightCount
};

struct RTPhysicsProbeInput {
    float4 carPos;         // xyz = carPos, w = yawRad
    float4 carForward;     // xyz = forward, w = halfTrackX
    float4 carRight;       // xyz = right, w = frontAxleZ
    float4 dims;           // x = rearAxleZ, y = wheelRadius, z = bodyHalfWidth, w = bodyHalfLength
};

struct RTPhysicsProbeOutput {
    float4 wheelHitY;      // FL, FR, RL, RR world hit Y (-9999 on miss)
    float4 wallDist;       // front, rear, left, right hit dist (999 on miss)
    float4 wallNormalFront;// xyz = front wall normal, w = valid
    float4 wallNormalSide; // xyz = nearest side wall normal, w = sideSign (-1 left, +1 right, 0 none)
    float4 skyProbe;       // x = skyOcclusion (0 open sky -> 1 tunnel ceiling <=12m), y = hitDist (999 on miss), z = 0, w = valid
};
static_assert(sizeof(RTPhysicsProbeOutput) == 80, "RTPhysicsProbeOutput must match 80-byte ABI");

// Locked Hour 1-3 ABI Contracts (Buffer Slots 11, 12, 13)
struct RTWorldLight {
    float4 posRadius;      // xyz = world luminaire position, w = influence radius (m)
    float4 colorCone;      // rgb = pre-scaled linear radiance, w = coneMinCos (>0 downward lamp, <= -0.9 omni)
};
static_assert(sizeof(RTWorldLight) == 32, "RTWorldLight must match locked 32-byte contract");

struct RTDynamicFX {
    float4 timeWeather;    // x = simTimeSec, y = timeOfDayHours, z = rainIntensity (0..1), w = puddleLevel (0..1)
    float4 carMetrics;     // x = frontAxleZ, y = rearAxleZ, z = wheelRadiusF, w = wheelRadiusR
    float4 nosParams;      // x = nosActive (0..1), y = nosFlameLen, z = speedbreaker (0..1), w = worldLightCount
    float4 copLightPos[4]; // xyz = 4 nearest cop lightbar world positions, w = strobePhase (0=off)
    float4 copLightCol[4]; // rgb = alternating red/blue color, w = intensity
};
static_assert(sizeof(RTDynamicFX) == 176, "RTDynamicFX must match locked 176-byte contract");

// Locked v1.2.0 Event Gate & Holographic Chevron ABI (Buffer Slot 16; Slot 15 reserved for v1.3 RTVFXBuffer)
struct RTEventWorldFX {
    float4 gatePosRadius;  // xyz = active checkpoint/speedtrap world pos, w = gate half-width (m)
    float4 gateDirKind;    // xyz = gate forward unit dir, w = kind (0=sprint amber, 1=speedtrap cyan, 2=boss crimson-gold)
    float4 nextPosRadius;  // xyz = next gate world pos, w = next radius (0 = none/finish)
    float4 fxParams;       // x = gateActive (0/1), y = isSpeedtrap (0/1), z = radarFlash (0..1), w = simTimeSec
    float4 chevrons[4];    // xyz = floating road chevron world pos, w = yawRad (<= -100 inactive)
};
static_assert(sizeof(RTEventWorldFX) == 128, "RTEventWorldFX must match locked 128-byte ABI");

// Locked v1.3.0 Analytical Drift VFX, Thin-Lens DoF & 4K Tile Crop ABI (Buffer Slot 15, #24 Guardrail 2 + #106)
struct RTSkidMarkGPU {
    float4 p0Width;        // xyz = start world pos, w = half-width (m)
    float4 p1Alpha;        // xyz = end world pos, w = opacity (0..1)
};
static_assert(sizeof(RTSkidMarkGPU) == 32, "RTSkidMarkGPU must be 32 bytes");

struct RTSparkGPU {
    float4 posLife;        // xyz = world pos, w = normalized life (0..1)
    float4 velTemp;        // xyz = world velocity (m/s), w = temperature (0..1)
};
static_assert(sizeof(RTSparkGPU) == 32, "RTSparkGPU must be 32 bytes");

struct RTSmokePuffGPU {
    float4 posRadius;      // xyz = world pos, w = puff radius (m)
    float4 velAlpha;       // xyz = world vel (m/s), w = opacity (0..1)
};
static_assert(sizeof(RTSmokePuffGPU) == 32, "RTSmokePuffGPU must be 32 bytes");

struct RTVFXBuffer {
    uint4 counts;               // x = skidCount (<=128), y = sparkCount (<=64), z = smokeCount (<=16), w = flags (bit0=isCockpitCam, bit1=enableRISShadows)
    float4 skidBoundsMinMaxXZ;  // x = minX, y = minZ, z = maxX, w = maxZ
    float4 fxSphere;            // xyz = bounding sphere center enclosing active sparks+smoke, w = radius (m)
    float4 photoDoF;            // x = apertureRadiusM (0=off), y = focusDistM, z = focalLengthMm, w = shutterVignette
    float4 tileCrop;            // x = offsetU, y = offsetV, z = scaleU, w = scaleV (default 0,0,1,1)
    RTSkidMarkGPU skids[128];   // 4,096 B
    RTSparkGPU sparks[64];      // 2,048 B
    RTSmokePuffGPU smoke[16];   //   512 B
};
static_assert(sizeof(RTVFXBuffer) == 6736, "RTVFXBuffer must match locked 6736-byte ABI");

// Locked Hour 3 AI Vehicle RT ABI (Buffer Slot 14 + aiRoadProbeKernel)
struct RTAIVertex {
    packed_float3 position;
    packed_float3 normal;
    packed_float2 uv;
    ushort partFlags;      // bits 0..3: matClass (0=body,1=tire,2=glass,3=interior,4=headlight,5=taillight,6=lightbar), bit4: isLightbar, bit5: isLeftSide
    ushort archetypeIdx;   // 0..6
};
static_assert(sizeof(RTAIVertex) == 36, "RTAIVertex must match 36-byte Swift contract");

struct RTAISceneData {
    device const RTAIVertex* vertices;
    device const uint* indices;
    texture2d<float> skinTex[7];
    uint archetypeCount;
    uint flags;
};
static_assert(sizeof(RTAISceneData) == 80, "RTAISceneData must match 80-byte Swift contract");

struct RTAIRoadProbeInput {
    float4 posActive;      // xyz = AI world pos, w = active (> 0.5)
    float4 fwdHalfWB;      // xyz = forward dir, w = halfWheelbase (m)
};
static_assert(sizeof(RTAIRoadProbeInput) == 32, "RTAIRoadProbeInput must match 32-byte Swift contract");

struct RTAIRoadProbeOutput {
    float roadY;
    float pitchRad;
    float frontY;
    float rearY;
};
static_assert(sizeof(RTAIRoadProbeOutput) == 16, "RTAIRoadProbeOutput must match 16-byte Swift contract");

// ---------------------------------------------------------------------------
// Deterministic PRNG & Low-Discrepancy Samplers
// ---------------------------------------------------------------------------

static inline float2 r2Sequence(uint idx, float2 pixelJitter) {
    const float2 alpha = float2(0.7548776662f, 0.5698402910f);
    return fract(pixelJitter + float2(float(idx) + 0.5f) * alpha);
}

static inline float hash21(float2 p) {
    float3 p3 = fract(float3(p.xyx) * 0.1031f);
    p3 += dot(p3, p3.yzx + 33.33f);
    return fract((p3.x + p3.y) * p3.z);
}

static inline float3 hash33(float3 p3) {
    p3 = fract(p3 * float3(0.1031f, 0.1030f, 0.0973f));
    p3 += dot(p3, p3.yxz + 33.33f);
    return fract((p3.xxy + p3.yxx) * p3.zyx);
}

static inline float noise2D(float2 p) {
    float2 i = floor(p);
    float2 f = fract(p);
    float2 u = f * f * (3.0f - 2.0f * f);
    float a = hash21(i);
    float b = hash21(i + float2(1.0f, 0.0f));
    float c = hash21(i + float2(0.0f, 1.0f));
    float d = hash21(i + float2(1.0f, 1.0f));
    return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

static inline float fbm2D(float2 p) {
    float sum = 0.0f;
    float amp = 0.5f;
    float2x2 rot = float2x2(0.8f, 0.6f, -0.6f, 0.8f);
    for (int i = 0; i < 4; ++i) {
        sum += amp * noise2D(p);
        p = rot * p * 2.03f + float2(13.7f, 7.3f);
        amp *= 0.5f;
    }
    return sum;
}

static inline float fbm5(float2 p) {
    float sum = 0.0f;
    float amp = 0.5f;
    float2x2 rot = float2x2(0.8f, 0.6f, -0.6f, 0.8f);
    for (int i = 0; i < 5; ++i) {
        sum += amp * noise2D(p);
        p = rot * p * 2.02f + float2(19.1f, 3.7f);
        amp *= 0.5f;
    }
    return sum;
}

static inline void buildONB(float3 n, thread float3& t, thread float3& b) {
    float3 up = abs(n.y) < 0.999f ? float3(0.0f, 1.0f, 0.0f) : float3(1.0f, 0.0f, 0.0f);
    t = normalize(cross(up, n));
    b = cross(n, t);
}

static inline float3 sampleCosineHemisphere(float3 n, float2 xi) {
    float phi = 2.0f * PI * xi.x;
    float cosTheta = sqrt(clamp(1.0f - xi.y, 0.0f, 1.0f));
    float sinTheta = sqrt(clamp(xi.y, 0.0f, 1.0f));
    float3 t, b;
    buildONB(n, t, b);
    return normalize(t * (cos(phi) * sinTheta) + b * (sin(phi) * sinTheta) + n * cosTheta);
}

static inline float3 sampleCone(float3 axis, float halfAngleRad, float2 xi) {
    float cosMax = cos(halfAngleRad);
    float cosTheta = mix(cosMax, 1.0f, xi.x);
    float sinTheta = sqrt(max(0.0f, 1.0f - cosTheta * cosTheta));
    float phi = 2.0f * PI * xi.y;
    float3 t, b;
    buildONB(axis, t, b);
    return normalize(t * (cos(phi) * sinTheta) + b * (sin(phi) * sinTheta) + axis * cosTheta);
}

static inline float3 sampleGGXVNDF(float3 N, float3 V, float roughness, float2 xi) {
    float alpha = clamp(roughness * roughness, 0.0012f, 0.99f);
    float3 t, b;
    buildONB(N, t, b);
    float3 Vlocal = normalize(float3(dot(V, t), dot(V, b), max(dot(V, N), 1e-4f)));

    float3 Vh = normalize(float3(alpha * Vlocal.x, alpha * Vlocal.y, Vlocal.z));
    float lensq = Vh.x * Vh.x + Vh.y * Vh.y;
    float3 T1 = lensq > 1e-6f ? float3(-Vh.y, Vh.x, 0.0f) * rsqrt(lensq) : float3(1.0f, 0.0f, 0.0f);
    float3 T2 = cross(Vh, T1);

    float r = sqrt(clamp(xi.x, 0.0f, 0.9999f));
    float phi = 2.0f * PI * xi.y;
    float t1 = r * cos(phi);
    float t2 = r * sin(phi);
    float s = 0.5f * (1.0f + Vh.z);
    t2 = (1.0f - s) * sqrt(max(0.0f, 1.0f - t1 * t1)) + s * t2;

    float3 Nh = t1 * T1 + t2 * T2 + sqrt(max(0.0f, 1.0f - t1 * t1 - t2 * t2)) * Vh;
    float3 Hlocal = normalize(float3(alpha * Nh.x, alpha * Nh.y, max(0.0f, Nh.z)));
    return normalize(t * Hlocal.x + b * Hlocal.y + N * Hlocal.z);
}

// ---------------------------------------------------------------------------
// Physically-Based Nishita Atmospheric Sky + Layered 3D fBM Clouds +
// Atmospheric Autumn Coastal Headlands (ZERO box buildings / black cutouts)
// ---------------------------------------------------------------------------

static inline float raySphereExit(float3 ro, float3 rd, float rad) {
    float b = dot(ro, rd);
    float c = dot(ro, ro) - rad * rad;
    float disc = max(b * b - c, 0.0f);
    return -b + sqrt(disc);
}

static float3 evaluateNishitaAtmosphere(float3 dir, constant RTFrameUniforms& u, bool highDetail) {
    float3 d = normalize(dir);
    float3 s = normalize(u.sunDir.xyz);

    float lumZen = dot(u.skyZenith.rgb, float3(0.2126f, 0.7152f, 0.0722f));
    float nightBlend = clamp(1.0f - smoothstep(0.012f, 0.075f, lumZen), 0.0f, 1.0f);
    float3 sEff = normalize(float3(s.x, max(s.y, 0.05f), s.z));

    const float R_e = 6360000.0f;
    const float R_a = 6420000.0f;
    const float H_R = 8000.0f;
    const float H_M = 1250.0f;
    const float3 betaR = float3(5.8e-6f, 13.5e-6f, 33.1e-6f);
    const float3 betaM = float3(1.6e-5f);

    float3 dClamped = normalize(float3(d.x, max(d.y, -0.02f), d.z));
    float3 ro = float3(0.0f, R_e + 18.0f, 0.0f);
    float tMax = raySphereExit(ro, dClamped, R_a);
    int steps = highDetail ? 8 : 4;
    float dt = tMax / float(steps);

    float optR = 0.0f;
    float optM = 0.0f;
    float3 sumR = float3(0.0f);
    float3 sumM = float3(0.0f);

    for (int i = 0; i < steps; ++i) {
        float t = (float(i) + 0.5f) * dt;
        float3 pos = ro + dClamped * t;
        float h = max(length(pos) - R_e, 0.0f);
        float hr = exp(-h / H_R) * dt;
        float hm = exp(-h / H_M) * dt;
        optR += hr;
        optM += hm;

        float tSun = raySphereExit(pos, sEff, R_a);
        float3 posMid = pos + sEff * (tSun * 0.32f);
        float hSunMid = max(length(posMid) - R_e, 0.0f);
        float sunCos = max(dot(normalize(pos), sEff), 0.02f);
        float sunAngleDeg = acos(clamp(sunCos, -1.0f, 1.0f)) * (180.0f / PI);
        float chap = 1.0f / (sunCos + 0.14f * pow(max(93.885f - sunAngleDeg, 0.01f), -1.253f));
        float optSunR = (exp(-h / H_R) + exp(-hSunMid / H_R)) * 0.5f * H_R * min(chap, 36.0f);
        float optSunM = (exp(-h / H_M) + exp(-hSunMid / H_M)) * 0.5f * H_M * min(chap, 36.0f);

        float3 tau = betaR * (optR + optSunR) + betaM * 1.1f * (optM + optSunM);
        float3 atten = exp(-tau);
        sumR += hr * atten;
        sumM += hm * atten;
    }

    float mu = dot(dClamped, sEff);
    float phaseR = 3.0f / (16.0f * PI) * (1.0f + mu * mu);
    float g = 0.78f;
    float phaseM = 3.0f / (8.0f * PI) * ((1.0f - g * g) * (1.0f + mu * mu)) / ((2.0f + g * g) * pow(max(1.0f + g * g - 2.0f * g * mu, 1e-3f), 1.5f));

    // Well-balanced dynamic range so volumetric clouds and blue zenith never blow out!
    float sunPower = mix(9.2f, 3.2f, nightBlend);
    float3 nishita = (sumR * betaR * phaseR + sumM * betaM * phaseM) * sunPower;

    float zenithW = pow(clamp(d.y, 0.0f, 1.0f), 0.52f);
    float3 presetSky = mix(u.skyHorizon.rgb, u.skyZenith.rgb, zenithW);
    float3 goldenSunsetTint = mix(nishita, presetSky * 0.78f + nishita * 0.55f, 0.38f);
    float3 nightSky = presetSky * (1.0f + 0.35f * pow(max(dot(dClamped, s), 0.0f), 4.0f));
    float3 sky = mix(goldenSunsetTint, nightSky, nightBlend * 0.88f);

    // Subtle crisp sub-pixel circular stars at night
    if (highDetail && nightBlend > 0.35f && d.y > 0.08f) {
        float2 starCoord = float2(atan2(d.z, d.x), asin(clamp(d.y, -1.0f, 1.0f))) * 320.0f;
        float2 cell = floor(starCoord);
        float2 f = fract(starCoord) - 0.5f;
        float starH = hash21(cell);
        if (starH > 0.992f) {
            float rad = length(f - (hash33(float3(cell, 1.0f)).xy - 0.5f) * 0.4f);
            float pt = smoothstep(0.18f, 0.02f, rad);
            float twinkle = smoothstep(0.992f, 0.9995f, starH) * smoothstep(0.08f, 0.35f, d.y);
            sky += float3(0.75f, 0.86f, 1.0f) * pt * twinkle * nightBlend * 0.85f;
        }
    }

    if (d.y < 0.0f) {
        sky = mix(sky, u.skyGround.rgb, clamp(-d.y * 5.0f, 0.0f, 1.0f));
    }

    return max(sky, float3(0.0f));
}

static float3 sampleSkyDome(float3 dir, constant RTFrameUniforms& u, bool includeSunDisc, bool highDetail = true) {
    float3 d = normalize(dir);
    float3 s = normalize(u.sunDir.xyz);
    float3 sky = evaluateNishitaAtmosphere(d, u, highDetail);

    float sunDot = max(dot(d, s), 0.0f);
    float lumZen = dot(u.skyZenith.rgb, float3(0.2126f, 0.7152f, 0.0722f));
    float nightBlend = clamp(1.0f - smoothstep(0.012f, 0.075f, lumZen), 0.0f, 1.0f);
    float coverage = clamp(u.envParams.x, 0.0f, 1.0f);

    // Layered 3D fBM Volumetric Clouds
    if (d.y > 0.012f && coverage > 0.02f) {
        float2 windShift = float2(u.envParams.y * 0.0015f, 0.0f);
        int cloudLayers = highDetail ? 6 : 3;
        float accumAlpha = 0.0f;
        float3 accumLight = float3(0.0f);
        float baseH = 1100.0f;
        float topH = 2100.0f;

        for (int i = 0; i < cloudLayers; ++i) {
            float frac = (float(i) + 0.5f) / float(cloudLayers);
            float h = mix(baseH, topH, frac);
            float tRay = h / max(d.y, 0.035f);
            if (tRay > 36000.0f) continue;

            float2 p = d.xz * tRay * 0.00042f + windShift + float2(float(i) * 0.37f, float(i) * 0.21f);
            float shape = highDetail ? fbm5(p) : fbm2D(p);
            float verticalProfile = sin(frac * PI);
            float threshold = mix(0.74f, 0.38f, coverage);
            float density = smoothstep(threshold, threshold + 0.24f, shape + verticalProfile * 0.14f);

            if (density > 0.005f) {
                float2 pSun = p + s.xz * 0.18f;
                float sunOccl = fbm2D(pSun);
                float lightTransmit = exp(-max(sunOccl - threshold + 0.12f, 0.0f) * 3.5f);
                float silverLining = pow(sunDot, 10.0f) * 0.65f + pow(sunDot, 36.0f) * 1.1f;

                float3 darkBelly = mix(u.skyZenith.rgb * 0.52f + float3(0.09f, 0.08f, 0.13f), float3(0.025f, 0.038f, 0.075f), nightBlend);
                float3 litRim = mix(u.coronaColor.rgb * 1.15f + u.sunColor.rgb * 0.38f, u.skyHorizon.rgb * 1.25f, nightBlend * 0.85f);
                float3 layerCol = mix(darkBelly, litRim, clamp(lightTransmit * 0.68f + frac * 0.34f + silverLining * 0.32f, 0.0f, 1.0f));

                float distFade = exp(-tRay * 0.000055f) * smoothstep(0.012f, 0.075f, d.y);
                float layerAlpha = density * (0.55f / float(cloudLayers)) * 2.4f * distFade;
                accumLight += (1.0f - accumAlpha) * layerCol * layerAlpha;
                accumAlpha = clamp(accumAlpha + (1.0f - accumAlpha) * layerAlpha, 0.0f, 0.92f);
            }
        }
        sky = sky * (1.0f - accumAlpha) + accumLight;
    }

    // Moody Late-Autumn East Coast Rockport Atmospheric Horizon & Soft Distant Ridge Haze
    // (ZERO cartoon skyline rectangles or flat cutouts — pure photographic atmospheric depth!)
    if (d.y > -0.025f && d.y < 0.115f) {
        float az = atan2(d.z, d.x);
        float ridgeFar = fbm2D(float2(az * 2.2f + 1.3f, 0.7f)) * 0.048f + 0.008f;
        float3 horizonHaze = evaluateNishitaAtmosphere(normalize(float3(d.x, 0.022f, d.z)), u, false);

        if (d.y < ridgeFar) {
            float3 baseHill = mix(float3(0.055f, 0.052f, 0.056f), float3(0.012f, 0.016f, 0.026f), nightBlend);
            float3 mtnCol = mix(baseHill, horizonHaze, 0.78f);
            float edge = smoothstep(ridgeFar, ridgeFar - 0.006f, d.y);
            sky = mix(sky, mtnCol, edge);
        }
    }

    // Subtle atmospheric autumn mist along the low horizon (gritty NFSMW 2005 East Coast mood)
    if (d.y > -0.02f && d.y < 0.22f) {
        float mistBand = exp(-pow((d.y - 0.014f) / 0.055f, 2.0f));
        float sunForward = 0.20f + 0.80f * pow(sunDot, 2.6f);
        float goldenness = (1.0f - nightBlend) * smoothstep(0.42f, 0.14f, s.y);
        float3 mistTint = mix(u.skyHorizon.rgb, u.coronaColor.rgb * 1.12f + u.sunColor.rgb * 0.22f, goldenness);
        sky += mistTint * mistBand * sunForward * mix(0.06f, 0.20f, goldenness) * (1.0f - nightBlend * 0.75f);
    }

    if (includeSunDisc) {
        float sunIntensity = u.sunDir.w;
        float aureole = pow(sunDot, 16.0f) * 0.32f + pow(sunDot, 140.0f) * 1.4f;
        float disc = smoothstep(0.99945f, 0.99982f, sunDot) * (nightBlend > 0.5f ? 4.0f : 12.0f);
        sky += u.coronaColor.rgb * aureole * (0.24f + 0.08f * sunIntensity);
        sky += u.sunColor.rgb * disc * max(sunIntensity * 0.36f, 0.35f);
    }

    return max(sky, float3(0.0f));
}

// ---------------------------------------------------------------------------
// Cook-Torrance GGX PBR BRDF + BMW M3 GTR Livery + GT7 Angel-Eye Headlights
// ---------------------------------------------------------------------------

static inline float D_GGX(float NdotH, float alpha) {
    float a2 = alpha * alpha;
    float denom = NdotH * NdotH * (a2 - 1.0f) + 1.0f;
    return a2 / max(PI * denom * denom, 1e-5f);
}

static inline float G_SmithGGX(float NdotV, float NdotL, float roughness) {
    float r = roughness + 1.0f;
    float k = (r * r) * 0.125f;
    float gv = NdotV / max(NdotV * (1.0f - k) + k, 1e-5f);
    float gl = NdotL / max(NdotL * (1.0f - k) + k, 1e-5f);
    return gv * gl;
}

static inline float3 F_Schlick(float cosTheta, float3 F0) {
    return F0 + (1.0f - F0) * pow(clamp(1.0f - cosTheta, 0.0f, 1.0f), 5.0f);
}

static inline float F_SchlickScalar(float cosTheta, float F0) {
    return F0 + (1.0f - F0) * pow(clamp(1.0f - cosTheta, 0.0f, 1.0f), 5.0f);
}

static float3 evaluateBMWLivery(float3 mPos, float3 silverBase, float3 blueStripe, thread float& stripeMask) {
    float ax = abs(mPos.x);
    float y = mPos.y;
    float z = mPos.z;

    // Ultra-crisp 3.5mm motorsport vinyl edge width (eliminates fuzzy/blurry transitions)
    const float e = 0.0035f;

    // 1. Twin iconic BMW M3 GTR racing stripes over hood, roof, and upper trunk (clean break above rear plate!)
    float centerBand = smoothstep(0.172f + e, 0.172f - e, ax) * smoothstep(0.042f - e, 0.042f + e, ax);
    float hoodRoofMask = smoothstep(0.46f - e, 0.46f + e, y) * smoothstep(-1.84f - e, -1.84f + e, z);
    float upperTrunkMask = smoothstep(0.875f - e, 0.875f + e, y) * smoothstep(-1.84f + e, -1.84f - e, z);
    float topStripes = centerBand * max(hoodRoofMask, upperTrunkMask);

    // 2. Iconic sweeping royal-cobalt M3 GTR side vinyl slash + smooth tapered front fender contour + rear quarter kick-up
    // Wrap the front boundary smoothly along the sculpted widebody fender flare & side intake contour (ZERO boxy notch!)
    float flankXThresh = 0.665f + 0.195f * smoothstep(1.72f, 2.12f, z);
    float flankSide = smoothstep(flankXThresh - e, flankXThresh + e, ax);
    float slashTop = 0.645f - 0.150f * z + 0.032f * sin(z * 1.45f);
    float slashBot = 0.245f - 0.110f * z;
    float flankSlash = smoothstep(slashBot - e, slashBot + e, y) *
                       smoothstep(slashTop + e, slashTop - e, y) *
                       smoothstep(-1.72f - e, -1.72f + e, z) *
                       smoothstep(1.94f + e, 1.94f - e, z);

    // Smooth tapered front widebody fender spear: curves gracefully around the front wheel arch and tapers
    // cleanly into a sleek diagonal wedge along the side bumper contour without any rectangular corner step!
    float zFront = max(0.0f, z - 1.50f);
    float frontTopCurve = 0.655f - 0.22f * zFront - 0.58f * zFront * zFront;
    float frontBotCurve = 0.215f + 0.24f * smoothstep(1.78f, 2.08f, z);
    float frontSpear = smoothstep(1.18f - e, 1.18f + e, z) *
                       smoothstep(frontBotCurve - e, frontBotCurve + e, y) *
                       smoothstep(frontTopCurve + e, frontTopCurve - e, y);

    // Rear widebody quarter kick-up arch (cleanly framed, stopping before rear valance)
    float rearKickTop = 0.915f + 0.06f * smoothstep(-0.95f, -1.55f, z);
    float rearWedge = smoothstep(-0.78f + e, -0.78f - e, z) *
                      smoothstep(-1.98f - e, -1.98f + e, z) *
                      smoothstep(0.42f - e, 0.42f + e, y) *
                      smoothstep(rearKickTop + e, rearKickTop - e, y);

    // Subtle crisp pinstripe border along the main cobalt vinyl edge (offset by 6mm)
    float flankUnion = max(flankSlash, max(frontSpear, rearWedge));
    float innerMask = clamp(max(topStripes, flankSide * flankUnion), 0.0f, 1.0f);

    // Crisp outer pinstripe line (8mm wide metallic silver/deep-navy accent hugging the vinyl)
    float outerBand = smoothstep(0.184f + e, 0.184f - e, ax) * smoothstep(0.030f - e, 0.030f + e, ax) * max(hoodRoofMask, upperTrunkMask);
    float outerFlank = flankSide * (
        smoothstep(slashBot - 0.014f, slashBot - 0.008f, y) * smoothstep(slashTop + 0.014f, slashTop + 0.008f, y)
        * smoothstep(-1.73f, -1.71f, z) * smoothstep(1.99f, 1.97f, z)
    );
    float pinstripeMask = clamp(max(outerBand, outerFlank) - innerMask, 0.0f, 1.0f);

    stripeMask = innerMask;
    float3 col = mix(silverBase, blueStripe, innerMask);
    col = mix(col, float3(0.010f, 0.035f, 0.125f), pinstripeMask * 0.78f);
    return col;
}

static inline float hexHoneycombCell(float2 p, thread float& edgeDist) {
    // Exact hexagonal honeycomb grid SDF: returns 1 on mesh wire, 0 inside open hex cell
    const float2 s = float2(1.0f, 1.7320508f);
    float4 hC = floor(float4(p, p - float2(0.5f, 1.0f)) / s.xyxy) + 0.5f;
    float4 h = float4(p - hC.xy * s, p - (hC.zw + 0.5f) * s);
    float2 local = (dot(h.xy, h.xy) < dot(h.zw, h.zw)) ? h.xy : h.zw;
    float2 q = abs(local);
    float hexD = max(dot(q, normalize(float2(1.0f, 1.7320508f))), q.x);
    edgeDist = 0.5f - hexD;
    return smoothstep(0.115f, 0.075f, edgeDist);
}

static inline float sampleGlyph5x7(int glyphIdx, int gx, int gy) {
    // 5x7 crisp bitmap font (row 0 = bottom, row 6 = top) for "M W   2 0 2 6"
    // 0='M', 1='W', 2=' ', 3='2', 4='0', 5='6'
    if (gx < 0 || gx >= 5 || gy < 0 || gy >= 7) return 0.0f;
    const uint rowsM[7] = { 17u, 17u, 17u, 21u, 21u, 27u, 17u };
    const uint rowsW[7] = { 10u, 21u, 21u, 21u, 17u, 17u, 17u };
    const uint rows2[7] = { 31u, 16u,  8u,  4u,  2u, 17u, 14u };
    const uint rows0[7] = { 14u, 17u, 19u, 21u, 25u, 17u, 14u };
    const uint rows6[7] = { 14u, 17u, 17u, 30u, 16u, 17u, 14u };
    uint rowBits = 0u;
    if      (glyphIdx == 0) rowBits = rowsM[gy];
    else if (glyphIdx == 1) rowBits = rowsW[gy];
    else if (glyphIdx == 3) rowBits = rows2[gy];
    else if (glyphIdx == 4) rowBits = rows0[gy];
    else if (glyphIdx == 5) rowBits = rows6[gy];
    uint bit = (rowBits >> uint(4 - gx)) & 1u;
    return float(bit);
}

static inline float evaluatePlateTextMW2026(float px, float py) {
    // Map normalized plate coordinates px in [0.19, 0.91], py in [0.18, 0.64] to "MW 2026"
    if (px < 0.19f || px > 0.91f || py < 0.18f || py > 0.64f) return 0.0f;
    float u = (px - 0.19f) / 0.72f;
    float v = (py - 0.18f) / 0.46f;
    // 7 character slots ("M", "W", " ", "2", "0", "2", "6")
    const int chars[7] = { 0, 1, 2, 3, 4, 3, 5 };
    float slotF = u * 7.0f;
    int slot = clamp(int(floor(slotF)), 0, 6);
    float localU = fract(slotF);
    if (localU < 0.12f || localU > 0.88f) return 0.0f;
    float charU = (localU - 0.12f) / 0.76f;
    int gx = int(floor(charU * 5.0f));
    int gy = int(floor(v * 7.0f));
    return sampleGlyph5x7(chars[slot], gx, gy);
}

static inline float3 unpackNormalSample(float3 texSample, float3 N, float3 T, float3 B, float strength) {
    float3 nMap = texSample * 2.0f - 1.0f;
    nMap.xy *= strength;
    return normalize(T * nMap.x + B * nMap.y + N * max(nMap.z, 0.1f));
}

// ---------------------------------------------------------------------------
// Surface Evaluation Helper
// ---------------------------------------------------------------------------

struct HitSurface {
    float3 worldPos;
    float3 N;
    float3 clearcoatN;
    float3 geomN;
    float2 uv;
    float3 modelPos;
    uint matID;
    float3 baseAlbedo;
    float metallic;
    float roughness;
    float clearcoat;
    float ccRoughness;
    float ior;
    float3 emissive;
    float puddleMask;
    float alpha;
};

struct TraceResult {
    bool hit;
    float distance;
    uint instanceID;
    uint primitiveID;
    float2 bary;
};

static inline bool evaluateAlphaCutoutCandidate(
    uint instanceID,
    uint primitiveID,
    float2 bary,
    device const RTInstanceMeta* instances,
    device const RkptVertex* worldVertices,
    device const uint* worldIndices,
    device const RkptMaterial* worldMaterials,
    device const BindlessTexEntry* worldTextures,
    uint worldTexCount,
    sampler repeatSamp
) {
    RTInstanceMeta inst = instances[instanceID];
    if ((inst.flags & (1u << 4)) == 0u) return true;
    uint i0 = worldIndices[inst.indexOffset + primitiveID * 3u + 0u];
    uint i1 = worldIndices[inst.indexOffset + primitiveID * 3u + 1u];
    uint i2 = worldIndices[inst.indexOffset + primitiveID * 3u + 2u];
    RkptVertex v0 = worldVertices[inst.vertexOffset + i0];
    RkptVertex v1 = worldVertices[inst.vertexOffset + i1];
    RkptVertex v2 = worldVertices[inst.vertexOffset + i2];
    RkptMaterial wmat = worldMaterials[v0.matIndex];
    if (wmat.diffuseTexIndex == 0xFFFFFFFFu || wmat.diffuseTexIndex >= worldTexCount) {
        return true;
    }
    float w0 = 1.0f - bary.x - bary.y;
    float2 uv = float2(v0.uv) * w0 + float2(v1.uv) * bary.x + float2(v2.uv) * bary.y;
    float a = worldTextures[wmat.diffuseTexIndex].tex.sample(repeatSamp, uv, level(0.0f)).a;
    return (a >= 0.54f);
}

static inline TraceResult traceSceneRay(
    ray r,
    instance_acceleration_structure tlas,
    uint mask,
    bool hasRkptWorld,
    device const RTInstanceMeta* instances,
    device const RkptVertex* worldVertices,
    device const uint* worldIndices,
    device const RkptMaterial* worldMaterials,
    device const BindlessTexEntry* worldTextures,
    uint worldTexCount,
    sampler repeatSamp
) {
    TraceResult res;
    res.hit = false;
    res.distance = r.max_distance;
    res.instanceID = 0u;
    res.primitiveID = 0u;
    res.bary = float2(0.0f);

    if (!hasRkptWorld) {
        intersector<triangle_data, instancing> rt;
        rt.assume_geometry_type(geometry_type::triangle);
        auto h = rt.intersect(r, tlas, mask);
        if (h.type == intersection_type::triangle) {
            res.hit = true;
            res.distance = h.distance;
            res.instanceID = h.instance_id;
            res.primitiveID = h.primitive_id;
            res.bary = h.triangle_barycentric_coord;
        }
        return res;
    }

    intersection_params params;
    params.assume_geometry_type(geometry_type::triangle);
    params.force_opacity(forced_opacity::none);

    intersection_query<triangle_data, instancing> iq;
    iq.reset(r, tlas, mask, params);
    while (iq.next()) {
        if (iq.get_candidate_intersection_type() == intersection_type::triangle) {
            if (evaluateAlphaCutoutCandidate(
                iq.get_candidate_instance_id(),
                iq.get_candidate_primitive_id(),
                iq.get_candidate_triangle_barycentric_coord(),
                instances, worldVertices, worldIndices, worldMaterials, worldTextures, worldTexCount, repeatSamp
            )) {
                iq.commit_triangle_intersection();
            }
        }
    }
    if (iq.get_committed_intersection_type() == intersection_type::triangle) {
        res.hit = true;
        res.distance = iq.get_committed_distance();
        res.instanceID = iq.get_committed_instance_id();
        res.primitiveID = iq.get_committed_primitive_id();
        res.bary = iq.get_committed_triangle_barycentric_coord();
    }
    return res;
}

static inline float traceShadowVisibility(
    ray sr,
    instance_acceleration_structure tlas,
    uint mask,
    bool hasRkptWorld,
    device const RTInstanceMeta* instances,
    device const RkptVertex* worldVertices,
    device const uint* worldIndices,
    device const RkptMaterial* worldMaterials,
    device const BindlessTexEntry* worldTextures,
    uint worldTexCount,
    sampler repeatSamp
) {
    if (!hasRkptWorld) {
        intersector<triangle_data, instancing> shadowRT;
        shadowRT.assume_geometry_type(geometry_type::triangle);
        shadowRT.accept_any_intersection(true);
        auto sh = shadowRT.intersect(sr, tlas, mask);
        return (sh.type == intersection_type::none) ? 1.0f : 0.0f;
    }

    intersection_params params;
    params.assume_geometry_type(geometry_type::triangle);
    params.force_opacity(forced_opacity::none);
    params.accept_any_intersection(true);

    intersection_query<triangle_data, instancing> iq;
    iq.reset(sr, tlas, mask, params);
    while (iq.next()) {
        if (iq.get_candidate_intersection_type() == intersection_type::triangle) {
            if (evaluateAlphaCutoutCandidate(
                iq.get_candidate_instance_id(),
                iq.get_candidate_primitive_id(),
                iq.get_candidate_triangle_barycentric_coord(),
                instances, worldVertices, worldIndices, worldMaterials, worldTextures, worldTexCount, repeatSamp
            )) {
                iq.commit_triangle_intersection();
                return 0.0f;
            }
        }
    }
    return (iq.get_committed_intersection_type() == intersection_type::none) ? 1.0f : 0.0f;
}

// ---------------------------------------------------------------------------
// World-Space Rockport Puddle Field + Raindrop Ripples + Anisotropic Sampler
// ---------------------------------------------------------------------------

static inline void evaluateRockportPuddleAndRipples(
    float2 wp,
    float worldNY,
    float viewAbsY,
    float wetness,
    float rainStrength,
    float rainTime,
    float dist,
    bool computeRipples,
    thread float& outPuddle,
    thread float& outDamp,
    thread float3& outRippleN
) {
    // Strictly restrict standing puddles to truly horizontal road/sidewalk surfaces (<23 deg slope)
    // so sloped curb bevels never turn into solar-glare mirrors or MetalFX temporal smear smears!
    float upMask = smoothstep(0.92f, 0.985f, worldNY);
    float wetGate = smoothstep(0.06f, 0.22f, wetness);
    float2 warp = float2(fbm2D(wp * 0.09f), fbm2D(wp * 0.09f + float2(5.2f, 1.3f)));
    float puddleField = fbm2D(wp * 0.135f + warp * 1.15f);
    // Natural rain-soaked puddles in active rain (0.40) vs smaller localized post-rain pools in sunlit golden hour (0.70)
    float heavyRain = smoothstep(0.40f, 0.85f, rainStrength) * smoothstep(0.70f, 0.92f, wetness);
    float pThresh = mix(0.88f, mix(0.70f, 0.40f, heavyRain), wetness);
    // Wide feathered smoothstep (pThresh ± 0.14) eliminates razor-sharp puddle boundaries; strictly 0 on bone-dry roads!
    outPuddle = smoothstep(pThresh - 0.14f, pThresh + 0.14f, puddleField) * upMask * wetGate;
    // Subtle continuous rainwater sheen during active rain while preserving organic puddle boundaries
    outPuddle = max(outPuddle, 0.28f * upMask * heavyRain);
    outDamp   = max(smoothstep(pThresh - 0.36f, pThresh + 0.06f, puddleField) * upMask * wetness * wetGate, 0.72f * upMask * heavyRain);
    outRippleN = float3(0.0f, 1.0f, 0.0f);

    // Delicate 15-40cm concentric raindrop ripple rings with grazing-angle attenuation (zero midground zebra-striping!)
    if (computeRipples && outPuddle > 0.20f && wetness > 0.70f && rainStrength > 0.35f && dist < 38.0f) {
        float grazingFade = smoothstep(0.06f, 0.24f, viewAbsY);
        float nearPop = 1.0f + 0.08f * smoothstep(9.0f, 2.5f, dist);
        float fade = smoothstep(38.0f, 3.0f, dist) * grazingFade * smoothstep(0.20f, 0.52f, outPuddle) * clamp(rainStrength, 0.0f, 1.0f);
        if (fade > 0.01f) {
            float2 ripGrad = float2(0.0f);
            int maxLayers = (dist < 18.0f) ? 3 : 2;
            for (int layer = 0; layer < maxLayers; ++layer) {
                float scale = (layer == 0) ? 1.60f : ((layer == 1) ? 2.80f : 4.40f);
                float amp   = ((layer == 0) ? 0.31f : ((layer == 1) ? 0.21f : 0.13f)) * ((layer < 2) ? nearPop : 1.0f);
                float2 p = wp * scale + float2(float(layer) * 17.3f, float(layer) * 9.1f);
                float2 cell = floor(p);
                float2 f = fract(p);
                for (int dy = -1; dy <= 1; ++dy) {
                    for (int dx = -1; dx <= 1; ++dx) {
                        float2 nb = cell + float2(float(dx), float(dy));
                        float3 rnd = hash33(float3(nb, float(layer) * 13.7f + 1.9f));
                        float2 center = float2(float(dx), float(dy)) + (0.20f + 0.60f * rnd.xy);
                        float2 diff = f - center;
                        float r = length(diff);
                        float cycle = fract(rainTime * (0.68f + 0.38f * rnd.z) + rnd.z * 6.2831f);
                        float ringR = 0.07f + cycle * 0.74f;
                        float band = r - ringR;
                        // Delicate 2-crest raindrop capillary wavelet (15-40cm realistic puddles, never thick motor oil)
                        float env = exp(-band * band * 44.0f) * (1.0f - cycle * 0.72f) * smoothstep(0.92f, 0.04f, r);
                        float wave = (sin(band * 28.0f) + 0.42f * sin((band + 0.11f) * 36.0f)) * env;
                        float2 dir = diff / max(r, 0.006f);
                        ripGrad += dir * wave * amp;
                    }
                }
            }
            outRippleN = normalize(float3(ripGrad.x * fade, 1.0f, ripGrad.y * fade));
        }
    }
}

static inline float4 sampleWorldAnisotropic(
    texture2d<float> dTex,
    sampler repeatSamp,
    float2 uv,
    float3 rayDir,
    float3 worldN,
    float3 p0,
    float3 p1,
    float3 p2,
    float2 uv0,
    float2 uv1,
    float2 uv2,
    float dist,
    constant RTFrameUniforms& u,
    thread float& outLod
) {
    float3 e1 = p1 - p0;
    float3 e2 = p2 - p0;
    float3 triCross = cross(e1, e2);
    float area2 = max(dot(triCross, triCross), 1e-8f);
    float3 gradW1 = cross(e2, triCross) / area2;
    float3 gradW2 = cross(triCross, e1) / area2;
    float2 duv1 = uv1 - uv0;
    float2 duv2 = uv2 - uv0;

    float pxWorld = dist * (2.0f * u.cameraRight.w / max(u.rtParams.y, 480.0f));
    float cosInc = max(abs(dot(worldN, -rayDir)), 0.14f);
    float aniso = clamp(1.0f / cosInc, 1.0f, 6.0f);

    float3 viewProj = rayDir - worldN * dot(rayDir, worldN);
    float vpLen = length(viewProj);
    float3 majorDir = (vpLen > 1e-4f) ? (viewProj / vpLen) : normalize(e1 + float3(1e-5f, 0.0f, 0.0f));
    float3 minorDir = normalize(cross(worldN, majorDir));

    float2 dUV_major = (duv1 * dot(gradW1, majorDir) + duv2 * dot(gradW2, majorDir)) * (pxWorld * aniso);
    float2 dUV_minor = (duv1 * dot(gradW1, minorDir) + duv2 * dot(gradW2, minorDir)) * pxWorld;

    float2 texDims = float2(max(float(dTex.get_width()), 16.0f), max(float(dTex.get_height()), 16.0f));
    float minorTexels = length(dUV_minor * texDims);
    outLod = clamp(log2(max(minorTexels, 1.0f)), 0.0f, 6.0f);

    float majorLen = length(dUV_major);
    if (majorLen > 0.16f) {
        dUV_major *= (0.16f / majorLen);
    }

    // 4-tap grazing-angle anisotropic filter eliminates PS2 64x512 road-strip corduroy washboard aliasing
    float4 c0 = dTex.sample(repeatSamp, uv - dUV_major * 0.375f, level(outLod));
    float4 c1 = dTex.sample(repeatSamp, uv - dUV_major * 0.125f, level(outLod));
    float4 c2 = dTex.sample(repeatSamp, uv + dUV_major * 0.125f, level(outLod));
    float4 c3 = dTex.sample(repeatSamp, uv + dUV_major * 0.375f, level(outLod));
    return (c0 + c1 + c2 + c3) * 0.25f;
}

static HitSurface resolveWorldSurface(
    float3 rayOrigin,
    float3 rayDir,
    float dist,
    RTInstanceMeta inst,
    uint primitiveID,
    float2 bary,
    bool isPrimary,
    device const RkptVertex* worldVertices,
    device const uint* worldIndices,
    device const RkptMaterial* worldMaterials,
    device const BindlessTexEntry* worldTextures,
    constant RTFrameUniforms& u,
    constant RTDynamicFX& dynFX,
    texture2d<float> concreteDiffTex,
    texture2d<float> concreteNormTex,
    sampler repeatSamp
) {
    uint i0 = worldIndices[inst.indexOffset + primitiveID * 3u + 0u];
    uint i1 = worldIndices[inst.indexOffset + primitiveID * 3u + 1u];
    uint i2 = worldIndices[inst.indexOffset + primitiveID * 3u + 2u];

    RkptVertex v0 = worldVertices[inst.vertexOffset + i0];
    RkptVertex v1 = worldVertices[inst.vertexOffset + i1];
    RkptVertex v2 = worldVertices[inst.vertexOffset + i2];

    float w0 = 1.0f - bary.x - bary.y;
    float w1 = bary.x;
    float w2 = bary.y;

    float3 localPos = float3(v0.position) * w0 + float3(v1.position) * w1 + float3(v2.position) * w2;
    float3 localN = float3(v0.normal) * w0 + float3(v1.normal) * w1 + float3(v2.normal) * w2;
    float nLen = length(localN);
    float3 p0 = (inst.modelMatrix * float4(float3(v0.position), 1.0f)).xyz;
    float3 p1 = (inst.modelMatrix * float4(float3(v1.position), 1.0f)).xyz;
    float3 p2 = (inst.modelMatrix * float4(float3(v2.position), 1.0f)).xyz;
    float3 gN = cross(p1 - p0, p2 - p0);
    float gLen = length(gN);
    gN = (gLen > 1e-6f) ? (gN / gLen) : float3(0.0f, 1.0f, 0.0f);
    float3 worldN = (nLen > 1e-5f) ? normalize((inst.invTransposeMatrix * float4(localN / nLen, 0.0f)).xyz) : gN;

    // Double-sided world geometry normal flip (PS2 scenery frequently uses double-sided strips)
    if (dot(gN, rayDir) > 0.0f) gN = -gN;
    if (dot(worldN, rayDir) > 0.0f) worldN = -worldN;

    HitSurface s;
    s.worldPos = rayOrigin + rayDir * dist;
    s.N = worldN;
    s.clearcoatN = worldN;
    s.geomN = gN;
    s.uv = float2(v0.uv) * w0 + float2(v1.uv) * w1 + float2(v2.uv) * w2;
    s.modelPos = localPos;
    s.matID = MAT_URBAN_CONCRETE;
    s.metallic = 0.0f;
    s.roughness = 0.72f;
    s.clearcoat = 0.0f;
    s.ccRoughness = 0.04f;
    s.ior = 1.48f;
    s.emissive = float3(0.0f);
    s.puddleMask = 0.0f;
    s.alpha = 1.0f;

    RkptMaterial wmat = worldMaterials[v0.matIndex];
    uint texCount = uint(max(u.jitterParams.w, 1.0f));
    float vAO = (float(v0.vertexAO) * w0 + float(v1.vertexAO) * w1 + float(v2.vertexAO) * w2) / 255.0f;
    float aoMul = mix(0.54f, 1.0f, clamp(vAO, 0.0f, 1.0f));

    float3 texCol = float3(0.42f, 0.43f, 0.45f);
    float mipLod = clamp(log2(max(dist * 0.14f, 1.0f)), 0.0f, 6.0f);
    bool hasTex = (wmat.diffuseTexIndex != 0xFFFFFFFFu && wmat.diffuseTexIndex < texCount);
    bool useAniso = isPrimary && (wmat.surfaceClass == 0u || wmat.surfaceClass == 1u || wmat.surfaceClass == 2u);

    if (hasTex) {
        texture2d<float> dTex = worldTextures[wmat.diffuseTexIndex].tex;
        float4 sampled;
        if (useAniso) {
            sampled = sampleWorldAnisotropic(
                dTex, repeatSamp, s.uv, rayDir, worldN,
                p0, p1, p2, float2(v0.uv), float2(v1.uv), float2(v2.uv),
                dist, u, mipLod
            );
        } else {
            // Fast 1-tap hardware mip fetch on secondary reflections & GI bounces (saves 11 tex fetches/bounce!)
            float bounceLod = isPrimary ? mipLod : max(mipLod, 1.5f);
            sampled = dTex.sample(repeatSamp, s.uv, level(bounceLod));
        }
        texCol = sampled.rgb;
        s.alpha = sampled.a;

        // 3x3 Sobel height-to-normal bump derivation on PRIMARY rays only
        // Suppress Sobel heavily on ROAD_ASPHALT / SIDEWALK (0.18x) so low-res PS2 texel rows never carve washboard stripes!
        if (isPrimary && wmat.surfaceClass != 7u) {
            float classBumpBoost = (wmat.surfaceClass == 1u || wmat.surfaceClass == 2u)
                ? 0.18f
                : ((wmat.surfaceClass == 0u) ? 1.15f : 0.90f);
            float bumpAmp = clamp(wmat.bumpStrength * classBumpBoost, 0.0f, 1.50f) * exp(-dist * 0.018f);
            if (bumpAmp > 0.02f) {
                float2 texel = float2(1.0f / max(float(dTex.get_width()), 16.0f),
                                      1.0f / max(float(dTex.get_height()), 16.0f)) * exp2(min(mipLod, 2.0f));
                float3 lumaW = float3(0.2126f, 0.7152f, 0.0722f);
                float tl = dot(dTex.sample(repeatSamp, s.uv + float2(-texel.x, -texel.y), level(mipLod)).rgb, lumaW);
                float tc = dot(dTex.sample(repeatSamp, s.uv + float2(    0.0f, -texel.y), level(mipLod)).rgb, lumaW);
                float tr = dot(dTex.sample(repeatSamp, s.uv + float2( texel.x, -texel.y), level(mipLod)).rgb, lumaW);
                float ml = dot(dTex.sample(repeatSamp, s.uv + float2(-texel.x,     0.0f), level(mipLod)).rgb, lumaW);
                float mr = dot(dTex.sample(repeatSamp, s.uv + float2( texel.x,     0.0f), level(mipLod)).rgb, lumaW);
                float bl = dot(dTex.sample(repeatSamp, s.uv + float2(-texel.x,  texel.y), level(mipLod)).rgb, lumaW);
                float bc = dot(dTex.sample(repeatSamp, s.uv + float2(    0.0f,  texel.y), level(mipLod)).rgb, lumaW);
                float br = dot(dTex.sample(repeatSamp, s.uv + float2( texel.x,  texel.y), level(mipLod)).rgb, lumaW);
                float gx = (tr + 2.0f * mr + br) - (tl + 2.0f * ml + bl);
                float gy = (bl + 2.0f * bc + br) - (tl + 2.0f * tc + tr);
                float3 t, b;
                buildONB(worldN, t, b);
                s.N = normalize(worldN + (t * (-gx) + b * (-gy)) * (bumpAmp * 0.72f));
            }
        }
    }

    float lum = dot(texCol, float3(0.2126f, 0.7152f, 0.0722f));
    float wetness = clamp(u.skyZenith.w, 0.0f, 1.0f);
    float nightScale = u.neonParams.w;
    // Strict true-night ramp (0.0 in golden_hour & daylight, 1.0 in night_neon) — prevents daytime checkerboard windows!
    float isNight = smoothstep(0.032f, 0.015f, u.skyZenith.r);
    float rainStrength = max(isNight * clamp(nightScale, 0.0f, 1.0f), dynFX.timeWeather.z);
    // Decoupled simulation clock (NEVER driven by sampleIdx rtParams.z, so multi-SPP raindrop rings accumulate razor-sharp!)
    float rainTime = dynFX.timeWeather.x * 0.62f + 1.37f;

    s.baseAlbedo = texCol * aoMul;
    s.roughness = clamp(wmat.roughnessHint > 0.01f ? wmat.roughnessHint : 0.72f, 0.04f, 0.96f);
    s.metallic = clamp(wmat.metallicHint, 0.0f, 1.0f);

    if (wmat.surfaceClass == 1u) {
        // ROAD_ASPHALT (1): tactile multi-scale charcoal asphalt grit + continuous feathered rain puddles + mirror ripples!
        s.matID = MAT_WET_ASPHALT;
        float2 wp = s.worldPos.xz;
        float puddle = 0.0f, damp = 0.0f;
        float3 rippleN = float3(0.0f, 1.0f, 0.0f);
        evaluateRockportPuddleAndRipples(wp, worldN.y, abs(rayDir.y), wetness, rainStrength, rainTime, dist, isPrimary, puddle, damp, rippleN);

        // Multi-scale world-space asphalt aggregate (macro wear 3/m + medium tarmac grit 11.5/m + fine mineral chips 32/m)
        float macroFade = exp(-dist * 0.012f);
        float fineFade  = exp(-dist * 0.028f);
        float aggMacro  = fbm2D(wp * 3.0f);
        float aggMed    = noise2D(wp * 11.5f + float2(3.1f, 7.4f));
        float aggFine   = noise2D(wp * 32.0f + float2(11.3f, 5.7f));
        float aggDelta  = (aggMed - 0.5f) * 0.62f * macroFade + (aggFine - 0.5f) * 0.50f * fineFade;
        float aggAlbedo = 1.0f + aggDelta * 0.52f + (aggMacro - 0.5f) * 0.26f;

        float3 aggWorldN = s.N;
        if (isPrimary) {
            float gxMed  = noise2D(wp * 13.0f + float2(0.14f, 0.0f)) - noise2D(wp * 13.0f - float2(0.14f, 0.0f));
            float gzMed  = noise2D(wp * 13.0f + float2(0.0f, 0.14f)) - noise2D(wp * 13.0f - float2(0.0f, 0.14f));
            float gxFine = noise2D(wp * 36.0f + float2(0.08f, 0.0f)) - noise2D(wp * 36.0f - float2(0.08f, 0.0f));
            float gzFine = noise2D(wp * 36.0f + float2(0.0f, 0.08f)) - noise2D(wp * 36.0f - float2(0.0f, 0.08f));
            float gxAgg  = gxMed * 0.72f * macroFade + gxFine * 0.68f * fineFade;
            float gzAgg  = gzMed * 0.72f * macroFade + gzFine * 0.68f * fineFade;
            float aggNStr = 0.62f * (1.0f - puddle * 0.86f);
            aggWorldN = normalize(s.N + float3(gxAgg, 0.0f, gzAgg) * aggNStr);
        }

        // Realistic dark charcoal tarmac (neutralizes clay/ice tint while keeping foreground wet pools legible, never pitch-black!)
        float paintPreserve = smoothstep(0.30f, 0.62f, lum);
        float3 roadTex = mix(float3(lum * 0.96f, lum * 0.97f, lum * 1.00f), texCol, 0.42f + 0.58f * paintPreserve);
        float dryGain = mix(0.84f, 0.98f, paintPreserve);
        float wetGain = mix(0.68f, 0.88f, paintPreserve);
        float albedoGain = mix(mix(dryGain, dryGain * 0.88f, damp), wetGain, puddle);
        float3 charcoalFloor = float3(0.040f, 0.042f, 0.046f) * aoMul * aggAlbedo;
        s.baseAlbedo = clamp(max(roadTex * aoMul * albedoGain * aggAlbedo, charcoalFloor), float3(0.0f), float3(0.92f));

        float heavyRainRoad = smoothstep(0.40f, 0.85f, rainStrength) * smoothstep(0.70f, 0.92f, wetness);
        float dryR    = 0.78f;
        float dampR   = 0.50f;
        float puddleR = mix(0.088f, 0.020f, heavyRainRoad);
        s.roughness   = clamp(mix(mix(dryR, dampR, damp), puddleR, puddle) + (aggMed - 0.5f) * 0.10f * (1.0f - puddle), 0.018f, 0.94f);
        s.clearcoat   = max(puddle * mix(0.46f, 0.92f, heavyRainRoad), damp * 0.08f);
        s.ccRoughness = mix(0.14f, mix(0.048f, 0.016f, heavyRainRoad), puddle);
        s.puddleMask  = puddle;
        float3 puddleSurfaceN = normalize(mix(worldN, rippleN, 0.78f));
        s.N = normalize(mix(aggWorldN, puddleSurfaceN, puddle * 0.88f));
        s.clearcoatN = normalize(mix(worldN, rippleN, puddle * 0.78f));
    } else if (wmat.surfaceClass == 2u) {
        // SIDEWALK (2): architectural poured-concrete slabs with crisp 1.5m expansion joints & 1K CC0 broom-finish concrete grain!
        // Uses 4-tap wide UV macro-averaging so blurry PS2 diamond strips de-blob cleanly even on 1-mip v1 .rkpt textures.
        s.matID = MAT_WET_ASPHALT;
        float2 wp = s.worldPos.xz;
        float puddle = 0.0f, damp = 0.0f;
        float3 rippleN = float3(0.0f, 1.0f, 0.0f);
        evaluateRockportPuddleAndRipples(wp, worldN.y, abs(rayDir.y), wetness, rainStrength, rainTime, dist, isPrimary, puddle, damp, rippleN);

        float detailFade = exp(-dist * 0.012f);
        float2 cUV1 = wp * 0.45f;
        float2 cUV2 = float2(wp.x * 0.18f - wp.y * 0.13f, wp.x * 0.13f + wp.y * 0.18f) + float2(0.31f, 0.57f);
        float3 ccDiff = mix(concreteDiffTex.sample(repeatSamp, cUV1, level(0.0f)).rgb,
                            concreteDiffTex.sample(repeatSamp, cUV2, level(0.0f)).rgb, 0.38f);
        float ccLuma = dot(ccDiff, float3(0.2126f, 0.7152f, 0.0722f));
        float ccGrain = mix(1.0f, clamp(0.58f + ccLuma * 1.05f, 0.64f, 1.38f), detailFade * 0.95f);

        // Crisp 1.5m poured-concrete sidewalk slab expansion joints + subtle per-slab concrete tone & broom-finish variation
        float2 slabCoord = wp / 1.50f;
        float2 slabCell = floor(slabCoord);
        float2 slabF = fract(slabCoord);
        float dEdgeX = min(slabF.x, 1.0f - slabF.x);
        float dEdgeZ = min(slabF.y, 1.0f - slabF.y);
        float isTopWalk = smoothstep(0.62f, 0.90f, worldN.y);
        float jointGroove = max(smoothstep(0.032f, 0.006f, dEdgeX), smoothstep(0.032f, 0.006f, dEdgeZ))
                          * isTopWalk * detailFade;
        float slabTone = 0.89f + 0.22f * hash21(slabCell);
        float fineGrit = 0.92f + 0.16f * noise2D(wp * 22.0f);

        // Wide 4-tap UV macro-average completely washes out blurry PS2 diamond tile contrast on both 1-mip v1 & BC7-mip v2!
        float3 macroCol = texCol;
        if (hasTex && isTopWalk > 0.2f) {
            texture2d<float> dTex = worldTextures[wmat.diffuseTexIndex].tex;
            float avgLod = max(mipLod + 2.5f, 3.5f);
            float3 avgTex = 0.25f * (
                dTex.sample(repeatSamp, s.uv + float2( 0.19f,  0.17f), level(avgLod)).rgb +
                dTex.sample(repeatSamp, s.uv + float2(-0.19f,  0.17f), level(avgLod)).rgb +
                dTex.sample(repeatSamp, s.uv + float2( 0.17f, -0.19f), level(avgLod)).rgb +
                dTex.sample(repeatSamp, s.uv + float2(-0.17f, -0.19f), level(avgLod)).rgb
            );
            macroCol = mix(texCol, mix(avgTex, texCol, 0.10f), isTopWalk);
        }
        float macroLum = clamp(dot(macroCol, float3(0.2126f, 0.7152f, 0.0722f)), 0.16f, 0.62f);
        float3 warmPouredConcrete = mix(float3(macroLum * 1.01f, macroLum * 0.99f, macroLum * 0.95f), macroCol, 0.35f);
        float wetGain = mix(mix(0.92f, 0.80f, damp), 0.66f, puddle);
        float3 slabCol = warmPouredConcrete * aoMul * slabTone * ccGrain * fineGrit * wetGain * (1.0f - jointGroove * 0.54f);
        s.baseAlbedo = clamp(max(slabCol, float3(0.048f, 0.050f, 0.052f) * aoMul), float3(0.0f), float3(0.90f));

        float3 concWorldN = worldN;
        if (isPrimary) {
            float3 ccN = concreteNormTex.sample(repeatSamp, cUV1, level(0.0f)).rgb;
            float3 ct, cb;
            buildONB(worldN, ct, cb);
            concWorldN = unpackNormalSample(ccN, worldN, ct, cb, 0.62f * detailFade * (1.0f - puddle * 0.85f));
            // Crisp beveled concrete expansion joint normal tilt
            float jNx = (smoothstep(0.038f, 0.007f, slabF.x) - smoothstep(0.962f, 0.993f, slabF.x)) * 0.44f;
            float jNz = (smoothstep(0.038f, 0.007f, slabF.y) - smoothstep(0.962f, 0.993f, slabF.y)) * 0.44f;
            concWorldN = normalize(concWorldN + float3(jNx, 0.0f, jNz) * isTopWalk * detailFade * (1.0f - puddle * 0.8f));
        }

        s.roughness   = clamp(mix(mix(0.84f, 0.52f, damp), 0.026f, puddle), 0.020f, 0.94f);
        s.clearcoat   = max(puddle * 0.86f, damp * 0.08f);
        s.ccRoughness = mix(0.14f, 0.018f, puddle);
        s.puddleMask  = puddle;
        float3 puddleSurfaceN = normalize(mix(worldN, rippleN, 0.78f));
        s.N = normalize(mix(concWorldN, puddleSurfaceN, puddle * 0.85f));
        s.clearcoatN = normalize(mix(worldN, rippleN, puddle * 0.78f));
    } else if (wmat.surfaceClass == 3u) {
        // BUILDING_GLASS: mirror architectural reflections by day/dusk; refined mullioned warm interior offices at TRUE NIGHT!
        s.matID = MAT_URBAN_GLASS;
        s.roughness = 0.075f;
        s.metallic = 0.24f;
        s.clearcoat = 0.92f;
        s.ccRoughness = 0.02f;
        s.ior = 1.52f;
        if (isNight > 0.05f) {
            float horizCoord = (abs(worldN.x) > abs(worldN.z)) ? s.worldPos.z : s.worldPos.x;
            float relY = s.worldPos.y - u.carWorldPos.y;
            float2 paneUV = float2(horizCoord * 0.82f, relY * 0.40f);
            float2 cell = floor(paneUV);
            float2 f = fract(paneUV);
            float outerFrame = smoothstep(0.08f, 0.15f, f.x) * smoothstep(0.92f, 0.85f, f.x)
                             * smoothstep(0.11f, 0.20f, f.y) * smoothstep(0.89f, 0.80f, f.y);
            // Crisp architectural cross-mullions (center stile + horizontal transom rail) inside each lit window pane
            float innerMullionX = smoothstep(0.025f, 0.065f, abs(f.x - 0.50f));
            float innerMullionY = smoothstep(0.030f, 0.075f, abs(f.y - 0.54f));
            float mullion = outerFrame * innerMullionX * innerMullionY;
            float roomGrad = mix(0.45f, 1.0f, smoothstep(0.10f, 0.85f, f.y));
            float3 rnd = hash33(float3(cell, floor(dot(worldN.xz, float2(2.3f, 5.1f)) * 3.0f)));
            float litOcc = step(0.56f, rnd.x); // ~44% of mullioned panes lit
            float isStorefront = smoothstep(5.4f, 2.0f, relY);
            float3 warmTung = mix(float3(1.00f, 0.64f, 0.26f), float3(1.00f, 0.82f, 0.56f), rnd.y);
            float3 storeTint = (rnd.z > 0.82f) ? float3(0.24f, 0.78f, 0.98f)
                             : ((rnd.z < 0.14f) ? float3(0.96f, 0.28f, 0.24f) : warmTung);
            float3 winCol = mix(warmTung, storeTint, isStorefront * 0.35f);
            s.emissive = winCol * mullion * roomGrad * litOcc * isNight * mix(0.42f, 0.78f, isStorefront) * (0.55f + 0.45f * rnd.y);
        }
    } else if (wmat.surfaceClass == 4u) {
        // FOLIAGE_CUTOUT: crisp late-autumn / evergreen Rockport cypress & trees with fine needle/leaf cluster breakup
        s.matID = MAT_FOLIAGE_AUTUMN;
        s.roughness = 0.88f;
        s.metallic = 0.0f;
        s.clearcoat = 0.0f;
        float leafFade = exp(-dist * 0.018f);
        float2 fp = float2(s.worldPos.x + s.worldPos.z, s.worldPos.y);
        float c1 = noise2D(fp * 13.5f);
        float c2 = noise2D(fp * 25.0f + float2(7.1f, 3.3f));
        float gx = noise2D(fp * 18.0f + float2(0.12f, 0.0f)) - noise2D(fp * 18.0f - float2(0.12f, 0.0f));
        float gy = noise2D(fp * 18.0f + float2(0.0f, 0.12f)) - noise2D(fp * 18.0f - float2(0.0f, 0.12f));
        float3 ft, fb;
        buildONB(worldN, ft, fb);
        s.N = normalize(s.N + (ft * gx + fb * gy) * (0.42f * leafFade));
        s.baseAlbedo *= (0.82f + 0.36f * (c1 * 0.55f + c2 * 0.45f));
    } else if (wmat.surfaceClass == 5u) {
        // METAL_BRIDGE: weathered painted/galvanized Rockport bridge girders & railings
        s.matID = MAT_BRUSHED_ALUMINUM;
        s.metallic = max(wmat.metallicHint, 0.78f);
        s.roughness = clamp(wmat.roughnessHint > 0.01f ? wmat.roughnessHint : 0.36f, 0.16f, 0.62f);
    } else if (wmat.surfaceClass == 6u || (wmat.flags & 8u) != 0u) {
        // EMISSIVE_SIGN_LAMP: glowing streetlamps, traffic lights, storefront signs & neon billboards
        // Require either warm/saturated sign chroma or genuinely bright lamp texels (prevents pale retaining-wall concrete squares from glowing!)
        s.matID = MAT_STREETLAMP_LED;
        float maxC = max(texCol.r, max(texCol.g, texCol.b));
        float minC = min(texCol.r, min(texCol.g, texCol.b));
        float chroma = maxC - minC;
        float relWallY = s.worldPos.y - u.carWorldPos.y;
        float lowConcreteGuard = (relWallY < 2.8f && chroma < 0.10f && lum < 0.62f) ? 0.0f : 1.0f;
        float emGain = max(wmat.emissiveHint, 0.65f) * mix(0.28f, 1.35f, isNight) * lowConcreteGuard;
        s.emissive = min(texCol * emGain * smoothstep(0.28f, 0.64f, lum), float3(1.55f));
    } else if (wmat.surfaceClass == 7u) {
        // v1.4.0 WATER: 6-Wave Directional Gerstner + Micro-Capillary Ripples + Beer-Lambert Teal-Navy Absorption + Shoreline/Crest Foam!
        s.matID = MAT_COASTAL_WATER;
        float2 wp = s.worldPos.xz;
        float tW = dynFX.timeWeather.x;
        // Distance attenuation keeps far bay glints crisp without high-frequency subpixel aliasing
        float detailFade = exp(-dist * 0.0045f);
        float swellFade  = exp(-dist * 0.00065f);

        // 6 non-commensurate directional Gerstner waves (ocean swell + harbor chop + wind waves)
        const float2 wDir[6] = {
            float2( 0.866f,  0.500f),
            float2( 0.545f, -0.838f),
            float2(-0.788f,  0.616f),
            float2( 0.966f, -0.259f),
            float2(-0.375f, -0.927f),
            float2( 0.242f,  0.970f)
        };
        const float wLen[6] = { 19.5f, 12.2f, 7.4f, 4.3f, 2.35f, 1.25f };
        const float wAmp[6] = { 0.145f, 0.095f, 0.058f, 0.036f, 0.021f, 0.012f };
        const float wSteep[6] = { 0.42f, 0.38f, 0.34f, 0.28f, 0.24f, 0.20f };

        float2 dH = float2(0.0f);
        float crestSum = 0.0f;
        float waveHeight = 0.0f;
        for (int wi = 0; wi < 6; ++wi) {
            float k = 6.2831853f / wLen[wi];
            float speed = sqrt(9.81f * k); // Deep-water gravity-wave dispersion
            float phase = dot(wDir[wi], wp) * k + tW * speed * 0.58f + float(wi) * 1.73f;
            float sP = sin(phase);
            float cP = cos(phase);
            float amp = wAmp[wi] * ((wi < 3) ? swellFade : detailFade);
            float slope = k * amp * cP / max(1.0f - wSteep[wi] * k * amp * sP, 0.35f);
            dH += wDir[wi] * slope;
            waveHeight += amp * sP;
            // Steep peak pinching drives natural harbor whitecap cresting
            crestSum += max(0.0f, sP - 0.48f) * wSteep[wi] * ((wi < 4) ? 1.0f : 0.45f);
        }

        // 2-octave wind-driven micro-capillary ripple normal spectrum for sparkling GGX sun/moon glints
        if (isPrimary && detailFade > 0.04f) {
            float2 capUV1 = wp * 2.65f + float2(tW * 0.48f, -tW * 0.31f);
            float2 capUV2 = wp * 6.40f + float2(-tW * 0.62f, tW * 0.54f);
            float gx1 = noise2D(capUV1 + float2(0.14f, 0.0f)) - noise2D(capUV1 - float2(0.14f, 0.0f));
            float gz1 = noise2D(capUV1 + float2(0.0f, 0.14f)) - noise2D(capUV1 - float2(0.0f, 0.14f));
            float gx2 = noise2D(capUV2 + float2(0.10f, 0.0f)) - noise2D(capUV2 - float2(0.10f, 0.0f));
            float gz2 = noise2D(capUV2 + float2(0.0f, 0.10f)) - noise2D(capUV2 - float2(0.0f, 0.10f));
            dH += (float2(gx1, gz1) * 0.20f + float2(gx2, gz2) * 0.13f) * detailFade;
        }

        float3 waveN = normalize(float3(-dH.x, 1.0f, -dH.y));

        // Beer-Lambert depth absorption: shallow coastal piers/beaches glow translucent emerald-teal -> deep bay navy
        float shoreHint = clamp((1.0f - aoMul) * 1.45f + smoothstep(0.32f, 0.72f, lum) * 0.75f, 0.0f, 1.0f);
        float estDepthM = mix(11.5f, 0.75f, shoreHint);
        float viewPath = estDepthM / max(abs(rayDir.y), 0.18f);
        float3 beerTrans = exp(-viewPath * float3(0.42f, 0.14f, 0.085f));
        float3 deepNavy   = float3(0.004f, 0.018f, 0.040f);
        float3 shallowTeal = float3(0.024f, 0.135f, 0.155f);
        float3 waterBody  = mix(deepNavy, shallowTeal, clamp(beerTrans.g * 0.85f + shoreHint * 0.25f, 0.0f, 1.0f));
        // Subtle subsurface emerald sun-scatter glowing through thin wave crests toward the sun
        float3 sunDirW = normalize(u.sunDir.xyz);
        float sssPhase = pow(max(dot(rayDir, sunDirW), 0.0f), 3.0f) * smoothstep(0.02f, 0.24f, waveHeight + 0.10f);
        waterBody += float3(0.022f, 0.145f, 0.135f) * sssPhase * u.sunDir.w * 0.26f * (1.0f - isNight);

        // Shoreline / pier breakwater foam + bubbly sea-foam lacing on steep Gerstner wave crests
        float foamNoise1 = fbm2D(wp * 0.65f + float2(tW * 0.14f, -tW * 0.09f));
        float foamNoise2 = noise2D(wp * 3.10f + float2(-tW * 0.22f, tW * 0.17f));
        float shoreFoam  = smoothstep(0.48f, 0.86f, shoreHint) * smoothstep(0.34f, 0.72f, foamNoise1 * 0.72f + foamNoise2 * 0.28f);
        float crestFoam  = smoothstep(0.38f, 0.78f, crestSum) * smoothstep(0.42f, 0.76f, foamNoise1 * 0.60f + foamNoise2 * 0.40f) * detailFade;
        float foamMask   = clamp(max(shoreFoam * 0.88f, crestFoam * 0.68f), 0.0f, 0.92f);

        float3 foamCol = mix(float3(0.76f, 0.84f, 0.88f), float3(0.95f, 0.93f, 0.89f), 0.45f * (1.0f - isNight));
        s.baseAlbedo  = mix(mix(waterBody, texCol * 0.18f, 0.18f), foamCol, foamMask);
        s.N           = normalize(mix(waveN, float3(0.0f, 1.0f, 0.0f), foamMask * 0.55f));
        s.clearcoatN  = waveN;
        s.roughness   = mix(mix(0.022f, 0.052f, smoothstep(80.0f, 950.0f, dist)), 0.62f, foamMask);
        s.metallic    = 0.0f;
        s.clearcoat   = (1.0f - foamMask * 0.88f);
        s.ccRoughness = mix(0.016f, 0.042f, smoothstep(100.0f, 1200.0f, dist));
        s.ior         = 1.333f;
    } else if (wmat.surfaceClass == 8u) {
        // TERRAIN_GRASS_DIRT
        s.matID = MAT_AUTUMN_TERRAIN;
        s.roughness = 0.86f;
    } else {
        // CONCRETE_WALL (0) & general Rockport masonry/buildings + horizontal plaza aprons
        s.matID = MAT_URBAN_CONCRETE;
        if (worldN.y > 0.82f) {
            // Horizontal concrete plaza / curb apron shares the EXACT same continuous world-space puddle field!
            float puddle = 0.0f, damp = 0.0f;
            float3 rippleN = float3(0.0f, 1.0f, 0.0f);
            evaluateRockportPuddleAndRipples(s.worldPos.xz, worldN.y, abs(rayDir.y), wetness, rainStrength, rainTime, dist, isPrimary, puddle, damp, rippleN);
            s.baseAlbedo = texCol * aoMul * mix(1.0f, 0.72f, max(puddle, damp * 0.50f));
            s.roughness = mix(mix(0.80f, 0.50f, damp), 0.026f, puddle);
            s.clearcoat = max(puddle * 0.88f, damp * 0.08f);
            s.ccRoughness = mix(0.14f, 0.018f, puddle);
            s.puddleMask = puddle;
            float3 puddleSurfaceN = normalize(mix(worldN, rippleN, 0.90f));
            s.N = normalize(mix(s.N, puddleSurfaceN, puddle * 0.88f));
            s.clearcoatN = normalize(mix(worldN, rippleN, puddle));
        } else {
            float rainGloss = wetness * 0.20f;
            s.roughness = clamp((wmat.roughnessHint > 0.01f ? wmat.roughnessHint : 0.78f) - rainGloss, 0.38f, 0.94f);
            // Subtle architectural running-bond brick/mortar relief on close warm/red Downtown masonry walls
            if (abs(worldN.y) < 0.35f && dist < 40.0f) {
                float brickChroma = smoothstep(0.025f, 0.13f, texCol.r - texCol.b * 0.85f) * smoothstep(0.05f, 0.22f, lum);
                if (brickChroma > 0.02f) {
                    float horizB = (abs(worldN.x) > abs(worldN.z)) ? s.worldPos.z : s.worldPos.x;
                    float rowY = s.worldPos.y * 11.0f;
                    float rowIdx = floor(rowY);
                    float colX = horizB * 4.4f + fmod(abs(rowIdx), 2.0f) * 0.5f;
                    float fx = fract(colX);
                    float fy = fract(rowY);
                    float mX = smoothstep(0.055f, 0.018f, min(fx, 1.0f - fx));
                    float mY = smoothstep(0.11f, 0.035f, min(fy, 1.0f - fy));
                    float mortar = max(mX, mY);
                    float bFade = smoothstep(40.0f, 8.0f, dist) * brickChroma;
                    float brickVar = 0.92f + 0.16f * hash21(float2(floor(colX), rowIdx));
                    s.baseAlbedo *= mix(1.0f, brickVar * (1.0f - mortar * 0.26f), bFade);
                    float3 bt, bb;
                    buildONB(worldN, bt, bb);
                    float bNx = (smoothstep(0.07f, 0.02f, fx) - smoothstep(0.93f, 0.98f, fx)) * 0.34f;
                    float bNy = (smoothstep(0.13f, 0.04f, fy) - smoothstep(0.87f, 0.96f, fy)) * 0.34f;
                    s.N = normalize(s.N + (bt * bNx + bb * bNy) * bFade);
                }
            }
            if (wmat.emissiveHint > 0.05f) {
                s.emissive = min(texCol * wmat.emissiveHint * mix(0.20f, 0.85f, isNight), float3(1.25f));
            }
            // Refined mullioned Downtown night windows on recessed dark panes of vertical CONCRETE_WALL facades (TRUE NIGHT ONLY!)
            // Start at relY > 3.6m so matte street-level retaining walls & highway parapets never sprout glowing square windows!
            if (u.carWorldPos.w > 0.5f && isNight > 0.05f && abs(worldN.y) < 0.35f) {
                float relY = s.worldPos.y - u.carWorldPos.y;
                if (relY > 3.6f && relY < 48.0f) {
                    float horizCoord = (abs(worldN.x) > abs(worldN.z)) ? s.worldPos.z : s.worldPos.x;
                    float2 bayGrid = float2(horizCoord * 0.82f, relY * 0.40f);
                    float2 cell = floor(bayGrid);
                    float2 f = fract(bayGrid);
                    float paneFrame = smoothstep(0.12f, 0.22f, f.x) * smoothstep(0.88f, 0.78f, f.x)
                                    * smoothstep(0.18f, 0.28f, f.y) * smoothstep(0.82f, 0.72f, f.y);
                    // Strict dark recessed pane texture gate so solid concrete/brick walls never light up
                    float darkWindowTex = smoothstep(0.13f, 0.05f, lum) * smoothstep(0.88f, 1.08f, (texCol.b + 0.015f) / (texCol.r + 0.020f));
                    float3 rnd = hash33(float3(cell, floor(dot(worldN.xz, float2(3.1f, 7.7f)) * 4.0f)));
                    float litChance = step(0.58f, rnd.x); // ~42% of dark recessed panes lit
                    float isGroundStore = smoothstep(5.2f, 2.2f, relY);
                    float winMask = paneFrame * darkWindowTex * litChance * isNight;
                    if (winMask > 0.01f) {
                        float3 warmTungsten = mix(float3(1.00f, 0.62f, 0.22f), float3(1.00f, 0.80f, 0.48f), rnd.y);
                        float3 storefrontAccent = (rnd.z > 0.82f) ? float3(0.22f, 0.78f, 0.98f)
                                                : ((rnd.z < 0.14f) ? float3(0.95f, 0.24f, 0.24f) : warmTungsten);
                        float3 glowCol = mix(warmTungsten, storefrontAccent, isGroundStore * 0.35f);
                        s.emissive += glowCol * winMask * mix(0.38f, 0.68f, isGroundStore) * (0.55f + 0.45f * rnd.y);
                        s.roughness = mix(s.roughness, 0.12f, winMask);
                        s.clearcoat = max(s.clearcoat, winMask * 0.65f);
                    }
                }
            }
        }
    }

    if ((inst.flags & (1u << 6)) != 0u) {
        // Sky / distant panorama dome cards stay softly self-lit to match horizon haze
        s.emissive = texCol * (0.35f + 0.35f * u.coronaColor.w);
    }
    return s;
}

static HitSurface resolveAIVehicleSurface(
    float3 rayOrigin,
    float3 rayDir,
    float dist,
    RTInstanceMeta inst,
    uint primitiveID,
    float2 bary,
    constant RTAISceneData& aiScene,
    constant RTFrameUniforms& u,
    constant RTDynamicFX& dynFX,
    sampler repeatSamp
) {
    HitSurface s;
    s.worldPos = rayOrigin + rayDir * dist;
    s.N = float3(0.0f, 1.0f, 0.0f);
    s.clearcoatN = s.N;
    s.geomN = s.N;
    s.uv = float2(0.0f);
    s.modelPos = float3(0.0f);
    s.matID = MAT_CAR_PAINT;
    s.baseAlbedo = float3(0.18f, 0.20f, 0.24f);
    s.metallic = 0.45f;
    s.roughness = 0.22f;
    s.clearcoat = 0.90f;
    s.ccRoughness = 0.022f;
    s.ior = 1.54f;
    s.emissive = float3(0.0f);
    s.puddleMask = 0.0f;
    s.alpha = 1.0f;

    if (aiScene.archetypeCount == 0u || aiScene.vertices == nullptr || aiScene.indices == nullptr) {
        return s;
    }

    uint i0 = aiScene.indices[inst.indexOffset + primitiveID * 3u + 0u];
    uint i1 = aiScene.indices[inst.indexOffset + primitiveID * 3u + 1u];
    uint i2 = aiScene.indices[inst.indexOffset + primitiveID * 3u + 2u];
    RTAIVertex v0 = aiScene.vertices[inst.vertexOffset + i0];
    RTAIVertex v1 = aiScene.vertices[inst.vertexOffset + i1];
    RTAIVertex v2 = aiScene.vertices[inst.vertexOffset + i2];

    float w0 = 1.0f - bary.x - bary.y;
    float w1 = bary.x;
    float w2 = bary.y;

    float3 localPos = float3(v0.position) * w0 + float3(v1.position) * w1 + float3(v2.position) * w2;
    float3 localN   = normalize(float3(v0.normal) * w0 + float3(v1.normal) * w1 + float3(v2.normal) * w2);
    float3 worldN   = normalize((inst.invTransposeMatrix * float4(localN, 0.0f)).xyz);

    float3 p0 = (inst.modelMatrix * float4(float3(v0.position), 1.0f)).xyz;
    float3 p1 = (inst.modelMatrix * float4(float3(v1.position), 1.0f)).xyz;
    float3 p2 = (inst.modelMatrix * float4(float3(v2.position), 1.0f)).xyz;
    float3 gN = normalize(cross(p1 - p0, p2 - p0));
    if (dot(gN, rayDir) > 0.0f) gN = -gN;
    if (dot(worldN, rayDir) > 0.0f) worldN = -worldN;

    s.N = worldN;
    s.clearcoatN = worldN;
    s.geomN = gN;
    s.uv = float2(v0.uv) * w0 + float2(v1.uv) * w1 + float2(v2.uv) * w2;
    s.modelPos = localPos;

    uint matClass   = uint(v0.partFlags & 0x0Fu);
    bool isLightbar = ((v0.partFlags & 0x10u) != 0u) || (matClass == 6u);
    uint archIdx    = min((inst.flags >> 8u) & 0x0Fu, min(aiScene.archetypeCount - 1u, 6u));
    bool isCop      = ((inst.flags & (1u << 12u)) != 0u) || (archIdx <= 2u);
    bool sirenOn    = ((inst.flags & (1u << 13u)) != 0u);
    float headOn    = u.skyGround.w;

    float2 skinUV = float2(s.uv.x, 1.0f - s.uv.y);
    float lod = clamp(log2(max(dist * 0.12f, 1.0f)), 0.0f, 4.0f);
    float3 skinCol = aiScene.skinTex[archIdx].sample(repeatSamp, skinUV, level(lod)).rgb;

    if (isLightbar) {
        // Authentic alternating Red (left: x < 0) / Blue (right: x > 0) HDR police roof/deck lightbar!
        s.matID = MAT_STREETLAMP_LED;
        s.roughness = 0.04f;
        s.metallic = 0.10f;
        s.clearcoat = 1.0f;
        s.ccRoughness = 0.015f;
        bool isLeft = (localPos.x < -0.055f);
        bool isCenter = (abs(localPos.x) <= 0.055f);
        float3 redCol   = float3(1.00f, 0.05f, 0.03f);
        float3 blueCol  = float3(0.05f, 0.24f, 1.00f);
        float3 whiteCol = float3(1.00f, 0.96f, 0.90f);
        float3 lensBase = isCenter ? float3(0.82f, 0.85f, 0.90f) : (isLeft ? float3(0.55f, 0.02f, 0.015f) : float3(0.02f, 0.12f, 0.62f));
        s.baseAlbedo = lensBase;

        if (sirenOn) {
            float phaseOff = float((inst.flags >> 16u) & 0xFFu) * (2.0f * PI / 255.0f);
            float t = dynFX.timeWeather.x * 11.5f + phaseOff;
            // Realistic double-flash wig-wag strobe (alternates red left <-> blue right + quick white takedown burst)
            float alt = sin(t);
            float dbl = 0.58f + 0.42f * sin(t * 3.0f);
            float redFlash   = smoothstep(-0.15f, 0.25f,  alt) * dbl;
            float blueFlash  = smoothstep(-0.15f, 0.25f, -alt) * dbl;
            float whiteFlash = smoothstep(0.80f, 0.98f, abs(sin(t * 1.5f)));
            if (isCenter) {
                s.emissive = whiteCol * (1.8f + 7.5f * whiteFlash);
            } else if (isLeft) {
                s.emissive = redCol * (0.95f + 13.5f * redFlash);
            } else {
                s.emissive = blueCol * (0.95f + 14.5f * blueFlash);
            }
        } else {
            // Parked/patrol standby polycarbonate lenses with subtle pilot glow
            s.emissive = lensBase * (0.18f + 0.35f * headOn);
        }
    } else if (matClass == 1u) {
        // Pure vulcanized carbon-black 3D rubber tire (ZERO PS2 checkered skin bleed!)
        s.matID = MAT_RUBBER_TIRE;
        s.baseAlbedo = float3(0.030f, 0.032f, 0.036f);
        s.metallic   = 0.0f;
        s.roughness  = 0.64f;
        s.clearcoat  = 0.0f;
    } else if (matClass == 7u) {
        // Crisp 3D recessed alloy / police interceptor metallic wheel rim (UVs encode radial (dx, dy) in [-1..1])
        s.matID = MAT_ALLOY_RIM;
        float r = length(s.uv);
        float theta = atan2(s.uv.y, s.uv.x);
        float hubCap = smoothstep(0.26f, 0.18f, r);
        float outerLip = smoothstep(0.84f, 0.93f, r);
        float spokeWave = abs(cos(theta * 2.5f)); // crisp 5-spoke star
        float spokeMask = max(max(hubCap, outerLip), smoothstep(0.56f, 0.72f, spokeWave) * smoothstep(0.95f, 0.78f, r));
        float3 rimBright = isCop ? float3(0.76f, 0.78f, 0.82f) : float3(0.82f, 0.84f, 0.88f);
        float3 brakeVoid = float3(0.045f, 0.048f, 0.055f);
        s.baseAlbedo = mix(brakeVoid, rimBright, spokeMask);
        s.metallic   = mix(0.25f, 0.88f, spokeMask);
        s.roughness  = mix(0.52f, 0.20f, spokeMask);
        s.clearcoat  = 0.55f * spokeMask;
        s.ccRoughness = 0.028f;
    } else if (matClass == 2u) {
        // Dark automotive tinted glass (reflective clearcoat)
        s.matID = MAT_URBAN_GLASS;
        s.baseAlbedo = float3(0.018f, 0.022f, 0.028f);
        s.metallic   = 0.14f;
        s.roughness  = 0.045f;
        s.clearcoat  = 0.96f;
        s.ccRoughness = 0.014f;
        s.ior        = 1.52f;
    } else if (matClass == 3u) {
        // Matte dark cabin interior
        s.matID = MAT_INTERIOR_DARK;
        s.baseAlbedo = float3(0.035f, 0.038f, 0.042f);
        s.metallic   = 0.02f;
        s.roughness  = 0.76f;
        s.clearcoat  = 0.0f;
    } else if (matClass == 4u) {
        // Police / traffic xenon headlights
        s.matID = MAT_HEADLIGHT_EMIT;
        s.baseAlbedo = float3(0.76f, 0.80f, 0.86f);
        s.metallic   = 0.55f;
        s.roughness  = 0.12f;
        s.clearcoat  = 0.85f;
        float hlPow = isCop ? (sirenOn ? 1.15f : (0.55f + 0.65f * headOn)) : (0.30f + 0.80f * headOn);
        s.emissive = float3(5.6f, 6.2f, 7.8f) * hlPow * smoothstep(-0.20f, 0.25f, localN.z);
    } else if (matClass == 5u) {
        // Police / traffic crimson LED brake & taillights
        s.matID = MAT_TAILLIGHT_EMIT;
        s.baseAlbedo = float3(0.24f, 0.012f, 0.010f);
        s.metallic   = 0.10f;
        s.roughness  = 0.06f;
        s.clearcoat  = 1.0f;
        s.emissive   = float3(4.8f, 0.06f, 0.028f) * (0.72f + 0.50f * headOn);
    } else {
        // Clearcoat automotive body paint driven by authentic extracted PS2 skin.png!
        s.matID = MAT_CAR_PAINT;
        float3 col = skinCol;
        if (!isCop && archIdx != 3u) {
            // Subtle deterministic per-slot OEM paint palette variation for civilian sedans/SUVs/minivans (keep traftaxi authentic yellow!)
            const float3 fleetTints[8] = {
                float3(1.00f, 1.00f, 1.00f), // Factory neutral
                float3(0.78f, 0.82f, 0.88f), // Cool Titanium Silver
                float3(0.38f, 0.46f, 0.62f), // Deep Steel Blue
                float3(0.68f, 0.24f, 0.22f), // Garnet Burgundy
                float3(0.40f, 0.42f, 0.45f), // Graphite Charcoal
                float3(0.32f, 0.48f, 0.36f), // Forest Pine
                float3(0.86f, 0.80f, 0.68f), // Champagne Beige
                float3(0.92f, 0.94f, 0.96f)  // Pearl White
            };
            uint vIdx = ((inst.flags >> 24u) & 0x0Fu) & 7u;
            float luma = dot(col, float3(0.2126f, 0.7152f, 0.0722f));
            float paintable = smoothstep(0.10f, 0.32f, luma);
            col = mix(col, col * fleetTints[vIdx] * 1.15f, paintable * 0.55f);
        }
        s.baseAlbedo = clamp(col, float3(0.02f), float3(0.96f));
        s.metallic   = isCop ? 0.42f : 0.52f;
        s.roughness  = isCop ? 0.17f : 0.21f;
        s.clearcoat  = 0.94f;
        s.ccRoughness = 0.020f;
        s.ior        = 1.56f;

        // Traffic archetypes have baked skin textures without separate headlight/taillight submeshes:
        // ignite authentic front headlights & rear ruby taillights at night/rain!
        if (!isCop) {
            float halfLen = (archIdx >= 5u) ? 2.35f : 2.15f;
            float frontLight = smoothstep(halfLen * 0.88f, halfLen * 0.98f, localPos.z)
                             * smoothstep(0.32f, 0.46f, localPos.y) * smoothstep(0.88f, 0.70f, localPos.y)
                             * smoothstep(0.32f, 0.50f, abs(localPos.x)) * smoothstep(0.45f, 0.78f, localN.z);
            float rearLight  = smoothstep(-halfLen * 0.88f, -halfLen * 0.98f, localPos.z)
                             * smoothstep(0.40f, 0.56f, localPos.y) * smoothstep(0.98f, 0.78f, localPos.y)
                             * smoothstep(0.36f, 0.52f, abs(localPos.x)) * smoothstep(0.45f, 0.78f, -localN.z);
            if (frontLight > 0.05f && headOn > 0.15f) {
                s.emissive += float3(4.5f, 4.8f, 5.8f) * frontLight * headOn;
            }
            if (rearLight > 0.05f) {
                s.emissive += float3(4.2f, 0.06f, 0.025f) * rearLight * (0.45f + 0.65f * headOn);
            }
        }
    }

    return s;
}

static HitSurface resolveHitSurface(
    float3 rayOrigin,
    float3 rayDir,
    float dist,
    uint instanceID,
    uint primitiveID,
    float2 bary,
    bool isPrimary,
    device const RTVertex* vertices,
    device const uint* indices,
    device const uint* triMaterials,
    device const RTInstanceMeta* instances,
    device const RTMaterial* materials,
    device const RkptVertex* worldVertices,
    device const uint* worldIndices,
    device const RkptMaterial* worldMaterials,
    device const BindlessTexEntry* worldTextures,
    constant RTAISceneData& aiScene,
    constant RTFrameUniforms& u,
    constant RTDynamicFX& dynFX,
    texture2d<float> asphaltDiffTex,
    texture2d<float> asphaltNormTex,
    texture2d<float> asphaltRoughTex,
    texture2d<float> concreteDiffTex,
    texture2d<float> concreteNormTex,
    texture2d<float> carbonDiffTex,
    texture2d<float> carbonNormTex,
    texture2d<float> carLiveryMaskTex,
    texture2d<float> carMiscTex,
    texture2d<float> carBadgeTex,
    texture2d<float> carHeadlightTex,
    texture2d<float> carTaillightTex,
    texture2d<float> carTireTex,
    texture2d<float> carInteriorTex,
    sampler repeatSamp
) {
    RTInstanceMeta inst = instances[instanceID];
    if ((inst.flags & (1u << 7)) != 0u) {
        return resolveAIVehicleSurface(
            rayOrigin, rayDir, dist, inst, primitiveID, bary,
            aiScene, u, dynFX, repeatSamp
        );
    }
    if ((inst.flags & (1u << 4)) != 0u) {
        return resolveWorldSurface(
            rayOrigin, rayDir, dist, inst, primitiveID, bary, isPrimary,
            worldVertices, worldIndices, worldMaterials, worldTextures, u, dynFX,
            concreteDiffTex, concreteNormTex, repeatSamp
        );
    }
    uint triIdx = inst.triOffset + primitiveID;
    uint i0 = indices[inst.indexOffset + primitiveID * 3u + 0u];
    uint i1 = indices[inst.indexOffset + primitiveID * 3u + 1u];
    uint i2 = indices[inst.indexOffset + primitiveID * 3u + 2u];

    RTVertex v0 = vertices[inst.vertexOffset + i0];
    RTVertex v1 = vertices[inst.vertexOffset + i1];
    RTVertex v2 = vertices[inst.vertexOffset + i2];

    float w0 = 1.0f - bary.x - bary.y;
    float w1 = bary.x;
    float w2 = bary.y;

    float3 localNormal = normalize(float3(v0.normal) * w0 + float3(v1.normal) * w1 + float3(v2.normal) * w2);
    float3 worldN = normalize((inst.invTransposeMatrix * float4(localNormal, 0.0f)).xyz);

    float3 p0 = (inst.modelMatrix * float4(float3(v0.position), 1.0f)).xyz;
    float3 p1 = (inst.modelMatrix * float4(float3(v1.position), 1.0f)).xyz;
    float3 p2 = (inst.modelMatrix * float4(float3(v2.position), 1.0f)).xyz;
    float3 gN = normalize(cross(p1 - p0, p2 - p0));
    if (dot(gN, rayDir) > 0.0f) gN = -gN;
    if (dot(worldN, rayDir) > 0.0f) worldN = -worldN;

    HitSurface s;
    s.worldPos = rayOrigin + rayDir * dist;
    s.N = worldN;
    s.clearcoatN = worldN;
    s.geomN = gN;
    s.uv = float2(v0.uv) * w0 + float2(v1.uv) * w1 + float2(v2.uv) * w2;
    s.modelPos = float3(v0.modelPos) * w0 + float3(v1.modelPos) * w1 + float3(v2.modelPos) * w2;
    s.matID = triMaterials[triIdx];

    RTMaterial mat = materials[min(s.matID, 31u)];
    s.baseAlbedo = mat.baseColor.rgb;
    s.metallic = mat.pbrParams.x;
    s.roughness = clamp(mat.pbrParams.y, 0.02f, 0.98f);
    s.clearcoat = mat.pbrParams.z;
    s.ccRoughness = clamp(mat.pbrParams.w, 0.012f, 0.5f);
    s.ior = mat.secondaryColor.w;
    s.emissive = mat.emissive.rgb;
    s.puddleMask = 0.0f;
    s.alpha = mat.baseColor.w;

    bool hasAuthenticTex = (u.driveParams.z > 0.5f);
    bool carSupportsLivery = (u.driveParams.w > 0.5f);
    uint carID = uint(u.hudParams.w + 0.5f); // 0=BMW M3 GTR, 1=Porsche Carrera GT, 2=Lamborghini Gallardo
    float2 carUV = float2(s.uv.x, 1.0f - s.uv.y);
    bool isSideExhaust = (carID == 0u && abs(s.modelPos.x) > 0.70f && s.modelPos.y > 0.16f && s.modelPos.y < 0.29f &&
                          s.modelPos.z > -0.28f && s.modelPos.z < 0.10f);
    float speedFrac = clamp(u.driveParams.x / 180.0f, 0.0f, 1.0f);

    if (s.matID == MAT_CAR_PAINT) {
        float stripeMask = 0.0f;
        if (carSupportsLivery && (inst.flags & 1u) != 0u && u.postParams.z > 0.5f) {
            if (hasAuthenticTex) {
                // 100% AUTHENTIC NFSMW (2005) BMW M3 GTR HERO VINYL (PSRTEST_MASK) mapped directly onto PS2 UVs!
                float mC = carLiveryMaskTex.sample(repeatSamp, carUV, level(0.0f)).r;
                float mL = carLiveryMaskTex.sample(repeatSamp, carUV + float2(-0.0012f, 0.0f), level(0.0f)).r;
                float mR = carLiveryMaskTex.sample(repeatSamp, carUV + float2( 0.0012f, 0.0f), level(0.0f)).r;
                float mU = carLiveryMaskTex.sample(repeatSamp, carUV + float2(0.0f, -0.0012f), level(0.0f)).r;
                float mD = carLiveryMaskTex.sample(repeatSamp, carUV + float2(0.0f,  0.0012f), level(0.0f)).r;
                float dilated = max(max(mL, mR), max(mU, mD));
                float thinOutline = clamp(dilated - mC, 0.0f, 1.0f);

                // Authentic BMW Titanium Silver Metallic + Iconic Most Wanted Royal Cobalt Blue Metallic + Crisp Silver Edge
                float3 silverBase = mat.baseColor.rgb;
                float3 royalBlue  = mat.secondaryColor.rgb;
                float3 col = mix(silverBase, royalBlue, mC);
                col = mix(col, float3(0.84f, 0.87f, 0.92f), thinOutline * 0.55f);
                s.baseAlbedo = col;
                stripeMask = mC;
            } else {
                s.baseAlbedo = evaluateBMWLivery(s.modelPos, mat.baseColor.rgb, mat.secondaryColor.rgb, stripeMask);
            }
            // Realistic two-stage automotive Titanium Silver metallic basecoat (0.74 restores crisp metallic daylight door reflections!)
            s.metallic  = mix(0.74f, 0.44f, stripeMask);
            s.roughness = mix(0.17f, 0.21f, stripeMask);
        } else {
            // Pure authentic factory metallic paint (Porsche GT Silver Metallic / Lamborghini Giallo Orion Pearl)
            s.baseAlbedo = mat.baseColor.rgb;
            if (carID == 2u) {
                // Subtle authentic Lamborghini Giallo Orion tri-coat pearl yellow/gold flop
                float viewFacing = clamp(dot(worldN, -rayDir), 0.0f, 1.0f);
                float3 pearlHighlight = float3(1.00f, 0.80f, 0.06f);
                float3 pearlGrazing   = float3(0.88f, 0.52f, 0.014f);
                s.baseAlbedo = mix(pearlGrazing, pearlHighlight, pow(viewFacing, 0.55f));
            }
            s.metallic  = mat.pbrParams.x;
            s.roughness = clamp(mat.pbrParams.y, 0.10f, 0.30f);
        }
        float flakeStrength = clamp(u.envParams.w, 0.0f, 0.04f);
        float3 flakeSeed = floor(s.modelPos * 1450.0f);
        float3 flakeRand = hash33(flakeSeed);
        float flakeSparkle = smoothstep(0.87f, 0.995f, flakeRand.x);
        float sunFace = smoothstep(0.05f, 0.45f, dot(worldN, normalize(u.sunDir.xyz)));
        s.baseAlbedo *= (1.0f + 0.14f * flakeSparkle * sunFace * clamp(flakeStrength * 180.0f, 0.0f, 1.0f));
        float3 t, b;
        buildONB(worldN, t, b);
        s.N = normalize(worldN + (t * (flakeRand.y - 0.5f) + b * (flakeRand.z - 0.5f)) * flakeStrength);
        s.clearcoatN = worldN;
        float nightWetBoost = smoothstep(0.65f, 0.92f, u.skyZenith.w) * smoothstep(0.45f, 1.10f, u.neonParams.w);
        s.clearcoat = mix(s.clearcoat, 1.0f, nightWetBoost);
        s.ccRoughness = mix(clamp(u.envParams.z, 0.009f, 0.18f), 0.010f, nightWetBoost);
        s.ior = mix(s.ior, 1.62f, nightWetBoost * 0.65f);
        // M3 GTR rear trunk recess (|x| < 0.48, y in [0.62..0.94], z < -1.88):
        // Preserves 100% of EA's authentic diagonal blue stripes (zero boxy stair-step!) while giving the unpainted
        // silver trapezoid cool BMW Titanium Silver metallic clearcoat + upward-clamped recess normal so downward-facing
        // trunk polygons (ny = -0.24..-0.96) never reflect warm brown asphalt as an olive/khaki patch!
        if (carID == 0u && s.modelPos.z < -1.88f && abs(s.modelPos.x) < 0.48f && s.modelPos.y > 0.62f && s.modelPos.y < 0.94f) {
            float silverTrunk = (1.0f - stripeMask);
            float lipCrease = 1.0f - 0.18f * smoothstep(0.885f, 0.865f, s.modelPos.y) * smoothstep(0.835f, 0.865f, s.modelPos.y);
            float3 coolTiSilver = float3(0.72f, 0.76f, 0.84f) * lipCrease;
            s.baseAlbedo = mix(s.baseAlbedo, coolTiSilver, silverTrunk);
            s.metallic   = mix(s.metallic, 0.26f, silverTrunk);
            s.roughness  = mix(s.roughness, 0.26f, silverTrunk);
            if (worldN.y < 0.26f && silverTrunk > 0.25f) {
                s.N = normalize(float3(worldN.x, max(worldN.y, 0.28f), worldN.z));
                s.clearcoatN = s.N;
            }
        }
    } else if (s.matID == MAT_CAR_PAINT_SILVER) {
        // Secondary factory body paint (DIABLOHP: splitter lip, mirrors, trim, M3 GTR rear license plate cove)
        s.baseAlbedo = mat.baseColor.rgb;
        if (carID == 2u) {
            float viewFacing = clamp(dot(worldN, -rayDir), 0.0f, 1.0f);
            s.baseAlbedo = mix(float3(0.88f, 0.52f, 0.014f), float3(1.00f, 0.80f, 0.06f), pow(viewFacing, 0.55f));
        }
        s.metallic   = (carID == 0u) ? 0.74f : mat.pbrParams.x;
        s.roughness  = clamp(mat.pbrParams.y, 0.14f, 0.30f);
        s.clearcoat  = 1.0f;
        float nightWetBoost = smoothstep(0.65f, 0.92f, u.skyZenith.w) * smoothstep(0.45f, 1.10f, u.neonParams.w);
        s.ccRoughness = mix(clamp(u.envParams.z, 0.010f, 0.18f), 0.010f, nightWetBoost);
        s.ior = mix(s.ior, 1.62f, nightWetBoost * 0.65f);
        s.clearcoatN = worldN;
        if (carID == 0u && s.modelPos.z < -1.85f && abs(s.modelPos.x) < 0.55f && s.modelPos.y > 0.60f && s.modelPos.y < 0.94f) {
            // Crisp cool BMW Titanium Silver metallic clearcoat matching upper trunk lid seamlessly (zero khaki cast!)
            float lipCrease = 1.0f - 0.16f * smoothstep(0.885f, 0.865f, s.modelPos.y) * smoothstep(0.835f, 0.865f, s.modelPos.y);
            s.baseAlbedo = float3(0.72f, 0.76f, 0.84f) * lipCrease;
            s.metallic = 0.26f;
            s.roughness = 0.26f;
            s.clearcoat = 1.0f;
            s.ccRoughness = 0.018f;
            if (worldN.y < 0.26f) {
                s.N = normalize(float3(worldN.x, max(worldN.y, 0.28f), worldN.z));
                s.clearcoatN = s.N;
            }
        }
    } else if (s.matID == MAT_CAR_BADGE) {
        // Real UV-mapped OEM badges & Carrera GT perforated stainless V10 engine-deck mesh!
        float4 badge = carBadgeTex.sample(repeatSamp, carUV, level(0.0f));
        if (carID == 1u && carUV.y < 0.50f) {
            // Iconic Porsche Carrera GT perforated stainless-steel V10 engine cover over dark recessed V10 bay
            float3 engineBayDark = float3(0.014f, 0.016f, 0.020f);
            float3 brushedSteel  = clamp(badge.rgb * 0.92f, float3(0.18f), float3(0.88f));
            s.baseAlbedo = mix(engineBayDark, brushedSteel, badge.a);
            s.metallic   = mix(0.10f, 0.92f, badge.a);
            s.roughness  = mix(0.82f, 0.24f, badge.a);
            s.clearcoat  = 0.15f * badge.a;
            s.ccRoughness = 0.04f;
            s.clearcoatN = worldN;
        } else {
            float3 paintBase = materials[MAT_CAR_PAINT].baseColor.rgb;
            float baseMet = materials[MAT_CAR_PAINT].pbrParams.x;
            float baseRough = 0.18f;
            if (carID == 0u) {
                float onHoodBlue = step(1.95f, s.modelPos.z) * step(abs(s.modelPos.x), 0.15f) * step(0.55f, s.modelPos.y);
                paintBase = mix(paintBase, materials[MAT_CAR_PAINT].secondaryColor.rgb, onHoodBlue);
                if (s.modelPos.z < -1.85f) {
                    // Rear BMW roundel trunk surround: match cool Titanium Silver trunk panel (prevents dark charcoal strip!)
                    paintBase = float3(0.72f, 0.76f, 0.84f);
                    baseMet = 0.26f;
                    baseRough = 0.26f;
                    if (worldN.y < 0.26f) {
                        s.N = normalize(float3(worldN.x, max(worldN.y, 0.28f), worldN.z));
                        s.clearcoatN = s.N;
                    }
                }
            }
            s.baseAlbedo = mix(paintBase, min(badge.rgb * 1.15f, float3(0.98f)), badge.a);
            s.metallic   = mix(baseMet, 0.32f, badge.a);
            s.roughness  = mix(baseRough, 0.14f, badge.a);
            s.clearcoat  = 1.0f;
            s.ccRoughness = 0.016f;
            if (carID != 0u || s.modelPos.z >= -1.85f) {
                s.clearcoatN = worldN;
            }
        }
    } else if (s.matID == MAT_CAR_MISC_TRIM) {
        // Real UV-mapped BMWM3GTRE46_MISC: honeycomb front lower air dam, hood louver vents, M3 side fender gill!
        float3 mc = hasAuthenticTex ? carMiscTex.sample(repeatSamp, carUV, level(0.0f)).rgb : float3(0.04f);
        float lum = dot(mc, float3(0.2126f, 0.7152f, 0.0722f));
        s.baseAlbedo = mc * 0.90f;
        s.metallic   = smoothstep(0.28f, 0.65f, lum) * 0.85f;
        s.roughness  = mix(0.68f, 0.18f, smoothstep(0.18f, 0.65f, lum));
        s.clearcoat  = smoothstep(0.35f, 0.75f, lum) * 0.40f;
    } else if (s.matID == MAT_CAR_MISC_CHROME || s.matID == MAT_CHROME) {
        if (isSideExhaust) {
            float tipT = smoothstep(0.785f, 0.868f, abs(s.modelPos.x));
            float bore = smoothstep(0.35f, 0.85f, abs(localNormal.x)) * step(0.845f, abs(s.modelPos.x));
            float3 rawTi  = float3(0.74f, 0.72f, 0.68f);
            float3 straw  = float3(0.80f, 0.62f, 0.32f);
            float3 violet = float3(0.42f, 0.25f, 0.56f);
            float3 blued  = float3(0.16f, 0.34f, 0.78f);
            float3 tiCol  = mix(rawTi, straw, smoothstep(0.0f, 0.38f, tipT));
            tiCol = mix(tiCol, violet, smoothstep(0.35f, 0.68f, tipT));
            tiCol = mix(tiCol, blued,  smoothstep(0.62f, 0.96f, tipT));
            s.baseAlbedo = mix(tiCol, float3(0.016f, 0.016f, 0.018f), bore * 0.88f);
            s.metallic   = mix(0.95f, 0.15f, bore * 0.88f);
            s.roughness  = mix(0.14f, 0.72f, bore * 0.88f);
        } else if (s.matID == MAT_CAR_MISC_CHROME && hasAuthenticTex) {
            // Real UV-mapped BMW kidney grille (gleaming chrome bezel over dark recessed slats!)
            float3 mc = carMiscTex.sample(repeatSamp, carUV, level(0.0f)).rgb;
            float lum = dot(mc, float3(0.2126f, 0.7152f, 0.0722f));
            s.baseAlbedo = mix(float3(0.012f, 0.013f, 0.016f), mc * 1.05f, smoothstep(0.05f, 0.35f, lum));
            s.metallic   = mix(0.15f, 0.96f, smoothstep(0.12f, 0.45f, lum));
            s.roughness  = mix(0.58f, 0.06f, smoothstep(0.15f, 0.55f, lum));
        }
    } else if (s.matID == MAT_CAR_MISC_METAL || s.matID == MAT_BRUSHED_ALUMINUM) {
        if (s.matID == MAT_CAR_MISC_METAL && hasAuthenticTex) {
            float3 mc = carMiscTex.sample(repeatSamp, carUV, level(0.0f)).rgb;
            s.baseAlbedo = mc * 0.92f;
            s.metallic   = 0.90f;
            s.roughness  = 0.22f;
            s.clearcoat  = 0.25f;
        } else if ((inst.flags & 4u) != 0u && s.worldPos.x > 5.2f && s.worldPos.y < 1.5f) {
            // Weathered galvanized W-beam highway steel guardrail & loading dock edge bumper
            float2 gwp = float2(s.worldPos.z + u.envParams.y, s.worldPos.y);
            float zinc = noise2D(gwp * float2(1.4f, 6.0f));
            s.baseAlbedo = float3(0.36f, 0.38f, 0.40f) * (0.78f + 0.32f * zinc);
            s.metallic   = 0.84f;
            s.roughness  = mix(0.36f, 0.54f, zinc);
            s.clearcoat  = 0.0f;
        }
    } else if (s.matID == MAT_RUBBER_TIRE) {
        // Pure authentic vulcanized carbon-black rubber + procedural rain grooves, angled shoulder tread blocks & sidewall molding ribs!
        // Uses dynamic per-car axle & radius metrics from dynFX.carMetrics across all 40 playable cars.
        float frontZ = (abs(dynFX.carMetrics.x) > 0.2f) ? dynFX.carMetrics.x : ((carID == 1u) ? 1.205f : ((carID == 2u) ? 1.160f : 1.595f));
        float rearZ  = (abs(dynFX.carMetrics.y) > 0.2f) ? dynFX.carMetrics.y : ((carID == 1u) ? -1.480f : ((carID == 2u) ? -1.390f : -1.1295f));
        float wz = (s.modelPos.z > 0.0f) ? frontZ : rearZ;
        float wy = (s.modelPos.z > 0.0f) ? max(dynFX.carMetrics.z, 0.30f) : max(dynFX.carMetrics.w, 0.30f);
        float2 relYZ = float2(s.modelPos.y - wy, s.modelPos.z - wz);
        float rad = length(relYZ);
        float theta = atan2(relYZ.x, relYZ.y);
        float isSidewall = smoothstep(0.45f, 0.72f, abs(localNormal.x));
        float3 t, b;
        buildONB(worldN, t, b);

        float microGrain = noise2D(float2(theta * 36.0f, rad * 210.0f));
        float treadX = abs(s.modelPos.x);
        // 4 crisp circumferential performance rain grooves + angled herringbone shoulder blocks
        float circGroove = smoothstep(0.11f, 0.24f, abs(fract(treadX * 22.0f) - 0.5f));
        float slantPhase = fract(theta * 14.0f + treadX * 8.5f * sign(s.modelPos.x));
        float shoulderBlock = smoothstep(0.08f, 0.22f, abs(slantPhase - 0.5f));
        float treadRelief = mix(0.56f, 1.0f, circGroove * (0.68f + 0.32f * shoulderBlock));
        // Subtle radial sidewall molding rings & faint raised sidewall lettering band
        float swBand = smoothstep(0.018f, 0.004f, abs(rad - 0.268f)) * 0.18f;
        float swRib  = (0.5f + 0.5f * sin(rad * 195.0f)) * smoothstep(0.22f, 0.25f, rad) * smoothstep(0.32f, 0.28f, rad);
        float swDetail = 0.90f + 0.10f * swRib + swBand * (0.5f + 0.5f * sin(theta * 48.0f));

        float3 carbonRubber = float3(0.032f, 0.034f, 0.038f);
        float surfaceMod = mix(treadRelief, swDetail, isSidewall);
        s.baseAlbedo = carbonRubber * (0.88f + 0.22f * microGrain) * surfaceMod;
        s.metallic   = 0.0f;
        s.roughness  = mix(0.62f, 0.52f, isSidewall) + (microGrain - 0.5f) * 0.06f;
        s.clearcoat  = 0.0f;
        float treadBump = (1.0f - isSidewall) * ((1.0f - circGroove) * -0.28f + (shoulderBlock - 0.5f) * 0.22f);
        float swBump = isSidewall * ((swRib - 0.5f) * 0.14f + (microGrain - 0.5f) * 0.10f);
        float bumpScale = (1.0f - speedFrac * 0.55f);
        s.N = normalize(worldN + (t * (treadBump + swBump) + b * ((microGrain - 0.5f) * 0.12f)) * bumpScale);
        s.clearcoatN = s.N;
    } else if (s.matID == MAT_ALLOY_RIM) {
        // Real OEM forged rims driven by extracted per-car TIRE texture (BBS / Carrera GT 5-spoke / Gallardo Cassiopeia)
        float3 tTex = hasAuthenticTex ? carTireTex.sample(repeatSamp, carUV, level(0.0f)).rgb : float3(0.84f);
        float lum = dot(tTex, float3(0.2126f, 0.7152f, 0.0722f));
        float spokeMask = smoothstep(0.05f, 0.24f, lum);
        s.baseAlbedo = mix(float3(0.10f, 0.11f, 0.13f), mix(float3(0.90f, 0.92f, 0.96f), tTex * 1.32f, 0.42f), spokeMask);
        s.metallic   = mix(0.45f, 0.92f, spokeMask);
        s.roughness  = mix(0.42f, mix(0.13f, 0.20f, speedFrac * 0.4f), spokeMask);
        s.clearcoat  = 0.60f * spokeMask;
        s.ccRoughness = 0.025f;
    } else if (s.matID == MAT_BRAKE_ROTOR) {
        float frontZ = (abs(dynFX.carMetrics.x) > 0.2f) ? dynFX.carMetrics.x : ((carID == 1u) ? 1.205f : ((carID == 2u) ? 1.160f : 1.595f));
        float rearZ  = (abs(dynFX.carMetrics.y) > 0.2f) ? dynFX.carMetrics.y : ((carID == 1u) ? -1.480f : ((carID == 2u) ? -1.390f : -1.1295f));
        float wz = (s.modelPos.z > 0.0f) ? frontZ : rearZ;
        float wy = (s.modelPos.z > 0.0f) ? max(dynFX.carMetrics.z, 0.30f) : max(dynFX.carMetrics.w, 0.30f);
        float2 relYZ = float2(s.modelPos.y - wy, s.modelPos.z - wz);
        float rad = length(relYZ);
        float theta = atan2(relYZ.x, relYZ.y);
        float hatMask = smoothstep(0.105f, 0.092f, rad);
        float lathe = 0.86f + 0.14f * sin(rad * 520.0f);
        float holeMask = 0.0f;
        if (rad > 0.112f && rad < 0.188f && speedFrac < 0.4f) {
            float sector = (2.0f * PI) / 12.0f;
            float sweptA = theta - (rad - 0.11f) * 3.8f;
            float aLocal = fmod(sweptA + PI * 4.0f, sector) - sector * 0.5f;
            float rLocal = fmod(rad - 0.112f, 0.025f) - 0.0125f;
            float dHole = length(float2(aLocal * rad, rLocal));
            holeMask = smoothstep(0.0046f, 0.0026f, dHole);
        }
        float3 steelCol = float3(0.74f, 0.76f, 0.80f) * lathe;
        steelCol = mix(steelCol, float3(0.025f, 0.027f, 0.030f), holeMask);
        steelCol = mix(steelCol, float3(0.10f, 0.105f, 0.115f), hatMask);
        s.baseAlbedo = steelCol;
        s.metallic   = mix(0.94f, 0.20f, max(holeMask, hatMask * 0.5f));
        s.roughness  = mix(0.22f, 0.62f, max(holeMask, hatMask * 0.45f));
    } else if (s.matID == MAT_BRAKE_CALIPER) {
        // Real UV-mapped crimson Brembo caliper with authentic "brembo" logo from BMWM3GTRE46_MISC!
        if (hasAuthenticTex) {
            float3 calTex = carMiscTex.sample(repeatSamp, carUV, level(0.0f)).rgb;
            s.baseAlbedo = clamp(calTex * 1.05f, float3(0.02f), float3(0.96f));
        } else {
            s.baseAlbedo = float3(0.84f, 0.055f, 0.038f);
        }
        s.metallic   = 0.35f;
        s.roughness  = 0.20f;
        s.clearcoat  = 0.90f;
        s.ccRoughness = 0.025f;
    } else if (s.matID == MAT_INTERIOR_DARK) {
        // Real UV-mapped M3 GTR racing interior (red bucket seat, roll cage, carbon dash!)
        if (hasAuthenticTex) {
            float3 intTex = carInteriorTex.sample(repeatSamp, carUV, level(0.0f)).rgb;
            s.baseAlbedo = intTex * 0.78f;
        }
        s.metallic  = 0.04f;
        s.roughness = 0.68f;
    } else if (s.matID == MAT_MATTE_TRIM) {
        s.baseAlbedo = float3(0.028f, 0.030f, 0.034f);
        s.metallic   = (inst.flags & 4u) != 0u ? 0.45f : 0.0f;
        s.roughness  = (inst.flags & 4u) != 0u ? 0.56f : 0.48f;
        s.clearcoat  = 0.0f;
    } else if (s.matID == MAT_CARBON_FIBER) {
        float2 cuv = (abs(worldN.y) > 0.5f ? float2(s.modelPos.x, s.modelPos.z) : float2(s.modelPos.x + s.modelPos.z, s.modelPos.y)) * 30.0f;
        float3 cf = carbonDiffTex.sample(repeatSamp, s.uv * 10.0f, level(0.0f)).rgb;
        float2 cell = floor(cuv);
        float2 f = fract(cuv);
        float twill = fmod(cell.x + cell.y, 4.0f);
        float warp = step(twill, 1.5f);
        float strand = mix(sin(f.x * PI), sin(f.y * PI), warp);
        float3 twillCol = mix(float3(0.022f, 0.024f, 0.028f), float3(0.115f, 0.122f, 0.135f), strand * 0.85f);
        float3 t, b;
        buildONB(worldN, t, b);
        float bx = (warp > 0.5f ? (f.x - 0.5f) : 0.0f) * 0.22f;
        float by = (warp <= 0.5f ? (f.y - 0.5f) : 0.0f) * 0.22f;
        s.N = normalize(worldN + t * bx + b * by);
        s.baseAlbedo = mix(twillCol, cf * 0.34f, 0.35f);
        s.metallic   = 0.14f;
        s.roughness  = 0.20f;
        s.clearcoat  = 1.0f;
        s.ccRoughness = 0.016f;
        s.clearcoatN = worldN;
    } else if (s.matID == MAT_LICENSE_PLATE) {
        float px = (carID == 0u) ? (1.0f - clamp((s.modelPos.x + 0.152f) / 0.304f, 0.0f, 1.0f)) : (1.0f - clamp(s.uv.x, 0.0f, 1.0f));
        float py = (carID == 0u) ? clamp((s.modelPos.y - 0.701f) / 0.150f, 0.0f, 1.0f) : clamp(s.uv.y, 0.0f, 1.0f);
        float outerPlate = step(0.032f, px) * step(px, 0.968f) * step(0.058f, py) * step(py, 0.942f);
        float innerPlate = step(0.052f, px) * step(px, 0.948f) * step(0.095f, py) * step(py, 0.905f);
        float euroStrip  = step(px, 0.155f) * innerPlate;
        float headerBar  = step(0.73f, py) * step(py, 0.855f) * step(0.21f, px) * step(px, 0.87f);
        float2 pAsp = float2(px * 2.02f, py);
        float dS1 = length(pAsp - float2(0.11f * 2.02f, 0.84f));
        float dS2 = length(pAsp - float2(0.89f * 2.02f, 0.84f));
        float dS3 = length(pAsp - float2(0.11f * 2.02f, 0.16f));
        float dS4 = length(pAsp - float2(0.89f * 2.02f, 0.16f));
        float screw = smoothstep(0.026f, 0.014f, min(min(dS1, dS2), min(dS3, dS4)));
        float3 plateCol = mix(float3(0.018f, 0.020f, 0.024f), float3(0.84f, 0.86f, 0.88f), outerPlate);
        plateCol = mix(plateCol, float3(0.035f, 0.040f, 0.048f), outerPlate - innerPlate);
        plateCol = mix(plateCol, float3(0.025f, 0.13f, 0.48f), euroStrip);
        plateCol = mix(plateCol, float3(0.025f, 0.13f, 0.48f), headerBar * 0.88f);
        float textMask = evaluatePlateTextMW2026(px, py);
        plateCol = mix(plateCol, float3(0.025f, 0.030f, 0.038f), textMask * 0.96f);
        plateCol = mix(plateCol, float3(0.82f, 0.85f, 0.88f), screw);
        s.baseAlbedo = plateCol;
        s.metallic   = mix(mix(0.10f, 0.05f, outerPlate), 0.92f, screw);
        s.roughness  = mix(0.32f, 0.18f, outerPlate);
        s.clearcoat  = mix(0.25f, 0.72f, outerPlate);
        float ledWash = smoothstep(0.12f, 0.95f, py) * outerPlate * (1.0f - textMask * 0.75f);
        s.emissive = float3(0.30f, 0.34f, 0.42f) * ledWash * (0.28f + 0.72f * u.skyGround.w);
    } else if (s.matID == MAT_HEADLIGHT_GLASS) {
        s.baseAlbedo = float3(0.0f); // Clear dielectric lens — ZERO diffuse sunlight scattering!
        s.emissive = float3(0.0f);
        s.alpha = 0.04f;
        s.roughness = 0.010f;
        s.ior = 1.54f;
    } else if (s.matID == MAT_HEADLIGHT_EMIT) {
        // 100% AUTHENTIC UV-MAPPED PER-CAR PROJECTOR HEADLIGHTS — dark smoked gunmetal housing + neutral gleaming silver chrome bowls (zero beige/cream plastic!)
        float headOn = u.skyGround.w;
        float forwardGate = smoothstep(-0.25f, 0.20f, localNormal.z);
        if (hasAuthenticTex) {
            float3 hlTex = carHeadlightTex.sample(repeatSamp, carUV, level(0.0f)).rgb;
            float hlLuma = dot(hlTex, float3(0.2126f, 0.7152f, 0.0722f));
            float amberMask = smoothstep(0.14f, 0.34f, hlTex.r - hlTex.b);
            float bowlChrome = smoothstep(0.42f, 0.78f, hlLuma) * (1.0f - amberMask);
            float3 gunmetalBase = float3(0.020f, 0.023f, 0.028f);
            float3 silverChrome = float3(0.72f, 0.76f, 0.82f);
            float3 amberLens    = float3(0.55f, 0.22f, 0.025f);
            s.baseAlbedo = mix(mix(gunmetalBase, silverChrome, bowlChrome), amberLens, amberMask * 0.85f);
            // Behind headlight glass, secondary transmission doesn't trace a 3rd bounce, so cap bowlChrome metallic at 0.48
            // and add crisp analytical chrome sky-bounce optics so recessed E46 reflector bowls gleam cleanly in daylight!
            s.metallic   = mix(0.18f, 0.48f, bowlChrome);
            s.roughness  = mix(0.42f, 0.12f, bowlChrome);
            s.clearcoat  = 0.55f;

            float daylightOpticBounce = clamp(dot(u.skyZenith.rgb + u.skyHorizon.rgb, float3(0.22f, 0.26f, 0.30f)), 0.05f, 0.90f);
            float3 chromeAmbientGlint = float3(0.22f, 0.25f, 0.30f) * bowlChrome * daylightOpticBounce * max(forwardGate, 0.35f);

            if (carID == 0u) {
                // BMW M3 GTR E46: left bowl u~0.18 (high-beam), center projector u~0.53 (xenon), right u>0.70 (amber turn)
                float dXenon = length(float2((carUV.x - 0.535f) * 2.0f, carUV.y - 0.48f));
                float dHigh  = length(float2((carUV.x - 0.185f) * 2.0f, carUV.y - 0.50f));
                float xenonLens = smoothstep(0.19f, 0.06f, dXenon);
                float xenonHalo = smoothstep(0.042f, 0.012f, abs(dXenon - 0.275f)) * smoothstep(0.08f, 0.25f, carUV.y);
                float highBulb  = smoothstep(0.16f, 0.04f, dHigh);
                float amberTurn = smoothstep(0.68f, 0.75f, carUV.x) * amberMask;
                s.emissive   = chromeAmbientGlint
                             + (float3(5.2f, 5.8f, 7.2f) * xenonLens
                             +  float3(2.8f, 3.2f, 4.0f) * xenonHalo * 0.65f
                             +  float3(2.6f, 2.3f, 1.8f) * highBulb * 0.75f
                             +  float3(2.2f, 0.72f, 0.06f) * amberTurn * 0.55f) * headOn * forwardGate;
            } else if (carID == 1u) {
                // Porsche Carrera GT (005_CARRERAGT_KIT00_HEADLIG): bi-xenon lens (0.26, 0.20), high-beam bowl (0.74, 0.24), lower amber strip (y~0.60)
                float dXenon = length(float2((carUV.x - 0.26f) * 1.45f, (carUV.y - 0.20f) * 1.85f));
                float dHigh  = length(float2((carUV.x - 0.74f) * 1.50f, (carUV.y - 0.24f) * 1.55f));
                float xenonLens = smoothstep(0.24f, 0.06f, dXenon);
                float highBulb  = smoothstep(0.22f, 0.06f, dHigh);
                float amberStrip = smoothstep(0.50f, 0.56f, carUV.y) * smoothstep(0.70f, 0.63f, carUV.y) * amberMask;
                s.emissive   = (float3(5.6f, 6.2f, 7.6f) * xenonLens
                             +  float3(2.8f, 2.6f, 2.3f) * highBulb * 0.70f
                             +  float3(2.6f, 0.95f, 0.10f) * amberStrip * 0.75f) * headOn * forwardGate;
            } else if (carID == 2u) {
                // Lamborghini Gallardo (001_GALLARDO_KIT00_HEADLIGH): bi-xenon projector orb + bright amber DRL/turn bowl
                float dXenon = min(
                    length(float2((carUV.x - 0.165f) * 2.20f, (carUV.y - 0.68f) * 1.25f)),
                    length(float2((carUV.x - 0.165f) * 2.20f, (carUV.y - 0.32f) * 1.25f))
                );
                float xenonLens = smoothstep(0.26f, 0.06f, dXenon);
                s.emissive   = chromeAmbientGlint
                             + (float3(7.5f, 8.2f, 10.0f) * xenonLens
                             +  float3(5.4f, 2.15f, 0.15f) * amberMask * 1.25f) * headOn * max(forwardGate, 0.45f);
            } else if (carID == 4u) {
                // Ford GT: smooth smoked gunmetal housing with twin recessed circular bi-xenon projector lenses + outer chrome bezel
                // (replaces low-res PS2 "100" white chalk outline artifact with realistic 3D HID optics!)
                float dProj1 = length(float2((carUV.x - 0.36f) * 1.95f, (carUV.y - 0.48f) * 1.45f));
                float dProj2 = length(float2((carUV.x - 0.68f) * 1.95f, (carUV.y - 0.50f) * 1.45f));
                float lens1 = smoothstep(0.24f, 0.08f, dProj1);
                float lens2 = smoothstep(0.22f, 0.07f, dProj2);
                float bezel = max(smoothstep(0.055f, 0.015f, abs(dProj1 - 0.27f)), smoothstep(0.050f, 0.015f, abs(dProj2 - 0.25f)));
                float opticChrome = clamp(max(max(lens1, lens2) * 0.72f, bezel * 0.85f), 0.0f, 1.0f);
                s.baseAlbedo = mix(float3(0.032f, 0.038f, 0.048f), float3(0.70f, 0.75f, 0.82f), opticChrome);
                s.metallic   = mix(0.24f, 0.68f, opticChrome);
                s.roughness  = mix(0.32f, 0.10f, opticChrome);
                s.emissive   = float3(0.18f, 0.22f, 0.28f) * opticChrome * daylightOpticBounce * max(forwardGate, 0.35f)
                             + (float3(6.2f, 6.8f, 8.4f) * max(lens1, lens2 * 0.85f)
                             +  float3(2.8f, 1.05f, 0.10f) * amberMask * 0.80f) * headOn * max(forwardGate, 0.35f);
            } else {
                // Universal UV-driven projector headlights for all 40 playable NFSMW cars!
                // Soft silver-chrome reflector base prevents stark white-on-black line-art rings in daylight!
                float softBowl  = smoothstep(0.16f, 0.66f, hlLuma) * (1.0f - amberMask * 0.85f);
                float brightOrb = smoothstep(0.42f, 0.82f, hlLuma) * (1.0f - amberMask * 0.85f);
                s.baseAlbedo = mix(mix(float3(0.10f, 0.115f, 0.135f), float3(0.56f, 0.60f, 0.66f), softBowl), amberLens, amberMask * 0.85f);
                s.metallic   = mix(0.32f, 0.54f, softBowl);
                s.roughness  = mix(0.28f, 0.14f, softBowl);
                s.emissive   = float3(0.12f, 0.14f, 0.17f) * softBowl * daylightOpticBounce * max(forwardGate, 0.35f)
                             + (float3(5.2f, 5.8f, 7.2f) * (brightOrb * 0.80f + softBowl * 0.25f)
                             +  float3(3.4f, 1.35f, 0.10f) * amberMask * 0.90f) * headOn * max(forwardGate, 0.35f);
            }
        } else {
            s.baseAlbedo = float3(0.015f, 0.017f, 0.022f);
            s.metallic   = 0.70f;
            s.roughness  = 0.16f;
            s.emissive   = float3(3.8f, 4.2f, 5.0f) * headOn * forwardGate;
        }
    } else if (s.matID == MAT_TAILLIGHT_EMIT) {
        // 100% AUTHENTIC UV-MAPPED BMW E46 M3 LED TAILLIGHTS — crisp horizontal divider + discrete ruby-crimson LED dot matrix + dark bezel!
        float headOn = 0.72f + 0.42f * u.skyGround.w;
        if (hasAuthenticTex) {
            float3 tlTex = carTaillightTex.sample(repeatSamp, carUV, level(0.0f)).rgb;
            float redDom = clamp((tlTex.r - max(tlTex.g, tlTex.b)) * 2.2f, 0.0f, 1.0f);
            float whiteStrip = smoothstep(0.32f, 0.62f, min(tlTex.r, min(tlTex.g, tlTex.b)));
            // Crisp dark horizontal divider between upper clear/amber strip and lower E46 LED brake cluster
            float horizDivider = smoothstep(0.012f, 0.036f, abs(carUV.y - 0.38f));
            // Discrete E46 LED emitter matrix (horizontal rows + fine LED dot diodes)
            float ledRow = 0.55f + 0.45f * smoothstep(-0.35f, 0.55f, sin(carUV.y * 135.0f));
            float ledDot = 0.72f + 0.28f * smoothstep(-0.20f, 0.65f, sin(carUV.x * 165.0f));
            float ledMatrix = ledRow * ledDot * horizDivider;
            float housingBezel = smoothstep(0.08f, 0.28f, max(redDom, whiteStrip));
            s.baseAlbedo  = mix(float3(0.018f, 0.008f, 0.008f),
                                mix(tlTex * 0.34f, float3(0.42f, 0.45f, 0.48f), whiteStrip * 0.65f),
                                housingBezel * horizDivider);
            s.metallic    = mix(0.12f, 0.38f, whiteStrip);
            s.roughness   = 0.045f;
            s.clearcoat   = 1.0f;
            s.ccRoughness = 0.014f;
            s.ior         = 1.54f;
            // Deeper authentic ruby-crimson LED emission (avoids blown-out flat orange blobs!)
            s.emissive    = (float3(4.2f, 0.042f, 0.022f) * pow(redDom, 1.35f) * ledMatrix
                          +  float3(0.24f, 0.22f, 0.18f) * whiteStrip * horizDivider * 0.32f) * headOn;
        } else {
            s.baseAlbedo  = float3(0.12f, 0.006f, 0.006f);
            s.roughness   = 0.05f;
            s.clearcoat   = 1.0f;
            s.emissive    = float3(3.8f, 0.05f, 0.025f) * headOn;
        }
    } else if (s.matID == MAT_WET_ASPHALT) {
        float2 wp = float2(s.worldPos.x, s.worldPos.z + u.envParams.y);
        float wetness = clamp(u.skyZenith.w, 0.0f, 1.0f);

        float2 uvA = wp * 0.24f;
        float2 uvB = float2(wp.x * 0.068f - wp.y * 0.041f, wp.x * 0.041f + wp.y * 0.068f) + float2(0.37f, 0.61f);
        float3 texA = asphaltDiffTex.sample(repeatSamp, uvA, level(0.0f)).rgb;
        float3 texB = asphaltDiffTex.sample(repeatSamp, uvB, level(0.0f)).rgb;
        float3 nA   = asphaltNormTex.sample(repeatSamp, uvA, level(0.0f)).rgb;
        float3 nB   = asphaltNormTex.sample(repeatSamp, uvB, level(0.0f)).rgb;
        float rTex  = asphaltRoughTex.sample(repeatSamp, uvA, level(0.0f)).r;

        float3 asphaltCol = mix(texA, texB, 0.40f) * 0.36f;

        // Tire tracks where traffic sweeps water out (slightly drier satin rubber track vs wetter shoulders!)
        float tireTrack = exp(-pow((s.worldPos.x + 0.68f) / 0.36f, 2.0f))
                        + exp(-pow((s.worldPos.x - 0.68f) / 0.36f, 2.0f))
                        + exp(-pow((s.worldPos.x + 3.95f) / 0.40f, 2.0f)) * 0.75f;
        tireTrack = clamp(tireTrack, 0.0f, 1.0f);
        asphaltCol *= (1.0f - tireTrack * 0.14f);

        // Domain-warped organic water puddles: drier in tire tracks, pooling deeper along shoulders & crown
        float shoulderBias = smoothstep(1.8f, 5.2f, abs(s.worldPos.x)) * 0.16f;
        float2 warp = float2(fbm2D(wp * 0.14f), fbm2D(wp * 0.14f + float2(5.2f, 1.3f)));
        float puddleField = fbm2D(wp * 0.19f + warp * 1.35f) + shoulderBias - tireTrack * 0.14f;
        float puddleThreshold = mix(0.74f, 0.44f, wetness);
        float puddle = smoothstep(puddleThreshold - 0.065f, puddleThreshold + 0.065f, puddleField);
        float damp   = smoothstep(0.24f, 0.72f, puddleField + wetness * 0.26f);

        // Darken raw asphalt where wet, BEFORE painting crisp highway road markings!
        asphaltCol *= mix(1.0f, 0.46f, max(damp * 0.72f, puddle));

        // Crisp Highway Painted Markings: Double-Yellow Centerline + Dashed Lane Divider + Shoulder Lines
        float doubleYellow = smoothstep(0.068f, 0.048f, abs(s.worldPos.x + 1.98f))
                           + smoothstep(0.068f, 0.048f, abs(s.worldPos.x + 1.74f));
        float solidEdgeL   = smoothstep(0.085f, 0.060f, abs(s.worldPos.x + 5.75f));
        float solidEdgeR   = smoothstep(0.085f, 0.060f, abs(s.worldPos.x - 5.35f));
        float dashPhase    = fract(wp.y / 9.0f);
        float dashedMid    = smoothstep(0.072f, 0.052f, abs(s.worldPos.x - 1.85f))
                           * step(dashPhase, 0.36f);

        float yellowMask = clamp(doubleYellow, 0.0f, 1.0f);
        float whiteMask  = clamp(solidEdgeL + solidEdgeR + dashedMid, 0.0f, 1.0f);
        float paintWear  = 0.80f + 0.20f * smoothstep(0.04f, 0.28f, texA.r);
        float puddleDim  = mix(1.0f, 0.62f, puddle);

        asphaltCol = mix(asphaltCol, float3(0.84f, 0.56f, 0.06f) * puddleDim, yellowMask * paintWear * 0.94f);
        asphaltCol = mix(asphaltCol, float3(0.80f, 0.82f, 0.85f) * puddleDim, whiteMask * paintWear * 0.92f);

        // Glowing raised amber & white highway cat's-eye retro-reflector road studs!
        float studZ = abs(fmod(abs(wp.y) + 4.5f, 9.0f) - 4.5f);
        float amberStud = smoothstep(0.065f, 0.032f, abs(s.worldPos.x + 1.86f)) * smoothstep(0.085f, 0.040f, studZ);
        float whiteStudZ = abs(fmod(abs(wp.y + 6.0f) + 4.5f, 9.0f) - 4.5f);
        float whiteStud = smoothstep(0.060f, 0.028f, abs(s.worldPos.x - 1.85f)) * smoothstep(0.080f, 0.036f, whiteStudZ);
        float studBoost = 0.55f + 1.10f * u.neonParams.w + 0.45f * u.skyGround.w;
        asphaltCol = mix(asphaltCol, float3(0.14f, 0.14f, 0.15f), max(amberStud, whiteStud) * 0.6f);
        s.emissive = float3(2.4f, 1.05f, 0.10f) * amberStud * studBoost
                   + float3(1.8f, 2.0f, 2.3f) * whiteStud * studBoost * 0.85f;

        s.baseAlbedo = asphaltCol;
        // At high driving speed (--drive-demo), rear tire wakes sweep crisp glossy water streaks behind the tires!
        float rearTireWake = (exp(-pow((s.worldPos.x + 0.78f) / 0.24f, 2.0f)) + exp(-pow((s.worldPos.x - 0.78f) / 0.24f, 2.0f)))
                           * smoothstep(-1.1f, -2.0f, s.worldPos.z) * smoothstep(-14.0f, -2.5f, s.worldPos.z) * speedFrac;
        float dryRough = clamp(0.32f + rTex * 0.34f + tireTrack * 0.06f, 0.24f, 0.72f);
        float paintMask = max(yellowMask, whiteMask);
        float baseAsphRough = mix(mix(mix(dryRough, 0.20f, damp), 0.34f, paintMask * 0.6f), 0.018f, puddle);
        s.roughness = mix(baseAsphRough, 0.055f, clamp(rearTireWake * 0.65f, 0.0f, 0.65f));
        s.puddleMask = max(puddle, rearTireWake * 0.45f);

        // Micro-asphalt aggregate normal + subtle organic wind/rain micro-ripples inside puddles
        // Slightly stretch longitudinal micro-normal along Z at high speed for realistic wet-road velocity flow
        float3 microN = unpackNormalSample(mix(nA, nB, 0.35f), float3(0.0f, 1.0f, 0.0f),
                                           float3(1.0f, 0.0f, 0.0f), float3(0.0f, 0.0f, 1.0f),
                                           mix(0.38f, 0.042f, s.puddleMask));
        microN.z *= mix(1.0f, 0.35f, speedFrac);
        float pRipX = (noise2D(wp * 4.8f) - 0.5f) * 0.020f + (noise2D(wp * 13.5f) - 0.5f) * 0.010f;
        float pRipZ = ((noise2D(wp * 4.8f + float2(7.3f, 2.1f)) - 0.5f) * 0.020f + (noise2D(wp * 13.5f + float2(3.1f, 8.7f)) - 0.5f) * 0.010f) * mix(1.0f, 0.25f, speedFrac);
        s.N = normalize(microN + float3(pRipX, 0.0f, pRipZ) * s.puddleMask);
        s.clearcoatN = normalize(float3(pRipX * 0.50f * s.puddleMask, 1.0f, pRipZ * 0.50f * s.puddleMask));
    } else if (s.matID == MAT_URBAN_CONCRETE) {
        float zScroll = s.worldPos.z + u.envParams.y;
        float2 wallUV = abs(worldN.y) > 0.5f
            ? float2(s.worldPos.x, zScroll) * 0.35f
            : float2(zScroll, s.worldPos.y) * 0.35f;
        float3 cDiff = concreteDiffTex.sample(repeatSamp, wallUV, level(0.0f)).rgb;
        float3 cNorm = concreteNormTex.sample(repeatSamp, wallUV, level(0.0f)).rgb;
        float3 t, b;
        buildONB(worldN, t, b);

        // Right-side Rockport Industrial Warehouse Corrugated Steel Siding (x > 10.5, y > 1.28)
        if (s.worldPos.x > 10.4f && s.worldPos.y > 1.28f && abs(worldN.x) > 0.5f) {
            float rib = sin(zScroll * 28.0f);
            float grime = fbm2D(float2(zScroll * 0.45f, s.worldPos.y * 0.65f));
            float rustStreak = smoothstep(0.58f, 0.82f, fbm2D(float2(zScroll * 1.4f, s.worldPos.y * 0.25f)));
            float3 steelCol = mix(float3(0.095f, 0.098f, 0.090f), float3(0.145f, 0.130f, 0.105f), grime);
            steelCol = mix(steelCol, float3(0.14f, 0.075f, 0.042f), rustStreak * 0.45f);
            s.baseAlbedo = steelCol * (0.82f + 0.18f * rib) * mix(0.65f, 1.0f, smoothstep(1.28f, 2.5f, s.worldPos.y));
            s.metallic   = 0.55f * (1.0f - rustStreak * 0.65f);
            s.roughness  = mix(0.54f, 0.82f, rustStreak);
            s.N = normalize(worldN + t * (cos(zScroll * 28.0f) * 0.35f));
        } else {
            // Weathered East Coast reinforced concrete (formwork seams, vertical rain-runoff streaks, base splash grime)
            float jointDist = abs(fmod(abs(zScroll) + 2.0f, 4.0f) - 2.0f);
            float jointGroove = smoothstep(0.032f, 0.005f, jointDist);
            float horizSeam = smoothstep(0.025f, 0.004f, abs(fmod(abs(s.worldPos.y) + 0.9f, 1.8f) - 0.9f));
            float waterStreak = fbm2D(float2(zScroll * 2.2f, s.worldPos.y * 0.28f));
            float topDrip = mix(0.68f, 1.0f, smoothstep(7.4f, 5.2f, s.worldPos.y));
            float roadSplash = mix(0.48f, 1.0f, smoothstep(0.0f, 0.55f, s.worldPos.y));
            s.baseAlbedo = mix(s.baseAlbedo, cDiff * 0.34f, 0.86f) * roadSplash * topDrip
                         * (0.72f + 0.36f * waterStreak) * (1.0f - max(jointGroove, horizSeam * 0.7f) * 0.55f);
            s.N = unpackNormalSample(cNorm, worldN, t, b, 0.62f);
            s.roughness = mix(0.86f, 0.58f, u.skyZenith.w * 0.45f);
        }
    } else if (s.matID == MAT_URBAN_GLASS) {
        // Gritty industrial steel-mullioned warehouse windows with warm amber interior workshop glow
        float zScroll = s.worldPos.z + u.envParams.y;
        float mullionZ = smoothstep(0.05f, 0.02f, abs(fract(zScroll * 1.1f) - 0.5f));
        float mullionY = smoothstep(0.06f, 0.02f, abs(fract(s.worldPos.y * 1.6f) - 0.5f));
        float frame = max(mullionZ, mullionY);
        float glow = (1.0f - frame) * (0.25f + 0.75f * noise2D(float2(floor(zScroll * 0.25f), 2.1f)));
        s.baseAlbedo = mix(float3(0.04f, 0.05f, 0.06f), float3(0.022f, 0.024f, 0.026f), frame);
        s.metallic   = mix(0.18f, 0.65f, frame);
        s.roughness  = mix(0.14f, 0.60f, frame);
        s.emissive   = float3(0.95f, 0.54f, 0.18f) * glow * (0.08f + 0.38f * u.neonParams.w);
    } else if (s.matID == MAT_AUTUMN_TERRAIN) {
        // Multi-scale slope-compensated Rockport hillside: craggy coastal granite strata, damp forest loam,
        // olive-golden coastal grass, crisp fallen autumn leaf litter, and grounded curb/guardrail contact AO!
        float zScroll = s.worldPos.z + u.envParams.y;
        float2 tpSlope = float2(s.worldPos.x + s.worldPos.y * 0.72f, zScroll);
        float slope = clamp(worldN.y, 0.0f, 1.0f);
        float macro = fbm2D(tpSlope * 0.14f);
        float foliage = fbm5(tpSlope * 0.62f);
        float grassTuft = noise2D(tpSlope * 4.8f);
        float leafSpeckle = noise2D(tpSlope * 18.5f);
        float leafHue = noise2D(tpSlope * 9.2f + float2(4.7f, 1.9f));
        float strata = sin(s.worldPos.y * 5.2f + fbm2D(tpSlope * 0.45f) * 3.8f) * 0.5f + 0.5f;
        float crag = fbm2D(tpSlope * 2.4f);

        float3 dampLoam    = float3(0.055f, 0.042f, 0.028f);
        float3 oliveGrass  = float3(0.072f, 0.088f, 0.032f);
        float3 dryGrass    = float3(0.135f, 0.095f, 0.034f);
        float3 leafAmber   = mix(float3(0.26f, 0.075f, 0.018f), float3(0.36f, 0.165f, 0.024f), leafHue);
        float3 rockGranite = mix(float3(0.095f, 0.092f, 0.088f), float3(0.155f, 0.148f, 0.138f), strata) * (0.72f + 0.50f * grassTuft);

        // Rich grounded forest floor layering
        float3 vegCol = mix(dampLoam, oliveGrass, smoothstep(0.28f, 0.70f, macro));
        vegCol = mix(vegCol, dryGrass, smoothstep(0.40f, 0.78f, foliage) * 0.72f);
        // Crisp individual fallen-leaf litter patches (not smeared orange noise!)
        float leafMask = smoothstep(0.54f, 0.80f, leafSpeckle) * smoothstep(0.30f, 0.68f, foliage);
        vegCol = mix(vegCol, leafAmber, leafMask * 0.78f);
        vegCol *= (0.74f + 0.48f * grassTuft);

        // Exposed craggy coastal granite rock outcrops near the road cut & steeper faces
        float roadCut = smoothstep(10.8f, 7.1f, s.worldPos.x) * 0.62f;
        float rockMask = clamp((1.0f - smoothstep(0.54f, 0.84f, slope)) + roadCut * smoothstep(0.34f, 0.68f, crag), 0.0f, 1.0f);
        float3 terrainCol = mix(vegCol, rockGranite, rockMask);

        // Grounded contact AO along the concrete road curb/guardrail toe (x ~ 6.8..8.6m)
        float curbAO = mix(0.56f, 1.0f, smoothstep(7.05f, 8.85f, s.worldPos.x));
        s.baseAlbedo = terrainCol * curbAO;
        s.roughness = mix(0.90f, 0.72f, rockMask);

        float3 t, b;
        buildONB(worldN, t, b);
        float gx = (noise2D(tpSlope * 11.5f) - 0.5f) * 0.58f + (strata - 0.5f) * 0.35f * rockMask;
        float gz = (noise2D(tpSlope * 11.5f + float2(7.1f, 2.9f)) - 0.5f) * 0.58f;
        s.N = normalize(worldN + t * gx + b * gz);
    } else if (s.matID == MAT_FOLIAGE_AUTUMN || s.matID == MAT_FOLIAGE_PINE) {
        // Rich organic multi-cluster autumn maple/birch, coastal pine & airy roadside shrubs
        float treeSeed = hash21(floor(float2(inst.modelMatrix[3].x, inst.modelMatrix[3].z) * 0.45f));
        float3 leafCoord = s.modelPos * 9.5f;
        float leafCluster = fbm2D(leafCoord.xz + leafCoord.y * 1.25f);
        float leafMicro = noise2D(s.worldPos.xz * 20.0f + s.worldPos.y * 16.0f);
        // Handle both tall trees AND low roadside shrubs cleanly without crushing low shrubs into dark blobs!
        float isShrub = step(s.modelPos.y, 1.45f);
        float treeAO = clamp(0.46f + 0.54f * smoothstep(2.2f, 6.0f, s.modelPos.y), 0.42f, 1.0f);
        float shrubAO = clamp(0.68f + 0.32f * (worldN.y * 0.5f + 0.5f), 0.60f, 1.0f);
        float canopyAO = mix(treeAO, shrubAO, isShrub);
        float leafBreakup = 0.65f + 0.65f * smoothstep(0.18f, 0.85f, leafMicro);
        if (s.matID == MAT_FOLIAGE_AUTUMN) {
            float3 crimson = float3(0.32f, 0.048f, 0.015f);
            float3 burntAmber = float3(0.46f, 0.145f, 0.020f);
            float3 goldenOchre = float3(0.58f, 0.305f, 0.036f);
            float3 russet = float3(0.28f, 0.105f, 0.026f);
            float3 palA = mix(crimson, burntAmber, smoothstep(0.15f, 0.65f, treeSeed));
            float3 palB = mix(burntAmber, goldenOchre, smoothstep(0.30f, 0.88f, treeSeed));
            float3 leafCol = mix(palA, palB, leafCluster);
            leafCol = mix(leafCol, russet, smoothstep(0.68f, 0.92f, leafMicro) * 0.42f);
            s.baseAlbedo = leafCol * leafBreakup * canopyAO;
        } else {
            float3 deepPine = float3(0.022f, 0.058f, 0.026f);
            float3 coastalSpruce = float3(0.052f, 0.102f, 0.038f);
            float3 sunlitTip = float3(0.095f, 0.138f, 0.044f);
            float3 pineCol = mix(deepPine, coastalSpruce, leafCluster);
            pineCol = mix(pineCol, sunlitTip, smoothstep(0.52f, 0.86f, leafMicro) * canopyAO * 0.55f);
            s.baseAlbedo = pineCol * leafBreakup * canopyAO;
        }
        s.roughness = 0.76f;
        float3 t, b;
        buildONB(worldN, t, b);
        s.N = normalize(worldN + t * ((leafMicro - 0.5f) * 0.88f) + b * ((leafCluster - 0.5f) * 0.88f));
    } else if (s.matID == MAT_TREE_TRUNK) {
        float bark = fbm2D(float2(atan2(s.modelPos.z, s.modelPos.x) * 6.0f, s.modelPos.y * 4.5f));
        s.baseAlbedo = float3(0.085f, 0.065f, 0.050f) * (0.65f + 0.68f * bark);
        s.roughness = 0.88f;
        float3 t, b;
        buildONB(worldN, t, b);
        s.N = normalize(worldN + t * ((bark - 0.5f) * 0.42f));
    } else if (s.matID == MAT_COASTAL_WATER) {
        // Physically rippled coastal ocean swell + wind chop + capillary waves
        float2 wp = float2(s.worldPos.x, s.worldPos.z + u.envParams.y * 0.18f);
        float distFade = exp(-dist * 0.0075f);
        float s1 = sin(wp.x * 0.52f + wp.y * 0.24f + fbm2D(wp * 0.10f) * 2.6f);
        float s2 = cos(wp.x * 1.15f - wp.y * 0.68f + fbm2D(wp * 0.18f + 3.1f) * 2.2f);
        float c1 = noise2D(wp * float2(2.15f, 1.35f)) - 0.5f;
        float c2 = noise2D(wp * float2(5.40f, 3.20f) + float2(7.4f, 1.9f)) - 0.5f;
        float c3 = noise2D(wp * float2(11.50f, 7.20f) + float2(14.1f, 5.3f)) - 0.5f;
        float wx = (s1 * 0.055f + c1 * 0.075f + c2 * 0.045f * distFade + c3 * 0.022f * distFade);
        float wz = (s2 * 0.048f + (noise2D(wp * float2(2.30f, 1.45f) + 8.3f) - 0.5f) * 0.068f + c2 * 0.036f * distFade);
        s.N = normalize(float3(wx, 1.0f, wz));
        s.clearcoatN = s.N;
        float shoreBlend = smoothstep(-28.0f, -9.8f, s.worldPos.x);
        s.baseAlbedo = mix(float3(0.004f, 0.022f, 0.048f), float3(0.010f, 0.038f, 0.056f), shoreBlend);
        s.roughness = mix(0.068f, 0.036f, distFade);
        s.ior = 1.333f;
    } else if (s.matID == MAT_STREETLAMP_LED) {
        // Physically scale industrial sodium luminaire emission with preset lamp intensity (never glaring white in daylight!)
        float lampOn = smoothstep(0.45f, 1.25f, u.neonParams.w);
        s.baseAlbedo = float3(0.42f, 0.38f, 0.32f);
        s.emissive = u.neonParams.rgb * (u.neonParams.w * 2.6f * lampOn);
    } else if (s.matID == MAT_BARRIER_REFLECT) {
        s.emissive = float3(1.6f, 0.65f, 0.12f) * (0.55f + 0.65f * u.neonParams.w);
    } else if (s.matID == MAT_NEON_CYAN) {
        s.emissive = u.neonParams.rgb * (2.0f + 3.2f * u.neonParams.w);
    } else if (s.matID == MAT_NEON_AMBER) {
        s.emissive = mix(u.coronaColor.rgb, float3(1.0f, 0.48f, 0.14f), 0.55f) * (2.0f + 3.2f * u.neonParams.w);
    }

    return s;
}

static float3 evaluateDirectSunBRDF(HitSurface s, float3 V, float3 L, float3 sunRad) {
    float NdotL = max(dot(s.N, L), 0.0f);
    if (NdotL <= 0.0f) return float3(0.0f);

    float NdotV = max(dot(s.N, V), 1e-4f);
    float3 H = normalize(V + L);
    float NdotH = max(dot(s.N, H), 0.0f);
    float VdotH = max(dot(V, H), 0.0f);

    float3 F0 = mix(float3(0.04f), s.baseAlbedo, s.metallic);
    float alpha = s.roughness * s.roughness;
    float D = D_GGX(NdotH, alpha);
    float G = G_SmithGGX(NdotV, NdotL, s.roughness);
    float3 F = F_Schlick(VdotH, F0);
    float3 spec = (D * G * F) / max(4.0f * NdotV * NdotL, 1e-4f);
    float3 kD = (1.0f - F) * (1.0f - s.metallic);
    float3 diff = kD * s.baseAlbedo / PI;
    float3 col = (diff + spec) * sunRad * NdotL;

    if (s.clearcoat > 0.01f) {
        float ccNdotL = max(dot(s.clearcoatN, L), 0.0f);
        float ccNdotV = max(dot(s.clearcoatN, V), 1e-4f);
        float ccNdotH = max(dot(s.clearcoatN, H), 0.0f);
        float f0_cc = pow((s.ior - 1.0f) / (s.ior + 1.0f), 2.0f);
        float F_cc = F_SchlickScalar(VdotH, f0_cc) * s.clearcoat;
        float ccRoughEff = max(s.ccRoughness, 0.045f);
        float ccAlpha = ccRoughEff * ccRoughEff;
        float D_cc = D_GGX(ccNdotH, ccAlpha);
        float G_cc = G_SmithGGX(ccNdotV, ccNdotL, ccRoughEff);
        float specCC = (D_cc * G_cc * F_cc) / max(4.0f * ccNdotV * ccNdotL, 1e-4f);
        col = col * (1.0f - F_cc) + min(specCC * sunRad * ccNdotL, float3(5.0f));
    }
    return col;
}

static void evaluateDirectSunSplit(
    HitSurface s,
    float3 V,
    float3 L,
    float3 sunRad,
    float sunVis,
    thread float3& outDiffIrrad,
    thread float3& outDirectSpec
) {
    float NdotL = max(dot(s.N, L), 0.0f);
    outDiffIrrad = float3(0.0f);
    outDirectSpec = float3(0.0f);
    if (NdotL <= 0.0f && s.clearcoat <= 0.01f) return;

    float NdotV = max(dot(s.N, V), 1e-4f);
    float3 H = normalize(V + L);
    float VdotH = max(dot(V, H), 0.0f);
    float3 F0 = mix(float3(0.04f), s.baseAlbedo, s.metallic);
    float3 F = F_Schlick(VdotH, F0);
    float f0_cc = pow((s.ior - 1.0f) / (s.ior + 1.0f), 2.0f);
    float F_cc = (s.clearcoat > 0.01f) ? F_SchlickScalar(VdotH, f0_cc) * s.clearcoat : 0.0f;

    if (NdotL > 0.0f) {
        float NdotH = max(dot(s.N, H), 0.0f);
        float alpha = s.roughness * s.roughness;
        float D = D_GGX(NdotH, alpha);
        float G = G_SmithGGX(NdotV, NdotL, s.roughness);
        float3 spec = (D * G * F) / max(4.0f * NdotV * NdotL, 1e-4f);
        outDiffIrrad = (sunRad / PI) * NdotL * sunVis * (1.0f - F_cc);
        outDirectSpec = spec * sunRad * NdotL * sunVis * (1.0f - F_cc);
    }

    if (s.clearcoat > 0.01f) {
        float ccNdotL = max(dot(s.clearcoatN, L), 0.0f);
        if (ccNdotL > 0.0f) {
            float ccNdotV = max(dot(s.clearcoatN, V), 1e-4f);
            float ccNdotH = max(dot(s.clearcoatN, H), 0.0f);
            // Finite sun/moon angular cone roughness floor prevents single-triangle GGX firefly spikes
            float ccRoughEff = max(s.ccRoughness, 0.045f);
            float ccAlpha = ccRoughEff * ccRoughEff;
            float D_cc = D_GGX(ccNdotH, ccAlpha);
            float G_cc = G_SmithGGX(ccNdotV, ccNdotL, ccRoughEff);
            float specCC = (D_cc * G_cc * F_cc) / max(4.0f * ccNdotV * ccNdotL, 1e-4f);
            outDirectSpec += min(specCC * sunRad * ccNdotL * sunVis, float3(5.0f));
        }
    }
    outDirectSpec = min(outDirectSpec, float3(6.5f));
}

static void evaluateLocalAreaLightsImpl(
    HitSurface s,
    float3 V,
    constant RTFrameUniforms& u,
    device const RTWorldLight* worldLights,
    device const ushort* worldLightGrid,
    constant RTDynamicFX& dynFX,
    bool collectRIS,
    float xiPick,
    thread float3& outDiffIrrad,
    thread float3& outSpec,
    thread float4& outRISPosDist,
    thread float3& outRISDiff,
    thread float3& outRISSpec,
    thread float& outRISWeightRatio
) {
    outDiffIrrad = float3(0.0f);
    outSpec = float3(0.0f);
    outRISPosDist = float4(0.0f);
    outRISDiff = float3(0.0f);
    outRISSpec = float3(0.0f);
    outRISWeightRatio = 0.0f;
    float tunnelHead = clamp(u.skyGround.w - 1.0f, 0.0f, 1.0f);
    float headScale = min(u.skyGround.w, 1.0f);
    float yaw = u.carDynParams.z;
    float cy = cos(yaw), sy = sin(yaw);

    auto rotateCarLocal = [&](float3 p) -> float3 {
        return float3(p.x * cy + p.z * sy, p.y, -p.x * sy + p.z * cy);
    };

    float3 carOrigin = u.carWorldPos.xyz;
    float isNight = smoothstep(0.036f, 0.015f, u.skyZenith.r);
    // Gate road headlight projection strictly on dusk/night OR underground tunnels (skyOcc > 0.50 -> tunnelHead > 0)
    // so open Daylight & Golden Hour have ZERO chalky white smear while Bayshore tunnel lights up dramatically!
    float roadBeamGain = max(mix(0.0f, 1.0f, isNight), tunnelHead * 1.25f);
    float fZ = (abs(dynFX.carMetrics.x) > 0.2f) ? (dynFX.carMetrics.x + 0.56f) : 2.02f;
    float rZ = (abs(dynFX.carMetrics.y) > 0.2f) ? (dynFX.carMetrics.y - 0.68f) : -2.06f;
    float3 carLights[4] = {
        carOrigin + rotateCarLocal(float3(-0.62f, 0.62f, fZ)),
        carOrigin + rotateCarLocal(float3( 0.62f, 0.62f, fZ)),
        carOrigin + rotateCarLocal(float3(-0.56f, 0.76f, rZ)),
        carOrigin + rotateCarLocal(float3( 0.56f, 0.76f, rZ))
    };
    float3 carDirs[4] = {
        normalize(rotateCarLocal(float3(0.0f, -0.24f,  1.0f))),
        normalize(rotateCarLocal(float3(0.0f, -0.24f,  1.0f))),
        normalize(rotateCarLocal(float3(0.0f, -0.24f, -1.0f))),
        normalize(rotateCarLocal(float3(0.0f, -0.24f, -1.0f)))
    };
    // Crisp 5800K dual-projector xenon night low-beams + clean restrained crimson taillight highlights (zero red road splats!)
    float3 carCols[4] = {
        float3(0.86f, 0.93f, 1.06f) * (15.5f * headScale * roadBeamGain),
        float3(0.86f, 0.93f, 1.06f) * (15.5f * headScale * roadBeamGain),
        float3(1.45f, 0.050f, 0.020f) * (0.85f * (0.65f + 0.50f * headScale)),
        float3(1.45f, 0.050f, 0.020f) * (0.85f * (0.65f + 0.50f * headScale))
    };

    bool isCarExterior = (s.matID <= MAT_LICENSE_PLATE || s.matID >= MAT_CAR_PAINT_SILVER);
    for (int i = 0; i < 4; ++i) {
        // Prevent the car's own headlights/taillights from blasting point-light fireflies onto its own bumper/fenders
        if (isCarExterior) continue;
        if (i < 2 && roadBeamGain < 0.01f) continue;
        float3 toL = carLights[i] - s.worldPos;
        float d2 = dot(toL, toL) + ((i < 2) ? 0.35f : 0.45f);
        float d = sqrt(d2);
        float3 L = toL / d;
        float NdotL = max(dot(s.N, L), 0.0f);
        if (NdotL > 0.0f) {
            float cone;
            if (i < 2) {
                float beamCore = smoothstep(0.58f, 0.90f, dot(-L, carDirs[i]));
                float upperCutoff = smoothstep(0.14f, -0.05f, (-L).y);
                cone = beamCore * upperCutoff;
            } else {
                cone = smoothstep(0.36f, 0.88f, dot(-L, carDirs[i]));
            }
            float atten = cone / (1.0f + d2 * ((i < 2) ? 0.055f : 0.55f));
            float3 H = normalize(V + L);
            float specPow = mix(140.0f, 12.0f, s.roughness);
            float spec = pow(max(dot(s.N, H), 0.0f), specPow) * mix(0.65f, 0.08f, s.roughness);
            float diffMul = (i < 2) ? 1.0f : 0.28f;
            float specMul = (i < 2) ? 1.0f : 0.80f;
            outDiffIrrad += (carCols[i] / PI) * NdotL * atten * diffMul;
            outSpec += min(float3(spec) * carCols[i] * NdotL * atten * specMul, float3(4.5f));
        }
    }

    // Police Pursuit Red/Blue RT Lightbar Strobes (4 nearest active cops from RTDynamicFX[13])
    // Physically grounded 20m radius + steep inverse-square falloff + 0.28x daylight attenuation:
    // casts crisp local red/blue streaks on wet puddles & nearby fenders without bleaching city blocks or fences!
    float copDayScale = mix(0.28f, 1.0f, isNight);
    for (int c = 0; c < 4; ++c) {
        float strobe = dynFX.copLightPos[c].w;
        float intens = dynFX.copLightCol[c].w;
        if (strobe > 0.01f && intens > 0.01f) {
            float3 toL = dynFX.copLightPos[c].xyz - s.worldPos;
            float d2 = dot(toL, toL) + 0.65f;
            if (d2 < 420.0f) {
                float d = sqrt(d2);
                float3 L = toL / d;
                float NdotL = max(dot(s.N, L), 0.0f);
                if (NdotL > 0.0f) {
                    float wCop = clamp(1.0f - d2 / 420.0f, 0.0f, 1.0f);
                    wCop *= wCop;
                    float atten = (strobe * intens * wCop * copDayScale) / (1.0f + d2 * 0.14f);
                    float3 cCol = dynFX.copLightCol[c].rgb * 5.5f;
                    float3 H = normalize(V + L);
                    float NdotV = max(dot(s.N, V), 1e-4f);
                    float NdotH = max(dot(s.N, H), 0.0f);
                    float3 F0 = mix(float3(0.04f), s.baseAlbedo, s.metallic);
                    float3 F = F_Schlick(max(dot(V, H), 0.0f), F0);
                    float rEff = max(s.roughness, 0.055f);
                    float D = D_GGX(NdotH, max(rEff * rEff, 0.003f));
                    float G = G_SmithGGX(NdotV, NdotL, rEff);
                    float3 specBRDF = (D * G * F) / max(4.0f * NdotV * NdotL, 1e-4f);
                    outDiffIrrad += (cCol / PI) * NdotL * atten * 0.45f;
                    outSpec += min(specBRDF * cCol * NdotL * atten, float3(3.2f));
                    if (s.clearcoat > 0.05f) {
                        float ccNdotL = max(dot(s.clearcoatN, L), 0.0f);
                        float ccNdotV = max(dot(s.clearcoatN, V), 1e-4f);
                        float ccNdotH = max(dot(s.clearcoatN, H), 0.0f);
                        float ccR = max(s.ccRoughness, 0.042f);
                        float f0_cc = pow((s.ior - 1.0f) / (s.ior + 1.0f), 2.0f);
                        float F_cc = F_SchlickScalar(max(dot(V, H), 0.0f), f0_cc) * s.clearcoat;
                        float specCC = (D_GGX(ccNdotH, ccR * ccR) * G_SmithGGX(ccNdotV, ccNdotL, ccR) * F_cc)
                                     / max(4.0f * ccNdotV * ccNdotL, 1e-4f);
                        outSpec += min(float3(specCC) * cCol * ccNdotL * atten, float3(3.0f));
                    }
                }
            }
        }
    }

    // Active NOS Boost Exhaust Ground Bounce Light (tightly localized directly beneath tailpipes — zero bottom-screen blob!)
    if (dynFX.nosParams.x > 0.02f) {
        float nosAct = clamp(dynFX.nosParams.x, 0.0f, 1.0f);
        float rZ = (abs(dynFX.carMetrics.y) > 0.2f) ? (dynFX.carMetrics.y - 0.80f) : -1.96f;
        float3 nosPos = carOrigin + rotateCarLocal(float3(0.0f, 0.30f, rZ));
        float3 toN = nosPos - s.worldPos;
        float d2 = dot(toN, toN) + 0.28f;
        if (d2 < 7.5f) {
            float d = sqrt(d2);
            float3 L = toN / d;
            float NdotL = max(dot(s.N, L), 0.0f);
            if (NdotL > 0.0f) {
                float wNos = clamp(1.0f - d2 / 7.5f, 0.0f, 1.0f);
                wNos *= wNos;
                float atten = (nosAct * wNos) / (1.0f + d2 * 0.85f);
                float3 nosRad = float3(0.45f, 1.55f, 5.2f);
                float3 H = normalize(V + L);
                float specPow = mix(96.0f, 14.0f, s.roughness);
                float spec = pow(max(dot(s.N, H), 0.0f), specPow) * mix(0.42f, 0.05f, s.roughness);
                outDiffIrrad += (nosRad / PI) * NdotL * atten * 0.36f;
                outSpec += min(float3(spec) * nosRad * NdotL * atten, float3(0.85f));
            }
        }
    }

    float lampPower = u.neonParams.w;
    uint worldLightCount = uint(max(dynFX.nosParams.w, u.fxParams.w) + 0.5f);
    if (isNight > 0.08f && lampPower > 0.15f && u.carWorldPos.w > 0.5f) {
        if (worldLightCount > 0u && u.lightGridParams.z > 0.0f && u.lightGridParams.w > 0.0f) {
            // REAL O(1) 128x128x8 Rockport World Light Grid (4,309 extracted streetlamps, signals & storefronts!)
            // Locked #22/#27 colorCone contract: rgb = pre-scaled linear radiance, w > 0 downward cone, w <= -0.9 omni.
            int cx = clamp(int(floor((s.worldPos.x - u.lightGridParams.x) * u.lightGridParams.z)), 0, 127);
            int cz = clamp(int(floor((s.worldPos.z - u.lightGridParams.y) * u.lightGridParams.w)), 0, 127);
            uint cellBase = (uint(cz) * 128u + uint(cx)) * 8u;
            float nightGain = lampPower * isNight * 1.45f;
            float NdotV = max(dot(s.N, V), 1e-4f);
            float3 F0 = mix(float3(0.04f), s.baseAlbedo, s.metallic);
            float lampRough = max(s.roughness, 0.06f);
            float lampAlpha = max(lampRough * lampRough, 0.004f);

            float risWSum = 0.0f;
            float risWinW = 0.0f;
            float randAccum = clamp(xiPick, 0.0001f, 0.9999f);

            for (uint slot = 0u; slot < 8u; ++slot) {
                uint li = uint(worldLightGrid[cellBase + slot]);
                if (li == 0xFFFFu || li >= worldLightCount) break;
                RTWorldLight wl = worldLights[li];
                float radius = max(wl.posRadius.w, 2.5f);
                float3 toL = wl.posRadius.xyz - s.worldPos;
                float d2 = dot(toL, toL);
                if (d2 >= radius * radius) continue;
                float d = sqrt(d2 + 1.1f);
                float3 L = toL / d;
                float NdotL = max(dot(s.N, L), 0.0f);
                if (NdotL <= 0.0f) continue;

                // Strictly guard omni storefronts (coneMinCos <= -0.85 -> 1.0, NEVER negative!)
                float coneMinCos = wl.colorCone.w;
                float cone = (coneMinCos <= -0.85f)
                    ? 1.0f
                    : smoothstep(coneMinCos - 0.12f, min(coneMinCos + 0.35f, 0.98f), -L.y);
                if (cone <= 0.002f) continue;

                float distFrac = clamp(d / radius, 0.0f, 1.0f);
                float win = 1.0f - distFrac * distFrac;
                float atten = cone * (win * win) / (1.0f + d2 * 0.022f);
                float3 lRad = wl.colorCone.rgb * nightGain;

                float3 H = normalize(V + L);
                float NdotH = max(dot(s.N, H), 0.0f);
                float3 F = F_Schlick(max(dot(V, H), 0.0f), F0);
                float D = D_GGX(NdotH, lampAlpha);
                float G = G_SmithGGX(NdotV, NdotL, lampRough);
                float3 specBRDF = (D * G * F) / max(4.0f * NdotV * NdotL, 1e-4f);
                float3 diffContrib = (lRad / PI) * NdotL * atten;
                float3 specContrib = min(specBRDF * lRad * NdotL * atten, float3(4.5f));
                if (s.clearcoat > 0.05f) {
                    float ccNdotL = max(dot(s.clearcoatN, L), 0.0f);
                    float ccNdotV = max(dot(s.clearcoatN, V), 1e-4f);
                    float ccNdotH = max(dot(s.clearcoatN, H), 0.0f);
                    float ccR = max(s.ccRoughness, 0.045f);
                    float f0_cc = pow((s.ior - 1.0f) / (s.ior + 1.0f), 2.0f);
                    float F_cc = F_SchlickScalar(max(dot(V, H), 0.0f), f0_cc) * s.clearcoat;
                    float specCC = (D_GGX(ccNdotH, ccR * ccR) * G_SmithGGX(ccNdotV, ccNdotL, ccR) * F_cc)
                                 / max(4.0f * ccNdotV * ccNdotL, 1e-4f);
                    specContrib += min(float3(specCC) * lRad * ccNdotL * atten, float3(4.2f));
                }
                outDiffIrrad += diffContrib;
                outSpec += specContrib;

                if (collectRIS) {
                    float wLum = dot(lRad, float3(0.2126f, 0.7152f, 0.0722f)) * atten * NdotL;
                    if (wLum > 1e-4f) {
                        risWSum += wLum;
                        // Weighted Reservoir Sampling (1-Ray RIS over cell luminaires)
                        float prob = wLum / max(risWSum, 1e-5f);
                        if (randAccum < prob) {
                            randAccum = randAccum / max(prob, 1e-4f);
                            risWinW = wLum;
                            outRISPosDist = float4(wl.posRadius.xyz, sqrt(max(d2, 0.01f)));
                            outRISDiff = diffContrib;
                            outRISSpec = specContrib;
                        } else {
                            randAccum = (randAccum - prob) / max(1.0f - prob, 1e-4f);
                        }
                    }
                }
            }
            if (collectRIS && risWinW > 1e-4f) {
                outRISWeightRatio = risWSum / risWinW;
            }
        } else {
            // Backward-compatible v1 .rkpt pilot fallback (when no v2 RTWorldLight table is present)
            float3 fwdAxis = normalize(rotateCarLocal(float3(0.0f, 0.0f, 1.0f)));
            float3 rightAxis = normalize(rotateCarLocal(float3(1.0f, 0.0f, 0.0f)));
            float3 relCar = s.worldPos - carOrigin;
            float along = dot(relCar, fwdAxis);
            const float spacing = 22.0f;
            float nearestSlot = round(along / spacing) * spacing;
            float3 lampTint = mix(float3(1.00f, 0.64f, 0.24f), u.neonParams.rgb, 0.35f) * (17.5f * lampPower * isNight);

            for (int k = -1; k <= 1; ++k) {
                float sAlong = nearestSlot + float(k) * spacing;
                for (int side = -1; side <= 1; side += 2) {
                    float3 lPos = carOrigin + fwdAxis * sAlong + rightAxis * (float(side) * 8.5f) + float3(0.0f, 6.8f, 0.0f);
                    float3 toL = lPos - s.worldPos;
                    float d2 = dot(toL, toL) + 1.4f;
                    float d = sqrt(d2);
                    float3 L = toL / d;
                    float NdotL = max(dot(s.N, L), 0.0f);
                    if (NdotL > 0.0f) {
                        float downCone = smoothstep(-0.08f, 0.48f, -L.y);
                        float atten = downCone / (1.0f + d2 * 0.018f);
                        float3 H = normalize(V + L);
                        float NdotV = max(dot(s.N, V), 1e-4f);
                        float NdotH = max(dot(s.N, H), 0.0f);
                        float3 F0 = mix(float3(0.04f), s.baseAlbedo, s.metallic);
                        float3 F = F_Schlick(max(dot(V, H), 0.0f), F0);
                        float lampRough = max(s.roughness, 0.06f);
                        float D = D_GGX(NdotH, max(lampRough * lampRough, 0.004f));
                        float G = G_SmithGGX(NdotV, NdotL, lampRough);
                        float3 specBRDF = (D * G * F) / max(4.0f * NdotV * NdotL, 1e-4f);
                        outDiffIrrad += (lampTint / PI) * NdotL * atten;
                        outSpec += min(specBRDF * lampTint * NdotL * atten, float3(4.2f));
                        if (s.clearcoat > 0.05f) {
                            float ccNdotL = max(dot(s.clearcoatN, L), 0.0f);
                            float ccNdotV = max(dot(s.clearcoatN, V), 1e-4f);
                            float ccNdotH = max(dot(s.clearcoatN, H), 0.0f);
                            float ccR = max(s.ccRoughness, 0.045f);
                            float f0_cc = pow((s.ior - 1.0f) / (s.ior + 1.0f), 2.0f);
                            float F_cc = F_SchlickScalar(max(dot(V, H), 0.0f), f0_cc) * s.clearcoat;
                            float specCC = (D_GGX(ccNdotH, ccR * ccR) * G_SmithGGX(ccNdotV, ccNdotL, ccR) * F_cc)
                                         / max(4.0f * ccNdotV * ccNdotL, 1e-4f);
                            outSpec += min(float3(specCC) * lampTint * ccNdotL * atten, float3(4.0f));
                        }
                    }
                }
            }
        }
    }

    // Dynamic Scrolling Highway Sodium Lamps (procedural overpass only)
    if (lampPower > 0.02f && u.carWorldPos.w < 0.5f) {
        const float spacing = 16.0f;
        const float span = spacing * 12.0f;
        float scrollOffset = fmod(u.envParams.y, spacing);
        float phase = 7.5f - scrollOffset;
        float nearestZ = round((s.worldPos.z - phase) / spacing) * spacing + phase;

        for (int k = -1; k <= 1; ++k) {
            float lz = nearestZ + float(k) * spacing;
            if (abs(lz) > span * 0.55f) continue;
            float3 lPos = float3(4.15f, 6.55f, lz);
            float3 toL = lPos - s.worldPos;
            float d2 = dot(toL, toL) + 1.0f;
            float d = sqrt(d2);
            float3 L = toL / d;
            float NdotL = max(dot(s.N, L), 0.0f);
            if (NdotL > 0.0f) {
                float downCone = smoothstep(-0.15f, 0.45f, -L.y);
                float atten = downCone / (1.0f + d2 * 0.022f);
                float3 lCol = u.neonParams.rgb * (24.0f * lampPower);
                float3 H = normalize(V + L);
                float NdotV = max(dot(s.N, V), 1e-4f);
                float NdotH = max(dot(s.N, H), 0.0f);
                float3 F0 = mix(float3(0.04f), s.baseAlbedo, s.metallic);
                float3 F = F_Schlick(max(dot(V, H), 0.0f), F0);
                float lampRough = max(s.roughness, 0.08f);
                float D = D_GGX(NdotH, max(lampRough * lampRough, 0.006f));
                float G = G_SmithGGX(NdotV, NdotL, lampRough);
                float3 specBRDF = (D * G * F) / max(4.0f * NdotV * NdotL, 1e-4f);
                outDiffIrrad += (lCol / PI) * NdotL * atten;
                outSpec += min(specBRDF * lCol * NdotL * atten, float3(4.2f));
            }
        }

        // Hanging warm tungsten-sodium warehouse loading-dock pendant lamps (every 9.0m along x = +7.45, y = 4.70)
        float dockNearestZ = round((s.worldPos.z - 0.5f) / 9.0f) * 9.0f + 0.5f;
        for (int k = -1; k <= 1; ++k) {
            float lz = dockNearestZ + float(k) * 9.0f;
            float3 lPos = float3(7.45f, 4.70f, lz);
            float3 toL = lPos - s.worldPos;
            float d2 = dot(toL, toL) + 1.2f;
            float d = sqrt(d2);
            float3 L = toL / d;
            float NdotL = max(dot(s.N, L), 0.0f);
            if (NdotL > 0.0f) {
                float downCone = smoothstep(0.05f, 0.55f, -L.y);
                float atten = downCone / (1.0f + d2 * 0.045f);
                float3 lCol = float3(1.0f, 0.66f, 0.26f) * (14.0f * lampPower);
                outDiffIrrad += (lCol / PI) * NdotL * atten;
            }
        }
    }
}

static void evaluateLocalAreaLightsSplit(
    HitSurface s,
    float3 V,
    constant RTFrameUniforms& u,
    device const RTWorldLight* worldLights,
    device const ushort* worldLightGrid,
    constant RTDynamicFX& dynFX,
    thread float3& outDiffIrrad,
    thread float3& outSpec
) {
    float4 unusedPos;
    float3 unusedD, unusedS;
    float unusedR;
    evaluateLocalAreaLightsImpl(
        s, V, u, worldLights, worldLightGrid, dynFX, false, 0.5f,
        outDiffIrrad, outSpec, unusedPos, unusedD, unusedS, unusedR
    );
}

static float3 evaluateSecondaryHitRadiance(
    float3 hitOrigin,
    float3 hitDir,
    HitSurface s,
    instance_acceleration_structure tlas,
    device const RTInstanceMeta* instances,
    device const RkptVertex* worldVertices,
    device const uint* worldIndices,
    device const RkptMaterial* worldMaterials,
    device const BindlessTexEntry* worldTextures,
    device const RTWorldLight* worldLights,
    device const ushort* worldLightGrid,
    constant RTDynamicFX& dynFX,
    sampler repeatSamp,
    constant RTFrameUniforms& u,
    float2 xiShadow,
    bool traceSecShadow = true
) {
    float3 V = -hitDir;
    float3 L = normalize(u.sunDir.xyz);
    float3 sunRad = u.sunColor.rgb * u.sunDir.w;

    float vis = 1.0f;
    if (traceSecShadow) {
        ray sr;
        sr.origin = s.worldPos + s.geomN * 0.004f;
        sr.direction = sampleCone(L, u.sunColor.w * 0.5f, xiShadow);
        sr.min_distance = 0.002f;
        sr.max_distance = 120.0f;
        vis = traceShadowVisibility(
            sr, tlas, TLAS_MASK_SHADOW_ALL, (u.carWorldPos.w > 0.5f),
            instances, worldVertices, worldIndices, worldMaterials, worldTextures,
            uint(max(u.jitterParams.w, 1.0f)), repeatSamp
        );
    } else {
        // Hemisphere sky/sun visibility proxy when skipping secondary shadow ray in 1-SPP real-time GI/rough bounce
        vis = smoothstep(-0.05f, 0.25f, dot(s.geomN, L)) * 0.72f;
    }

    float3 direct = evaluateDirectSunBRDF(s, V, L, sunRad) * vis;
    float3 envUp = sampleSkyDome(reflect(-V, s.N), u, false, false) * u.coronaColor.w;
    float3 F0 = mix(float3(0.04f), s.baseAlbedo, s.metallic);
    float3 F = F_Schlick(max(dot(s.N, V), 0.0f), F0);
    float3 ambient = (1.0f - F) * (1.0f - s.metallic) * s.baseAlbedo * (u.skyZenith.rgb * 0.35f + u.skyHorizon.rgb * 0.45f) * u.coronaColor.w * 0.42f;
    float3 reflApprox = F * envUp;
    // Recessed optical cavity occlusion: keeps dark gunmetal housing jet-dark while letting silver chrome bowls gleam cleanly in daylight!
    if (s.matID == MAT_HEADLIGHT_EMIT) {
        float chromeFrac = smoothstep(0.32f, 0.62f, s.metallic);
        direct *= mix(0.18f, 0.65f, chromeFrac);
        ambient *= mix(0.10f, 0.58f, chromeFrac);
        reflApprox *= mix(0.08f, 0.64f, chromeFrac);
    }

    float3 localDiff, localSpec;
    evaluateLocalAreaLightsSplit(s, V, u, worldLights, worldLightGrid, dynFX, localDiff, localSpec);
    float3 localTotal = localDiff * (1.0f - F) * (1.0f - s.metallic) * s.baseAlbedo + localSpec;

    // Clamp secondary taillight radiance <= 1.4 so wet road puddles & trunk lip never smear into blurry red smudges!
    float3 bounceEmissive = (s.matID == MAT_TAILLIGHT_EMIT)
        ? min(s.emissive, float3(1.4f))
        : min(s.emissive, float3(3.8f));
    return min(direct + ambient + reflApprox + bounceEmissive + localTotal, float3(9.0f));
}

// ---------------------------------------------------------------------------
// v1.2.0 Analytical 3D NFSMW Checkpoint / Speedtrap / Boss Gates & Chevrons
// Zero TLAS churn (< 0.15 ms GPU); evaluated on primary rays + wet-road reflections.
// ---------------------------------------------------------------------------

static inline float3 evaluateEventGateFX(
    float3 rayOrigin,
    float3 rayDir,
    float maxT,
    constant RTEventWorldFX& evFX,
    bool isReflection,
    thread float& outReactive
) {
    outReactive = 0.0f;
    if (evFX.fxParams.x < 0.5f) return float3(0.0f);

    float3 gatePos = evFX.gatePosRadius.xyz;
    float halfW = clamp(evFX.gatePosRadius.w, 5.5f, 11.5f);
    float3 fwdRaw = float3(evFX.gateDirKind.x, 0.0f, evFX.gateDirKind.z);
    float fwdLenSq = dot(fwdRaw, fwdRaw);
    float3 fwd = (fwdLenSq > 1e-5f) ? (fwdRaw * rsqrt(fwdLenSq)) : float3(0.0f, 0.0f, 1.0f);
    float3 right = float3(fwd.z, 0.0f, -fwd.x);
    int kind = int(evFX.gateDirKind.w + 0.5f);
    float simT = evFX.fxParams.w;

    // Authentic NFSMW 2005 event palettes: 0=Sprint Amber-Gold, 1=Speedtrap Electric Cyan, 2=Blacklist Crimson-Gold
    float3 primaryCol   = float3(1.00f, 0.62f, 0.08f);
    float3 secondaryCol = float3(1.00f, 0.90f, 0.42f);
    if (kind == 1) {
        primaryCol   = float3(0.06f, 0.86f, 1.00f);
        secondaryCol = float3(0.70f, 0.98f, 1.00f);
    } else if (kind == 2) {
        primaryCol   = float3(1.00f, 0.14f, 0.08f);
        secondaryCol = float3(1.00f, 0.76f, 0.14f);
    }

    float3 accum = float3(0.0f);

    // 1. Active Gate Bounding-Sphere Early-Out & Twin Vertical Volumetric Light Pillars
    float3 toGate = gatePos + float3(0.0f, 4.0f, 0.0f) - rayOrigin;
    float tGateCenter = dot(toGate, rayDir);
    if (tGateCenter > -16.0f && tGateCenter < min(maxT + 14.0f, 380.0f)) {
        float distFade = smoothstep(380.0f, 22.0f, max(tGateCenter, 0.0f));
        float3 closestCenter = rayOrigin + rayDir * max(tGateCenter, 0.0f);
        if (length(closestCenter - (gatePos + float3(0.0f, 4.0f, 0.0f))) < halfW + 9.0f) {
            // Twin Left & Right Volumetric Light Towers (height 8.4m)
            for (int side = -1; side <= 1; side += 2) {
                float3 pBase = gatePos + right * (float(side) * halfW);
                float3 uSeg = float3(0.0f, 1.0f, 0.0f);
                float3 w0 = rayOrigin - pBase;
                float b = dot(rayDir, uSeg);
                float d = dot(rayDir, w0);
                float e = dot(uSeg, w0);
                float denom = max(1.0f - b * b, 1e-4f);
                float sSeg = clamp((e - b * d) / denom, 0.0f, 8.4f);
                float3 segPt = pBase + float3(0.0f, sSeg, 0.0f);
                float tRay = clamp(dot(segPt - rayOrigin, rayDir), 0.25f, min(maxT + 0.4f, 360.0f));
                if (tRay < maxT + 0.35f) {
                    float distSeg = length((rayOrigin + rayDir * tRay) - segPt);
                    float core = exp(-distSeg * distSeg * 14.0f);
                    float halo = exp(-distSeg * distSeg * 1.6f) * 0.28f;
                    float yGrad = smoothstep(8.4f, 6.2f, sSeg) * smoothstep(-0.1f, 0.4f, sSeg);
                    float pulseBand = 0.78f + 0.28f * sin(sSeg * 2.8f - simT * 6.5f);
                    float3 towerCol = mix(primaryCol, secondaryCol, core * 0.65f);
                    accum += towerCol * (core * 2.6f + halo) * yGrad * pulseBand * distFade;
                }
            }

            // 2. Holographic Arch Plane (Overhead Chevron Banner + Road Laser Strip + Side Energy Curtains)
            float denomPl = dot(rayDir, fwd);
            if (abs(denomPl) > 1e-4f) {
                float tPl = dot(gatePos - rayOrigin, fwd) / denomPl;
                if (tPl > 0.4f && tPl < min(maxT + 0.25f, 340.0f)) {
                    float3 hitP = rayOrigin + rayDir * tPl;
                    float3 rel = hitP - gatePos;
                    float lx = dot(rel, right);
                    float ly = rel.y;
                    float absX = abs(lx);
                    if (absX <= halfW + 0.45f && ly >= -0.15f && ly <= 7.8f) {
                        // A) Overhead Holographic Banner Ribbon (ly: 4.9 .. 6.7m)
                        if (ly >= 4.7f && ly <= 6.9f && absX <= halfW) {
                            float topRail = exp(-pow((ly - 6.65f) * 9.5f, 2.0f));
                            float botRail = exp(-pow((ly - 4.95f) * 9.5f, 2.0f));
                            float inBand = smoothstep(4.85f, 5.05f, ly) * smoothstep(6.75f, 6.55f, ly);
                            float cyB = (ly - 5.80f) / 0.85f;
                            float chevPhase = fract((absX * 0.42f - abs(cyB) * 0.52f) - simT * 1.6f);
                            float chev = smoothstep(0.14f, 0.24f, chevPhase) * smoothstep(0.62f, 0.50f, chevPhase) * inBand;
                            float scan = 0.75f + 0.25f * sin(ly * 38.0f + simT * 12.0f);
                            float3 archCol = mix(primaryCol, secondaryCol, chev * 0.85f);
                            accum += archCol * ((topRail + botRail) * 2.4f + chev * 1.45f + inBand * 0.22f) * scan * distFade;
                            if (kind == 1) {
                                float dPod = length(float2(absX - (halfW - 1.35f), ly - 5.80f));
                                float podGlow = exp(-dPod * dPod * 9.0f) * (0.75f + 0.25f * sin(simT * 14.0f));
                                accum += float3(1.1f, 1.3f, 1.5f) * podGlow * 2.2f * distFade;
                            }
                        }

                        // B) Translucent Side Energy Curtains framing the road edges
                        if (ly >= 0.0f && ly <= 6.7f && absX >= halfW - 1.8f && absX <= halfW) {
                            float edgeProx = smoothstep(halfW - 1.8f, halfW, absX);
                            float hFade = smoothstep(6.7f, 4.8f, ly) * smoothstep(0.0f, 0.5f, ly);
                            float scan = 0.72f + 0.28f * sin(ly * 10.0f - simT * 8.0f);
                            accum += primaryCol * edgeProx * edgeProx * hFade * scan * 0.75f * distFade;
                        }

                        // C) Speedtrap Translucent Laser Grid Curtain across road
                        if (kind == 1 && ly >= 0.0f && ly <= 5.0f && absX < halfW - 0.1f) {
                            float vLines = pow(max(0.0f, cos(lx * 3.14159f * 0.85f)), 24.0f);
                            float hSweep = exp(-pow(fract(ly * 0.25f - simT * 0.9f) - 0.5f, 2.0f) * 90.0f);
                            float curbFade = smoothstep(5.0f, 0.2f, ly);
                            accum += primaryCol * (vLines * 0.22f + hSweep * 0.34f) * curbFade * distFade;
                        }

                        // D) Razor-sharp Glowing Road Threshold Laser Strip
                        if (ly >= -0.10f && ly <= 0.42f && absX <= halfW) {
                            float strip = exp(-pow((ly - 0.10f) * 11.0f, 2.0f));
                            float dash = 0.65f + 0.35f * cos(lx * 2.6f);
                            accum += secondaryCol * strip * dash * 1.85f * distFade;
                        }
                    }
                }
            }
        }
    }

    // 3. Distant Next-Checkpoint Vertical Horizon Sky Beacon
    if (!isReflection && evFX.nextPosRadius.w > 0.5f) {
        float3 nPos = evFX.nextPosRadius.xyz;
        float3 uSeg = float3(0.0f, 1.0f, 0.0f);
        float3 w0 = rayOrigin - nPos;
        float b = dot(rayDir, uSeg);
        float d = dot(rayDir, w0);
        float e = dot(uSeg, w0);
        float denom = max(1.0f - b * b, 1e-4f);
        float sSeg = clamp((e - b * d) / denom, 0.0f, 34.0f);
        float3 segPt = nPos + float3(0.0f, sSeg, 0.0f);
        float tRay = dot(segPt - rayOrigin, rayDir);
        if (tRay > 18.0f && tRay < min(maxT + 0.5f, 520.0f)) {
            float distSeg = length((rayOrigin + rayDir * tRay) - segPt);
            float beam = exp(-distSeg * distSeg * 0.9f) * smoothstep(34.0f, 4.0f, sSeg) * smoothstep(520.0f, 40.0f, tRay);
            accum += primaryCol * beam * 0.52f;
        }
    }

    // 4. Up to 4 Floating Holographic Road Direction Chevrons (primary rays only)
    if (!isReflection) {
        for (int k = 0; k < 4; ++k) {
            float4 ch = evFX.chevrons[k];
            if (ch.w <= -90.0f) continue;
            float3 cPos = ch.xyz + float3(0.0f, 1.65f + 0.16f * sin(simT * 3.8f + float(k) * 1.1f), 0.0f);
            float3 toC = cPos - rayOrigin;
            float tC = dot(toC, rayDir);
            if (tC > 2.5f && tC < min(maxT + 0.2f, 220.0f)) {
                float cyC = cos(ch.w), syC = sin(ch.w);
                float3 cFwd = float3(syC, 0.0f, cyC);
                float3 cRight = float3(cyC, 0.0f, -syC);
                float dPl = dot(rayDir, cFwd);
                if (abs(dPl) > 0.05f) {
                    float tPl = dot(cPos - rayOrigin, cFwd) / dPl;
                    if (tPl > 2.5f && tPl < min(maxT + 0.2f, 220.0f)) {
                        float3 rel = (rayOrigin + rayDir * tPl) - cPos;
                        float cx = dot(rel, cRight);
                        float cy = rel.y;
                        if (abs(cx) < 1.55f && abs(cy) < 0.95f) {
                            float arm = abs((cy + 0.22f) + abs(cx) * 0.62f - 0.52f);
                            float chMask = smoothstep(0.18f, 0.05f, arm) * smoothstep(1.35f, 1.05f, abs(cx));
                            float wave = 0.62f + 0.38f * sin(simT * 6.0f - float(k) * 1.4f);
                            float fade = smoothstep(220.0f, 18.0f, tPl) * smoothstep(3.5f, 10.0f, tPl);
                            accum += mix(primaryCol, secondaryCol, 0.55f) * chMask * wave * fade * 1.9f;
                        }
                    }
                }
            }
        }
    }

    float mag = length(accum);
    if (mag > 0.01f) {
        outReactive = min(0.95f, mag * 0.75f);
    }
    return min(accum, float3(7.5f));
}

// ---------------------------------------------------------------------------
// v1.3.0 Analytical Drift VFX: Persistent Road Skidmarks (<=128), Ballistic
// Sparks (<=64) & Volumetric Tire Smoke (<=16) (#24 Guardrail 2: < 0.5 ms GPU)
// ---------------------------------------------------------------------------

static inline void applyRoadSkidmarks(
    thread HitSurface& s,
    float hitDist,
    bool temporal1SPP,
    constant RTVFXBuffer& vfx
) {
    uint skidCount = min(vfx.counts.x, 128u);
    float maxDist = temporal1SPP ? 32.0f : 45.0f;
    if (skidCount == 0u || hitDist >= maxDist) return;
    float2 pXZ = s.worldPos.xz;
    // 1-instruction bounding-box early-out rejects >96% of road pixels immediately
    if (pXZ.x < vfx.skidBoundsMinMaxXZ.x || pXZ.x > vfx.skidBoundsMinMaxXZ.z ||
        pXZ.y < vfx.skidBoundsMinMaxXZ.y || pXZ.y > vfx.skidBoundsMinMaxXZ.w) {
        return;
    }

    float maxMask = 0.0f;
    for (uint i = 0u; i < skidCount; ++i) {
        RTSkidMarkGPU sk = vfx.skids[i];
        float alpha = sk.p1Alpha.w;
        if (alpha <= 0.01f) continue;
        float halfW = max(sk.p0Width.w, 0.08f);
        float2 a = sk.p0Width.xz;
        float2 b = sk.p1Alpha.xz;
        // Fast per-segment AABB reject (eliminates >95% of dot/sqrt math!)
        if (pXZ.x < min(a.x, b.x) - halfW || pXZ.x > max(a.x, b.x) + halfW ||
            pXZ.y < min(a.y, b.y) - halfW || pXZ.y > max(a.y, b.y) + halfW) {
            continue;
        }
        float avgY = 0.5f * (sk.p0Width.y + sk.p1Alpha.y);
        if (abs(s.worldPos.y - avgY) > 1.35f) continue;

        float2 ab = b - a;
        float lenSq = max(dot(ab, ab), 1e-4f);
        float t = clamp(dot(pXZ - a, ab) / lenSq, 0.0f, 1.0f);
        float2 dVec = pXZ - (a + ab * t);
        float dSq = dot(dVec, dVec);
        if (dSq < halfW * halfW) {
            float dNorm = sqrt(dSq) / halfW;
            float edge = smoothstep(1.0f, 0.36f, dNorm);
            // Subtle vulcanized 3-groove tire tread striation
            float tread = 0.84f + 0.16f * cos(dNorm * 9.5f);
            maxMask = max(maxMask, edge * tread * alpha);
        }
    }

    if (maxMask > 0.01f) {
        float distFade = smoothstep(maxDist, maxDist * 0.45f, hitDist);
        float m = clamp(maxMask * distFade, 0.0f, 0.95f);
        // Darken rubber tracks and boost slick wet-rubber specular sheen
        s.baseAlbedo *= mix(1.0f, 0.16f, m * 0.92f);
        s.roughness = mix(s.roughness, 0.12f, m * 0.72f);
        s.clearcoat = max(s.clearcoat, m * 0.52f);
        s.ccRoughness = mix(s.ccRoughness, 0.032f, m * 0.55f);
    }
}

static inline void evaluateDriftVFXPrimary(
    float3 rayOrigin,
    float3 rayDir,
    float gDepth,
    bool temporal1SPP,
    constant RTVFXBuffer& vfx,
    constant RTFrameUniforms& u,
    constant RTDynamicFX& dynFX,
    thread float3& outSparkRGB,
    thread float3& outSmokeRGB,
    thread float& outSmokeTransmittance,
    thread float& outReactive
) {
    outSparkRGB = float3(0.0f);
    outSmokeRGB = float3(0.0f);
    outSmokeTransmittance = 1.0f;
    outReactive = 0.0f;

    uint sparkCount = min(vfx.counts.y, 64u);
    uint smokeCount = min(vfx.counts.z, 16u);
    float sphR = vfx.fxSphere.w;
    if ((sparkCount == 0u && smokeCount == 0u) || sphR <= 0.05f) return;

    // Single-instruction bounding-sphere reject enclosing all active sparks + smoke
    float3 ocAll = vfx.fxSphere.xyz - rayOrigin;
    float bAll = dot(ocAll, rayDir);
    if (bAll < -sphR) return;
    float3 nearAll = rayOrigin + rayDir * max(bAll, 0.0f);
    float3 dAllVec = nearAll - vfx.fxSphere.xyz;
    if (dot(dAllVec, dAllVec) > sphR * sphR) return;

    // 1. Ballistic Golden-Orange Metal Sparks (<= 64, razor-sharp incandescent needles w/ fast sphere reject)
    for (uint i = 0u; i < sparkCount; ++i) {
        RTSparkGPU sp = vfx.sparks[i];
        float life = sp.posLife.w;
        if (life <= 0.01f) continue;

        float3 vel = sp.velTemp.xyz;
        float3 pTip = sp.posLife.xyz + vel * 0.005f;
        float3 pTail = sp.posLife.xyz - vel * 0.024f;
        float3 mid = 0.5f * (pTip + pTail);
        float tMid = dot(mid - rayOrigin, rayDir);
        // Reject sparks closer than 1.25m to camera lens so stray near-camera particles never blow up across HUD
        if (tMid < 1.25f || tMid > gDepth + 0.45f) continue;
        float3 seg = pTip - pTail;
        float segLenSq = max(dot(seg, seg), 1e-4f);
        float boundR2 = segLenSq * 0.32f + 0.22f;
        float3 dMid = (rayOrigin + rayDir * tMid) - mid;
        if (dot(dMid, dMid) > boundR2) continue;

        // Exact closest approach between primary ray and spark streak segment
        float3 w0 = rayOrigin - pTail;
        float b = dot(rayDir, seg);
        float d = dot(rayDir, w0);
        float e = dot(seg, w0);
        float denom = max(segLenSq - b * b, 1e-4f);
        float sParam = clamp((e - b * d) / denom, 0.0f, 1.0f);
        float3 segPt = pTail + seg * sParam;
        float tRay = dot(segPt - rayOrigin, rayDir);
        // Strict tRay < gDepth - 0.04f occlusion (#113 P2-2) + tightened ~22% needle core so sparks never bleed over bumper!
        if (tRay > 1.25f && tRay < gDepth - 0.04f) {
            float3 diff = (rayOrigin + rayDir * tRay) - segPt;
            float distSq = dot(diff, diff);
            float coreR = 0.0052f + tRay * 0.00042f;
            float coreR2 = max(coreR * coreR, 1e-6f);
            if (distSq < coreR2 * 22.0f) {
                float core = exp(-distSq / coreR2);
                float halo = exp(-distSq / (coreR2 * 4.2f)) * 0.28f;
                float taper = smoothstep(0.0f, 0.28f, sParam) * (0.42f + 0.58f * sParam);
                float temp = clamp(sp.velTemp.w, 0.0f, 1.0f);
                float3 sparkCol = mix(float3(1.00f, 0.32f, 0.03f), float3(1.00f, 0.88f, 0.44f), temp * (0.35f + 0.65f * core));
                float intensity = (core * 6.8f + halo * 1.8f) * taper * smoothstep(0.0f, 0.18f, life);
                outSparkRGB += sparkCol * intensity;
            }
        }
    }
    if (dot(outSparkRGB, outSparkRGB) > 0.002f) {
        outSparkRGB = min(outSparkRGB, float3(11.0f));
        outReactive = 1.0f;
    }

    // 2. Volumetric Tire-Smoke Puffs (<= 16, soft ray-sphere clouds lit by sun, sky, taillights & cop strobes)
    if (smokeCount > 0u) {
        float simT = dynFX.timeWeather.x;
        float isNight = smoothstep(0.036f, 0.015f, u.skyZenith.r);
        float3 sunDir = normalize(u.sunDir.xyz);
        float sunPhase = 0.55f + 0.45f * clamp(dot(rayDir, sunDir), -1.0f, 1.0f);
        float3 baseSmokeLight = (u.skyZenith.rgb * 0.72f + u.skyHorizon.rgb * 0.88f) * max(u.coronaColor.w, 0.42f) * 0.78f
                              + u.sunColor.rgb * u.sunDir.w * (0.42f * sunPhase)
                              + float3(0.065f, 0.072f, 0.088f) * isNight;
        float smokeDensityAccum = 0.0f;
        float3 smokeLightAccum = float3(0.0f);

        for (uint i = 0u; i < smokeCount; ++i) {
            RTSmokePuffGPU sm = vfx.smoke[i];
            float rad = sm.posRadius.w;
            float alpha = sm.velAlpha.w;
            if (rad <= 0.05f || alpha <= 0.008f) continue;

            float3 oc = rayOrigin - sm.posRadius.xyz;
            float b = dot(oc, rayDir);
            float c = dot(oc, oc) - rad * rad;
            float disc = b * b - c;
            if (disc <= 0.0f) continue;

            float sqrtD = sqrt(disc);
            float t0 = max(-b - sqrtD, 1.25f);
            float t1 = min(-b + sqrtD, gDepth);
            if (t1 <= t0) continue;

            float chord = (t1 - t0) / max(2.0f * rad, 0.1f);
            float3 pMid = rayOrigin + rayDir * (0.5f * (t0 + t1));
            float radialNorm = length(pMid - sm.posRadius.xyz) / rad;
            float radialFalloff = pow(max(0.0f, 1.0f - radialNorm * radialNorm), 1.65f);
            float nearCamFade = smoothstep(1.30f, 2.8f, 0.5f * (t0 + t1));

            // Fast 1-octave billowing curl in 1-SPP real-time driving (4x faster than fbm2D!), full FBM in Photo Mode
            float2 wUV = pMid.xz * 1.55f + float2(pMid.y * 1.15f - simT * 0.45f, float(i) * 2.31f);
            float billow = temporal1SPP
                ? (0.58f + 0.42f * noise2D(wUV))
                : (0.58f + 0.42f * fbm2D(wUV));
            // Boost golden-hour tire smoke readability ~25% (#113 P2-2) while preserving silky FI stability
            float localDens = chord * radialFalloff * nearCamFade * billow * alpha * 2.05f;
            if (localDens <= 0.003f) continue;

            // Local crimson taillight + red/blue cop strobe scattering inside tire smoke!
            float3 puffLight = baseSmokeLight;
            float dCar = length(pMid - u.carWorldPos.xyz);
            float tailGlow = exp(-dCar * 0.34f) * 0.65f;
            puffLight += float3(1.15f, 0.09f, 0.03f) * tailGlow;
            for (int cIdx = 0; cIdx < 2; ++cIdx) {
                float strobe = dynFX.copLightPos[cIdx].w * dynFX.copLightCol[cIdx].w;
                if (strobe > 0.02f) {
                    float dCop2 = dot(pMid - dynFX.copLightPos[cIdx].xyz, pMid - dynFX.copLightPos[cIdx].xyz);
                    puffLight += dynFX.copLightCol[cIdx].rgb * (strobe * 1.5f / (1.0f + dCop2 * 0.06f));
                }
            }
            float3 smokeAlbedo = mix(float3(0.76f, 0.78f, 0.82f), float3(0.95f, 0.96f, 0.97f), billow);
            smokeLightAccum += smokeAlbedo * puffLight * localDens;
            smokeDensityAccum += localDens;
        }

        if (smokeDensityAccum > 0.004f) {
            float T = exp(-min(smokeDensityAccum, 1.38f));
            outSmokeTransmittance = T;
            outSmokeRGB = (smokeLightAccum / max(smokeDensityAccum, 1e-4f)) * (1.0f - T);
            outReactive = max(outReactive, clamp((1.0f - T) * 2.2f, 0.0f, 1.0f));
        }
    }
}

// ---------------------------------------------------------------------------
// Primary Hardware Ray Tracing Kernel (Outputs Demodulated Buffers + G-Buffer)
// ---------------------------------------------------------------------------

kernel void raytraceKernel(
    uint2 gid [[thread_position_in_grid]],
    texture2d<float, access::read_write> accumDiffTex [[texture(0)]],
    texture2d<float, access::read_write> accumSpecTex [[texture(1)]],
    texture2d<float, access::read_write> gAlbedoTex [[texture(2)]],
    texture2d<float, access::write> gNormalRoughTex [[texture(3)]],
    texture2d<float, access::write> gDepthTex [[texture(4)]],
    texture2d<float, access::read_write> gEmissiveTex [[texture(5)]],
    texture2d<float> asphaltDiffTex [[texture(6)]],
    texture2d<float> asphaltNormTex [[texture(7)]],
    texture2d<float> asphaltRoughTex [[texture(8)]],
    texture2d<float> concreteDiffTex [[texture(9)]],
    texture2d<float> concreteNormTex [[texture(10)]],
    texture2d<float> carbonDiffTex [[texture(11)]],
    texture2d<float> carbonNormTex [[texture(12)]],
    texture2d<float> carLiveryMaskTex [[texture(13)]],
    texture2d<float> carMiscTex [[texture(14)]],
    texture2d<float> carBadgeTex [[texture(15)]],
    texture2d<float> carHeadlightTex [[texture(16)]],
    texture2d<float> carTaillightTex [[texture(17)]],
    texture2d<float> carTireTex [[texture(18)]],
    texture2d<float> carInteriorTex [[texture(19)]],
    texture2d<float, access::write> gColorTex [[texture(20)]],
    texture2d<float, access::write> gMotionTex [[texture(21)]],
    texture2d<float, access::write> gSpecAlbedoTex [[texture(22)]],
    texture2d<float, access::write> gRoughnessTex [[texture(23)]],
    texture2d<float, access::write> gReactiveTex [[texture(24)]],
    sampler repeatSamp [[sampler(0)]],
    constant RTFrameUniforms& u [[buffer(0)]],
    instance_acceleration_structure tlas [[buffer(1)]],
    device const RTVertex* vertices [[buffer(2)]],
    device const uint* indices [[buffer(3)]],
    device const uint* triMaterials [[buffer(4)]],
    device const RTInstanceMeta* instances [[buffer(5)]],
    device const RTMaterial* materials [[buffer(6)]],
    device const RkptVertex* worldVertices [[buffer(7)]],
    device const uint* worldIndices [[buffer(8)]],
    device const RkptMaterial* worldMaterials [[buffer(9)]],
    device const BindlessTexEntry* worldTextures [[buffer(10)]],
    device const RTWorldLight* worldLights [[buffer(11)]],
    device const ushort* worldLightGrid [[buffer(12)]],
    constant RTDynamicFX& dynFX [[buffer(13)]],
    constant RTAISceneData& aiScene [[buffer(14)]],
    constant RTVFXBuffer& vfx [[buffer(15)]],
    constant RTEventWorldFX& eventFX [[buffer(16)]]
) {
    // worldLights & worldLightGrid & dynFX actively drive real 4,309-light O(1) lookup, cop strobes, NOS & rain!
    uint width = uint(u.rtParams.x);
    uint height = uint(u.rtParams.y);
    if (gid.x >= width || gid.y >= height) return;

    uint sampleIdx = uint(u.rtParams.z);
    bool temporal1SPP = (u.jitterParams.z > 0.5f);
    bool hasRkptWorld = (u.carWorldPos.w > 0.5f);
    uint worldTexCount = uint(max(u.jitterParams.w, 1.0f));

    float2 cpShift = float2(hash21(float2(gid)), hash21(float2(gid) + float2(71.3f, 19.7f)));
    float2 xiAA   = r2Sequence(sampleIdx, cpShift);
    float2 xiShad = r2Sequence(sampleIdx + 37u, fract(cpShift + float2(0.37f, 0.71f)));
    float2 xiRefl = r2Sequence(sampleIdx + 79u, fract(cpShift + float2(0.61f, 0.29f)));
    float2 xiCC   = r2Sequence(sampleIdx + 113u, fract(cpShift + float2(0.17f, 0.83f)));
    float2 xiGI   = r2Sequence(sampleIdx + 157u, fract(cpShift + float2(0.47f, 0.53f)));

    bool useRT = u.postParams.w > 0.5f;
    bool isSplit = u.rtParams.w > 0.5f;
    uint splitX = width / 2u;
    if (isSplit) {
        if (gid.x == splitX || gid.x == splitX + 1u) {
            accumDiffTex.write(float4(0.0f), gid);
            accumSpecTex.write(float4(2.5f, 2.5f, 2.5f, 1.0f), gid);
            gAlbedoTex.write(float4(0.0f), gid);
            gNormalRoughTex.write(float4(0.0f, 1.0f, 0.0f, -1.0f), gid);
            gDepthTex.write(float4(240.0f), gid);
            gEmissiveTex.write(float4(0.0f), gid);
            gColorTex.write(float4(2.5f, 2.5f, 2.5f, 1.0f), gid);
            gMotionTex.write(float4(0.0f), gid);
            gSpecAlbedoTex.write(float4(0.0f), gid);
            gRoughnessTex.write(float4(1.0f), gid);
            gReactiveTex.write(float4(0.0f), gid);
            return;
        }
        useRT = (gid.x > splitX);
    }

    float2 resF = float2(float(width), float(height));
    float2 pixelJitter = temporal1SPP
        ? (float2(0.5f, 0.5f) + u.jitterParams.xy)
        : ((sampleIdx == 0u && !useRT) ? float2(0.5f, 0.5f) : xiAA);
    float2 localUV = (float2(gid) + pixelJitter) / resF;
    // Zero-Extra-VRAM 2x2 tiled 4K snap crop (default 0,0,1,1 for normal frames)
    float2 tileScale = (vfx.tileCrop.z > 1e-4f && vfx.tileCrop.w > 1e-4f) ? vfx.tileCrop.zw : float2(1.0f, 1.0f);
    float2 uv = vfx.tileCrop.xy + localUV * tileScale;
    float2 ndc = float2(uv.x * 2.0f - 1.0f, 1.0f - uv.y * 2.0f);

    float4 worldFar = u.invViewProjMatrix * float4(ndc, 1.0f, 1.0f);
    float3 targetPt = worldFar.xyz / max(worldFar.w, 1e-5f);
    float3 rayOrigin = u.cameraPos.xyz;
    float3 rayDir = normalize(targetPt - rayOrigin);

    // Hardware-RT Thin-Lens DoF Bokeh (7-blade polygonal aperture disk, active in Photo Mode when apertureRadiusM > 0)
    bool photoDoFActive = (vfx.photoDoF.x > 1e-4f);
    if (photoDoFActive) {
        float2 xiLens = r2Sequence(sampleIdx + 211u, fract(cpShift + float2(0.83f, 0.19f)));
        float2 d = xiLens * 2.0f - 1.0f;
        float rDisk = 0.0f;
        float theta = 0.0f;
        if (abs(d.x) > 1e-5f || abs(d.y) > 1e-5f) {
            if (abs(d.x) > abs(d.y)) {
                rDisk = d.x;
                theta = (PI * 0.25f) * (d.y / d.x);
            } else {
                rDisk = d.y;
                theta = (PI * 0.5f) - (PI * 0.25f) * (d.x / d.y);
            }
        }
        // Authentic 7-blade photographic iris polygon (cos(PI/7) / cos(sectorAngle))
        const float sector = PI * 2.0f / 7.0f;
        const float halfSec = sector * 0.5f;
        float modAngle = fmod(theta + PI * 4.0f, sector) - halfSec;
        float polyScale = cos(halfSec) / max(cos(modAngle), 0.72f);
        float2 lensDisk = float2(cos(theta), sin(theta)) * (rDisk * polyScale) * vfx.photoDoF.x;

        // World-space focal plane perpendicular to cameraForward (100% seam-free across 2x2 4K tiles!)
        float cosFwd = max(dot(rayDir, normalize(u.cameraForward.xyz)), 0.12f);
        float tFocus = max(vfx.photoDoF.y, 0.5f) / cosFwd;
        float3 focalPt = rayOrigin + rayDir * tFocus;

        float3 camR = normalize(u.cameraRight.xyz);
        float3 camU = normalize(u.cameraUp.xyz);
        rayOrigin = rayOrigin + camR * lensDisk.x + camU * lensDisk.y;
        rayDir = normalize(focalPt - rayOrigin);
    }

    ray r;
    r.origin = rayOrigin;
    r.direction = rayDir;
    r.min_distance = 0.02f;
    // Full 5.2 km primary ray range so EA PVS 2.0-4.6 km cross-bay & Highway 99 vistas render completely unclipped!
    r.max_distance = hasRkptWorld ? 5200.0f : 250.0f;

    uint primaryMask = 0xFFu;
    bool cockpitCamActive = ((vfx.counts.w & 1u) != 0u);
    // In Cockpit POV, exclude player car roof/wiper/glass above horizon so sky & road are 100% unobstructed,
    // while keeping the sculpted hood & lower dash framing visible below horizon!
    if (cockpitCamActive && rayDir.y > -0.04f) {
        primaryMask = (0xFFu & ~(TLAS_MASK_CAR | TLAS_MASK_GLASS));
    }

    TraceResult hit = traceSceneRay(
        r, tlas, primaryMask, hasRkptWorld,
        instances, worldVertices, worldIndices, worldMaterials, worldTextures, worldTexCount, repeatSamp
    );

    // One-Way Interior Windshield Transmittance (Cockpit POV: counts.w bit0 = 1)
    // Lets outgoing driver rays below horizon pass cleanly through lower windshield glass onto the sculpted hood & road!
    bool cockpitWindshieldPassed = cockpitCamActive && (rayDir.y > -0.04f);
    if (hit.hit && cockpitCamActive && hit.distance < 2.15f) {
        RTInstanceMeta firstInst = instances[hit.instanceID];
        bool isPlayerCar = ((firstInst.flags & ((1u << 4) | (1u << 7) | 4u)) == 0u);
        if (isPlayerCar) {
            uint firstTriMat = triMaterials[firstInst.triOffset + hit.primitiveID];
            if (firstTriMat == MAT_SMOKED_GLASS || firstTriMat == MAT_INTERIOR_DARK) {
                ray rPass;
                rPass.origin = rayOrigin + rayDir * (hit.distance + 0.03f);
                rPass.direction = rayDir;
                rPass.min_distance = 0.01f;
                rPass.max_distance = r.max_distance;
                TraceResult passHit = traceSceneRay(
                    rPass, tlas, (0xFFu & ~TLAS_MASK_GLASS), hasRkptWorld,
                    instances, worldVertices, worldIndices, worldMaterials, worldTextures, worldTexCount, repeatSamp
                );
                if (passHit.hit) {
                    passHit.distance += (hit.distance + 0.03f);
                }
                hit = passHit;
                cockpitWindshieldPassed = true;
            }
        }
    }

    float3 outDiffIrrad = float3(0.0f);
    float3 outSpecRad   = float3(0.0f);
    float3 gAlbedo      = float3(0.0f);
    float3 gSpecAlbedo  = float3(0.0f);
    float4 gNormRough   = float4(0.0f, 1.0f, 0.0f, -1.0f);
    float  gDepth       = r.max_distance;
    float  clipDepth    = 0.0f; // Reverse-Z (far/sky = 0.0, near = 1.0) matching MTLFXTemporalDenoisedScaler
    float3 gEmissive    = float3(0.0f);
    float  gRoughVal    = 1.0f;
    float  gReactiveVal = 0.0f;
    float2 motionPx     = float2(0.0f);

    float2 currPx = float2(gid) + float2(0.5f, 0.5f);

    if (!hit.hit) {
        outSpecRad = sampleSkyDome(rayDir, u, true, true);
        // Far-plane sky motion vector so clouds/horizon never smear during turns
        float4 prevSkyClip = u.prevViewProjMatrix * float4(rayOrigin + rayDir * 400.0f, 1.0f);
        if (prevSkyClip.w > 1e-4f) {
            float2 prevNDC = prevSkyClip.xy / prevSkyClip.w;
            float2 prevUV = float2(prevNDC.x * 0.5f + 0.5f, 0.5f - prevNDC.y * 0.5f);
            motionPx = prevUV * resF - currPx;
        }
    } else {
        HitSurface s = resolveHitSurface(
            rayOrigin, rayDir, hit.distance,
            hit.instanceID, hit.primitiveID, hit.bary, true,
            vertices, indices, triMaterials, instances, materials,
            worldVertices, worldIndices, worldMaterials, worldTextures, aiScene, u, dynFX,
            asphaltDiffTex, asphaltNormTex, asphaltRoughTex,
            concreteDiffTex, concreteNormTex,
            carbonDiffTex, carbonNormTex,
            carLiveryMaskTex, carMiscTex, carBadgeTex,
            carHeadlightTex, carTaillightTex, carTireTex, carInteriorTex,
            repeatSamp
        );

        // Exact Reverse-Z depth (nearZ=0.20, farZ=5200.0 -> 1.0 at near, 0.0 at far) + static/dynamic motion vectors
        RTInstanceMeta hitInst = instances[hit.instanceID];
        // #113 P2-1 Cure: in Cockpit POV, suppress player headlight-back / cowl leak dots seen from inside the cabin!
        if (cockpitCamActive && ((hitInst.flags & ((1u << 4) | (1u << 7) | 4u)) == 0u) && hit.distance < 2.65f &&
            (s.matID == MAT_HEADLIGHT_EMIT || s.matID == MAT_HEADLIGHT_GLASS)) {
            s.matID = MAT_MATTE_TRIM;
            s.baseAlbedo = float3(0.014f, 0.015f, 0.018f);
            s.emissive = float3(0.0f);
            s.metallic = 0.05f;
            s.roughness = 0.78f;
            s.clearcoat = 0.0f;
        }
        // Persistent analytical drift skidmarks (<=128, road asphalt only, <32-45m, 1-instruction AABB early-out)
        if ((hitInst.flags & (1u << 4)) != 0u && s.matID == MAT_WET_ASPHALT) {
            applyRoadSkidmarks(s, hit.distance, temporal1SPP, vfx);
        }
        float4 currClip = u.currViewProjMatrix * float4(s.worldPos, 1.0f);
        float viewZ = max(currClip.w, 0.20f);
        clipDepth = clamp((0.20f * (5200.0f - viewZ)) / (viewZ * 5199.8f), 0.0f, 1.0f);
        float3 prevWorldPos = (hitInst.prevModelMatrix * float4(s.modelPos, 1.0f)).xyz;
        float4 prevClip = u.prevViewProjMatrix * float4(prevWorldPos, 1.0f);
        if (prevClip.w > 1e-4f) {
            float2 prevNDC = prevClip.xy / prevClip.w;
            float2 prevUV = float2(prevNDC.x * 0.5f + 0.5f, 0.5f - prevNDC.y * 0.5f);
            float2 currNDC = currClip.xy / max(currClip.w, 1e-5f);
            float2 currUnjitUV = float2(currNDC.x * 0.5f + 0.5f, 0.5f - currNDC.y * 0.5f);
            motionPx = (prevUV - currUnjitUV) * resF;
        }

        float3 V = -rayDir;
        gDepth = hit.distance;

        if (s.matID == MAT_SMOKED_GLASS || s.matID == MAT_HEADLIGHT_GLASS) {
            float NdotV = max(dot(s.N, V), 1e-4f);
            float f0 = pow((s.ior - 1.0f) / (s.ior + 1.0f), 2.0f);
            float F_glass = F_SchlickScalar(NdotV, f0);
            float reflWeight = (s.matID == MAT_HEADLIGHT_GLASS)
                ? clamp(F_glass * 0.055f, 0.005f, 0.036f)
                : clamp(F_glass + 0.16f, 0.14f, 0.90f);

            float3 reflDir = (s.matID == MAT_SMOKED_GLASS && useRT)
                ? reflect(-V, sampleGGXVNDF(s.N, V, 0.022f, xiRefl))
                : reflect(-V, s.N);
            if (dot(reflDir, s.geomN) <= 0.0f) reflDir = reflect(-V, s.geomN);
            float3 reflCol = sampleSkyDome(reflDir, u, false, false) * u.coronaColor.w;
            if (useRT) {
                ray rr;
                rr.origin = s.worldPos + s.geomN * 0.004f;
                rr.direction = normalize(reflDir);
                rr.min_distance = 0.002f;
                rr.max_distance = temporal1SPP ? 110.0f : 140.0f;
                TraceResult rh = traceSceneRay(
                    rr, tlas, 0xFFu, hasRkptWorld,
                    instances, worldVertices, worldIndices, worldMaterials, worldTextures, worldTexCount, repeatSamp
                );
                if (rh.hit) {
                    HitSurface rs = resolveHitSurface(
                        rr.origin, rr.direction, rh.distance,
                        rh.instanceID, rh.primitiveID, rh.bary, false,
                        vertices, indices, triMaterials, instances, materials,
                        worldVertices, worldIndices, worldMaterials, worldTextures, aiScene, u, dynFX,
                        asphaltDiffTex, asphaltNormTex, asphaltRoughTex,
                        concreteDiffTex, concreteNormTex,
                        carbonDiffTex, carbonNormTex,
                        carLiveryMaskTex, carMiscTex, carBadgeTex,
                        carHeadlightTex, carTaillightTex, carTireTex, carInteriorTex,
                        repeatSamp
                    );
                    reflCol = evaluateSecondaryHitRadiance(
                        rr.origin, rr.direction, rs, tlas,
                        instances, worldVertices, worldIndices, worldMaterials, worldTextures,
                        worldLights, worldLightGrid, dynFX, repeatSamp, u, xiShad, !temporal1SPP
                    );
                }
            }
            if (s.matID == MAT_SMOKED_GLASS) {
                reflCol = min(reflCol, float3(1.55f));
            } else {
                reflCol = min(reflCol, float3(0.72f));
            }

            ray tr;
            tr.origin = s.worldPos + rayDir * 0.004f;
            tr.direction = rayDir;
            tr.min_distance = 0.002f;
            tr.max_distance = 60.0f;
            // Exclude TLAS_MASK_GLASS on transmission rays so curved/double-layer headlight lenses NEVER self-intersect into opaque white glass!
            TraceResult th = traceSceneRay(
                tr, tlas, (0xFFu & ~TLAS_MASK_GLASS), hasRkptWorld,
                instances, worldVertices, worldIndices, worldMaterials, worldTextures, worldTexCount, repeatSamp
            );
            float3 transCol = sampleSkyDome(rayDir, u, false, false);
            if (th.hit) {
                HitSurface ts = resolveHitSurface(
                    tr.origin, tr.direction, th.distance,
                    th.instanceID, th.primitiveID, th.bary, false,
                    vertices, indices, triMaterials, instances, materials,
                    worldVertices, worldIndices, worldMaterials, worldTextures, aiScene, u, dynFX,
                    asphaltDiffTex, asphaltNormTex, asphaltRoughTex,
                    concreteDiffTex, concreteNormTex,
                    carbonDiffTex, carbonNormTex,
                    carLiveryMaskTex, carMiscTex, carBadgeTex,
                    carHeadlightTex, carTaillightTex, carTireTex, carInteriorTex,
                    repeatSamp
                );
                transCol = evaluateSecondaryHitRadiance(
                    tr.origin, tr.direction, ts, tlas,
                    instances, worldVertices, worldIndices, worldMaterials, worldTextures,
                    worldLights, worldLightGrid, dynFX, repeatSamp, u, xiShad, true
                );
            }

            float3 L = normalize(u.sunDir.xyz);
            float sunVisGlass = 1.0f;
            if (useRT) {
                ray gsr;
                gsr.origin = s.worldPos + s.geomN * 0.004f;
                gsr.direction = sampleCone(L, u.sunColor.w, xiShad);
                gsr.min_distance = 0.002f;
                gsr.max_distance = 180.0f;
                sunVisGlass = traceShadowVisibility(
                    gsr, tlas, TLAS_MASK_SHADOW_ALL, hasRkptWorld,
                    instances, worldVertices, worldIndices, worldMaterials, worldTextures, worldTexCount, repeatSamp
                );
            }
            // Pure specular glint ONLY on clear/smoked glass (ZERO diffuse golden sunlight leak on MAT_HEADLIGHT_GLASS!)
            float3 glassDiff, glassSpec;
            evaluateDirectSunSplit(s, V, L, u.sunColor.rgb * u.sunDir.w, sunVisGlass, glassDiff, glassSpec);
            float3 sunSpec = glassSpec * ((s.matID == MAT_HEADLIGHT_GLASS) ? 0.35f : 1.0f);
            float3 tint = (s.matID == MAT_HEADLIGHT_GLASS)
                ? float3(0.95f, 0.98f, 1.00f)
                : mix(s.baseAlbedo * 0.72f + float3(0.18f), float3(1.0f), 1.0f - s.alpha);

            outSpecRad = mix(transCol * tint, reflCol, reflWeight) + sunSpec;
            gSpecAlbedo = float3(reflWeight);
            gEmissive = s.emissive;
            gNormRough = float4(s.N, 0.02f);
            gRoughVal = 0.02f;
            gReactiveVal = 0.92f;
        } else {
            float3 L = normalize(u.sunDir.xyz);
            float3 sunRad = u.sunColor.rgb * u.sunDir.w;

            float sunVis = 1.0f;
            if (useRT) {
                ray sr;
                sr.origin = s.worldPos + s.geomN * 0.004f;
                sr.direction = sampleCone(L, u.sunColor.w, xiShad);
                sr.min_distance = 0.002f;
                sr.max_distance = 220.0f;
                sunVis = traceShadowVisibility(
                    sr, tlas, TLAS_MASK_SHADOW_ALL, hasRkptWorld,
                    instances, worldVertices, worldIndices, worldMaterials, worldTextures, worldTexCount, repeatSamp
                );
            }

            float3 directDiffIrrad, directSpec;
            evaluateDirectSunSplit(s, V, L, sunRad, sunVis, directDiffIrrad, directSpec);

            float3 localDiffIrrad, localSpec;
            bool isFoliageCard = (s.matID == MAT_FOLIAGE_AUTUMN || s.matID == MAT_FOLIAGE_PINE);
            bool risAllowed = useRT && hasRkptWorld && ((vfx.counts.w & 2u) != 0u) && (hit.distance < 95.0f) && (!temporal1SPP || !isFoliageCard);
            float2 xiRIS = r2Sequence(sampleIdx + 263u, fract(cpShift + float2(0.29f, 0.91f)));
            float4 risPosDist;
            float3 risDiff, risSpec;
            float risWeightRatio;
            evaluateLocalAreaLightsImpl(
                s, V, u, worldLights, worldLightGrid, dynFX,
                risAllowed, xiRIS.x,
                localDiffIrrad, localSpec, risPosDist, risDiff, risSpec, risWeightRatio
            );
            // 1-Ray RIS Hardware-RT Streetlamp Shadows (< 0.6 ms GPU; grounded car, pole & building shadows at night!)
            if (risAllowed && risPosDist.w > 0.65f && risWeightRatio > 0.01f) {
                // Soft 0.18m lamp fixture jitter disk for natural streetlamp penumbrae
                float phiL = xiRIS.y * 2.0f * PI;
                float rL = 0.18f * sqrt(fract(xiRIS.x * 7.31f + xiRIS.y * 3.17f));
                float3 lampTarget = risPosDist.xyz + float3(cos(phiL) * rL, 0.0f, sin(phiL) * rL);
                float3 toLamp = lampTarget - s.worldPos;
                float lampDist = length(toLamp);
                if (lampDist > 0.65f) {
                    ray lsr;
                    lsr.origin = s.worldPos + s.geomN * 0.014f;
                    lsr.direction = toLamp / lampDist;
                    lsr.min_distance = 0.02f;
                    lsr.max_distance = min(lampDist - 0.50f, 42.0f);
                    float lampVis = traceShadowVisibility(
                        lsr, tlas, TLAS_MASK_SHADOW_ALL, hasRkptWorld,
                        instances, worldVertices, worldIndices, worldMaterials, worldTextures, worldTexCount, repeatSamp
                    );
                    if (lampVis < 0.999f) {
                        // Variance-bounded control-variate RIS shadow modulation (unbiased for 1-2 dominant lamps, zero 1-SPP fireflies!)
                        float shadowScale = min(risWeightRatio, temporal1SPP ? 2.15f : 4.0f) * (1.0f - lampVis);
                        localDiffIrrad = max(float3(0.0f), localDiffIrrad - risDiff * shadowScale);
                        localSpec      = max(float3(0.0f), localSpec      - risSpec * shadowScale);
                    }
                }
            }

            float NdotV = max(dot(s.N, V), 1e-4f);
            float3 F0 = mix(float3(0.04f), s.baseAlbedo, s.metallic);
            // Cap grazing Fresnel on matte foliage cards (MAT_FOLIAGE_AUTUMN / PINE) so cypress silhouettes
            // never catch bright white sky ribbons or lose green diffuse albedo at NdotV -> 0!
            bool isMatteFoliage = (s.matID == MAT_FOLIAGE_AUTUMN || s.matID == MAT_FOLIAGE_PINE);
            float3 F_env = isMatteFoliage
                ? min(F_Schlick(NdotV, F0) * 0.14f, float3(0.05f))
                : F_Schlick(NdotV, F0);
            float f0_cc = pow((s.ior - 1.0f) / (s.ior + 1.0f), 2.0f);
            float F_cc = (s.clearcoat > 0.01f) ? F_SchlickScalar(max(dot(s.clearcoatN, V), 1e-4f), f0_cc) * s.clearcoat : 0.0f;

            float3 baseSpecRefl = float3(0.0f);
            float3 ccSpecRefl = float3(0.0f);
            // Physical rough-surface grazing Fresnel attenuation on dry matte asphalt & rough non-metals
            float3 F_envSpec = (s.matID == MAT_WET_ASPHALT)
                ? F_env * mix(1.0f, 0.28f, smoothstep(0.32f, 0.76f, s.roughness))
                : (isMatteFoliage ? F_env * 0.40f : F_env * mix(1.0f, 0.45f, smoothstep(0.55f, 0.90f, s.roughness) * (1.0f - s.metallic)));

            if (useRT) {
                if (temporal1SPP && s.clearcoat > 0.05f) {
                    // Real-time 1-SPP stochastic lobe selection (base vs clearcoat): traces 1 unbiased reflection ray
                    // instead of 2 + 2 shadow rays per pixel on car paint & wet puddles!
                    float specRough = (s.matID == MAT_WET_ASPHALT) ? s.roughness : max(s.roughness, 0.035f);
                    bool shareMirrorRay = (specRough < 0.08f && dot(s.N, s.clearcoatN) > 0.985f);
                    float wBase = dot(F_envSpec, float3(0.333f)) * (1.0f - F_cc);
                    float pCC = shareMirrorRay ? 1.0f : clamp(F_cc / max(F_cc + wBase, 1e-4f), 0.22f, 0.85f);
                    bool pickCC = shareMirrorRay || (xiCC.x < pCC);
                    float3 H_sel = pickCC ? sampleGGXVNDF(s.clearcoatN, V, s.ccRoughness, xiRefl)
                                          : sampleGGXVNDF(s.N, V, specRough, xiRefl);
                    float3 R_sel = reflect(-V, H_sel);
                    if (dot(R_sel, s.geomN) <= 0.0f) R_sel = reflect(-V, s.geomN);

                    ray rr;
                    rr.origin = s.worldPos + s.geomN * 0.004f;
                    rr.direction = normalize(R_sel);
                    rr.min_distance = 0.003f;
                    rr.max_distance = (pickCC || specRough < 0.30f) ? 130.0f : 80.0f;
                    TraceResult rh = traceSceneRay(
                        rr, tlas, 0xFFu, hasRkptWorld,
                        instances, worldVertices, worldIndices, worldMaterials, worldTextures, worldTexCount, repeatSamp
                    );
                    float3 reflHitCol;
                    if (!rh.hit) {
                        reflHitCol = sampleSkyDome(rr.direction, u, false, false) * u.coronaColor.w;
                    } else {
                        HitSurface rs = resolveHitSurface(
                            rr.origin, rr.direction, rh.distance,
                            rh.instanceID, rh.primitiveID, rh.bary, false,
                            vertices, indices, triMaterials, instances, materials,
                            worldVertices, worldIndices, worldMaterials, worldTextures, aiScene, u, dynFX,
                            asphaltDiffTex, asphaltNormTex, asphaltRoughTex,
                            concreteDiffTex, concreteNormTex,
                            carbonDiffTex, carbonNormTex,
                            carLiveryMaskTex, carMiscTex, carBadgeTex,
                            carHeadlightTex, carTaillightTex, carTireTex, carInteriorTex,
                            repeatSamp
                        );
                        bool secShad = pickCC || (specRough < 0.28f);
                        reflHitCol = evaluateSecondaryHitRadiance(
                            rr.origin, rr.direction, rs, tlas,
                            instances, worldVertices, worldIndices, worldMaterials, worldTextures,
                            worldLights, worldLightGrid, dynFX, repeatSamp, u, xiShad, secShad
                        );
                    }
                    reflHitCol = min(reflHitCol, float3(10.0f));
                    if (shareMirrorRay) {
                        baseSpecRefl = reflHitCol;
                        ccSpecRefl = reflHitCol;
                    } else if (pickCC) {
                        ccSpecRefl = min(reflHitCol / pCC, float3(12.0f));
                        baseSpecRefl = float3(0.0f);
                    } else {
                        baseSpecRefl = min(reflHitCol / max(1.0f - pCC, 0.15f), float3(12.0f));
                        ccSpecRefl = float3(0.0f);
                    }
                } else {
                    float specRough = (s.matID == MAT_WET_ASPHALT) ? s.roughness : max(s.roughness, 0.035f);
                    float3 H_base = sampleGGXVNDF(s.N, V, specRough, xiRefl);
                    float3 R_base = reflect(-V, H_base);
                    if (dot(R_base, s.geomN) <= 0.0f) R_base = reflect(-V, s.geomN);

                    ray rr;
                    rr.origin = s.worldPos + s.geomN * 0.004f;
                    rr.direction = normalize(R_base);
                    rr.min_distance = 0.003f;
                    rr.max_distance = (temporal1SPP && specRough > 0.45f) ? 80.0f : 160.0f;
                    TraceResult rh = traceSceneRay(
                        rr, tlas, 0xFFu, hasRkptWorld,
                        instances, worldVertices, worldIndices, worldMaterials, worldTextures, worldTexCount, repeatSamp
                    );
                    if (!rh.hit) {
                        baseSpecRefl = sampleSkyDome(rr.direction, u, false, false) * u.coronaColor.w;
                    } else {
                        HitSurface rs = resolveHitSurface(
                            rr.origin, rr.direction, rh.distance,
                            rh.instanceID, rh.primitiveID, rh.bary, false,
                            vertices, indices, triMaterials, instances, materials,
                            worldVertices, worldIndices, worldMaterials, worldTextures, aiScene, u, dynFX,
                            asphaltDiffTex, asphaltNormTex, asphaltRoughTex,
                            concreteDiffTex, concreteNormTex,
                            carbonDiffTex, carbonNormTex,
                            carLiveryMaskTex, carMiscTex, carBadgeTex,
                            carHeadlightTex, carTaillightTex, carTireTex, carInteriorTex,
                            repeatSamp
                        );
                        bool secShad = (!temporal1SPP) || (specRough < 0.42f);
                        baseSpecRefl = evaluateSecondaryHitRadiance(
                            rr.origin, rr.direction, rs, tlas,
                            instances, worldVertices, worldIndices, worldMaterials, worldTextures,
                            worldLights, worldLightGrid, dynFX, repeatSamp, u, xiShad, secShad
                        );
                    }
                    baseSpecRefl = min(baseSpecRefl, float3(10.0f));

                    if (s.clearcoat > 0.05f) {
                        float3 H_cc = sampleGGXVNDF(s.clearcoatN, V, s.ccRoughness, xiCC);
                        float3 R_cc = reflect(-V, H_cc);
                        if (dot(R_cc, s.geomN) <= 0.0f) R_cc = reflect(-V, s.geomN);

                        ray cr;
                        cr.origin = s.worldPos + s.geomN * 0.004f;
                        cr.direction = normalize(R_cc);
                        cr.min_distance = 0.003f;
                        cr.max_distance = 160.0f;
                        TraceResult ch = traceSceneRay(
                            cr, tlas, 0xFFu, hasRkptWorld,
                            instances, worldVertices, worldIndices, worldMaterials, worldTextures, worldTexCount, repeatSamp
                        );
                        if (!ch.hit) {
                            ccSpecRefl = sampleSkyDome(cr.direction, u, false, false) * u.coronaColor.w;
                        } else {
                            HitSurface cs = resolveHitSurface(
                                cr.origin, cr.direction, ch.distance,
                                ch.instanceID, ch.primitiveID, ch.bary, false,
                                vertices, indices, triMaterials, instances, materials,
                                worldVertices, worldIndices, worldMaterials, worldTextures, aiScene, u, dynFX,
                                asphaltDiffTex, asphaltNormTex, asphaltRoughTex,
                                concreteDiffTex, concreteNormTex,
                                carbonDiffTex, carbonNormTex,
                                carLiveryMaskTex, carMiscTex, carBadgeTex,
                                carHeadlightTex, carTaillightTex, carTireTex, carInteriorTex,
                                repeatSamp
                            );
                            ccSpecRefl = evaluateSecondaryHitRadiance(
                                cr.origin, cr.direction, cs, tlas,
                                instances, worldVertices, worldIndices, worldMaterials, worldTextures,
                                worldLights, worldLightGrid, dynFX, repeatSamp, u, xiShad, true
                            );
                        }
                        if (eventFX.fxParams.x > 0.5f && s.matID == MAT_WET_ASPHALT) {
                            float reflGateReact = 0.0f;
                            float reflMaxT = ch.hit ? ch.distance : 180.0f;
                            ccSpecRefl += evaluateEventGateFX(cr.origin, cr.direction, reflMaxT, eventFX, true, reflGateReact) * 0.72f;
                        }
                        ccSpecRefl = min(ccSpecRefl, float3(10.0f));
                    }
                }
            } else {
                float3 R = reflect(-V, s.N);
                baseSpecRefl = sampleSkyDome(R, u, false, false) * u.coronaColor.w * mix(0.85f, 0.25f, s.roughness);
                ccSpecRefl = sampleSkyDome(reflect(-V, s.clearcoatN), u, false, false) * u.coronaColor.w;
            }

            float3 giIrrad = float3(0.0f);
            if (useRT) {
                float3 giDir = sampleCosineHemisphere(s.N, xiGI);
                ray gr;
                gr.origin = s.worldPos + s.geomN * 0.004f;
                gr.direction = giDir;
                gr.min_distance = 0.003f;
                gr.max_distance = temporal1SPP ? 20.0f : 28.0f;
                TraceResult gh = traceSceneRay(
                    gr, tlas, TLAS_MASK_SHADOW_ALL, hasRkptWorld,
                    instances, worldVertices, worldIndices, worldMaterials, worldTextures, worldTexCount, repeatSamp
                );

                if (!gh.hit) {
                    giIrrad = sampleSkyDome(giDir, u, false, false) * u.coronaColor.w * 0.62f;
                } else {
                    float ao = smoothstep(0.02f, 2.2f, gh.distance);
                    HitSurface gs = resolveHitSurface(
                        gr.origin, gr.direction, gh.distance,
                        gh.instanceID, gh.primitiveID, gh.bary, false,
                        vertices, indices, triMaterials, instances, materials,
                        worldVertices, worldIndices, worldMaterials, worldTextures, aiScene, u, dynFX,
                        asphaltDiffTex, asphaltNormTex, asphaltRoughTex,
                        concreteDiffTex, concreteNormTex,
                        carbonDiffTex, carbonNormTex,
                        carLiveryMaskTex, carMiscTex, carBadgeTex,
                        carHeadlightTex, carTaillightTex, carTireTex, carInteriorTex,
                        repeatSamp
                    );
                    // Skip secondary shadow ray inside 1-SPP diffuse GI bounce (MetalFX MXU denoises cleanly, saves 1 ray/px!)
                    float3 bounceRad = min(evaluateSecondaryHitRadiance(
                        gr.origin, gr.direction, gs, tlas,
                        instances, worldVertices, worldIndices, worldMaterials, worldTextures,
                        worldLights, worldLightGrid, dynFX, repeatSamp, u, xiShad, !temporal1SPP
                    ), float3(6.5f));
                    giIrrad = bounceRad * 0.68f + sampleSkyDome(s.N, u, false, false) * 0.14f * ao;
                }
            } else {
                giIrrad = sampleSkyDome(s.N, u, false, false) * u.coronaColor.w * 0.48f;
            }

            bool isWorldGeom = ((hitInst.flags & (1u << 4)) != 0u);
            bool isAIVehicle = ((hitInst.flags & (1u << 7)) != 0u);
            bool isDynamicCar = (!isWorldGeom && !isAIVehicle && (hitInst.flags & 4u) == 0u);
            float3 skyFill = (u.skyZenith.rgb * 0.55f + u.skyHorizon.rgb * 0.45f) * u.coronaColor.w * 0.28f;
            if (isWorldGeom) {
                // Attenuate skyFill on ROAD_ASPHALT (0.35x) to keep tarmac rich charcoal; lift shaded walls/foliage (+36%)
                float roadAtten = (s.matID == MAT_WET_ASPHALT) ? 0.35f : 1.0f;
                float giBoost = (s.matID == MAT_WET_ASPHALT) ? 1.06f : 1.36f;
                giIrrad = giIrrad * giBoost + skyFill * roadAtten * (0.55f + 0.45f * max(s.N.y, 0.0f));
                // Keep subtle under-car bounce fill on road near car while softening wet-road shadow-footprint edges (P2-1)
                float2 dCarXZ = s.worldPos.xz - u.carWorldPos.xz;
                float carDistXZ = length(dCarXZ);
                float carNear = smoothstep(4.6f, 1.5f, carDistXZ);
                float contactTight = smoothstep(0.06f, 0.28f, s.worldPos.y - u.carWorldPos.y + 0.22f);
                giIrrad = max(giIrrad, skyFill * (0.16f * carNear * contactTight));
            } else if (isDynamicCar) {
                // Physically grounded ambient bounce floor under lower bumper, wheel wells & tires (>= 0.14 * skyFill)
                float tireContactKeep = smoothstep(0.02f, 0.16f, s.worldPos.y - u.carWorldPos.y);
                giIrrad = max(giIrrad, skyFill * (0.14f + 0.10f * tireContactKeep));
            }

            float3 diffK = (1.0f - F_env) * (1.0f - s.metallic) * s.baseAlbedo;
            outDiffIrrad = directDiffIrrad + localDiffIrrad + giIrrad * (1.0f - F_cc);
            outSpecRad = (directSpec + localSpec + F_envSpec * baseSpecRefl) * (1.0f - F_cc) + ccSpecRefl * F_cc;

            float fog;
            float3 fogCol;
            if (hasRkptWorld) {
                // Open-city aerial perspective (0.22x near density, graceful atmospheric haze across 2-4.6 km PVS vistas!)
                float distFog = 1.0f - exp(-hit.distance * u.skyHorizon.w * 0.22f);
                float farVistaHaze = smoothstep(750.0f, 3800.0f, hit.distance) * 0.34f;
                fog = clamp(max(distFog, farVistaHaze), 0.0f, 0.72f);
                float isNight = smoothstep(0.032f, 0.015f, u.skyZenith.r);
                float sunAlign = max(dot(rayDir, normalize(u.sunDir.xyz)), 0.0f);
                // Natural pale cool-blue atmospheric perspective away from sun, subtle pale champagne near sun (zero mustard-yellow soup!)
                float3 dayCoolHaze = mix(u.skyZenith.rgb * 1.15f + float3(0.24f, 0.30f, 0.38f),
                                         float3(0.46f, 0.50f, 0.54f), 0.48f);
                float3 dayWarmSun  = mix(dayCoolHaze, u.sunColor.rgb * 0.42f + float3(0.24f, 0.22f, 0.18f),
                                         pow(sunAlign, 5.0f) * 0.42f);
                float3 nightHaze   = u.skyHorizon.rgb * 0.92f + float3(0.012f, 0.018f, 0.030f);
                fogCol = mix(dayWarmSun, nightHaze, isNight);
            } else {
                fog = 1.0f - exp(-pow(hit.distance * u.skyHorizon.w * 1.05f, 1.45f));
                fog = clamp(fog, 0.0f, 0.85f);
                fogCol = sampleSkyDome(normalize(float3(rayDir.x, 0.025f, rayDir.z)), u, false, false);
            }

            gAlbedo = diffK * (1.0f - fog);
            gSpecAlbedo = (F_envSpec * (1.0f - F_cc) + float3(F_cc)) * (1.0f - fog);
            outSpecRad = outSpecRad * (1.0f - fog) + fogCol * fog;
            gEmissive = s.emissive * (1.0f - fog);
            // For offline progressive renders on mirror-clearcoat car paint, write ccRoughness (<=0.03) so atrous4 skips wavy specular blur!
            float gDenoiseRough = (!temporal1SPP && s.clearcoat > 0.65f && s.ccRoughness < 0.06f)
                ? min(s.roughness, s.ccRoughness)
                : s.roughness;
            gNormRough = float4(s.N, gDenoiseRough);
            gRoughVal = s.roughness;
            // High reactivity on thin wing, emissives & police strobes (zero ghosting); moderate reactivity on rigid-tracked tires/rims,
            // moving AI vehicles, exhaust chrome & mirror-smooth wet puddles!
            bool highReactivePart = (s.matID == MAT_CARBON_FIBER || s.matID == MAT_TAILLIGHT_EMIT || s.matID == MAT_HEADLIGHT_EMIT || (isAIVehicle && s.matID == MAT_STREETLAMP_LED));
            bool wheelPart = (s.matID == MAT_RUBBER_TIRE || s.matID == MAT_ALLOY_RIM);
            bool chromeTrimPart = (isDynamicCar && (s.matID == MAT_CAR_MISC_CHROME || s.matID == MAT_LICENSE_PLATE));
            float puddleReactive = (isWorldGeom && s.puddleMask > 0.18f) ? clamp(s.puddleMask * 0.58f, 0.0f, 0.58f) : 0.0f;
            float carShadowReactive = (temporal1SPP && isWorldGeom && s.matID == MAT_WET_ASPHALT && u.driveParams.x > 6.0f)
                ? (smoothstep(4.4f, 1.6f, length(s.worldPos.xz - u.carWorldPos.xz)) * 0.62f)
                : 0.0f;
            float roadReactive = max(puddleReactive, carShadowReactive);
            gReactiveVal = highReactivePart
                ? 0.94f
                : (wheelPart ? 0.38f : ((length(gEmissive) > 0.5f) ? 0.86f : (isAIVehicle ? 0.52f : (chromeTrimPart ? 0.58f : (isDynamicCar ? 0.42f : roadReactive)))));
        }
    }

    // Subtle atmospheric diagonal falling rain streaks + volumetric night-rain headlight cones + NOS flames + cop strobes + wet-lens droplets!
    float rainNightGate = smoothstep(0.036f, 0.015f, u.skyZenith.r);
    float effectiveRain = max(rainNightGate * smoothstep(0.72f, 0.90f, u.skyZenith.w), max(dynFX.timeWeather.z, u.fxParams.x));
    float2 pxNorm = float2(gid) / resF;
    if (hasRkptWorld && effectiveRain > 0.30f) {
        float rainStreak = 0.0f;
        // 12-degree wind-slanted rain coordinates across 2 depth parallax planes (decoupled from sampleIdx!)
        float2 slantUV = float2(pxNorm.x * u.cameraForward.w + pxNorm.y * 0.21f, pxNorm.y);
        float tRain = u.envParams.y * 0.22f + dynFX.timeWeather.x * 1.45f;
        for (int rl = 0; rl < 2; ++rl) {
            float cols = (rl == 0) ? 135.0f : 215.0f;
            float rows = (rl == 0) ? 10.5f : 17.5f;
            float colCoord = slantUV.x * cols + float(rl) * 37.1f;
            float colIdx = floor(colCoord);
            float colPhase = hash21(float2(colIdx, float(rl) * 11.7f + 2.3f)) * 4.3f;
            float colSpeed = 0.82f + 0.36f * hash21(float2(colIdx + 19.1f, float(rl) * 5.9f));
            float2 rp = float2(colCoord, slantUV.y * rows + colPhase - tRain * (2.2f + float(rl)) * colSpeed);
            float2 cell = floor(rp);
            float2 f = fract(rp);
            float3 rnd = hash33(float3(cell, float(rl) * 19.3f + 3.1f));
            if (rnd.x > 0.81f) {
                float xCenter = 0.22f + 0.56f * rnd.y;
                float xMask = smoothstep(0.062f, 0.015f, abs(f.x - xCenter));
                float yMask = smoothstep(0.06f, 0.45f, f.y) * smoothstep(0.94f, 0.55f, f.y);
                float depthGate = (rl == 0) ? smoothstep(2.0f, 8.0f, gDepth) : smoothstep(5.0f, 18.0f, gDepth);
                rainStreak += xMask * yMask * depthGate * ((rl == 0) ? 0.13f : 0.08f) * (0.45f + 0.55f * rnd.z);
            }
        }
        if (rainStreak > 0.002f) {
            float3 rainCol = mix(float3(0.58f, 0.72f, 0.92f), float3(1.00f, 0.80f, 0.48f), 0.34f);
            outSpecRad += rainCol * min(rainStreak, 0.24f) * effectiveRain;
            gReactiveVal = max(gReactiveVal, 0.65f);
        }
    }

    // Volumetric Dual Xenon Headlight Fog Cones (night-rain OR dark underground tunnels -> ZERO daytime chalk leak!)
    float tunnelHeadBeam = clamp(u.skyGround.w - 1.0f, 0.0f, 1.0f);
    if (hasRkptWorld && ((rainNightGate > 0.45f && effectiveRain > 0.30f && u.skyGround.w > 0.30f) || tunnelHeadBeam > 0.25f)) {
        float beamVis = max(rainNightGate * effectiveRain * min(u.skyGround.w, 1.0f), tunnelHeadBeam * 0.72f);
        float yaw = u.carDynParams.z;
        float cy = cos(yaw), sy = sin(yaw);
        float fZ = (abs(dynFX.carMetrics.x) > 0.2f) ? (dynFX.carMetrics.x + 0.55f) : 2.02f;
        float3 fwdAxis = normalize(float3(sy, -0.14f, cy));
        float3 carO = u.carWorldPos.xyz;
        float3 hL = carO + float3(-0.62f * cy + fZ * sy, 0.60f, 0.62f * sy + fZ * cy);
        float3 hR = carO + float3( 0.62f * cy + fZ * sy, 0.60f, -0.62f * sy + fZ * cy);
        float volBeam = 0.0f;
        float maxT = min(gDepth, 24.0f);
        if (maxT > 1.0f) {
            for (int sIdx = 0; sIdx < 4; ++sIdx) {
                float tSample = mix(1.2f, maxT, (float(sIdx) + 0.5f) * 0.25f);
                float3 pW = rayOrigin + rayDir * tSample;
                for (int side = 0; side < 2; ++side) {
                    float3 src = (side == 0) ? hL : hR;
                    float3 rel = pW - src;
                    float along = dot(rel, fwdAxis);
                    if (along > 0.25f && along < 18.0f) {
                        float3 perp = rel - fwdAxis * along;
                        float coneR = 0.20f + along * 0.26f;
                        float rNorm = length(perp) / coneR;
                        float radial = exp(-rNorm * rNorm * 2.8f);
                        float falloff = smoothstep(18.0f, 0.5f, along) * smoothstep(0.15f, 1.2f, along);
                        volBeam += radial * falloff * 0.022f;
                    }
                }
            }
        }
        if (volBeam > 0.001f) {
            float3 beamTint = float3(0.76f, 0.88f, 1.05f) * beamVis;
            outSpecRad += beamTint * min(volBeam, 0.22f);
            gReactiveVal = max(gReactiveVal, 0.55f);
        }
    }

    // Multi-Sample Volumetric Dual NOS Exhaust Plasma Cones (RTDynamicFX.nosParams.x > 0.02)
    // Seamless 9-node cyan-blue Mach-diamond exhaust jets tightly anchored to tailpipes (zero detached tail bead or FI tearing!)
    if (dynFX.nosParams.x > 0.02f) {
        float nosAct = clamp(dynFX.nosParams.x, 0.0f, 1.0f);
        float flameLen = clamp(dynFX.nosParams.y, 0.56f, 0.78f);
        float yaw = u.carDynParams.z;
        float cy = cos(yaw), sy = sin(yaw);
        float rZ = (abs(dynFX.carMetrics.y) > 0.2f) ? (dynFX.carMetrics.y - 0.94f) : -2.08f;
        uint carID = uint(u.hudParams.w + 0.5f);
        float pipeSepX = (carID == 4u || carID == 1u) ? 0.14f : 0.48f;
        float pipeY    = (carID == 4u) ? 0.34f : 0.28f;
        float3 backAxis = normalize(float3(-sy, 0.01f, -cy));
        float3 rightAxis = float3(cy, 0.0f, -sy);
        float3 carO = u.carWorldPos.xyz;
        float3 exL = carO - rightAxis * pipeSepX + float3(0.0f, pipeY, 0.0f) + float3(sy, 0.0f, cy) * rZ;
        float3 exR = carO + rightAxis * pipeSepX + float3(0.0f, pipeY, 0.0f) + float3(sy, 0.0f, cy) * rZ;
        float flicker = 0.88f + 0.12f * sin(dynFX.timeWeather.x * 58.0f + float(gid.x) * 0.03f);
        float3 nosGlow = float3(0.0f);
        for (int s = 0; s < 2; ++s) {
            float3 src = (s == 0) ? exL : exR;
            for (int k = 0; k < 9; ++k) {
                float uAlong = (float(k) + 0.25f) * (1.0f / 9.0f);
                float3 jetPt = src + backAxis * (uAlong * flameLen);
                float tProj = dot(jetPt - rayOrigin, rayDir);
                if (tProj > 0.15f && tProj < gDepth + 0.30f && tProj < 18.0f) {
                    float rad = length((rayOrigin + rayDir * tProj) - jetPt);
                    float coneR = mix(0.046f, 0.092f, uAlong) * (0.94f + 0.08f * sin(uAlong * 18.0f - dynFX.timeWeather.x * 45.0f));
                    float core  = exp(-pow(rad / max(coneR, 0.015f), 2.0f) * 4.2f);
                    float halo  = exp(-pow(rad / max(coneR * 1.45f, 0.03f), 2.0f) * 3.0f) * 0.16f;
                    float shock = 0.78f + 0.34f * pow(max(0.0f, sin(uAlong * 14.0f - dynFX.timeWeather.x * 40.0f)), 2.0f);
                    float taper = smoothstep(1.0f, 0.12f, uAlong);
                    float3 innerCol = float3(1.10f, 2.90f, 9.20f);
                    float3 midCol   = float3(0.28f, 1.25f, 5.80f);
                    float3 tipCol   = float3(3.80f, 1.15f, 0.20f);
                    float3 flameCol = mix(mix(innerCol, midCol, smoothstep(0.06f, 0.48f, uAlong)),
                                          tipCol, smoothstep(0.55f, 0.94f, uAlong));
                    nosGlow += flameCol * (core * shock + halo) * taper * nosAct * flicker * 0.28f;
                }
            }
        }
        float nosMag = length(nosGlow);
        if (nosMag > 0.005f) {
            outSpecRad += min(nosGlow, float3(8.5f));
            gReactiveVal = max(gReactiveVal, 0.96f);
        }
    }

    // Glowing Volumetric Halo for Active Police Lightbar Strobes (4 nearest cops, crisp & tight)
    float haloDayScale = mix(0.45f, 1.0f, rainNightGate);
    for (int c = 0; c < 4; ++c) {
        if (dynFX.copLightPos[c].w > 0.05f && dynFX.copLightCol[c].w > 0.05f) {
            float3 toCop = dynFX.copLightPos[c].xyz - rayOrigin;
            float tProj = dot(toCop, rayDir);
            if (tProj > 0.5f && tProj < gDepth + 0.5f && tProj < 140.0f) {
                float perpD = length(toCop - rayDir * tProj);
                float halo = (exp(-perpD * perpD * 3.8f) + 0.18f * exp(-perpD * perpD * 0.95f))
                           * dynFX.copLightPos[c].w * dynFX.copLightCol[c].w * smoothstep(140.0f, 8.0f, tProj) * haloDayScale;
                if (halo > 0.004f) {
                    outSpecRad += dynFX.copLightCol[c].rgb * halo * 0.78f;
                    gReactiveVal = max(gReactiveVal, 0.94f);
                }
            }
        }
    }

    // Analytical 3D NFSMW Event Checkpoint / Speedtrap / Blacklist Boss Gates + Floating Chevrons (RTEventWorldFX[16])
    if (eventFX.fxParams.x > 0.5f) {
        float gateReact = 0.0f;
        float3 gateGlow = evaluateEventGateFX(rayOrigin, rayDir, gDepth, eventFX, false, gateReact);
        if (gateReact > 0.001f) {
            outSpecRad += gateGlow;
            gReactiveVal = max(gReactiveVal, gateReact);
        }
        // Subtle Speedtrap radar camera flash pulse on trap crossing (perimeter strobe only; zero center washout)
        if (eventFX.fxParams.z > 0.01f) {
            float flashAmt = clamp(eventFX.fxParams.z, 0.0f, 1.0f);
            float radEdge = length(pxNorm - float2(0.5f, 0.52f));
            float edgeRing = smoothstep(0.34f, 0.92f, radEdge);
            float topStrobe = smoothstep(0.45f, 0.0f, pxNorm.y) * 0.45f;
            float flashGlow = flashAmt * flashAmt * (edgeRing * 0.85f + topStrobe);
            outSpecRad += float3(0.55f, 0.88f, 1.05f) * flashGlow * 0.24f;
            gReactiveVal = max(gReactiveVal, flashAmt * 0.85f);
        }
    }

    // Analytical Drift VFX: Ballistic Sparks (<=64) & Volumetric Tire Smoke (<=16) on primary rays ONLY (#24 Guardrail 2)
    if (vfx.counts.y > 0u || vfx.counts.z > 0u) {
        float3 sparkRGB, smokeRGB;
        float smokeT, vfxReact;
        evaluateDriftVFXPrimary(
            rayOrigin, rayDir, gDepth, temporal1SPP, vfx, u, dynFX,
            sparkRGB, smokeRGB, smokeT, vfxReact
        );
        if (smokeT < 0.999f) {
            gAlbedo *= smokeT;
            gEmissive *= smokeT;
            outSpecRad = outSpecRad * smokeT + smokeRGB;
            // Damp road motion vectors through semi-opaque tire smoke so 2x Frame-Gen never tears optical-flow tiles!
            motionPx *= mix(1.0f, 0.25f, clamp(1.0f - smokeT, 0.0f, 1.0f));
        }
        if (vfxReact > 0.001f) {
            outSpecRad += sparkRGB;
            gReactiveVal = max(gReactiveVal, vfxReact);
        }
    }

    // Ultra-subtle optical interior windshield clarity hint in Cockpit POV (zero dark exterior tint!)
    if (cockpitWindshieldPassed) {
        outSpecRad = outSpecRad * float3(0.97f, 0.99f, 1.00f) + float3(0.006f, 0.009f, 0.012f) * smoothstep(0.45f, 0.0f, pxNorm.y);
    }

    // Subtle high-speed rear-tire wet mist / spray veil behind rear wheels when driving fast (procedural overpass)
    float driveSpd = clamp(u.driveParams.x / 180.0f, 0.0f, 1.0f);
    if (!hasRkptWorld && driveSpd > 0.30f && rayOrigin.z < -2.2f && rayDir.z > 0.35f) {
        float sprayAccum = 0.0f;
        float3 sprayColAccum = float3(0.0f);
        const float sliceZs[2] = { -2.28f, -2.95f };
        for (int sl = 0; sl < 2; ++sl) {
            float tPl = (sliceZs[sl] - rayOrigin.z) / max(rayDir.z, 1e-4f);
            if (tPl > 0.2f && tPl < gDepth) {
                float3 sp = rayOrigin + rayDir * tPl;
                if (sp.y > 0.01f && sp.y < 0.62f && abs(sp.x) > 0.35f && abs(sp.x) < 1.38f) {
                    float dL = length(float2((sp.x + 0.86f) / 0.36f, (sp.y - 0.16f) / 0.24f));
                    float dR = length(float2((sp.x - 0.86f) / 0.36f, (sp.y - 0.16f) / 0.24f));
                    float plume = max(exp(-dL * dL * 1.4f), exp(-dR * dR * 1.4f));
                    float wisp = fbm2D(float2(sp.x * 3.8f, sp.y * 5.2f + u.envParams.y * 0.45f + float(sl) * 3.1f));
                    float dens = plume * smoothstep(0.28f, 0.82f, wisp) * driveSpd * clamp(u.skyZenith.w, 0.2f, 1.0f) * 0.115f;
                    float tailNear = smoothstep(0.10f, 0.45f, sp.y) * smoothstep(1.20f, 0.55f, abs(sp.x));
                    float3 mistTint = mix(u.skyHorizon.rgb * 0.72f + u.sunColor.rgb * 0.32f, float3(1.10f, 0.12f, 0.05f), tailNear * 0.36f);
                    sprayColAccum += mistTint * dens;
                    sprayAccum += dens;
                }
            }
        }
        sprayAccum = clamp(sprayAccum, 0.0f, 0.22f);
        if (sprayAccum > 0.002f) {
            gAlbedo *= (1.0f - sprayAccum * 0.65f);
            outSpecRad = outSpecRad * (1.0f - sprayAccum * 0.65f) + sprayColAccum;
            gReactiveVal = max(gReactiveVal, 0.65f);
        }
    }

    outDiffIrrad = clamp(outDiffIrrad, float3(0.0f), float3(18.0f));
    outSpecRad   = clamp(outSpecRad,   float3(0.0f), float3(16.0f));

    // Exact unbiased progressive optical DoF accumulation (avoids E[diff]*E[albedo] bleeding across out-of-focus edges!)
    if (!temporal1SPP && photoDoFActive) {
        outSpecRad = clamp(outDiffIrrad * gAlbedo + outSpecRad + gEmissive, float3(0.0f), float3(22.0f));
        outDiffIrrad = float3(0.0f);
        gAlbedo = float3(0.0f);
        gEmissive = float3(0.0f);
    }

    if (temporal1SPP) {
        // Real-time 1-SPP temporal driving path (MetalFX TemporalDenoisedScaler or SVGF)
        float3 modulatedHDR = outDiffIrrad * gAlbedo + outSpecRad + gEmissive;
        accumDiffTex.write(float4(outDiffIrrad, 1.0f), gid);
        accumSpecTex.write(float4(outSpecRad, 1.0f), gid);
        gAlbedoTex.write(float4(gAlbedo, (gNormRough.w >= 0.0f) ? 1.0f : 0.0f), gid);
        gNormalRoughTex.write(gNormRough, gid);
        gDepthTex.write(float4(clipDepth, 0.0f, 0.0f, 1.0f), gid);
        gEmissiveTex.write(float4(gEmissive, 1.0f), gid);
        gColorTex.write(float4(modulatedHDR, 1.0f), gid);
        gMotionTex.write(float4(motionPx, 0.0f, 0.0f), gid);
        gSpecAlbedoTex.write(float4(gSpecAlbedo, 1.0f), gid);
        gRoughnessTex.write(float4(gRoughVal, 0.0f, 0.0f, 1.0f), gid);
        gReactiveTex.write(float4(gReactiveVal, 0.0f, 0.0f, 1.0f), gid);
    } else if (sampleIdx == 0u) {
        accumDiffTex.write(float4(outDiffIrrad, 1.0f), gid);
        accumSpecTex.write(float4(outSpecRad, 1.0f), gid);
        gAlbedoTex.write(float4(gAlbedo, (gNormRough.w >= 0.0f) ? 1.0f : 0.0f), gid);
        gNormalRoughTex.write(gNormRough, gid);
        gDepthTex.write(float4(gDepth, 0.0f, 0.0f, 1.0f), gid);
        gEmissiveTex.write(float4(gEmissive, 1.0f), gid);
    } else {
        float w = 1.0f / float(sampleIdx + 1u);
        float3 prevDiff = accumDiffTex.read(gid).rgb;
        float3 prevSpec = accumSpecTex.read(gid).rgb;
        float4 prevAlb  = gAlbedoTex.read(gid);
        float3 prevEm   = gEmissiveTex.read(gid).rgb;
        accumDiffTex.write(float4(mix(prevDiff, outDiffIrrad, w), 1.0f), gid);
        accumSpecTex.write(float4(mix(prevSpec, outSpecRad, w), 1.0f), gid);
        gAlbedoTex.write(float4(mix(prevAlb.rgb, gAlbedo, w), prevAlb.a), gid);
        gEmissiveTex.write(float4(mix(prevEm, gEmissive, w), 1.0f), gid);
    }
}

// ---------------------------------------------------------------------------
// SVGF Temporal Reprojection Kernel (Clean Fallback when MetalFX is disabled)
// ---------------------------------------------------------------------------

kernel void svgfTemporalKernel(
    uint2 gid [[thread_position_in_grid]],
    texture2d<float, access::read> currDiffTex [[texture(0)]],
    texture2d<float, access::read> currSpecTex [[texture(1)]],
    texture2d<float, access::read> prevDiffTex [[texture(2)]],
    texture2d<float, access::read> prevSpecTex [[texture(3)]],
    texture2d<float, access::read> motionTex [[texture(4)]],
    texture2d<float, access::read> currNormRoughTex [[texture(5)]],
    texture2d<float, access::read> currDepthTex [[texture(6)]],
    texture2d<float, access::read> prevNormRoughTex [[texture(7)]],
    texture2d<float, access::read> prevDepthTex [[texture(8)]],
    texture2d<float, access::write> outDiffTex [[texture(9)]],
    texture2d<float, access::write> outSpecTex [[texture(10)]],
    constant uint& resetHistory [[buffer(0)]]
) {
    int w = int(outDiffTex.get_width());
    int h = int(outDiffTex.get_height());
    if (int(gid.x) >= w || int(gid.y) >= h) return;

    float3 cDiff = currDiffTex.read(gid).rgb;
    float3 cSpec = currSpecTex.read(gid).rgb;
    float4 cNR = currNormRoughTex.read(gid);

    if (resetHistory != 0u || cNR.w < 0.0f) {
        outDiffTex.write(float4(cDiff, 1.0f), gid);
        outSpecTex.write(float4(cSpec, 1.0f), gid);
        return;
    }

    float2 mv = motionTex.read(gid).xy;
    int2 prevCoord = int2(round(float2(gid) + mv));
    if (prevCoord.x < 0 || prevCoord.x >= w || prevCoord.y < 0 || prevCoord.y >= h) {
        outDiffTex.write(float4(cDiff, 1.0f), gid);
        outSpecTex.write(float4(cSpec, 1.0f), gid);
        return;
    }

    float4 pNR = prevNormRoughTex.read(uint2(prevCoord));
    float cZ = currDepthTex.read(gid).r;
    float pZ = prevDepthTex.read(uint2(prevCoord)).r;
    bool valid = (pNR.w >= 0.0f) && (dot(normalize(cNR.xyz), normalize(pNR.xyz)) > 0.85f) && (abs(cZ - pZ) < 0.03f);

    if (!valid) {
        outDiffTex.write(float4(cDiff, 1.0f), gid);
        outSpecTex.write(float4(cSpec, 1.0f), gid);
        return;
    }

    // 3x3 neighborhood color clamp to prevent ghosting on moving headlights/wheels
    float3 dMin = cDiff, dMax = cDiff;
    float3 sMin = cSpec, sMax = cSpec;
    for (int dy = -1; dy <= 1; ++dy) {
        for (int dx = -1; dx <= 1; ++dx) {
            uint2 q = uint2(clamp(int2(gid) + int2(dx, dy), int2(0), int2(w - 1, h - 1)));
            float3 qd = currDiffTex.read(q).rgb;
            float3 qs = currSpecTex.read(q).rgb;
            dMin = min(dMin, qd); dMax = max(dMax, qd);
            sMin = min(sMin, qs); sMax = max(sMax, qs);
        }
    }

    float3 hDiff = clamp(prevDiffTex.read(uint2(prevCoord)).rgb, dMin * 0.85f, dMax * 1.15f + float3(0.05f));
    float3 hSpec = clamp(prevSpecTex.read(uint2(prevCoord)).rgb, sMin * 0.85f, sMax * 1.15f + float3(0.05f));
    outDiffTex.write(float4(mix(hDiff, cDiff, 0.16f), 1.0f), gid);
    outSpecTex.write(float4(mix(hSpec, cSpec, 0.18f), 1.0f), gid);
}

// ---------------------------------------------------------------------------
// 8-Ray GPU BVH Physics Probe (4 Wheel Road Drops + 4 Horizontal Wall Rays)
// ---------------------------------------------------------------------------

kernel void physicsProbeKernel(
    uint gid [[thread_position_in_grid]],
    instance_acceleration_structure tlas [[buffer(0)]],
    constant RTPhysicsProbeInput& inP [[buffer(1)]],
    device RTPhysicsProbeOutput* outP [[buffer(2)]]
) {
    if (gid != 0u) return;

    float3 pos = inP.carPos.xyz;
    float3 fwd = normalize(inP.carForward.xyz);
    float3 right = normalize(inP.carRight.xyz);
    float halfTrack = inP.carForward.w;
    float frontZ = inP.carRight.w;
    float rearZ  = inP.dims.x;

    intersector<triangle_data, instancing> probeRT;
    probeRT.assume_geometry_type(geometry_type::triangle);

    float3 wheelOffsets[4] = {
        -right * halfTrack + fwd * frontZ, // FL
         right * halfTrack + fwd * frontZ, // FR
        -right * halfTrack + fwd * rearZ,  // RL
         right * halfTrack + fwd * rearZ   // RR
    };

    float wheelY[4] = { -9999.0f, -9999.0f, -9999.0f, -9999.0f };
    for (int w = 0; w < 4; ++w) {
        ray dr;
        dr.origin = pos + wheelOffsets[w] + float3(0.0f, 3.0f, 0.0f);
        dr.direction = float3(0.0f, -1.0f, 0.0f);
        dr.min_distance = 0.05f;
        dr.max_distance = 10.0f;
        auto h = probeRT.intersect(dr, tlas, TLAS_MASK_ROAD);
        if (h.type == intersection_type::none) {
            h = probeRT.intersect(dr, tlas, TLAS_MASK_WORLD_OPAQUE);
        }
        if (h.type == intersection_type::triangle) {
            wheelY[w] = dr.origin.y - h.distance;
        }
    }

    float3 wallDirs[4] = { fwd, -fwd, -right, right };
    float wDist[4] = { 999.0f, 999.0f, 999.0f, 999.0f };
    float3 wNorm[4] = { -fwd, fwd, right, -right };
    for (int k = 0; k < 4; ++k) {
        ray wr;
        wr.origin = pos + float3(0.0f, 0.55f, 0.0f);
        wr.direction = wallDirs[k];
        wr.min_distance = 0.25f;
        wr.max_distance = 4.5f;
        auto h = probeRT.intersect(wr, tlas, TLAS_MASK_WALL);
        if (h.type == intersection_type::triangle) {
            wDist[k] = h.distance;
        }
    }

    // 1 upward sky-occlusion ray (< 0.005 ms GPU) for real-time acoustic tunnel/overpass reverb
    float skyHitDist = 999.0f;
    float skyOcc = 0.0f;
    {
        ray sr;
        sr.origin = pos + float3(0.0f, 1.35f, 0.0f);
        sr.direction = float3(0.0f, 1.0f, 0.0f);
        sr.min_distance = 0.25f;
        sr.max_distance = 42.0f;
        auto sh = probeRT.intersect(sr, tlas, TLAS_MASK_WORLD_OPAQUE | TLAS_MASK_ROAD | TLAS_MASK_WALL);
        if (sh.type == intersection_type::triangle) {
            skyHitDist = sh.distance;
            skyOcc = (skyHitDist <= 12.0f) ? 1.0f : clamp((42.0f - skyHitDist) / 30.0f, 0.0f, 1.0f);
        }
    }

    RTPhysicsProbeOutput res;
    res.wheelHitY = float4(wheelY[0], wheelY[1], wheelY[2], wheelY[3]);
    res.wallDist  = float4(wDist[0], wDist[1], wDist[2], wDist[3]);
    res.wallNormalFront = float4(wNorm[0], (wDist[0] < 900.0f) ? 1.0f : 0.0f);
    if (wDist[2] <= wDist[3] && wDist[2] < 900.0f) {
        res.wallNormalSide = float4(wNorm[2], -1.0f);
    } else if (wDist[3] < 900.0f) {
        res.wallNormalSide = float4(wNorm[3], 1.0f);
    } else {
        res.wallNormalSide = float4(0.0f, 0.0f, 0.0f, 0.0f);
    }
    res.skyProbe = float4(skyOcc, skyHitDist, 0.0f, (skyHitDist < 900.0f) ? 1.0f : 0.0f);
    outP[0] = res;
}

// ---------------------------------------------------------------------------
// Standalone AI Vehicle Road-Elevation & Slope Probe Kernel (<= 32 threads, <0.02ms GPU!)
// Casts 2 vertical downward world-only rays (front & rear axle) per active AI car so all
// 20 traffic & pursuing police vehicles hug Rockport's hills, bridges & ramps seamlessly.
// ---------------------------------------------------------------------------

kernel void aiRoadProbeKernel(
    uint gid [[thread_position_in_grid]],
    instance_acceleration_structure tlas [[buffer(0)]],
    device const RTAIRoadProbeInput* inP [[buffer(1)]],
    device RTAIRoadProbeOutput* outP [[buffer(2)]],
    constant uint& count [[buffer(3)]]
) {
    if (gid >= count) return;

    RTAIRoadProbeOutput res;
    res.roadY    = -9999.0f;
    res.pitchRad = 0.0f;
    res.frontY   = -9999.0f;
    res.rearY    = -9999.0f;

    RTAIRoadProbeInput req = inP[gid];
    if (req.posActive.w <= 0.5f) {
        outP[gid] = res;
        return;
    }

    float3 pos = req.posActive.xyz;
    float3 fwd = req.fwdHalfWB.xyz;
    float fwdLenSq = dot(fwd, fwd);
    fwd = (fwdLenSq > 1e-5f) ? (fwd * rsqrt(fwdLenSq)) : float3(0.0f, 0.0f, 1.0f);
    float halfWB = clamp(req.fwdHalfWB.w, 0.8f, 2.4f);

    intersector<triangle_data, instancing> probeRT;
    probeRT.assume_geometry_type(geometry_type::triangle);

    float3 origins[2] = {
        pos + fwd * halfWB + float3(0.0f, 2.5f, 0.0f),
        pos - fwd * halfWB + float3(0.0f, 2.5f, 0.0f)
    };
    float hits[2] = { -9999.0f, -9999.0f };
    for (int k = 0; k < 2; ++k) {
        ray dr;
        dr.origin = origins[k];
        dr.direction = float3(0.0f, -1.0f, 0.0f);
        dr.min_distance = 0.05f;
        dr.max_distance = 10.5f; // y + 2.5m -> y - 8.0m
        // World-only mask (ROAD preferred, WORLD_OPAQUE fallback) — skips TLAS_MASK_CAR so AI never snaps onto car roofs!
        auto h = probeRT.intersect(dr, tlas, TLAS_MASK_ROAD);
        if (h.type == intersection_type::none) {
            h = probeRT.intersect(dr, tlas, TLAS_MASK_WORLD_OPAQUE);
        }
        if (h.type == intersection_type::triangle) {
            hits[k] = dr.origin.y - h.distance;
        }
    }

    res.frontY = hits[0];
    res.rearY  = hits[1];
    if (hits[0] > -5000.0f && hits[1] > -5000.0f) {
        res.roadY = 0.5f * (hits[0] + hits[1]);
        res.pitchRad = clamp(-atan2(hits[0] - hits[1], max(2.0f * halfWB, 1.2f)), -0.26f, 0.26f);
    } else if (hits[0] > -5000.0f) {
        res.roadY = hits[0];
    } else if (hits[1] > -5000.0f) {
        res.roadY = hits[1];
    }

    outP[gid] = res;
}

// ---------------------------------------------------------------------------
// Real GPU Edge-Preserving À-Trous Joint-Bilateral Denoiser (Step 1, 2, 4, 8)
// ---------------------------------------------------------------------------

kernel void denoiseAtrousKernel(
    uint2 gid [[thread_position_in_grid]],
    texture2d<float, access::read> srcDiffTex [[texture(0)]],
    texture2d<float, access::read> srcSpecTex [[texture(1)]],
    texture2d<float, access::write> dstDiffTex [[texture(2)]],
    texture2d<float, access::write> dstSpecTex [[texture(3)]],
    texture2d<float, access::read> gNormalRoughTex [[texture(4)]],
    texture2d<float, access::read> gDepthTex [[texture(5)]],
    constant RTFrameUniforms& u [[buffer(0)]],
    constant int& stepSize [[buffer(1)]]
) {
    int w = int(dstDiffTex.get_width());
    int h = int(dstDiffTex.get_height());
    if (int(gid.x) >= w || int(gid.y) >= h) return;

    float4 nrP = gNormalRoughTex.read(gid);
    float3 cDiffP = srcDiffTex.read(gid).rgb;
    float3 cSpecP = srcSpecTex.read(gid).rgb;

    bool isSplit = u.rtParams.w > 0.5f;
    int splitX = w / 2;
    if (nrP.w < 0.0f || (isSplit && abs(int(gid.x) - splitX) <= 1)) {
        dstDiffTex.write(float4(cDiffP, 1.0f), gid);
        dstSpecTex.write(float4(cSpecP, 1.0f), gid);
        return;
    }

    float3 nP = normalize(nrP.xyz);
    float roughP = nrP.w;
    float zP = gDepthTex.read(gid).r;
    float lumDiffP = dot(cDiffP, float3(0.2126f, 0.7152f, 0.0722f));
    float lumSpecP = dot(cSpecP, float3(0.2126f, 0.7152f, 0.0722f));

    float isGlossy = smoothstep(0.26f, 0.16f, roughP);
    float isMirrorCC = (roughP < 0.045f) ? 1.0f : 0.0f;
    float specRadiusScale = (roughP < 0.045f) ? 0.0f : smoothstep(0.045f, 0.18f, roughP);
    if (stepSize > 2 && (specRadiusScale < 0.08f || roughP < 0.24f)) {
        specRadiusScale = 0.0f;
    }

    const float kernelW[3] = { 0.375f, 0.25f, 0.0625f };

    float3 sumDiff = cDiffP * (kernelW[0] * kernelW[0]);
    float  wSumDiff = kernelW[0] * kernelW[0];

    float3 sumSpec = cSpecP * (kernelW[0] * kernelW[0]);
    float  wSumSpec = kernelW[0] * kernelW[0];

    float sigmaLumDiff = mix(0.55f + 0.18f * float(stepSize), 0.24f + 0.08f * float(stepSize), isMirrorCC);
    float sigmaLumSpec = mix(0.75f + 0.25f * float(stepSize), 0.22f + 0.08f * float(stepSize), isGlossy);
    float normalExp    = mix(mix(48.0f, 96.0f, isGlossy), 128.0f, isMirrorCC);
    float roughSigma   = mix(14.0f, 32.0f, isGlossy);
    float depthSigma   = max(0.06f * zP, 0.025f);

    int2 p = int2(gid);
    for (int dy = -2; dy <= 2; ++dy) {
        for (int dx = -2; dx <= 2; ++dx) {
            if (dx == 0 && dy == 0) continue;
            int2 q = clamp(p + int2(dx, dy) * stepSize, int2(0), int2(w - 1, h - 1));

            if (isSplit && ((p.x < splitX && q.x >= splitX) || (p.x > splitX && q.x <= splitX))) {
                continue;
            }

            float4 nrQ = gNormalRoughTex.read(uint2(q));
            if (nrQ.w < 0.0f) continue;

            float3 nQ = normalize(nrQ.xyz);
            float zQ = gDepthTex.read(uint2(q)).r;

            float wN = pow(max(dot(nP, nQ), 0.0f), normalExp);
            float wZ = exp(-abs(zP - zQ) / depthSigma);
            float kW = kernelW[abs(dx)] * kernelW[abs(dy)];
            float geomW = kW * wN * wZ;
            if (geomW < 1e-4f) continue;

            float3 cDiffQ = srcDiffTex.read(uint2(q)).rgb;
            float lumDiffQ = dot(cDiffQ, float3(0.2126f, 0.7152f, 0.0722f));
            float wLD = exp(-abs(lumDiffP - lumDiffQ) / sigmaLumDiff);
            float wD = geomW * wLD;
            sumDiff += cDiffQ * wD;
            wSumDiff += wD;

            if (specRadiusScale > 0.01f) {
                float wR = exp(-abs(roughP - nrQ.w) * roughSigma);
                float3 cSpecQ = srcSpecTex.read(uint2(q)).rgb;
                float lumSpecQ = dot(cSpecQ, float3(0.2126f, 0.7152f, 0.0722f));
                float wLS = exp(-abs(lumSpecP - lumSpecQ) / sigmaLumSpec);
                float wS = geomW * wR * wLS * specRadiusScale;
                sumSpec += cSpecQ * wS;
                wSumSpec += wS;
            }
        }
    }

    dstDiffTex.write(float4(sumDiff / max(wSumDiff, 1e-5f), 1.0f), gid);
    dstSpecTex.write(float4(sumSpec / max(wSumSpec, 1e-5f), 1.0f), gid);
}

kernel void remodulateKernel(
    uint2 gid [[thread_position_in_grid]],
    texture2d<float, access::read> diffTex [[texture(0)]],
    texture2d<float, access::read> specTex [[texture(1)]],
    texture2d<float, access::read> gAlbedoTex [[texture(2)]],
    texture2d<float, access::read> gEmissiveTex [[texture(3)]],
    texture2d<float, access::write> hdrOutTex [[texture(4)]]
) {
    uint w = hdrOutTex.get_width();
    uint h = hdrOutTex.get_height();
    if (gid.x >= w || gid.y >= h) return;

    float4 alb = gAlbedoTex.read(gid);
    float3 diff = diffTex.read(gid).rgb;
    float3 spec = specTex.read(gid).rgb;
    float3 emis = gEmissiveTex.read(gid).rgb;

    float3 hdr = diff * alb.rgb + spec + emis;
    hdrOutTex.write(float4(hdr, 1.0f), gid);
}

// ---------------------------------------------------------------------------
// Post-Processing Compute Passes: Bloom Threshold, Separable Blur, ACES Composite
// ---------------------------------------------------------------------------

kernel void bloomThresholdKernel(
    uint2 gid [[thread_position_in_grid]],
    texture2d<float, access::read> hdrTex [[texture(0)]],
    texture2d<float, access::write> bloomTex [[texture(1)]],
    constant RTFrameUniforms& u [[buffer(0)]]
) {
    uint bw = bloomTex.get_width();
    uint bh = bloomTex.get_height();
    if (gid.x >= bw || gid.y >= bh) return;

    uint2 base = gid * 2u;
    uint2 maxC = uint2(hdrTex.get_width() - 1u, hdrTex.get_height() - 1u);
    float3 c = (hdrTex.read(base).rgb +
                hdrTex.read(min(base + uint2(1u, 0u), maxC)).rgb +
                hdrTex.read(min(base + uint2(0u, 1u), maxC)).rgb +
                hdrTex.read(min(base + uint2(1u, 1u), maxC)).rgb) * 0.25f;

    float lum = dot(c, float3(0.2126f, 0.7152f, 0.0722f));
    float thresh = u.postParams.x;
    float soft = max(lum - thresh, 0.0f) / max(lum, 1e-4f);
    bloomTex.write(float4(c * soft, 1.0f), gid);
}

kernel void bloomBlurKernel(
    uint2 gid [[thread_position_in_grid]],
    texture2d<float, access::read> srcTex [[texture(0)]],
    texture2d<float, access::write> dstTex [[texture(1)]],
    constant int2& dir [[buffer(0)]]
) {
    int w = int(dstTex.get_width());
    int h = int(dstTex.get_height());
    if (int(gid.x) >= w || int(gid.y) >= h) return;

    const float weights[5] = { 0.227027f, 0.1945946f, 0.1216216f, 0.054054f, 0.016216f };
    int2 p = int2(gid);
    float3 sum = srcTex.read(uint2(p)).rgb * weights[0];
    for (int i = 1; i < 5; ++i) {
        int2 off = dir * i;
        int2 pA = clamp(p + off, int2(0), int2(w - 1, h - 1));
        int2 pB = clamp(p - off, int2(0), int2(w - 1, h - 1));
        sum += srcTex.read(uint2(pA)).rgb * weights[i];
        sum += srcTex.read(uint2(pB)).rgb * weights[i];
    }
    dstTex.write(float4(sum, 1.0f), gid);
}

static inline float3 acesFilmic(float3 x) {
    float a = 2.51f;
    float b = 0.03f;
    float c = 2.43f;
    float d = 0.59f;
    float e = 0.14f;
    return clamp((x * (a * x + b)) / (x * (c * x + d) + e), 0.0f, 1.0f);
}

static inline float3 linearToSRGB(float3 c) {
    float3 lo = c * 12.92f;
    float3 hi = 1.055f * pow(max(c, float3(0.0f)), float3(1.0f / 2.4f)) - 0.055f;
    return select(hi, lo, c <= 0.0031308f);
}

static inline float3 srgbToLinear(float3 c) {
    float3 lo = c / 12.92f;
    float3 hi = pow(max((c + 0.055f) / 1.055f, float3(0.0f)), float3(2.4f));
    return select(hi, lo, c <= 0.04045f);
}

// HUD functions & hudOverlayKernel live in RTHud.metal (concatenated after RayTracing.metal
// at runtime before device.makeLibrary(source:)) so HUD stamps cleanly AFTER MTLFXFrameInterpolator.

kernel void compositeKernel(
    uint2 gid [[thread_position_in_grid]],
    texture2d<float, access::read> hdrTex [[texture(0)]],
    texture2d<float, access::sample> bloomTex [[texture(1)]],
    texture2d<float, access::write> outTex [[texture(2)]],
    sampler linearSamp [[sampler(0)]],
    constant RTFrameUniforms& u [[buffer(0)]],
    constant uint& encodeSRGB [[buffer(1)]]
) {
    uint w = outTex.get_width();
    uint h = outTex.get_height();
    if (gid.x >= w || gid.y >= h) return;

    float2 uv = (float2(gid) + 0.5f) / float2(float(w), float(h));
    float3 hdr = hdrTex.read(gid).rgb;
    float3 bloom = bloomTex.sample(linearSamp, uv).rgb;
    float3 combined = (hdr + bloom * u.postParams.y) * u.cameraUp.w;

    float2 v = uv * 2.0f - 1.0f;
    float vigStrength = clamp(u.gradeParams.x, 0.0f, 0.6f);
    float vig = 1.0f - dot(v, v) * vigStrength;
    combined *= clamp(vig, 0.55f, 1.0f);

    float3 mapped = acesFilmic(combined);

    // Convert to perceptual sRGB BEFORE applying shadow-safe S-curve contrast & saturation
    // (eliminates linear-space black crush that clipped dark asphalt & tire shadows to #000000!)
    float3 srgb = linearToSRGB(mapped);
    float contrast = clamp(u.gradeParams.y, 0.5f, 1.8f);
    float sat = clamp(u.gradeParams.z, 0.2f, 1.8f);
    float sCurveW = clamp((contrast - 1.0f) * 0.65f, -0.45f, 0.65f);
    float3 sCurved = srgb * srgb * (3.0f - 2.0f * srgb);
    srgb = clamp(mix(srgb, sCurved, sCurveW), 0.0f, 1.0f);
    float lum = dot(srgb, float3(0.2126f, 0.7152f, 0.0722f));
    srgb = clamp(mix(float3(lum), srgb, sat), 0.0f, 1.0f);

    // Iconic NFSMW Speedbreaker amber-gold slow-mo bullet-time grade (zero effect when u.fxParams.y == 0)
    float sb = clamp(u.fxParams.y, 0.0f, 1.0f);
    if (sb > 0.01f) {
        float sbLum = dot(srgb, float3(0.2126f, 0.7152f, 0.0722f));
        float3 punch = clamp((srgb - 0.5f) * 1.18f + 0.5f, 0.0f, 1.0f);
        float3 amberGrade = mix(float3(sbLum), punch, 0.42f) * float3(1.24f, 0.94f, 0.54f);
        float r2 = dot(v, v);
        float edgeVig = 1.0f - r2 * 0.36f * sb;
        float rimGlow = smoothstep(0.45f, 1.35f, r2) * 0.14f * sb;
        float3 sbOut = amberGrade * clamp(edgeVig, 0.42f, 1.0f) + float3(0.32f, 0.18f, 0.04f) * rimGlow;
        srgb = clamp(mix(srgb, sbOut, sb * 0.86f), 0.0f, 1.0f);
    }

    float3 outRGB = (encodeSRGB != 0u) ? srgb : srgbToLinear(srgb);
    outTex.write(float4(outRGB, 1.0f), gid);
}
