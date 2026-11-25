#include <metal_stdlib>
#include <metal_raytracing>
using namespace metal;
using namespace raytracing;

constant float PI = 3.14159265359f;

// Material IDs
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

struct RTVertex {
    packed_float3 position;
    packed_float3 normal;
    packed_float2 uv;
    packed_float3 modelPos;
    uint materialID;
};

struct RTInstanceMeta {
    uint vertexOffset;
    uint indexOffset;
    uint triOffset;
    uint flags; // bit 0: liveryEnabled, bit 1: isGlass, bit 2: isWorldEnv
    float4x4 modelMatrix;
    float4x4 invTransposeMatrix;
};

struct RTMaterial {
    float4 baseColor;      // rgb, w = alpha
    float4 secondaryColor; // rgb, w = ior
    float4 pbrParams;      // x = metallic, y = roughness, z = clearcoat, w = clearcoatRoughness
    float4 emissive;       // rgb, w = flags (1.0 = livery)
};

struct RTFrameUniforms {
    float4x4 invViewProjMatrix;
    float4 cameraPos;      // xyz, w = fovDeg
    float4 cameraForward;  // xyz, w = aspect
    float4 cameraRight;    // xyz, w = tanHalfFov
    float4 cameraUp;       // xyz, w = exposure
    float4 sunDir;         // xyz normalized, w = sunIntensity
    float4 sunColor;       // rgb, w = sunConeRadius
    float4 coronaColor;    // rgb, w = iblIntensity
    float4 skyZenith;      // rgb, w = wetness
    float4 skyHorizon;     // rgb, w = fogDensity
    float4 skyGround;      // rgb, w = headlightsOn
    float4 neonParams;     // rgb = primary neon tint, w = neonIntensity
    float4 postParams;     // x = bloomThreshold, y = bloomIntensity, z = liveryEnabled, w = rtEnabled
    float4 rtParams;       // x = width, y = height, z = sampleIndex, w = splitCompareMode
};

// ---------------------------------------------------------------------------
// Deterministic PRNG & Low-Discrepancy Helpers
// ---------------------------------------------------------------------------

static inline uint pcgHash(uint v) {
    uint state = v * 747796405u + 2891336453u;
    uint word = ((state >> ((state >> 28u) + 4u)) ^ state) * 277803737u;
    return (word >> 22u) ^ word;
}

static inline float rand01(thread uint& seed) {
    seed = pcgHash(seed);
    return float(seed & 0x00FFFFFFu) / float(0x01000000u);
}

static inline float2 r2Sequence(uint idx, float2 pixelJitter) {
    // Roberts R2 quasi-Monte Carlo sequence with Cranley-Patterson pixel shift
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
        p = rot * p * 2.04f + float2(13.7f, 7.3f);
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

static inline float3 sampleGGXHalfVector(float3 N, float roughness, float2 xi) {
    float a = max(roughness * roughness, 0.0015f);
    float phi = 2.0f * PI * xi.x;
    float cosTheta = sqrt((1.0f - xi.y) / (1.0f + (a * a - 1.0f) * xi.y));
    float sinTheta = sqrt(max(0.0f, 1.0f - cosTheta * cosTheta));
    float3 H_local = float3(cos(phi) * sinTheta, sin(phi) * sinTheta, cosTheta);
    float3 t, b;
    buildONB(N, t, b);
    return normalize(t * H_local.x + b * H_local.y + N * H_local.z);
}

// ---------------------------------------------------------------------------
// Procedural Nishita Sky-Dome + Rockport Harbor Skyline
// ---------------------------------------------------------------------------

static float3 sampleSkyDome(float3 dir, constant RTFrameUniforms& u, bool includeSunDisc, bool highDetail = true) {
    float3 d = normalize(dir);
    float3 s = normalize(u.sunDir.xyz);
    float up = clamp(d.y, -1.0f, 1.0f);

    float horizonFactor = pow(1.0f - clamp(abs(up), 0.0f, 1.0f), 3.6f);
    float zenithFactor = pow(clamp(up, 0.0f, 1.0f), 0.58f);
    float3 sky = mix(u.skyHorizon.rgb, u.skyZenith.rgb, zenithFactor);

    float sunDot = max(dot(d, s), 0.0f);
    float mieAzimuth = pow(sunDot, 4.0f) * (0.45f + 0.55f * horizonFactor);
    sky += u.coronaColor.rgb * mieAzimuth * 0.85f;
    sky = mix(sky, u.skyHorizon.rgb * 1.18f + u.coronaColor.rgb * 0.25f, horizonFactor * 0.52f);

    if (up < 0.0f) {
        float gFade = clamp(-up * 6.0f, 0.0f, 1.0f);
        sky = mix(sky, u.skyGround.rgb, gFade);
    }

    // Subtle cirrus clouds
    if (up > 0.03f) {
        float2 cloudUV = d.xz / (up + 0.16f) * 0.75f;
        float cloud = smoothstep(0.52f, 0.86f, fbm2D(cloudUV + float2(1.4f, 2.8f)));
        float3 cloudCol = mix(u.skyHorizon.rgb * 0.8f, u.coronaColor.rgb * 1.15f, pow(sunDot, 3.0f));
        sky = mix(sky, cloudCol, cloud * 0.28f * smoothstep(0.03f, 0.25f, up));
    }

    // Distant Rockport skyline silhouette along horizon
    if (up >= -0.01f && up < 0.145f) {
        float az = atan2(d.z, d.x);
        float b1 = floor((az + PI) * 28.0f);
        float b2 = floor((az + PI) * 62.0f);
        float h1 = hash21(float2(b1, 3.1f)) * 0.075f + 0.012f;
        float h2 = hash21(float2(b2, 9.7f)) * 0.048f + 0.008f;
        float district = smoothstep(-0.3f, 0.5f, sin(az * 2.0f + 0.6f)) * 0.042f;
        float buildingH = max(h1, h2) + district * hash21(float2(b1, 17.3f));
        float sunClear = 1.0f - 0.6f * pow(max(dot(normalize(float3(d.x, 0.0f, d.z)), normalize(float3(s.x, 0.0f, s.z))), 0.0f), 6.0f);
        buildingH *= sunClear;

        if (up < buildingH) {
            float3 bCol = mix(u.skyHorizon.rgb * 0.16f, float3(0.025f, 0.032f, 0.048f), 0.75f);
            float3 winGlow = mix(u.coronaColor.rgb, float3(1.0f, 0.86f, 0.55f), 0.45f) * (0.18f + 0.22f * u.neonParams.w);
            if (highDetail) {
                float winX = fract((az + PI) * 160.0f);
                float winY = fract(up * 400.0f);
                float gx = smoothstep(0.20f, 0.32f, winX) * (1.0f - smoothstep(0.68f, 0.80f, winX));
                float gy = smoothstep(0.24f, 0.36f, winY) * (1.0f - smoothstep(0.64f, 0.76f, winY));
                float lit = step(0.62f, hash21(float2(floor((az + PI) * 160.0f), floor(up * 400.0f))));
                bCol += winGlow * (gx * gy * lit);
            } else {
                // Filtered city-light band for reflections/GI (eliminates 1px aliasing streaks)
                bCol += winGlow * 0.14f * smoothstep(0.0f, 0.06f, up);
            }
            float edge = smoothstep(buildingH, buildingH - 0.004f, up);
            sky = mix(sky, bCol, edge);
        }
    }

    if (includeSunDisc) {
        float sunIntensity = u.sunDir.w;
        float corona = pow(sunDot, 22.0f) * 0.95f + pow(sunDot, 160.0f) * 3.2f;
        float disc = smoothstep(0.9990f, 0.99965f, sunDot) * 18.0f;
        sky += u.coronaColor.rgb * corona * (0.35f + 0.12f * sunIntensity);
        sky += u.sunColor.rgb * disc * max(sunIntensity * 0.45f, 0.5f);
    }

    return max(sky, float3(0.0f));
}

// ---------------------------------------------------------------------------
// Cook-Torrance GGX PBR BRDF + BMW M3 GTR Livery
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

    float centerBand = smoothstep(0.19f, 0.175f, ax) - smoothstep(0.055f, 0.04f, ax);
    float hoodTrunk = step(0.45f, y);

    float slashCurve = 0.56f - 0.165f * z + 0.04f * sin(z * 1.4f);
    float flankSide = smoothstep(0.68f, 0.72f, ax);
    float flankSlash = smoothstep(slashCurve - 0.18f, slashCurve - 0.15f, y) *
                       smoothstep(slashCurve + 0.19f, slashCurve + 0.16f, y);
    float frontSpear = smoothstep(1.25f, 1.32f, z) * smoothstep(2.25f, 2.15f, z) *
                       smoothstep(0.32f, 0.36f, y) * smoothstep(0.68f, 0.64f, y);
    float rearWedge = smoothstep(-0.75f, -0.85f, z) * smoothstep(-2.08f, -2.00f, z) *
                      smoothstep(0.50f, 0.54f, y) * smoothstep(0.96f, 0.92f, y);

    float innerMask = clamp(max(centerBand * hoodTrunk, max(flankSide * flankSlash, max(flankSide * frontSpear, rearWedge * 0.85f))), 0.0f, 1.0f);
    float pinstripe = smoothstep(0.15f, 0.45f, innerMask) * (1.0f - smoothstep(0.55f, 0.85f, innerMask));

    stripeMask = innerMask;
    float3 col = mix(silverBase, blueStripe, innerMask);
    col = mix(col, float3(0.02f, 0.05f, 0.14f), pinstripe * 0.65f);
    return col;
}

// ---------------------------------------------------------------------------
// Surface Evaluation Helper (for Primary, Reflection & GI Hits)
// ---------------------------------------------------------------------------

struct HitSurface {
    float3 worldPos;
    float3 N;
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

static HitSurface resolveHitSurface(
    float3 rayOrigin,
    float3 rayDir,
    float dist,
    uint instanceID,
    uint primitiveID,
    float2 bary,
    bool frontFacing,
    device const RTVertex* vertices,
    device const uint* indices,
    device const uint* triMaterials,
    device const RTInstanceMeta* instances,
    device const RTMaterial* materials,
    constant RTFrameUniforms& u,
    texture2d<float> asphaltTex,
    texture2d<float> carbonTex,
    sampler repeatSamp
) {
    RTInstanceMeta inst = instances[instanceID];
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
    s.geomN = gN;
    s.uv = float2(v0.uv) * w0 + float2(v1.uv) * w1 + float2(v2.uv) * w2;
    s.modelPos = float3(v0.modelPos) * w0 + float3(v1.modelPos) * w1 + float3(v2.modelPos) * w2;
    s.matID = triMaterials[triIdx];

    RTMaterial mat = materials[min(s.matID, 19u)];
    s.baseAlbedo = mat.baseColor.rgb;
    s.metallic = mat.pbrParams.x;
    s.roughness = clamp(mat.pbrParams.y, 0.025f, 0.98f);
    s.clearcoat = mat.pbrParams.z;
    s.ccRoughness = clamp(mat.pbrParams.w, 0.02f, 0.5f);
    s.ior = mat.secondaryColor.w;
    s.emissive = mat.emissive.rgb;
    s.puddleMask = 0.0f;
    s.alpha = mat.baseColor.w;

    if (s.matID == MAT_CAR_PAINT) {
        if ((inst.flags & 1u) != 0u && u.postParams.z > 0.5f) {
            float stripeMask = 0.0f;
            s.baseAlbedo = evaluateBMWLivery(s.modelPos, mat.baseColor.rgb, mat.secondaryColor.rgb, stripeMask);
            s.metallic = mix(mat.pbrParams.x, 0.80f, stripeMask);
            s.roughness = mix(mat.pbrParams.y, 0.14f, stripeMask);
        }
        float3 flakeSeed = floor(s.modelPos * 460.0f);
        float3 flakeJitter = (hash33(flakeSeed) - 0.5f) * 0.022f;
        s.N = normalize(s.N + flakeJitter);
    } else if (s.matID == MAT_CARBON_FIBER) {
        float3 cf = carbonTex.sample(repeatSamp, s.uv * 8.0f, level(0.0f)).rgb;
        float weave = sin(s.modelPos.x * 85.0f) * sin(s.modelPos.z * 85.0f);
        s.baseAlbedo = mix(s.baseAlbedo * (0.78f + 0.22f * weave), cf * 0.34f, 0.45f);
    } else if (s.matID == MAT_HEADLIGHT_EMIT) {
        float halo = 0.55f + 0.45f * pow(max(0.0f, sin(s.modelPos.x * 32.0f)), 2.0f);
        s.emissive *= u.skyGround.w * halo * 0.48f;
    } else if (s.matID == MAT_TAILLIGHT_EMIT) {
        float tailPulse = 0.85f + 0.15f * smoothstep(0.72f, 0.82f, s.modelPos.y);
        s.emissive *= (0.75f + 0.55f * u.skyGround.w) * tailPulse * 0.72f;
    } else if (s.matID == MAT_WET_ASPHALT) {
        float2 wp = s.worldPos.xz;
        float wetness = clamp(u.skyZenith.w, 0.0f, 1.0f);
        float3 texA = asphaltTex.sample(repeatSamp, wp * 0.28f, level(0.0f)).rgb;
        float3 texB = asphaltTex.sample(repeatSamp, wp * 0.071f + float2(0.37f, 0.61f), level(0.0f)).rgb;
        float3 asphaltCol = mix(texA, texB, 0.35f) * 0.34f;

        float2 warp = float2(fbm2D(wp * 0.18f), fbm2D(wp * 0.18f + float2(5.2f, 1.3f)));
        float puddleField = fbm2D(wp * 0.24f + warp * 1.45f);
        float puddleThreshold = mix(0.74f, 0.42f, wetness);
        float puddle = smoothstep(puddleThreshold - 0.07f, puddleThreshold + 0.07f, puddleField);
        float damp = smoothstep(0.25f, 0.72f, puddleField + wetness * 0.25f);

        s.baseAlbedo = asphaltCol * mix(1.0f, 0.42f, max(damp * 0.75f, puddle));
        s.roughness = mix(mix(0.54f, 0.24f, damp), 0.018f, puddle);
        s.puddleMask = puddle;

        float microX = (noise2D(wp * 22.0f) - 0.5f) * 0.065f * (1.0f - puddle);
        float microZ = (noise2D(wp * 22.0f + float2(11.4f, 3.8f)) - 0.5f) * 0.065f * (1.0f - puddle);
        s.N = normalize(float3(microX, 1.0f, microZ));
    } else if (s.matID == MAT_URBAN_CONCRETE) {
        float2 wallUV = abs(s.N.y) > 0.5f ? s.worldPos.xz : (abs(s.N.x) > 0.5f ? s.worldPos.zy : s.worldPos.xy);
        float grain = fbm2D(wallUV * 1.4f);
        s.baseAlbedo *= (0.76f + 0.42f * grain);
        s.roughness = mix(0.82f, 0.62f, u.skyZenith.w * 0.40f);
    } else if (s.matID == MAT_URBAN_GLASS) {
        if (abs(s.N.y) > 0.5f) {
            // Flat tower rooftop should be dark concrete, not window grid
            s.baseAlbedo = float3(0.06f, 0.065f, 0.075f);
            s.roughness = 0.85f;
            s.metallic = 0.0f;
            s.emissive = float3(0.0f);
        } else {
            float2 winUV = abs(s.N.x) > 0.5f ? s.worldPos.zy : s.worldPos.xy;
            float2 cell = floor(winUV * float2(0.85f, 0.45f));
            float2 f = fract(winUV * float2(0.85f, 0.45f));
            float inPane = step(0.18f, f.x) * step(f.x, 0.82f) * step(0.22f, f.y) * step(f.y, 0.78f);
            float lit = step(0.62f, hash21(cell + float2(3.7f, 11.1f)));
            float3 winGlow = mix(u.coronaColor.rgb, u.neonParams.rgb, hash21(cell * 1.7f));
            s.emissive = winGlow * inPane * lit * (0.28f + 1.15f * u.neonParams.w);
            s.roughness = mix(0.36f, 0.06f, inPane);
            s.metallic = mix(0.08f, 0.42f, inPane);
        }
    } else if (s.matID == MAT_NEON_CYAN) {
        s.emissive = u.neonParams.rgb * (2.0f + 3.8f * u.neonParams.w);
    } else if (s.matID == MAT_NEON_AMBER) {
        s.emissive = mix(u.coronaColor.rgb, float3(1.0f, 0.42f, 0.12f), 0.55f) * (1.8f + 3.4f * u.neonParams.w);
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
        float f0_cc = pow((s.ior - 1.0f) / (s.ior + 1.0f), 2.0f);
        float F_cc = F_SchlickScalar(VdotH, f0_cc) * s.clearcoat;
        float ccAlpha = s.ccRoughness * s.ccRoughness;
        float D_cc = D_GGX(NdotH, ccAlpha);
        float G_cc = G_SmithGGX(NdotV, NdotL, s.ccRoughness);
        float specCC = (D_cc * G_cc * F_cc) / max(4.0f * NdotV * NdotL, 1e-4f);
        col = col * (1.0f - F_cc) + specCC * sunRad * NdotL;
    }
    return col;
}

// Local car headlight/taillight + streetlamp analytic fill for bounce/direct illumination
static float3 evaluateLocalAreaLights(HitSurface s, float3 V, constant RTFrameUniforms& u) {
    float3 accum = float3(0.0f);
    float headScale = u.skyGround.w;
    // Left & right xenon headlight emitters (front of BMW: z ~ +2.25)
    float3 lightPos[4] = {
        float3(-0.62f, 0.56f, 2.34f),
        float3(+0.62f, 0.56f, 2.34f),
        float3(-0.62f, 0.68f, -2.08f),
        float3(+0.62f, 0.68f, -2.08f)
    };
    float3 lightCol[4] = {
        float3(0.75f, 0.88f, 1.05f) * (3.2f * headScale),
        float3(0.75f, 0.88f, 1.05f) * (3.2f * headScale),
        float3(1.10f, 0.14f, 0.05f) * (2.2f * (0.65f + 0.5f * headScale)),
        float3(1.10f, 0.14f, 0.05f) * (2.2f * (0.65f + 0.5f * headScale))
    };

    for (int i = 0; i < 4; ++i) {
        float3 toL = lightPos[i] - s.worldPos;
        float d2 = dot(toL, toL) + 0.18f;
        float d = sqrt(d2);
        float3 L = toL / d;
        float NdotL = max(dot(s.N, L), 0.0f);
        if (NdotL > 0.0f) {
            float3 dir = (i < 2) ? float3(0.0f, -0.15f, 1.0f) : float3(0.0f, -0.10f, -1.0f);
            float cone = smoothstep(0.15f, 0.85f, dot(-L, normalize(dir)));
            float atten = cone / (1.0f + d2 * 0.55f);
            float3 H = normalize(V + L);
            float spec = pow(max(dot(s.N, H), 0.0f), mix(128.0f, 12.0f, s.roughness)) * 0.45f;
            float3 contrib = (s.baseAlbedo * (1.0f - s.metallic) / PI + float3(spec)) * lightCol[i] * NdotL * atten;
            accum += contrib;
        }
    }
    return accum;
}

static float3 evaluateSecondaryHitRadiance(
    float3 hitOrigin,
    float3 hitDir,
    HitSurface s,
    instance_acceleration_structure tlas,
    constant RTFrameUniforms& u,
    float2 xiShadow
) {
    float3 V = -hitDir;
    float3 L = normalize(u.sunDir.xyz);
    float3 sunRad = u.sunColor.rgb * u.sunDir.w;

    intersector<triangle_data, instancing> shadowRT;
    shadowRT.assume_geometry_type(geometry_type::triangle);
    shadowRT.accept_any_intersection(true);

    ray sr;
    sr.origin = s.worldPos + s.geomN * 0.004f;
    sr.direction = sampleCone(L, u.sunColor.w * 0.5f, xiShadow);
    sr.min_distance = 0.002f;
    sr.max_distance = 80.0f;
    auto sh = shadowRT.intersect(sr, tlas, 0x01u);
    float vis = (sh.type == intersection_type::none) ? 1.0f : 0.0f;

    float3 direct = evaluateDirectSunBRDF(s, V, L, sunRad) * vis;
    float3 envUp = sampleSkyDome(reflect(-V, s.N), u, false, false) * u.coronaColor.w;
    float3 F0 = mix(float3(0.04f), s.baseAlbedo, s.metallic);
    float3 F = F_Schlick(max(dot(s.N, V), 0.0f), F0);
    float3 ambient = (1.0f - F) * (1.0f - s.metallic) * s.baseAlbedo * (u.skyZenith.rgb * 0.35f + u.skyHorizon.rgb * 0.45f) * u.coronaColor.w * 0.45f;
    float3 reflApprox = F * envUp;

    return direct + ambient + reflApprox + s.emissive + evaluateLocalAreaLights(s, V, u);
}

// ---------------------------------------------------------------------------
// Primary Hardware Ray Tracing Compute Kernel (M5 Pro GPU)
// ---------------------------------------------------------------------------

kernel void raytraceKernel(
    uint2 gid [[thread_position_in_grid]],
    texture2d<float, access::read_write> accumTex [[texture(0)]],
    texture2d<float> asphaltTex [[texture(1)]],
    texture2d<float> carbonTex [[texture(2)]],
    sampler repeatSamp [[sampler(0)]],
    constant RTFrameUniforms& u [[buffer(0)]],
    instance_acceleration_structure tlas [[buffer(1)]],
    device const RTVertex* vertices [[buffer(2)]],
    device const uint* indices [[buffer(3)]],
    device const uint* triMaterials [[buffer(4)]],
    device const RTInstanceMeta* instances [[buffer(5)]],
    device const RTMaterial* materials [[buffer(6)]]
) {
    uint width = uint(u.rtParams.x);
    uint height = uint(u.rtParams.y);
    if (gid.x >= width || gid.y >= height) return;

    uint sampleIdx = uint(u.rtParams.z);
    uint seed = pcgHash(gid.x + gid.y * 4099u + sampleIdx * 104729u + 17u);
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
            accumTex.write(float4(2.2f, 2.2f, 2.2f, 1.0f), gid);
            return;
        }
        useRT = (gid.x > splitX);
    }

    float2 jitter = (sampleIdx == 0u && !useRT) ? float2(0.5f, 0.5f) : xiAA;
    float2 uv = (float2(gid) + jitter) / float2(float(width), float(height));
    float2 ndc = float2(uv.x * 2.0f - 1.0f, 1.0f - uv.y * 2.0f);

    float4 worldFar = u.invViewProjMatrix * float4(ndc, 1.0f, 1.0f);
    float3 targetPt = worldFar.xyz / max(worldFar.w, 1e-5f);
    float3 rayOrigin = u.cameraPos.xyz;
    float3 rayDir = normalize(targetPt - rayOrigin);

    intersector<triangle_data, instancing> rt;
    rt.assume_geometry_type(geometry_type::triangle);

    ray r;
    r.origin = rayOrigin;
    r.direction = rayDir;
    r.min_distance = 0.02f;
    r.max_distance = 220.0f;

    auto hit = rt.intersect(r, tlas, 0xFFu);

    float3 radiance = float3(0.0f);
    if (hit.type == intersection_type::none) {
        radiance = sampleSkyDome(rayDir, u, true);
    } else {
        HitSurface s = resolveHitSurface(
            rayOrigin, rayDir, hit.distance,
            hit.instance_id, hit.primitive_id, hit.triangle_barycentric_coord, hit.triangle_front_facing,
            vertices, indices, triMaterials, instances, materials, u, asphaltTex, carbonTex, repeatSamp
        );

        float3 V = -rayDir;

        if (s.matID == MAT_SMOKED_GLASS || s.matID == MAT_HEADLIGHT_GLASS) {
            float NdotV = max(dot(s.N, V), 1e-4f);
            float f0 = pow((s.ior - 1.0f) / (s.ior + 1.0f), 2.0f);
            float F_glass = F_SchlickScalar(NdotV, f0);
            float reflWeight = clamp(F_glass + 0.14f, 0.12f, 0.92f);

            float3 reflDir = reflect(-V, s.N);
            float3 reflCol = sampleSkyDome(reflDir, u, true, false) * u.coronaColor.w;
            if (useRT) {
                ray rr;
                rr.origin = s.worldPos + s.geomN * 0.004f;
                rr.direction = reflDir;
                rr.min_distance = 0.002f;
                rr.max_distance = 120.0f;
                auto rh = rt.intersect(rr, tlas, 0xFFu);
                if (rh.type == intersection_type::triangle) {
                    HitSurface rs = resolveHitSurface(
                        rr.origin, rr.direction, rh.distance,
                        rh.instance_id, rh.primitive_id, rh.triangle_barycentric_coord, rh.triangle_front_facing,
                        vertices, indices, triMaterials, instances, materials, u, asphaltTex, carbonTex, repeatSamp
                    );
                    reflCol = evaluateSecondaryHitRadiance(rr.origin, rr.direction, rs, tlas, u, xiShad);
                }
            }

            ray tr;
            tr.origin = s.worldPos + rayDir * 0.004f;
            tr.direction = rayDir;
            tr.min_distance = 0.002f;
            tr.max_distance = 80.0f;
            auto th = rt.intersect(tr, tlas, 0xFFu);
            float3 transCol = sampleSkyDome(rayDir, u, false, false);
            if (th.type == intersection_type::triangle) {
                HitSurface ts = resolveHitSurface(
                    tr.origin, tr.direction, th.distance,
                    th.instance_id, th.primitive_id, th.triangle_barycentric_coord, th.triangle_front_facing,
                    vertices, indices, triMaterials, instances, materials, u, asphaltTex, carbonTex, repeatSamp
                );
                transCol = evaluateSecondaryHitRadiance(tr.origin, tr.direction, ts, tlas, u, xiShad);
            }

            float3 L = normalize(u.sunDir.xyz);
            float3 sunSpec = evaluateDirectSunBRDF(s, V, L, u.sunColor.rgb * u.sunDir.w);
            float3 tint = mix(s.baseAlbedo * 0.75f + float3(0.25f), float3(1.0f), 1.0f - s.alpha);
            radiance = mix(transCol * tint, reflCol, reflWeight) + sunSpec + s.emissive;
        } else {
            float3 L = normalize(u.sunDir.xyz);
            float3 sunRad = u.sunColor.rgb * u.sunDir.w;

            float sunVis = 1.0f;
            if (useRT) {
                intersector<triangle_data, instancing> shadowRT;
                shadowRT.assume_geometry_type(geometry_type::triangle);
                shadowRT.accept_any_intersection(true);

                ray sr;
                sr.origin = s.worldPos + s.geomN * 0.004f;
                sr.direction = sampleCone(L, u.sunColor.w, xiShad);
                sr.min_distance = 0.002f;
                sr.max_distance = 120.0f;
                auto sh = shadowRT.intersect(sr, tlas, 0x01u);
                sunVis = (sh.type == intersection_type::none) ? 1.0f : 0.0f;
            }

            float3 directSun = evaluateDirectSunBRDF(s, V, L, sunRad) * sunVis;
            float3 localLights = evaluateLocalAreaLights(s, V, u);

            float NdotV = max(dot(s.N, V), 1e-4f);
            float3 F0 = mix(float3(0.04f), s.baseAlbedo, s.metallic);
            float3 F_env = F_Schlick(NdotV, F0);
            float f0_cc = pow((s.ior - 1.0f) / (s.ior + 1.0f), 2.0f);
            float F_cc = (s.clearcoat > 0.01f) ? F_SchlickScalar(NdotV, f0_cc) * s.clearcoat : 0.0f;

            float3 baseSpecRefl = float3(0.0f);
            float3 ccSpecRefl = float3(0.0f);

            if (useRT) {
                float specRough = (s.matID == MAT_WET_ASPHALT) ? s.roughness : max(s.roughness, 0.04f);
                float3 H_base = sampleGGXHalfVector(s.N, specRough, xiRefl);
                float3 R_base = reflect(-V, H_base);
                if (dot(R_base, s.geomN) <= 0.0f) {
                    R_base = reflect(-V, s.geomN);
                }

                ray rr;
                rr.origin = s.worldPos + s.geomN * 0.004f;
                rr.direction = normalize(R_base);
                rr.min_distance = 0.003f;
                rr.max_distance = 140.0f;
                auto rh = rt.intersect(rr, tlas, 0xFFu);
                if (rh.type == intersection_type::none) {
                    baseSpecRefl = sampleSkyDome(rr.direction, u, true, false) * u.coronaColor.w;
                } else {
                    HitSurface rs = resolveHitSurface(
                        rr.origin, rr.direction, rh.distance,
                        rh.instance_id, rh.primitive_id, rh.triangle_barycentric_coord, rh.triangle_front_facing,
                        vertices, indices, triMaterials, instances, materials, u, asphaltTex, carbonTex, repeatSamp
                    );
                    baseSpecRefl = evaluateSecondaryHitRadiance(rr.origin, rr.direction, rs, tlas, u, xiShad);
                }

                if (s.clearcoat > 0.05f) {
                    float3 H_cc = sampleGGXHalfVector(s.N, s.ccRoughness, xiCC);
                    float3 R_cc = reflect(-V, H_cc);
                    if (dot(R_cc, s.geomN) <= 0.0f) R_cc = reflect(-V, s.geomN);

                    ray cr;
                    cr.origin = s.worldPos + s.geomN * 0.004f;
                    cr.direction = normalize(R_cc);
                    cr.min_distance = 0.003f;
                    cr.max_distance = 140.0f;
                    auto ch = rt.intersect(cr, tlas, 0xFFu);
                    if (ch.type == intersection_type::none) {
                        ccSpecRefl = sampleSkyDome(cr.direction, u, true, false) * u.coronaColor.w;
                    } else {
                        HitSurface cs = resolveHitSurface(
                            cr.origin, cr.direction, ch.distance,
                            ch.instance_id, ch.primitive_id, ch.triangle_barycentric_coord, ch.triangle_front_facing,
                            vertices, indices, triMaterials, instances, materials, u, asphaltTex, carbonTex, repeatSamp
                        );
                        ccSpecRefl = evaluateSecondaryHitRadiance(cr.origin, cr.direction, cs, tlas, u, xiShad);
                    }
                }
            } else {
                float3 R = reflect(-V, s.N);
                baseSpecRefl = sampleSkyDome(R, u, false, false) * u.coronaColor.w * mix(0.85f, 0.25f, s.roughness);
                ccSpecRefl = sampleSkyDome(R, u, false, false) * u.coronaColor.w;
            }

            float3 diffuseIndirect = float3(0.0f);
            float3 diffWeight = (1.0f - F_env) * (1.0f - s.metallic) * s.baseAlbedo;

            if (useRT) {
                float3 giDir = sampleCosineHemisphere(s.N, xiGI);
                ray gr;
                gr.origin = s.worldPos + s.geomN * 0.004f;
                gr.direction = giDir;
                gr.min_distance = 0.003f;
                gr.max_distance = 24.0f;
                auto gh = rt.intersect(gr, tlas, 0x01u);

                if (gh.type == intersection_type::none) {
                    float3 skyGI = sampleSkyDome(giDir, u, false, false) * u.coronaColor.w * 0.62f;
                    diffuseIndirect = diffWeight * skyGI;
                } else {
                    float ao = smoothstep(0.02f, 2.4f, gh.distance);
                    HitSurface gs = resolveHitSurface(
                        gr.origin, gr.direction, gh.distance,
                        gh.instance_id, gh.primitive_id, gh.triangle_barycentric_coord, gh.triangle_front_facing,
                        vertices, indices, triMaterials, instances, materials, u, asphaltTex, carbonTex, repeatSamp
                    );
                    float3 bounceRad = evaluateSecondaryHitRadiance(gr.origin, gr.direction, gs, tlas, u, xiShad);
                    diffuseIndirect = diffWeight * (bounceRad * 0.68f + sampleSkyDome(s.N, u, false, false) * 0.14f * ao);
                }
            } else {
                float3 skyAmb = sampleSkyDome(s.N, u, false, false) * u.coronaColor.w * 0.48f;
                diffuseIndirect = diffWeight * skyAmb;
            }

            float3 totalSpec = F_env * baseSpecRefl;
            float3 baseResult = directSun + localLights + diffuseIndirect + totalSpec;

            if (s.clearcoat > 0.05f) {
                baseResult = baseResult * (1.0f - F_cc) + ccSpecRefl * F_cc;
            }

            radiance = baseResult + s.emissive;
        }

        float fog = 1.0f - exp(-pow(hit.distance * u.skyHorizon.w * 1.45f, 1.55f));
        float3 fogCol = sampleSkyDome(normalize(float3(rayDir.x, 0.02f, rayDir.z)), u, false, false);
        radiance = mix(radiance, fogCol, clamp(fog, 0.0f, 0.92f));
    }

    (void)seed;
    radiance = clamp(radiance, float3(0.0f), float3(18.0f));

    if (sampleIdx == 0u) {
        accumTex.write(float4(radiance, 1.0f), gid);
    } else {
        float4 prev = accumTex.read(gid);
        float w = 1.0f / float(sampleIdx + 1u);
        float3 blended = mix(prev.rgb, radiance, w);
        accumTex.write(float4(blended, 1.0f), gid);
    }
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
    float3 c = (hdrTex.read(base).rgb +
                hdrTex.read(min(base + uint2(1u, 0u), uint2(hdrTex.get_width() - 1u, hdrTex.get_height() - 1u))).rgb +
                hdrTex.read(min(base + uint2(0u, 1u), uint2(hdrTex.get_width() - 1u, hdrTex.get_height() - 1u))).rgb +
                hdrTex.read(min(base + uint2(1u, 1u), uint2(hdrTex.get_width() - 1u, hdrTex.get_height() - 1u))).rgb) * 0.25f;

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

    // Subtle cinematic edge vignette
    float2 v = uv * 2.0f - 1.0f;
    float vig = 1.0f - dot(v, v) * 0.12f;
    combined *= clamp(vig, 0.72f, 1.0f);

    float3 mapped = acesFilmic(combined);
    if (encodeSRGB != 0u) {
        mapped = linearToSRGB(mapped);
    }
    outTex.write(float4(mapped, 1.0f), gid);
}
