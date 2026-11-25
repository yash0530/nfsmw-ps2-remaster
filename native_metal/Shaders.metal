#include <metal_stdlib>
using namespace metal;

struct VertexIn {
    float3 position [[attribute(0)]];
    float3 normal   [[attribute(1)]];
    float2 uv       [[attribute(2)]];
    float3 modelPos [[attribute(3)]];
};

struct VertexOut {
    float4 clipPos  [[position]];
    float3 worldPos;
    float3 worldNormal;
    float2 uv;
    float3 modelPos;
};

struct QuadOut {
    float4 clipPos [[position]];
    float2 uv;
};

struct FrameUniforms {
    float4x4 viewProjMatrix;
    float4x4 invViewProjMatrix;
    float4x4 modelMatrix;
    float4 cameraPos;    // xyz, w = fovDeg
    float4 sunDir;       // xyz normalized, w = sunIntensity
    float4 sunColor;     // rgb, w = exposure
    float4 coronaColor;  // rgb, w = iblIntensity
    float4 skyZenith;    // rgb, w = wetness
    float4 skyHorizon;   // rgb, w = fogDensity
    float4 skyGround;    // rgb, w = headlightsOn
    float4 postParams;   // x = bloomThreshold, y = bloomIntensity, z = liveryMode, w = encodeSRGB
    float4 screenSize;   // x = w, y = h, z = 1/w, w = 1/h
};

struct MaterialUniforms {
    float4 baseColor;       // rgb, w = alpha
    float4 secondaryColor;  // rgb, w = ior
    float4 pbrParams;       // x = metallic, y = roughness, z = clearcoat, w = clearcoatRoughness
    float4 emissive;        // rgb, w = materialKind
    float4 flags;           // x = liveryEnabled, y = hasTexture, z = uvScale, w = unused
};

constant float PI = 3.14159265359;

// ---------------------------------------------------------------------------
// Deterministic Hash & Value Noise Helpers
// ---------------------------------------------------------------------------

static inline float hash21(float2 p) {
    float3 p3 = fract(float3(p.xyx) * 0.1031);
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.x + p3.y) * p3.z);
}

static inline float3 hash33(float3 p3) {
    p3 = fract(p3 * float3(0.1031, 0.1030, 0.0973));
    p3 += dot(p3, p3.yxz + 33.33);
    return fract((p3.xxy + p3.yxx) * p3.zyx);
}

static inline float noise2D(float2 p) {
    float2 i = floor(p);
    float2 f = fract(p);
    float2 u = f * f * (3.0 - 2.0 * f);
    float a = hash21(i);
    float b = hash21(i + float2(1.0, 0.0));
    float c = hash21(i + float2(0.0, 1.0));
    float d = hash21(i + float2(1.0, 1.0));
    return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

static inline float fbm2D(float2 p) {
    float sum = 0.0;
    float amp = 0.5;
    float2x2 rot = float2x2(0.8, 0.6, -0.6, 0.8);
    for (int i = 0; i < 4; ++i) {
        sum += amp * noise2D(p);
        p = rot * p * 2.04 + float2(13.7, 7.3);
        amp *= 0.5;
    }
    return sum;
}

// ---------------------------------------------------------------------------
// Procedural Nishita Sky-Dome + Rockport Skyline + Analytic IBL
// ---------------------------------------------------------------------------

static float3 sampleSkyDome(float3 dir, constant FrameUniforms& u, bool includeSunDisc) {
    float3 d = normalize(dir);
    float3 s = normalize(u.sunDir.xyz);
    float up = clamp(d.y, -1.0, 1.0);

    // Atmospheric Rayleigh/Mie gradient
    float horizonFactor = pow(1.0 - clamp(abs(up), 0.0, 1.0), 3.6);
    float zenithFactor = pow(clamp(up, 0.0, 1.0), 0.58);
    float3 sky = mix(u.skyHorizon.rgb, u.skyZenith.rgb, zenithFactor);

    // Warm/cool Mie scattering lobe around sun azimuth near horizon
    float sunDot = max(dot(d, s), 0.0);
    float mieAzimuth = pow(sunDot, 4.0) * (0.45 + 0.55 * horizonFactor);
    sky += u.coronaColor.rgb * mieAzimuth * 0.85;
    sky = mix(sky, u.skyHorizon.rgb * 1.18 + u.coronaColor.rgb * 0.25, horizonFactor * 0.52);

    // Below-horizon ground reflection bounce
    if (up < 0.0) {
        float gFade = clamp(-up * 6.0, 0.0, 1.0);
        sky = mix(sky, u.skyGround.rgb, gFade);
    }

    // Distant Rockport Downtown Skyline silhouette softly misted into horizon haze
    if (up >= -0.005 && up < 0.08) {
        float az = atan2(d.z, d.x); // [-pi, pi]
        float b1 = floor((az + PI) * 32.0);
        float b2 = floor((az + PI) * 74.0);
        float h1 = hash21(float2(b1, 3.1)) * 0.028 + 0.004;
        float h2 = hash21(float2(b2, 9.7)) * 0.018 + 0.003;
        float district = smoothstep(-0.3, 0.6, sin(az * 2.0 + 0.6)) * 0.020;
        float buildingH = max(h1, h2) + district * hash21(float2(b1, 17.3));
        float sunClear = 1.0 - 0.55 * pow(max(dot(normalize(float3(d.x, 0.0, d.z)), normalize(float3(s.x, 0.0, s.z))), 0.0), 6.0);
        buildingH *= sunClear;

        if (up < buildingH) {
            float3 hazeBase = mix(u.skyHorizon.rgb * 0.42, float3(0.05, 0.06, 0.09), 0.58);
            float winX = fract((az + PI) * 240.0);
            float winY = fract(up * 680.0);
            float lit = step(0.76, hash21(float2(floor((az + PI) * 240.0), floor(up * 680.0))));
            float duskAmt = clamp(1.1 - dot(u.skyHorizon.rgb, float3(0.333)), 0.10, 0.85);
            if (winX > 0.3 && winX < 0.7 && winY > 0.35 && winY < 0.65 && lit > 0.5) {
                hazeBase += mix(u.coronaColor.rgb, float3(1.0, 0.84, 0.52), 0.5) * 0.20 * duskAmt;
            }
            float edge = smoothstep(buildingH, buildingH - 0.003, up) * 0.68;
            sky = mix(sky, hazeBase, edge);
        }
    }

    if (includeSunDisc) {
        float sunIntensity = u.sunDir.w;
        float corona = pow(sunDot, 22.0) * 0.90 + pow(sunDot, 160.0) * 3.0;
        float disc = smoothstep(0.9991, 0.99965, sunDot) * 14.0;
        sky += u.coronaColor.rgb * corona * (0.32 + 0.10 * sunIntensity);
        sky += u.sunColor.rgb * disc * max(sunIntensity * 0.42, 0.45);
    }

    return max(sky, float3(0.0));
}

static float3 sampleEnvironmentIBL(float3 reflDir, constant FrameUniforms& u, float roughness) {
    float3 d = normalize(reflDir);
    // Crisp sky sample for low-roughness clearcoat / chrome
    float3 sharpSky = sampleSkyDome(d, u, true);
    // Soft overhead studio reflection ribbon to accentuate widebody creases
    float softbox = smoothstep(0.45, 0.78, d.y) * smoothstep(0.96, 0.72, d.y) *
                    (0.55 + 0.45 * sin(d.x * 3.5));
    sharpSky += mix(u.sunColor.rgb, float3(0.85, 0.92, 1.0), 0.55) * softbox * 0.28;

    // Diffuse/rough irradiance approximation
    float3 upCol = u.skyZenith.rgb * 0.75 + u.skyHorizon.rgb * 0.35;
    float3 horizCol = u.skyHorizon.rgb * 0.85 + u.coronaColor.rgb * 0.25;
    float3 downCol = u.skyGround.rgb * 1.15;
    float3 roughEnv = mix(horizCol, upCol, clamp(d.y, 0.0, 1.0));
    roughEnv = mix(roughEnv, downCol, clamp(-d.y, 0.0, 1.0));

    float blend = clamp(roughness * roughness * 1.15, 0.0, 1.0);
    return mix(sharpSky, roughEnv, blend) * u.coronaColor.w;
}

// ---------------------------------------------------------------------------
// Cook-Torrance GGX PBR BRDF + Dual-Lobe Clearcoat
// ---------------------------------------------------------------------------

static inline float D_GGX(float NdotH, float alpha) {
    float a2 = alpha * alpha;
    float denom = NdotH * NdotH * (a2 - 1.0) + 1.0;
    return a2 / max(PI * denom * denom, 1e-5);
}

static inline float G_SmithGGX(float NdotV, float NdotL, float roughness) {
    float r = roughness + 1.0;
    float k = (r * r) * 0.125;
    float gv = NdotV / max(NdotV * (1.0 - k) + k, 1e-5);
    float gl = NdotL / max(NdotL * (1.0 - k) + k, 1e-5);
    return gv * gl;
}

static inline float3 F_Schlick(float cosTheta, float3 F0) {
    float f = pow(clamp(1.0 - cosTheta, 0.0, 1.0), 5.0);
    return F0 + (1.0 - F0) * f;
}

static inline float F_SchlickScalar(float cosTheta, float F0) {
    return F0 + (1.0 - F0) * pow(clamp(1.0 - cosTheta, 0.0, 1.0), 5.0);
}

// Analytical BMW M3 GTR E46 Silver & Cobalt Blue Racing Livery in Y-up model space
static float3 evaluateBMWLivery(float3 mPos, float3 silverBase, float3 blueStripe, thread float& stripeMask) {
    float ax = abs(mPos.x);
    float y = mPos.y;
    float z = mPos.z;

    // 1. Twin center speed stripes over hood, roof, and trunk
    float centerBand = smoothstep(0.24, 0.22, ax) - smoothstep(0.055, 0.038, ax);
    float upperBody = smoothstep(0.55, 0.62, y);

    // 2. Sweeping Most Wanted side-flank livery: wraps lower fender/door and rises onto rear quarter
    float flankSide = smoothstep(0.58, 0.66, ax);
    float diagonalTop = 0.56 - 0.15 * z;
    float diagonalBot = 0.24 - 0.11 * z;
    float flankSlash = smoothstep(diagonalBot - 0.025, diagonalBot + 0.025, y) *
                       smoothstep(diagonalTop + 0.025, diagonalTop - 0.025, y) *
                       smoothstep(-2.05, -1.92, z) * smoothstep(2.20, 2.05, z);

    // 3. Bold front fender & hood-edge sweep joining the flank into the BMW hood bulge
    float hoodSweep = smoothstep(0.38, 0.42, ax) * smoothstep(0.66, 0.60, ax) *
                      smoothstep(0.95, 1.08, z) * smoothstep(1.96, 1.84, z) *
                      smoothstep(0.46, 0.52, y);

    // 4. Rear quarter graphic rising into C-pillar/trunk edge
    float rearSweep = smoothstep(0.45, 0.52, ax) *
                      smoothstep(-0.75, -0.90, z) * smoothstep(-2.08, -1.96, z) *
                      smoothstep(0.58, 0.64, y) * smoothstep(1.04, 0.98, y);

    float innerMask = clamp(max(centerBand * upperBody, max(flankSide * flankSlash, max(hoodSweep, rearSweep))), 0.0, 1.0);
    // Crisp dark navy/silver pinstripe border along stripe transitions
    float pinstripe = smoothstep(0.12, 0.42, innerMask) * (1.0 - smoothstep(0.58, 0.88, innerMask));

    stripeMask = innerMask;
    float3 col = mix(silverBase, blueStripe, innerMask);
    col = mix(col, float3(0.015, 0.04, 0.14), pinstripe * 0.72);
    return col;
}

// ---------------------------------------------------------------------------
// Sky-Dome Pass
// ---------------------------------------------------------------------------

vertex QuadOut skyVertex(uint vid [[vertex_id]]) {
    float2 pos[3] = { float2(-1.0, -1.0), float2(3.0, -1.0), float2(-1.0, 3.0) };
    QuadOut out;
    out.clipPos = float4(pos[vid], 0.9999, 1.0);
    out.uv = pos[vid] * 0.5 + 0.5;
    return out;
}

fragment float4 skyFragment(QuadOut in [[stage_in]], constant FrameUniforms& u [[buffer(0)]]) {
    float2 ndc = float2(in.uv.x * 2.0 - 1.0, in.uv.y * 2.0 - 1.0);
    float4 worldFar = u.invViewProjMatrix * float4(ndc, 1.0, 1.0);
    float3 worldPos = worldFar.xyz / max(worldFar.w, 1e-5);
    float3 rayDir = normalize(worldPos - u.cameraPos.xyz);
    float3 col = sampleSkyDome(rayDir, u, true);
    return float4(col, 1.0);
}

// ---------------------------------------------------------------------------
// Car Mesh PBR Shader (Opaque + Glass Passes)
// ---------------------------------------------------------------------------

vertex VertexOut pbrVertex(VertexIn in [[stage_in]], constant FrameUniforms& u [[buffer(1)]]) {
    VertexOut out;
    float4 worldPos = u.modelMatrix * float4(in.position, 1.0);
    out.clipPos = u.viewProjMatrix * worldPos;
    out.worldPos = worldPos.xyz;
    out.worldNormal = normalize((u.modelMatrix * float4(in.normal, 0.0)).xyz);
    out.uv = in.uv;
    out.modelPos = in.modelPos;
    return out;
}

fragment float4 pbrFragment(
    VertexOut in [[stage_in]],
    bool isFront [[front_facing]],
    constant FrameUniforms& u [[buffer(1)]],
    constant MaterialUniforms& m [[buffer(2)]],
    texture2d<float> carbonTex [[texture(0)]],
    sampler texSampler [[sampler(0)]]
) {
    float3 N = normalize(in.worldNormal);
    float3 V = normalize(u.cameraPos.xyz - in.worldPos);
    if (dot(N, V) < 0.0) {
        N = -N;
    }
    float3 L = normalize(u.sunDir.xyz);
    float3 H = normalize(V + L);

    float3 baseAlbedo = m.baseColor.rgb;
    float metallic = m.pbrParams.x;
    float roughness = clamp(m.pbrParams.y, 0.03, 0.98);
    float clearcoat = m.pbrParams.z;
    float ccRoughness = clamp(m.pbrParams.w, 0.02, 0.5);
    float ior = m.secondaryColor.w;
    int kind = int(m.emissive.w + 0.5);
    float3 emissiveCol = m.emissive.rgb;

    // 1. BMW M3 GTR Dual-Lobe Metallic Clearcoat Paint + Micro-Flake Normal Perturbation
    float3 baseNormal = N;
    if (kind == 0) {
        if (m.flags.x > 0.5 && u.postParams.z > 0.5) {
            float stripeMask = 0.0;
            baseAlbedo = evaluateBMWLivery(in.modelPos, m.baseColor.rgb, m.secondaryColor.rgb, stripeMask);
            metallic = mix(m.pbrParams.x, 0.42, stripeMask);
            roughness = mix(m.pbrParams.y, 0.19, stripeMask);
        }
        // Subtle high-frequency metallic micro-flake shimmer in base coat
        float3 flakeSeed = floor(in.modelPos * 950.0);
        float3 flakeJitter = (hash33(flakeSeed) - 0.5) * 0.026;
        baseNormal = normalize(N + flakeJitter);
        if (dot(baseNormal, V) < 0.0) baseNormal = N;
        float flakeSparkle = pow(max(dot(baseNormal, H), 0.0), 64.0) * 0.09;
        baseAlbedo *= (1.0 + flakeSparkle);
    }

    // 2. Carbon Fiber Weave (CC0 texture or procedural 2x2 twill)
    if (kind == 3) {
        if (m.flags.y > 0.5) {
            float3 cfSample = carbonTex.sample(texSampler, in.uv * 8.0).rgb;
            baseAlbedo = mix(baseAlbedo, cfSample * 0.32, 0.75);
        } else {
            float2 cell = in.modelPos.xz * 85.0;
            float weave = sin(cell.x) * sin(cell.y);
            baseAlbedo *= (0.75 + 0.25 * weave);
        }
    }

    // 3. Emissive Headlights (6000K Xenon + Angel-Eye Halo) & Taillights
    float headScale = u.skyGround.w;
    if (kind == 6) {
        // Xenon projector core glow boosted at center of headlight assembly
        float halo = 0.65 + 0.35 * sin(in.modelPos.x * 28.0);
        emissiveCol *= headScale * halo;
    } else if (kind == 7) {
        float tailPulse = 0.8 + 0.2 * smoothstep(0.72, 0.82, in.modelPos.y);
        emissiveCol *= (0.75 + 0.45 * headScale) * tailPulse;
    }

    // 4. Direct Sun Cook-Torrance GGX Evaluation
    float NdotL = max(dot(baseNormal, L), 0.0);
    float NdotV = max(dot(baseNormal, V), 1e-4);
    float NdotH = max(dot(baseNormal, H), 0.0);
    float VdotH = max(dot(V, H), 0.0);

    float3 F0 = mix(float3(0.04), baseAlbedo, metallic);
    float alpha = roughness * roughness;
    float D = D_GGX(NdotH, alpha);
    float G = G_SmithGGX(NdotV, NdotL, roughness);
    float3 F = F_Schlick(VdotH, F0);
    float3 specBase = (D * G * F) / max(4.0 * NdotV * NdotL, 1e-4);
    float3 kD = (1.0 - F) * (1.0 - metallic);
    float3 diffBase = kD * baseAlbedo / PI;

    float3 sunRad = u.sunColor.rgb * u.sunDir.w;
    float3 directColor = (diffBase + specBase) * sunRad * NdotL;

    // 5. Analytic IBL Indirect Lighting (Base Coat)
    float3 R = reflect(-V, baseNormal);
    float3 envBase = sampleEnvironmentIBL(R, u, roughness);
    float3 envDiff = sampleEnvironmentIBL(baseNormal, u, 0.95);
    float3 F_env = F_Schlick(NdotV, F0);
    float3 indirectColor = (1.0 - F_env) * (1.0 - metallic) * baseAlbedo * envDiff * 0.55 + F_env * envBase;

    // 6. Dual-Lobe High-IOR Clearcoat Layer (IOR 1.5 -> F0 = 0.04)
    float3 color = directColor + indirectColor;
    if (clearcoat > 0.01) {
        float ccNdotL = max(dot(N, L), 0.0);
        float ccNdotV = max(dot(N, V), 1e-4);
        float ccNdotH = max(dot(N, H), 0.0);
        float f0_cc = pow((ior - 1.0) / (ior + 1.0), 2.0);
        float F_cc_direct = F_SchlickScalar(VdotH, f0_cc) * clearcoat;
        float ccAlpha = ccRoughness * ccRoughness;
        float D_cc = D_GGX(ccNdotH, ccAlpha);
        float G_cc = G_SmithGGX(ccNdotV, ccNdotL, ccRoughness);
        float specCC = (D_cc * G_cc * F_cc_direct) / max(4.0 * ccNdotV * ccNdotL, 1e-4);

        float3 R_cc = reflect(-V, N);
        float3 envCC = sampleEnvironmentIBL(R_cc, u, ccRoughness);
        float F_cc_env = F_SchlickScalar(ccNdotV, f0_cc) * clearcoat;

        // Energy-conserving attenuation of base coat underneath clearcoat
        color = color * (1.0 - F_cc_env) + specCC * sunRad * ccNdotL + envCC * F_cc_env;
    }

    // Ground bounce fill on underside normals so tires/rockers aren't crushed black
    float groundBounce = smoothstep(0.3, -0.8, N.y) * 0.12;
    color += baseAlbedo * (u.skyHorizon.rgb * 0.35 + u.skyGround.rgb) * groundBounce;
    color += emissiveCol;

    // Glass Fresnel opacity modulation
    float outAlpha = m.baseColor.w;
    if (kind == 5) {
        float fresnelGlass = F_SchlickScalar(max(dot(N, V), 0.0), 0.045);
        outAlpha = clamp(m.baseColor.w + fresnelGlass * 0.48, 0.14, 0.96);
    }

    return float4(color, outAlpha);
}

// ---------------------------------------------------------------------------
// Wet Asphalt Ground Plane + Puddle Roughness + Planar Car/Light Reflections
// ---------------------------------------------------------------------------

vertex VertexOut groundVertex(VertexIn in [[stage_in]], constant FrameUniforms& u [[buffer(1)]]) {
    VertexOut out;
    out.clipPos = u.viewProjMatrix * float4(in.position, 1.0);
    out.worldPos = in.position;
    out.worldNormal = float3(0.0, 1.0, 0.0);
    out.uv = in.uv;
    out.modelPos = in.position;
    return out;
}

fragment float4 groundFragment(
    VertexOut in [[stage_in]],
    constant FrameUniforms& u [[buffer(1)]],
    texture2d<float> asphaltTex [[texture(0)]],
    sampler texSampler [[sampler(0)]]
) {
    float2 wp = in.worldPos.xz;
    float wetness = clamp(u.skyZenith.w, 0.0, 1.0);

    // Sample CC0 4K wet asphalt texture with smooth dual-scale UV to eliminate tiling
    float2 uvA = wp * 0.28;
    float2 uvB = wp * 0.071 + float2(0.37, 0.61);
    float3 texA = asphaltTex.sample(texSampler, uvA).rgb;
    float3 texB = asphaltTex.sample(texSampler, uvB).rgb;
    float3 asphaltAlbedo = mix(texA, texB, 0.35) * 0.16;

    // Procedural domain-warped puddle mask (balanced ~35-45% organic puddles)
    float2 warp = float2(fbm2D(wp * 0.20), fbm2D(wp * 0.20 + float2(5.2, 1.3)));
    float puddleField = fbm2D(wp * 0.26 + warp * 1.45);
    float puddleThreshold = mix(0.80, 0.54, wetness);
    float puddleMask = smoothstep(puddleThreshold - 0.08, puddleThreshold + 0.08, puddleField);

    // Damp asphalt darkening & subtle road micro-normal + soft puddle breeze ripples
    float damp = smoothstep(0.28, 0.72, puddleField + wetness * 0.22);
    asphaltAlbedo *= mix(1.0, 0.38, max(damp * 0.78, puddleMask));

    float distToCam = length(in.worldPos - u.cameraPos.xyz);
    float microFade = 1.0 - smoothstep(10.0, 30.0, distToCam);
    float rippleX = (fbm2D(wp * 3.8) - 0.5) * 0.025 * puddleMask;
    float rippleZ = (fbm2D(wp * 3.8 + float2(7.1, 2.9)) - 0.5) * 0.025 * puddleMask;
    float microX = (noise2D(wp * 26.0) - 0.5) * 0.11 * (1.0 - puddleMask) * microFade + rippleX;
    float microZ = (noise2D(wp * 26.0 + float2(11.4, 3.8)) - 0.5) * 0.11 * (1.0 - puddleMask) * microFade + rippleZ;
    float3 N = normalize(float3(microX, 1.0, microZ));

    float roughness = mix(mix(0.72, 0.38, damp), 0.055, puddleMask);
    roughness = mix(roughness, 0.52, smoothstep(12.0, 34.0, distToCam));
    float3 V = normalize(u.cameraPos.xyz - in.worldPos);
    float3 L = normalize(u.sunDir.xyz);
    float3 H = normalize(V + L);

    float NdotL = max(dot(N, L), 0.0);
    float NdotV = max(dot(N, V), 1e-4);
    float NdotH = max(dot(N, H), 0.0);
    float VdotH = max(dot(V, H), 0.0);

    // Soft Car Contact AO + 4 Tire Contact Shadows + Directional Sun Shadow
    float2 carHalf = float2(0.95, 2.26);
    float2 carCenter = float2(0.0, 0.16);
    float2 dContact = abs(wp - carCenter) / carHalf;
    float contactDist = length(max(dContact - float2(0.58, 0.64), 0.0));
    float contactAO = mix(0.04, 1.0, smoothstep(0.0, 0.45, contactDist));
    // Tire patch dark grounding
    float tFL = smoothstep(0.16, 0.46, length((wp - float2(-0.80, 1.51)) * float2(1.4, 0.85)));
    float tFR = smoothstep(0.16, 0.46, length((wp - float2(+0.80, 1.51)) * float2(1.4, 0.85)));
    float tRL = smoothstep(0.16, 0.46, length((wp - float2(-0.80, -1.13)) * float2(1.4, 0.85)));
    float tRR = smoothstep(0.16, 0.46, length((wp - float2(+0.80, -1.13)) * float2(1.4, 0.85)));
    contactAO *= min(min(tFL, tFR), min(tRL, tRR)) * 0.88 + 0.12;

    // Directional sun-offset body shadow
    float2 sunProj = -L.xz / max(L.y, 0.28);
    float2 shadowCenter = carCenter + sunProj * 0.52;
    float2 dShadow = abs(wp - shadowCenter) / (carHalf * float2(1.04, 1.06));
    float shadowDist = length(max(dShadow - float2(0.72, 0.76), 0.0));
    float sunShadow = mix(0.12, 1.0, smoothstep(0.0, 0.40, shadowDist)) * mix(0.25, 1.0, contactAO);

    // Direct sun GGX specular on wet asphalt
    float3 F0 = float3(mix(0.035, 0.022, puddleMask));
    float alpha = roughness * roughness;
    float D = D_GGX(NdotH, alpha);
    float G = G_SmithGGX(NdotV, NdotL, roughness);
    float3 F = F_Schlick(VdotH, F0);
    float3 spec = (D * G * F) / max(4.0 * NdotV * NdotL, 1e-4);
    float3 diff = (1.0 - F) * asphaltAlbedo / PI;
    float3 sunRad = u.sunColor.rgb * u.sunDir.w;

    float3 direct = (diff + spec) * sunRad * NdotL * sunShadow;

    // Puddle & wet asphalt IBL reflection + analytic car/headlight/taillight puddle reflection
    float3 R = reflect(-V, N);
    float3 envRefl = sampleEnvironmentIBL(R, u, roughness);
    float3 envDiff = sampleEnvironmentIBL(N, u, 0.96);
    float3 F_env = F_Schlick(NdotV, F0);

    // Analytic reflected car silhouette darkening + xenon/taillight streaks on wet ground
    float headScale = u.skyGround.w;
    float tHit = (0.55 - in.worldPos.y) / max(R.y, 0.05);
    if (R.y > 0.01 && tHit > 0.0 && tHit < 12.0) {
        float2 hitXZ = wp + R.xz * tHit;
        float2 dCarRefl = abs(hitXZ - carCenter) / float2(0.95, 2.25);
        float carReflMask = 1.0 - smoothstep(0.75, 1.15, max(dCarRefl.x, dCarRefl.y));
        envRefl *= mix(1.0, 0.22, carReflMask * (0.4 + 0.6 * puddleMask));
    }

    // Headlight & taillight wet-road reflection pools
    float frontGlowL = exp(-length((wp - float2(-0.60, 2.95)) * float2(1.8, 0.65)) * 1.45);
    float frontGlowR = exp(-length((wp - float2(+0.60, 2.95)) * float2(1.8, 0.65)) * 1.45);
    float rearGlowL  = exp(-length((wp - float2(-0.60, -2.45)) * float2(1.8, 0.72)) * 1.65);
    float rearGlowR  = exp(-length((wp - float2(+0.60, -2.45)) * float2(1.8, 0.72)) * 1.65);

    float glossBoost = mix(0.16, 0.85, puddleMask);
    float3 lightPools = float3(0.68, 0.82, 1.0) * (frontGlowL + frontGlowR) * headScale * 0.45 * glossBoost +
                        float3(1.0, 0.10, 0.03) * (rearGlowL + rearGlowR) * (0.55 + 0.45 * headScale) * 0.48 * glossBoost;

    float3 indirect = (1.0 - F_env) * asphaltAlbedo * envDiff * 0.5 * contactAO +
                      F_env * envRefl * mix(0.14, 1.0, contactAO) * mix(0.35, 1.0, sunShadow) +
                      lightPools;

    float3 color = direct + indirect;

    // Distance atmospheric horizon blend so ground merges seamlessly into sky horizon
    float dist = length(in.worldPos - u.cameraPos.xyz);
    float fog = 1.0 - exp(-pow(dist * u.skyHorizon.w * 2.2, 1.6));
    float3 fogCol = sampleSkyDome(normalize(float3(V.x * -1.0, 0.02, V.z * -1.0)), u, false);
    color = mix(color, fogCol, clamp(fog, 0.0, 1.0));

    return float4(color, 1.0);
}

// ---------------------------------------------------------------------------
// HDR Bloom Threshold, Separable 9-Tap Gaussian Blur & ACES Filmic Composite
// ---------------------------------------------------------------------------

vertex QuadOut fullscreenVertex(uint vid [[vertex_id]]) {
    float2 pos[3] = { float2(-1.0, -1.0), float2(3.0, -1.0), float2(-1.0, 3.0) };
    QuadOut out;
    out.clipPos = float4(pos[vid], 0.0, 1.0);
    out.uv = float2((pos[vid].x + 1.0) * 0.5, 1.0 - (pos[vid].y + 1.0) * 0.5);
    return out;
}

fragment float4 bloomThresholdFragment(
    QuadOut in [[stage_in]],
    constant FrameUniforms& u [[buffer(0)]],
    texture2d<float> hdrScene [[texture(0)]],
    sampler s [[sampler(0)]]
) {
    float3 col = hdrScene.sample(s, in.uv).rgb;
    float lum = dot(col, float3(0.2126, 0.7152, 0.0722));
    float thresh = u.postParams.x;
    float soft = clamp((lum - thresh * 0.7) / max(thresh * 0.6, 1e-4), 0.0, 1.0);
    float contrib = max(lum - thresh, 0.0) + soft * soft * 0.25;
    float3 bright = col * (contrib / max(lum, 1e-4));
    return float4(min(bright, float3(12.0)), 1.0);
}

fragment float4 bloomBlurFragment(
    QuadOut in [[stage_in]],
    constant float4& dirStep [[buffer(0)]],
    texture2d<float> srcTex [[texture(0)]],
    sampler s [[sampler(0)]]
) {
    // 9-tap separable Gaussian kernel
    const float weights[5] = { 0.227027, 0.1945946, 0.1216216, 0.054054, 0.016216 };
    float3 sum = srcTex.sample(s, in.uv).rgb * weights[0];
    for (int i = 1; i < 5; ++i) {
        float2 off = dirStep.xy * float(i);
        sum += srcTex.sample(s, in.uv + off).rgb * weights[i];
        sum += srcTex.sample(s, in.uv - off).rgb * weights[i];
    }
    return float4(sum, 1.0);
}

static inline float3 acesFilmic(float3 x) {
    const float a = 2.51;
    const float b = 0.03;
    const float c = 2.43;
    const float d = 0.59;
    const float e = 0.14;
    return clamp((x * (a * x + b)) / (x * (c * x + d) + e), 0.0, 1.0);
}

fragment float4 tonemapCompositeFragment(
    QuadOut in [[stage_in]],
    constant FrameUniforms& u [[buffer(0)]],
    texture2d<float> hdrScene [[texture(0)]],
    texture2d<float> bloomTex [[texture(1)]],
    sampler s [[sampler(0)]]
) {
    float3 hdr = hdrScene.sample(s, in.uv).rgb;
    float3 bloom = bloomTex.sample(s, in.uv).rgb;
    float3 col = (hdr + bloom * u.postParams.y) * u.sunColor.w;

    // Subtle cinematic edge vignette
    float2 vigUV = in.uv * 2.0 - 1.0;
    float vig = 1.0 - dot(vigUV, vigUV) * 0.14;
    col *= clamp(vig, 0.72, 1.0);

    col = acesFilmic(col);
    if (u.postParams.w > 0.5) {
        col = pow(col, float3(1.0 / 2.2));
    }
    return float4(col, 1.0);
}
