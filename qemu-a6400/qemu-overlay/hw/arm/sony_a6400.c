/*
 * Sony ILCE-6400 (BIONZ X / CXD90045 / ARM-CXD900X0) Machine Model
 *
 * Initial Milestone 1 bring-up machine derived from verified CXD90045
 * L2 hardware contracts (src/a6400/machine/soc_cxd90045.py):
 *   - Machine ID: 0x00000DA9 (3497, ARM-CXD900X0)
 *   - RAM bank 0: 0x00000000 .. 0x40000000 (1 GiB)
 *   - RAM bank 1: 0x80000000 .. 0xBF800000 (1016 MiB)
 *   - SRAM:       0xFE000000 .. 0xFE100000 (1 MiB)
 *   - A9MPCore:   0xF8000000 (GIC CPU @ 0xF8000100, GIC Dist @ 0xF8001000)
 *   - PL011 UART0:0xF2000000 (IRQ 150 -> SPI 118)
 *   - CXD4115 timer: 0xF2403000, channel n -> IRQ 153 + n (E0522)
 *   - 4 x Cortex-A5 (E0522: secondaries released through SRAM mailbox
 *     0xFE005010 + 4 * (cpu - 1) by cxd900x0_smp_init)
 *   - CRCDC Clk:  0xF2A31000 (+0x30 bit 0 ready)
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "qapi/error.h"
#include "hw/arm/boot.h"
#include "hw/loader.h"
#include "qemu/error-report.h"
#include "system/reset.h"
#include "hw/boards.h"
#include "hw/irq.h"
#include "hw/char/pl011.h"
#include "hw/cpu/a9mpcore.h"
#include "hw/misc/unimp.h"
#include "hw/qdev-properties.h"
#include "monitor/qdev.h"
#include "hw/sd/sd.h"
#include "hw/sd/sdhci.h"
#include "hw/misc/cxd90014_sio.h"
#include "hw/misc/cxd90014_adc.h"
#include "hw/sysbus.h"
#include "system/blockdev.h"
#include "qemu/log.h"
#include "qemu/timer.h"
#include "qemu/units.h"
#include "system/address-spaces.h"
#include "system/system.h"
#include "target/arm/cpu.h"
#include "ui/input.h"

#define CXD90045_MACHINE_ID      0x00000DA9
#define CXD90045_RAM0_BASE       0x00000000ULL
#define CXD90045_RAM0_SIZE       (1 * GiB)
#define CXD90045_RAM1_BASE       0x80000000ULL
#define CXD90045_RAM1_SIZE       (1016 * MiB)
#define CXD90045_SRAM_BASE       0xFE000000ULL
#define CXD90045_SRAM_SIZE       (1 * MiB)

#define CXD90045_PERIPH_BASE     0xF8000000ULL
#define CXD90045_UART0_BASE      0xF2000000ULL
#define CXD90045_UART0_SPI       (150 - 32)
#define CXD90045_TIMER_BASE      0xF2403000ULL
#define CXD90045_TIMER_IRQ0      153
#define CXD90045_MAX_CPUS        4
#define CXD90045_SIO_BASE        0xF2405000ULL
#define CXD90045_SIO_IRQ0        201
#define CXD90045_GPIO_BASE       0xF2910000ULL
#define CXD90045_ADC_BASE        0xF2406000ULL
#define CXD90045_ADC_IRQ0        215

/*
 * GPIO input levels at reset (E0526), "port.pin" pins that read 1:
 *  - input.ko GPIO keys marked active low ("L") in the a6400 key table
 *    (/proc/udm/input): P0.3 (0x70FD), P0.4 (0x70FE), P0.7 (0x70DC),
 *    P0.10 (0x1F0 MOVIE), P0.11 (0x70C6), P1.1 (0x70EF) -> released;
 *  - P0.1, P0.13, P1.5: a5000 shutter S1/S2 lines (active low, idle high);
 *    the a6400 av-cam registers the matching pin IRQs 122/142 [inference];
 *  - P1.4: E-mount lens detect (a6400 Backup F9050C/0D = port 1 pin 4,
 *    IRQ 141 registered); active low on the a5000, high = empty mount
 *    [inference for the polarity]. Holding it low (lens present, unanswered)
 *    did not make av-cam start an SA function before SetContentTarget.
 * P9.22 carries both an "H" key (0x4C) and an "L" key (0x4B), i.e. a
 * two-position switch; it is left at 0 (0x4B side) [assumption].
 *
 * Sub-micro link lines (a6400 Backup ids, cf. a5000 RE_SUBMICRO_LINK.md):
 * request F904CA/CB = P2.5 (a5000 p3.17), strobe F90465/66 = P0.8 / IRQ 129
 * (a5000 p0.12), release-lock F9045F/60 = P0.9 / IRQ 130 (a5000 p0.7),
 * exposure F90200/01 = P1.2 / IRQ 139 (a5000 p0.16), S2REQ F90455/56 =
 * P7.15 (CPU output), CS F900B1/6D = P5.14.  All idle low.
 */
#define CXD90045_GPIO_RESET_HIGH "0.1,0.3,0.4,0.7,0.10,0.11,0.13,1.1,1.4,1.5"
#define CXD90045_SM_REQ_LINE     (2 * 32 + 5)
#define CXD90045_SM_STROBE_LINE  (0 * 32 + 8)
#define CXD90045_CRCDC_BASE      0xF2A31000ULL
#define CXD90045_GPES_BAR_BASE   0xF1000000ULL
#define CXD90045_GPES_SPI        (76 - 32)
#define CXD90045_SUGI_CLK_BASE   0xF3400000ULL
#define CXD90045_DFS_CTRL_BASE   0xF2916000ULL
#define CXD90045_L2C_STUB_BASE   0xF8002000ULL
#define CXD90045_EMMC0_BASE      0xF0304000ULL
#define CXD90045_EMMC0_SPI       (227 - 32)
#define CXD90045_XDMAC0_BASE     0xF0000000ULL
#define CXD90045_XDMAC0_SPI_BASE (189 - 32)
#define CXD90045_XDMAC1_BASE     0xF0010000ULL
#define CXD90045_XDMAC1_SPI_BASE (193 - 32)
#define CXD90045_LDEC_BASE       0xF2903000ULL
#define CXD90045_LDEC_IN_BASE    0xF0020000ULL
#define CXD90045_LDEC_IN_SIZE    0x10000ULL
#define CXD90045_LDEC_SPI        (171 - 32)
#define CXD90045_F2A01_BASE      0xF2A01000ULL

#define CXD90045_NUM_IRQS        256

static uint64_t cxd90045_crcdc_read(void *opaque, hwaddr offset, unsigned size)
{
    /* +0x30 bit 0 ready after clock control writes (E0423) */
    if (offset == 0x30) {
        return 0x1;
    }
    return 0;
}

static void cxd90045_crcdc_write(void *opaque, hwaddr offset, uint64_t value,
                                 unsigned size)
{
}

static const MemoryRegionOps cxd90045_crcdc_ops = {
    .read = cxd90045_crcdc_read,
    .write = cxd90045_crcdc_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

static uint64_t cxd90045_l2c_stub_read(void *opaque, hwaddr offset, unsigned size)
{
    /* Observed read stub 0xF800277C -> 0x100 */
    if (offset == 0x77C) {
        return 0x100;
    }
    return 0;
}

static void cxd90045_l2c_stub_write(void *opaque, hwaddr offset, uint64_t value,
                                    unsigned size)
{
}

static const MemoryRegionOps cxd90045_l2c_stub_ops = {
    .read = cxd90045_l2c_stub_read,
    .write = cxd90045_l2c_stub_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

typedef struct CxdRwStub {
    MemoryRegion mmio;
    uint32_t *regs;
    size_t num_regs;
} CxdRwStub;

static uint64_t cxd90045_rw_stub_read(void *opaque, hwaddr offset, unsigned size)
{
    CxdRwStub *s = opaque;
    size_t idx = offset >> 2;
    if (idx < s->num_regs) {
        return s->regs[idx];
    }
    return 0;
}

static void cxd90045_rw_stub_write(void *opaque, hwaddr offset, uint64_t value,
                                   unsigned size)
{
    CxdRwStub *s = opaque;
    size_t idx = offset >> 2;
    if (idx < s->num_regs) {
        s->regs[idx] = (uint32_t)value;
    }
}

static const MemoryRegionOps cxd90045_rw_stub_ops = {
    .read = cxd90045_rw_stub_read,
    .write = cxd90045_rw_stub_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

static CxdRwStub *cxd90045_create_rw_stub(MemoryRegion *sysmem, const char *name,
                                         hwaddr base, size_t size, int priority)
{
    CxdRwStub *s = g_new0(CxdRwStub, 1);
    s->num_regs = size / sizeof(uint32_t);
    s->regs = g_new0(uint32_t, s->num_regs);
    memory_region_init_io(&s->mmio, NULL, &cxd90045_rw_stub_ops, s, name, size);
    memory_region_add_subregion_overlap(sysmem, base, &s->mmio, priority);
    return s;
}

/*
 * Sugilite / DMP SMAPH-S30 is hw/display/dmp_smaph.c (ported from
 * Oein/a5000-emulator).  It executes PSC fills, PPF copies and P3D
 * command lists, and raises IRQ 76 from status +0x34.  The previous
 * register stub completed jobs without writing a pixel.
 */

typedef struct CxdLdec {
    MemoryRegion mmio;
    qemu_irq irq;
    uint32_t regs[0x100 >> 2];
    uint8_t *in_buf;
    size_t in_cap;
    size_t in_len;
} CxdLdec;

static CxdLdec *s_cxd_ldec;

static bool cxd90045_ldec_lz77_decode(const uint8_t *in, size_t in_len,
                                      uint8_t *out, size_t max_out,
                                      size_t *out_len_p)
{
    static const uint32_t lengths[16] = {
        3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 32, 64
    };
    size_t pos = 0;
    size_t out_len = 0;
    while (pos < in_len && out_len < max_out) {
        uint8_t kind = in[pos++];
        if (kind == 0x0f) {
            if (pos + 3 > in_len) {
                return false;
            }
            pos++; /* skip 1 byte */
            uint32_t count = in[pos] | ((uint32_t)in[pos + 1] << 8);
            pos += 2;
            if (pos + count > in_len || out_len + count > max_out) {
                return false;
            }
            memcpy(out + out_len, in + pos, count);
            pos += count;
            out_len += count;
        } else if (kind == 0xf0) {
            bool done = false;
            while (!done) {
                if (pos >= in_len) {
                    return false;
                }
                uint8_t flags = in[pos++];
                if (flags == 0) {
                    if (pos + 8 > in_len || out_len + 8 > max_out) {
                        return false;
                    }
                    memcpy(out + out_len, in + pos, 8);
                    pos += 8;
                    out_len += 8;
                    continue;
                }
                for (int bit = 0; bit < 8; bit++) {
                    if ((flags >> bit) & 1) {
                        if (pos + 2 > in_len) {
                            return false;
                        }
                        uint8_t a = in[pos++];
                        uint8_t b = in[pos++];
                        uint32_t dist = ((uint32_t)(a & 0x0f) << 8) | b;
                        if (dist == 0) {
                            done = true;
                            break;
                        }
                        uint32_t len = lengths[a >> 4];
                        if (dist > out_len || out_len + len > max_out) {
                            return false;
                        }
                        for (uint32_t k = 0; k < len; k++) {
                            out[out_len] = out[out_len - dist];
                            out_len++;
                        }
                    } else {
                        if (pos >= in_len || out_len >= max_out) {
                            return false;
                        }
                        out[out_len++] = in[pos++];
                    }
                }
            }
        } else {
            return false;
        }
    }
    *out_len_p = out_len;
    return true;
}

static void cxd90045_ldec_feed(uint32_t dsa_off, uint32_t ssa, uint32_t len)
{
    CxdLdec *s = s_cxd_ldec;
    if (!s || len == 0 || len > (16 * 1024 * 1024) || dsa_off > (16 * 1024 * 1024)) {
        return;
    }
    if (dsa_off == 0) {
        s->in_len = 0;
    }
    size_t need = (size_t)dsa_off + (size_t)len;
    if (need > s->in_cap) {
        s->in_cap = need < 65536 ? 65536 : need * 2;
        s->in_buf = g_realloc(s->in_buf, s->in_cap);
    }
    cpu_physical_memory_read(ssa, s->in_buf + dsa_off, len);
    if (need > s->in_len) {
        s->in_len = need;
    }
    uint32_t records = s->regs[0x0c >> 2];
    uint32_t dst_phys = s->regs[0x10 >> 2];
    size_t max_out = (records > 0 && records <= 4096) ? (size_t)records * 4096 : (16 * 1024 * 1024);
    if (dst_phys != 0) {
        uint8_t *out_buf = g_malloc0(max_out);
        size_t decoded = 0;
        bool ok = cxd90045_ldec_lz77_decode(s->in_buf, s->in_len, out_buf, max_out, &decoded);
        if (ok && decoded > 0) {
            static int ldec_ok = 0;
            if (ldec_ok < 5) {
                fprintf(stderr,
                        "[LDEC OK #%d] in=0x%zx -> dst=0x%x out=0x%zx (rec=%u)\n",
                        ldec_ok, s->in_len, dst_phys, decoded, records);
                ldec_ok++;
            }
            cpu_physical_memory_write(dst_phys, out_buf, decoded);
            s->in_len = 0;
            s->regs[0x34 >> 2] = 0x1;
            qemu_irq_raise(s->irq);
        }
        g_free(out_buf);
    }
}

static uint64_t cxd90045_ldec_read(void *opaque, hwaddr offset, unsigned size)
{
    CxdLdec *s = opaque;
    size_t idx = offset >> 2;
    if (idx < ARRAY_SIZE(s->regs)) {
        return s->regs[idx];
    }
    return 0;
}

static void cxd90045_ldec_write(void *opaque, hwaddr offset, uint64_t value,
                                unsigned size)
{
    CxdLdec *s = opaque;
    size_t idx = offset >> 2;
    if (idx < ARRAY_SIZE(s->regs)) {
        s->regs[idx] = (uint32_t)value;
    }
    if (offset == 0x30) {
        s->regs[0x34 >> 2] &= ~(uint32_t)value;
        if ((s->regs[0x34 >> 2] & 0x7bU) == 0) {
            qemu_irq_lower(s->irq);
        }
    }
}

static const MemoryRegionOps cxd90045_ldec_ops = {
    .read = cxd90045_ldec_read,
    .write = cxd90045_ldec_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

static uint64_t cxd90045_f2a01_read(void *opaque, hwaddr offset, unsigned size)
{
    uint32_t *regs = opaque;
    if (offset == 0x04) {
        return regs[0x04 >> 2] | 0x1U;
    }
    if (offset == 0x2c) {
        return regs[0x2c >> 2] | 0x2U;
    }
    if (offset == 0x30) {
        return 0;
    }
    if ((offset >> 2) < 0x400) {
        return regs[offset >> 2];
    }
    return 0;
}

static void cxd90045_f2a01_write(void *opaque, hwaddr offset, uint64_t value,
                                 unsigned size)
{
    uint32_t *regs = opaque;
    if (offset == 0x30) {
        return; /* self-clearing trigger */
    }
    if ((offset >> 2) < 0x400) {
        regs[offset >> 2] = (uint32_t)value;
    }
}

static const MemoryRegionOps cxd90045_f2a01_ops = {
    .read = cxd90045_f2a01_read,
    .write = cxd90045_f2a01_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

/*
 * Leo clock generator at 0xFC021000 (av-cam 0x63782020).  Enable bits are
 * written at +0x000; PLL lock is polled at +0x050/+0x054/+0x058 bit0, up to
 * 1001 tight reads.  Unmapped reads are 0, so that poll logs
 * "IMG:Timeout:Leo Clkgen PLL Wait" and the imager init stops there (E0534).
 * Lock is always reported set.  Other offsets read back what was written.
 */
static uint64_t cxd90045_leo_read(void *opaque, hwaddr offset, unsigned size)
{
    uint32_t *regs = opaque;
    uint32_t v = 0;

    if ((offset >> 2) < 0x400) {
        v = regs[offset >> 2];
    }
    /* +0x050/054/058 bit0: Leo PLL lock (av-cam 0x63782020). */
    if (offset == 0x50 || offset == 0x54 || offset == 0x58) {
        v |= 1;
    }
    return v;
}

static void cxd90045_leo_write(void *opaque, hwaddr offset, uint64_t value,
                               unsigned size)
{
    uint32_t *regs = opaque;

    if ((offset >> 2) < 0x400) {
        regs[offset >> 2] = (uint32_t)value;
    }
}

static const MemoryRegionOps cxd90045_leo_ops = {
    .read = cxd90045_leo_read,
    .write = cxd90045_leo_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

static void cxd90045_create_ldec(MemoryRegion *sysmem, hwaddr base, qemu_irq irq)
{
    CxdLdec *s = g_new0(CxdLdec, 1);
    s->irq = irq;
    s_cxd_ldec = s;
    memory_region_init_io(&s->mmio, NULL, &cxd90045_ldec_ops, s,
                          "cxd90045.ldec", 0x1000);
    memory_region_add_subregion(sysmem, base, &s->mmio);
}

typedef struct CxdXdmac {
    MemoryRegion mmio;
    qemu_irq irqs[4];
    uint32_t regs[0x100 >> 2];
} CxdXdmac;

static uint64_t cxd90045_xdmac_read(void *opaque, hwaddr offset, unsigned size)
{
    CxdXdmac *s = opaque;
    size_t idx = offset >> 2;
    if (idx < ARRAY_SIZE(s->regs)) {
        return s->regs[idx];
    }
    return 0;
}

static void cxd90045_xdmac_write(void *opaque, hwaddr offset, uint64_t value,
                                 unsigned size)
{
    CxdXdmac *s = opaque;
    size_t idx = offset >> 2;
    if (idx < ARRAY_SIZE(s->regs)) {
        s->regs[idx] = (uint32_t)value;
    }
    if (offset >= 0x10 && offset < 0x10 + 4 * 0x30) {
        int ch = (int)((offset - 0x10) / 0x30);
        hwaddr ch_off = (offset - 0x10) % 0x30;
        hwaddr ch_base = 0x10 + (hwaddr)ch * 0x30;
        if (ch_off == 0x18 && value != 0) {
            uint32_t tbc = s->regs[(ch_base + 0x00) >> 2] + 1;
            uint32_t ssa = s->regs[(ch_base + 0x04) >> 2];
            uint32_t dsa = s->regs[(ch_base + 0x08) >> 2];
            if (dsa >= CXD90045_LDEC_IN_BASE &&
                dsa < CXD90045_LDEC_IN_BASE + CXD90045_LDEC_IN_SIZE) {
                cxd90045_ldec_feed((uint32_t)(dsa - CXD90045_LDEC_IN_BASE),
                                   ssa, tbc);
            } else if (tbc > 0 && tbc <= (16 * 1024 * 1024)) {
                uint8_t buf[4096];
                uint32_t rem = tbc;
                uint32_t off = 0;
                while (rem > 0) {
                    uint32_t chunk = rem > sizeof(buf) ? sizeof(buf) : rem;
                    cpu_physical_memory_read(ssa + off, buf, chunk);
                    cpu_physical_memory_write(dsa + off, buf, chunk);
                    off += chunk;
                    rem -= chunk;
                }
            }
            s->regs[(ch_base + 0x20) >> 2] = 0x8;
            qemu_irq_raise(s->irqs[ch]);
        } else if (ch_off == 0x20 && value == 0) {
            s->regs[(ch_base + 0x20) >> 2] = 0;
            qemu_irq_lower(s->irqs[ch]);
        }
    }
}

static const MemoryRegionOps cxd90045_xdmac_ops = {
    .read = cxd90045_xdmac_read,
    .write = cxd90045_xdmac_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

static void cxd90045_create_xdmac(MemoryRegion *sysmem, hwaddr base,
                                  qemu_irq *irqs)
{
    CxdXdmac *s = g_new0(CxdXdmac, 1);
    for (int i = 0; i < 4; i++) {
        s->irqs[i] = irqs[i];
    }
    memory_region_init_io(&s->mmio, NULL, &cxd90045_xdmac_ops, s,
                          "cxd90045.xdmac", 0x10000);
    memory_region_add_subregion(sysmem, base, &s->mmio);
}

/*
 * Sony boot contract (boot/loadaddr.txt "0x00108000,0x00700000,0x00103000",
 * E0425): the raw kernel Image runs at physical 0x00108000 (TEXT_OFFSET),
 * the initrd sits at 0x00700000, and r2 points at a NUL-terminated
 * "kemco ..." parameter string at 0x00103000 instead of ATAGS/DTB.
 * setup_arch() checks strncmp(ptr, "kemco", 5) and parses from ptr+5.
 * QEMU's generic arm_load_kernel() cannot express this layout, so the
 * machine loads the images itself and enters the kernel on CPU reset.
 */
#define CXD90045_KERNEL_LOAD     0x00108000ULL
#define CXD90045_INITRD_LOAD     0x00700000ULL
#define CXD90045_INITRD_MAX      0x00800000ULL
#define CXD90045_BOOT_PARAMS     0x00103000ULL
#define CXD90045_BOOT_PARAMS_MAX (CXD90045_KERNEL_LOAD - CXD90045_BOOT_PARAMS)

/*
 * Boot stubs in the reserved first megabyte (memrsv=1M@0), standing in for
 * what the boot ROM / bootloader does before the kernel runs (E0522):
 *
 *   CPU0 (CXD90045_STUB0): program GICD_ITARGETSR8..63, make the
 *     ack-less frame pulses SYSV 98/99 and vsync 101/102 edge-triggered
 *     (GICD_ICFGR6 = 0x28A0, as the a5000 board stub does), then r0 = 0,
 *     r1 = machine id, r2 = kemco block, jump to the kernel.  Sony's
 *     gic_dist_init never writes SPI targets and its irq_select_affinity
 *     (0x6011feb0) only reads the ITARGETSR byte back into the affinity
 *     mask, so the targets must already be set when the kernel starts.
 *   CPU1..3 (CXD90045_SMPSTUB): WFE until cxd900x0_smp_init stores the
 *     physical cxd900x0_secondary_entry at 0xFE005010 + 4 * (cpu - 1)
 *     and issues SEV, then jump there.  The kernel's own park loop then
 *     waits for pen_release (boot_secondary).
 *
 * Encodings assembled with arm-linux-gnueabi-as (receipt in E0522).
 */
#define CXD90045_STUB0           0x00001000ULL
#define CXD90045_SMPSTUB         0x00001200ULL
#define CXD90045_SMP_MBOX        0xFE005010ULL

static const uint32_t cxd90045_stub0[20] = {
    0xe59f3030,             /* ldr r3, =0xF8001820 (ITARGETSR8)  */
    0xe59f4030,             /* ldr r4, =0xF8001900               */
    0xe28f6040,             /* add r6, pc, #0x40 (table @ +0x50) */
    0xe4965004,             /* 1: ldr r5, [r6], #4               */
    0xe4835004,             /* str r5, [r3], #4                  */
    0xe1530004,             /* cmp r3, r4                        */
    0x3afffffb,             /* blo 1b                            */
    0xe59f301c,             /* ldr r3, =0xF8001C18 (ICFGR6)      */
    0xe59f501c,             /* ldr r5, =0x28A0                   */
    0xe5835000,             /* str r5, [r3]                      */
    0xe3a00000,             /* mov r0, #0                        */
    0xe59f1014,             /* ldr r1, =CXD90045_MACHINE_ID      */
    0xe59f2014,             /* ldr r2, =CXD90045_BOOT_PARAMS     */
    0xe3a0f942,             /* mov pc, #0x108000 (kernel entry)  */
    0xf8001820,
    0xf8001900,
    0xf8001c18,
    0x000028a0,             /* IRQ 98, 99, 101, 102 edge (E0529) */
    CXD90045_MACHINE_ID,
    0x00103000,
};

static const uint32_t cxd90045_smpstub[11] = {
    0xee103fb0,             /* mrc p15, 0, r3, c0, c0, 5 (MPIDR) */
    0xe2033003,             /* and r3, r3, #3                    */
    0xe2433001,             /* sub r3, r3, #1                    */
    0xe59f2014,             /* ldr r2, =0xFE005010               */
    0xe0822103,             /* add r2, r2, r3, lsl #2            */
    0xe320f002,             /* 1: wfe                            */
    0xe5920000,             /* ldr r0, [r2]                      */
    0xe3500000,             /* cmp r0, #0                        */
    0x0afffffb,             /* beq 1b                            */
    0xe12fff10,             /* bx r0                             */
    0xfe005010,
};

/*
 * SPI target bytes for IRQ 32..255.  Per-CPU sources come from this kernel:
 * clockevent channels ch0/ch2/ch3/ch4 = IRQ 153/155/156/157 for CPU0..3
 * (table 0x604d8d80) and hwtimer0..3 = ch5..8 = IRQ 158..161
 * (hwtimer_devs 0x603c9354; hwtimerN serving CPU N is inferred, and agrees
 * with the a5000 real-camera GICD dump in Oein/a5000-emulator).  Every
 * other SPI targets CPU0 [assumption: no CXD90045 GICD dump yet].
 */
static void cxd90045_spi_targets(uint32_t *words, unsigned ncpu)
{
    uint8_t t[256 - 32];
    static const struct { int irq; unsigned cpu; } percpu[] = {
        { 153, 0 }, { 155, 1 }, { 156, 2 }, { 157, 3 },
        { 158, 0 }, { 159, 1 }, { 160, 2 }, { 161, 3 },
    };

    memset(t, 0x01, sizeof(t));
    for (size_t i = 0; i < ARRAY_SIZE(percpu); i++) {
        if (percpu[i].cpu < ncpu) {
            t[percpu[i].irq - 32] = 1u << percpu[i].cpu;
        }
    }
    for (int i = 0; i < 56; i++) {
        words[i] = cpu_to_le32(t[4 * i] | (t[4 * i + 1] << 8) |
                               (t[4 * i + 2] << 16) |
                               ((uint32_t)t[4 * i + 3] << 24));
    }
}

static void sony_a6400_write_stubs(unsigned ncpu)
{
    uint32_t blob0[20 + 56];
    uint32_t smp[11];

    for (int i = 0; i < 20; i++) {
        blob0[i] = cpu_to_le32(cxd90045_stub0[i]);
    }
    cxd90045_spi_targets(&blob0[20], ncpu);
    rom_add_blob_fixed("sony_a6400.stub0", blob0, sizeof(blob0),
                       CXD90045_STUB0);
    for (int i = 0; i < 11; i++) {
        smp[i] = cpu_to_le32(cxd90045_smpstub[i]);
    }
    rom_add_blob_fixed("sony_a6400.smpstub", smp, sizeof(smp),
                       CXD90045_SMPSTUB);
}

static void sony_a6400_cpu_reset(void *opaque)
{
    ARMCPU *cpu = opaque;
    CPUState *cs = CPU(cpu);

    cpu_reset(cs);
    if (cs->cpu_index == 0) {
        /*
         * A reset sends the secondaries back to the mailbox loop; a stale
         * entry left by the previous kernel must not release them early.
         * SRAM is zero at power-on, which is the state reproduced here.
         */
        uint32_t mbox[3] = { 0 };
        address_space_write(&address_space_memory, CXD90045_SMP_MBOX,
                            MEMTXATTRS_UNSPECIFIED, mbox, sizeof(mbox));
        cpu_set_pc(cs, CXD90045_STUB0);
    } else {
        cpu_set_pc(cs, CXD90045_SMPSTUB);
    }
}

static void sony_a6400_load_boot_contract(MachineState *machine,
                                          ARMCPU **cpus, unsigned ncpu)
{
    const char *append = machine->kernel_cmdline ? machine->kernel_cmdline : "";
    g_autofree char *params = NULL;
    ssize_t size;

    for (unsigned i = 0; i < ncpu; i++) {
        qemu_register_reset(sony_a6400_cpu_reset, cpus[i]);
    }
    sony_a6400_write_stubs(ncpu);
    if (!machine->kernel_filename) {
        return;
    }

    size = load_image_targphys(machine->kernel_filename, CXD90045_KERNEL_LOAD,
                               CXD90045_INITRD_LOAD - CXD90045_KERNEL_LOAD);
    if (size <= 0) {
        error_report("sony-a6400: could not load kernel '%s'",
                     machine->kernel_filename);
        exit(1);
    }

    if (machine->initrd_filename) {
        size = load_image_targphys(machine->initrd_filename,
                                   CXD90045_INITRD_LOAD, CXD90045_INITRD_MAX);
        if (size <= 0) {
            error_report("sony-a6400: could not load initrd '%s'",
                         machine->initrd_filename);
            exit(1);
        }
    }

    params = g_str_has_prefix(append, "kemco") ? g_strdup(append)
                                                : g_strdup_printf("kemco %s", append);
    if (strlen(params) + 1 > CXD90045_BOOT_PARAMS_MAX) {
        error_report("sony-a6400: kemco boot parameters too long");
        exit(1);
    }
    rom_add_blob_fixed("sony_a6400.kemco", params, strlen(params) + 1,
                       CXD90045_BOOT_PARAMS);
}

/*
 * SIO slave used until a channel has a real peer model (E0526): logs every
 * transfer under -d unimp ("sony-a6400 sio chN") and leaves the FIFO as is,
 * which is what the a5000 SIO model does for a channel without a slave.
 */
/*
 * Sub-micro strobe (E0526), the a5000 board's a5k_submicro_tick applied to
 * the a6400 pins: the request line P2.5 stays low (frame exchange, not
 * command), and the strobe P0.8 gives a 1 ms pulse every 50 ms.  Each edge
 * is a link event 0 (IRQ 129).  The strobe must be low when the engine tick
 * samples it, or the CPU skips its slot-A exchange ("FrtComSkip").  The real
 * micro's timing is unknown [assumption: a5000 values].
 */
static DeviceState *sony_a6400_gpio;
static QEMUTimer *sony_a6400_sm_timer;
static int sony_a6400_sm_step;

static void sony_a6400_submicro_tick(void *opaque)
{
    qemu_set_irq(qdev_get_gpio_in(sony_a6400_gpio, CXD90045_SM_REQ_LINE), 0);
    sony_a6400_sm_step ^= 1;
    qemu_set_irq(qdev_get_gpio_in(sony_a6400_gpio, CXD90045_SM_STROBE_LINE),
                 sony_a6400_sm_step);
    timer_mod(sony_a6400_sm_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
              (sony_a6400_sm_step ? SCALE_MS : 49 * SCALE_MS));
}

static void sony_a6400_sio_log(void *opaque, int ch, uint32_t mode,
                               uint8_t *buf, int len)
{
    char hex[3 * 48 + 1];
    int n = len < 48 ? len : 48;

    for (int i = 0; i < n; i++) {
        snprintf(hex + 3 * i, 4, "%02x ", buf[i]);
    }
    hex[3 * n] = 0;
    qemu_log_mask(LOG_UNIMP, "sony-a6400 sio ch%d mode=0x%08x len=%d tx: %s\n",
                  ch, mode, len, hex);
    /*
     * Diagnostic only (A6400_SIO_MARKER=1): answer with a recognisable
     * marker so the RX buffer can be located in a RAM dump (E0526).
     */
    if (getenv("A6400_SIO_MARKER") && len >= 128) {
        /* type 0x30 slot-B frame, one TLV id 0x13 (12 B) = marker, CRC */
        static const char mark[] = "A6400RXMARK!";
        uint16_t c = 0xffff;
        memset(buf, 0, len);
        buf[0] = 0x30;
        buf[2] = 4 + 12;
        buf[4] = 0x13;
        buf[5] = 12;
        memcpy(buf + 8, mark, 12);
        for (int i = 0; i < 126; i++) {
            c ^= buf[i] << 8;
            for (int b = 0; b < 8; b++) {
                c = (c & 0x8000) ? (c << 1) ^ 0x1021 : c << 1;
            }
        }
        buf[126] = c;
        buf[127] = c >> 8;
    }
}

/*
 * Sub-micro side of the link (E0527): replay the real camera's frames.
 * "submicro-frames" names a 384-byte file = av-cam's micro->CPU link RX
 * buffer as captured from a live ILCE-6400 at PA 0x034433A4..0x03443523
 * (one read-only 4 KiB /dev/mem page): slot A (type 0x20, task-48 packet),
 * slot C (type 0x60) and slot B (type 0x30, TLV status frame), 128 B each,
 * all CRC-16/CCITT-FALSE valid.  The CPU's TX buffer holds 0x21 / 0x61 /
 * 0x31 in the same order.  Like the a5000 board (a5k_submicro_xfer), each
 * CPU frame is answered by its pair: 0x31 -> slot B, 0x61 -> slot C, any
 * other 128-byte frame -> slot A; a 256-byte exchange gets slot A + B.  Contents are
 * replayed as captured, never synthesised (the slot-A clock bytes stay at
 * the capture time [limitation]).
 */
static char *sony_a6400_sm_file;
static uint8_t sony_a6400_sm_frames[384];
static bool sony_a6400_sm_loaded;
static bool sony_a6400_rear_controls;
static DeviceState *sony_a6400_rear_adc;
static QKeyCode sony_a6400_adc_owner[2];
static uint16_t sony_a6400_front_pressed;
static bool sony_a6400_af_lever_low;
static uint16_t sony_a6400_wheel_count;

static uint64_t sony_a6400_wheel_read(void *opaque, hwaddr offset, unsigned size)
{
    return sony_a6400_wheel_count;
}

static void sony_a6400_wheel_write(void *opaque, hwaddr offset, uint64_t value,
                                  unsigned size)
{
    sony_a6400_wheel_count = value;
}

static const MemoryRegionOps sony_a6400_wheel_ops = {
    .read = sony_a6400_wheel_read,
    .write = sony_a6400_wheel_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 2, .max_access_size = 2 },
    .impl = { .min_access_size = 2, .max_access_size = 2 },
};

static void sony_a6400_rear_reset(void *opaque)
{
    memset(sony_a6400_adc_owner, 0, sizeof(sony_a6400_adc_owner));
    sony_a6400_front_pressed = 0;
    sony_a6400_af_lever_low = false;
    sony_a6400_wheel_count = 0;
}

static void sony_a6400_rear_key(DeviceState *dev, QemuConsole *src,
                               InputEvent *event)
{
    static const struct {
        QKeyCode key;
        unsigned channel;
        uint16_t level;
    } keys[] = {
        { Q_KEY_CODE_RET,   3, 45  },
        { Q_KEY_CODE_UP,    3, 182 },
        { Q_KEY_CODE_LEFT,  3, 356 },
        { Q_KEY_CODE_DOWN,  3, 520 },
        { Q_KEY_CODE_RIGHT, 3, 686 },
        { Q_KEY_CODE_DELETE, 0, 45 },
        { Q_KEY_CODE_F,     0, 520 },
    };
    InputKeyEvent *key = event->u.key.data;
    QKeyCode code = qemu_input_key_value_to_qcode(key->key);
    uint16_t front = 0;

    switch (code) {
    case Q_KEY_CODE_BRACKET_LEFT:
    case Q_KEY_CODE_BRACKET_RIGHT:
        if (key->down) {
            sony_a6400_wheel_count += code == Q_KEY_CODE_BRACKET_RIGHT ? 1 : -1;
        }
        return;
    case Q_KEY_CODE_A:
        qemu_set_irq(qdev_get_gpio_in(sony_a6400_gpio, 7), !key->down);
        return;
    case Q_KEY_CODE_B:
        if (key->down) {
            sony_a6400_af_lever_low = !sony_a6400_af_lever_low;
            qemu_set_irq(qdev_get_gpio_in(sony_a6400_gpio, 11),
                         !sony_a6400_af_lever_low);
        }
        return;
    case Q_KEY_CODE_M:
        front = BIT(2);
        break;
    case Q_KEY_CODE_P:
        front = BIT(1);
        break;
    case Q_KEY_CODE_L:
        front = BIT(9);
        break;
    case Q_KEY_CODE_R:
        qemu_set_irq(qdev_get_gpio_in(sony_a6400_gpio, 10), !key->down);
        return;
    default:
        break;
    }
    if (front) {
        if (key->down) {
            sony_a6400_front_pressed |= front;
        } else {
            sony_a6400_front_pressed &= ~front;
        }
        return;
    }
    for (size_t i = 0; i < ARRAY_SIZE(keys); i++) {
        if (keys[i].key != code) {
            continue;
        }
        unsigned channel = keys[i].channel;
        QKeyCode *owner = &sony_a6400_adc_owner[channel != 0];
        if (key->down) {
            *owner = code;
            cxd90014_adc_set(sony_a6400_rear_adc, channel, keys[i].level);
        } else if (*owner == code) {
            *owner = Q_KEY_CODE_UNMAPPED;
            cxd90014_adc_set(sony_a6400_rear_adc, channel, 1023);
        }
        return;
    }
}

static const QemuInputHandler sony_a6400_rear_handler = {
    .name = "Sony a6400 rear controls",
    .mask = INPUT_EVENT_MASK_KEY,
    .event = sony_a6400_rear_key,
};

static bool sony_a6400_get_rear_controls(Object *obj, Error **errp)
{
    return sony_a6400_rear_controls;
}

static void sony_a6400_set_rear_controls(Object *obj, bool value, Error **errp)
{
    sony_a6400_rear_controls = value;
}
static bool sony_a6400_campig_enable_readback;
static bool sony_a6400_sgc1_event_a;

/*
 * E0568: Sgc1 input sync experiment. The 36 MHz clock is inferred from
 * the programmed a6400 geometry and the nominal 33367 us frame period.
 */
#define SGC1_BASE 0xF3120000ULL
#define SGC1_CLOCK_HZ 36000000ULL
#define SGC1_START BIT(0)
#define SGC1_CONTINUOUS BIT(8)
#define SGC1_EVENT_A BIT(0)

typedef enum SonySgc1Register {
    SGC1_STATUS, SGC1_ENABLE, SGC1_CONTROL, SGC1_GEOMETRY, SGC1_COMPARE,
    SGC1_REGISTER_COUNT,
} SonySgc1Register;

typedef struct SonySgc1 SonySgc1;

typedef struct SonySgc1Word {
    MemoryRegion mr;
    SonySgc1 *sg;
    SonySgc1Register reg;
} SonySgc1Word;

struct SonySgc1 {
    SonySgc1Word words[SGC1_REGISTER_COUNT];
    uint32_t regs[SGC1_REGISTER_COUNT];
    QEMUTimer *timer;
    qemu_irq irq;
    int64_t frame_end;
    bool compare_pending;
};

static void sony_sgc1_update_irq(SonySgc1 *sg)
{
    qemu_set_irq(sg->irq, (sg->regs[SGC1_STATUS] &
                         sg->regs[SGC1_ENABLE] & SGC1_EVENT_A) != 0);
}

static void sony_sgc1_start_frame(SonySgc1 *sg, int64_t now)
{
    uint32_t geometry = sg->regs[SGC1_GEOMETRY];
    uint64_t lines = ((geometry >> 16) & 0x3fff) + 1;
    uint64_t clocks_per_line = (geometry & 0x3fff) + 1;
    uint64_t compare = sg->regs[SGC1_COMPARE];

    if (!geometry) {
        sg->regs[SGC1_CONTROL] &= ~SGC1_START;
        return;
    }

    sg->frame_end = now + DIV_ROUND_UP(lines * clocks_per_line *
                                       NANOSECONDS_PER_SECOND, SGC1_CLOCK_HZ);
    sg->compare_pending = compare < lines;
    if (sg->compare_pending) {
        timer_mod(sg->timer, now +
                  DIV_ROUND_UP(compare * clocks_per_line *
                               NANOSECONDS_PER_SECOND, SGC1_CLOCK_HZ));
    } else {
        timer_mod(sg->timer, sg->frame_end);
    }
}

static void sony_sgc1_tick(void *opaque)
{
    SonySgc1 *sg = opaque;

    if (sg->compare_pending) {
        sg->compare_pending = false;
        sg->regs[SGC1_STATUS] |= SGC1_EVENT_A;
        sony_sgc1_update_irq(sg);
        timer_mod(sg->timer, sg->frame_end);
    } else if (sg->regs[SGC1_CONTROL] & SGC1_CONTINUOUS) {
        sony_sgc1_start_frame(sg, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    } else {
        sg->regs[SGC1_CONTROL] &= ~SGC1_START;
    }
}

static uint64_t sony_sgc1_read(void *opaque, hwaddr off, unsigned size)
{
    SonySgc1Word *word = opaque;

    return word->sg->regs[word->reg];
}

static void sony_sgc1_write(void *opaque, hwaddr off, uint64_t value,
                             unsigned size)
{
    SonySgc1Word *word = opaque;
    SonySgc1 *sg = word->sg;
    uint32_t *reg = &sg->regs[word->reg];
    bool running;

    switch (word->reg) {
    case SGC1_STATUS:
        *reg &= ~(value & SGC1_EVENT_A);
        sony_sgc1_update_irq(sg);
        break;
    case SGC1_ENABLE:
        *reg = value & SGC1_EVENT_A;
        sony_sgc1_update_irq(sg);
        break;
    case SGC1_CONTROL:
        running = *reg & SGC1_START;
        *reg = value & (SGC1_START | SGC1_CONTINUOUS);
        if (!(*reg & SGC1_START)) {
            timer_del(sg->timer);
        } else if (!running) {
            sony_sgc1_start_frame(sg, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
        }
        break;
    case SGC1_COMPARE:
        *reg = value & 0x3fff;
        break;
    case SGC1_GEOMETRY:
        *reg = value;
        break;
    default:
        g_assert_not_reached();
    }
}

static const MemoryRegionOps sony_sgc1_ops = {
    .read = sony_sgc1_read,
    .write = sony_sgc1_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
    .impl = { .min_access_size = 4, .max_access_size = 4 },
};

static void sony_sgc1_reset(void *opaque)
{
    SonySgc1 *sg = opaque;

    timer_del(sg->timer);
    memset(sg->regs, 0, sizeof(sg->regs));
    sg->compare_pending = false;
    sony_sgc1_update_irq(sg);
}

static void sony_sgc1_create(MachineState *machine, qemu_irq irq)
{
    static const hwaddr offsets[SGC1_REGISTER_COUNT] = {
        [SGC1_STATUS] = 0x48, [SGC1_ENABLE] = 0x50, [SGC1_CONTROL] = 0x210,
        [SGC1_GEOMETRY] = 0x220, [SGC1_COMPARE] = 0x224,
    };
    SonySgc1 *sg = g_new0(SonySgc1, 1);

    sg->irq = irq;
    sg->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, sony_sgc1_tick, sg);
    for (size_t i = 0; i < ARRAY_SIZE(offsets); i++) {
        SonySgc1Word *word = &sg->words[i];

        word->sg = sg;
        word->reg = i;
        memory_region_init_io(&word->mr, OBJECT(machine), &sony_sgc1_ops, word,
                              "cxd90045.sgc1-event-a", 4);
        memory_region_add_subregion_overlap(get_system_memory(),
                                            SGC1_BASE + offsets[i],
                                            &word->mr, -500);
    }
    qemu_register_reset(sony_sgc1_reset, sg);
}

static bool sony_a6400_get_sgc1_event_a(Object *obj, Error **errp)
{
    return sony_a6400_sgc1_event_a;
}

static void sony_a6400_set_sgc1_event_a(Object *obj, bool value, Error **errp)
{
    sony_a6400_sgc1_event_a = value;
}

static bool sony_a6400_get_campig_enable_readback(Object *obj, Error **errp)
{
    return sony_a6400_campig_enable_readback;
}

static void sony_a6400_set_campig_enable_readback(Object *obj, bool value,
                                                Error **errp)
{
    sony_a6400_campig_enable_readback = value;
}

/*
 * E0580: the original FUSB handler samples bit 0 at 0xF2920010.
 * Its UDIF descriptor requests rising-edge IRQ 230. A virtual cable
 * transition updates the input before pulsing that interrupt.
 */
typedef struct SonyUsbVbus {
    MemoryRegion status;
    qemu_irq irq;
    bool present;
} SonyUsbVbus;

static bool sony_a6400_usb_vbus_test;
static bool sony_a6400_usb_cid_high;
static bool sony_a6400_dwc3_gadget;
static SonyUsbVbus *sony_a6400_usb_vbus;

static uint64_t sony_usb_vbus_read(void *opaque, hwaddr addr, unsigned size)
{
    SonyUsbVbus *vbus = opaque;

    return (vbus->present ? 1U : 0U) | (sony_a6400_usb_cid_high ? 2U : 0U);
}

static bool sony_usb_vbus_accepts(void *opaque, hwaddr addr, unsigned size,
                                  bool is_write, MemTxAttrs attrs)
{
    return !is_write;
}

static const MemoryRegionOps sony_usb_vbus_ops = {
    .read = sony_usb_vbus_read,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {
        .min_access_size = 4,
        .max_access_size = 4,
        .accepts = sony_usb_vbus_accepts,
    },
};

static void sony_usb_vbus_reset(void *opaque)
{
    SonyUsbVbus *vbus = opaque;

    vbus->present = false;
    qemu_irq_lower(vbus->irq);
}

static void sony_usb_vbus_create(MachineState *machine, qemu_irq irq)
{
    SonyUsbVbus *vbus = g_new0(SonyUsbVbus, 1);

    vbus->irq = irq;
    memory_region_init_io(&vbus->status, OBJECT(machine), &sony_usb_vbus_ops,
                          vbus, "cxd90045.usb-vbus-status", 4);
    memory_region_add_subregion_overlap(get_system_memory(), 0xF2920010ULL,
                                        &vbus->status, 0);
    qemu_register_reset(sony_usb_vbus_reset, vbus);
    sony_a6400_usb_vbus = vbus;
}

static bool sony_get_usb_vbus_test(Object *obj, Error **errp)
{
    return sony_a6400_usb_vbus_test;
}

static void sony_set_usb_vbus_test(Object *obj, bool enabled, Error **errp)
{
    if (phase_check(PHASE_MACHINE_INITIALIZED)) {
        error_setg(errp, "usb-vbus-test is a startup option");
        return;
    }
    sony_a6400_usb_vbus_test = enabled;
}

static bool sony_get_usb_cid_high(Object *obj, Error **errp)
{
    return sony_a6400_usb_cid_high;
}

static void sony_set_usb_cid_high(Object *obj, bool enabled, Error **errp)
{
    if (phase_check(PHASE_MACHINE_INITIALIZED)) {
        error_setg(errp, "usb-cid-high is a startup option");
        return;
    }
    sony_a6400_usb_cid_high = enabled;
}

static bool sony_get_dwc3_gadget(Object *obj, Error **errp)
{
    return sony_a6400_dwc3_gadget;
}

static void sony_set_dwc3_gadget(Object *obj, bool enabled, Error **errp)
{
    if (phase_check(PHASE_MACHINE_INITIALIZED)) {
        error_setg(errp, "dwc3-gadget is a startup option");
        return;
    }
    sony_a6400_dwc3_gadget = enabled;
}

static bool sony_get_usb_vbus(Object *obj, Error **errp)
{
    return sony_a6400_usb_vbus && sony_a6400_usb_vbus->present;
}

static void sony_set_usb_vbus(Object *obj, bool present, Error **errp)
{
    SonyUsbVbus *vbus = sony_a6400_usb_vbus;

    if (!vbus) {
        error_setg(errp, "usb-vbus requires a running machine with usb-vbus-test enabled");
        return;
    }
    if (vbus->present != present) {
        vbus->present = present;
        qemu_irq_pulse(vbus->irq);
    }
}

static void sony_a6400_submicro_xfer(void *opaque, int ch, uint32_t mode,
                                     uint8_t *buf, int len)
{
    uint8_t out[256];

    sony_a6400_sio_log(opaque, ch, mode, buf, len);
    if (!sony_a6400_sm_loaded || len < 128) {
        return;
    }
    memcpy(out, sony_a6400_sm_frames +
           (buf[0] == 0x31 ? 256 : buf[0] == 0x61 ? 128 : 0), 128);
    if (sony_a6400_rear_controls && out[0] == 0x20 && sony_a6400_front_pressed) {
        uint16_t crc = 0xffff;
        out[28] |= sony_a6400_front_pressed & (BIT(1) | BIT(2));
        out[29] &= ~(sony_a6400_front_pressed >> 8);
        for (unsigned i = 0; i < 126; i++) {
            crc ^= out[i] << 8;
            for (unsigned bit = 0; bit < 8; bit++) {
                crc = (crc & 0x8000) ? (crc << 1) ^ 0x1021 : crc << 1;
            }
        }
        out[126] = crc;
        out[127] = crc >> 8;
    }
    if (len >= 256) {
        memcpy(out + 128, sony_a6400_sm_frames + 256, 128);
    }
    memcpy(buf, out, len >= 256 ? 256 : 128);
}

static char *sony_a6400_get_sm_file(Object *obj, Error **errp)
{
    return g_strdup(sony_a6400_sm_file);
}

static void sony_a6400_set_sm_file(Object *obj, const char *value,
                                   Error **errp)
{
    g_free(sony_a6400_sm_file);
    sony_a6400_sm_file = g_strdup(value);
}

static void sony_a6400_init(MachineState *machine)
{
    MemoryRegion *sysmem = get_system_memory();
    MemoryRegion *ram0 = g_new(MemoryRegion, 1);
    MemoryRegion *ram1 = g_new(MemoryRegion, 1);
    MemoryRegion *sram = g_new(MemoryRegion, 1);
    MemoryRegion *crcdc_mmio = g_new(MemoryRegion, 1);
    MemoryRegion *l2c_mmio = g_new(MemoryRegion, 1);
    CxdRwStub *sugi_clk;
    DeviceState *a9mp;
    SysBusDevice *sbd;
    qemu_irq pic[CXD90045_NUM_IRQS];
    unsigned ncpu = machine->smp.cpus;
    ARMCPU *cpus[CXD90045_MAX_CPUS];
    DeviceState *timer;
    int i;

    for (unsigned c = 0; c < ncpu; c++) {
        Object *cpuobj = object_new(machine->cpu_type);
        object_property_set_bool(cpuobj, "has_el3", false, &error_fatal);
        if (object_property_find(cpuobj, "reset-cbar")) {
            object_property_set_int(cpuobj, "reset-cbar",
                                    CXD90045_PERIPH_BASE, &error_fatal);
        }
        /* secondaries run from reset too; they park in the mailbox stub */
        qdev_realize(DEVICE(cpuobj), NULL, &error_fatal);
        cpus[c] = ARM_CPU(cpuobj);
    }

    memory_region_init_ram(ram0, NULL, "sony_a6400.ram0",
                           CXD90045_RAM0_SIZE, &error_fatal);
    memory_region_add_subregion(sysmem, CXD90045_RAM0_BASE, ram0);

    memory_region_init_ram(ram1, NULL, "sony_a6400.ram1",
                           CXD90045_RAM1_SIZE, &error_fatal);
    memory_region_add_subregion(sysmem, CXD90045_RAM1_BASE, ram1);

    memory_region_init_ram(sram, NULL, "sony_a6400.sram",
                           CXD90045_SRAM_SIZE, &error_fatal);
    memory_region_add_subregion(sysmem, CXD90045_SRAM_BASE, sram);

    a9mp = qdev_new(TYPE_A9MPCORE_PRIV);
    qdev_prop_set_uint32(a9mp, "num-cpu", ncpu);
    qdev_prop_set_uint32(a9mp, "num-irq", CXD90045_NUM_IRQS);
    sbd = SYS_BUS_DEVICE(a9mp);
    sysbus_realize_and_unref(sbd, &error_fatal);
    sysbus_mmio_map(sbd, 0, CXD90045_PERIPH_BASE);
    for (unsigned c = 0; c < ncpu; c++) {
        sysbus_connect_irq(sbd, c,
                           qdev_get_gpio_in(DEVICE(cpus[c]), ARM_CPU_IRQ));
        sysbus_connect_irq(sbd, c + ncpu,
                           qdev_get_gpio_in(DEVICE(cpus[c]), ARM_CPU_FIQ));
    }

    for (i = 0; i < CXD90045_NUM_IRQS - 32; i++) {
        pic[i] = qdev_get_gpio_in(a9mp, i);
    }

    pl011_create(CXD90045_UART0_BASE, pic[CXD90045_UART0_SPI], serial_hd(0));

    timer = qdev_new("cxd4115-timer");
    sysbus_realize_and_unref(SYS_BUS_DEVICE(timer), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(timer), 0, CXD90045_TIMER_BASE);
    for (i = 0; i < 16; i++) {
        sysbus_connect_irq(SYS_BUS_DEVICE(timer), i,
                           pic[CXD90045_TIMER_IRQ0 + i - 32]);
    }

    /*
     * Catch-all for unmodelled peripherals (E0525): same behaviour as the
     * unassigned space it covers (reads 0, writes dropped, see
     * ignore_memory_transaction_failures), but every access is logged
     * under -d unimp so missing devices are visible instead of silent.
     * A global read-back register file here broke dwc3 (a self-clearing
     * reset bit read back as set: "failed to initialize core", -110), so
     * read-back is applied per block below (E0528).
     */
    create_unimplemented_device("cxd90045.unmodelled", 0xF0000000ULL,
                                0x08000000ULL);
    if (sony_a6400_sgc1_event_a) {
        sony_sgc1_create(machine, pic[35 - 32]);
    }
    if (sony_a6400_usb_vbus_test) {
        sony_usb_vbus_create(machine, pic[230 - 32]);
    }
    if (sony_a6400_dwc3_gadget) {
        DeviceState *dwc3 = qdev_new("cxd90045-dwc3");
        sysbus_realize_and_unref(SYS_BUS_DEVICE(dwc3), &error_fatal);
        sysbus_mmio_map_overlap(SYS_BUS_DEVICE(dwc3), 0, 0xF020C000ULL, -500);
        sysbus_connect_irq(SYS_BUS_DEVICE(dwc3), 0, pic[229 - 32]);
    }

    /*
     * E0566: CamPig IRQ setup reads back +0x50 before setting its next
     * enable bit. Bind only the observed word in this opt-in experiment.
     */
    if (sony_a6400_campig_enable_readback) {
        DeviceState *rf = qdev_new("cxd90014-regfile");

        qdev_prop_set_string(rf, "name", "cxd90045.campig-enable");
        qdev_prop_set_uint64(rf, "size", 4);
        qdev_prop_set_uint64(rf, "base", 0xF3000050ULL);
        sysbus_realize_and_unref(SYS_BUS_DEVICE(rf), &error_fatal);
        sysbus_mmio_map_overlap(SYS_BUS_DEVICE(rf), 0, 0xF3000050ULL, -500);
    }

    /*
     * AudioIp (E0528): the av-cam audio driver writes and reads back plain
     * config registers in 0xF3420000-0xF3423FFF (a5000 RE_AUDIO.md section
     * 1.1, where a read-back register file let audio initialise fully); the
     * a6400 av-cam also touches 0xF3428000.  Read-back over 32 KiB, logged.
     */
    {
        DeviceState *rf = qdev_new("cxd90014-regfile");
        qdev_prop_set_string(rf, "name", "cxd90045.audioip");
        qdev_prop_set_uint64(rf, "size", 0x8000);
        qdev_prop_set_uint64(rf, "base", 0xF3420000ULL);
        sysbus_realize_and_unref(SYS_BUS_DEVICE(rf), &error_fatal);
        sysbus_mmio_map_overlap(SYS_BUS_DEVICE(rf), 0, 0xF3420000ULL, -500);
    }

    /*
     * Imager / ComboPHY window (E0534).  av-cam keeps absolute pointers
     * here (Leo clk 0xFC021000, MIPI D-PHY register blocks, sensor tables
     * under 0xFC0Axxxx).  Reads of unassigned space are 0, so a
     * read-after-write such as set_mptx_fbmode (wants 0x7c back) fails.
     * Plain read-back, same device as the audio block.  The Leo page is
     * mapped on top because its lock bits are status, not a stored write.
     */
    {
        DeviceState *rf = qdev_new("cxd90014-regfile");
        uint32_t *leo = g_new0(uint32_t, 0x400);
        MemoryRegion *leo_mmio = g_new(MemoryRegion, 1);

        qdev_prop_set_string(rf, "name", "cxd90045.imager");
        qdev_prop_set_uint64(rf, "size", 0x200000);
        qdev_prop_set_uint64(rf, "base", 0xFC000000ULL);
        sysbus_realize_and_unref(SYS_BUS_DEVICE(rf), &error_fatal);
        sysbus_mmio_map_overlap(SYS_BUS_DEVICE(rf), 0, 0xFC000000ULL, -500);
        memory_region_init_io(leo_mmio, NULL, &cxd90045_leo_ops, leo,
                              "cxd90045.leo_clk", 0x1000);
        memory_region_add_subregion_overlap(sysmem, 0xFC021000ULL,
                                            leo_mmio, 0);
    }

    /* GPIO p0..p17; pin IRQs p0.0-15 -> 121+, p0.16 -> 144, p1.0-6 -> 137+ */
    {
        DeviceState *gpio = qdev_new("cxd90014-gpio");
        qdev_prop_set_string(gpio, "reset-high", CXD90045_GPIO_RESET_HIGH);
        sysbus_realize_and_unref(SYS_BUS_DEVICE(gpio), &error_fatal);
        sysbus_mmio_map(SYS_BUS_DEVICE(gpio), 0, CXD90045_GPIO_BASE);
        for (i = 0; i <= 16; i++) {
            sysbus_connect_irq(SYS_BUS_DEVICE(gpio), i,
                               pic[(i < 16 ? 121 + i : 144) - 32]);
        }
        for (i = 0; i <= 6; i++) {
            sysbus_connect_irq(SYS_BUS_DEVICE(gpio), 17 + i,
                               pic[137 + i - 32]);
        }
        sony_a6400_gpio = gpio;
        sony_a6400_sm_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                           sony_a6400_submicro_tick, NULL);
        timer_mod(sony_a6400_sm_timer, 50 * SCALE_MS);
    }

    /* ADC units 0/1: all channels start at 1023 (released). */
    for (i = 0; i < 2; i++) {
        DeviceState *adc = sysbus_create_simple(
            "cxd90014-adc", CXD90045_ADC_BASE + i * 0x1000,
            pic[CXD90045_ADC_IRQ0 + i - 32]);
        if (i == 1) {
            sony_a6400_rear_adc = adc;
        }
    }
    if (sony_a6400_rear_controls) {
        MemoryRegion *counter = g_new0(MemoryRegion, 1);
        memory_region_init_io(counter, OBJECT(machine), &sony_a6400_wheel_ops,
                              NULL, "a6400.rear-wheel-counter", 2);
        memory_region_add_subregion_overlap(sysmem, 0xF2404014, counter, -500);
        qemu_input_handler_register(NULL, &sony_a6400_rear_handler);
        qemu_register_reset(sony_a6400_rear_reset, NULL);
    }

    if (sony_a6400_sm_file) {
        gchar *data = NULL;
        gsize flen = 0;

        if (!g_file_get_contents(sony_a6400_sm_file, &data, &flen, NULL) ||
            flen != sizeof(sony_a6400_sm_frames)) {
            error_report("sony-a6400: submicro-frames %s: need a 384-byte "
                         "file (slot A, C, B)", sony_a6400_sm_file);
            exit(1);
        }
        memcpy(sony_a6400_sm_frames, data, sizeof(sony_a6400_sm_frames));
        g_free(data);
        sony_a6400_sm_loaded = true;
    }

    /*
     * Camera timing-event block (E0527): engine frame events (group A,
     * IRQ 83) and 1 MHz one-shot comparators (group B, IRQ 84).  The engine's
     * 1/8-frame "int_HS" comparator chain sets the sub-micro slot-A permit
     * flag (a5000 RE_ENGINE_TICK.md sections 1-2).
     */
    {
        /*
         * VPH display controller + SYSV frame timer (E0529, a5000 wiring):
         * SYSV 0xF3402000 = master V clock (IRQ 98 every engine frame,
         * V counter +0x98, phase +0x28), panel/line vsync IRQ 101/102 at
         * 60 Hz, VPH 0xF3440000.  SYSV's V output drives the engine frame
         * of the timing-event block (ext-vd), as on the a5000.  av-cam's
         * AudioIp position poll runs from this frame clock.
         */
        DeviceState *vph = qdev_new("cxd90014-vph");
        DeviceState *ev = qdev_new("cxd90014-evsel");
        qdev_set_id(vph, g_strdup("vph"), &error_fatal);
        sysbus_realize_and_unref(SYS_BUS_DEVICE(vph), &error_fatal);
        sysbus_mmio_map(SYS_BUS_DEVICE(vph), 0, 0xF3440000ULL);
        sysbus_mmio_map(SYS_BUS_DEVICE(vph), 1, 0xF3402000ULL);
        sysbus_connect_irq(SYS_BUS_DEVICE(vph), 0, pic[98 - 32]);
        sysbus_connect_irq(SYS_BUS_DEVICE(vph), 1, pic[101 - 32]);
        sysbus_connect_irq(SYS_BUS_DEVICE(vph), 2, pic[102 - 32]);
        qdev_prop_set_bit(ev, "ext-vd", true);
        sysbus_realize_and_unref(SYS_BUS_DEVICE(ev), &error_fatal);
        qdev_connect_gpio_out(vph, 0, qdev_get_gpio_in(ev, 0));
        sysbus_mmio_map(SYS_BUS_DEVICE(ev), 0, 0xF3C30000ULL);
        sysbus_connect_irq(SYS_BUS_DEVICE(ev), 0, pic[83 - 32]);
        sysbus_connect_irq(SYS_BUS_DEVICE(ev), 1, pic[84 - 32]);
    }

    /* SIO: 5 channels, 0xF2405000 + 0x200 * ch, IRQ 201 + ch (E0526) */
    {
        DeviceState *sio = qdev_new("cxd90014-sio");
        sysbus_realize_and_unref(SYS_BUS_DEVICE(sio), &error_fatal);
        sysbus_mmio_map(SYS_BUS_DEVICE(sio), 0, CXD90045_SIO_BASE);
        for (i = 0; i < 5; i++) {
            sysbus_connect_irq(SYS_BUS_DEVICE(sio), i,
                               pic[CXD90045_SIO_IRQ0 + i - 32]);
            cxd90014_sio_attach_dir(sio, i, i == 3 ?
                                    sony_a6400_submicro_xfer :
                                    sony_a6400_sio_log, NULL);
        }
    }

    memory_region_init_io(crcdc_mmio, NULL, &cxd90045_crcdc_ops, NULL,
                          "cxd90045.crcdc", 0x1000);
    memory_region_add_subregion(sysmem, CXD90045_CRCDC_BASE, crcdc_mmio);

    memory_region_init_io(l2c_mmio, NULL, &cxd90045_l2c_stub_ops, NULL,
                          "cxd90045.l2c_stub", 0x1000);
    memory_region_add_subregion(sysmem, CXD90045_L2C_STUB_BASE, l2c_mmio);

    {
        DeviceState *gpu = qdev_new("dmp-smaph-s30");
        sysbus_realize_and_unref(SYS_BUS_DEVICE(gpu), &error_fatal);
        sysbus_mmio_map(SYS_BUS_DEVICE(gpu), 0, CXD90045_GPES_BAR_BASE);
        sysbus_connect_irq(SYS_BUS_DEVICE(gpu), 0, pic[CXD90045_GPES_SPI]);
    }

    /*
     * SA DSP cores (a5000 null-SA model).  av-cam's imager start waits on
     * SA0 source A (IRQ 51) after ZIT ConvZ1C.  Without that completion the
     * mode sequencer never requests the YC output, so VPH 0x144 bit0 stays
     * clear.  The four ID strings are in this av-cam.bin.
     */
    {
        static const struct {
            hwaddr base;
            int irq[3];
            const char *id;
        } sa_cores[] = {
            { 0xF34A0000ULL, { 51, 52, 53 }, "ZMA0V1.0SA5_V1.0" },
            { 0xF34B0000ULL, { 54, 55, 56 }, "ZMA1V1.0SA5_V1.0" },
            { 0xF34C0000ULL, { 61, 62, 63 }, "SA0_V1.0SA5_V1.0" },
            { 0xF35C0000ULL, { 64, 65, 66 }, "SA1_V1.0SA5_V1.0" },
        };
        for (int n = 0; n < ARRAY_SIZE(sa_cores); n++) {
            DeviceState *sa = qdev_new("cxd90014-sa");
            qdev_prop_set_uint32(sa, "index", n);
            qdev_prop_set_uint64(sa, "base", sa_cores[n].base);
            qdev_prop_set_string(sa, "core-id", sa_cores[n].id);
            sysbus_realize_and_unref(SYS_BUS_DEVICE(sa), &error_fatal);
            sysbus_mmio_map(SYS_BUS_DEVICE(sa), 0, sa_cores[n].base);
            for (int k = 0; k < 3; k++) {
                sysbus_connect_irq(SYS_BUS_DEVICE(sa), k,
                                   pic[sa_cores[n].irq[k] - 32]);
            }
        }
    }

    /*
     * Sensor register writer (a5000 P2S).  av-cam has the same unit-0
     * bases.  A transfer that never clears 0xF3C00908 bit0 is refused
     * forever after the first one (RE_SENSOR_ISP.md).  IRQ 38.
     */
    {
        DeviceState *p2s = qdev_new("cxd90014-p2s");
        sysbus_realize_and_unref(SYS_BUS_DEVICE(p2s), &error_fatal);
        sysbus_mmio_map(SYS_BUS_DEVICE(p2s), 0, 0xF3C00000ULL);
        sysbus_connect_irq(SYS_BUS_DEVICE(p2s), 0, pic[38 - 32]);
    }

    /*
     * The 64 KiB window also covers SYSV at 0xF3402000.  Priority -1 lets
     * that device answer 0xF3402028.  At priority 0 the stub answered the
     * phase read with 0 (E0535).
     */
    sugi_clk = cxd90045_create_rw_stub(sysmem, "cxd90045.sugi_clk",
                                       CXD90045_SUGI_CLK_BASE, 0x10000, -1);
    /* sugilite_clock_up_internal expects 0x15 at 0xF3400080 */
    sugi_clk->regs[0x80 >> 2] = 0x15;

    cxd90045_create_rw_stub(sysmem, "cxd90045.dfs_ctrl",
                            CXD90045_DFS_CTRL_BASE, 0x1000, 0);

    cxd90045_create_xdmac(sysmem, CXD90045_XDMAC0_BASE,
                          &pic[CXD90045_XDMAC0_SPI_BASE]);
    cxd90045_create_xdmac(sysmem, CXD90045_XDMAC1_BASE,
                          &pic[CXD90045_XDMAC1_SPI_BASE]);
    cxd90045_create_ldec(sysmem, CXD90045_LDEC_BASE,
                         pic[CXD90045_LDEC_SPI]);

    {
        static const hwaddr f2a_bases[] = {
            CXD90045_F2A01_BASE, 0xF2A11000ULL, 0xF2A12000ULL,
            0xF2A21000ULL, 0xF2A22000ULL, 0xF2A30000ULL
        };
        for (size_t k = 0; k < ARRAY_SIZE(f2a_bases); k++) {
            uint32_t *regs = g_new0(uint32_t, 0x400);
            MemoryRegion *mmio = g_new(MemoryRegion, 1);
            memory_region_init_io(mmio, NULL, &cxd90045_f2a01_ops, regs,
                                  "cxd90045.f2a_clk", 0x1000);
            memory_region_add_subregion(sysmem, f2a_bases[k], mmio);
        }
    }

    {
        DeviceState *sdhci = qdev_new(TYPE_SYSBUS_SDHCI);
        DriveInfo *di = drive_get(IF_SD, 0, 0);
        BlockBackend *blk = di ? blk_by_legacy_dinfo(di) : NULL;
        qdev_prop_set_uint8(sdhci, "sd-spec-version", 3);
        qdev_prop_set_uint64(sdhci, "capareg", 0x05e830b0);
        sbd = SYS_BUS_DEVICE(sdhci);
        sysbus_realize_and_unref(sbd, &error_fatal);
        sysbus_mmio_map(sbd, 0, CXD90045_EMMC0_BASE);
        sysbus_connect_irq(sbd, 0, pic[CXD90045_EMMC0_SPI]);
        if (blk) {
            DeviceState *card = qdev_new(TYPE_EMMC);
            qdev_prop_set_uint64(card, "boot-partition-size", 4 * MiB);
            qdev_prop_set_uint8(card, "boot-config", 0x08);
            qdev_prop_set_drive_err(card, "drive", blk, &error_fatal);
            qdev_realize_and_unref(card, qdev_get_child_bus(sdhci, "sd-bus"),
                                   &error_fatal);
        }
    }

    sony_a6400_load_boot_contract(machine, cpus, ncpu);
}

static void sony_a6400_machine_init(MachineClass *mc)
{
    mc->desc = "Sony ILCE-6400 (BIONZ X / CXD90045)";
    mc->init = sony_a6400_init;
    mc->max_cpus = CXD90045_MAX_CPUS;
    mc->default_cpus = CXD90045_MAX_CPUS;
    mc->default_cpu_type = ARM_CPU_TYPE_NAME("cortex-a5");
    mc->default_ram_size = CXD90045_RAM0_SIZE;
    mc->ignore_memory_transaction_failures = true;
    object_class_property_add_str(OBJECT_CLASS(mc), "submicro-frames",
                                  sony_a6400_get_sm_file,
                                  sony_a6400_set_sm_file);
    object_class_property_set_description(OBJECT_CLASS(mc), "submicro-frames",
        "384-byte file: captured sub-micro RX slots A, C, B (E0527)");
    object_class_property_add_bool(OBJECT_CLASS(mc), "campig-enable-readback",
                                   sony_a6400_get_campig_enable_readback,
                                   sony_a6400_set_campig_enable_readback);
    object_class_property_set_description(OBJECT_CLASS(mc),
        "campig-enable-readback", "Retain the CamPig IRQ enable word (E0566)");
    object_class_property_add_bool(OBJECT_CLASS(mc), "sgc1-event-a",
                                   sony_a6400_get_sgc1_event_a,
                                   sony_a6400_set_sgc1_event_a);
    object_class_property_set_description(OBJECT_CLASS(mc), "sgc1-event-a",
        "Enable experimental Sgc1 input sync event A (E0568)");
    object_class_property_add_bool(OBJECT_CLASS(mc), "rear-controls",
                                   sony_a6400_get_rear_controls,
                                   sony_a6400_set_rear_controls);
    object_class_property_set_description(OBJECT_CLASS(mc), "rear-controls",
        "Connect host keys to a6400 ADC, GPIO and sub-micro inputs");
    object_class_property_add_bool(OBJECT_CLASS(mc), "usb-vbus-test",
                                   sony_get_usb_vbus_test,
                                   sony_set_usb_vbus_test);
    object_class_property_set_description(OBJECT_CLASS(mc), "usb-vbus-test",
        "Enable the experimental FUSB cable status and IRQ 230 (E0580)");
    object_class_property_add_bool(OBJECT_CLASS(mc), "usb-cid-high",
                                   sony_get_usb_cid_high,
                                   sony_set_usb_cid_high);
    object_class_property_set_description(OBJECT_CLASS(mc), "usb-cid-high",
        "Keep FUSB CID bit 1 high (floating / peripheral idle) at 0xF2920010 (E0587)");
    object_class_property_add_bool(OBJECT_CLASS(mc), "dwc3-gadget",
                                   sony_get_dwc3_gadget,
                                   sony_set_dwc3_gadget);
    object_class_property_set_description(OBJECT_CLASS(mc), "dwc3-gadget",
        "Enable the experimental CXD90045 DWC3 gadget controller at 0xF020C000 and IRQ 229 (E0590)");
    object_class_property_add_bool(OBJECT_CLASS(mc), "usb-vbus",
                                   sony_get_usb_vbus, sony_set_usb_vbus);
    object_class_property_set_description(OBJECT_CLASS(mc), "usb-vbus",
        "Set virtual cable presence at runtime");
}

DEFINE_MACHINE("sony-a6400", sony_a6400_machine_init)
