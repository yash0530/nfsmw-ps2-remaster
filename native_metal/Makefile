# NFSMW: Rockport 2026 — Native Apple Silicon Metal 2026 PBR Renderer
# Pure Apple Swift + Metal (zero external packages).
# Usage:
#   make -C native_metal OUT_DIR=../build/metal_wave3

OUT_DIR ?= ../build/metal_wave3
SWIFTC ?= /usr/bin/swiftc
SDK ?= $(shell xcrun --sdk macosx --show-sdk-path 2>/dev/null)
TARGET ?= arm64-apple-macosx14.0
BIN = $(OUT_DIR)/RockportMetalPBR
METALLIB = $(OUT_DIR)/RockportMetalPBR.metallib

SWIFT_SRCS = \
	ObjLoader.swift \
	TextureLoader.swift \
	PBRRenderer.swift \
	main.swift

FRAMEWORKS = \
	-framework Metal \
	-framework MetalKit \
	-framework AppKit \
	-framework CoreGraphics \
	-framework ImageIO \
	-framework UniformTypeIdentifiers

.PHONY: all clean metallib

all: $(BIN) metallib
	cp -f Shaders.metal $(OUT_DIR)/Shaders.metal

$(OUT_DIR):
	mkdir -p $(OUT_DIR)

$(BIN): $(SWIFT_SRCS) | $(OUT_DIR)
	$(SWIFTC) -O -whole-module-optimization \
		-target $(TARGET) \
		$(if $(SDK),-sdk $(SDK),) \
		$(FRAMEWORKS) \
		$(SWIFT_SRCS) -o $(BIN)

# Compile .metallib when Metal Toolchain is installed; runtime device.makeLibrary(source:)
# fallback in PBRRenderer.swift guarantees full portability even without MetalToolchain.
metallib: Shaders.metal | $(OUT_DIR)
	@if xcrun --sdk macosx --find metal >/dev/null 2>&1 && xcrun --sdk macosx metal --version >/dev/null 2>&1; then \
		xcrun --sdk macosx metal -c Shaders.metal -o $(OUT_DIR)/Shaders.air && \
		xcrun --sdk macosx metallib $(OUT_DIR)/Shaders.air -o $(METALLIB) && \
		echo "Built precompiled $(METALLIB)"; \
	else \
		echo "Note: xcrun metal toolchain unavailable; using runtime Shaders.metal compilation fallback."; \
	fi

clean:
	rm -f $(BIN) $(METALLIB) $(OUT_DIR)/Shaders.air $(OUT_DIR)/Shaders.metal
