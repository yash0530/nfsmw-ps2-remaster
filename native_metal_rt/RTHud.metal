// ---------------------------------------------------------------------------
// RTHud.metal — Standalone Post-FrameGen Crisp SDF Driving HUD Overlay
// Concatenated after RayTracing.metal at runtime before device.makeLibrary(source:)
// so HUD digits NEVER warp under MTLFXFrameInterpolator and require zero #include.
// ---------------------------------------------------------------------------

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
            glassBg = mix(glassBg, float3(0.055f, 0.060f, 0.072f), segGhost * 0.28f);
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

// Standalone Post-FrameGen HUD + Presentation Output Kernel
// Reads perceptual sRGB LDR input (real or MTLFXFrameInterpolator output), composites crisp
// unwarped SDF HUD, and writes either perceptual sRGB (headless bgra8Unorm) or linear (sRGB drawable).
kernel void hudOverlayKernel(
    uint2 gid [[thread_position_in_grid]],
    texture2d<float, access::read> inLdrTex [[texture(0)]],
    texture2d<float, access::write> outTex [[texture(1)]],
    constant RTFrameUniforms& u [[buffer(0)]],
    constant uint& encodeSRGB [[buffer(1)]]
) {
    uint w = outTex.get_width();
    uint h = outTex.get_height();
    if (gid.x >= w || gid.y >= h) return;

    float3 srgb = inLdrTex.read(gid).rgb;
    if (u.hudParams.x > 0.5f) {
        srgb = compositeDigitalDrivingHUD(srgb, float2(gid) + 0.5f, float2(float(w), float(h)), u);
    }

    float3 outRGB = (encodeSRGB != 0u) ? srgb : srgbToLinear(srgb);
    outTex.write(float4(outRGB, 1.0f), gid);
}
