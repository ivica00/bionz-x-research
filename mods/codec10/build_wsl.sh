#!/bin/bash
# Build Codec10 under WSL. Usage: mods/codec10/build_wsl.sh NEW_OUTPUT_DIR
#   host/   bench_codec, c10tool (x86-64, for tests and decoding)
#   arm/    bench_codec_sony   - dynamic, camera glibc 2.11 (tools/sony_sysroot), Cortex-A5, no NEON code
#           bench_codec_neon   - same plus the runtime-gated NEON row coder
#           bench_codec_static - static Ubuntu glibc (qemu-arm user mode only; the camera kernel
#                                3.0.27 is older than that glibc's minimum)
#           codec10_smod.o     - SMOD v2 PIE object check (no absolute relocations, no libc, no division)
set -euo pipefail
cd "$(dirname "$0")/../.."
OUT=$1
mkdir "$OUT" "$OUT/host" "$OUT/arm"
D=mods/codec10
WARN=(-Wall -Wextra -Werror -O3 -std=gnu11)

gcc "${WARN[@]}" -I"$D" "$D/codec10.c" "$D/bench_codec.c" -lpthread -lm -o "$OUT/host/bench_codec"
gcc "${WARN[@]}" -I"$D" "$D/codec10.c" "$D/c10tool.c" -o "$OUT/host/c10tool"

CC=arm-linux-gnueabi-gcc
ARM=(-mcpu=cortex-a5 -mfpu=vfpv3-d16 -mfloat-abi=softfp -marm -fno-math-errno)
SONY=(-nostdlib -fno-stack-protector -U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=0 -U_TIME_BITS -D_TIME_BITS=32
      -U_FILE_OFFSET_BITS -D_FILE_OFFSET_BITS=32
      -Wl,-e,_start,-z,defs,-z,max-page-size=4096,--dynamic-linker=/lib/ld-linux.so.3,--build-id=none,--hash-style=both)
SYSROOT=tools/sony_sysroot/lib
"$CC" "${WARN[@]}" "${ARM[@]}" "${SONY[@]}" -I"$D" "$D/start_sony.S" "$D/codec10.c" "$D/bench_codec.c" \
    "$SYSROOT/libpthread.so" "$SYSROOT/libc.so" "$SYSROOT/ld-linux.so.3" -lgcc -o "$OUT/arm/bench_codec_sony"
# NEON build: only codec10_neon.c gets -mfpu=neon; the rest is VFPv3-D16 code (no VFPv4 FMA: the QEMU cortex-a5 MVFR1 lacks it, see E0758).
"$CC" "${WARN[@]}" -mcpu=cortex-a5 -mfpu=neon -mfloat-abi=softfp -marm -c "$D/codec10_neon.c" -o "$OUT/arm/codec10_neon.o"
"$CC" "${WARN[@]}" "${ARM[@]}" "${SONY[@]}" -DC10_HAVE_NEON -I"$D" "$D/start_sony.S" "$D/codec10.c" "$D/bench_codec.c" \
    "$OUT/arm/codec10_neon.o" "$SYSROOT/libpthread.so" "$SYSROOT/libc.so" "$SYSROOT/ld-linux.so.3" -lgcc -o "$OUT/arm/bench_codec_neon"
"$CC" "${WARN[@]}" "${ARM[@]}" -static -DC10_HAVE_NEON -I"$D" "$D/codec10.c" "$D/bench_codec.c" \
    "$OUT/arm/codec10_neon.o" -lpthread -o "$OUT/arm/bench_codec_static"

# The camera binaries must not need anything newer than the camera's glibc 2.11.
for f in bench_codec_sony bench_codec_neon; do
    if arm-linux-gnueabi-objdump -T "$OUT/arm/$f" | grep -E "GLIBC_2\.(1[2-9]|[2-9][0-9])"; then
        echo "$f needs a newer glibc symbol than the camera has" >&2; exit 1
    fi
done
# The camera build must contain no NEON instruction (it would fault on the ILCE-6400).
if arm-linux-gnueabi-objdump -d "$OUT/arm/bench_codec_sony" | grep -qE "\s(vld1|vst1|vqsub|vclz|vshl|vmin|vmax)\.(u|s|i|8|16|32|64)"; then
    echo "bench_codec_sony contains NEON instructions" >&2; exit 1
fi

# SMOD v2 shape: freestanding Thumb PIE object, as Mod Loader v2 builds mods.
SMOD=(-mthumb -mcpu=cortex-a5 -mfloat-abi=softfp -O2 -ffreestanding -fno-builtin -nostdlib -nostartfiles
      -fno-stack-protector -fomit-frame-pointer -fpie -fvisibility=hidden -DC10_SMOD -Wall -Wextra -Werror)
"$CC" "${SMOD[@]}" -c "$D/codec10.c" -o "$OUT/arm/codec10_smod.o"
if arm-linux-gnueabi-readelf -rW "$OUT/arm/codec10_smod.o" | grep -E "R_ARM_(ABS32|GOT|TARGET1|PLT32|JUMP_SLOT|GLOB_DAT)"; then
    echo "codec10_smod.o has absolute or GOT relocations" >&2; exit 1
fi
if arm-linux-gnueabi-nm -u "$OUT/arm/codec10_smod.o" | grep -v "^\s*$"; then
    echo "codec10_smod.o has undefined symbols (libc or runtime helpers)" >&2; exit 1
fi
if [[ $(arm-linux-gnueabi-size -A "$OUT/arm/codec10_smod.o" | awk '$1 ~ /^\.(bss|data)/ {s += $2} END {print s+0}') != 0 ]]; then
    echo "codec10_smod.o has .data/.bss (SMOD v2 maps only code + --bss)" >&2; exit 1
fi
arm-linux-gnueabi-size "$OUT/arm/codec10_smod.o"
sha256sum "$OUT"/host/* "$OUT"/arm/* > "$OUT/SHA256SUMS"
echo "build OK: $OUT"
