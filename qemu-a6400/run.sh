#!/bin/bash
# Boot the main (nflasha3) a6400 firmware on the sony-a6400 QEMU machine.
#
# Reproduces what the Sony bootloader leaves in RAM before entering the
# kernel (E0521; same contract as Oein/a5000-emulator docs/BOOT.md):
#   - kernel Image at PA 0x00108000, initrd at 0x00700000 (board code)
#   - "kemco"+cmdline block at 0x00103000 (board code, from -append);
#     the cmdline is the main-boot /setting/kemco.txt, with only
#     console=ttynull swapped for the ttyAM0 console
#   - /setting/Backup.bin copied to the PA in its header word 0
#     (backup.ko Backup_load=0 expects it already in RAM)
#   - /system/av-cam.bin copied to 0x035C6000 (liro.ko adrs=035c6000)
# The eMMC image is never written: every run uses a fresh qcow2 overlay.
#
# Env overrides: A6400_QEMU_DIR, A6400_QEMU_DATA_DIR (verified prepared inputs),
# RUN_DIR, AVCAM (legacy mode only: path to av-cam.bin),
# TIMEOUT (seconds, 0 = none), EXTRA (appended cmdline tokens),
# QEMU_EXTRA (extra QEMU options; default "-icount shift=0,sleep=on" ties
# virtual time to executed instructions, E0522; set QEMU_EXTRA= to disable),
# EVSEL_LATE_US (opt-in source-30 delay in us, E0557; unset = off),
# CAMPIG_ENABLE_READBACK=1 (opt-in CamPig IRQ enable register, E0566),
# SGC1_EVENT_A=1 (opt-in Sgc1 event A and IRQ 35, E0569).
# P2S_TRACE=1 (log the first 64 sensor-writer transfers).
# USB_VBUS_TEST=1 (enable the runtime usb-vbus QMP property).
# USB_CID_HIGH=1 (opt-in FUSB CID bit 1 high / peripheral idle, E0587).
# DWC3_GADGET=1 (opt-in CXD90045 DWC3 gadget at 0xF020C000 and IRQ 229, E0590).
# REAR_CONTROLS=1 (route host keys to emulated rear camera controls).
# REAR_LCD_DIAGNOSTIC=1 (E0572 device select on the run's qcow2 overlay).
# SERIAL (QEMU -serial backend for ttyAM0; default mon:stdio). The mod tools use a unix socket.
# SETTING_FILES ("HOST_PATH:NAME ..." added to /setting on the run's disposable overlay only;
# host paths must not contain spaces). Mod development stages the guard and request this way.
# The machine models 4 Cortex-A5 cores by default (E0522); -smp 1 still works.
set -euo pipefail
E="$(cd "$(dirname "$0")" && pwd)"
D="$E/data"
PREPARED=0
if [ -n "${A6400_QEMU_DATA_DIR:-}" ]; then
    D="$(cd "$A6400_QEMU_DATA_DIR" && pwd)"
    PREPARED=1
    (cd "$E/.." && python3 -m tools.research.qemu_boot_inputs verify --report-dir "$D")
fi
if [ -n "${A6400_QEMU_DIR:-}" ]; then Q="$A6400_QEMU_DIR"
elif [[ "$E" == *" "* ]]; then Q="$HOME/sony-a6400-qemu"
else Q="$E/qemu"; fi
R="${RUN_DIR:-$HOME/a6400-runs}"
if [ "$PREPARED" = 1 ]; then
    if [ -n "${AVCAM:-}" ] && [ "$AVCAM" != "$D/av-cam.bin" ]; then
        echo "Prepared inputs do not permit an unverified AVCAM override." >&2
        exit 1
    fi
    AVCAM="$D/av-cam.bin"
    R="${RUN_DIR:-$HOME/a6400-runs/$(date -u +%Y%m%dT%H%M%SZ)-$$}"
    if [ -e "$R" ]; then
        echo "Refusing to overwrite an existing prepared-input run: $R" >&2
        exit 1
    fi
else
    AVCAM="${AVCAM:-$E/../firmware/extracted_a6400/firmware.tar_unpacked/0700_part_image/dev/nflasha3_unpacked/av-cam.bin}"
fi
AVCAM_PA=0x035C6000
mkdir -p "$R"

# QEMU's option parser splits on ',' and the repo path may contain spaces,
# so stage the preload inputs under $R.
tar xOf "$D/setting.tar" ./Backup.bin > "$R/Backup.bin"
cp "$AVCAM" "$R/av-cam.bin"
BACKUP_PA=$(printf '0x%08X' "$(od -An -tu4 -N4 "$R/Backup.bin" | tr -d ' ')")

if [ "$PREPARED" = 1 ]; then
    CMD=$(cat "$D/serial-cmdline.txt")
else
    CMD=$(tar xOf "$D/setting.tar" ./kemco.txt | tr '\r\n' '  ' | tr -s ' ' \
          | sed 's/ \*[[:space:]]*$//; s/^kemco //; s/console=ttynull/console=ttyAM0,115200n8 amba2.console=1/')
fi

rm -f "$R/emmc-overlay.qcow2"
"$Q/build/qemu-img" create -q -f qcow2 -F raw -b "$D/emmc.img" "$R/emmc-overlay.qcow2"

# /setting/ulogio.bin (E0525): without it osal_ulogio.ko fails, nobody serves
# the ulogio queue 0x13A, its message pool fills and every task that logs
# (av-cam task 65, bootin, im.elf THREAD_INFRA*) blocks in vpool_alloc.
# The a6400 package ships no ulogio.bin; the ZV-E10 (same CXD90045 platform)
# copy is used as a stand-in [assumption]; its LOGB/PRTB buffers fall inside
# the a6400 cmdline's wbimem=0x500000@0x03085000.  Set ULOGIO= to skip.
# nflasha2 = user-area 0x00800000 (+8 MiB of eMMC boot partitions in the file).
ULOGIO="${ULOGIO-$E/../firmware/extracted_zve10_v203_packman/firmware.tar_unpacked/0800_appli/setting/ulogio.bin}"
if [ -n "$ULOGIO" ] || [ -n "${SETTING_FILES:-}" ]; then
    SET_OFF=$((0x1000000)); SET_LEN=$((0x1400000))
    dd if="$D/emmc.img" of="$R/setting-part.img" bs=1M skip=$((SET_OFF >> 20)) \
       count=$((SET_LEN >> 20)) status=none
    if [ -n "$ULOGIO" ]; then
        python3 "$E/tools/fat_add_file.py" "$R/setting-part.img" "$ULOGIO" ulogio.bin >&2
    fi
    for item in ${SETTING_FILES:-}; do
        python3 "$E/tools/fat_add_file.py" "$R/setting-part.img" "${item%%:*}" "${item##*:}" >&2
    done
    # qemu-io splits the -c string on whitespace, including within paths.
    # Use a relative source name so a run directory with spaces works.
    QEMU_IO="$(cd "$Q/build" && pwd)/qemu-io"
    (cd "$R" && "$QEMU_IO" -f qcow2 \
        -c "write -q -s setting-part.img $SET_OFF $SET_LEN" emmc-overlay.qcow2)
fi

# E0572 is an opt-in firmware diagnostic applied only to the disposable overlay.
if [ "${REAR_LCD_DIAGNOSTIC:-0}" = 1 ]; then
    LCD_OFF=$(python3 "$E/tools/rear_lcd_overlay.py" "$D/emmc.img" "$R/rear-lcd-sector.bin")
    QEMU_IO="$(cd "$Q/build" && pwd)/qemu-io"
    (cd "$R" && "$QEMU_IO" -f qcow2 \
        -c "write -q -s rear-lcd-sector.bin $LCD_OFF 512" emmc-overlay.qcow2)
fi

# Sub-micro frames (E0527): av-cam's micro->CPU RX slots A/C/B
# (PA 0x034433A4, 0x03443424, 0x034434A4; 384 bytes), cut from the 4 KiB
# /dev/mem page captured read-only on a
# live ILCE-6400.  Replayed on SIO ch3 by the board.  Set SUBMICRO_PAGE= to
# run without a micro (the power lever then reads OFF, off factor 7).
SUBMICRO_PAGE="${SUBMICRO_PAGE-$E/../artifacts/qemu-a6400/e0526/camera-page-0x03443000.bin}"
SUBMICRO_SHA=14fba4e607b0efdb76ced74e0463355a279146c96bbba0abec89776b8972fb54
MACHINE=sony-a6400
if [ -n "$SUBMICRO_PAGE" ]; then
    echo "$SUBMICRO_SHA  $SUBMICRO_PAGE" | sha256sum -c --quiet - >&2
    dd if="$SUBMICRO_PAGE" bs=1 skip=$((0x3A4)) count=384 status=none \
        > "$R/submicro-frames.bin"
    MACHINE="sony-a6400,submicro-frames=$R/submicro-frames.bin"
fi
if [ "${CAMPIG_ENABLE_READBACK:-0}" = 1 ]; then
    MACHINE="$MACHINE,campig-enable-readback=on"
fi
if [ "${SGC1_EVENT_A:-0}" = 1 ]; then
    MACHINE="$MACHINE,sgc1-event-a=on"
fi
if [ "${REAR_CONTROLS:-0}" = 1 ]; then
    MACHINE="$MACHINE,rear-controls=on"
fi
if [ "${USB_VBUS_TEST:-0}" = 1 ]; then
    MACHINE="$MACHINE,usb-vbus-test=on"
fi
if [ "${USB_CID_HIGH:-0}" = 1 ]; then
    MACHINE="$MACHINE,usb-cid-high=on"
fi
if [ "${DWC3_GADGET:-0}" = 1 ]; then
    MACHINE="$MACHINE,dwc3-gadget=on"
fi

# Evsel source order (E0557, opt-in): deliver timing-event source 30 (av-cam
# HS root 0x639242D8) EVSEL_LATE_US microseconds after source 29 (frame start
# 0x63924A38) instead of in the same pulse.  The real phase is unknown
# [assumption]; av-cam only needs 30 after 29 and before the first int_HS slot
# step (~4170 us).  Unset = off (the a5000 single-pulse model).
EVSEL=()
if [ -n "${EVSEL_LATE_US:-}" ]; then
    EVSEL=(-global cxd90014-evsel.late-src=30
           -global cxd90014-evsel.late-us="$EVSEL_LATE_US")
fi

P2S=()
if [ "${P2S_TRACE:-0}" = 1 ]; then
    P2S=(-global cxd90014-p2s.trace-transfers=on)
fi

echo "Backup.bin -> $BACKUP_PA, av-cam.bin -> $AVCAM_PA, overlay $R/emmc-overlay.qcow2" >&2
T=()
if [ "${TIMEOUT:-0}" != 0 ]; then T=(timeout "$TIMEOUT"); fi
exec "${T[@]}" "$Q/build/qemu-system-arm" -M "$MACHINE" -m 1G -nographic -serial "${SERIAL:-mon:stdio}" \
  -kernel "$D/nflasha3-Image" -initrd "$D/nflasha3-initrd" \
  -append "$CMD ${EXTRA:-}" \
  -drive if=sd,format=qcow2,file="$R/emmc-overlay.qcow2" \
  -device loader,file="$R/Backup.bin",addr="$BACKUP_PA",force-raw=on \
  -device loader,file="$R/av-cam.bin",addr="$AVCAM_PA",force-raw=on \
  "${EVSEL[@]}" "${P2S[@]}" \
  ${QEMU_EXTRA--icount shift=0,sleep=on}
