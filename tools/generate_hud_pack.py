#!/usr/bin/env python3
"""generate_hud_pack.py — 100% Original / CC0 4K Modern Racing HUD Kit Generator.

Generates a cohesive, coaxially-aligned suite of transparent Power-of-Two RGBA PNG
HUD textures for Need for Speed: Most Wanted — Rockport 2026 (SLUS-21351), using
2x supersampled vector rasterization (4096->2048 Lanczos downsample) with Pillow.

Supported themes:
  - motorsport2026 : Forza Motorsport / GT7 ultra-clean smoked obsidian & titanium style
  - rockport_neon  : Modernized MW2005 homage with Rockport amber-gold & electric cyan

Generated assets per theme:
  - hud_tach_dial.png          (2048x2048 default, 2x supersampled)
  - hud_tach_needle.png        (2048x2048 default, centered coaxial pivot at 12 o'clock)
  - hud_nos_arc.png            (2048x2048 default, coaxial outer-left nitrous boost arc)
  - hud_speedbreaker_arc.png   (2048x2048 default, coaxial outer-right speedbreaker arc)
  - hud_speed_plate.png        (1024x512  default, chamfered digital speed/gear backing)
  - hud_heat_1.png .. 5.png    (512x512   default, escalating Police Heat Level badges)
  - hud_minimap_frame.png      (1024x1024 default, circular radar bezel with transparent cutout)
  - hud_player_arrow.png       (512x512   default, faceted directional GPS player arrow)
  - hud_contact_sheet.png      (dark-backdrop visual showcase & coaxial assembly preview)

Dependencies: Pillow only (100% original procedural artwork — zero copyrighted assets).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

RGBA = Tuple[int, int, int, int]


@dataclasses.dataclass(frozen=True)
class HUDTheme:
    name: str
    title: str
    subtitle: str
    face_dark: RGBA
    face_mid: RGBA
    bezel_outer: RGBA
    bezel_inner: RGBA
    bezel_highlight: RGBA
    tick_major: RGBA
    tick_minor: RGBA
    numeral: RGBA
    redline: RGBA
    redline_glow: RGBA
    needle_core: RGBA
    needle_edge: RGBA
    needle_glow: RGBA
    nos_start: RGBA
    nos_end: RGBA
    nos_glow: RGBA
    sb_start: RGBA
    sb_end: RGBA
    sb_glow: RGBA
    plate_bg: RGBA
    plate_border: RGBA
    plate_accent: RGBA
    minimap_ring: RGBA
    minimap_accent: RGBA
    arrow_light: RGBA
    arrow_shade: RGBA
    arrow_glow: RGBA
    card_bg: RGBA


THEMES: Dict[str, HUDTheme] = {
    "motorsport2026": HUDTheme(
        name="motorsport2026",
        title="MOTORSPORT 2026",
        subtitle="Forza / GT7 Clean Smoked-Glass Telemetry Suite",
        face_dark=(9, 13, 20, 232),
        face_mid=(18, 25, 36, 215),
        bezel_outer=(148, 166, 190, 230),
        bezel_inner=(56, 70, 90, 220),
        bezel_highlight=(226, 236, 248, 240),
        tick_major=(238, 245, 255, 240),
        tick_minor=(142, 160, 184, 155),
        numeral=(244, 249, 255, 255),
        redline=(255, 48, 78, 255),
        redline_glow=(255, 36, 72, 175),
        needle_core=(255, 255, 255, 255),
        needle_edge=(255, 58, 86, 255),
        needle_glow=(255, 52, 82, 185),
        nos_start=(0, 156, 255, 255),
        nos_end=(40, 236, 255, 255),
        nos_glow=(0, 208, 255, 165),
        sb_start=(235, 140, 22, 255),
        sb_end=(255, 214, 72, 255),
        sb_glow=(255, 178, 42, 160),
        plate_bg=(11, 16, 25, 232),
        plate_border=(112, 132, 160, 215),
        plate_accent=(0, 214, 255, 255),
        minimap_ring=(26, 34, 48, 242),
        minimap_accent=(0, 214, 255, 255),
        arrow_light=(250, 253, 255, 255),
        arrow_shade=(174, 224, 255, 255),
        arrow_glow=(0, 214, 255, 195),
        card_bg=(14, 19, 28, 255),
    ),
    "rockport_neon": HUDTheme(
        name="rockport_neon",
        title="ROCKPORT NEON 2026",
        subtitle="Modernized Most Wanted Amber-Gold & Electric Cyan Homage",
        face_dark=(12, 11, 16, 238),
        face_mid=(26, 21, 22, 220),
        bezel_outer=(245, 168, 44, 235),
        bezel_inner=(92, 62, 34, 225),
        bezel_highlight=(255, 218, 128, 245),
        tick_major=(255, 238, 204, 245),
        tick_minor=(188, 148, 96, 170),
        numeral=(255, 246, 224, 255),
        redline=(255, 52, 42, 255),
        redline_glow=(255, 44, 24, 195),
        needle_core=(255, 248, 230, 255),
        needle_edge=(255, 140, 20, 255),
        needle_glow=(255, 122, 14, 210),
        nos_start=(0, 178, 255, 255),
        nos_end=(76, 255, 242, 255),
        nos_glow=(0, 232, 255, 190),
        sb_start=(255, 96, 18, 255),
        sb_end=(255, 206, 46, 255),
        sb_glow=(255, 152, 22, 190),
        plate_bg=(16, 14, 20, 238),
        plate_border=(220, 146, 46, 225),
        plate_accent=(255, 174, 34, 255),
        minimap_ring=(22, 19, 26, 246),
        minimap_accent=(255, 176, 36, 255),
        arrow_light=(255, 244, 208, 255),
        arrow_shade=(255, 168, 38, 255),
        arrow_glow=(255, 158, 28, 210),
        card_bg=(18, 15, 22, 255),
    ),
}

HEAT_TIER_STYLES: Dict[int, Dict[str, Any]] = {
    1: {"label": "PATROL", "primary": (56, 189, 248, 255), "secondary": (18, 96, 168, 255), "glow": (56, 189, 248, 170), "wings": 0},
    2: {"label": "PURSUIT", "primary": (250, 204, 21, 255), "secondary": (176, 122, 12, 255), "glow": (250, 204, 21, 175), "wings": 0},
    3: {"label": "STATE", "primary": (251, 146, 60, 255), "secondary": (184, 72, 18, 255), "glow": (251, 132, 42, 185), "wings": 1},
    4: {"label": "HEAVY", "primary": (244, 63, 94, 255), "secondary": (158, 22, 48, 255), "glow": (244, 48, 86, 195), "wings": 2},
    5: {"label": "FEDERAL", "primary": (255, 34, 66, 255), "secondary": (235, 246, 255, 255), "glow": (255, 30, 62, 220), "wings": 3},
}


def load_font(pixel_size: int, style: str = "bold") -> ImageFont.ImageFont:
    """Load clean motorsport geometric sans-serif fonts on macOS/Linux with safe fallback."""
    bold_candidates = [
        ("/System/Library/Fonts/Supplemental/DIN Alternate Bold.ttf", 0),
        ("/System/Library/Fonts/Avenir Next.ttc", 0),
        ("/System/Library/Fonts/HelveticaNeue.ttc", 1),
        ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
    ]
    regular_candidates = [
        ("/System/Library/Fonts/Avenir Next.ttc", 2),
        ("/System/Library/Fonts/HelveticaNeue.ttc", 0),
        ("/System/Library/Fonts/Supplemental/DIN Alternate Bold.ttf", 0),
        ("/System/Library/Fonts/Menlo.ttc", 0),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
    ]
    candidates = bold_candidates if style == "bold" else regular_candidates
    px = max(8, int(round(pixel_size)))
    for path, idx in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=px, index=idx)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=px)
    except TypeError:
        return ImageFont.load_default()


def polar(cx: float, cy: float, radius: float, deg: float) -> Tuple[float, float]:
    rad = math.radians(deg)
    return (cx + radius * math.cos(rad), cy + radius * math.sin(rad))


def annular_wedge(
    cx: float, cy: float, r_in: float, r_out: float, a0: float, a1: float, steps: int = 24
) -> List[Tuple[float, float]]:
    pts: List[Tuple[float, float]] = []
    n = max(2, steps)
    for i in range(n + 1):
        ang = a0 + (a1 - a0) * (i / float(n))
        pts.append(polar(cx, cy, r_out, ang))
    for i in range(n, -1, -1):
        ang = a0 + (a1 - a0) * (i / float(n))
        pts.append(polar(cx, cy, r_in, ang))
    return pts


def lerp_rgba(c0: RGBA, c1: RGBA, t: float) -> RGBA:
    u = max(0.0, min(1.0, float(t)))
    return (
        int(round(c0[0] + (c1[0] - c0[0]) * u)),
        int(round(c0[1] + (c1[1] - c0[1]) * u)),
        int(round(c0[2] + (c1[2] - c0[2]) * u)),
        int(round(c0[3] + (c1[3] - c0[3]) * u)),
    )


def chamfer_rect(x0: float, y0: float, x1: float, y1: float, ch: float) -> List[Tuple[float, float]]:
    return [
        (x0 + ch, y0),
        (x1 - ch, y0),
        (x1, y0 + ch),
        (x1, y1 - ch),
        (x1 - ch, y1),
        (x0 + ch, y1),
        (x0, y1 - ch),
        (x0, y0 + ch),
    ]


def _finish_supersampled(
    base: Image.Image, glow: Optional[Image.Image], target_w: int, target_h: int, glow_radius: float
) -> Image.Image:
    if glow is not None and glow_radius > 0.0:
        blurred = glow.filter(ImageFilter.GaussianBlur(radius=glow_radius))
        composed = Image.alpha_composite(blurred, base)
    else:
        composed = base
    if composed.size != (target_w, target_h):
        return composed.resize((target_w, target_h), Image.Resampling.LANCZOS)
    return composed


def render_tach_dial(theme: HUDTheme, size: int = 2048, supersample: int = 2) -> Image.Image:
    ss = max(1, min(int(supersample), 3))
    s = size * ss
    cx = cy = s * 0.5

    base = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(base, "RGBA")
    gdraw = ImageDraw.Draw(glow, "RGBA")

    r_outer = s * 0.375
    r_inner_face = s * 0.358
    draw.ellipse((cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer), fill=(5, 7, 11, 215))

    ring_steps = 22
    for i in range(ring_steps):
        t = i / float(ring_steps - 1)
        r = r_inner_face * (1.0 - 0.85 * t)
        col = lerp_rgba(theme.face_dark, theme.face_mid, math.sin(t * math.pi) * 0.72)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col)

    for frac in (0.340, 0.296, 0.220, 0.145):
        rr = s * frac
        draw.ellipse(
            (cx - rr, cy - rr, cx + rr, cy + rr),
            outline=(theme.bezel_inner[0], theme.bezel_inner[1], theme.bezel_inner[2], 70),
            width=max(1, int(round(s * 0.0012))),
        )

    draw.ellipse(
        (cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer),
        outline=theme.bezel_outer,
        width=max(2, int(round(s * 0.0042))),
    )
    r_bezel2 = s * 0.365
    draw.ellipse(
        (cx - r_bezel2, cy - r_bezel2, cx + r_bezel2, cy + r_bezel2),
        outline=theme.bezel_inner,
        width=max(2, int(round(s * 0.0022))),
    )
    draw.arc(
        (cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer),
        start=210,
        end=330,
        fill=theme.bezel_highlight,
        width=max(2, int(round(s * 0.0028))),
    )

    start_deg = 138.0
    sweep_deg = 264.0
    max_rpm = 9000.0
    redline = 7500.0

    track_poly = annular_wedge(cx, cy, s * 0.306, s * 0.348, start_deg, start_deg + sweep_deg, steps=96)
    draw.polygon(track_poly, fill=(14, 18, 26, 200))

    red_a0 = start_deg + (redline / max_rpm) * sweep_deg
    red_a1 = start_deg + sweep_deg
    red_band = annular_wedge(cx, cy, s * 0.312, s * 0.346, red_a0, red_a1, steps=28)
    draw.polygon(red_band, fill=(theme.redline[0], theme.redline[1], theme.redline[2], 78))
    red_rim = annular_wedge(cx, cy, s * 0.340, s * 0.348, red_a0, red_a1, steps=28)
    draw.polygon(red_rim, fill=theme.redline)
    gdraw.polygon(red_rim, fill=theme.redline_glow)

    n_segs = 54
    seg_step = sweep_deg / float(n_segs)
    for i in range(n_segs):
        a0 = start_deg + i * seg_step + 0.55
        a1 = start_deg + (i + 1) * seg_step - 0.55
        rpm_mid = ((i + 0.5) / float(n_segs)) * max_rpm
        seg_poly = annular_wedge(cx, cy, s * 0.326, s * 0.338, a0, a1, steps=3)
        if rpm_mid >= redline:
            draw.polygon(seg_poly, fill=(theme.redline[0], theme.redline[1], theme.redline[2], 195))
        else:
            draw.polygon(seg_poly, fill=(theme.bezel_inner[0], theme.bezel_inner[1], theme.bezel_inner[2], 125))

    num_major = 9
    sub_per_major = 4
    total_ticks = num_major * sub_per_major
    for idx in range(total_ticks + 1):
        rpm_val = (idx / float(total_ticks)) * max_rpm
        ang = start_deg + (idx / float(total_ticks)) * sweep_deg
        is_major = (idx % sub_per_major) == 0
        is_half = (idx % 2) == 0 and not is_major
        is_red = rpm_val >= redline

        r0 = s * (0.286 if is_major else (0.298 if is_half else 0.306))
        r1 = s * 0.324
        w_px = max(2, int(round(s * (0.0046 if is_major else (0.0026 if is_half else 0.0016)))))
        col = theme.redline if is_red else (theme.tick_major if is_major else theme.tick_minor)
        draw.line([polar(cx, cy, r0, ang), polar(cx, cy, r1, ang)], fill=col, width=w_px)
        if is_major and is_red:
            gdraw.line([polar(cx, cy, r0, ang), polar(cx, cy, r1, ang)], fill=theme.redline_glow, width=w_px * 2)

    font_num = load_font(int(round(s * 0.036)), style="bold")
    r_num = s * 0.252
    for digit in range(num_major + 1):
        ang = start_deg + (digit / float(num_major)) * sweep_deg
        lx, ly = polar(cx, cy, r_num, ang)
        col = theme.redline if (digit * 1000.0 >= redline) else theme.numeral
        draw.text((lx, ly + s * 0.002), str(digit), fill=(0, 0, 0, 150), font=font_num, anchor="mm")
        draw.text((lx, ly), str(digit), fill=col, font=font_num, anchor="mm")

    shift_leds = 7
    led_span = 26.0
    for idx in range(shift_leds):
        frac = idx / float(shift_leds - 1)
        ang = 270.0 - led_span * 0.5 + frac * led_span
        led_poly = annular_wedge(cx, cy, s * 0.382, s * 0.394, ang - 1.35, ang + 1.35, steps=3)
        if idx < 3:
            col = (0, 226, 148, 220)
        elif idx < 5:
            col = (255, 192, 36, 230)
        else:
            col = theme.redline
        draw.polygon(led_poly, fill=(col[0], col[1], col[2], 145), outline=(235, 244, 255, 120))
        gdraw.polygon(led_poly, fill=(col[0], col[1], col[2], 110))

    font_cap = load_font(int(round(s * 0.0155)), style="regular")
    draw.text((cx, cy - s * 0.122), "RPM ×1000", fill=theme.tick_minor, font=font_cap, anchor="mm")

    r_hub = s * 0.044
    draw.ellipse(
        (cx - r_hub, cy - r_hub, cx + r_hub, cy + r_hub),
        fill=(14, 18, 26, 225),
        outline=theme.bezel_outer,
        width=max(2, int(round(s * 0.0022))),
    )

    return _finish_supersampled(base, glow, size, size, glow_radius=s * 0.0075)


def render_tach_needle(theme: HUDTheme, size: int = 2048, supersample: int = 2) -> Image.Image:
    """Coaxial transparent tachometer needle centered at (cx, cy), pointing straight up (-90 deg)."""
    ss = max(1, min(int(supersample), 3))
    s = size * ss
    cx = cy = s * 0.5

    base = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(base, "RGBA")
    gdraw = ImageDraw.Draw(glow, "RGBA")

    tip_y = cy - s * 0.344
    tail_y = cy + s * 0.088
    w_base = s * 0.0145
    w_tip = s * 0.0028
    w_tail = s * 0.0105

    sh = s * 0.006
    shadow_poly = [
        (cx - w_tip + sh, tip_y + sh),
        (cx + w_tip + sh, tip_y + sh),
        (cx + w_base + sh, cy + sh),
        (cx + w_tail + sh, tail_y + sh),
        (cx - w_tail + sh, tail_y + sh),
        (cx - w_base + sh, cy + sh),
    ]
    draw.polygon(shadow_poly, fill=(0, 0, 0, 125))

    outer_poly = [
        (cx - w_tip * 1.4, tip_y),
        (cx + w_tip * 1.4, tip_y),
        (cx + w_base, cy),
        (cx + w_tail, tail_y),
        (cx - w_tail, tail_y),
        (cx - w_base, cy),
    ]
    draw.polygon(outer_poly, fill=theme.needle_edge)
    gdraw.polygon(outer_poly, fill=theme.needle_glow)

    inner_poly = [
        (cx - w_tip * 0.65, tip_y + s * 0.008),
        (cx + w_tip * 0.65, tip_y + s * 0.008),
        (cx + w_base * 0.42, cy - s * 0.012),
        (cx - w_base * 0.42, cy - s * 0.012),
    ]
    draw.polygon(inner_poly, fill=theme.needle_core)

    tail_poly = [
        (cx - w_base * 0.85, cy + s * 0.018),
        (cx + w_base * 0.85, cy + s * 0.018),
        (cx + w_tail * 0.92, tail_y - s * 0.004),
        (cx - w_tail * 0.92, tail_y - s * 0.004),
    ]
    draw.polygon(tail_poly, fill=(20, 26, 36, 245))

    r_cap_out = s * 0.042
    r_cap_mid = s * 0.032
    r_cap_in = s * 0.017
    draw.ellipse(
        (cx - r_cap_out, cy - r_cap_out, cx + r_cap_out, cy + r_cap_out),
        fill=(24, 30, 42, 255),
        outline=theme.bezel_highlight,
        width=max(2, int(round(s * 0.0032))),
    )
    draw.ellipse(
        (cx - r_cap_mid, cy - r_cap_mid, cx + r_cap_mid, cy + r_cap_mid),
        fill=(42, 52, 68, 255),
        outline=theme.needle_edge,
        width=max(2, int(round(s * 0.0026))),
    )
    draw.ellipse(
        (cx - r_cap_in, cy - r_cap_in, cx + r_cap_in, cy + r_cap_in),
        fill=(14, 18, 26, 255),
        outline=(190, 208, 232, 200),
        width=max(1, int(round(s * 0.0018))),
    )

    return _finish_supersampled(base, glow, size, size, glow_radius=s * 0.0085)


def _render_side_boost_arc(
    theme: HUDTheme,
    size: int,
    supersample: int,
    start_deg: float,
    end_deg: float,
    col_start: RGBA,
    col_end: RGBA,
    col_glow: RGBA,
    label: str,
    fill_ratio: float = 0.84,
) -> Image.Image:
    ss = max(1, min(int(supersample), 3))
    s = size * ss
    cx = cy = s * 0.5

    base = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(base, "RGBA")
    gdraw = ImageDraw.Draw(glow, "RGBA")

    r_in = s * 0.402
    r_out = s * 0.442
    r_rail_in = s * 0.394
    r_rail_out = s * 0.450

    a_min = min(start_deg, end_deg)
    a_max = max(start_deg, end_deg)

    housing = annular_wedge(cx, cy, r_rail_in, r_rail_out, a_min - 2.0, a_max + 2.0, steps=48)
    draw.polygon(housing, fill=(10, 14, 22, 212), outline=theme.bezel_inner)

    rail = annular_wedge(cx, cy, r_rail_out - s * 0.0032, r_rail_out, a_min - 1.5, a_max + 1.5, steps=48)
    draw.polygon(rail, fill=(col_end[0], col_end[1], col_end[2], 190))

    n_segs = 18
    span = end_deg - start_deg
    step = span / float(n_segs)
    gap = 0.55 if span > 0 else -0.55

    for i in range(n_segs):
        frac = (i + 0.5) / float(n_segs)
        sa0 = start_deg + i * step + gap
        sa1 = start_deg + (i + 1) * step - gap
        seg = annular_wedge(cx, cy, r_in, r_out, min(sa0, sa1), max(sa0, sa1), steps=4)
        if frac <= fill_ratio:
            seg_col = lerp_rgba(col_start, col_end, frac)
            draw.polygon(seg, fill=seg_col)
            hi_seg = annular_wedge(
                cx, cy, r_out - s * 0.008, r_out - s * 0.002, min(sa0, sa1), max(sa0, sa1), steps=4
            )
            draw.polygon(hi_seg, fill=(255, 255, 255, 115))
            gdraw.polygon(seg, fill=col_glow)
        else:
            draw.polygon(seg, fill=(28, 36, 48, 175))

    pill_ang = start_deg - (7.0 if span > 0 else -7.0)
    px, py = polar(cx, cy, (r_in + r_out) * 0.5, pill_ang)
    pw, ph = s * 0.068, s * 0.032
    pill = chamfer_rect(px - pw * 0.5, py - ph * 0.5, px + pw * 0.5, py + ph * 0.5, s * 0.007)
    draw.polygon(pill, fill=(10, 14, 22, 240), outline=col_end)
    font_lbl = load_font(int(round(s * 0.0185)), style="bold")
    draw.text((px, py), label, fill=col_end, font=font_lbl, anchor="mm")

    return _finish_supersampled(base, glow, size, size, glow_radius=s * 0.009)


def render_nos_arc(theme: HUDTheme, size: int = 2048, supersample: int = 2) -> Image.Image:
    return _render_side_boost_arc(
        theme=theme,
        size=size,
        supersample=supersample,
        start_deg=144.0,
        end_deg=226.0,
        col_start=theme.nos_start,
        col_end=theme.nos_end,
        col_glow=theme.nos_glow,
        label="N2O",
        fill_ratio=0.86,
    )


def render_speedbreaker_arc(theme: HUDTheme, size: int = 2048, supersample: int = 2) -> Image.Image:
    return _render_side_boost_arc(
        theme=theme,
        size=size,
        supersample=supersample,
        start_deg=36.0,
        end_deg=-46.0,
        col_start=theme.sb_start,
        col_end=theme.sb_end,
        col_glow=theme.sb_glow,
        label="SB",
        fill_ratio=0.80,
    )


def render_speed_plate(
    theme: HUDTheme, width: int = 1024, height: int = 512, supersample: int = 2
) -> Image.Image:
    ss = max(1, min(int(supersample), 3))
    w, h = width * ss, height * ss

    base = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(base, "RGBA")
    gdraw = ImageDraw.Draw(glow, "RGBA")

    pad_x = w * 0.045
    pad_y = h * 0.10
    ch = h * 0.14

    sh = h * 0.025
    shadow_poly = chamfer_rect(pad_x + sh, pad_y + sh, w - pad_x + sh, h - pad_y + sh, ch)
    draw.polygon(shadow_poly, fill=(0, 0, 0, 125))

    outer_poly = chamfer_rect(pad_x, pad_y, w - pad_x, h - pad_y, ch)
    draw.polygon(outer_poly, fill=theme.plate_bg, outline=theme.plate_border)

    inset = h * 0.036
    inner_poly = chamfer_rect(
        pad_x + inset, pad_y + inset, w - pad_x - inset, h - pad_y - inset, ch * 0.72
    )
    draw.polygon(
        inner_poly,
        fill=(theme.face_mid[0], theme.face_mid[1], theme.face_mid[2], 150),
        outline=(theme.plate_border[0], theme.plate_border[1], theme.plate_border[2], 110),
    )

    bar_w = max(3, int(round(h * 0.022)))
    bar_y = pad_y + bar_w * 0.5
    draw.line([(pad_x + ch, bar_y), (w - pad_x - ch, bar_y)], fill=theme.plate_accent, width=bar_w)
    gdraw.line([(pad_x + ch, bar_y), (w - pad_x - ch, bar_y)], fill=theme.plate_accent, width=bar_w * 2)

    div_x = w * 0.67
    gear_x0 = div_x + w * 0.024
    gear_x1 = w - pad_x - inset - w * 0.022
    gear_y0 = pad_y + inset + h * 0.055
    gear_y1 = h - pad_y - inset - h * 0.055
    gear_poly = chamfer_rect(gear_x0, gear_y0, gear_x1, gear_y1, h * 0.08)
    draw.polygon(
        gear_poly,
        fill=(theme.face_dark[0], theme.face_dark[1], theme.face_dark[2], 235),
        outline=theme.plate_accent,
    )

    draw.line(
        [(div_x, pad_y + inset * 1.8), (div_x, h - pad_y - inset * 1.8)],
        fill=(theme.plate_border[0], theme.plate_border[1], theme.plate_border[2], 145),
        width=max(2, int(round(h * 0.008))),
    )

    font_micro = load_font(int(round(h * 0.072)), style="bold")
    font_unit = load_font(int(round(h * 0.082)), style="regular")
    draw.text(
        (pad_x + inset + w * 0.045, pad_y + inset + h * 0.115),
        "SPEED",
        fill=theme.tick_minor,
        font=font_micro,
        anchor="lm",
    )
    draw.text(
        (div_x - w * 0.035, h - pad_y - inset - h * 0.11),
        "KM/H",
        fill=theme.plate_accent,
        font=font_unit,
        anchor="rm",
    )
    draw.text(
        ((gear_x0 + gear_x1) * 0.5, gear_y0 + h * 0.085),
        "GEAR",
        fill=theme.tick_minor,
        font=font_micro,
        anchor="mm",
    )

    for k in range(3):
        bx = pad_x + inset + w * (0.14 + k * 0.16)
        by = h * 0.55
        r_dot = h * 0.008
        draw.ellipse(
            (bx - r_dot, by + h * 0.15 - r_dot, bx + r_dot, by + h * 0.15 + r_dot),
            fill=(theme.plate_border[0], theme.plate_border[1], theme.plate_border[2], 65),
        )

    return _finish_supersampled(base, glow, width, height, glow_radius=h * 0.022)


def render_heat_badge(
    theme: HUDTheme, level: int, size: int = 512, supersample: int = 2
) -> Image.Image:
    lvl = max(1, min(5, int(level)))
    style = HEAT_TIER_STYLES[lvl]
    ss = max(1, min(int(supersample), 3))
    s = size * ss
    cx = cy = s * 0.5

    base = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(base, "RGBA")
    gdraw = ImageDraw.Draw(glow, "RGBA")

    primary: RGBA = style["primary"]
    secondary: RGBA = style["secondary"]
    glow_col: RGBA = style["glow"]
    wings: int = int(style["wings"])

    for w_idx in range(wings):
        wy = cy - s * 0.09 + w_idx * s * 0.072
        span = s * (0.44 - w_idx * 0.032)
        thick = s * 0.022
        for sign in (-1.0, 1.0):
            wing_poly = [
                (cx + sign * s * 0.25, wy - thick * 0.5),
                (cx + sign * span, wy - thick * 1.1),
                (cx + sign * (span - s * 0.035), wy + thick * 0.6),
                (cx + sign * s * 0.25, wy + thick * 0.5),
            ]
            w_col = primary if (lvl < 5 or w_idx % 2 == 0) else secondary
            draw.polygon(wing_poly, fill=w_col)
            gdraw.polygon(wing_poly, fill=glow_col)

    shield_outer = [
        (cx, s * 0.09),
        (cx + s * 0.29, s * 0.175),
        (cx + s * 0.265, s * 0.56),
        (cx + s * 0.165, s * 0.77),
        (cx, s * 0.90),
        (cx - s * 0.165, s * 0.77),
        (cx - s * 0.265, s * 0.56),
        (cx - s * 0.29, s * 0.175),
    ]
    shield_inner = [
        (cx, s * 0.135),
        (cx + s * 0.245, s * 0.205),
        (cx + s * 0.225, s * 0.545),
        (cx + s * 0.138, s * 0.73),
        (cx, s * 0.845),
        (cx - s * 0.138, s * 0.73),
        (cx - s * 0.225, s * 0.545),
        (cx - s * 0.245, s * 0.205),
    ]

    gdraw.polygon(shield_outer, fill=glow_col)
    draw.polygon(shield_outer, fill=primary)
    draw.polygon(shield_inner, fill=(theme.face_dark[0], theme.face_dark[1], theme.face_dark[2], 248))

    crown = [
        (cx, s * 0.135),
        (cx + s * 0.245, s * 0.205),
        (cx + s * 0.238, s * 0.265),
        (cx, s * 0.205),
        (cx - s * 0.238, s * 0.265),
        (cx - s * 0.245, s * 0.205),
    ]
    draw.polygon(crown, fill=(primary[0], primary[1], primary[2], 82))

    font_hdr = load_font(int(round(s * 0.068)), style="bold")
    font_lvl = load_font(int(round(s * 0.315)), style="bold")
    draw.text((cx, s * 0.265), "HEAT", fill=primary, font=font_hdr, anchor="mm")
    draw.text((cx, s * 0.50 + s * 0.012), str(lvl), fill=(0, 0, 0, 175), font=font_lvl, anchor="mm")
    draw.text((cx, s * 0.50), str(lvl), fill=(252, 254, 255, 255), font=font_lvl, anchor="mm")

    pip_count = 5
    pip_w = s * 0.042
    pip_h = s * 0.022
    pip_gap = s * 0.014
    total_w = pip_count * pip_w + (pip_count - 1) * pip_gap
    px0 = cx - total_w * 0.5
    py = s * 0.705
    for idx in range(pip_count):
        x0 = px0 + idx * (pip_w + pip_gap)
        x1 = x0 + pip_w
        active = idx < lvl
        col = primary if active else (46, 56, 72, 185)
        draw.rectangle((x0, py, x1, py + pip_h), fill=col)
        if active:
            gdraw.rectangle((x0, py, x1, py + pip_h), fill=glow_col)

    return _finish_supersampled(base, glow, size, size, glow_radius=s * 0.018)


def render_minimap_frame(theme: HUDTheme, size: int = 1024, supersample: int = 2) -> Image.Image:
    ss = max(1, min(int(supersample), 3))
    s = size * ss
    cx = cy = s * 0.5

    base = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(base, "RGBA")
    gdraw = ImageDraw.Draw(glow, "RGBA")

    r_out = s * 0.448
    r_in = s * 0.398

    ring_poly = annular_wedge(cx, cy, r_in, r_out, 0.0, 360.0, steps=128)
    draw.polygon(ring_poly, fill=theme.minimap_ring)

    draw.ellipse(
        (cx - r_out, cy - r_out, cx + r_out, cy + r_out),
        outline=theme.bezel_outer,
        width=max(2, int(round(s * 0.0055))),
    )
    draw.ellipse(
        (cx - r_in, cy - r_in, cx + r_in, cy + r_in),
        outline=theme.minimap_accent,
        width=max(2, int(round(s * 0.0042))),
    )
    gdraw.ellipse(
        (cx - r_in, cy - r_in, cx + r_in, cy + r_in),
        outline=(theme.minimap_accent[0], theme.minimap_accent[1], theme.minimap_accent[2], 145),
        width=max(3, int(round(s * 0.0075))),
    )

    for deg in range(0, 360, 10):
        is_cardinal = (deg % 90) == 0
        is_major = (deg % 30) == 0
        r0 = r_in + s * (0.008 if is_cardinal else (0.014 if is_major else 0.020))
        r1 = r_out - s * 0.010
        col = theme.minimap_accent if is_cardinal else (theme.tick_major if is_major else theme.tick_minor)
        w_px = max(2, int(round(s * (0.0045 if is_cardinal else (0.0028 if is_major else 0.0018)))))
        draw.line([polar(cx, cy, r0, deg), polar(cx, cy, r1, deg)], fill=col, width=w_px)

    for corner_deg in (45.0, 135.0, 225.0, 315.0):
        bracket = annular_wedge(
            cx, cy, r_out + s * 0.012, r_out + s * 0.024, corner_deg - 12.0, corner_deg + 12.0, steps=12
        )
        draw.polygon(bracket, fill=(theme.bezel_outer[0], theme.bezel_outer[1], theme.bezel_outer[2], 195))

    ny = cy - r_out - s * 0.002
    n_badge = [
        (cx, ny - s * 0.044),
        (cx + s * 0.044, ny + s * 0.024),
        (cx - s * 0.044, ny + s * 0.024),
    ]
    draw.polygon(n_badge, fill=theme.minimap_accent, outline=(12, 16, 24, 240))
    gdraw.polygon(n_badge, fill=theme.minimap_accent)
    font_n = load_font(int(round(s * 0.036)), style="bold")
    draw.text((cx, ny + s * 0.002), "N", fill=(10, 14, 22, 255), font=font_n, anchor="mm")

    return _finish_supersampled(base, glow, size, size, glow_radius=s * 0.010)


def render_player_arrow(theme: HUDTheme, size: int = 512, supersample: int = 2) -> Image.Image:
    ss = max(1, min(int(supersample), 3))
    s = size * ss
    cx = cy = s * 0.5

    base = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(base, "RGBA")
    gdraw = ImageDraw.Draw(glow, "RGBA")

    tip = (cx, s * 0.11)
    right_wing = (cx + s * 0.285, s * 0.80)
    left_wing = (cx - s * 0.285, s * 0.80)
    notch = (cx, s * 0.64)

    outline_pts = [
        (cx, s * 0.075),
        (cx + s * 0.325, s * 0.835),
        (cx, s * 0.685),
        (cx - s * 0.325, s * 0.835),
    ]
    gdraw.polygon(outline_pts, fill=theme.arrow_glow)
    draw.polygon(outline_pts, fill=(8, 12, 18, 240))

    draw.polygon([tip, left_wing, notch], fill=theme.arrow_light)
    draw.polygon([tip, right_wing, notch], fill=theme.arrow_shade)
    draw.line([tip, notch], fill=(255, 255, 255, 235), width=max(2, int(round(s * 0.009))))

    return _finish_supersampled(base, glow, size, size, glow_radius=s * 0.025)


def build_assembled_tach_showcase(
    theme: HUDTheme,
    dial: Image.Image,
    needle: Image.Image,
    nos_arc: Image.Image,
    sb_arc: Image.Image,
    speed_plate: Image.Image,
    out_size: int = 1024,
    demo_rpm: float = 6900.0,
    demo_speed: int = 264,
    demo_gear: str = "5",
) -> Image.Image:
    w, h = dial.size
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas = Image.alpha_composite(canvas, nos_arc)
    canvas = Image.alpha_composite(canvas, sb_arc)
    canvas = Image.alpha_composite(canvas, dial)

    target_deg = 138.0 + (demo_rpm / 9000.0) * 264.0
    pil_ccw = -(target_deg - 270.0)
    rot_needle = needle.rotate(pil_ccw, resample=Image.Resampling.BICUBIC, expand=False)
    canvas = Image.alpha_composite(canvas, rot_needle)

    pw = int(round(w * 0.36))
    ph = int(round(pw * (speed_plate.height / float(speed_plate.width))))
    plate_resized = speed_plate.resize((pw, ph), Image.Resampling.LANCZOS)
    px = (w - pw) // 2
    py = int(round(h * 0.60))
    canvas.alpha_composite(plate_resized, (px, py))

    draw = ImageDraw.Draw(canvas, "RGBA")
    font_spd = load_font(int(round(ph * 0.48)), style="bold")
    font_gear = load_font(int(round(ph * 0.44)), style="bold")
    draw.text((px + pw * 0.30, py + ph * 0.54), str(demo_speed), fill=(250, 253, 255, 255), font=font_spd, anchor="mm")
    draw.text((px + pw * 0.80, py + ph * 0.57), demo_gear, fill=theme.plate_accent, font=font_gear, anchor="mm")

    return canvas.resize((out_size, out_size), Image.Resampling.LANCZOS)


def render_contact_sheet(
    theme: HUDTheme,
    assets: Dict[str, Image.Image],
    sheet_width: int = 2400,
    sheet_height: int = 1520,
) -> Image.Image:
    sheet = Image.new("RGBA", (sheet_width, sheet_height), (10, 13, 19, 255))
    draw = ImageDraw.Draw(sheet, "RGBA")

    grid_col = (24, 31, 44, 140)
    for gx in range(0, sheet_width, 60):
        draw.line([(gx, 0), (gx, sheet_height)], fill=grid_col, width=1)
    for gy in range(0, sheet_height, 60):
        draw.line([(0, gy), (sheet_width, gy)], fill=grid_col, width=1)

    draw.rectangle((0, 0, sheet_width, 118), fill=(7, 10, 16, 245))
    draw.line([(0, 118), (sheet_width, 118)], fill=theme.plate_accent, width=3)
    font_title = load_font(44, style="bold")
    font_sub = load_font(22, style="regular")
    font_lbl = load_font(20, style="bold")
    font_meta = load_font(16, style="regular")

    draw.text((48, 34), f"NFSMW: ROCKPORT 2026  —  {theme.title} 4K HUD KIT", fill=(246, 250, 255, 255), font=font_title)
    draw.text((48, 82), f"{theme.subtitle}  |  100% Original / CC0 Vector Assets (2x Supersampled Lanczos)", fill=theme.plate_accent, font=font_sub)

    def draw_card(x: int, y: int, w: int, h: int, title: str, meta: str) -> Tuple[int, int, int, int]:
        draw.rounded_rectangle((x, y, x + w, y + h), radius=16, fill=theme.card_bg, outline=(46, 58, 78, 220), width=2)
        draw.text((x + 18, y + 14), title, fill=(236, 244, 255, 255), font=font_lbl)
        draw.text((x + w - 18, y + 16), meta, fill=(140, 158, 184, 220), font=font_meta, anchor="ra")
        return (x + 14, y + 48, w - 28, h - 60)

    def paste_fit(img: Image.Image, box: Tuple[int, int, int, int]) -> None:
        bx, by, bw, bh = box
        scale = min(bw / float(img.width), bh / float(img.height))
        tw = max(1, int(round(img.width * scale)))
        th = max(1, int(round(img.height * scale)))
        resized = img.resize((tw, th), Image.Resampling.LANCZOS)
        sheet.alpha_composite(resized, (bx + (bw - tw) // 2, by + (bh - th) // 2))

    hero_box = draw_card(40, 148, 860, 920, "COAXIAL ASSEMBLED CLUSTER", "LIVE LAYER COMPOSITE")
    assembled = build_assembled_tach_showcase(
        theme=theme,
        dial=assets["hud_tach_dial.png"],
        needle=assets["hud_tach_needle.png"],
        nos_arc=assets["hud_nos_arc.png"],
        sb_arc=assets["hud_speedbreaker_arc.png"],
        speed_plate=assets["hud_speed_plate.png"],
        out_size=820,
    )
    paste_fit(assembled, hero_box)

    layer_cards = [
        ("hud_tach_dial.png", "TACHOMETER DIAL"),
        ("hud_tach_needle.png", "TACHOMETER NEEDLE"),
        ("hud_nos_arc.png", "NITROUS BOOST ARC"),
        ("hud_speedbreaker_arc.png", "SPEEDBREAKER ARC"),
    ]
    gx0, gy0 = 930, 148
    cw, ch_card = 460, 446
    for idx, (fname, title) in enumerate(layer_cards):
        col = idx % 2
        row = idx // 2
        img = assets[fname]
        box = draw_card(
            gx0 + col * (cw + 28),
            gy0 + row * (ch_card + 28),
            cw,
            ch_card,
            title,
            f"{img.width}x{img.height} RGBA",
        )
        paste_fit(img, box)

    rx = gx0 + 2 * (cw + 28)
    rw = sheet_width - rx - 40
    sp_img = assets["hud_speed_plate.png"]
    sp_box = draw_card(rx, gy0, rw, 340, "SPEED / GEAR PLATE", f"{sp_img.width}x{sp_img.height} RGBA")
    paste_fit(sp_img, sp_box)

    mm_img = assets["hud_minimap_frame.png"]
    arr_img = assets["hud_player_arrow.png"]
    mm_box = draw_card(rx, gy0 + 368, rw, 552, "MINIMAP FRAME + ARROW", f"{mm_img.width}px / {arr_img.width}px")
    mm_preview = mm_img.copy()
    arr_small = arr_img.resize((mm_img.width // 5, mm_img.height // 5), Image.Resampling.LANCZOS)
    mm_preview.alpha_composite(
        arr_small, ((mm_img.width - arr_small.width) // 2, (mm_img.height - arr_small.height) // 2)
    )
    paste_fit(mm_preview, mm_box)

    by = 1098
    bh = 382
    heat_card_w = (sheet_width - 80 - 5 * 24) // 6
    for lvl in range(1, 6):
        fname = f"hud_heat_{lvl}.png"
        h_img = assets[fname]
        bx = 40 + (lvl - 1) * (heat_card_w + 24)
        box = draw_card(bx, by, heat_card_w, bh, f"HEAT LEVEL {lvl}", f"{h_img.width}x{h_img.height}")
        paste_fit(h_img, box)

    ax = 40 + 5 * (heat_card_w + 24)
    box = draw_card(ax, by, heat_card_w, bh, "PLAYER ARROW", f"{arr_img.width}x{arr_img.height}")
    paste_fit(arr_img, box)

    return sheet.convert("RGB")


def generate_hud_kit(
    out_dir: Path,
    theme_name: str = "motorsport2026",
    dial_size: int = 2048,
    supersample: int = 2,
    write_contact_sheet: bool = True,
) -> Dict[str, Any]:
    if theme_name not in THEMES:
        raise ValueError(f"Unknown theme {theme_name!r}. Choose from: {', '.join(sorted(THEMES))}")

    theme = THEMES[theme_name]
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    plate_w = max(256, dial_size // 2)
    plate_h = max(128, dial_size // 4)
    minimap_sz = max(256, dial_size // 2)
    badge_sz = max(128, dial_size // 4)

    assets: Dict[str, Image.Image] = {
        "hud_tach_dial.png": render_tach_dial(theme, size=dial_size, supersample=supersample),
        "hud_tach_needle.png": render_tach_needle(theme, size=dial_size, supersample=supersample),
        "hud_nos_arc.png": render_nos_arc(theme, size=dial_size, supersample=supersample),
        "hud_speedbreaker_arc.png": render_speedbreaker_arc(theme, size=dial_size, supersample=supersample),
        "hud_speed_plate.png": render_speed_plate(theme, width=plate_w, height=plate_h, supersample=supersample),
        "hud_minimap_frame.png": render_minimap_frame(theme, size=minimap_sz, supersample=supersample),
        "hud_player_arrow.png": render_player_arrow(theme, size=badge_sz, supersample=supersample),
    }
    for lvl in range(1, 6):
        assets[f"hud_heat_{lvl}.png"] = render_heat_badge(theme, level=lvl, size=badge_sz, supersample=supersample)

    saved_files: List[Dict[str, Any]] = []
    for fname, img in assets.items():
        fp = out_dir / fname
        img.save(fp, format="PNG")
        saved_files.append(
            {"filename": fname, "path": str(fp), "size": [img.width, img.height], "mode": img.mode}
        )

    contact_sheet_path: Optional[str] = None
    if write_contact_sheet:
        sheet = render_contact_sheet(theme, assets)
        sheet_fp = out_dir / "hud_contact_sheet.png"
        sheet.save(sheet_fp, format="PNG")
        contact_sheet_path = str(sheet_fp)

    return {
        "theme": theme.name,
        "title": theme.title,
        "out_dir": str(out_dir),
        "dial_size": dial_size,
        "supersample": supersample,
        "asset_count": len(saved_files),
        "assets": saved_files,
        "contact_sheet": contact_sheet_path,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="generate_hud_pack.py",
        description=(
            "Generate a 100% original / CC0 4K Modern Racing HUD Kit (2x supersampled Lanczos) "
            "for NFSMW: Rockport 2026 in motorsport2026 and/or rockport_neon themes."
        ),
    )
    parser.add_argument(
        "--theme",
        default="motorsport2026",
        choices=["motorsport2026", "rockport_neon", "all"],
        help="HUD visual theme (default: motorsport2026; use 'all' to generate both themes).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("build/hud_pack"),
        help="Output root directory (default: build/hud_pack).",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=2048,
        help="Main dial/arc square texture size in pixels (default: 2048, rendered at 2x = 4096).",
    )
    parser.add_argument(
        "--supersample",
        type=int,
        default=2,
        help="Supersampling factor before Lanczos downsample (default: 2).",
    )
    parser.add_argument(
        "--no-contact-sheet",
        action="store_true",
        help="Skip generating hud_contact_sheet.png preview.",
    )
    parser.add_argument("--json", action="store_true", help="Emit manifest summary as JSON.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.size < 256 or args.size > 4096:
        parser.error("--size must be between 256 and 4096.")

    themes = ["motorsport2026", "rockport_neon"] if args.theme == "all" else [args.theme]
    reports: List[Dict[str, Any]] = []
    for t in themes:
        target_dir = (args.out / t) if args.theme == "all" else args.out
        rep = generate_hud_kit(
            out_dir=target_dir,
            theme_name=t,
            dial_size=args.size,
            supersample=args.supersample,
            write_contact_sheet=not args.no_contact_sheet,
        )
        reports.append(rep)

    if args.json:
        print(json.dumps(reports if len(reports) > 1 else reports[0], indent=2))
    else:
        for rep in reports:
            print(
                f"Generated HUD kit [{rep['theme']}]: {rep['asset_count']} RGBA assets "
                f"(dial={rep['dial_size']}px, {rep['supersample']}x SS) -> {rep['out_dir']}"
            )
            if rep["contact_sheet"]:
                print(f"  Contact sheet: {rep['contact_sheet']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
