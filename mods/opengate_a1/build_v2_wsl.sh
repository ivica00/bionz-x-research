#!/bin/bash
# Build OpenGateA1 as an SMOD v2 (relocatable) mod for Mod Loader v2.
# Usage: build_v2_wsl.sh OUTPUT_DIR
set -euo pipefail
cd "$(dirname "$0")/../.."
OUT=$1
mkdir -p "$OUT"
DIR=mods/opengate_a1
common=(-mthumb -mcpu=cortex-a5 -mfloat-abi=softfp -Os -ffreestanding -fno-builtin
        -nostdlib -nostartfiles -fno-stack-protector -fomit-frame-pointer -Wall -Wextra -Werror
        -fpie -fvisibility=hidden)

arm-linux-gnueabi-gcc "${common[@]}" -c "$DIR/opengate_a1.c" -o "$OUT/oga1.o"
arm-linux-gnueabi-gcc "${common[@]}" -c "$DIR/opengate_a1_trampoline.S" -o "$OUT/oga1_tramp.o"

for o in "$OUT/oga1.o" "$OUT/oga1_tramp.o"; do
  if arm-linux-gnueabi-readelf -rW "$o" | grep -E "R_ARM_(ABS32|GOT|TARGET1|PLT32|JUMP_SLOT|GLOB_DAT)"; then
    echo "$o has absolute or GOT relocations; it would not be position-independent" >&2
    exit 1
  fi
done

arm-linux-gnueabi-ld --build-id=none -T tools/native_camera/mlv2/mod_v2.ld \
  "$OUT/oga1.o" "$OUT/oga1_tramp.o" -o "$OUT/oga1.elf"

if arm-linux-gnueabi-objdump -d "$OUT/oga1.elf" | grep -qE "<(memcpy|memset|__aeabi_[a-z]*)>"; then
  echo "unexpected runtime helper in oga1.elf" >&2
  exit 1
fi

python3 -m tools.research.pack_mod_v2 \
  --elf "$OUT/oga1.elf" \
  --name "OpenGate A1" \
  --on oga1_on \
  --off oga1_off \
  --hook "0x079a52ac:f8b50446d0f8d85000af0dbb:tkhd_hook_tramp" \
  --hook "0x079a55f4:f0b593b00c4600af06460b68:stsd_hook_tramp" \
  --data-symbol AV_PATCHES \
  --data-symbol VU_CELL_OFFS \
  --reloc-layout tools/native_camera/mlv2/reference_layout_ilce6400.txt \
  --out "$OUT/OpenGateA1.mod" | tee "$OUT/pack.json"
