# NFSMW: Rockport 2026 — Adversarial Multi-Agent Video QA Rubric & Playbook

> **Owner**: Tech Lead — Release Engineering & Independent Adversarial QA Lead  
> **Scope**: Independent visual, structural, lighting, and telemetry QA for live PCSX2 v2.6.3 (Metal 4K) full-session MP4 captures and extracted frame contact sheets.  
> **Golden Rule**: **ZERO rubber-stamping.** Every build must survive 4 independent specialist reviewers before receiving a `PASS` release gate.

---

## 1. Pre-Review Media Safety Protocol (Mandatory)

Before ANY reviewer inspects a screenshot, montage, contact sheet, or video frame:
1. Validate every PNG/JPG via Pillow (`Image.open(p).load()`) — quarantine any truncated or corrupt image by renaming to `.corrupt`.
2. Validate MP4 container integrity via `ffprobe` (duration > 0, video stream present, non-zero frame count, expected aspect ratio `16:9` or `21:9`).
3. Extract a deterministic **timeline contact sheet** (e.g., 1 frame every 2–3 seconds across boot -> main menu -> loading -> safehouse/spawn -> high-speed drive -> cornering -> reverse -> NOS/sprint -> pursuit/menu) plus **1:1 full-resolution crops** of:
   - Main menu hero car (BMW M3 GTR) & UI icons
   - In-race HUD (minimap, tachometer/gear, heat bar, EA TRAX banner)
   - Player car rear/flank (liveries, taillights, wheel rims, glass reflections)
   - Road surface, buildings/signage, and sky/horizon

---

## 2. Independent 4-Reviewer Multi-Agent Panel

When a full-session MP4 + contact sheets arrive, spawn **4 independent specialist QA subagents** in parallel with strictly scoped prompts (each reviewer inspects ONLY the validated visual artifacts + manifest/telemetry JSON, never edits code, and reports explicit defect IDs with frame timestamps):

| Reviewer | Role | Primary Defect Hunting Focus |
|---|---|---|
| **QA-1** | **UI / Icons / Logos / HUD Specialist** | Main menu logo (`MW_LOGO`), Career/Quick Race icons, loading-screen icons/grit, font clarity, EA TRAX overlay, minimap road tiles & player arrow, tachometer/speedo/gear (`1–6`, `R`), NOS/Speedbreaker bars, heat meter, 16:9 aspect scaling (no stretched circles or clipped text). |
| **QA-2** | **3D Geometry / Car Liveries / Wheels Specialist** | PN-subdivided high-poly car body continuity (zero missing triangles, zero inverted normals, zero VU1 strip spikes/explosions, zero Z-fighting), intact car liveries (`BMWM3GTRE46` blue/silver, Golf GTI decals/plate, traffic cars), intact taillights/headlights (NEVER brick/carbon!), round alloy wheels & brake discs, crisp 5-face car reflections, clean glass/windshield. |
| **QA-3** | **World Lighting / Sky / Textures Specialist** | Zero 2005 olive/yellow-green tint (`R/B` neutral `0.88–1.05` in GT7 Daylight; warm luminous gold without muddy brown crush in Forza Autumn), clean blue sky & clouds, lifted midtones/shadows (no crushed blacks), authentic Rockport signage & storefronts (NEVER overwritten by generic brick/carbon!), crisp asphalt/curb/brick detail, zero flickering/z-fighting or broken texture seams, stable 60Hz ScreenEFX. |
| **QA-4** | **Driving / Reverse / Telemetry & Motion Specialist** | Smooth open-road driving verified against PINE memory telemetry (real forward speed progression `> 80 MPH`, steering responsiveness, cornering without repeated wall collisions, deliberate reverse maneuver showing `R` gear & negative/reverse motion, clean recovery & NOS boost, zero crashes/freezes/audio-sync stalls, stable frame pacing across the MP4). |

---

## 3. Severity Tiers & Release Gate Criteria

| Severity | Definition | Release Gate Impact |
|---|---|---|
| **P0 — Blocker** | Crash, freeze, black screen, VU1 mesh explosion/missing body panels, broken/missing menu or loading icons, corrupted fonts/minimap tiles, car taillights/liveries overwritten by world textures, severe 2005 yellow-green regression, or car permanently pinned against a wall for the entire driving segment. | **AUTOMATIC FAIL** — blocks release immediately; must be fixed and re-captured. |
| **P1 — Major** | Noticeable world signage/building overwritten by wrong material, camera wedged/inverted during normal driving, sustained low-speed wall scraping (> 30% of drive), washed-out bloom or crushed shadows (`mean_luma < 40` in daylight), or aspect-ratio distortion. | **FAIL** unless explicitly waived by CEO with tracked follow-up issue. |
| **P2 — Minor** | Subtle mip shimmer, minor texture seam on distant prop, brief curb scrape with immediate clean recovery, slight compression banding in dark corners. | **CONDITIONAL PASS** — log for next flywheel wave. |

---

## 4. Quantitative Scorecard (0–10 per Axis, Target >= 8.5/10 GT7/FH6 Bar)

Each reviewer assigns an objective score (0–10) backed by timestamped frame citations:

1. **UI & HUD Integrity (QA-1)**:
   - `10`: Every logo, font glyph, loading icon, minimap road tile, and gauge is razor-sharp and 100% authentic.
   - `<= 5`: Any single replaced/corrupted UI icon, font, or minimap tile (**auto P0**).
2. **3D Geometry, Reflections & Liveries (QA-2)**:
   - `10`: Smooth PN-subdivided curves, zero spikes/holes, glossy clearcoat reflections, intact taillights/wheels/liveries across player & traffic cars.
   - `<= 5`: Any mesh artifact, missing panel, or overwritten car/taillight texture (**auto P0**).
3. **Lighting, Sky & World Texture Fidelity (QA-3)**:
   - `10`: Crisp GT7 daylight / Forza autumn palette (`R/B` balanced, vibrant sky & autumn foliage, clean lifted shadows, crisp 2K/4K roads & preserved Rockport signage).
   - `<= 6`: Residual olive/yellow haze, dark crushed midtones, or generic tile swaps overwriting iconic storefronts/signs.
4. **Driving, Reverse & Telemetry Fluency (QA-4)**:
   - `10`: Sustained high-speed boulevard driving, clean steering around corners, verified `R` reverse segment + clean forward recovery, smooth 60 FPS motion.
   - `<= 5`: Car stuck on wall/curb or zero real boulevard progression (**auto P0**).

---

## 5. Texture Replacement Deployment Safety Checklist (Pre-Capture Gate)

Before mounting ANY `replacements/` pack into PCSX2:
1. Run `python3 tools/enhance_world_dumps.py ...` (detail-preserving 4× world texture enhancer preserving exact PS2 GS `0x80` alpha) and audit the manifest:
   - `hud_leaks == 0` (zero `hud*` categories under `--world-only`)
   - `small_leaks == 0` (zero `< 64px` textures replaced by world materials)
   - `alpha_leaks == 0` (zero alpha-cutout textures overwritten by opaque world tiles)
   - `livery_leaks == 0` (zero `paint`, zero unverified structural `carbon`/`tire`, zero non-tileable UV sheets/taillights/minimap tiles/signs)
2. Visually inspect the generated `audit_sheet.png` contact sheet of ALL source dumps scheduled for replacement before launching PCSX2.

---

## 6. Consolidated Tech-Lead Report Format (Delivered to CEO)

```markdown
## Video QA Verdict: [PASS / CONDITIONAL PASS / FAIL] — <iter_name>
- **Video**: `<path>.mp4` (<duration>s, <resolution>, <fps> fps)
- **Scores**: UI/HUD `X/10` | Geometry/Liveries `X/10` | Lighting/World `X/10` | Driving/Telemetry `X/10` | **Overall `X.X/10`**
- **P0 Blockers (<count>)**:
  - `[P0-1]` (<timestamp> / <frame>): <exact defect + root cause + owner>
- **P1 Majors (<count>)**:
  - `[P1-1]` (<timestamp> / <frame>): <exact defect + recommended fix>
- **P2 Minors (<count>)**:
  - ...
- **What Improved vs Previous Iteration**:
  - ...
- **Recommended Next Action**:
  - <exact engineering action before next capture>
```
