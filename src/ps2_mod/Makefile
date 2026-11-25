# NFSMW: Rockport 2026 — ps2dev/ps2sdk EE C Mod Blueprint Makefile
#
# All intermediate objects, ELFs, and raw cave binaries are written strictly to
# ../../build/caves_wave3/ps2_mod/ so src/ps2_mod/ stays completely clean.

REPO_ROOT := ../..
BUILD_DIR := $(REPO_ROOT)/build/caves_wave3/ps2_mod

EE_PREFIX ?= mips64r5900el-ps2-elf-
EE_CC      := $(EE_PREFIX)gcc
EE_OBJCOPY := $(EE_PREFIX)objcopy
EE_OBJDUMP := $(EE_PREFIX)objdump

# Fall back to legacy ee-gcc prefix if mips64r5900el-ps2-elf-gcc is absent
ifeq ($(shell which $(EE_CC) 2>/dev/null),)
  EE_PREFIX  := ee-
  EE_CC      := $(EE_PREFIX)gcc
  EE_OBJCOPY := $(EE_PREFIX)objcopy
  EE_OBJDUMP := $(EE_PREFIX)objdump
endif

EE_CFLAGS := -Os -G0 -Wall -Wextra -Werror \
             -ffreestanding -fno-builtin -fno-pic -mno-abicalls \
             -march=r5900 -mabi=eabi -mgp64 -mno-gpopt \
             -ffunction-sections -fdata-sections

EE_LDFLAGS := -T linkfile.ld -nostdlib -nostartfiles -Wl,--gc-sections

ELF_OUT    := $(BUILD_DIR)/rockport_mod.elf
CAVE_A_BIN := $(BUILD_DIR)/cave_a.bin
CAVE_B_BIN := $(BUILD_DIR)/cave_b.bin
OBJ_OUT    := $(BUILD_DIR)/rockport_mod.o

.PHONY: all clean disasm verify-python

all: $(CAVE_A_BIN) $(CAVE_B_BIN)

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(OBJ_OUT): rockport_mod.c rockport_mod.h | $(BUILD_DIR)
	$(EE_CC) $(EE_CFLAGS) -c rockport_mod.c -o $(OBJ_OUT)

$(ELF_OUT): $(OBJ_OUT) linkfile.ld
	$(EE_CC) $(EE_CFLAGS) $(EE_LDFLAGS) $(OBJ_OUT) -o $(ELF_OUT)

$(CAVE_A_BIN): $(ELF_OUT)
	$(EE_OBJCOPY) -O binary --only-section=.text.rockport_cave_a $(ELF_OUT) $(CAVE_A_BIN)

$(CAVE_B_BIN): $(ELF_OUT)
	$(EE_OBJCOPY) -O binary --only-section=.text.rockport_cave_b $(ELF_OUT) $(CAVE_B_BIN)

disasm: $(ELF_OUT)
	$(EE_OBJDUMP) -d -M reg-names=numeric $(ELF_OUT)

verify-python:
	python3 $(REPO_ROOT)/tools/mips_cave_builder.py verify $(REPO_ROOT)/patches/elf_caves.json

clean:
	rm -rf $(BUILD_DIR)
