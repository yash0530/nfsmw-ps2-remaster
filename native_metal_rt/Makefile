# NFSMW: Rockport 2026 — Native Apple Silicon Metal 4 Hardware Ray Tracing Engine
# Pure Apple Swift + Metal (zero external packages).
# Usage:
#   make -C native_metal_rt OUT_DIR=../build/metal_rt_wave4

OUT_DIR ?= ../build/metal_rt_wave4
SWIFTC ?= /usr/bin/swiftc
SDK ?= $(shell xcrun --sdk macosx --show-sdk-path 2>/dev/null)
TARGET ?= arm64-apple-macosx14.0
BIN = $(OUT_DIR)/RockportMetalRT
METALLIB = $(OUT_DIR)/RockportMetalRT.metallib

SWIFT_SRCS = \
	RTWorldLoader.swift \
	RTWorldStreamer.swift \
	RTSceneBuilder.swift \
	RTHudTypes.swift \
	RTPhysics.swift \
	RTAudioEngine.swift \
	RTGameWorld.swift \
	RTEventManager.swift \
	RTVFXSystem.swift \
	RTAIVehicleLoader.swift \
	RTRenderer.swift \
	main.swift

FRAMEWORKS = \
	-framework Metal \
	-framework MetalKit \
	-framework MetalFX \
	-framework AppKit \
	-framework CoreGraphics \
	-framework ImageIO \
	-framework UniformTypeIdentifiers \
	-framework AudioToolbox \
	-framework CoreAudio \
	-framework AVFoundation \
	-framework GameController

.PHONY: all clean metallib

all: $(BIN) metallib
	cp -f RayTracing.metal $(OUT_DIR)/RayTracing.metal
	cp -f RTHud.metal $(OUT_DIR)/RTHud.metal

$(OUT_DIR):
	mkdir -p $(OUT_DIR)

$(BIN): $(SWIFT_SRCS) | $(OUT_DIR)
	$(SWIFTC) -O -whole-module-optimization \
		-target $(TARGET) \
		$(if $(SDK),-sdk $(SDK),) \
		$(FRAMEWORKS) \
		$(SWIFT_SRCS) -o $(BIN)

# Compile .metallib when Metal Toolchain is installed; runtime device.makeLibrary(source:)
# fallback in RTRenderer.swift guarantees full portability even without MetalToolchain.
metallib: RayTracing.metal | $(OUT_DIR)
	@if xcrun --sdk macosx --find metal >/dev/null 2>&1 && xcrun --sdk macosx metal --version >/dev/null 2>&1; then \
		xcrun --sdk macosx metal -std=metal3.1 -c RayTracing.metal -o $(OUT_DIR)/RayTracing.air && \
		xcrun --sdk macosx metallib $(OUT_DIR)/RayTracing.air -o $(METALLIB) && \
		echo "Built precompiled $(METALLIB)"; \
	else \
		echo "Note: xcrun metal toolchain unavailable; using runtime RayTracing.metal compilation fallback."; \
	fi

clean:
	rm -f $(BIN) $(METALLIB) $(OUT_DIR)/RayTracing.air $(OUT_DIR)/RayTracing.metal $(OUT_DIR)/RTHud.metal
