// ---------------------------------------------------------------------------
// RTHud.metal — Authentic NFSMW 2005 Post-FrameGen Crisp SDF Driving HUD + GPU Minimap
// Concatenated after RayTracing.metal at runtime before device.makeLibrary(source:)
// so HUD digits & minimap NEVER warp under MTLFXFrameInterpolator (zero #include).
//
// Locked Buffer & Texture Slots (per #40 / #41 + v1.1.0 EA TRAX + v1.2.0 Events/WorldMap):
//   texture(0) = inLdrTex (read), texture(1) = outTex (write), texture(2) = worldMapTex (sample)
//   sampler(0) = linearSamp
//   buffer(0) = RTFrameUniforms       (unchanged base contract)
//   buffer(1) = uint encodeSRGB       (unchanged base contract)
//   buffer(2) = RTHudExtUniforms      (64 bytes, 16B aligned)
//   buffer(3) = RTMinimapBuffer       (1,936 bytes <= 2 KB, 16B aligned)
//   buffer(4) = RTMusicBannerUniforms (112 bytes <= 128 B, 16B aligned)
//   buffer(5) = RTEventHudUniforms    (864 bytes <= 1 KB, 16B aligned)
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

struct RTEventHudUniforms {
    uint  mode;                 // 0=none, 1=sprint, 2=speedtrap, 3=boss
    uint  state;                // 0=idle, 1=countdown, 2=active, 3=win, 4=lose
    uint  cpIndex;              // 1-based current cp / trap
    uint  cpTotal;

    float elapsedSec;
    float currentScore;         // speedtrap cumulative km/h
    float targetScore;          // speedtrap target km/h
    float bossGapM;             // >0 player ahead, <0 boss ahead

    uint  playerRank;           // 1 or 2
    uint  bossRank;             // 1..15
    uint  flags;                // bit0=eventActive, bit1=worldMapOpen, bit2=hasSelected, bit3=radarFlash
    float nextGateDistM;

    float4 mapViewParams;       // xy=centerNorm, z=zoom, w=playerHeadingRad
    float4 playerMapPos;        // xy=playerNorm, z=selectedIdx, w=markerCount
    float4 mapMarkers[24];      // xy=normPos, z=type [0=landmark,1=sprint,2=speedtrap,3=boss], w=bossRank
    float4 gpsRoute[16];        // xy=normStart, zw=normEnd

    uint  eventTitleLen;
    uint  bossNameLen;
    uint  selectedTitleLen;
    uint  selectedSubLen;

    uint  eventTitleChars[8];   // 32 ASCII bytes
    uint  bossNameChars[6];     // 24 ASCII bytes
    uint  selectedTitleChars[8];// 32 ASCII bytes
    uint  selectedSubChars[8];  // 32 ASCII bytes

    uint  gpsSegCount;
    uint  pad0;
};
static_assert(sizeof(RTEventHudUniforms) == 864, "RTEventHudUniforms must be 864 bytes (<= 1 KB, 16B aligned)");

static constant uint EVENT_HUD_FLAG_ACTIVE       = 1u << 0;
static constant uint EVENT_HUD_FLAG_WORLD_MAP    = 1u << 1;
static constant uint EVENT_HUD_FLAG_HAS_SELECTED = 1u << 2;
static constant uint EVENT_HUD_FLAG_RADAR_FLASH  = 1u << 3;

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

// Helper to render up to 5-digit unsigned integer in 5x7 font (returns coverage)
static inline float evalUint5x7(float2 relPx, float2 originPx, float cellPx, float stridePx, uint val, uint minDigits) {
    uint digs[5];
    uint n = 0u;
    uint v = min(val, 99999u);
    do {
        digs[n++] = 48u + (v % 10u);
        v /= 10u;
    } while (v > 0u && n < 5u);
    while (n < min(minDigits, 5u)) {
        digs[n++] = 48u;
    }
    uint rev[5];
    for (uint i = 0u; i < n; ++i) rev[i] = digs[n - 1u - i];
    return evalLiteral7(relPx, originPx, cellPx, stridePx, rev, n);
}

// Helper to render MM:SS.T timer in 5x7 font
static inline float evalTimer5x7(float2 relPx, float2 originPx, float cellPx, float stridePx, float sec) {
    float s = max(sec, 0.0f);
    uint totalTenths = uint(s * 10.0f);
    uint tenths = totalTenths % 10u;
    uint totalSec = totalTenths / 10u;
    uint mins = min(totalSec / 60u, 99u);
    uint secs = totalSec % 60u;
    const uint buf[7] = {
        48u + (mins / 10u), 48u + (mins % 10u), 58u,
        48u + (secs / 10u), 48u + (secs % 10u), 46u,
        48u + tenths
    };
    return evalLiteral7(relPx, originPx, cellPx, stridePx, buf, 7u);
}

// ---------------------------------------------------------------------------
// Widget 6: Top-Center Active NFSMW Event HUD Strip (Sprint / Speedtrap / Blacklist Boss)
// ---------------------------------------------------------------------------

static inline float3 compositeEventHudStrip(
    float3 baseCol,
    float2 px,
    float2 res,
    constant RTHudExtUniforms& ext,
    constant RTEventHudUniforms& ev
) {
    if ((ev.flags & EVENT_HUD_FLAG_ACTIVE) == 0u || ev.mode == 0u) return baseCol;

    float scale = res.y / 1080.0f;
    bool pursuitOn = (ext.heatLevel > 0.5f) || ((ext.hudFlags & HUD_FLAG_PURSUIT_ACTIVE) != 0u);
    float centerY = pursuitOn ? 104.0f : 44.0f;
    float2 center = float2(res.x * 0.5f, centerY * scale);
    float2 rel = (px - center) / scale; // virtual 1080p coords centered on strip

    const float halfW = 248.0f;
    const float halfH = 28.0f;
    // Allow extra vertical room below strip for victory/finish banner when finished
    bool finished = (ev.state == 3u || ev.state == 4u);
    if (abs(rel.x) > halfW + 14.0f || rel.y < -(halfH + 12.0f) || rel.y > (finished ? 76.0f : (halfH + 12.0f))) {
        return baseCol;
    }

    float3 accent = float3(1.00f, 0.70f, 0.12f); // Sprint Gold
    if (ev.mode == 2u) accent = float3(0.10f, 0.88f, 1.00f); // Speedtrap Cyan
    else if (ev.mode == 3u) accent = float3(1.00f, 0.22f, 0.14f); // Blacklist Crimson-Gold

    float3 outCol = baseCol;
    float dBox = sdRoundedBoxHUD(rel, float2(halfW, halfH), 8.0f);
    if (dBox <= 10.0f) {
        float shadow = smoothstep(10.0f, 0.0f, dBox) * 0.34f * step(0.0f, dBox);
        outCol *= (1.0f - shadow);
        float mask = smoothstep(1.2f, -0.8f, dBox);
        if (mask > 0.001f) {
            float3 bg = mix(baseCol * 0.22f, float3(0.028f, 0.032f, 0.044f), 0.84f);
            float border = smoothstep(1.5f, 0.3f, abs(dBox));
            bg = mix(bg, mix(float3(0.40f, 0.44f, 0.52f), accent, 0.55f), border * 0.78f);

            // Top 2.5px glowing event accent bar
            if (rel.y < -halfH + 3.2f) {
                float3 topCol = (ev.mode == 3u) ? mix(accent, float3(1.00f, 0.76f, 0.14f), smoothstep(-halfW, halfW, rel.x)) : accent;
                bg = mix(bg, topCol, 0.94f);
            }

            // Local origin inside card [0..496, 0..56]
            float2 cRel = rel + float2(halfW, halfH);

            // Row 1 Left: Mode Pill Badge
            float pillHalfW = (ev.mode == 2u) ? 46.0f : 38.0f;
            float2 pillC = float2(14.0f + pillHalfW, 16.5f);
            float dPill = sdRoundedBoxHUD(cRel - pillC, float2(pillHalfW, 8.0f), 3.5f);
            float pM = smoothstep(1.0f, -0.6f, dPill);
            if (pM > 0.001f) {
                bg = mix(bg, accent, pM * 0.96f);
                float txt = 0.0f;
                if (ev.mode == 1u) {
                    const uint kSpr[6] = { 83u,80u,82u,73u,78u,84u }; // SPRINT
                    txt = evalLiteral7(cRel, float2(27.0f, 11.7f), 1.35f, 8.2f, kSpr, 6u);
                } else if (ev.mode == 2u) {
                    const uint kSt[9] = { 83u,80u,69u,69u,68u,84u,82u,65u,80u }; // SPEEDTRAP
                    txt = evalLiteral7(cRel, float2(23.5f, 11.8f), 1.30f, 8.0f, kSt, 9u);
                } else {
                    // "BL #15"
                    uint rk = clamp(ev.bossRank, 1u, 15u);
                    const uint kBl[6] = { 66u,76u,32u,35u, 48u + (rk / 10u), 48u + (rk % 10u) };
                    txt = evalLiteral7(cRel, float2(25.5f, 11.7f), 1.35f, 8.2f, kBl, 6u);
                }
                float3 pillInk = (ev.mode == 3u) ? float3(0.98f, 0.99f, 1.00f) : float3(0.03f, 0.035f, 0.045f);
                bg = mix(bg, pillInk, txt * pM);
            }

            // Row 1 Center: Event Title (or Boss Name in Boss Duel)
            float titleX = 24.0f + pillHalfW * 2.0f;
            if (ev.mode == 3u && ev.bossNameLen > 0u) {
                float tFg = evalPackedString5x7(cRel, float2(titleX, 11.2f), 1.55f, 9.6f, ev.bossNameChars, ev.bossNameLen, 24u);
                bg = mix(bg, float3(1.00f, 0.90f, 0.62f), tFg);
            } else {
                float tFg = evalPackedString5x7(cRel, float2(titleX, 11.2f), 1.55f, 9.6f, ev.eventTitleChars, ev.eventTitleLen, 26u);
                bg = mix(bg, float3(0.96f, 0.98f, 1.00f), tFg);
            }

            // Row 1 Right: CP / TRAP Counter ("CP 03/12" or "TR 2/5")
            if (ev.cpTotal > 0u) {
                uint cur = clamp(ev.cpIndex, 1u, 99u);
                uint tot = clamp(ev.cpTotal, 1u, 99u);
                const uint kCp[8] = {
                    (ev.mode == 2u) ? 84u : 67u, (ev.mode == 2u) ? 82u : 80u, 32u,
                    48u + (cur / 10u), 48u + (cur % 10u), 47u,
                    48u + (tot / 10u), 48u + (tot % 10u)
                };
                float cpFg = evalLiteral7(cRel, float2(408.0f, 11.2f), 1.50f, 9.4f, kCp, 8u);
                bg = mix(bg, accent, cpFg);
            }

            // Row 2 (y: 33..46): Mode-specific Live Telemetry
            if (ev.mode == 2u) {
                // Speedtrap: cumulative KM/H + target + cyan progress bar
                float curSpd = max(ev.currentScore, 0.0f);
                float tgtSpd = max(ev.targetScore, 1.0f);
                const uint kTot[3] = { 84u,79u,84u }; // TOT
                bg = mix(bg, float3(0.68f, 0.78f, 0.86f), evalLiteral7(cRel, float2(16.0f, 34.5f), 1.45f, 8.8f, kTot, 3u));
                bg = mix(bg, float3(0.28f, 0.96f, 1.00f), evalUint5x7(cRel, float2(52.0f, 33.5f), 1.70f, 10.2f, uint(curSpd + 0.5f), 1u));
                const uint kSlash[1] = { 47u };
                bg = mix(bg, float3(0.62f, 0.70f, 0.78f), evalLiteral7(cRel, float2(104.0f, 34.5f), 1.50f, 8.8f, kSlash, 1u));
                bg = mix(bg, float3(0.92f, 0.96f, 1.00f), evalUint5x7(cRel, float2(118.0f, 34.5f), 1.50f, 9.2f, uint(tgtSpd + 0.5f), 1u));
                const uint kKmh[3] = { 75u,77u,72u };
                bg = mix(bg, float3(0.64f, 0.76f, 0.86f), evalLiteral7(cRel, float2(164.0f, 34.5f), 1.40f, 8.6f, kKmh, 3u));

                // Progress bar (x: 212..392, y: 36..44)
                if (cRel.x >= 212.0f && cRel.x <= 392.0f && cRel.y >= 36.0f && cRel.y <= 44.0f) {
                    float uP = (cRel.x - 212.0f) / 180.0f;
                    float fill = clamp(curSpd / tgtSpd, 0.0f, 1.0f);
                    float3 barC = (uP <= fill) ? mix(float3(0.06f, 0.74f, 1.00f), float3(0.55f, 1.00f, 1.00f), uP) : float3(0.10f, 0.14f, 0.20f);
                    bg = mix(bg, barC, 0.94f);
                }
                bg = mix(bg, float3(0.92f, 0.96f, 1.00f), evalTimer5x7(cRel, float2(414.0f, 34.5f), 1.50f, 9.4f, ev.elapsedSec));
            } else if (ev.mode == 3u) {
                // Blacklist Boss Duel: 1ST/2ND badge + Gap (+42M / -18M) + Next Gate + Timer
                bool first = (ev.playerRank <= 1u);
                float3 rkCol = first ? float3(1.00f, 0.78f, 0.16f) : float3(1.00f, 0.24f, 0.16f);
                const uint k1st[3] = { 49u,83u,84u };
                const uint k2nd[3] = { 50u,78u,68u };
                bg = mix(bg, rkCol, evalLiteral7(cRel, float2(18.0f, 33.0f), 1.75f, 10.6f, first ? k1st : k2nd, 3u));

                const uint kGap[3] = { 71u,65u,80u }; // GAP
                bg = mix(bg, float3(0.68f, 0.74f, 0.80f), evalLiteral7(cRel, float2(74.0f, 34.5f), 1.40f, 8.6f, kGap, 3u));
                const uint kSign[1] = { (ev.bossGapM >= 0.0f) ? 43u : 45u }; // + or -
                bg = mix(bg, rkCol, evalLiteral7(cRel, float2(110.0f, 34.0f), 1.55f, 9.2f, kSign, 1u));
                uint absGap = uint(abs(ev.bossGapM) + 0.5f);
                bg = mix(bg, rkCol, evalUint5x7(cRel, float2(122.0f, 34.0f), 1.55f, 9.4f, absGap, 1u));
                const uint kM[1] = { 77u }; // M
                bg = mix(bg, float3(0.76f, 0.80f, 0.86f), evalLiteral7(cRel, float2(162.0f, 34.5f), 1.40f, 8.6f, kM, 1u));

                // Next gate dist
                const uint kNxt[4] = { 71u,65u,84u,69u }; // GATE
                bg = mix(bg, float3(0.68f, 0.74f, 0.80f), evalLiteral7(cRel, float2(214.0f, 34.5f), 1.40f, 8.6f, kNxt, 4u));
                bg = mix(bg, float3(0.98f, 0.99f, 1.00f), evalUint5x7(cRel, float2(258.0f, 34.0f), 1.55f, 9.4f, uint(max(ev.nextGateDistM, 0.0f) + 0.5f), 1u));
                bg = mix(bg, float3(0.76f, 0.80f, 0.86f), evalLiteral7(cRel, float2(304.0f, 34.5f), 1.40f, 8.6f, kM, 1u));

                bg = mix(bg, float3(0.95f, 0.97f, 1.00f), evalTimer5x7(cRel, float2(414.0f, 34.0f), 1.55f, 9.4f, ev.elapsedSec));
            } else {
                // Sprint: Timer + Next Gate Distance
                const uint kTim[4] = { 84u,73u,77u,69u }; // TIME
                bg = mix(bg, float3(0.70f, 0.76f, 0.82f), evalLiteral7(cRel, float2(18.0f, 34.5f), 1.45f, 8.8f, kTim, 4u));
                bg = mix(bg, float3(0.98f, 0.99f, 1.00f), evalTimer5x7(cRel, float2(64.0f, 33.5f), 1.70f, 10.2f, ev.elapsedSec));
                const uint kNxt[4] = { 78u,69u,88u,84u }; // NEXT
                bg = mix(bg, float3(0.70f, 0.76f, 0.82f), evalLiteral7(cRel, float2(246.0f, 34.5f), 1.45f, 8.8f, kNxt, 4u));
                bg = mix(bg, accent, evalUint5x7(cRel, float2(292.0f, 33.5f), 1.70f, 10.2f, uint(max(ev.nextGateDistM, 0.0f) + 0.5f), 1u));
                const uint kM[1] = { 77u };
                bg = mix(bg, float3(0.78f, 0.82f, 0.88f), evalLiteral7(cRel, float2(342.0f, 34.5f), 1.45f, 8.8f, kM, 1u));
            }

            outCol = mix(outCol, bg, mask);
        }
    }

    // Optional Finish / Victory / Defeat Pill directly beneath strip
    if (finished) {
        float2 fRel = rel - float2(0.0f, 48.0f);
        float dFin = sdRoundedBoxHUD(fRel, float2(158.0f, 16.0f), 6.0f);
        float fM = smoothstep(1.0f, -0.6f, dFin);
        if (fM > 0.001f) {
            bool won = (ev.state == 3u);
            float3 fCol = won ? float3(1.00f, 0.78f, 0.16f) : float3(1.00f, 0.22f, 0.16f);
            float3 fBg = mix(float3(0.04f, 0.045f, 0.06f), fCol * 0.24f, 0.55f);
            fBg = mix(fBg, fCol, smoothstep(1.4f, 0.2f, abs(dFin)) * 0.90f);
            float2 fc = fRel + float2(158.0f, 16.0f);
            if (won) {
                const uint kWin[14] = { 69u,86u,69u,78u,84u,32u,67u,79u,77u,80u,76u,69u,84u,69u }; // EVENT COMPLETE
                fBg = mix(fBg, fCol, evalLiteral7(fc, float2(68.0f, 9.5f), 1.75f, 11.2f, kWin, 14u));
            } else {
                const uint kLos[12] = { 69u,86u,69u,78u,84u,32u,70u,65u,73u,76u,69u,68u }; // EVENT FAILED
                fBg = mix(fBg, fCol, evalLiteral7(fc, float2(80.0f, 9.5f), 1.75f, 11.2f, kLos, 12u));
            }
            outCol = mix(outCol, fBg, fM * 0.95f);
        }
    }

    return outCol;
}

// ---------------------------------------------------------------------------
// Widget 7: Interactive O(1) Full-Island Rockport World Map Overlay (`M` toggle)
// Samples pre-baked 1024x1024 6,538-segment road map texture in O(1) + crisp SDF UI.
// ---------------------------------------------------------------------------

static inline float3 compositeWorldMapOverlay(
    float3 baseCol,
    float2 px,
    float2 res,
    constant RTHudExtUniforms& ext,
    constant RTEventHudUniforms& ev,
    texture2d<float, access::sample> worldMapTex,
    sampler linearSamp
) {
    if ((ev.flags & EVENT_HUD_FLAG_WORLD_MAP) == 0u) return baseCol;

    float scale = res.y / 1080.0f;
    float2 vPx = px / scale; // virtual 1080p coords [0..1920, 0..1080]

    // Smooth dark tactical backdrop dimming live RT Rockport underneath
    float3 outCol = baseCol * 0.22f + float3(0.012f, 0.018f, 0.028f);

    // Centered World Map Frame: x=[280..1640] (1360w), y=[128..952] (824h)
    const float2 frameOrg = float2(280.0f, 128.0f);
    const float2 frameSz  = float2(1360.0f, 824.0f);
    float2 fRel = vPx - frameOrg;
    float dFrame = sdRoundedBoxHUD(fRel - frameSz * 0.5f, frameSz * 0.5f, 12.0f);

    if (dFrame > 16.0f) return outCol;
    float fShadow = smoothstep(16.0f, 0.0f, dFrame) * 0.45f * step(0.0f, dFrame);
    outCol *= (1.0f - fShadow);

    float frameMask = smoothstep(1.2f, -0.8f, dFrame);
    if (frameMask <= 0.001f) return outCol;

    float3 card = float3(0.026f, 0.034f, 0.048f);
    float fBorder = smoothstep(1.8f, 0.4f, abs(dFrame));
    card = mix(card, float3(0.42f, 0.48f, 0.58f), fBorder * 0.72f);

    // Header Bar (y: 0..56)
    if (fRel.y < 56.0f) {
        card = float3(0.036f, 0.044f, 0.062f);
        if (fRel.y < 3.5f) card = float3(1.00f, 0.72f, 0.14f);
        // [WORLD MAP] Gold Pill
        float dWp = sdRoundedBoxHUD(fRel - float2(86.0f, 29.0f), float2(64.0f, 12.0f), 4.5f);
        float wpM = smoothstep(1.0f, -0.6f, dWp);
        if (wpM > 0.001f) {
            card = mix(card, float3(1.00f, 0.72f, 0.14f), wpM);
            const uint kWm[9] = { 87u,79u,82u,76u,68u,32u,77u,65u,80u }; // WORLD MAP
            card = mix(card, float3(0.03f, 0.035f, 0.045f), evalLiteral7(fRel, float2(37.0f, 22.8f), 1.75f, 10.8f, kWm, 9u) * wpM);
        }
        const uint kCity[13] = { 82u,79u,67u,75u,80u,79u,82u,84u,32u,67u,73u,84u,89u }; // ROCKPORT CITY
        card = mix(card, float3(0.96f, 0.98f, 1.00f), evalLiteral7(fRel, float2(170.0f, 21.0f), 2.15f, 13.0f, kCity, 13u));

        const uint kStats[18] = { 54u,53u,51u,56u,32u,82u,79u,65u,68u,83u,32u,45u,32u,50u,50u,57u,75u,77u }; // 6538 ROADS - 229KM
        card = mix(card, float3(0.62f, 0.74f, 0.88f), evalLiteral7(fRel, float2(968.0f, 23.0f), 1.60f, 9.8f, kStats, 18u));

        // Zoom readout "1.0X"
        float zm = clamp(ev.mapViewParams.z, 1.0f, 4.5f);
        uint z10 = uint(zm * 10.0f + 0.5f);
        const uint kZm[4] = { 48u + (z10 / 10u), 46u, 48u + (z10 % 10u), 88u };
        card = mix(card, float3(1.00f, 0.76f, 0.22f), evalLiteral7(fRel, float2(1292.0f, 22.0f), 1.80f, 11.0f, kZm, 4u));
    }

    // Main Interactive Map Viewport: x=[20..970] (950w), y=[68..758] (690h)
    const float2 vpOrg = float2(20.0f, 68.0f);
    const float2 vpSz  = float2(950.0f, 690.0f);
    float2 vpRel = fRel - vpOrg;
    float dVp = sdRoundedBoxHUD(vpRel - vpSz * 0.5f, vpSz * 0.5f, 8.0f);
    if (dVp <= 2.0f) {
        float vpMask = smoothstep(1.0f, -0.6f, dVp);
        float2 vpUV = vpRel / vpSz;
        float zoom = clamp(ev.mapViewParams.z, 1.0f, 4.5f);
        float2 mapCenter = ev.mapViewParams.xy;
        // Aspect-preserving square world map projection
        float vpAspect = vpSz.x / vpSz.y;
        float2 centered = (vpUV - 0.5f) * float2(vpAspect, 1.0f) / zoom;
        float2 mapUV = centered + mapCenter;

        float3 mCol = float3(0.018f, 0.032f, 0.052f); // Deep Rockport Bay navy outside land
        if (mapUV.x >= 0.0f && mapUV.x <= 1.0f && mapUV.y >= 0.0f && mapUV.y <= 1.0f) {
            // O(1) pre-baked 1024x1024 full-island 6,538-segment road texture!
            mCol = worldMapTex.sample(linearSamp, mapUV, level(0.0f)).rgb;
        }

        // Subtle tactical coordinate grid
        float2 gF = abs(fract(mapUV * 8.0f) - 0.5f);
        float gridLine = smoothstep(0.012f, 0.002f, min(gF.x, gF.y));
        mCol += float3(0.08f, 0.14f, 0.22f) * gridLine * 0.28f;

        // Convert any normalized map coordinate [0..1] into viewport pixel coords
        float pxPerMap = vpSz.y * zoom;

        // 1. Glowing GPS Route Polyline (up to 16 segments)
        uint gpsN = min(ev.gpsSegCount, 16u);
        if (gpsN > 0u) {
            float minGpsPx = 1e5f;
            for (uint i = 0u; i < gpsN; ++i) {
                float4 seg = ev.gpsRoute[i];
                if (seg.x < -0.5f) continue;
                float2 p0 = (seg.xy - mapCenter) * pxPerMap + vpSz * 0.5f;
                float2 p1 = (seg.zw - mapCenter) * pxPerMap + vpSz * 0.5f;
                minGpsPx = min(minGpsPx, sdSegmentHUD(vpRel, p0, p1));
            }
            float3 gpsCol = (ev.mode == 2u) ? float3(0.14f, 0.90f, 1.00f)
                          : ((ev.mode == 3u) ? float3(1.00f, 0.26f, 0.14f) : float3(1.00f, 0.74f, 0.14f));
            mCol += gpsCol * smoothstep(9.0f, 1.5f, minGpsPx) * 0.45f;
            mCol = mix(mCol, mix(gpsCol, float3(1.0f), 0.35f), smoothstep(3.2f, 1.4f, minGpsPx) * 0.95f);
        }

        // 2. Interactive Event & Landmark Markers (up to 24)
        uint mCount = min(uint(ev.playerMapPos.w + 0.5f), 24u);
        int selIdx = int(ev.playerMapPos.z + 0.5f);
        for (uint i = 0u; i < mCount; ++i) {
            float4 mk = ev.mapMarkers[i];
            float2 mPx = (mk.xy - mapCenter) * pxPerMap + vpSz * 0.5f;
            float2 d = vpRel - mPx;
            float dist = length(d);
            if (dist > 28.0f) continue;

            int mType = int(mk.z + 0.5f);
            float3 mkCol = float3(0.92f, 0.95f, 1.00f); // 0 = Landmark White
            if (mType == 1) mkCol = float3(1.00f, 0.74f, 0.14f);      // 1 = Sprint Gold
            else if (mType == 2) mkCol = float3(0.10f, 0.88f, 1.00f); // 2 = Speedtrap Cyan
            else if (mType == 3) mkCol = float3(1.00f, 0.20f, 0.12f); // 3 = Blacklist Boss Crimson

            bool isSel = (int(i) == selIdx);
            float iconR = isSel ? 9.5f : 6.5f;
            float halo = smoothstep(iconR * 2.4f, iconR * 0.6f, dist) * (isSel ? 0.55f : 0.24f);
            mCol += mkCol * halo;

            if (mType == 0) {
                // Landmark diamond
                float diam = (abs(d.x) + abs(d.y)) - iconR;
                mCol = mix(mCol, float3(0.02f, 0.03f, 0.05f), smoothstep(2.4f, 0.6f, diam) * 0.85f);
                mCol = mix(mCol, mkCol, smoothstep(0.8f, -0.8f, diam));
            } else if (mType == 2) {
                // Speedtrap radar square
                float sq = sdRoundedBoxHUD(d, float2(iconR * 0.82f), 2.0f);
                mCol = mix(mCol, float3(0.02f, 0.03f, 0.05f), smoothstep(2.2f, 0.4f, sq) * 0.85f);
                mCol = mix(mCol, mkCol, smoothstep(0.8f, -0.8f, sq));
            } else if (mType == 3) {
                // Blacklist Boss crimson shield + gold core
                float circ = dist - iconR;
                mCol = mix(mCol, float3(0.02f, 0.02f, 0.03f), smoothstep(2.4f, 0.6f, circ) * 0.90f);
                mCol = mix(mCol, mkCol, smoothstep(0.8f, -0.8f, circ));
                mCol = mix(mCol, float3(1.00f, 0.82f, 0.20f), smoothstep(iconR * 0.48f, iconR * 0.30f, dist));
            } else {
                // Sprint gold chevron ring
                float circ = dist - iconR;
                mCol = mix(mCol, float3(0.02f, 0.03f, 0.04f), smoothstep(2.2f, 0.4f, circ) * 0.85f);
                mCol = mix(mCol, mkCol, smoothstep(0.8f, -0.8f, circ));
                mCol = mix(mCol, float3(0.06f, 0.07f, 0.09f), smoothstep(iconR * 0.44f, iconR * 0.28f, dist));
            }

            if (isSel) {
                float pulseR = 14.5f + 2.5f * sin(ext.simTimeSec * 7.0f);
                float ring = smoothstep(2.0f, 0.4f, abs(dist - pulseR));
                mCol = mix(mCol, float3(1.00f, 0.96f, 0.78f), ring * 0.95f);
            }
        }

        // 3. Pulsing Player Arrow on Map
        {
            float2 pPx = (ev.playerMapPos.xy - mapCenter) * pxPerMap + vpSz * 0.5f;
            float2 d = vpRel - pPx;
            if (length(d) < 22.0f) {
                // Rotate into player heading (+Z world -> -Y on map or +Y depending on bake; align with heading)
                float yaw = ev.mapViewParams.w;
                float cy = cos(yaw), sy = sin(yaw);
                float2 rp = float2(d.x * cy - d.y * sy, d.x * sy + d.y * cy);
                float pTri = sdTriangleHUD(rp, float2(0.0f, -11.0f), float2(-7.5f, 8.5f), float2(7.5f, 8.5f));
                mCol = mix(mCol, float3(0.02f, 0.02f, 0.04f), smoothstep(2.6f, 0.6f, pTri) * 0.90f);
                mCol = mix(mCol, float3(0.32f, 1.00f, 0.56f), smoothstep(0.8f, -0.8f, pTri));
            }
        }

        // Top-Right North Compass Pill inside Viewport
        {
            float2 nC = float2(vpSz.x - 28.0f, 26.0f);
            float dN = length(vpRel - nC) - 15.0f;
            float nM = smoothstep(1.0f, -0.6f, dN);
            if (nM > 0.001f) {
                mCol = mix(mCol, float3(0.03f, 0.04f, 0.06f), nM * 0.85f);
                mCol = mix(mCol, float3(1.00f, 0.72f, 0.14f), smoothstep(1.3f, 0.2f, abs(dN)) * 0.85f);
                const uint kN[1] = { 78u };
                mCol = mix(mCol, float3(1.00f, 0.78f, 0.24f), evalLiteral7(vpRel, nC - float2(4.2f, 5.8f), 1.65f, 10.0f, kN, 1u));
            }
        }

        float vpBorder = smoothstep(1.8f, 0.3f, abs(dVp));
        mCol = mix(mCol, float3(0.36f, 0.44f, 0.56f), vpBorder * 0.75f);
        card = mix(card, mCol, vpMask);
    }

    // Right Inspector Sidebar: x=[988..1340] (352w), y=[68..758] (690h)
    if (fRel.x >= 986.0f && fRel.x <= 1342.0f && fRel.y >= 66.0f && fRel.y <= 760.0f) {
        float2 sRel = fRel - float2(988.0f, 68.0f); // [0..352, 0..690]

        // Card A: Selected Event / Landmark Inspector (y: 0..270)
        float dSel = sdRoundedBoxHUD(sRel - float2(176.0f, 135.0f), float2(176.0f, 135.0f), 8.0f);
        if (dSel <= 1.5f) {
            float sM = smoothstep(1.0f, -0.6f, dSel);
            float3 sBg = float3(0.034f, 0.044f, 0.062f);
            int selIdx = clamp(int(ev.playerMapPos.z + 0.5f), 0, 23);
            int selType = int(ev.mapMarkers[selIdx].z + 0.5f);
            float3 selAccent = (selType == 3) ? float3(1.00f, 0.22f, 0.14f)
                             : ((selType == 2) ? float3(0.10f, 0.88f, 1.00f)
                             : ((selType == 1) ? float3(1.00f, 0.72f, 0.14f) : float3(0.86f, 0.92f, 1.00f)));
            sBg = mix(sBg, selAccent, smoothstep(1.5f, 0.3f, abs(dSel)) * 0.65f);
            if (sRel.y < 3.5f) sBg = selAccent;

            // Selected Type Pill
            float dTp = sdRoundedBoxHUD(sRel - float2(96.0f, 30.0f), float2(78.0f, 11.0f), 4.0f);
            float tpM = smoothstep(1.0f, -0.6f, dTp);
            if (tpM > 0.001f) {
                sBg = mix(sBg, selAccent, tpM);
                float tt = 0.0f;
                if (selType == 3) {
                    const uint kT[14] = { 66u,76u,65u,67u,75u,76u,73u,83u,84u,32u,68u,85u,69u,76u }; // BLACKLIST DUEL
                    tt = evalLiteral7(sRel, float2(27.0f, 24.5f), 1.45f, 9.2f, kT, 14u);
                } else if (selType == 2) {
                    const uint kT[9] = { 83u,80u,69u,69u,68u,84u,82u,65u,80u }; // SPEEDTRAP
                    tt = evalLiteral7(sRel, float2(48.0f, 24.5f), 1.55f, 9.8f, kT, 9u);
                } else if (selType == 1) {
                    const uint kT[11] = { 83u,80u,82u,73u,78u,84u,32u,82u,65u,67u,69u }; // SPRINT RACE
                    tt = evalLiteral7(sRel, float2(40.0f, 24.5f), 1.50f, 9.5f, kT, 11u);
                } else {
                    const uint kT[8] = { 76u,65u,78u,68u,77u,65u,82u,75u }; // LANDMARK
                    tt = evalLiteral7(sRel, float2(52.0f, 24.5f), 1.55f, 9.8f, kT, 8u);
                }
                float3 selInk = (selType == 3) ? float3(0.98f, 0.99f, 1.00f) : float3(0.03f, 0.035f, 0.045f);
                sBg = mix(sBg, selInk, tt * tpM);
            }

            // Selected Title & Subtitle
            sBg = mix(sBg, float3(0.98f, 0.99f, 1.00f), evalPackedString5x7(sRel, float2(18.0f, 64.0f), 1.85f, 11.2f, ev.selectedTitleChars, ev.selectedTitleLen, 28u));
            sBg = mix(sBg, float3(0.76f, 0.84f, 0.94f), evalPackedString5x7(sRel, float2(18.0f, 98.0f), 1.50f, 9.4f, ev.selectedSubChars, ev.selectedSubLen, 32u));

            // Glowing Action Button: [ENTER] LAUNCH / FAST TRAVEL
            float dBtn = sdRoundedBoxHUD(sRel - float2(176.0f, 224.0f), float2(154.0f, 20.0f), 6.0f);
            float bM = smoothstep(1.0f, -0.6f, dBtn);
            if (bM > 0.001f) {
                sBg = mix(sBg, selAccent * 0.24f, bM);
                sBg = mix(sBg, selAccent, smoothstep(1.5f, 0.3f, abs(dBtn)) * 0.92f);
                if (selType == 0) {
                    const uint kGo[19] = { 91u,69u,78u,84u,69u,82u,93u,32u,70u,65u,83u,84u,32u,84u,82u,65u,86u,69u,76u }; // [ENTER] FAST TRAVEL
                    sBg = mix(sBg, selAccent, evalLiteral7(sRel, float2(79.0f, 217.5f), 1.65f, 10.2f, kGo, 19u) * bM);
                } else {
                    const uint kGo[20] = { 91u,69u,78u,84u,69u,82u,93u,32u,76u,65u,85u,78u,67u,72u,32u,69u,86u,69u,78u,84u }; // [ENTER] LAUNCH EVENT
                    sBg = mix(sBg, selAccent, evalLiteral7(sRel, float2(74.0f, 217.5f), 1.65f, 10.2f, kGo, 20u) * bM);
                }
            }
            card = mix(card, sBg, sM);
        }

        // Card B: Tactical Legend Panel (y: 290..690)
        float dLeg = sdRoundedBoxHUD(sRel - float2(176.0f, 490.0f), float2(176.0f, 200.0f), 8.0f);
        if (dLeg <= 1.5f) {
            float lM = smoothstep(1.0f, -0.6f, dLeg);
            float3 lBg = float3(0.032f, 0.040f, 0.056f);
            lBg = mix(lBg, float3(0.32f, 0.38f, 0.48f), smoothstep(1.5f, 0.3f, abs(dLeg)) * 0.65f);

            const uint kHdr[10] = { 77u,65u,80u,32u,76u,69u,71u,69u,78u,68u }; // MAP LEGEND
            lBg = mix(lBg, float3(1.00f, 0.74f, 0.16f), evalLiteral7(sRel, float2(18.0f, 312.0f), 1.75f, 10.8f, kHdr, 10u));

            // 5 Legend Rows (y: 364, 418, 472, 526, 580)
            const float3 rowCols[5] = {
                float3(1.00f, 0.20f, 0.12f), // Blacklist Boss
                float3(0.10f, 0.88f, 1.00f), // Speedtrap
                float3(1.00f, 0.74f, 0.14f), // Sprint
                float3(0.92f, 0.95f, 1.00f), // Landmark
                float3(0.32f, 1.00f, 0.56f)  // Player
            };
            for (int r = 0; r < 5; ++r) {
                float ry = 368.0f + float(r) * 52.0f;
                float dDot = length(sRel - float2(30.0f, ry)) - 7.5f;
                lBg = mix(lBg, rowCols[r], smoothstep(0.8f, -0.8f, dDot));
            }
            const uint kL0[17] = { 66u,76u,65u,67u,75u,76u,73u,83u,84u,32u,49u,53u,32u,66u,79u,83u,83u }; // BLACKLIST 15 BOSS
            const uint kL1[15] = { 83u,80u,69u,69u,68u,84u,82u,65u,80u,32u,82u,65u,68u,65u,82u };       // SPEEDTRAP RADAR
            const uint kL2[17] = { 67u,72u,69u,67u,75u,80u,79u,73u,78u,84u,32u,83u,80u,82u,73u,78u,84u }; // CHECKPOINT SPRINT
            const uint kL3[17] = { 68u,73u,83u,84u,82u,73u,67u,84u,32u,76u,65u,78u,68u,77u,65u,82u,75u }; // DISTRICT LANDMARK
            const uint kL4[15] = { 80u,76u,65u,89u,69u,82u,32u,80u,79u,83u,73u,84u,73u,79u,78u };         // PLAYER POSITION
            lBg = mix(lBg, float3(0.90f, 0.93f, 0.98f), evalLiteral7(sRel, float2(52.0f, 362.0f), 1.55f, 9.6f, kL0, 17u));
            lBg = mix(lBg, float3(0.90f, 0.93f, 0.98f), evalLiteral7(sRel, float2(52.0f, 414.0f), 1.55f, 9.6f, kL1, 15u));
            lBg = mix(lBg, float3(0.90f, 0.93f, 0.98f), evalLiteral7(sRel, float2(52.0f, 466.0f), 1.55f, 9.6f, kL2, 17u));
            lBg = mix(lBg, float3(0.90f, 0.93f, 0.98f), evalLiteral7(sRel, float2(52.0f, 518.0f), 1.55f, 9.6f, kL3, 17u));
            lBg = mix(lBg, float3(0.90f, 0.93f, 0.98f), evalLiteral7(sRel, float2(52.0f, 570.0f), 1.55f, 9.6f, kL4, 15u));
            card = mix(card, lBg, lM);
        }
    }

    // Bottom Control Hint Bar (y: 772..814)
    if (fRel.y >= 770.0f) {
        const uint kF1[10] = { 91u,87u,65u,83u,68u,93u,32u,80u,65u,78u };                                     // [WASD] PAN
        const uint kF2[10] = { 91u,43u,47u,45u,93u,32u,90u,79u,79u,77u };                                     // [+/-] ZOOM
        const uint kF3[10] = { 91u,84u,65u,66u,93u,32u,78u,69u,88u,84u };                                     // [TAB] NEXT
        const uint kF4[10] = { 91u,69u,78u,84u,69u,82u,93u,32u,71u,79u };                                     // [ENTER] GO
        const uint kF5[13] = { 91u,77u,47u,69u,83u,67u,93u,32u,67u,76u,79u,83u,69u };                         // [M/ESC] CLOSE
        float3 hintC = float3(0.76f, 0.84f, 0.94f);
        card = mix(card, hintC, evalLiteral7(fRel, float2(140.0f, 784.0f), 1.65f, 10.2f, kF1, 10u));
        card = mix(card, hintC, evalLiteral7(fRel, float2(330.0f, 784.0f), 1.65f, 10.2f, kF2, 10u));
        card = mix(card, hintC, evalLiteral7(fRel, float2(530.0f, 784.0f), 1.65f, 10.2f, kF3, 10u));
        card = mix(card, float3(1.00f, 0.78f, 0.22f), evalLiteral7(fRel, float2(730.0f, 784.0f), 1.65f, 10.2f, kF4, 10u));
        card = mix(card, hintC, evalLiteral7(fRel, float2(960.0f, 784.0f), 1.65f, 10.2f, kF5, 13u));
    }

    return mix(outCol, card, frameMask);
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
// unwarped NFSMW 2005 SDF HUD + GPU Minimap + sliding EA TRAX Banner + Race HUD + O(1) World Map.
kernel void hudOverlayKernel(
    uint2 gid [[thread_position_in_grid]],
    texture2d<float, access::read> inLdrTex [[texture(0)]],
    texture2d<float, access::write> outTex [[texture(1)]],
    texture2d<float, access::sample> worldMapTex [[texture(2)]],
    sampler linearSamp [[sampler(0)]],
    constant RTFrameUniforms& u [[buffer(0)]],
    constant uint& encodeSRGB [[buffer(1)]],
    constant RTHudExtUniforms& ext [[buffer(2)]],
    constant RTMinimapBuffer& minimap [[buffer(3)]],
    constant RTMusicBannerUniforms& music [[buffer(4)]],
    constant RTEventHudUniforms& evHud [[buffer(5)]]
) {
    uint w = outTex.get_width();
    uint h = outTex.get_height();
    if (gid.x >= w || gid.y >= h) return;

    float3 srgb = inLdrTex.read(gid).rgb;
    float2 px = float2(gid) + 0.5f;
    float2 res = float2(float(w), float(h));
    bool mapOpen = (evHud.flags & EVENT_HUD_FLAG_WORLD_MAP) != 0u;

    if (mapOpen) {
        srgb = compositeWorldMapOverlay(srgb, px, res, ext, evHud, worldMapTex, linearSamp);
    } else {
        bool hudVisible = (u.hudParams.x > 0.5f) || ((ext.hudFlags & HUD_FLAG_VISIBLE) != 0u);
        if (hudVisible) {
            srgb = compositeDigitalDrivingHUD(srgb, px, res, u, ext, minimap);
        }
        if ((evHud.flags & EVENT_HUD_FLAG_ACTIVE) != 0u) {
            srgb = compositeEventHudStrip(srgb, px, res, ext, evHud);
        }
        if (music.bannerAlpha > 0.001f) {
            srgb = compositeEATraxBanner(srgb, px, res, ext, music);
        }
    }

    float3 outRGB = (encodeSRGB != 0u) ? srgb : srgbToLinear(srgb);
    outTex.write(float4(outRGB, 1.0f), gid);
}
