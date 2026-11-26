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
    uint flags; // bit 0: liveryEnabled, bit 1: isGlass, bit 2: isWorldEnv, bit 3: isScrollableRoad
    float4x4 modelMatrix;
    float4x4 invTransposeMatrix;
};

struct RTMaterial {
    float4 baseColor;      // rgb, w = alpha
    float4 secondaryColor; // rgb, w = ior
    float4 pbrParams;      // x = metallic, y = roughness, z = clearcoat, w = clearcoatRoughness
    float4 emissive;       // rgb, w = flags
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
};

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

static HitSurface resolveHitSurface(
    float3 rayOrigin,
    float3 rayDir,
    float dist,
    uint instanceID,
    uint primitiveID,
    float2 bary,
    device const RTVertex* vertices,
    device const uint* indices,
    device const uint* triMaterials,
    device const RTInstanceMeta* instances,
    device const RTMaterial* materials,
    constant RTFrameUniforms& u,
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
            s.metallic  = mix(0.85f, 0.42f, stripeMask);
            s.roughness = mix(0.18f, 0.22f, stripeMask);
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
        s.ccRoughness = clamp(u.envParams.z, 0.009f, 0.18f);
    } else if (s.matID == MAT_CAR_PAINT_SILVER) {
        // Secondary factory body paint (DIABLOHP: splitter lip, mirrors, trim matching base factory paint)
        s.baseAlbedo = mat.baseColor.rgb;
        if (carID == 2u) {
            float viewFacing = clamp(dot(worldN, -rayDir), 0.0f, 1.0f);
            s.baseAlbedo = mix(float3(0.88f, 0.52f, 0.014f), float3(1.00f, 0.80f, 0.06f), pow(viewFacing, 0.55f));
        }
        s.metallic   = mat.pbrParams.x;
        s.roughness  = clamp(mat.pbrParams.y, 0.12f, 0.30f);
        s.clearcoat  = 1.0f;
        s.ccRoughness = clamp(u.envParams.z, 0.010f, 0.18f);
        s.clearcoatN = worldN;
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
            if (carID == 0u) {
                float onHoodBlue = step(1.95f, s.modelPos.z) * step(abs(s.modelPos.x), 0.15f) * step(0.55f, s.modelPos.y);
                paintBase = mix(paintBase, materials[MAT_CAR_PAINT].secondaryColor.rgb, onHoodBlue);
            }
            s.baseAlbedo = mix(paintBase, min(badge.rgb * 1.15f, float3(0.98f)), badge.a);
            s.metallic   = mix(materials[MAT_CAR_PAINT].pbrParams.x, 0.32f, badge.a);
            s.roughness  = mix(0.18f, 0.14f, badge.a);
            s.clearcoat  = 1.0f;
            s.ccRoughness = 0.016f;
            s.clearcoatN = worldN;
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
        // Real OEM tire sidewall & tread driven by extracted per-car TIRE texture
        float frontZ = (carID == 1u) ? 1.205f : ((carID == 2u) ? 1.160f : 1.595f);
        float rearZ  = (carID == 1u) ? -1.480f : ((carID == 2u) ? -1.390f : -1.1295f);
        float wz = (s.modelPos.z > 0.0f) ? frontZ : rearZ;
        float wy = (s.modelPos.z > 0.0f) ? 0.320f : ((carID == 1u) ? 0.340f : 0.330f);
        float2 relYZ = float2(s.modelPos.y - wy, s.modelPos.z - wz);
        float rad = length(relYZ);
        float theta = atan2(relYZ.x, relYZ.y);
        float isSidewall = smoothstep(0.42f, 0.68f, abs(localNormal.x));
        float3 t, b;
        buildONB(worldN, t, b);
        float3 tTex = hasAuthenticTex ? carTireTex.sample(repeatSamp, carUV, level(0.0f)).rgb : float3(0.18f);
        float microGrain = noise2D(float2(theta * 28.0f, rad * 180.0f));
        float treadX = abs(s.modelPos.x);
        float circGroove = smoothstep(0.18f, 0.38f, abs(fract(treadX * 26.0f) - 0.5f));
        float treadPattern = mix(1.0f, mix(0.65f, 1.0f, circGroove), 1.0f - isSidewall);

        float3 tireBase = hasAuthenticTex ? clamp(tTex * 0.24f + float3(0.020f), float3(0.026f), float3(0.11f)) : float3(0.044f, 0.046f, 0.050f);
        s.baseAlbedo = tireBase * (0.90f + 0.20f * microGrain) * treadPattern;
        s.metallic   = 0.0f;
        s.roughness  = 0.52f + (microGrain - 0.5f) * 0.06f;
        s.clearcoat  = 0.0f;
        float bumpScale = mix(0.20f, 0.12f, isSidewall) * (1.0f - speedFrac * 0.5f);
        s.N = normalize(worldN + t * ((microGrain - 0.5f) * bumpScale));
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
        s.emissive = float3(0.0f);
        s.alpha = 0.04f;
        s.roughness = 0.012f;
    } else if (s.matID == MAT_HEADLIGHT_EMIT) {
        // 100% AUTHENTIC UV-MAPPED PER-CAR PROJECTOR HEADLIGHTS (BMW E46 / Porsche Carrera GT / Lamborghini Gallardo)
        float headOn = u.skyGround.w;
        float forwardGate = smoothstep(0.05f, 0.28f, localNormal.z);
        if (hasAuthenticTex) {
            float3 hlTex = carHeadlightTex.sample(repeatSamp, carUV, level(0.0f)).rgb;
            float lum = dot(hlTex, float3(0.2126f, 0.7152f, 0.0722f));
            s.baseAlbedo = pow(max(hlTex, float3(0.0f)), float3(1.25f)) * 0.88f;
            s.metallic   = mix(0.15f, 0.88f, smoothstep(0.18f, 0.58f, lum));
            s.roughness  = mix(0.58f, 0.12f, smoothstep(0.18f, 0.58f, lum));
            s.clearcoat  = 0.40f;

            if (carID == 0u) {
                // BMW M3 GTR E46: left bowl u~0.18 (high-beam), center projector u~0.53 (xenon), right u>0.70 (amber turn)
                float dXenon = length(float2((carUV.x - 0.535f) * 2.0f, carUV.y - 0.48f));
                float dHigh  = length(float2((carUV.x - 0.185f) * 2.0f, carUV.y - 0.50f));
                float xenonLens = smoothstep(0.19f, 0.06f, dXenon);
                float xenonHalo = smoothstep(0.042f, 0.012f, abs(dXenon - 0.275f)) * smoothstep(0.08f, 0.25f, carUV.y);
                float highBulb  = smoothstep(0.16f, 0.04f, dHigh);
                float amberTurn = smoothstep(0.68f, 0.75f, carUV.x) * smoothstep(0.12f, 0.35f, hlTex.r - hlTex.b);
                s.emissive   = (float3(5.2f, 5.8f, 7.2f) * xenonLens
                             +  float3(2.8f, 3.2f, 4.0f) * xenonHalo * 0.65f
                             +  float3(2.6f, 2.3f, 1.8f) * highBulb * 0.75f
                             +  float3(2.2f, 0.72f, 0.06f) * amberTurn * 0.55f) * headOn * forwardGate;
            } else if (carID == 1u) {
                // Porsche Carrera GT (005_CARRERAGT_KIT00_HEADLIG): bi-xenon lens (0.26, 0.20), high-beam bowl (0.74, 0.24), lower amber strip (y~0.60)
                float dXenon = length(float2((carUV.x - 0.26f) * 1.45f, (carUV.y - 0.20f) * 1.85f));
                float dHigh  = length(float2((carUV.x - 0.74f) * 1.50f, (carUV.y - 0.24f) * 1.55f));
                float xenonLens = smoothstep(0.24f, 0.06f, dXenon);
                float highBulb  = smoothstep(0.22f, 0.06f, dHigh);
                float amberStrip = smoothstep(0.50f, 0.56f, carUV.y) * smoothstep(0.70f, 0.63f, carUV.y) * smoothstep(0.06f, 0.22f, hlTex.r - hlTex.b);
                s.emissive   = (float3(5.6f, 6.2f, 7.6f) * xenonLens
                             +  float3(2.8f, 2.6f, 2.3f) * highBulb * 0.70f
                             +  float3(2.6f, 0.95f, 0.10f) * amberStrip * 0.75f) * headOn * forwardGate;
            } else {
                // Lamborghini Gallardo (001_GALLARDO_KIT00_HEADLIGH): bi-xenon projector (0.17, 0.68) + bright amber DRL/turn bowl (0.45, 0.68)
                float dXenon = length(float2((carUV.x - 0.17f) * 2.20f, (carUV.y - 0.68f) * 1.25f));
                float dAmber = length(float2((carUV.x - 0.45f) * 2.10f, (carUV.y - 0.68f) * 1.25f));
                float xenonLens = smoothstep(0.28f, 0.06f, dXenon);
                float amberBowl = smoothstep(0.32f, 0.08f, dAmber) * smoothstep(0.04f, 0.22f, hlTex.r - hlTex.b);
                s.baseAlbedo = pow(max(hlTex, float3(0.0f)), float3(1.40f)) * 0.62f;
                s.emissive   = (float3(6.2f, 6.8f, 8.2f) * xenonLens
                             +  float3(5.2f, 2.05f, 0.15f) * amberBowl * 1.15f) * headOn * forwardGate;
            }
        } else {
            s.baseAlbedo = float3(0.08f, 0.09f, 0.10f);
            s.metallic   = 0.80f;
            s.roughness  = 0.16f;
            s.emissive   = float3(3.8f, 4.2f, 5.0f) * headOn * forwardGate;
        }
    } else if (s.matID == MAT_TAILLIGHT_EMIT) {
        // 100% AUTHENTIC UV-MAPPED BMW E46 M3 LED TAILLIGHTS (BMWM3GTRE46_KIT00_BRAKE)
        float headOn = 0.82f + 0.55f * u.skyGround.w;
        if (hasAuthenticTex) {
            float3 tlTex = carTaillightTex.sample(repeatSamp, carUV, level(0.0f)).rgb;
            float redDom = clamp((tlTex.r - max(tlTex.g, tlTex.b)) * 2.2f, 0.0f, 1.0f);
            float whiteStrip = smoothstep(0.35f, 0.65f, min(tlTex.r, min(tlTex.g, tlTex.b)));
            s.baseAlbedo  = mix(tlTex * 0.55f, float3(0.45f, 0.48f, 0.52f), whiteStrip * 0.5f);
            s.metallic    = mix(0.08f, 0.30f, whiteStrip);
            s.roughness   = 0.05f;
            s.clearcoat   = 1.0f;
            s.ccRoughness = 0.015f;
            s.ior         = 1.54f;
            s.emissive    = (float3(5.8f, 0.09f, 0.03f) * pow(redDom, 1.25f) + float3(0.18f, 0.20f, 0.22f) * whiteStrip * 0.35f) * headOn;
        } else {
            s.baseAlbedo  = float3(0.16f, 0.008f, 0.006f);
            s.roughness   = 0.06f;
            s.clearcoat   = 1.0f;
            s.emissive    = float3(4.5f, 0.10f, 0.04f) * headOn;
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

    float3 carLights[4] = {
        rotateCarLocal(float3(-0.635f, 0.59f,  2.48f)),
        rotateCarLocal(float3( 0.635f, 0.59f,  2.48f)),
        rotateCarLocal(float3(-0.56f,  0.76f, -2.06f)),
        rotateCarLocal(float3( 0.56f,  0.76f, -2.06f))
    };
    float3 carDirs[4] = {
        normalize(rotateCarLocal(float3(0.0f, -0.10f,  1.0f))),
        normalize(rotateCarLocal(float3(0.0f, -0.10f,  1.0f))),
        normalize(rotateCarLocal(float3(0.0f, -0.22f, -1.0f))),
        normalize(rotateCarLocal(float3(0.0f, -0.22f, -1.0f)))
    };
    float3 carCols[4] = {
        float3(0.84f, 0.90f, 1.02f) * (3.6f * headScale),
        float3(0.84f, 0.90f, 1.02f) * (3.6f * headScale),
        float3(1.55f, 0.055f, 0.022f) * (2.6f * (0.75f + 0.55f * headScale)),
        float3(1.55f, 0.055f, 0.022f) * (2.6f * (0.75f + 0.55f * headScale))
    };

    bool isCarExterior = (s.matID <= MAT_LICENSE_PLATE || s.matID >= MAT_CAR_PAINT_SILVER);
    for (int i = 0; i < 4; ++i) {
        // Prevent the car's own headlights/taillights from blasting point-light fireflies onto its own bumper/fenders
        if (isCarExterior) continue;
        float3 toL = carLights[i] - s.worldPos;
        float d2 = dot(toL, toL) + 0.30f;
        float d = sqrt(d2);
        float3 L = toL / d;
        float NdotL = max(dot(s.N, L), 0.0f);
        if (NdotL > 0.0f) {
            float cone = (i < 2)
                ? smoothstep(0.76f, 0.96f, dot(-L, carDirs[i]))
                : smoothstep(0.18f, 0.80f, dot(-L, carDirs[i]));
            float atten = cone / (1.0f + d2 * 0.19f);
            float3 H = normalize(V + L);
            float specPow = mix(140.0f, 12.0f, s.roughness);
            float spec = pow(max(dot(s.N, H), 0.0f), specPow) * mix(0.60f, 0.08f, s.roughness);
            outDiffIrrad += (carCols[i] / PI) * NdotL * atten;
            outSpec += min(float3(spec) * carCols[i] * NdotL * atten, float3(4.5f));
        }
    }

    // Dynamic Scrolling Highway Sodium Lamps (x = +4.15, y = 6.55) + Industrial Warehouse Pendant Lamps (x = +7.45, y = 4.70)
    float lampPower = u.neonParams.w;
    if (lampPower > 0.02f) {
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
    float3 ambient = (1.0f - F) * (1.0f - s.metallic) * s.baseAlbedo * (u.skyZenith.rgb * 0.35f + u.skyHorizon.rgb * 0.45f) * u.coronaColor.w * 0.42f;
    float3 reflApprox = F * envUp;
    // Recessed smoked headlight housings sit deep under the hood/bezel overhang — apply cavity occlusion
    // so bright daylight sky never washes the optical bowl into milky grey!
    if (s.matID == MAT_HEADLIGHT_EMIT) {
        direct *= 0.22f;
        ambient *= 0.16f;
        reflApprox *= 0.14f;
    }

    float3 localDiff, localSpec;
    evaluateLocalAreaLightsSplit(s, V, u, localDiff, localSpec);
    float3 localTotal = localDiff * (1.0f - F) * (1.0f - s.metallic) * s.baseAlbedo + localSpec;

    return min(direct + ambient + reflApprox + min(s.emissive, float3(3.8f)) + localTotal, float3(9.0f));
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
    r.max_distance = 250.0f;

    auto hit = rt.intersect(r, tlas, 0xFFu);

    float3 outDiffIrrad = float3(0.0f);
    float3 outSpecRad   = float3(0.0f);
    float3 gAlbedo      = float3(0.0f);
    float4 gNormRough   = float4(0.0f, 1.0f, 0.0f, -1.0f);
    float  gDepth       = 250.0f;
    float3 gEmissive    = float3(0.0f);

    if (hit.type == intersection_type::none) {
        outSpecRad = sampleSkyDome(rayDir, u, true, true);
    } else {
        HitSurface s = resolveHitSurface(
            rayOrigin, rayDir, hit.distance,
            hit.instance_id, hit.primitive_id, hit.triangle_barycentric_coord,
            vertices, indices, triMaterials, instances, materials, u,
            asphaltDiffTex, asphaltNormTex, asphaltRoughTex,
            concreteDiffTex, concreteNormTex,
            carbonDiffTex, carbonNormTex,
            carLiveryMaskTex, carMiscTex, carBadgeTex,
            carHeadlightTex, carTaillightTex, carTireTex, carInteriorTex,
            repeatSamp
        );

        float3 V = -rayDir;
        gDepth = hit.distance;

        if (s.matID == MAT_SMOKED_GLASS || s.matID == MAT_HEADLIGHT_GLASS) {
            float NdotV = max(dot(s.N, V), 1e-4f);
            float f0 = pow((s.ior - 1.0f) / (s.ior + 1.0f), 2.0f);
            float F_glass = F_SchlickScalar(NdotV, f0);
            float reflWeight = (s.matID == MAT_HEADLIGHT_GLASS)
                ? clamp(F_glass * 0.075f, 0.006f, 0.046f)
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
                rr.max_distance = 120.0f;
                auto rh = rt.intersect(rr, tlas, 0xFFu);
                if (rh.type == intersection_type::triangle) {
                    HitSurface rs = resolveHitSurface(
                        rr.origin, rr.direction, rh.distance,
                        rh.instance_id, rh.primitive_id, rh.triangle_barycentric_coord,
                        vertices, indices, triMaterials, instances, materials, u,
                        asphaltDiffTex, asphaltNormTex, asphaltRoughTex,
                        concreteDiffTex, concreteNormTex,
                        carbonDiffTex, carbonNormTex,
                        carLiveryMaskTex, carMiscTex, carBadgeTex,
                        carHeadlightTex, carTaillightTex, carTireTex, carInteriorTex,
                        repeatSamp
                    );
                    reflCol = evaluateSecondaryHitRadiance(rr.origin, rr.direction, rs, tlas, u, xiShad);
                }
            }
            if (s.matID == MAT_SMOKED_GLASS) {
                reflCol = min(reflCol, float3(1.55f));
            }

            ray tr;
            tr.origin = s.worldPos + rayDir * 0.004f;
            tr.direction = rayDir;
            tr.min_distance = 0.002f;
            tr.max_distance = 60.0f;
            auto th = rt.intersect(tr, tlas, 0xFFu);
            float3 transCol = sampleSkyDome(rayDir, u, false, false);
            if (th.type == intersection_type::triangle) {
                HitSurface ts = resolveHitSurface(
                    tr.origin, tr.direction, th.distance,
                    th.instance_id, th.primitive_id, th.triangle_barycentric_coord,
                    vertices, indices, triMaterials, instances, materials, u,
                    asphaltDiffTex, asphaltNormTex, asphaltRoughTex,
                    concreteDiffTex, concreteNormTex,
                    carbonDiffTex, carbonNormTex,
                    carLiveryMaskTex, carMiscTex, carBadgeTex,
                    carHeadlightTex, carTaillightTex, carTireTex, carInteriorTex,
                    repeatSamp
                );
                transCol = evaluateSecondaryHitRadiance(tr.origin, tr.direction, ts, tlas, u, xiShad);
            }

            float3 L = normalize(u.sunDir.xyz);
            float3 sunSpec = evaluateDirectSunBRDF(s, V, L, u.sunColor.rgb * u.sunDir.w) * ((s.matID == MAT_HEADLIGHT_GLASS) ? 0.14f : 1.0f);
            float3 tint = (s.matID == MAT_HEADLIGHT_GLASS)
                ? float3(0.88f, 0.92f, 0.98f)
                : mix(s.baseAlbedo * 0.72f + float3(0.18f), float3(1.0f), 1.0f - s.alpha);

            outSpecRad = mix(transCol * tint, reflCol, reflWeight) + sunSpec;
            gEmissive = s.emissive;
            gNormRough = float4(s.N, 0.02f);
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
                sr.max_distance = 160.0f;
                auto sh = shadowRT.intersect(sr, tlas, 0x01u);
                sunVis = (sh.type == intersection_type::none) ? 1.0f : 0.0f;
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

            if (useRT) {
                float specRough = (s.matID == MAT_WET_ASPHALT) ? s.roughness : max(s.roughness, 0.035f);
                float3 H_base = sampleGGXVNDF(s.N, V, specRough, xiRefl);
                float3 R_base = reflect(-V, H_base);
                if (dot(R_base, s.geomN) <= 0.0f) R_base = reflect(-V, s.geomN);

                ray rr;
                rr.origin = s.worldPos + s.geomN * 0.004f;
                rr.direction = normalize(R_base);
                rr.min_distance = 0.003f;
                rr.max_distance = 140.0f;
                auto rh = rt.intersect(rr, tlas, 0xFFu);
                if (rh.type == intersection_type::none) {
                    // Direct sun/moon lighting is already evaluated analytically via evaluateDirectSunSplit;
                    // setting includeSunDisc = false eliminates double-counted sun/moon specular firefly glints!
                    baseSpecRefl = sampleSkyDome(rr.direction, u, false, false) * u.coronaColor.w;
                } else {
                    HitSurface rs = resolveHitSurface(
                        rr.origin, rr.direction, rh.distance,
                        rh.instance_id, rh.primitive_id, rh.triangle_barycentric_coord,
                        vertices, indices, triMaterials, instances, materials, u,
                        asphaltDiffTex, asphaltNormTex, asphaltRoughTex,
                        concreteDiffTex, concreteNormTex,
                        carbonDiffTex, carbonNormTex,
                        carLiveryMaskTex, carMiscTex, carBadgeTex,
                        carHeadlightTex, carTaillightTex, carTireTex, carInteriorTex,
                        repeatSamp
                    );
                    baseSpecRefl = evaluateSecondaryHitRadiance(rr.origin, rr.direction, rs, tlas, u, xiShad);
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
                    cr.max_distance = 140.0f;
                    auto ch = rt.intersect(cr, tlas, 0xFFu);
                    if (ch.type == intersection_type::none) {
                        ccSpecRefl = sampleSkyDome(cr.direction, u, false, false) * u.coronaColor.w;
                    } else {
                        HitSurface cs = resolveHitSurface(
                            cr.origin, cr.direction, ch.distance,
                            ch.instance_id, ch.primitive_id, ch.triangle_barycentric_coord,
                            vertices, indices, triMaterials, instances, materials, u,
                            asphaltDiffTex, asphaltNormTex, asphaltRoughTex,
                            concreteDiffTex, concreteNormTex,
                            carbonDiffTex, carbonNormTex,
                            carLiveryMaskTex, carMiscTex, carBadgeTex,
                            carHeadlightTex, carTaillightTex, carTireTex, carInteriorTex,
                            repeatSamp
                        );
                        ccSpecRefl = evaluateSecondaryHitRadiance(cr.origin, cr.direction, cs, tlas, u, xiShad);
                    }
                    ccSpecRefl = min(ccSpecRefl, float3(10.0f));
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
                gr.max_distance = 24.0f;
                auto gh = rt.intersect(gr, tlas, 0x01u);

                if (gh.type == intersection_type::none) {
                    giIrrad = sampleSkyDome(giDir, u, false, false) * u.coronaColor.w * 0.62f;
                } else {
                    float ao = smoothstep(0.02f, 2.2f, gh.distance);
                    HitSurface gs = resolveHitSurface(
                        gr.origin, gr.direction, gh.distance,
                        gh.instance_id, gh.primitive_id, gh.triangle_barycentric_coord,
                        vertices, indices, triMaterials, instances, materials, u,
                        asphaltDiffTex, asphaltNormTex, asphaltRoughTex,
                        concreteDiffTex, concreteNormTex,
                        carbonDiffTex, carbonNormTex,
                        carLiveryMaskTex, carMiscTex, carBadgeTex,
                        carHeadlightTex, carTaillightTex, carTireTex, carInteriorTex,
                        repeatSamp
                    );
                    float3 bounceRad = min(evaluateSecondaryHitRadiance(gr.origin, gr.direction, gs, tlas, u, xiShad), float3(6.5f));
                    giIrrad = bounceRad * 0.68f + sampleSkyDome(s.N, u, false, false) * 0.14f * ao;
                }
            } else {
                giIrrad = sampleSkyDome(s.N, u, false, false) * u.coronaColor.w * 0.48f;
            }

            float3 diffK = (1.0f - F_env) * (1.0f - s.metallic) * s.baseAlbedo;
            outDiffIrrad = directDiffIrrad + localDiffIrrad + giIrrad * (1.0f - F_cc);
            outSpecRad = (directSpec + localSpec + F_env * baseSpecRefl) * (1.0f - F_cc) + ccSpecRefl * F_cc;

            float fog = 1.0f - exp(-pow(hit.distance * u.skyHorizon.w * 1.05f, 1.45f));
            fog = clamp(fog, 0.0f, 0.85f);
            float3 fogCol = sampleSkyDome(normalize(float3(rayDir.x, 0.025f, rayDir.z)), u, false, false);

            gAlbedo = diffK * (1.0f - fog);
            outSpecRad = outSpecRad * (1.0f - fog) + fogCol * fog;
            gEmissive = s.emissive * (1.0f - fog);
            gNormRough = float4(s.N, s.roughness);
        }
    }

    // Subtle high-speed rear-tire wet mist / spray veil behind rear wheels when driving fast (--drive-demo)
    float driveSpd = clamp(u.driveParams.x / 180.0f, 0.0f, 1.0f);
    if (driveSpd > 0.30f && rayOrigin.z < -2.2f && rayDir.z > 0.35f) {
        // Evaluate subtle volumetric spray plume in two slices behind the rear tires (z = -2.25m and z = -2.85m)
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
                    // Sunlit golden mist + subtle crimson taillight scatter near upper wake
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
        }
    }

    outDiffIrrad = clamp(outDiffIrrad, float3(0.0f), float3(18.0f));
    outSpecRad   = clamp(outSpecRad,   float3(0.0f), float3(16.0f));

    if (sampleIdx == 0u) {
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

    float specRadiusScale = smoothstep(0.018f, 0.14f, roughP);
    if (stepSize > 2 && specRadiusScale < 0.08f) {
        specRadiusScale = 0.0f;
    }

    const float kernelW[3] = { 0.375f, 0.25f, 0.0625f };

    float3 sumDiff = cDiffP * (kernelW[0] * kernelW[0]);
    float  wSumDiff = kernelW[0] * kernelW[0];

    float3 sumSpec = cSpecP * (kernelW[0] * kernelW[0]);
    float  wSumSpec = kernelW[0] * kernelW[0];

    float sigmaLumDiff = 0.55f + 0.18f * float(stepSize);
    float sigmaLumSpec = 0.75f + 0.25f * float(stepSize);
    float depthSigma = max(0.06f * zP, 0.025f);

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

            float wN = pow(max(dot(nP, nQ), 0.0f), 48.0f);
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
                float wR = exp(-abs(roughP - nrQ.w) * 14.0f);
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

static inline float sdRoundedBoxHUD(float2 p, float2 b, float r) {
    float2 q = abs(p) - b + r;
    return length(max(q, float2(0.0f))) + min(max(q.x, q.y), 0.0f) - r;
}

static inline float sdSegmentHUD(float2 p, float2 a, float2 b) {
    float2 pa = p - a, ba = b - a;
    float h = clamp(dot(pa, ba) / max(dot(ba, ba), 1e-6f), 0.0f, 1.0f);
    return length(pa - ba * h);
}

// Crisp anti-aliased 7-segment SDF digit (0..9, 10='N') in local box [-0.5..0.5, -1.0..1.0] (y down)
static inline float evalSevenSegDigit(float2 p, int digit, float thickness) {
    const uint masks[11] = {
        0x7Eu, // 0: a b c d e f
        0x30u, // 1: b c
        0x6Du, // 2: a b d e g
        0x79u, // 3: a b c d g
        0x33u, // 4: b c f g
        0x5Bu, // 5: a c d f g
        0x5Fu, // 6: a c d e f g
        0x70u, // 7: a b c
        0x7Fu, // 8: all
        0x7Bu, // 9: a b c d f g
        0x76u  // 10 ('N' / neutral arch): a b c e f
    };
    uint m = masks[clamp(digit, 0, 10)];
    float w = 0.36f;
    float h = 0.84f;
    float g = 0.06f;
    float d = 1e5f;
    if (m & 0x40u) d = min(d, sdSegmentHUD(p, float2(-w + g, -h), float2( w - g, -h))); // a top
    if (m & 0x20u) d = min(d, sdSegmentHUD(p, float2( w, -h + g), float2( w, -g)));     // b upper-right
    if (m & 0x10u) d = min(d, sdSegmentHUD(p, float2( w,  g),     float2( w,  h - g))); // c lower-right
    if (m & 0x08u) d = min(d, sdSegmentHUD(p, float2(-w + g,  h), float2( w - g,  h))); // d bottom
    if (m & 0x04u) d = min(d, sdSegmentHUD(p, float2(-w,  g),     float2(-w,  h - g))); // e lower-left
    if (m & 0x02u) d = min(d, sdSegmentHUD(p, float2(-w, -h + g), float2(-w, -g)));     // f upper-left
    if (m & 0x01u) d = min(d, sdSegmentHUD(p, float2(-w + g, 0.0f), float2( w - g, 0.0f))); // g mid
    return smoothstep(thickness + 0.045f, thickness - 0.025f, d);
}

// Minimal crisp "KM/H" micro-glyphs in local box [0..1, 0..1]
static inline float evalKmhLabel(float2 p) {
    if (p.x < 0.0f || p.x > 1.0f || p.y < 0.0f || p.y > 1.0f) return 0.0f;
    float d = 1e5f;
    // K (x: 0.02..0.20)
    d = min(d, sdSegmentHUD(p, float2(0.04f, 0.12f), float2(0.04f, 0.88f)));
    d = min(d, sdSegmentHUD(p, float2(0.18f, 0.12f), float2(0.05f, 0.50f)));
    d = min(d, sdSegmentHUD(p, float2(0.05f, 0.50f), float2(0.19f, 0.88f)));
    // M (x: 0.27..0.52)
    d = min(d, sdSegmentHUD(p, float2(0.28f, 0.88f), float2(0.28f, 0.12f)));
    d = min(d, sdSegmentHUD(p, float2(0.28f, 0.12f), float2(0.39f, 0.56f)));
    d = min(d, sdSegmentHUD(p, float2(0.39f, 0.56f), float2(0.50f, 0.12f)));
    d = min(d, sdSegmentHUD(p, float2(0.50f, 0.12f), float2(0.50f, 0.88f)));
    // / (x: 0.58..0.68)
    d = min(d, sdSegmentHUD(p, float2(0.67f, 0.10f), float2(0.58f, 0.90f)));
    // H (x: 0.74..0.94)
    d = min(d, sdSegmentHUD(p, float2(0.76f, 0.12f), float2(0.76f, 0.88f)));
    d = min(d, sdSegmentHUD(p, float2(0.93f, 0.12f), float2(0.93f, 0.88f)));
    d = min(d, sdSegmentHUD(p, float2(0.76f, 0.50f), float2(0.93f, 0.50f)));
    return smoothstep(0.048f, 0.022f, d);
}

// Sleek GT7 / NFSMW minimal frosted digital driving telemetry strip (bottom-right)
static inline float3 compositeDigitalDrivingHUD(float3 baseCol, float2 px, float2 res, constant RTFrameUniforms& u) {
    float scale = res.y / 1080.0f;
    float2 panelSize = float2(332.0f, 118.0f) * scale;
    float2 margin = float2(46.0f, 38.0f) * scale;
    float2 center = res - margin - panelSize * 0.5f;
    float2 rel = (px - center) / scale; // centered pixels in 1080p virtual coordinates

    float dBox = sdRoundedBoxHUD(rel, float2(166.0f, 59.0f), 11.0f);
    if (dBox > 10.0f) return baseCol;

    // Subtle soft drop shadow outside panel
    float shadow = smoothstep(10.0f, 0.0f, dBox) * 0.28f;
    float3 outCol = baseCol * (1.0f - shadow * step(0.0f, dBox));

    float panelMask = smoothstep(1.2f, -0.8f, dBox);
    if (panelMask <= 0.001f) return outCol;

    // Dark smoked translucent glass backing + thin metallic rim
    float3 glassBg = mix(baseCol * 0.30f, float3(0.032f, 0.036f, 0.044f), 0.74f);
    // Subtle top sheen inside glass card
    float topSheen = smoothstep(-20.0f, -58.0f, rel.y) * 0.055f;
    glassBg += float3(topSheen);
    float border = smoothstep(1.8f, 0.4f, abs(dBox));
    glassBg = mix(glassBg, float3(0.42f, 0.46f, 0.52f), border * 0.52f);

    // Top thin Rockport amber accent bar (left side)
    float accentBar = step(-150.0f, rel.x) * step(rel.x, -72.0f) * step(-54.0f, rel.y) * step(rel.y, -51.5f);
    glassBg = mix(glassBg, float3(0.96f, 0.58f, 0.10f), accentBar * 0.90f);

    // 1. Segmented 18-bar RPM tachometer across bottom of card (rel.x in [-148..+148], rel.y in [30..43])
    float rpmNorm = clamp(u.hudParams.y, 0.0f, 1.0f);
    if (rel.x >= -148.0f && rel.x <= 148.0f && rel.y >= 28.0f && rel.y <= 45.0f) {
        float uBar = (rel.x + 148.0f) / 296.0f;
        float segF = uBar * 18.0f;
        int segIdx = int(floor(segF));
        float segLocal = fract(segF);
        float inSeg = step(0.10f, segLocal) * step(segLocal, 0.86f) * step(30.5f, rel.y) * step(rel.y, 42.5f);
        float segThresh = (float(segIdx) + 0.5f) / 18.0f;
        float lit = step(segThresh, rpmNorm);
        float3 barCol = float3(0.94f, 0.96f, 0.99f);
        if (segIdx >= 11 && segIdx < 15) barCol = float3(0.98f, 0.62f, 0.08f);
        else if (segIdx >= 15) barCol = float3(0.96f, 0.14f, 0.10f);
        float3 unlitCol = float3(0.14f, 0.16f, 0.19f);
        glassBg = mix(glassBg, mix(unlitCol, barCol, lit), inSeg * 0.94f);
    }

    // 2. Digital Gear Indicator Pill on left (centered at rel = (-114, -10), half-size 30x30)
    float2 gRel = rel - float2(-114.0f, -10.0f);
    float dGearBox = sdRoundedBoxHUD(gRel, float2(29.0f, 29.0f), 7.0f);
    float gearBgMask = smoothstep(1.0f, -0.8f, dGearBox);
    float gearBorder = smoothstep(1.4f, 0.3f, abs(dGearBox));
    glassBg = mix(glassBg, float3(0.085f, 0.075f, 0.055f), gearBgMask * 0.85f);
    glassBg = mix(glassBg, float3(0.96f, 0.62f, 0.12f), gearBorder * 0.85f);

    int gearInt = int(u.hudParams.z + 0.5f);
    int gearGlyph = (gearInt <= 0) ? 10 : clamp(gearInt, 1, 6);
    float2 gDigitP = gRel / float2(23.0f, 21.0f);
    float gLit = evalSevenSegDigit(gDigitP, gearGlyph, 0.115f);
    glassBg = mix(glassBg, float3(1.00f, 0.72f, 0.16f), gLit * gearBgMask);

    // 3. Digital 3-digit Speedometer (right side, crisp pure white)
    int spd = clamp(int(u.driveParams.x + 0.5f), 0, 999);
    int d100 = (spd / 100) % 10;
    int d10  = (spd / 10) % 10;
    int d1   = spd % 10;
    float digCentersX[3] = { -18.0f, 24.0f, 66.0f };
    int digits[3] = { d100, d10, d1 };
    bool showDig[3] = { spd >= 100, spd >= 10, true };
    for (int k = 0; k < 3; ++k) {
        float2 dp = (rel - float2(digCentersX[k], -13.0f)) / float2(30.0f, 27.0f);
        if (abs(dp.x) < 0.7f && abs(dp.y) < 1.2f) {
            float segGhost = evalSevenSegDigit(dp, 8, 0.105f);
            glassBg = mix(glassBg, float3(0.11f, 0.12f, 0.15f), segGhost * 0.55f);
            if (showDig[k]) {
                float segOn = evalSevenSegDigit(dp, digits[k], 0.105f);
                glassBg = mix(glassBg, float3(0.98f, 0.99f, 1.00f), segOn);
            }
        }
    }

    // 4. Crisp "KM/H" unit label on far right of speed
    float2 kmhP = (rel - float2(96.0f, -1.0f)) / float2(52.0f, 16.0f);
    float kmhMask = evalKmhLabel(kmhP);
    glassBg = mix(glassBg, float3(0.68f, 0.73f, 0.80f), kmhMask * 0.92f);

    return mix(outCol, glassBg, panelMask);
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

    float2 v = uv * 2.0f - 1.0f;
    float vigStrength = clamp(u.gradeParams.x, 0.0f, 0.6f);
    float vig = 1.0f - dot(v, v) * vigStrength;
    combined *= clamp(vig, 0.55f, 1.0f);

    float3 mapped = acesFilmic(combined);

    float contrast = clamp(u.gradeParams.y, 0.5f, 1.8f);
    float sat = clamp(u.gradeParams.z, 0.2f, 1.8f);
    mapped = clamp((mapped - 0.5f) * contrast + 0.5f, 0.0f, 1.0f);
    float lum = dot(mapped, float3(0.2126f, 0.7152f, 0.0722f));
    mapped = clamp(mix(float3(lum), mapped, sat), 0.0f, 1.0f);

    if (u.hudParams.x > 0.5f) {
        mapped = compositeDigitalDrivingHUD(mapped, float2(gid) + 0.5f, float2(float(w), float(h)), u);
    }

    if (encodeSRGB != 0u) {
        mapped = linearToSRGB(mapped);
    }
    outTex.write(float4(mapped, 1.0f), gid);
}
