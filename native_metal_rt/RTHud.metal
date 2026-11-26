// ---------------------------------------------------------------------------
// RTHud.metal — Authentic NFSMW 2005 Post-FrameGen Crisp SDF Driving HUD + GPU Minimap
// Concatenated after RayTracing.metal at runtime before device.makeLibrary(source:)
// so HUD digits & minimap NEVER warp under MTLFXFrameInterpolator (zero #include).
//
// Locked Buffer Slots (per #40 / #41 + v1.1.0 EA TRAX extension):
//   buffer(0) = RTFrameUniforms       (unchanged base contract)
//   buffer(1) = uint encodeSRGB       (unchanged base contract)
//   buffer(2) = RTHudExtUniforms      (64 bytes, 16B aligned)
//   buffer(3) = RTMinimapBuffer       (1,936 bytes <= 2 KB, 16B aligned)
//   buffer(4) = RTMusicBannerUniforms (112 bytes <= 128 B, 16B aligned)
// ---------------------------------------------------------------------------

struct RTHudExtUniforms {
    float rpmNorm;          // 0..1 (falls back to u.hudParams.y if 0)
    float nosNorm;          // 0..1
    float sbNorm;           // 0..1 (Speedbreaker meter)
    float fps;              // > 0 displays top-right FPS pill

    float heatLevel;        // 0..5 (0 hides pursuit bar)
    float bounty;           // USD bounty
    float evadeProgress;    // 0..1
    float bustedProgress;   // 0..1

    float timeOfDayHours;   // 0..24
    float simTimeSec;       // continuous clock for siren/redline flash
    float driftIntensity;   // 0..1
    float speedKmhOverride; // > 0 overrides u.driveParams.x

    uint hudFlags;          // bit0=visible, bit1=frameGenOn, bit2=nosActive, bit3=sbActive, bit4=pursuitActive, bit5=busted, bit6=evaded, bit7=hideMinimap
    uint copCount;          // active pursuing cops
    uint districtIndex;     // 0..5
    uint carIndex;          // 0..39
};
static_assert(sizeof(RTHudExtUniforms) == 64, "RTHudExtUniforms must be 64 bytes");

struct RTMinimapBuffer {
    uint segCount;            // 0..96
    uint blipCount;           // 0..24
    float mapRangeM;          // world radius shown in radar circle (m)
    float playerYawRad;       // heading for North compass tick
    float4 roadSegments[96];  // (x0, y0, x1, y1) heading-up normalized [-1..+1], +y = forward
    float4 blips[24];         // (x, y, typeFlags [0=traf,1=patrol,2=pursuit,3=mark], flashPhase)
};
static_assert(sizeof(RTMinimapBuffer) == 1936, "RTMinimapBuffer must be 1936 bytes (<= 2 KB)");

struct RTMusicBannerUniforms {
    float bannerAlpha;     // 0..1 overall opacity (0 = instant early-out)
    float slideT;          // 0..1 horizontal slide-in progress
    uint  trackIndex;      // 1-based track number
    uint  totalTracks;     // total tracks in playlist

    uint  flags;           // bit0=visible, bit1=muted, bit2=pursuitScore, bit3=tunnelReverb
    uint  artistLen;       // 0..32
    uint  titleLen;        // 0..40
    float skyOcclusion;    // 0..1 smoothed acoustic tunnel reverb depth

    uint  artistChars[8];  // 32 ASCII bytes packed LE
    uint  titleChars[10];  // 40 ASCII bytes packed LE
    uint  pad0;
    uint  pad1;
};
static_assert(sizeof(RTMusicBannerUniforms) == 112, "RTMusicBannerUniforms must be 112 bytes (<= 128 B, 16B aligned)");

static constant uint MUSIC_FLAG_VISIBLE       = 1u << 0;
static constant uint MUSIC_FLAG_MUTED         = 1u << 1;
static constant uint MUSIC_FLAG_PURSUIT_SCORE = 1u << 2;
static constant uint MUSIC_FLAG_TUNNEL_REVERB = 1u << 3;

static constant uint HUD_FLAG_VISIBLE        = 1u << 0;
static constant uint HUD_FLAG_FRAMEGEN_ON    = 1u << 1;
static constant uint HUD_FLAG_NOS_ACTIVE     = 1u << 2;
static constant uint HUD_FLAG_SB_ACTIVE      = 1u << 3;
static constant uint HUD_FLAG_PURSUIT_ACTIVE = 1u << 4;
static constant uint HUD_FLAG_BUSTED         = 1u << 5;
static constant uint HUD_FLAG_EVADED         = 1u << 6;
static constant uint HUD_FLAG_HIDE_MINIMAP   = 1u << 7;

// ---------------------------------------------------------------------------
// SDF Primitives & Micro-Typography
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

static inline float sdTriangleHUD(float2 p, float2 p0, float2 p1, float2 p2) {
    float2 e0 = p1 - p0, e1 = p2 - p1, e2 = p0 - p2;
    float2 v0 = p - p0,  v1 = p - p1,  v2 = p - p2;
    float2 pq0 = v0 - e0 * clamp(dot(v0, e0) / max(dot(e0, e0), 1e-6f), 0.0f, 1.0f);
    float2 pq1 = v1 - e1 * clamp(dot(v1, e1) / max(dot(e1, e1), 1e-6f), 0.0f, 1.0f);
    float2 pq2 = v2 - e2 * clamp(dot(v2, e2) / max(dot(e2, e2), 1e-6f), 0.0f, 1.0f);
    float s = sign(e0.x * e2.y - e0.y * e2.x);
    float2 d = min(min(float2(dot(pq0, pq0), s * (v0.x * e0.y - v0.y * e0.x)),
                       float2(dot(pq1, pq1), s * (v1.x * e1.y - v1.y * e1.x))),
                       float2(dot(pq2, pq2), s * (v2.x * e2.y - v2.y * e2.x)));
    return -sqrt(d.x) * sign(d.y);
}

// Crisp anti-aliased 7-segment SDF digit (0..9, 10='N', 11='R') in local box [-0.5..0.5, -1.0..1.0] (y down)
static inline float evalSevenSegDigit(float2 p, int digit, float thickness) {
    const uint masks[12] = {
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
        0x76u, // 10 ('N'): a b c e f
        0x05u  // 11 ('r' / Reverse): e g
    };
    uint m = masks[clamp(digit, 0, 11)];
    float w = 0.36f;
    float h = 0.84f;
    float g = 0.06f;
    float d = 1e5f;
    if (m & 0x40u) d = min(d, sdSegmentHUD(p, float2(-w + g, -h), float2( w - g, -h)));
    if (m & 0x20u) d = min(d, sdSegmentHUD(p, float2( w, -h + g), float2( w, -g)));
    if (m & 0x10u) d = min(d, sdSegmentHUD(p, float2( w,  g),     float2( w,  h - g)));
    if (m & 0x08u) d = min(d, sdSegmentHUD(p, float2(-w + g,  h), float2( w - g,  h)));
    if (m & 0x04u) d = min(d, sdSegmentHUD(p, float2(-w,  g),     float2(-w,  h - g)));
    if (m & 0x02u) d = min(d, sdSegmentHUD(p, float2(-w, -h + g), float2(-w, -g)));
    if (m & 0x01u) d = min(d, sdSegmentHUD(p, float2(-w + g, 0.0f), float2( w - g, 0.0f)));
    return smoothstep(thickness + 0.045f, thickness - 0.025f, d);
}

// Isotropic pixel-space micro-glyphs (uniform ~1.8px stroke on horizontal, vertical & diagonal bars!)
static inline float evalKmhLabel(float2 pPx, float2 boxSz) {
    if (pPx.x < -1.5f || pPx.x > boxSz.x + 1.5f || pPx.y < -1.5f || pPx.y > boxSz.y + 1.5f) return 0.0f;
    float d = 1e5f;
    float yTop = 1.6f, yMid = boxSz.y * 0.5f, yBot = boxSz.y - 1.6f;
    // K (x: 1.5 .. 9.5)
    d = min(d, sdSegmentHUD(pPx, float2(1.8f, yTop), float2(1.8f, yBot)));
    d = min(d, sdSegmentHUD(pPx, float2(9.2f, yTop), float2(2.4f, yMid)));
    d = min(d, sdSegmentHUD(pPx, float2(2.4f, yMid), float2(9.6f, yBot)));
    // M (x: 13.0 .. 23.8)
    d = min(d, sdSegmentHUD(pPx, float2(13.2f, yBot), float2(13.2f, yTop)));
    d = min(d, sdSegmentHUD(pPx, float2(13.2f, yTop), float2(18.5f, yMid + 1.0f)));
    d = min(d, sdSegmentHUD(pPx, float2(18.5f, yMid + 1.0f), float2(23.8f, yTop)));
    d = min(d, sdSegmentHUD(pPx, float2(23.8f, yTop), float2(23.8f, yBot)));
    // / (x: 27.2 .. 31.6)
    d = min(d, sdSegmentHUD(pPx, float2(31.6f, yTop - 0.3f), float2(27.2f, yBot + 0.3f)));
    // H (x: 34.8 .. 42.4) — full isotropic 1.8px horizontal crossbar!
    d = min(d, sdSegmentHUD(pPx, float2(34.8f, yTop), float2(34.8f, yBot)));
    d = min(d, sdSegmentHUD(pPx, float2(42.4f, yTop), float2(42.4f, yBot)));
    d = min(d, sdSegmentHUD(pPx, float2(34.8f, yMid), float2(42.4f, yMid)));
    return smoothstep(1.30f, 0.55f, d);
}

// Isotropic pixel-space "NOS" micro-glyph in 32x11 virtual 1080p box
static inline float evalNosLabel(float2 pPx, float2 boxSz) {
    if (pPx.x < -1.5f || pPx.x > boxSz.x + 1.5f || pPx.y < -1.5f || pPx.y > boxSz.y + 1.5f) return 0.0f;
    float d = 1e5f;
    float yTop = 1.4f, yMid = boxSz.y * 0.5f, yBot = boxSz.y - 1.4f;
    // N (x: 1.6 .. 8.8)
    d = min(d, sdSegmentHUD(pPx, float2(1.8f, yBot), float2(1.8f, yTop)));
    d = min(d, sdSegmentHUD(pPx, float2(1.8f, yTop), float2(8.6f, yBot)));
    d = min(d, sdSegmentHUD(pPx, float2(8.6f, yBot), float2(8.6f, yTop)));
    // O (x: 12.2 .. 19.4)
    d = min(d, sdSegmentHUD(pPx, float2(12.4f, yTop), float2(19.2f, yTop)));
    d = min(d, sdSegmentHUD(pPx, float2(19.2f, yTop), float2(19.2f, yBot)));
    d = min(d, sdSegmentHUD(pPx, float2(19.2f, yBot), float2(12.4f, yBot)));
    d = min(d, sdSegmentHUD(pPx, float2(12.4f, yBot), float2(12.4f, yTop)));
    // S (x: 22.8 .. 30.2)
    d = min(d, sdSegmentHUD(pPx, float2(30.0f, yTop), float2(23.0f, yTop)));
    d = min(d, sdSegmentHUD(pPx, float2(23.0f, yTop), float2(23.0f, yMid)));
    d = min(d, sdSegmentHUD(pPx, float2(23.0f, yMid), float2(30.0f, yMid)));
    d = min(d, sdSegmentHUD(pPx, float2(30.0f, yMid), float2(30.0f, yBot)));
    d = min(d, sdSegmentHUD(pPx, float2(30.0f, yBot), float2(23.0f, yBot)));
    return smoothstep(1.22f, 0.50f, d);
}

// Isotropic pixel-space "SB" (Speedbreaker) micro-glyph in 22x11 virtual 1080p box
static inline float evalSbLabel(float2 pPx, float2 boxSz) {
    if (pPx.x < -1.5f || pPx.x > boxSz.x + 1.5f || pPx.y < -1.5f || pPx.y > boxSz.y + 1.5f) return 0.0f;
    float d = 1e5f;
    float yTop = 1.4f, yMid = boxSz.y * 0.5f, yBot = boxSz.y - 1.4f;
    // S (x: 1.6 .. 8.8)
    d = min(d, sdSegmentHUD(pPx, float2(8.6f, yTop), float2(1.8f, yTop)));
    d = min(d, sdSegmentHUD(pPx, float2(1.8f, yTop), float2(1.8f, yMid)));
    d = min(d, sdSegmentHUD(pPx, float2(1.8f, yMid), float2(8.6f, yMid)));
    d = min(d, sdSegmentHUD(pPx, float2(8.6f, yMid), float2(8.6f, yBot)));
    d = min(d, sdSegmentHUD(pPx, float2(8.6f, yBot), float2(1.8f, yBot)));
    // B (x: 12.6 .. 20.0)
    d = min(d, sdSegmentHUD(pPx, float2(12.8f, yTop), float2(12.8f, yBot)));
    d = min(d, sdSegmentHUD(pPx, float2(12.8f, yTop), float2(19.0f, yTop)));
    d = min(d, sdSegmentHUD(pPx, float2(19.0f, yTop), float2(19.0f, yMid)));
    d = min(d, sdSegmentHUD(pPx, float2(12.8f, yMid), float2(19.8f, yMid)));
    d = min(d, sdSegmentHUD(pPx, float2(19.8f, yMid), float2(19.8f, yBot)));
    d = min(d, sdSegmentHUD(pPx, float2(19.8f, yBot), float2(12.8f, yBot)));
    return smoothstep(1.22f, 0.50f, d);
}

// Isotropic pixel-space "FG" badge glyph in 24x12 virtual 1080p box
static inline float evalFGLabel(float2 pPx, float2 boxSz) {
    if (pPx.x < -1.5f || pPx.x > boxSz.x + 1.5f || pPx.y < -1.5f || pPx.y > boxSz.y + 1.5f) return 0.0f;
    float d = 1e5f;
    float yTop = 1.6f, yMid = boxSz.y * 0.5f, yBot = boxSz.y - 1.6f;
    // F (x: 2.0 .. 9.6)
    d = min(d, sdSegmentHUD(pPx, float2(2.2f, yTop), float2(2.2f, yBot)));
    d = min(d, sdSegmentHUD(pPx, float2(2.2f, yTop), float2(9.6f, yTop)));
    d = min(d, sdSegmentHUD(pPx, float2(2.2f, yMid), float2(8.2f, yMid)));
    // G (x: 13.4 .. 21.8)
    d = min(d, sdSegmentHUD(pPx, float2(21.6f, yTop), float2(13.6f, yTop)));
    d = min(d, sdSegmentHUD(pPx, float2(13.6f, yTop), float2(13.6f, yBot)));
    d = min(d, sdSegmentHUD(pPx, float2(13.6f, yBot), float2(21.6f, yBot)));
    d = min(d, sdSegmentHUD(pPx, float2(21.6f, yBot), float2(21.6f, yMid + 0.4f)));
    d = min(d, sdSegmentHUD(pPx, float2(17.8f, yMid + 0.4f), float2(21.6f, yMid + 0.4f)));
    return smoothstep(1.22f, 0.50f, d);
}

// Micro "X" multiplier glyph in [-0.5..0.5, -0.5..0.5]
static inline float evalCrossGlyph(float2 p, float thickness) {
    float d = min(sdSegmentHUD(p, float2(-0.32f, -0.32f), float2(0.32f, 0.32f)),
                  sdSegmentHUD(p, float2(0.32f, -0.32f), float2(-0.32f, 0.32f)));
    return smoothstep(thickness + 0.05f, thickness - 0.02f, d);
}

// ---------------------------------------------------------------------------
// Widget 1: Bottom-Right NFSMW 2005 Curved Tachometer + NOS + Speedbreaker
// ---------------------------------------------------------------------------

static inline float3 compositeNFSMWTachCluster(
    float3 baseCol,
    float2 px,
    float2 res,
    constant RTFrameUniforms& u,
    constant RTHudExtUniforms& ext
) {
    float scale = res.y / 1080.0f;
    // Dial center positioned cleanly in bottom-right corner with room for curved NOS/SB side wings
    float2 center = float2(res.x - 162.0f * scale, res.y - 148.0f * scale);
    float2 rel = (px - center) / scale; // 1080p virtual coords, +y down
    float r = length(rel);

    // Tight early-out covering dial + curved NOS/SB wings + full unclipped NOS/SB labels (max r <= 156px)
    if (r > 168.0f) return baseCol;

    bool hasExt = ((ext.hudFlags & HUD_FLAG_VISIBLE) != 0u) || (ext.rpmNorm > 0.001f) || (ext.speedKmhOverride > 0.01f);
    float rpmNorm = clamp((ext.rpmNorm > 0.001f) ? ext.rpmNorm : u.hudParams.y, 0.0f, 1.0f);
    float nosNorm = clamp(hasExt ? ext.nosNorm : 1.0f, 0.0f, 1.0f);
    float sbNorm  = clamp(hasExt ? ext.sbNorm  : 1.0f, 0.0f, 1.0f);
    float speedVal = (ext.speedKmhOverride > 0.01f) ? ext.speedKmhOverride : u.driveParams.x;
    bool nosActive = (ext.hudFlags & HUD_FLAG_NOS_ACTIVE) != 0u;
    bool sbActive  = (ext.hudFlags & HUD_FLAG_SB_ACTIVE) != 0u;
    float simT = ext.simTimeSec;

    // Angle in degrees where 0 = right (+X), +90 = down (+Y), -90 = up (-Y), +-180 = left (-X)
    float angDeg = atan2(rel.y, rel.x) * 57.2957795f;

    float3 outCol = baseCol;

    // 1. Main Circular Tachometer Dial (radius 106px)
    const float dialR = 106.0f;
    float dDial = r - dialR;
    if (dDial < 12.0f) {
        float shadow = smoothstep(12.0f, 0.0f, dDial) * 0.34f * step(0.0f, dDial);
        outCol *= (1.0f - shadow);

        float dialMask = smoothstep(1.2f, -0.8f, dDial);
        if (dialMask > 0.001f) {
            // Smoked carbon-glass backing with subtle radial depth gradient
            float radGrad = smoothstep(106.0f, 12.0f, r);
            float3 dialBg = mix(baseCol * 0.24f, float3(0.028f, 0.032f, 0.040f), 0.78f);
            dialBg += float3(0.025f, 0.028f, 0.036f) * radGrad;
            // Subtle warm drift glow around rim
            dialBg += float3(0.28f, 0.14f, 0.02f) * clamp(ext.driftIntensity, 0.0f, 1.0f) * smoothstep(60.0f, 104.0f, r) * 0.45f;

            // Metallic brushed gunmetal rim
            float rimMask = smoothstep(2.4f, 0.5f, abs(dDial + 1.2f));
            float rimHighlight = 0.55f + 0.45f * clamp(-rel.y / dialR, -1.0f, 1.0f);
            float3 rimCol = mix(float3(0.36f, 0.40f, 0.46f), float3(0.76f, 0.80f, 0.86f), rimHighlight);
            if (nosActive) rimCol = mix(rimCol, float3(0.10f, 0.85f, 1.00f), 0.55f);
            else if (sbActive) rimCol = mix(rimCol, float3(1.00f, 0.70f, 0.14f), 0.55f);
            dialBg = mix(dialBg, rimCol, rimMask * 0.75f);

            // Sweeping 240-deg NFSMW RPM Arc: starts at +150 deg (bottom-left), sweeps clockwise through -90 (top) to +30 deg (bottom-right)
            // Remap angle into [0..360) starting at +150 deg clockwise:
            float sweepDeg = fmod(angDeg - 150.0f + 720.0f, 360.0f);
            const float arcSpan = 240.0f;
            if (sweepDeg <= arcSpan && r >= 72.0f && r <= 99.0f) {
                float uArc = sweepDeg / arcSpan; // 0..1 along tach arc
                float segF = uArc * 28.0f;
                int segIdx = int(floor(segF));
                float segLocal = fract(segF);
                float inTick = smoothstep(0.09f, 0.16f, segLocal) * smoothstep(0.91f, 0.84f, segLocal);
                float radialMask = smoothstep(73.0f, 75.0f, r) * smoothstep(98.0f, 95.5f, r);
                // Major tick notch every 4 segments
                float isMajor = (segIdx % 4 == 0) ? 1.0f : 0.0f;
                float tickInner = mix(79.0f, 74.5f, isMajor);
                float tickShape = smoothstep(tickInner - 1.0f, tickInner + 1.0f, r) * smoothstep(97.5f, 95.5f, r);

                float segThresh = (float(segIdx) + 0.45f) / 28.0f;
                float lit = step(segThresh, rpmNorm);

                // NFSMW color ramp: crisp silver-white -> Rockport gold-amber -> pulsing crimson redline
                float3 activeCol = float3(0.95f, 0.97f, 1.00f);
                if (uArc >= 0.70f && uArc < 0.85f) {
                    activeCol = float3(1.00f, 0.64f, 0.08f);
                } else if (uArc >= 0.85f) {
                    float pulse = 0.82f + 0.18f * sin(simT * 24.0f);
                    activeCol = float3(1.00f, 0.14f, 0.08f) * pulse;
                }
                float3 unlitCol = (uArc >= 0.85f) ? float3(0.24f, 0.07f, 0.07f) : float3(0.13f, 0.15f, 0.19f);
                dialBg = mix(dialBg, mix(unlitCol, activeCol, lit), inTick * tickShape * 0.94f);

                // Soft glowing inner arc trace under lit RPM zone
                float innerBand = smoothstep(73.0f, 75.5f, r) * smoothstep(79.5f, 77.0f, r) * smoothstep(rpmNorm + 0.015f, rpmNorm - 0.015f, uArc);
                dialBg += activeCol * innerBand * 0.32f * radialMask;
            }

            // Crisp glowing RPM Needle Tip
            float needleRad = (150.0f + rpmNorm * arcSpan) * 0.0174532925f;
            float2 nDir = float2(cos(needleRad), sin(needleRad));
            float dNeedle = sdSegmentHUD(rel, nDir * 56.0f, nDir * 98.0f);
            float needleMask = smoothstep(2.6f, 0.8f, dNeedle);
            float3 needleCol = (rpmNorm > 0.86f) ? float3(1.00f, 0.20f, 0.12f) : float3(1.00f, 0.72f, 0.16f);
            dialBg = mix(dialBg, needleCol, needleMask * 0.96f);

            // Digital 3-Digit Speedometer centered in dial (y = -8)
            int spd = clamp(int(speedVal + 0.5f), 0, 999);
            int digs[3] = { (spd / 100) % 10, (spd / 10) % 10, spd % 10 };
            bool showD[3] = { spd >= 100, spd >= 10, true };
            float digX[3] = { -35.0f, 0.0f, 35.0f };
            for (int k = 0; k < 3; ++k) {
                float2 dp = (rel - float2(digX[k], -12.0f)) / float2(27.0f, 24.0f);
                if (abs(dp.x) < 0.72f && abs(dp.y) < 1.2f) {
                    float ghost = evalSevenSegDigit(dp, 8, 0.105f);
                    dialBg = mix(dialBg, float3(0.06f, 0.07f, 0.085f), ghost * 0.30f);
                    if (showD[k]) {
                        float on = evalSevenSegDigit(dp, digs[k], 0.105f);
                        dialBg = mix(dialBg, float3(0.98f, 0.99f, 1.00f), on);
                    }
                }
            }

            // Crisp "KM/H" unit label right below speed (isotropic 44x13 virtual 1080p px)
            float2 kmhSz = float2(44.0f, 13.0f);
            float2 kmhP = rel - float2(-22.0f, 15.0f);
            dialBg = mix(dialBg, float3(0.74f, 0.80f, 0.88f), evalKmhLabel(kmhP, kmhSz) * 0.92f);

            // Bold NFSMW Gear Indicator Pill at bottom-center of dial (rel = (0, 56))
            float2 gRel = rel - float2(0.0f, 56.0f);
            float dGear = sdRoundedBoxHUD(gRel, float2(21.0f, 21.0f), 6.0f);
            float gBg = smoothstep(1.0f, -0.8f, dGear);
            float gBorder = smoothstep(1.5f, 0.3f, abs(dGear));
            float3 gAccent = (rpmNorm > 0.88f) ? float3(1.00f, 0.20f, 0.12f) : float3(1.00f, 0.66f, 0.12f);
            dialBg = mix(dialBg, float3(0.07f, 0.06f, 0.045f), gBg * 0.90f);
            dialBg = mix(dialBg, gAccent, gBorder * 0.92f);

            int gearInt = int(round(u.hudParams.z));
            int gearGlyph = (gearInt < 0) ? 11 : ((gearInt == 0) ? 10 : clamp(gearInt, 1, 6));
            float gLit = evalSevenSegDigit(gRel / float2(17.0f, 15.5f), gearGlyph, 0.125f);
            dialBg = mix(dialBg, gAccent, gLit * gBg);

            outCol = mix(outCol, dialBg, dialMask);
        }
    }

    // 2. Curved Outer-Left Cyan NOS Gauge (radius 114..128, angle from +155 deg clockwise up to -125 deg -> 80 deg span)
    if (r >= 112.0f && r <= 132.0f) {
        // Left side wing: angle mapped around 180 deg (+152 -> +228 deg in [0..360))
        float a360 = (angDeg < 0.0f) ? (angDeg + 360.0f) : angDeg;
        if (a360 >= 146.0f && a360 <= 234.0f) {
            float uNos = clamp((a360 - 150.0f) / 80.0f, 0.0f, 1.0f);
            float barMask = smoothstep(113.5f, 115.2f, r) * smoothstep(129.5f, 127.8f, r)
                          * smoothstep(146.5f, 150.0f, a360) * smoothstep(233.5f, 230.0f, a360);
            float fillMask = smoothstep(nosNorm + 0.015f, nosNorm - 0.015f, uNos);
            float pulse = nosActive ? (0.85f + 0.15f * sin(simT * 28.0f)) : 1.0f;
            float3 nosBg = float3(0.03f, 0.06f, 0.10f);
            float3 nosCol = mix(float3(0.02f, 0.62f, 0.96f), float3(0.32f, 0.94f, 1.00f), uNos) * pulse;
            if (nosActive) nosCol *= 1.25f;
            outCol = mix(outCol, mix(nosBg, nosCol, fillMask), barMask * 0.92f);
        }

        // 3. Curved Outer-Right Amber Speedbreaker Gauge (angle from +34 deg counter-clockwise up to -46 deg -> 80 deg span)
        if (angDeg <= 34.0f && angDeg >= -54.0f) {
            float uSb = clamp((30.0f - angDeg) / 80.0f, 0.0f, 1.0f);
            float barMask = smoothstep(113.5f, 115.2f, r) * smoothstep(129.5f, 127.8f, r)
                          * smoothstep(33.5f, 30.0f, angDeg) * smoothstep(-53.5f, -50.0f, angDeg);
            float fillMask = smoothstep(sbNorm + 0.015f, sbNorm - 0.015f, uSb);
            float pulse = sbActive ? (0.85f + 0.15f * sin(simT * 20.0f)) : 1.0f;
            float3 sbBg = float3(0.09f, 0.06f, 0.02f);
            float3 sbCol = mix(float3(0.92f, 0.48f, 0.04f), float3(1.00f, 0.84f, 0.24f), uSb) * pulse;
            if (sbActive) sbCol *= 1.25f;
            outCol = mix(outCol, mix(sbBg, sbCol, fillMask), barMask * 0.92f);
        }
    }

    // Crisp unclipped "NOS" & "SB" micro-labels below left/right gauge wings (both inside r <= 156px < 168px)
    float2 nosSz = float2(32.0f, 11.0f);
    float2 nosLblP = rel - float2(-126.0f, 72.0f);
    float nosLbl = evalNosLabel(nosLblP, nosSz);
    if (nosLbl > 0.01f) {
        outCol = mix(outCol, float3(0.32f, 0.90f, 1.00f), nosLbl * 0.95f);
    }

    float2 sbSz = float2(22.0f, 11.0f);
    float2 sbLblP = rel - float2(102.0f, 72.0f);
    float sbLbl = evalSbLabel(sbLblP, sbSz);
    if (sbLbl > 0.01f) {
        outCol = mix(outCol, float3(1.00f, 0.76f, 0.20f), sbLbl * 0.95f);
    }

    return outCol;
}

// ---------------------------------------------------------------------------
// Widget 2: Bottom-Left Rotating Heading-Up Circular GPU Minimap
// ---------------------------------------------------------------------------

static inline float3 compositeGPUMinimap(
    float3 baseCol,
    float2 px,
    float2 res,
    constant RTHudExtUniforms& ext,
    constant RTMinimapBuffer& mm
) {
    if ((ext.hudFlags & HUD_FLAG_HIDE_MINIMAP) != 0u) return baseCol;

    float scale = res.y / 1080.0f;
    float2 center = float2(138.0f * scale, res.y - 138.0f * scale);
    float2 rel = (px - center) / scale; // virtual 1080p px, +y down
    const float mapR = 96.0f;
    float r = length(rel);

    // Tight early-out: skips ~95% of screen pixels!
    if (r > mapR + 14.0f) return baseCol;

    float dCircle = r - mapR;
    float shadow = smoothstep(14.0f, 0.0f, dCircle) * 0.34f * step(0.0f, dCircle);
    float3 outCol = baseCol * (1.0f - shadow);

    float circleMask = smoothstep(1.2f, -0.8f, dCircle);
    if (circleMask <= 0.001f) return outCol;

    // Smoked radar translucent glass backing + subtle concentric range rings
    float3 mapCol = mix(baseCol * 0.22f, float3(0.025f, 0.032f, 0.042f), 0.78f);
    float ring50 = smoothstep(1.1f, 0.2f, abs(r - mapR * 0.50f));
    float crossHair = (smoothstep(0.9f, 0.1f, abs(rel.x)) + smoothstep(0.9f, 0.1f, abs(rel.y))) * 0.08f;
    mapCol += float3(0.18f, 0.22f, 0.28f) * (ring50 * 0.24f + crossHair);

    // Normalized heading-up radar coordinates: +X = right, +Y = forward (UP on screen = -rel.y!)
    float2 mPos = float2(rel.x, -rel.y) / (mapR - 6.0f);
    float mLen = length(mPos);

    if (mLen <= 1.02f) {
        // 1. Crisp Road Ribbons from RTMinimapBuffer (up to 96 segments)
        uint segN = min(mm.segCount, 96u);
        if (segN > 0u) {
            float minRoadD = 10.0f;
            for (uint i = 0u; i < segN; ++i) {
                float4 seg = mm.roadSegments[i];
                float d = sdSegmentHUD(mPos, seg.xy, seg.zw);
                minRoadD = min(minRoadD, d);
            }
            float edgeMask = smoothstep(0.084f, 0.068f, minRoadD);
            float coreMask = smoothstep(0.056f, 0.040f, minRoadD);
            mapCol = mix(mapCol, float3(0.56f, 0.62f, 0.70f), edgeMask * 0.70f);
            mapCol = mix(mapCol, float3(0.16f, 0.19f, 0.24f), coreMask * 0.92f);
        }

        // 2. Traffic & Flashing Police Blips (up to 24 blips)
        uint blipN = min(mm.blipCount, 24u);
        for (uint i = 0u; i < blipN; ++i) {
            float4 b = mm.blips[i];
            float2 dPos = mPos - b.xy;
            float dist = length(dPos);
            if (dist > 0.18f) continue;

            int bType = int(b.z + 0.5f);
            if (bType == 0) {
                // Civilian traffic dot (warm off-white)
                float dotM = smoothstep(0.044f, 0.026f, dist);
                mapCol = mix(mapCol, float3(0.86f, 0.88f, 0.82f), dotM * 0.86f);
            } else if (bType == 1) {
                // Patrolling cop (cool police blue dot + subtle radar ring)
                float halo = smoothstep(0.11f, 0.04f, dist) * 0.22f;
                float dotM = smoothstep(0.052f, 0.032f, dist);
                mapCol += float3(0.12f, 0.45f, 1.00f) * halo;
                mapCol = mix(mapCol, float3(0.24f, 0.62f, 1.00f), dotM * 0.95f);
            } else if (bType == 2) {
                // Pursuing cop: flashing alternating RED / BLUE chevron + strobe halo!
                float strobe = step(0.0f, sin(ext.simTimeSec * 18.0f + b.w));
                float3 copCol = mix(float3(1.00f, 0.12f, 0.10f), float3(0.12f, 0.52f, 1.00f), strobe);
                float halo = smoothstep(0.14f, 0.03f, dist) * 0.42f;
                float triD = sdTriangleHUD(dPos, float2(0.0f, 0.068f), float2(-0.052f, -0.048f), float2(0.052f, -0.048f));
                float triM = smoothstep(0.015f, -0.008f, triD);
                mapCol += copCol * halo;
                mapCol = mix(mapCol, copCol, triM);
            } else {
                // Landmark / Objective gold diamond
                float diam = abs(dPos.x) + abs(dPos.y);
                float dM = smoothstep(0.068f, 0.046f, diam);
                mapCol = mix(mapCol, float3(1.00f, 0.78f, 0.14f), dM);
            }
        }

        // 3. Center Player Directional Chevron (always crisp, pointing up = forward)
        float pTri = sdTriangleHUD(mPos, float2(0.0f, 0.105f), float2(-0.072f, -0.075f), float2(0.072f, -0.075f));
        float pNotch = sdTriangleHUD(mPos, float2(0.0f, -0.025f), float2(-0.050f, -0.085f), float2(0.050f, -0.085f));
        float pArrow = max(pTri, -pNotch);
        float pOutline = smoothstep(0.026f, 0.006f, pArrow);
        float pFill = smoothstep(0.008f, -0.008f, pArrow);
        mapCol = mix(mapCol, float3(0.02f, 0.02f, 0.03f), pOutline * 0.85f);
        mapCol = mix(mapCol, float3(1.00f, 0.74f, 0.16f), pFill);
    }

    // 4. Outer Compass Bezel & Rotating North Marker
    float bezel = smoothstep(2.6f, 0.5f, abs(dCircle + 1.8f));
    bool pursuit = (ext.heatLevel > 0.5f) || ((ext.hudFlags & HUD_FLAG_PURSUIT_ACTIVE) != 0u);
    float3 bezelCol = float3(0.46f, 0.50f, 0.56f);
    if (pursuit) {
        float flash = step(0.0f, sin(ext.simTimeSec * 14.0f));
        bezelCol = mix(float3(1.00f, 0.16f, 0.12f), float3(0.16f, 0.54f, 1.00f), flash);
    }
    mapCol = mix(mapCol, bezelCol, bezel * 0.85f);

    // North compass jewel rotating with -playerYawRad (+Y world North -> screen up when yaw == 0)
    float2 northPt = float2(-sin(mm.playerYawRad), -cos(mm.playerYawRad)) * (mapR - 3.5f);
    float dNorth = length(rel - northPt);
    float northDot = smoothstep(5.2f, 3.2f, dNorth);
    mapCol = mix(mapCol, float3(1.00f, 0.68f, 0.12f), northDot);

    return mix(outCol, mapCol, circleMask);
}

// ---------------------------------------------------------------------------
// Widget 3: Top-Center NFSMW Heat x1..x5 / Bounty / Evade-Busted Pursuit Bar
// ---------------------------------------------------------------------------

static inline float3 compositePursuitBar(
    float3 baseCol,
    float2 px,
    float2 res,
    constant RTHudExtUniforms& ext
) {
    bool active = (ext.heatLevel > 0.5f) || ((ext.hudFlags & HUD_FLAG_PURSUIT_ACTIVE) != 0u);
    if (!active) return baseCol;

    float scale = res.y / 1080.0f;
    float2 center = float2(res.x * 0.5f, 44.0f * scale);
    float2 rel = (px - center) / scale; // virtual 1080p coords
    float dBox = sdRoundedBoxHUD(rel, float2(236.0f, 26.0f), 9.0f);
    if (dBox > 10.0f) return baseCol;

    float shadow = smoothstep(10.0f, 0.0f, dBox) * 0.30f * step(0.0f, dBox);
    float3 outCol = baseCol * (1.0f - shadow);
    float mask = smoothstep(1.2f, -0.8f, dBox);
    if (mask <= 0.001f) return outCol;

    float3 bg = mix(baseCol * 0.24f, float3(0.030f, 0.034f, 0.044f), 0.80f);
    float border = smoothstep(1.6f, 0.3f, abs(dBox));
    float strobe = step(0.0f, sin(ext.simTimeSec * 16.0f));
    float3 sirenAccent = mix(float3(1.00f, 0.14f, 0.10f), float3(0.14f, 0.56f, 1.00f), strobe);
    bg = mix(bg, mix(float3(0.45f, 0.48f, 0.55f), sirenAccent, 0.65f), border * 0.78f);

    // Top 3px red/blue strobe bar split left=red, right=blue alternating
    if (rel.y < -21.0f) {
        float side = step(0.0f, rel.x);
        float3 topCol = mix(float3(1.00f, 0.14f, 0.10f), float3(0.14f, 0.56f, 1.00f), abs(side - strobe));
        bg = mix(bg, topCol, 0.92f);
    }

    // Left: Heat x1..x5 Badge (rel.x in [-220..-158])
    float2 hRel = rel - float2(-190.0f, 1.0f);
    float dHeatPill = sdRoundedBoxHUD(hRel, float2(28.0f, 16.0f), 5.0f);
    float heatLevelF = clamp(ext.heatLevel, 1.0f, 5.0f);
    float3 heatCol = mix(float3(1.00f, 0.66f, 0.10f), float3(1.00f, 0.14f, 0.08f), (heatLevelF - 1.0f) * 0.25f);
    bg = mix(bg, heatCol * 0.22f, smoothstep(1.0f, -0.8f, dHeatPill) * 0.92f);
    bg = mix(bg, heatCol, smoothstep(1.3f, 0.2f, abs(dHeatPill)) * 0.90f);

    float xGlyph = evalCrossGlyph((hRel - float2(-10.0f, 1.0f)) / 15.0f, 0.10f);
    float hDig = evalSevenSegDigit((hRel - float2(9.0f, 0.0f)) / float2(13.0f, 12.0f), int(round(heatLevelF)), 0.13f);
    bg = mix(bg, heatCol, clamp(xGlyph + hDig, 0.0f, 1.0f));

    // Center: Evade (blue left) <-> Busted (crimson right) Tug-of-War Bar (rel.x in [-142..+62], rel.y in [-8..+8])
    if (rel.x >= -142.0f && rel.x <= 62.0f && rel.y >= -10.0f && rel.y <= 14.0f) {
        float uBar = (rel.x + 142.0f) / 204.0f; // 0 = full evade, 0.5 = neutral, 1.0 = full busted
        float inBar = step(-6.0f, rel.y) * step(rel.y, 4.0f);
        float3 barCol = float3(0.12f, 0.14f, 0.18f);
        float evadeSpan = clamp(ext.evadeProgress, 0.0f, 1.0f) * 0.5f;
        float bustSpan  = clamp(ext.bustedProgress, 0.0f, 1.0f) * 0.5f;
        if (uBar < 0.5f && uBar >= (0.5f - evadeSpan)) {
            barCol = float3(0.14f, 0.76f, 1.00f);
        } else if (uBar >= 0.5f && uBar <= (0.5f + bustSpan)) {
            barCol = float3(1.00f, 0.18f, 0.12f);
        }
        // Center neutral divider tick
        float midTick = smoothstep(1.5f, 0.3f, abs(rel.x + 40.0f));
        barCol = mix(barCol, float3(0.95f, 0.96f, 0.98f), midTick);
        bg = mix(bg, barCol, inBar * 0.94f);

        // Active cop count pips directly beneath bar (up to 8 pips)
        if (rel.y >= 7.0f && rel.y <= 13.0f) {
            uint cops = min(ext.copCount, 8u);
            for (uint c = 0u; c < 8u; ++c) {
                float cx = -130.0f + float(c) * 16.0f;
                float dPip = length(rel - float2(cx, 10.5f));
                float pipM = smoothstep(3.4f, 2.0f, dPip);
                if (pipM > 0.01f) {
                    float3 pCol = (c < cops) ? sirenAccent : float3(0.18f, 0.20f, 0.25f);
                    bg = mix(bg, pCol, pipM);
                }
            }
        }
    }

    // Right: Compact Bounty Readout (up to 6 digits in rel.x in [+82..+220])
    int bVal = clamp(int(ext.bounty + 0.5f), 0, 999999);
    int bDivs[6] = { 100000, 10000, 1000, 100, 10, 1 };
    for (int k = 0; k < 6; ++k) {
        int dig = (bVal / bDivs[k]) % 10;
        bool show = (bVal >= bDivs[k]) || (k == 5);
        if (!show) continue;
        float dx = 92.0f + float(k) * 21.0f;
        float2 dp = (rel - float2(dx, 1.0f)) / float2(14.5f, 13.0f);
        if (abs(dp.x) < 0.72f && abs(dp.y) < 1.2f) {
            float on = evalSevenSegDigit(dp, dig, 0.125f);
            bg = mix(bg, float3(1.00f, 0.84f, 0.30f), on);
        }
    }

    return mix(outCol, bg, mask);
}

// ---------------------------------------------------------------------------
// Widget 4: Subtle Top-Right FPS + Frame-Gen Badge
// ---------------------------------------------------------------------------

static inline float3 compositeTopRightTelemetry(
    float3 baseCol,
    float2 px,
    float2 res,
    constant RTHudExtUniforms& ext
) {
    bool showFG = (ext.hudFlags & HUD_FLAG_FRAMEGEN_ON) != 0u;
    if (ext.fps < 0.5f && !showFG) return baseCol;

    float scale = res.y / 1080.0f;
    float2 center = float2(res.x - 86.0f * scale, 34.0f * scale);
    float2 rel = (px - center) / scale;
    float dBox = sdRoundedBoxHUD(rel, float2(58.0f, 16.0f), 6.0f);
    if (dBox > 6.0f) return baseCol;

    float mask = smoothstep(1.0f, -0.8f, dBox);
    if (mask <= 0.001f) return baseCol;

    float3 bg = mix(baseCol * 0.30f, float3(0.032f, 0.036f, 0.044f), 0.74f);
    float border = smoothstep(1.4f, 0.3f, abs(dBox));
    bg = mix(bg, float3(0.40f, 0.44f, 0.50f), border * 0.55f);

    if (ext.fps >= 0.5f) {
        int fpsI = clamp(int(ext.fps + 0.5f), 0, 999);
        int digs[3] = { (fpsI / 100) % 10, (fpsI / 10) % 10, fpsI % 10 };
        bool showD[3] = { fpsI >= 100, fpsI >= 10, true };
        for (int k = 0; k < 3; ++k) {
            if (!showD[k]) continue;
            float2 dp = (rel - float2(-40.0f + float(k) * 16.0f, 0.0f)) / float2(11.5f, 10.5f);
            if (abs(dp.x) < 0.72f && abs(dp.y) < 1.2f) {
                bg = mix(bg, float3(0.92f, 0.96f, 1.00f), evalSevenSegDigit(dp, digs[k], 0.13f));
            }
        }
    }

    if (showFG) {
        float2 fgRel = rel - float2(30.0f, 0.0f);
        float dPill = sdRoundedBoxHUD(fgRel, float2(18.0f, 10.0f), 4.0f);
        float pMask = smoothstep(1.0f, -0.6f, dPill);
        bg = mix(bg, float3(0.04f, 0.28f, 0.38f), pMask * 0.90f);
        float fgGlyph = evalFGLabel(fgRel + float2(12.0f, 6.0f), float2(24.0f, 12.0f));
        bg = mix(bg, float3(0.28f, 0.94f, 1.00f), fgGlyph * pMask);
    }

    return mix(baseCol, bg, mask);
}

// ---------------------------------------------------------------------------
// Widget 5: Top-Left Authentic NFSMW 2005 Sliding EA TRAX Banner (5x7 Font)
// Covers ASCII 0x20 (' ') .. 0x5F ('_') in 448 bytes of constant ROM (O(1) lookup).
// ---------------------------------------------------------------------------

static constant uchar kFont5x7[448] = {
    // 0x20 ' ' .. 0x27 '\''
    0x00,0x00,0x00,0x00,0x00,0x00,0x00, // ' '
    0x04,0x04,0x04,0x04,0x04,0x00,0x04, // '!'
    0x0A,0x0A,0x00,0x00,0x00,0x00,0x00, // '"'
    0x0A,0x1F,0x0A,0x0A,0x1F,0x0A,0x00, // '#'
    0x04,0x0F,0x14,0x0E,0x05,0x1E,0x04, // '$'
    0x19,0x19,0x02,0x04,0x08,0x13,0x13, // '%'
    0x0C,0x12,0x14,0x08,0x15,0x12,0x0D, // '&'
    0x04,0x04,0x08,0x00,0x00,0x00,0x00, // '\''
    // 0x28 '(' .. 0x2F '/'
    0x02,0x04,0x08,0x08,0x08,0x04,0x02, // '('
    0x08,0x04,0x02,0x02,0x02,0x04,0x08, // ')'
    0x00,0x0A,0x04,0x1F,0x04,0x0A,0x00, // '*'
    0x00,0x04,0x04,0x1F,0x04,0x04,0x00, // '+'
    0x00,0x00,0x00,0x00,0x00,0x04,0x08, // ','
    0x00,0x00,0x00,0x1F,0x00,0x00,0x00, // '-'
    0x00,0x00,0x00,0x00,0x00,0x0C,0x0C, // '.'
    0x01,0x02,0x02,0x04,0x08,0x08,0x10, // '/'
    // 0x30 '0' .. 0x37 '7'
    0x0E,0x11,0x13,0x15,0x19,0x11,0x0E, // '0'
    0x04,0x0C,0x04,0x04,0x04,0x04,0x0E, // '1'
    0x0E,0x11,0x01,0x06,0x08,0x10,0x1F, // '2'
    0x1E,0x01,0x01,0x0E,0x01,0x01,0x1E, // '3'
    0x02,0x06,0x0A,0x12,0x1F,0x02,0x02, // '4'
    0x1F,0x10,0x1E,0x01,0x01,0x11,0x0E, // '5'
    0x0E,0x10,0x10,0x1E,0x11,0x11,0x0E, // '6'
    0x1F,0x01,0x02,0x04,0x08,0x08,0x08, // '7'
    // 0x38 '8' .. 0x3F '?'
    0x0E,0x11,0x11,0x0E,0x11,0x11,0x0E, // '8'
    0x0E,0x11,0x11,0x0F,0x01,0x01,0x0E, // '9'
    0x00,0x0C,0x0C,0x00,0x0C,0x0C,0x00, // ':'
    0x00,0x04,0x04,0x00,0x04,0x04,0x08, // ';'
    0x02,0x04,0x08,0x10,0x08,0x04,0x02, // '<'
    0x00,0x00,0x1F,0x00,0x1F,0x00,0x00, // '='
    0x08,0x04,0x02,0x01,0x02,0x04,0x08, // '>'
    0x0E,0x11,0x01,0x06,0x04,0x00,0x04, // '?'
    // 0x40 '@' .. 0x47 'G'
    0x0E,0x11,0x17,0x15,0x17,0x10,0x0E, // '@'
    0x0E,0x11,0x11,0x1F,0x11,0x11,0x11, // 'A'
    0x1E,0x11,0x11,0x1E,0x11,0x11,0x1E, // 'B'
    0x0E,0x11,0x10,0x10,0x10,0x11,0x0E, // 'C'
    0x1E,0x11,0x11,0x11,0x11,0x11,0x1E, // 'D'
    0x1F,0x10,0x10,0x1E,0x10,0x10,0x1F, // 'E'
    0x1F,0x10,0x10,0x1E,0x10,0x10,0x10, // 'F'
    0x0E,0x11,0x10,0x17,0x11,0x11,0x0F, // 'G'
    // 0x48 'H' .. 0x4F 'O'
    0x11,0x11,0x11,0x1F,0x11,0x11,0x11, // 'H'
    0x0E,0x04,0x04,0x04,0x04,0x04,0x0E, // 'I'
    0x07,0x02,0x02,0x02,0x12,0x12,0x0C, // 'J'
    0x11,0x12,0x14,0x18,0x14,0x12,0x11, // 'K'
    0x10,0x10,0x10,0x10,0x10,0x10,0x1F, // 'L'
    0x11,0x1B,0x15,0x15,0x11,0x11,0x11, // 'M'
    0x11,0x19,0x15,0x13,0x11,0x11,0x11, // 'N'
    0x0E,0x11,0x11,0x11,0x11,0x11,0x0E, // 'O'
    // 0x50 'P' .. 0x57 'W'
    0x1E,0x11,0x11,0x1E,0x10,0x10,0x10, // 'P'
    0x0E,0x11,0x11,0x11,0x15,0x12,0x0D, // 'Q'
    0x1E,0x11,0x11,0x1E,0x14,0x12,0x11, // 'R'
    0x0F,0x10,0x10,0x0E,0x01,0x01,0x1E, // 'S'
    0x1F,0x04,0x04,0x04,0x04,0x04,0x04, // 'T'
    0x11,0x11,0x11,0x11,0x11,0x11,0x0E, // 'U'
    0x11,0x11,0x11,0x11,0x11,0x0A,0x04, // 'V'
    0x11,0x11,0x11,0x15,0x15,0x1B,0x11, // 'W'
    // 0x58 'X' .. 0x5F '_'
    0x11,0x11,0x0A,0x04,0x0A,0x11,0x11, // 'X'
    0x11,0x11,0x0A,0x04,0x04,0x04,0x04, // 'Y'
    0x1F,0x01,0x02,0x04,0x08,0x10,0x1F, // 'Z'
    0x0E,0x08,0x08,0x08,0x08,0x08,0x0E, // '['
    0x10,0x08,0x08,0x04,0x02,0x02,0x01, // '\\'
    0x0E,0x02,0x02,0x02,0x02,0x02,0x0E, // ']'
    0x04,0x0A,0x11,0x00,0x00,0x00,0x00, // '^'
    0x00,0x00,0x00,0x00,0x00,0x00,0x1F  // '_'
};

static inline float sampleGlyph5x7(float2 localPx, uint asciiCode, float cellPx) {
    if (asciiCode <= 0x20u || asciiCode > 0x5Fu) return 0.0f;
    float2 g = localPx / cellPx;
    if (g.x < 0.0f || g.x >= 5.0f || g.y < 0.0f || g.y >= 7.0f) return 0.0f;
    uint col = uint(g.x);
    uint row = uint(g.y);
    uint idx = asciiCode - 0x20u;
    uint bits = uint(kFont5x7[idx * 7u + row]);
    if (((bits >> (4u - col)) & 1u) == 0u) return 0.0f;
    // Crisp OLED subpixel smoothing inside each lit texel
    float2 f = fract(g) - 0.5f;
    float edge = max(abs(f.x), abs(f.y));
    return smoothstep(0.56f, 0.34f, edge);
}

static inline uint unpackPackedChar(constant uint* words, uint charIdx) {
    return (words[charIdx >> 2u] >> ((charIdx & 3u) * 8u)) & 0xFFu;
}

static inline float evalPackedString5x7(
    float2 relPx,
    float2 originPx,
    float cellPx,
    float charStridePx,
    constant uint* words,
    uint len,
    uint maxChars
) {
    uint n = min(len, maxChars);
    if (n == 0u) return 0.0f;
    float2 p = relPx - originPx;
    if (p.y < 0.0f || p.y >= 7.0f * cellPx || p.x < 0.0f || p.x >= float(n) * charStridePx) {
        return 0.0f;
    }
    uint ci = uint(p.x / charStridePx);
    if (ci >= n) return 0.0f;
    float2 localP = float2(p.x - float(ci) * charStridePx, p.y);
    uint ch = unpackPackedChar(words, ci);
    return sampleGlyph5x7(localP, ch, cellPx);
}

static inline float evalLiteral7(float2 relPx, float2 originPx, float cellPx, float stridePx, thread const uint* codes, uint len) {
    float2 p = relPx - originPx;
    if (p.y < 0.0f || p.y >= 7.0f * cellPx || p.x < 0.0f || p.x >= float(len) * stridePx) {
        return 0.0f;
    }
    uint ci = uint(p.x / stridePx);
    if (ci >= len) return 0.0f;
    return sampleGlyph5x7(float2(p.x - float(ci) * stridePx, p.y), codes[ci], cellPx);
}

static inline float3 compositeEATraxBanner(
    float3 baseCol,
    float2 px,
    float2 res,
    constant RTHudExtUniforms& ext,
    constant RTMusicBannerUniforms& music
) {
    float alpha = clamp(music.bannerAlpha, 0.0f, 1.0f);
    if (alpha <= 0.001f || (music.flags & MUSIC_FLAG_VISIBLE) == 0u) return baseCol;

    float scale = res.y / 1080.0f;
    float t = clamp(music.slideT, 0.0f, 1.0f);
    float easeOut = 1.0f - (1.0f - t) * (1.0f - t) * (1.0f - t);

    const float boxW = 378.0f;
    const float boxH = 74.0f;
    float2 origin = float2(28.0f - (1.0f - easeOut) * 420.0f, 26.0f);
    float2 rel = (px / scale) - origin; // virtual 1080p px inside card [0..boxW, 0..boxH]

    // Tight bounding-box early-out (< 0.01 ms GPU cost, skips > 97% of pixels)
    if (rel.x < -12.0f || rel.x > boxW + 12.0f || rel.y < -12.0f || rel.y > boxH + 12.0f) {
        return baseCol;
    }

    float2 boxCenter = float2(boxW * 0.5f, boxH * 0.5f);
    float dBox = sdRoundedBoxHUD(rel - boxCenter, boxCenter, 8.0f);
    if (dBox > 10.0f) return baseCol;

    float shadow = smoothstep(10.0f, 0.0f, dBox) * 0.34f * step(0.0f, dBox) * alpha;
    float3 outCol = baseCol * (1.0f - shadow);

    float cardMask = smoothstep(1.2f, -0.8f, dBox) * alpha;
    if (cardMask <= 0.001f) return outCol;

    bool isMuted   = (music.flags & MUSIC_FLAG_MUTED) != 0u;
    bool isPursuit = (music.flags & MUSIC_FLAG_PURSUIT_SCORE) != 0u;
    bool isTunnel  = ((music.flags & MUSIC_FLAG_TUNNEL_REVERB) != 0u) || (music.skyOcclusion > 0.35f);

    float3 accentCol = float3(1.00f, 0.72f, 0.14f); // Iconic NFSMW 2005 EA TRAX gold-amber
    if (isMuted) {
        accentCol = float3(0.46f, 0.50f, 0.56f);
    } else if (isPursuit) {
        float pulse = 0.5f + 0.5f * sin(ext.simTimeSec * 8.0f);
        accentCol = mix(float3(0.22f, 0.72f, 1.00f), float3(1.00f, 0.44f, 0.14f), pulse);
    }

    // Smoked dark carbon-glass backing with subtle top sheen
    float sheen = smoothstep(boxH, 0.0f, rel.y) * 0.032f;
    float3 bg = mix(baseCol * 0.22f, float3(0.028f, 0.032f, 0.042f) + sheen, 0.84f);

    // Crisp outer rim + bold left NFSMW accent bar
    float border = smoothstep(1.5f, 0.3f, abs(dBox));
    bg = mix(bg, mix(float3(0.38f, 0.42f, 0.48f), accentCol, 0.45f), border * 0.72f);
    if (rel.x < 6.5f) {
        float stripe = smoothstep(6.5f, 4.8f, rel.x);
        bg = mix(bg, accentCol, stripe * 0.96f);
    }

    // Row 1 (y: 8..23): "EA TRAX" / "MUTED" / "SCORE" Pill Badge
    {
        float2 pillC = float2(52.0f, 15.5f);
        float dPill = sdRoundedBoxHUD(rel - pillC, float2(36.0f, 7.5f), 3.5f);
        float pMask = smoothstep(1.0f, -0.6f, dPill);
        if (pMask > 0.001f) {
            bg = mix(bg, accentCol, pMask * 0.96f);
            float txt = 0.0f;
            if (isMuted) {
                const uint kMuted[5] = { 77u, 85u, 84u, 69u, 68u }; // MUTED
                txt = evalLiteral7(rel, float2(31.0f, 10.7f), 1.35f, 8.2f, kMuted, 5u);
            } else if (isPursuit) {
                const uint kScore[5] = { 83u, 67u, 79u, 82u, 69u }; // SCORE
                txt = evalLiteral7(rel, float2(31.0f, 10.7f), 1.35f, 8.2f, kScore, 5u);
            } else {
                const uint kEaTrax[7] = { 69u, 65u, 32u, 84u, 82u, 65u, 88u }; // EA TRAX
                txt = evalLiteral7(rel, float2(23.5f, 10.7f), 1.35f, 8.2f, kEaTrax, 7u);
            }
            bg = mix(bg, float3(0.03f, 0.035f, 0.045f), txt * pMask);
        }
    }

    // Optional live acoustic reverb badge ("REVERB") when driving inside tunnels/underpasses
    if (isTunnel && !isMuted) {
        float2 revC = float2(126.0f, 15.5f);
        float dRev = sdRoundedBoxHUD(rel - revC, float2(30.0f, 7.5f), 3.5f);
        float rMask = smoothstep(1.0f, -0.6f, dRev);
        if (rMask > 0.001f) {
            float3 revTint = float3(0.18f, 0.86f, 1.00f);
            bg = mix(bg, float3(0.04f, 0.20f, 0.28f), rMask * 0.92f);
            bg = mix(bg, revTint, smoothstep(1.1f, 0.2f, abs(dRev)) * 0.85f);
            const uint kRev[6] = { 82u, 69u, 86u, 69u, 82u, 66u }; // REVERB
            float rTxt = evalLiteral7(rel, float2(101.5f, 10.8f), 1.30f, 8.0f, kRev, 6u);
            bg = mix(bg, revTint, rTxt * rMask);
        }
    }

    // Right-aligned track index "01/26"
    if (music.totalTracks > 0u) {
        uint cur = clamp(music.trackIndex, 1u, 99u);
        uint tot = clamp(music.totalTracks, 1u, 99u);
        const uint kIdx[5] = {
            48u + (cur / 10u), 48u + (cur % 10u), 47u,
            48u + (tot / 10u), 48u + (tot % 10u)
        };
        float idxTxt = evalLiteral7(rel, float2(274.0f, 10.8f), 1.35f, 8.2f, kIdx, 5u);
        bg = mix(bg, float3(0.72f, 0.76f, 0.82f), idxTxt * 0.92f);
    }

    // 5 animated mini spectrum EQ bars on top-right (x: 326..364, y: 9..22)
    if (rel.x >= 324.0f && rel.x <= 366.0f && rel.y >= 8.0f && rel.y <= 23.0f) {
        for (int b = 0; b < 5; ++b) {
            float bx = 327.0f + float(b) * 7.5f;
            float phase = ext.simTimeSec * (7.5f + float(b) * 2.1f) + float(b) * 1.4f;
            float level = isMuted ? 0.14f : (0.28f + 0.72f * (0.5f + 0.5f * sin(phase)));
            float barH = 12.5f * level;
            float inBar = smoothstep(0.6f, 0.0f, abs(rel.x - (bx + 2.2f)) - 2.0f)
                        * step(22.0f - barH, rel.y) * step(rel.y, 22.0f);
            float3 eqCol = isTunnel ? mix(accentCol, float3(0.22f, 0.90f, 1.00f), 0.45f) : accentCol;
            bg = mix(bg, eqCol, inBar * 0.94f);
        }
    }

    // Row 2 (y: 30..44): Crisp White Song Title (5x7 @ 1.95px cell, max 29 visible chars)
    {
        float sh = evalPackedString5x7(rel - float2(1.0f, 1.0f), float2(16.0f, 30.0f), 1.95f, 11.8f, music.titleChars, music.titleLen, 29u);
        float fg = evalPackedString5x7(rel, float2(16.0f, 30.0f), 1.95f, 11.8f, music.titleChars, music.titleLen, 29u);
        bg = mix(bg, float3(0.01f, 0.01f, 0.015f), sh * 0.65f);
        bg = mix(bg, float3(0.98f, 0.99f, 1.00f), fg * 0.98f);
    }

    // Row 3 (y: 51..62): Brushed Gold-Silver Artist Name (5x7 @ 1.55px cell, max 32 chars)
    {
        float sh = evalPackedString5x7(rel - float2(0.8f, 0.8f), float2(16.0f, 51.0f), 1.55f, 9.6f, music.artistChars, music.artistLen, 32u);
        float fg = evalPackedString5x7(rel, float2(16.0f, 51.0f), 1.55f, 9.6f, music.artistChars, music.artistLen, 32u);
        float3 artistCol = isMuted ? float3(0.56f, 0.60f, 0.66f) : float3(0.94f, 0.79f, 0.44f);
        bg = mix(bg, float3(0.01f, 0.01f, 0.015f), sh * 0.55f);
        bg = mix(bg, artistCol, fg * 0.95f);
    }

    return mix(outCol, bg, cardMask);
}

// Preserved public helper name (verified by tests/test_metal_raytracing.py)
static inline float3 compositeDigitalDrivingHUD(
    float3 baseCol,
    float2 px,
    float2 res,
    constant RTFrameUniforms& u,
    constant RTHudExtUniforms& ext,
    constant RTMinimapBuffer& minimap
) {
    float3 col = compositeNFSMWTachCluster(baseCol, px, res, u, ext);
    col = compositeGPUMinimap(col, px, res, ext, minimap);
    col = compositePursuitBar(col, px, res, ext);
    col = compositeTopRightTelemetry(col, px, res, ext);
    return col;
}

// Standalone Post-FrameGen HUD + Presentation Output Kernel
// Reads perceptual sRGB LDR input (real or MTLFXFrameInterpolator output), composites crisp
// unwarped NFSMW 2005 SDF HUD + GPU Minimap + sliding EA TRAX Banner, and writes output.
kernel void hudOverlayKernel(
    uint2 gid [[thread_position_in_grid]],
    texture2d<float, access::read> inLdrTex [[texture(0)]],
    texture2d<float, access::write> outTex [[texture(1)]],
    constant RTFrameUniforms& u [[buffer(0)]],
    constant uint& encodeSRGB [[buffer(1)]],
    constant RTHudExtUniforms& ext [[buffer(2)]],
    constant RTMinimapBuffer& minimap [[buffer(3)]],
    constant RTMusicBannerUniforms& music [[buffer(4)]]
) {
    uint w = outTex.get_width();
    uint h = outTex.get_height();
    if (gid.x >= w || gid.y >= h) return;

    float3 srgb = inLdrTex.read(gid).rgb;
    float2 px = float2(gid) + 0.5f;
    float2 res = float2(float(w), float(h));
    bool hudVisible = (u.hudParams.x > 0.5f) || ((ext.hudFlags & HUD_FLAG_VISIBLE) != 0u);
    if (hudVisible) {
        srgb = compositeDigitalDrivingHUD(srgb, px, res, u, ext, minimap);
    }
    if (music.bannerAlpha > 0.001f) {
        srgb = compositeEATraxBanner(srgb, px, res, ext, music);
    }

    float3 outRGB = (encodeSRGB != 0u) ? srgb : srgbToLinear(srgb);
    outTex.write(float4(outRGB, 1.0f), gid);
}
