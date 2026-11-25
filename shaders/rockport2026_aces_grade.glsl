#version 450

// NFSMW: Rockport 2026 — ACES Filmic Tone-Map, Bloom, Micro-Contrast & Dehaze Shader
// Target pipeline: PCSX2 librashader / Vulkan GLSL post-processing pass
// License: MIT (Original clean-room shader authored for NFSMW: Rockport 2026)
//
// Pipeline stages per fragment:
//   1. Linearization (sRGB -> Linear)
//   2. Atmospheric Dehaze (low-frequency atmospheric veil estimation & subtraction)
//   3. Dual-ring Karis-weighted HDR Bloom extraction & soft glow composite
//   4. Edge-aware Micro-Contrast (CAS-style unsharp luminance enhancement)
//   5. Fitted ACES RRT+ODT Filmic Tone Mapping (Hill / Narkowicz AP1 matrix fit)
//   6. Neutral Rockport 2026 daylight/golden-hour color balance & sRGB encoding

#ifdef VERTEX_SHADER
layout(location = 0) in vec4 a_pos;
layout(location = 1) in vec2 a_tex;
layout(location = 0) out vec2 v_tex;

void main()
{
    gl_Position = vec4(a_pos.x, a_pos.y, a_pos.z, a_pos.w);
    v_tex = a_tex;
}
#endif

#if defined(FRAGMENT_SHADER) || !defined(VERTEX_SHADER)
layout(push_constant) uniform PushConstants
{
    vec4 u_source_size;     // xy = width, height; zw = 1/width, 1/height
    float u_exposure;       // default 1.08
    float u_bloom_thresh;   // default 0.78
    float u_bloom_int;      // default 0.24
    float u_dehaze;         // default 0.18
    float u_micro_contrast; // default 0.22
    float u_saturation;     // default 1.06
} pc;

layout(set = 0, binding = 0) uniform sampler2D u_source_tex;

layout(location = 0) in vec2 v_tex;
layout(location = 0) out vec4 o_col0;

const vec3 LUMA_REC709 = vec3(0.2126, 0.7152, 0.0722);

vec3 srgbToLinear(vec3 c)
{
    vec3 lo = c / 12.92;
    vec3 hi = pow(max((c + 0.055) / 1.055, vec3(0.0)), vec3(2.4));
    return mix(lo, hi, step(vec3(0.04045), c));
}

vec3 linearToSrgb(vec3 c)
{
    c = clamp(c, vec3(0.0), vec3(1.0));
    vec3 lo = c * 12.92;
    vec3 hi = 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055;
    return mix(lo, hi, step(vec3(0.0031308), c));
}

float luminance(vec3 c)
{
    return dot(c, LUMA_REC709);
}

// ACES RRT+ODT Fitted Matrix Transform (Stephen Hill)
vec3 rrtAndOdtFit(vec3 v)
{
    vec3 a = v * (v + 0.0245786) - 0.000090537;
    vec3 b = v * (0.983729 * v + 0.4329510) + 0.238081;
    return a / max(b, vec3(1e-5));
}

vec3 acesFilmicToneMap(vec3 color)
{
    const mat3 ACES_INPUT_MAT = mat3(
        0.59719, 0.07600, 0.02840,
        0.35458, 0.90834, 0.13383,
        0.04823, 0.01566, 0.83777
    );
    const mat3 ACES_OUTPUT_MAT = mat3(
         1.60475, -0.10208, -0.00327,
        -0.53108,  1.10813, -0.07276,
        -0.07367, -0.00605,  1.07602
    );

    vec3 ap1 = ACES_INPUT_MAT * max(color, vec3(0.0));
    vec3 mapped = rrtAndOdtFit(ap1);
    return clamp(ACES_OUTPUT_MAT * mapped, vec3(0.0), vec3(1.0));
}

// Soft thresholded 8-tap Karis-averaged bloom
vec3 sampleSoftBloom(vec2 uv, vec2 texel, float threshold)
{
    const vec2 offsets[8] = vec2[8](
        vec2(-1.5, -1.5), vec2(0.0, -2.0), vec2(1.5, -1.5),
        vec2(-2.0,  0.0),                  vec2(2.0,  0.0),
        vec2(-1.5,  1.5), vec2(0.0,  2.0), vec2(1.5,  1.5)
    );

    vec3 bloom = vec3(0.0);
    float totalW = 0.0;

    for (int i = 0; i < 8; ++i)
    {
        vec2 suv = clamp(uv + offsets[i] * texel * 2.5, vec2(0.001), vec2(0.999));
        vec3 s = srgbToLinear(texture(u_source_tex, suv).rgb);
        float l = luminance(s);
        float soft = smoothstep(threshold * 0.75, threshold + 0.35, l);
        float karis = 1.0 / (1.0 + l);
        float w = soft * karis;
        bloom += s * w;
        totalW += karis;
    }

    return bloom / max(totalW, 1e-4);
}

// Estimate local atmospheric haze veil via dark-channel min-neighborhood
vec3 applyDehaze(vec3 linearCol, vec2 uv, vec2 texel, float strength)
{
    if (strength <= 0.001)
    {
        return linearCol;
    }

    vec3 minNeighborhood = linearCol;
    const vec2 veilOffsets[4] = vec2[4](
        vec2(-6.0, -6.0), vec2(6.0, -6.0),
        vec2(-6.0,  6.0), vec2(6.0,  6.0)
    );
    for (int i = 0; i < 4; ++i)
    {
        vec2 suv = clamp(uv + veilOffsets[i] * texel, vec2(0.001), vec2(0.999));
        vec3 s = srgbToLinear(texture(u_source_tex, suv).rgb);
        minNeighborhood = min(minNeighborhood, s);
    }

    float darkChannel = min(minNeighborhood.r, min(minNeighborhood.g, minNeighborhood.b));
    float transmission = clamp(1.0 - strength * darkChannel * 1.45, 0.35, 1.0);
    vec3 airlight = vec3(0.48, 0.52, 0.58) * darkChannel * strength * 0.55;
    return max((linearCol - airlight) / transmission, vec3(0.0));
}

// Edge-preserving local micro-contrast (clamped to avoid overshoot halos)
vec3 applyMicroContrast(vec3 linearCol, vec2 uv, vec2 texel, float amount)
{
    if (amount <= 0.001)
    {
        return linearCol;
    }
    vec3 n = srgbToLinear(texture(u_source_tex, clamp(uv + vec2(0.0, -texel.y), vec2(0.001), vec2(0.999))).rgb);
    vec3 s = srgbToLinear(texture(u_source_tex, clamp(uv + vec2(0.0,  texel.y), vec2(0.001), vec2(0.999))).rgb);
    vec3 e = srgbToLinear(texture(u_source_tex, clamp(uv + vec2( texel.x, 0.0), vec2(0.001), vec2(0.999))).rgb);
    vec3 w = srgbToLinear(texture(u_source_tex, clamp(uv + vec2(-texel.x, 0.0), vec2(0.001), vec2(0.999))).rgb);

    vec3 avg = 0.25 * (n + s + e + w);
    vec3 detail = clamp(linearCol - avg, vec3(-0.18), vec3(0.18));
    return max(linearCol + detail * amount, vec3(0.0));
}

void main()
{
    vec2 texel = (pc.u_source_size.z > 0.0) ? pc.u_source_size.zw : vec2(1.0 / 1920.0, 1.0 / 1080.0);
    float exposure = (pc.u_exposure > 0.0) ? pc.u_exposure : 1.08;
    float bloomThresh = (pc.u_bloom_thresh > 0.0) ? pc.u_bloom_thresh : 0.78;
    float bloomInt = (pc.u_bloom_int > 0.0) ? pc.u_bloom_int : 0.24;
    float dehazeAmt = (pc.u_dehaze > 0.0) ? pc.u_dehaze : 0.18;
    float microAmt = (pc.u_micro_contrast > 0.0) ? pc.u_micro_contrast : 0.22;
    float satAmt = (pc.u_saturation > 0.0) ? pc.u_saturation : 1.06;

    vec3 linearCol = srgbToLinear(texture(u_source_tex, v_tex).rgb);
    linearCol = applyDehaze(linearCol, v_tex, texel, dehazeAmt);
    linearCol = applyMicroContrast(linearCol, v_tex, texel, microAmt);

    vec3 bloom = sampleSoftBloom(v_tex, texel, bloomThresh);
    linearCol += bloom * bloomInt;

    vec3 mapped = acesFilmicToneMap(linearCol * exposure);
    float luma = luminance(mapped);
    mapped = mix(vec3(luma), mapped, satAmt);

    o_col0 = vec4(linearToSrgb(mapped), 1.0);
}
#endif
