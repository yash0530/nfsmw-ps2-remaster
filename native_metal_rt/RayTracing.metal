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
};

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
    return (a >= 0.48f);
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
    float upMask = smoothstep(0.58f, 0.90f, worldNY);
    float2 warp = float2(fbm2D(wp * 0.09f), fbm2D(wp * 0.09f + float2(5.2f, 1.3f)));
    float puddleField = fbm2D(wp * 0.135f + warp * 1.15f);
    // Natural rain-soaked puddles in wet night/rain (preserves dry/damp asphalt islands rather than a uniform oil sheet!)
    float heavyRain = smoothstep(0.40f, 0.85f, rainStrength) * smoothstep(0.70f, 0.92f, wetness);
    float pThresh = mix(0.84f, mix(0.56f, 0.40f, heavyRain), wetness);
    // Wide feathered smoothstep (pThresh ± 0.14) eliminates razor-sharp puddle boundaries
    outPuddle = smoothstep(pThresh - 0.14f, pThresh + 0.14f, puddleField) * upMask;
    // Subtle continuous rainwater sheen during active rain while preserving organic puddle boundaries
    outPuddle = max(outPuddle, 0.28f * upMask * heavyRain);
    outDamp   = max(smoothstep(pThresh - 0.36f, pThresh + 0.06f, puddleField) * upMask * wetness, 0.72f * upMask * heavyRain);
    outRippleN = float3(0.0f, 1.0f, 0.0f);

    // Delicate 15-40cm concentric raindrop ripple rings with grazing-angle attenuation (zero midground zebra-striping!)
    if (computeRipples && outPuddle > 0.20f && wetness > 0.70f && rainStrength > 0.35f && dist < 38.0f) {
        float grazingFade = smoothstep(0.06f, 0.24f, viewAbsY);
        float fade = smoothstep(38.0f, 3.0f, dist) * grazingFade * smoothstep(0.20f, 0.52f, outPuddle) * clamp(rainStrength, 0.0f, 1.0f);
        if (fade > 0.01f) {
            float2 ripGrad = float2(0.0f);
            int maxLayers = (dist < 18.0f) ? 3 : 2;
            for (int layer = 0; layer < maxLayers; ++layer) {
                float scale = (layer == 0) ? 1.60f : ((layer == 1) ? 2.80f : 4.40f);
                float amp   = (layer == 0) ? 0.36f : ((layer == 1) ? 0.24f : 0.14f);
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

        // Multi-scale world-space asphalt aggregate (macro wear 3/m + medium tarmac grit 11.5/m + fine mineral chips 30/m)
        float macroFade = exp(-dist * 0.012f);
        float fineFade  = exp(-dist * 0.032f);
        float aggMacro  = fbm2D(wp * 3.0f);
        float aggMed    = noise2D(wp * 11.5f + float2(3.1f, 7.4f));
        float aggFine   = noise2D(wp * 30.0f + float2(11.3f, 5.7f));
        float aggDelta  = (aggMed - 0.5f) * 0.58f * macroFade + (aggFine - 0.5f) * 0.42f * fineFade;
        float aggAlbedo = 1.0f + aggDelta * 0.46f + (aggMacro - 0.5f) * 0.24f;

        float3 aggWorldN = s.N;
        if (isPrimary) {
            float gxMed  = noise2D(wp * 13.0f + float2(0.14f, 0.0f)) - noise2D(wp * 13.0f - float2(0.14f, 0.0f));
            float gzMed  = noise2D(wp * 13.0f + float2(0.0f, 0.14f)) - noise2D(wp * 13.0f - float2(0.0f, 0.14f));
            float gxFine = noise2D(wp * 34.0f + float2(0.08f, 0.0f)) - noise2D(wp * 34.0f - float2(0.08f, 0.0f));
            float gzFine = noise2D(wp * 34.0f + float2(0.0f, 0.08f)) - noise2D(wp * 34.0f - float2(0.0f, 0.08f));
            float gxAgg  = gxMed * 0.65f * macroFade + gxFine * 0.55f * fineFade;
            float gzAgg  = gzMed * 0.65f * macroFade + gzFine * 0.55f * fineFade;
            float aggNStr = 0.52f * (1.0f - puddle * 0.88f);
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

        float dryR    = 0.78f;
        float dampR   = 0.48f;
        float puddleR = 0.020f;
        s.roughness   = clamp(mix(mix(dryR, dampR, damp), puddleR, puddle) + (aggMed - 0.5f) * 0.10f * (1.0f - puddle), 0.018f, 0.94f);
        s.clearcoat   = max(puddle * 0.92f, damp * 0.08f);
        s.ccRoughness = mix(0.14f, 0.016f, puddle);
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
        s.roughness = 0.76f;
        s.metallic = 0.0f;
        float leafFade = exp(-dist * 0.018f);
        float2 fp = float2(s.worldPos.x + s.worldPos.z, s.worldPos.y);
        float c1 = noise2D(fp * 13.5f);
        float c2 = noise2D(fp * 25.0f + float2(7.1f, 3.3f));
        float gx = noise2D(fp * 18.0f + float2(0.12f, 0.0f)) - noise2D(fp * 18.0f - float2(0.12f, 0.0f));
        float gy = noise2D(fp * 18.0f + float2(0.0f, 0.12f)) - noise2D(fp * 18.0f - float2(0.0f, 0.12f));
        float3 ft, fb;
        buildONB(worldN, ft, fb);
        s.N = normalize(s.N + (ft * gx + fb * gy) * (0.48f * leafFade));
        s.baseAlbedo *= (0.82f + 0.36f * (c1 * 0.55f + c2 * 0.45f));
    } else if (wmat.surfaceClass == 5u) {
        // METAL_BRIDGE: weathered painted/galvanized Rockport bridge girders & railings
        s.matID = MAT_BRUSHED_ALUMINUM;
        s.metallic = max(wmat.metallicHint, 0.78f);
        s.roughness = clamp(wmat.roughnessHint > 0.01f ? wmat.roughnessHint : 0.36f, 0.16f, 0.62f);
    } else if (wmat.surfaceClass == 6u || (wmat.flags & 8u) != 0u) {
        // EMISSIVE_SIGN_LAMP: glowing streetlamps, traffic lights, storefront signs & neon billboards (capped to prevent whiteout)
        s.matID = MAT_STREETLAMP_LED;
        float emGain = max(wmat.emissiveHint, 0.65f) * mix(0.28f, 1.35f, isNight);
        s.emissive = min(texCol * emGain * smoothstep(0.14f, 0.48f, lum), float3(1.55f));
    } else if (wmat.surfaceClass == 7u) {
        // WATER: Rockport coastal bay & harbor water with procedural ripples
        s.matID = MAT_COASTAL_WATER;
        float2 wp = s.worldPos.xz;
        float wx = sin(wp.x * 0.65f + wp.y * 0.35f) * 0.045f + (noise2D(wp * 2.5f) - 0.5f) * 0.055f;
        float wz = cos(wp.x * 1.05f - wp.y * 0.55f) * 0.045f + (noise2D(wp * 2.7f + 4.1f) - 0.5f) * 0.055f;
        s.N = normalize(float3(wx, 1.0f, wz));
        s.clearcoatN = s.N;
        s.baseAlbedo = mix(float3(0.008f, 0.026f, 0.044f), texCol * 0.25f, 0.35f);
        s.roughness = 0.032f;
        s.ior = 1.333f;
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
            if (u.carWorldPos.w > 0.5f && isNight > 0.05f && abs(worldN.y) < 0.35f) {
                float relY = s.worldPos.y - u.carWorldPos.y;
                if (relY > 1.4f && relY < 48.0f) {
                    float horizCoord = (abs(worldN.x) > abs(worldN.z)) ? s.worldPos.z : s.worldPos.x;
                    float2 bayGrid = float2(horizCoord * 0.82f, relY * 0.40f);
                    float2 cell = floor(bayGrid);
                    float2 f = fract(bayGrid);
                    float paneFrame = smoothstep(0.12f, 0.22f, f.x) * smoothstep(0.88f, 0.78f, f.x)
                                    * smoothstep(0.18f, 0.28f, f.y) * smoothstep(0.82f, 0.72f, f.y);
                    // Strict dark recessed pane texture gate so solid concrete/brick walls never light up
                    float darkWindowTex = smoothstep(0.18f, 0.07f, lum) * smoothstep(0.85f, 1.08f, (texCol.b + 0.015f) / (texCol.r + 0.020f));
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
        // (Does NOT sample EA's combined rim+tire atlas so metallic rim spokes never bleed onto tire shoulders!)
        float frontZ = (carID == 1u) ? 1.205f : ((carID == 2u) ? 1.160f : 1.595f);
        float rearZ  = (carID == 1u) ? -1.480f : ((carID == 2u) ? -1.390f : -1.1295f);
        float wz = (s.modelPos.z > 0.0f) ? frontZ : rearZ;
        float wy = (s.modelPos.z > 0.0f) ? 0.320f : ((carID == 1u) ? 0.340f : 0.330f);
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
        float frontZ = (carID == 1u) ? 1.205f : ((carID == 2u) ? 1.160f : 1.595f);
        float rearZ  = (carID == 1u) ? -1.480f : ((carID == 2u) ? -1.390f : -1.1295f);
        float wz = (s.modelPos.z > 0.0f) ? frontZ : rearZ;
        float wy = (s.modelPos.z > 0.0f) ? 0.320f : ((carID == 1u) ? 0.340f : 0.330f);
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
        float px = (carID == 0u) ? (1.0f - clamp((s.modelPos.x + 0.152f) / 0.304f, 0.0f, 1.0f)) : clamp(s.uv.x, 0.0f, 1.0f);
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
            } else {
                // Lamborghini Gallardo (001_GALLARDO_KIT00_HEADLIGH): bi-xenon projector orb + bright amber DRL/turn bowl
                float dXenon = min(
                    length(float2((carUV.x - 0.165f) * 2.20f, (carUV.y - 0.68f) * 1.25f)),
                    length(float2((carUV.x - 0.165f) * 2.20f, (carUV.y - 0.32f) * 1.25f))
                );
                float xenonLens = smoothstep(0.26f, 0.06f, dXenon);
                s.emissive   = (float3(7.5f, 8.2f, 10.0f) * xenonLens
                             +  float3(5.4f, 2.15f, 0.15f) * amberMask * 1.25f) * headOn * max(forwardGate, 0.45f);
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

static void evaluateLocalAreaLightsSplit(
    HitSurface s,
    float3 V,
    constant RTFrameUniforms& u,
    thread float3& outDiffIrrad,
    thread float3& outSpec
) {
    outDiffIrrad = float3(0.0f);
    outSpec = float3(0.0f);
    float headScale = u.skyGround.w;
    float yaw = u.carDynParams.z;
    float cy = cos(yaw), sy = sin(yaw);

    auto rotateCarLocal = [&](float3 p) -> float3 {
        return float3(p.x * cy + p.z * sy, p.y, -p.x * sy + p.z * cy);
    };

    float3 carOrigin = u.carWorldPos.xyz;
    float isNight = smoothstep(0.032f, 0.015f, u.skyZenith.r);
    // Gate road headlight projection strictly on true night so Daylight & Golden Hour have ZERO chalky white smear on foreground asphalt!
    float roadBeamGain = mix(0.0f, 1.0f, isNight);
    float3 carLights[4] = {
        carOrigin + rotateCarLocal(float3(-0.62f, 0.62f,  2.02f)),
        carOrigin + rotateCarLocal(float3( 0.62f, 0.62f,  2.02f)),
        carOrigin + rotateCarLocal(float3(-0.56f, 0.76f, -2.06f)),
        carOrigin + rotateCarLocal(float3( 0.56f, 0.76f, -2.06f))
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
                // Crisp automotive dual-projector low-beam cone with horizontal upper cutoff
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

    float lampPower = u.neonParams.w;
    if (isNight > 0.15f && lampPower > 0.35f && u.carWorldPos.w > 0.5f) {
        // Analytic Rockport Downtown/Rosewood overhead sodium-vapor avenue streetlamps (22m grid, sidewalk offsets ±8.5m, +6.8m height)
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
    // Recessed optical cavity occlusion: keeps dark gunmetal housing jet-dark while letting gleaming silver chrome bowls catch crisp glints!
    if (s.matID == MAT_HEADLIGHT_EMIT) {
        float chromeFrac = smoothstep(0.45f, 0.90f, s.metallic);
        direct *= mix(0.16f, 0.42f, chromeFrac);
        ambient *= 0.08f;
        reflApprox *= mix(0.05f, 0.32f, chromeFrac);
    }

    float3 localDiff, localSpec;
    evaluateLocalAreaLightsSplit(s, V, u, localDiff, localSpec);
    float3 localTotal = localDiff * (1.0f - F) * (1.0f - s.metallic) * s.baseAlbedo + localSpec;

    // Clamp secondary taillight radiance <= 1.4 so wet road puddles & trunk lip never smear into blurry red smudges!
    float3 bounceEmissive = (s.matID == MAT_TAILLIGHT_EMIT)
        ? min(s.emissive, float3(1.4f))
        : min(s.emissive, float3(3.8f));
    return min(direct + ambient + reflApprox + bounceEmissive + localTotal, float3(9.0f));
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
    constant RTDynamicFX& dynFX [[buffer(13)]]
) {
    (void)worldLights;
    (void)worldLightGrid;
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
    float2 uv = (float2(gid) + pixelJitter) / resF;
    float2 ndc = float2(uv.x * 2.0f - 1.0f, 1.0f - uv.y * 2.0f);

    float4 worldFar = u.invViewProjMatrix * float4(ndc, 1.0f, 1.0f);
    float3 targetPt = worldFar.xyz / max(worldFar.w, 1e-5f);
    float3 rayOrigin = u.cameraPos.xyz;
    float3 rayDir = normalize(targetPt - rayOrigin);

    ray r;
    r.origin = rayOrigin;
    r.direction = rayDir;
    r.min_distance = 0.02f;
    r.max_distance = hasRkptWorld ? 650.0f : 250.0f;

    TraceResult hit = traceSceneRay(
        r, tlas, 0xFFu, hasRkptWorld,
        instances, worldVertices, worldIndices, worldMaterials, worldTextures, worldTexCount, repeatSamp
    );

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
            worldVertices, worldIndices, worldMaterials, worldTextures, u, dynFX,
            asphaltDiffTex, asphaltNormTex, asphaltRoughTex,
            concreteDiffTex, concreteNormTex,
            carbonDiffTex, carbonNormTex,
            carLiveryMaskTex, carMiscTex, carBadgeTex,
            carHeadlightTex, carTaillightTex, carTireTex, carInteriorTex,
            repeatSamp
        );

        // Exact Reverse-Z depth (nearZ=0.20, farZ=800.0 -> 1.0 at near, 0.0 at far) + static/dynamic motion vectors
        RTInstanceMeta hitInst = instances[hit.instanceID];
        float4 currClip = u.currViewProjMatrix * float4(s.worldPos, 1.0f);
        float viewZ = max(currClip.w, 0.20f);
        clipDepth = clamp((0.20f * (800.0f - viewZ)) / (viewZ * 799.8f), 0.0f, 1.0f);
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
                        worldVertices, worldIndices, worldMaterials, worldTextures, u, dynFX,
                        asphaltDiffTex, asphaltNormTex, asphaltRoughTex,
                        concreteDiffTex, concreteNormTex,
                        carbonDiffTex, carbonNormTex,
                        carLiveryMaskTex, carMiscTex, carBadgeTex,
                        carHeadlightTex, carTaillightTex, carTireTex, carInteriorTex,
                        repeatSamp
                    );
                    reflCol = evaluateSecondaryHitRadiance(
                        rr.origin, rr.direction, rs, tlas,
                        instances, worldVertices, worldIndices, worldMaterials, worldTextures, repeatSamp, u, xiShad, !temporal1SPP
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
                    worldVertices, worldIndices, worldMaterials, worldTextures, u, dynFX,
                    asphaltDiffTex, asphaltNormTex, asphaltRoughTex,
                    concreteDiffTex, concreteNormTex,
                    carbonDiffTex, carbonNormTex,
                    carLiveryMaskTex, carMiscTex, carBadgeTex,
                    carHeadlightTex, carTaillightTex, carTireTex, carInteriorTex,
                    repeatSamp
                );
                transCol = evaluateSecondaryHitRadiance(
                    tr.origin, tr.direction, ts, tlas,
                    instances, worldVertices, worldIndices, worldMaterials, worldTextures, repeatSamp, u, xiShad, true
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
            evaluateLocalAreaLightsSplit(s, V, u, localDiffIrrad, localSpec);

            float NdotV = max(dot(s.N, V), 1e-4f);
            float3 F0 = mix(float3(0.04f), s.baseAlbedo, s.metallic);
            float3 F_env = F_Schlick(NdotV, F0);
            float f0_cc = pow((s.ior - 1.0f) / (s.ior + 1.0f), 2.0f);
            float F_cc = (s.clearcoat > 0.01f) ? F_SchlickScalar(max(dot(s.clearcoatN, V), 1e-4f), f0_cc) * s.clearcoat : 0.0f;

            float3 baseSpecRefl = float3(0.0f);
            float3 ccSpecRefl = float3(0.0f);
            // Physical rough-surface grazing Fresnel attenuation on dry matte asphalt (needed for lobe weights!)
            float3 F_envSpec = (s.matID == MAT_WET_ASPHALT)
                ? F_env * mix(1.0f, 0.28f, smoothstep(0.32f, 0.76f, s.roughness))
                : F_env;

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
                            worldVertices, worldIndices, worldMaterials, worldTextures, u, dynFX,
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
                            instances, worldVertices, worldIndices, worldMaterials, worldTextures, repeatSamp, u, xiShad, secShad
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
                            worldVertices, worldIndices, worldMaterials, worldTextures, u, dynFX,
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
                            instances, worldVertices, worldIndices, worldMaterials, worldTextures, repeatSamp, u, xiShad, secShad
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
                                worldVertices, worldIndices, worldMaterials, worldTextures, u, dynFX,
                                asphaltDiffTex, asphaltNormTex, asphaltRoughTex,
                                concreteDiffTex, concreteNormTex,
                                carbonDiffTex, carbonNormTex,
                                carLiveryMaskTex, carMiscTex, carBadgeTex,
                                carHeadlightTex, carTaillightTex, carTireTex, carInteriorTex,
                                repeatSamp
                            );
                            ccSpecRefl = evaluateSecondaryHitRadiance(
                                cr.origin, cr.direction, cs, tlas,
                                instances, worldVertices, worldIndices, worldMaterials, worldTextures, repeatSamp, u, xiShad, true
                            );
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
                        worldVertices, worldIndices, worldMaterials, worldTextures, u, dynFX,
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
                        instances, worldVertices, worldIndices, worldMaterials, worldTextures, repeatSamp, u, xiShad, !temporal1SPP
                    ), float3(6.5f));
                    giIrrad = bounceRad * 0.68f + sampleSkyDome(s.N, u, false, false) * 0.14f * ao;
                }
            } else {
                giIrrad = sampleSkyDome(s.N, u, false, false) * u.coronaColor.w * 0.48f;
            }

            bool isWorldGeom = ((hitInst.flags & (1u << 4)) != 0u);
            bool isDynamicCar = (!isWorldGeom && (hitInst.flags & 4u) == 0u);
            float3 skyFill = (u.skyZenith.rgb * 0.55f + u.skyHorizon.rgb * 0.45f) * u.coronaColor.w * 0.28f;
            if (isWorldGeom) {
                // Attenuate skyFill on ROAD_ASPHALT (0.35x) to keep tarmac rich charcoal; lift shaded walls/foliage (+36%)
                float roadAtten = (s.matID == MAT_WET_ASPHALT) ? 0.35f : 1.0f;
                float giBoost = (s.matID == MAT_WET_ASPHALT) ? 1.06f : 1.36f;
                giIrrad = giIrrad * giBoost + skyFill * roadAtten * (0.55f + 0.45f * max(s.N.y, 0.0f));
                // Keep subtle under-car bounce fill on road near car while preserving tight tire contact shadow
                float2 dCarXZ = s.worldPos.xz - u.carWorldPos.xz;
                float carNear = smoothstep(3.8f, 1.4f, length(dCarXZ));
                float contactTight = smoothstep(0.06f, 0.28f, s.worldPos.y - u.carWorldPos.y + 0.22f);
                giIrrad = max(giIrrad, skyFill * (0.14f * carNear * contactTight));
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
                // Open-city aerial perspective (0.22x viaduct density): buildings stay razor-clear 150-300m down Rosewood & Downtown!
                float distFog = 1.0f - exp(-hit.distance * u.skyHorizon.w * 0.22f);
                fog = clamp(distFog, 0.0f, 0.52f);
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
            // High reactivity on thin wing & emissives (zero ghosting); moderate reactivity on rigid-tracked tires/rims (zero 1-SPP speckle!)
            bool highReactivePart = (s.matID == MAT_CARBON_FIBER || s.matID == MAT_TAILLIGHT_EMIT || s.matID == MAT_HEADLIGHT_EMIT);
            bool wheelPart = (s.matID == MAT_RUBBER_TIRE || s.matID == MAT_ALLOY_RIM);
            gReactiveVal = highReactivePart
                ? 0.92f
                : (wheelPart ? 0.38f : ((length(gEmissive) > 0.5f) ? 0.82f : (isDynamicCar ? 0.42f : 0.0f)));
        }
    }

    // Subtle atmospheric diagonal falling rain streaks in wet Rockport night_rain scenes (TRUE NIGHT ONLY)
    float rainNightGate = smoothstep(0.032f, 0.015f, u.skyZenith.r);
    if (hasRkptWorld && rainNightGate > 0.45f && u.skyZenith.w > 0.72f) {
        float rainStreak = 0.0f;
        float2 pxNorm = float2(gid) / resF;
        // 12-degree wind-slanted rain coordinates across 2 depth parallax planes (decoupled from sampleIdx!)
        float2 slantUV = float2(pxNorm.x * u.cameraForward.w + pxNorm.y * 0.21f, pxNorm.y);
        float tRain = u.envParams.y * 0.22f + dynFX.timeWeather.x * 1.45f;
        for (int rl = 0; rl < 2; ++rl) {
            float cols = (rl == 0) ? 135.0f : 215.0f;
            float rows = (rl == 0) ? 10.5f : 17.5f;
            float colCoord = slantUV.x * cols + float(rl) * 37.1f;
            float colIdx = floor(colCoord);
            // Per-column random vertical phase stagger eliminates uniform horizontal rain-streak comb bands!
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
            outSpecRad += rainCol * min(rainStreak, 0.24f) * rainNightGate;
            gReactiveVal = max(gReactiveVal, 0.65f);
        }
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
    outP[0] = res;
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

    float3 outRGB = (encodeSRGB != 0u) ? srgb : srgbToLinear(srgb);
    outTex.write(float4(outRGB, 1.0f), gid);
}
