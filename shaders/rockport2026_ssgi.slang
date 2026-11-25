#version 450

// NFSMW: Rockport 2026 — Screen-Space Ray-Traced GI & Wet-Road SSR Shader
// Target pipeline: PCSX2 librashader (.slang / .slangp) via Vulkan or Metal (PR #14433)
// License: MIT (Original clean-room shader authored for NFSMW: Rockport 2026)
//
// Notes on PCSX2 post-processing scope:
//   librashader post-FX passes receive the presented RGBA color frame (`Source`),
//   without raw PS2 GS Z-buffer or G-buffer normals. This shader reconstructs an
//   approximate screen-space perspective elevation & luminance-gradient pseudo-normal
//   field to evaluate:
//     1. Multi-direction Screen-Space Global Illumination (SSGI) color bounce.
//     2. Perspective-weighted Wet-Road Screen-Space Reflections (SSR) with Fresnel
//        and micro-ripple roughness perturbation in the lower road band.
//     3. Bilateral edge-preserving spatial filtering to suppress ray-march noise.

#pragma name Rockport2026_SSGI
#pragma format R16G16B16A16_SFLOAT

#pragma parameter SSGI_STRENGTH "SSGI Bounce Intensity" 0.32 0.0 1.2 0.02
#pragma parameter SSGI_RADIUS "SSGI Sample Radius (px)" 48.0 8.0 128.0 4.0
#pragma parameter SSR_WETNESS "Wet-Road SSR Strength" 0.45 0.0 1.0 0.05
#pragma parameter SSR_HORIZON_Y "Road Horizon Split (UV Y)" 0.56 0.35 0.78 0.01
#pragma parameter SSR_ROUGHNESS "Wet Asphalt Micro-Roughness" 0.14 0.02 0.60 0.02
#pragma parameter DENOISE_BLEND "Bilateral Denoise Blend" 0.65 0.0 1.0 0.05

layout(push_constant) uniform Push
{
    vec4 SourceSize;
    vec4 OriginalSize;
    vec4 OutputSize;
    uint FrameCount;
    float SSGI_STRENGTH;
    float SSGI_RADIUS;
    float SSR_WETNESS;
    float SSR_HORIZON_Y;
    float SSR_ROUGHNESS;
    float DENOISE_BLEND;
} params;

layout(std140, set = 0, binding = 0) uniform UBO
{
    mat4 MVP;
} global;

#pragma stage vertex
layout(location = 0) in vec4 Position;
layout(location = 1) in vec2 TexCoord;
layout(location = 0) out vec2 vTexCoord;

void main()
{
    gl_Position = global.MVP * Position;
    vTexCoord = TexCoord;
}

#pragma stage fragment
layout(location = 0) in vec2 vTexCoord;
layout(location = 0) out vec4 FragColor;
layout(set = 0, binding = 2) uniform sampler2D Source;

const float PI = 3.14159265359;

float luminance(vec3 c)
{
    return dot(c, vec3(0.2126, 0.7152, 0.0722));
}

// Interleaved Gradient Noise (Jimenez 2014) for low-discrepancy ray jitter
float interleavedGradientNoise(vec2 px, uint frame)
{
    vec2 p = px + float(frame & 63u) * vec2(5.588238, 1.31831);
    return fract(52.9829189 * fract(dot(p, vec2(0.06711056, 0.00583715))));
}

vec2 hash22(vec2 p)
{
    vec3 p3 = fract(vec3(p.xyx) * vec3(0.1031, 0.1030, 0.0973));
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.xx + p3.yz) * p3.zy);
}

// Reconstruct approximate screen-space normal from luminance relief + road plane bias
vec3 estimateScreenNormal(vec2 uv, vec2 texel)
{
    float lC = luminance(texture(Source, uv).rgb);
    float lR = luminance(texture(Source, uv + vec2(texel.x, 0.0)).rgb);
    float lL = luminance(texture(Source, uv - vec2(texel.x, 0.0)).rgb);
    float lU = luminance(texture(Source, uv - vec2(0.0, texel.y)).rgb);
    float lD = luminance(texture(Source, uv + vec2(0.0, texel.y)).rgb);

    vec2 grad = vec2(lR - lL, lD - lU) * 3.5;
    // Bias road surface normals upward below the horizon split
    float roadMask = smoothstep(params.SSR_HORIZON_Y, params.SSR_HORIZON_Y + 0.18, uv.y);
    vec3 reliefNormal = normalize(vec3(-grad.x, 0.45 - grad.y * 0.5, 1.0));
    vec3 roadNormal = normalize(vec3(-grad.x * 0.35, 1.0, 0.25));
    return normalize(mix(reliefNormal, roadNormal, roadMask * 0.72));
}

// 8-direction golden-angle Screen-Space Global Illumination gather
vec3 computeSSGI(vec2 uv, vec3 baseCol, vec3 normal, vec2 texel, float jitter)
{
    const int NUM_DIRS = 8;
    const int STEPS = 3;
    float baseLuma = luminance(baseCol);
    vec3 giAccum = vec3(0.0);
    float weightSum = 0.0;

    float goldenAngle = 2.39996323;
    vec2 radiusUV = params.SSGI_RADIUS * texel;

    for (int d = 0; d < NUM_DIRS; ++d)
    {
        float ang = float(d) * goldenAngle + jitter * 2.0 * PI;
        vec2 dir = vec2(cos(ang), sin(ang));

        for (int s = 1; s <= STEPS; ++s)
        {
            float frac = (float(s) - 0.35 + jitter * 0.3) / float(STEPS);
            vec2 sampleUV = uv + dir * radiusUV * frac;
            if (sampleUV.x <= 0.002 || sampleUV.x >= 0.998 || sampleUV.y <= 0.002 || sampleUV.y >= 0.998)
            {
                continue;
            }

            vec3 sCol = texture(Source, sampleUV).rgb;
            float sLuma = luminance(sCol);
            // Highlight-weighted indirect bounce (headlights, tailbar, sun glints, sky)
            float emitWeight = smoothstep(0.18, 0.95, sLuma);
            float distFalloff = 1.0 / (1.0 + frac * frac * 3.0);
            float lumaDiff = abs(sLuma - baseLuma);
            float edgeWeight = exp(-lumaDiff * 2.2);
            float nDot = clamp(dot(normal.xy, dir) * 0.5 + 0.65, 0.15, 1.0);

            float w = (0.25 + 1.45 * emitWeight) * distFalloff * edgeWeight * nDot;
            giAccum += sCol * sCol * w; // Emphasize saturated/bright emitters in linear space
            weightSum += w;
        }
    }

    if (weightSum <= 1e-4)
    {
        return vec3(0.0);
    }
    return giAccum / weightSum;
}

// Perspective wet-road Screen-Space Reflection march
vec3 computeWetRoadSSR(vec2 uv, vec3 baseCol, vec2 texel, float jitter)
{
    float roadBand = smoothstep(params.SSR_HORIZON_Y, params.SSR_HORIZON_Y + 0.14, uv.y);
    // Keep HUD edges near bottom corners gently attenuated
    float edgeFade = smoothstep(0.0, 0.08, uv.x) * smoothstep(1.0, 0.92, uv.x)
                   * smoothstep(1.0, 0.90, uv.y);
    float mask = roadBand * edgeFade;
    if (mask <= 0.005 || params.SSR_WETNESS <= 0.001)
    {
        return vec3(0.0);
    }

    // Procedural wet-asphalt puddle & roughness variation
    vec2 ripple = hash22(floor(uv * params.SourceSize.xy * 0.5)) - 0.5;
    float puddleNoise = sin(uv.x * 29.0 + uv.y * 17.0) * cos(uv.x * 13.0 - uv.y * 23.0);
    float puddleMask = smoothstep(-0.15, 0.55, puddleNoise);
    float localRoughness = mix(params.SSR_ROUGHNESS * 1.8, params.SSR_ROUGHNESS * 0.45, puddleMask);

    // Distance below horizon determines reflection ray travel above the horizon
    float distFromHorizon = max(uv.y - params.SSR_HORIZON_Y, 0.01);
    vec3 reflAccum = vec3(0.0);
    float reflWeight = 0.0;

    const int SSR_TAPS = 6;
    for (int i = 0; i < SSR_TAPS; ++i)
    {
        float t = (float(i) + jitter) / float(SSR_TAPS);
        // Mirror across horizon with perspective compression and micro-ripple jitter
        float mirrorY = params.SSR_HORIZON_Y - distFromHorizon * (0.55 + 0.85 * t);
        vec2 tapUV = vec2(
            uv.x + ripple.x * localRoughness * 0.035 + (t - 0.5) * localRoughness * 0.015,
            clamp(mirrorY + ripple.y * localRoughness * 0.02, 0.02, params.SSR_HORIZON_Y)
        );

        vec3 tapCol = texture(Source, tapUV).rgb;
        float tapLuma = luminance(tapCol);
        float w = (0.35 + 1.2 * smoothstep(0.25, 0.95, tapLuma)) * (1.0 - 0.45 * t);
        reflAccum += tapCol * w;
        reflWeight += w;
    }

    vec3 reflected = (reflWeight > 1e-4) ? (reflAccum / reflWeight) : vec3(0.0);

    // Schlick Fresnel approximation at grazing road view angles (F0 = 0.028 for water/asphalt)
    float grazing = clamp(1.0 - distFromHorizon * 1.65, 0.15, 0.92);
    float fresnel = 0.028 + (1.0 - 0.028) * pow(grazing, 3.2);
    float darkRoadFactor = mix(1.0, 0.45, smoothstep(0.25, 0.75, luminance(baseCol)));

    return reflected * fresnel * mask * (0.45 + 0.55 * puddleMask) * darkRoadFactor * 1.65;
}

// Joint bilateral 3x3 stabilization pass over the local color neighborhood
vec3 bilateralStabilize(vec2 uv, vec3 centerCol, vec2 texel)
{
    float centerLuma = luminance(centerCol);
    vec3 sum = centerCol * 2.0;
    float wSum = 2.0;

    for (int y = -1; y <= 1; ++y)
    {
        for (int x = -1; x <= 1; ++x)
        {
            if (x == 0 && y == 0) continue;
            vec2 oUV = clamp(uv + vec2(float(x), float(y)) * texel, vec2(0.001), vec2(0.999));
            vec3 nCol = texture(Source, oUV).rgb;
            float dLuma = abs(luminance(nCol) - centerLuma);
            float w = exp(-dLuma * 14.0);
            sum += nCol * w;
            wSum += w;
        }
    }
    return sum / wSum;
}

void main()
{
    vec2 texel = params.SourceSize.zw;
    vec3 rawCol = texture(Source, vTexCoord).rgb;
    vec3 stabilized = mix(rawCol, bilateralStabilize(vTexCoord, rawCol, texel), params.DENOISE_BLEND);

    float jitter = interleavedGradientNoise(vTexCoord * params.SourceSize.xy, params.FrameCount);
    vec3 screenNormal = estimateScreenNormal(vTexCoord, texel);

    vec3 gi = computeSSGI(vTexCoord, stabilized, screenNormal, texel, jitter);
    vec3 ssr = computeWetRoadSSR(vTexCoord, stabilized, texel, jitter);

    // Energy-conserving diffuse GI + glossy SSR composite
    vec3 lit = rawCol + gi * params.SSGI_STRENGTH * (0.35 + 0.65 * rawCol) + ssr * params.SSR_WETNESS;
    FragColor = vec4(max(lit, vec3(0.0)), 1.0);
}
