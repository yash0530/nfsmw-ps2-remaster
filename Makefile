# NFSMW: Rockport 2026 — Reproducible Build & macOS PCSX2 Workflow
# Usage:
#   make status
#   make profiles
#   make verify
#   make build PROFILE=rockport2026_v0.3 ISO="/path/to/Need for Speed - Most Wanted - Black Edition.iso"

ISO ?= $(HOME)/Documents/PS2Games/Need for Speed - Most Wanted - Black Edition.iso
PROFILE ?= rockport2026_v0.4
PYTHON ?= python3

.PHONY: help status profiles verify test lint audit inspect zdir vlt lighting audio geometry tpk-extract build build-v0.2 build-v0.3 build-v0.4 setup-pcsx2 cc0-search hud-pack build-dds caves decomp-bridge metal-showcase metal-rt live-verify

help:
	@echo "NFSMW: Rockport 2026 — Open-Source PS2 + Native Metal 4 RT Suite (v0.4)"
	@echo ""
	@echo "Targets:"
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
	@echo "  make build-dds      - Compile native arm64 Rust BC7 encoder & build mipmapped DDS pack"
	@echo "  make caves          - Verify MIPS R5900 code caves & emit .pnach"
	@echo "  make decomp-bridge  - Generate C++20/Metal headers from VLT + lighting recipes"
	@echo "  make metal-showcase - Launch interactive native Apple Silicon Metal 2026 PBR showcase"
	@echo "  make metal-rt       - Launch interactive M5 Pro Metal 4 Hardware Ray Tracing showcase"
	@echo "  make live-verify    - Boot-verify v0.4 ISO live in PCSX2 v2.6.3 Metal"
	@echo "  make setup-pcsx2    - Install 4K Metal 60fps per-game PCSX2 settings (macOS)"
	@echo "  make cc0-search     - Search Poly Haven CC0 asphalt/road textures"
	@echo "  make hud-pack       - Render full 4K dual-theme procedural HUD kits"

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

build-dds:
	$(PYTHON) tools/rockport_cli.py build-dds --size 2048

caves:
	$(PYTHON) tools/rockport_cli.py build-caves

decomp-bridge:
	$(PYTHON) tools/rockport_cli.py decomp-bridge --json

metal-showcase:
	$(PYTHON) tools/rockport_cli.py metal-showcase --iso "$(ISO)" --preset golden_hour

metal-rt:
	$(PYTHON) tools/rockport_cli.py metal-rt --iso "$(ISO)" --preset golden_hour

live-verify:
	$(PYTHON) tools/rockport_cli.py live-verify --iso build/NFSMW_BlackEdition_Rockport2026_v0.4.iso

setup-pcsx2:
	$(PYTHON) tools/setup_pcsx2_macos.py --crcs 0518D274 --renderer metal --upscale 6 --anisotropy 16 --ee-cycle-rate 2

cc0-search:
	$(PYTHON) tools/fetch_cc0_assets.py search --source polyhaven --query asphalt --limit 10

hud-pack:
	$(PYTHON) tools/generate_hud_pack.py --theme all --out build/hud_pack
