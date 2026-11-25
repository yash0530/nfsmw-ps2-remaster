# NFSMW: Rockport 2026 — Reproducible Build & macOS PCSX2 Workflow
# Usage:
#   make status
#   make verify
#   make build-v0.2 ISO="/path/to/Need for Speed - Most Wanted - Black Edition.iso"
#   make setup-pcsx2

ISO ?= $(HOME)/Documents/PS2Games/Need for Speed - Most Wanted - Black Edition.iso
PROFILE ?= patches/profiles/rockport2026_v0.2.json
PYTHON ?= python3

.PHONY: help status verify test lint audit inspect zdir vlt build-v0.2 setup-pcsx2 cc0-search hud-pack

help:
	@echo "NFSMW: Rockport 2026 — Open-Source PS2 Remaster Toolchain (v0.2)"
	@echo ""
	@echo "Targets:"
	@echo "  make status      - Show ISO, modded build, PCSX2 profile, and tool readiness"
	@echo "  make verify      - Run py_compile, hygiene audit, pnach lint, and all unit tests"
	@echo "  make test        - Run full unittest suite"
	@echo "  make inspect     - Read-only inspection of source PS2 ISO & ZDIR.BIN"
	@echo "  make zdir        - List resolved virtual files inside ZZDATA0..3.BIN"
	@echo "  make vlt         - List decoded VLT classes inside GLOBAL\\ATTRIBUTES.BIN"
	@echo "  make build-v0.2  - Non-destructively build v0.2 modded ISO from $(PROFILE)"
	@echo "  make setup-pcsx2 - Install 4K Metal 60fps per-game PCSX2 settings (macOS)"
	@echo "  make cc0-search  - Search Poly Haven CC0 asphalt/road textures"
	@echo "  make hud-pack    - Render full 4K dual-theme procedural HUD kits"

status:
	$(PYTHON) tools/rockport_cli.py status

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

build-v0.2:
	$(PYTHON) tools/build_modded_iso.py --source "$(ISO)" --profile "$(PROFILE)" --verify-source-sha256

setup-pcsx2:
	$(PYTHON) tools/setup_pcsx2_macos.py --crcs 0518D274 --renderer metal --upscale 6 --anisotropy 16 --ee-cycle-rate 2

cc0-search:
	$(PYTHON) tools/fetch_cc0_assets.py search --source polyhaven --query asphalt --limit 10

hud-pack:
	$(PYTHON) tools/generate_hud_pack.py --theme all --out build/hud_pack
