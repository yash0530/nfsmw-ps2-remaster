# NFSMW: Rockport 2026 — Reproducible Build & macOS PCSX2 Workflow
# Usage:
#   make inspect ISO="/path/to/Need for Speed - Most Wanted - Black Edition.iso"
#   make build-iso ISO="/path/to/Need for Speed - Most Wanted - Black Edition.iso"
#   make setup-pcsx2

ISO ?= $(HOME)/Documents/PS2Games/Need for Speed - Most Wanted - Black Edition.iso
OUT_ISO ?= build/NFSMW_BlackEdition_Rockport2026_v0.1.iso
PYTHON ?= python3

.PHONY: help inspect zdir vlt build-iso setup-pcsx2 cc0-search hud-demo

help:
	@echo "NFSMW: Rockport 2026 — Open-Source PS2 Remaster Toolchain"
	@echo ""
	@echo "Targets:"
	@echo "  make inspect     - Read-only inspection of source PS2 ISO & ZDIR.BIN"
	@echo "  make zdir        - List resolved virtual files inside ZZDATA0..3.BIN"
	@echo "  make vlt         - Inspect PS2 GLOBAL\\ATTRIBUTES.BIN VPAK/VLT database"
	@echo "  make build-iso   - Non-destructively build modded ISO + companion .pnach"
	@echo "  make setup-pcsx2 - Install 4K Metal per-game PCSX2 settings & patches (macOS)"
	@echo "  make cc0-search  - Search Poly Haven CC0 asphalt/road textures"
	@echo "  make hud-demo    - Generate a 2048x2048 procedural modern tachometer prototype"

inspect:
	$(PYTHON) tools/iso_inspect.py "$(ISO)"

zdir:
	$(PYTHON) tools/zdir_tool.py list "$(ISO)" --resolved-only

vlt:
	$(PYTHON) tools/vlt_ps2.py "$(ISO)" --virtual-path "GLOBAL\\ATTRIBUTES.BIN"

build-iso:
	$(PYTHON) tools/build_modded_iso.py --source "$(ISO)" --output "$(OUT_ISO)" --verify-source-sha256

setup-pcsx2:
	$(PYTHON) tools/setup_pcsx2_macos.py --crcs 0518D274

cc0-search:
	$(PYTHON) tools/fetch_cc0_assets.py search --source polyhaven --query asphalt --limit 10

hud-demo:
	$(PYTHON) tools/texture_pipeline.py render-hud-demo --out build/hud_preview
