# NFSMW: Rockport 2026 — Native Apple Silicon Metal 4 RT & Reproducible PS2 Workflow
# Quickstart:
#   export NFSMW_ISO="/path/to/Need for Speed - Most Wanted - Black Edition.iso"
#   make export-rockport-full
#   make play-rockport
#   make play-pursuit
#   make bench-rockport

DEFAULT_ISO := $(HOME)/Documents/PS2Games/Need for Speed - Most Wanted - Black Edition.iso
ISO ?= $(if $(NFSMW_ISO),$(NFSMW_ISO),$(DEFAULT_ISO))
PROFILE ?= rockport2026_v0.5
PYTHON ?= python3
ROCKPORT_RT_DIR := $(CURDIR)/build/metal_rt_v06
ROCKPORT_RT_BIN := $(ROCKPORT_RT_DIR)/RockportMetalRT
ROCKPORT_FULL_RKPT := build/rockport_world/rockport_full.rkpt
ROCKPORT_GAMEPLAY_VLT := build/gameplay_pack/vlt_vehicles.json
ROCKPORT_ARGS ?=

.PHONY: help export-rockport-full export-music-pack build-rockport play-rockport play-pursuit bench-rockport status profiles verify test lint audit inspect zdir vlt lighting audio geometry tpk-extract build build-v0.2 build-v0.3 build-v0.4 build-v0.5 enhance-world pine-drive video-qa export-world metal-rt-world setup-pcsx2 cc0-search hud-pack build-dds caves decomp-bridge metal-rt live-verify

help:
	@echo "NFSMW: Rockport 2026 — Native Apple Silicon Metal 4 RT + Open-Source PS2 Suite (v1.1.0)"
	@echo ""
	@echo "Play & Benchmark (Native Metal 4 RT — Full Rockport Island):"
	@echo "  make export-rockport-full - Export full 718-section BC7 Rockport island + VLT/Audio/AI + EA TRAX music pack (requires NFSMW_ISO)"
	@echo "  make export-music-pack    - Export authentic 26 EA TRAX + 14 interactive pursuit/race score tracks -> build/music_pack (requires NFSMW_ISO)"
	@echo "  make build-rockport       - Compile native Apple Silicon Metal 4 RT engine -> build/metal_rt_v06/RockportMetalRT"
	@echo "  make play-rockport        - Export/build if needed and launch full-island Rockport 2026 @ 120 FPS Frame-Gen"
	@echo "  make play-pursuit         - Launch straight into an active Heat 3 Rosewood police pursuit @ 120 FPS Frame-Gen"
	@echo "  make bench-rockport       - Run 6-district full-island Frame-Gen benchmark & road-graph connectivity audit"
	@echo ""
	@echo "Verification & Tooling:"
	@echo "  make status         - Show ISO, modded builds, PCSX2 profile, and tool readiness"
	@echo "  make profiles       - Validate and compare all preset build profiles"
	@echo "  make verify         - Run py_compile, hygiene audit, pnach lint, profiles, and all tests"
	@echo "  make test           - Run full unittest suite"
	@echo "  make inspect        - Read-only inspection of source PS2 ISO & ZDIR.BIN"
	@echo "  make zdir           - List resolved virtual files inside ZZDATA0..3.BIN"
	@echo "  make vlt            - List decoded VLT classes inside GLOBAL\\ATTRIBUTES.BIN"
	@echo "  make lighting       - Inspect SunInfos, LightMaterials & MemoryFile inside GLOBALB.LZC"
	@echo "  make audio          - Summarize PS2 ABKC, Gnsu, and SCHl audio containers"
	@echo "  make geometry       - Inspect BMW M3 GTR GEOMETRY.BIN meshes & LODs"
	@echo "  make tpk-extract    - Decode embedded PS2 TPK textures read-only into build/tpk_extracted"
	@echo "  make build          - One-command build + PCSX2 4K Metal 60fps install for $(PROFILE)"
	@echo "  make build-v0.5     - Build flagship v0.5.0 Forza Ultra + Wet Reflections + 84-car profile"
	@echo "  make enhance-world  - Enhance live PCSX2 world dumps to 4x BC7 DDS (preserving 0x80 alpha)"
	@echo "  make pine-drive     - Probe live PCSX2 PINE IPC RAM telemetry (slot 28011)"
	@echo "  make video-qa       - Show native AVFoundation MP4 video QA pipeline usage"
	@echo "  make export-world   - Export Rosewood + Downtown pilot districts to .rkpt from source ISO"
	@echo "  make metal-rt-world - Build & launch native Metal 4 RT + MetalFX driving in Rockport City"
	@echo "  make build-dds      - Compile native arm64 Rust BC7 encoder & build mipmapped DDS pack"
	@echo "  make caves          - Verify MIPS R5900 code caves & emit .pnach"
	@echo "  make decomp-bridge  - Generate C++20/Metal headers from VLT + lighting recipes"
	@echo "  make metal-rt       - Launch interactive native Apple Silicon Metal 4 RT showcase"
	@echo "  make live-verify    - Boot-verify v0.5 ISO live in PCSX2 v2.6.3 Metal"
	@echo "  make setup-pcsx2    - Install 4K Metal 60fps per-game PCSX2 settings (macOS)"
	@echo "  make cc0-search     - Search Poly Haven CC0 asphalt/road textures"
	@echo "  make hud-pack       - Render full 4K dual-theme procedural HUD kits"

export-rockport-full:
	@ISO_PATH="$(ISO)"; \
	if [ -z "$$ISO_PATH" ] || [ ! -f "$$ISO_PATH" ]; then \
		echo "ERROR: Legal PS2 NFSMW Black Edition (SLUS-21351) ISO not found." >&2; \
		echo "  Checked: $$ISO_PATH" >&2; \
		echo "  Set NFSMW_ISO=/path/to/Need\\ for\\ Speed\\ -\\ Most\\ Wanted\\ -\\ Black\\ Edition.iso and re-run." >&2; \
		exit 1; \
	fi; \
	$(PYTHON) tools/export_rockport_world.py --district rockport_full --version 2 --bc7 --iso "$$ISO_PATH" --out-dir build/rockport_world && \
	$(PYTHON) tools/export_gameplay_pack.py --iso "$$ISO_PATH" --out build/gameplay_pack && \
	if command -v afconvert >/dev/null 2>&1; then \
		$(PYTHON) tools/export_music_pack.py --iso "$$ISO_PATH" --out build/music_pack; \
	else \
		echo "Note: skipping optional EA TRAX music export (macOS afconvert not found)."; \
	fi

export-music-pack:
	@ISO_PATH="$(ISO)"; \
	if [ -z "$$ISO_PATH" ] || [ ! -f "$$ISO_PATH" ]; then \
		echo "ERROR: Legal PS2 NFSMW Black Edition (SLUS-21351) ISO not found." >&2; \
		echo "  Checked: $$ISO_PATH" >&2; \
		echo "  Set NFSMW_ISO=/path/to/Need\\ for\\ Speed\\ -\\ Most\\ Wanted\\ -\\ Black\\ Edition.iso and re-run." >&2; \
		exit 1; \
	fi; \
	$(PYTHON) tools/export_music_pack.py --iso "$$ISO_PATH" --out build/music_pack

build-rockport:
	$(MAKE) -C native_metal_rt OUT_DIR="$(ROCKPORT_RT_DIR)"

play-rockport: build-rockport
	@if [ ! -f "$(ROCKPORT_FULL_RKPT)" ] || [ ! -f "$(ROCKPORT_GAMEPLAY_VLT)" ]; then \
		$(MAKE) export-rockport-full; \
	fi
	"$(ROCKPORT_RT_BIN)" --world "$(ROCKPORT_FULL_RKPT)" --frame-gen on --hud $(ROCKPORT_ARGS)

play-pursuit: build-rockport
	@if [ ! -f "$(ROCKPORT_FULL_RKPT)" ] || [ ! -f "$(ROCKPORT_GAMEPLAY_VLT)" ]; then \
		$(MAKE) export-rockport-full; \
	fi
	"$(ROCKPORT_RT_BIN)" --world "$(ROCKPORT_FULL_RKPT)" --landmark "Rosewood Blvd" --pursuit 3 --frame-gen on --hud $(ROCKPORT_ARGS)

bench-rockport: build-rockport
	$(PYTHON) tests/test_multi_district_bench.py -v && $(PYTHON) tests/test_multi_district_bench.py --bench

status:
	$(PYTHON) tools/rockport_cli.py status

profiles:
	$(PYTHON) tools/rockport_cli.py profiles

verify:
	$(PYTHON) tools/rockport_cli.py verify

test:
	$(PYTHON) -m unittest discover -s tests -v

lint:
	$(PYTHON) tools/lint_pnach.py

audit:
	$(PYTHON) tools/audit_repo_clean.py

inspect:
	$(PYTHON) tools/iso_inspect.py "$(ISO)"

zdir:
	$(PYTHON) tools/zdir_tool.py list "$(ISO)" --resolved-only

vlt:
	$(PYTHON) tools/vlt_ps2.py classes "$(ISO)"

lighting:
	$(PYTHON) tools/lighting_ps2.py inspect "$(ISO)" --sun MIDDAY --material ROAD

audio:
	$(PYTHON) tools/audio_inspect.py summary "$(ISO)"

geometry:
	$(PYTHON) tools/geometry_inspect.py inspect "$(ISO)" "CARS\\BMWM3GTRE46\\GEOMETRY.BIN"

tpk-extract:
	$(PYTHON) tools/tpk_extract.py extract "$(ISO)" --all --out build/tpk_extracted

build:
	$(PYTHON) tools/rockport_cli.py build --profile "$(PROFILE)" --source "$(ISO)" --install-pcsx2 --renderer metal --upscale 6 --anisotropy 16 --ee-cycle-rate 2

build-v0.2:
	$(PYTHON) tools/rockport_cli.py build --profile rockport2026_v0.2 --source "$(ISO)"

build-v0.3:
	$(PYTHON) tools/rockport_cli.py build --profile rockport2026_v0.3 --source "$(ISO)" --install-pcsx2 --renderer metal --upscale 6 --anisotropy 16 --ee-cycle-rate 2

build-v0.4:
	$(PYTHON) tools/rockport_cli.py build --profile rockport2026_v0.4 --source "$(ISO)" --install-pcsx2 --renderer metal --upscale 6 --anisotropy 16 --ee-cycle-rate 2

build-v0.5:
	$(PYTHON) tools/rockport_cli.py build --profile rockport2026_v0.5 --source "$(ISO)" --install-pcsx2 --renderer metal --upscale 6 --anisotropy 16 --ee-cycle-rate 2

enhance-world:
	$(PYTHON) tools/rockport_cli.py enhance-world --scale 4 --max-size 2048

pine-drive:
	$(PYTHON) tools/rockport_cli.py pine-drive probe

video-qa:
	$(PYTHON) tools/video_qa_pipeline.py --help

export-world:
	$(PYTHON) tools/rockport_cli.py export-world --district all --iso "$(ISO)" --out-dir build/rockport_world

metal-rt-world:
	$(PYTHON) tools/rockport_cli.py metal-rt --world $(ROCKPORT_FULL_RKPT) --denoise metalfx --frame-gen on --hud --preset golden_hour --camera-mode chase

build-dds:
	$(PYTHON) tools/rockport_cli.py build-dds --size 2048

caves:
	$(PYTHON) tools/rockport_cli.py build-caves

decomp-bridge:
	$(PYTHON) tools/rockport_cli.py decomp-bridge --json

metal-rt:
	$(PYTHON) tools/rockport_cli.py metal-rt --iso "$(ISO)" --preset golden_hour

live-verify:
	$(PYTHON) tools/rockport_cli.py live-verify --iso build/NFSMW_BlackEdition_Rockport2026_v0.5.iso

setup-pcsx2:
	$(PYTHON) tools/setup_pcsx2_macos.py --crcs 0518D274 --renderer metal --upscale 6 --anisotropy 16 --ee-cycle-rate 2

cc0-search:
	$(PYTHON) tools/fetch_cc0_assets.py search --source polyhaven --query asphalt --limit 10

hud-pack:
	$(PYTHON) tools/generate_hud_pack.py --theme all --out build/hud_pack
