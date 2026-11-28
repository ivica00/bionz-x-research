/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * Sony CXD90045 / CXD90057 Digital Front-End (DFE) Sensor Pipeline Model
 *
 * Models the BIONZ X / IMX284 0xFC000000..0xFC0FFFFF DFE sub-blocks behind
 * opt-in machine property dfe-sensor=on (mapped at overlap priority 10 above
 * cxd90045.imager at -500 and cxd90045.leo_clk at 0):
 *   - 0xFC01E000..0xFC01FFFF: DFE CPU Doorbell (+0x018 BIT(27)) & DFE IRQ VIC
 *                             (0xFC01F000..0xFC01FFFF, vector read/ack +0xFF00)
 *   - 0xFC021000..0xFC021FFF: Leo DFE CRG (PLL enable/dividers, block reset
 *                             +0x024 BIT(6) DrvSGC0 / BIT(7) DrvSGC1, PLL lock
 *                             +0x050/+0x054/+0x058 bit 0)
 *   - 0xFC02A000..0xFC02AFFF: MPHY lane status (+0x050) & D-PHY PLL lock (+0x080)
 *   - 0xFC030000..0xFC030FFF: DFE 64 KiB ARM coprocessor mailbox (+0x0F4..+0x0FC)
 *   - 0xFC080000..0xFC080FFF: SyncSeq 72 MHz frame sequencer (cold +0x030 =
 *                             0x00EE466C, SYSV VD qualification, +0x0F4 timer
 *                             -> P2S Ch 1 frame-sync release)
 *   - 0xFC098000..0xFC098FFF: DFE timer gates (+0x808 = 0x003C0006, +0x908 =
 *                             0x00020000)
 *   - 0xFC0A0000..0xFC0A0FFF: DfeTop W1S/W1C core gate (+0x000/+0x004), bridge
 *                             (+0x108), crossbar, and sub-IRQ router (+0x010..
 *                             +0x030 and +0x200..+0x208) -> GIC IRQ 136
 *   - 0xFC0A1000..0xFC0A1FFF: DrvSGC0 (+0x100..+0x1FF), DrvSGC1/Ifmt (+0x200..),
 *                             and DrvSGC0 NLR LUT (+0xB80..+0xBE0)
 *   - 0xFC0A8000..0xFC0A9FFF: DFE RDC & DFE AIF RdcW0 channel (0xFC0A9200)
 *   - 0xFC0E0000..0xFC0E1FFF: RdcW0 standalone Bayer RAW DMA writer (0xFC0E1100)
 *   - 0xFC0F0000..0xFC0F0FFF: Dual-port 8-lane SLVS-EC RX (+0x0B0 link lock)
 */

#include "qemu/osdep.h"
#include "hw/misc/cxd90045_dfe.h"
#include "hw/core/sysbus.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "migration/vmstate.h"
#include "qapi/error.h"
#include "qapi/visitor.h"
#include "qemu/log.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "qom/object.h"
#include "system/address-spaces.h"
#include "system/memory.h"

OBJECT_DECLARE_SIMPLE_TYPE(Cxd90045DfeState, CXD90045_DFE)

#define DFE_OVERLAP_PRIO        10

#define DFE_IRQ_VIC_BASE        0xFC01E000ULL
#define DFE_IRQ_VIC_SIZE        0x2000ULL
#define DFE_LEO_BASE            0xFC021000ULL
#define DFE_LEO_SIZE            0x1000ULL
#define DFE_MPHY_BASE           0xFC02A000ULL
#define DFE_MPHY_SIZE           0x1000ULL
#define DFE_MBOX_BASE           0xFC030000ULL
#define DFE_MBOX_SIZE           0x1000ULL
#define DFE_SYNCSEQ_BASE        0xFC080000ULL
#define DFE_SYNCSEQ_SIZE        0x1000ULL
#define DFE_TIMER_BASE          0xFC098000ULL
#define DFE_TIMER_SIZE          0x1000ULL
#define DFE_TOP_BASE            0xFC0A0000ULL
#define DFE_TOP_SIZE            0x1000ULL
#define DFE_SGC_BASE            0xFC0A1000ULL
#define DFE_SGC_SIZE            0x1000ULL
#define DFE_AIF_BASE            0xFC0A8000ULL
#define DFE_AIF_SIZE            0x2000ULL
#define DFE_RDCW_BASE           0xFC0E0000ULL
#define DFE_RDCW_SIZE           0x2000ULL
#define DFE_SLVSEC_BASE         0xFC0F0000ULL
#define DFE_SLVSEC_SIZE         0x1000ULL

/* Clock & reset constants */
#define DFE_CLOCK_HZ            72000000ULL
#define DFE_1V_TICKS_60HZ       0x00125430U /* 1,201,200 ticks @ 72 MHz */
#define DFE_SYNCSEQ_COLD_VAL    0x00EE466CU
#define DFE_SYNCSEQ_DEF_LIMIT   0x00FFFFFCU
#define DFE_HVCNT_IDLE          0x3FFF3FFFU
#define DFE_FCNT_COLD           0x0000007CU
#define DFE_RDCW0_MAX_BYTES     (32U * 1024U * 1024U)

/* Leo reset bits (0xFC021024) */
#define LEO_RST_SGC0            BIT(6)
#define LEO_RST_SGC1            BIT(7)

/* DfeTop core_en bits (0xFC0A0000 W1S / 0xFC0A0004 W1C) */
#define DFE_CORE_RDC            BIT(7)
#define DFE_CORE_SGC0           BIT(8)
#define DFE_CORE_SGC1           BIT(9)
#define DFE_CORE_AIF            BIT(15)
#define DFE_CORE_SLVSEC         BIT(16)
#define DFE_CORE_RDCW0          BIT(19)
#define DFE_CORE_BRIDGE         BIT(24)

/* DfeTop sub-IRQ bits (+0x200 / +0x204 / +0x208 and +0x010..+0x018) */
#define DFE_IRQ_SGC0_VD0        BIT(1)
#define DFE_IRQ_SYNCSEQ_0       BIT(17)
#define DFE_IRQ_SYNCSEQ_1       BIT(18)
#define DFE_IRQ_RDCW0           BIT(25)

/* DFE 0xFC01F000 VIC channels (av-cam 0x63780980..0x63780A60) */
#define DFE_VIC_CH_SYNCSEQ_AUX  17
#define DFE_VIC_CH_SYNCSEQ_SPI  18
#define DFE_VIC_CH_SGC0_VD0     23
#define DFE_VIC_CH_SGC0_VD1     24
#define DFE_VIC_CH_DFE_EVENT    26

/* DFE Mailbox bits */
#define DFE_DOORBELL_BIT        BIT(27)
#define DFE_MBOX_CTRL_ABORT     BIT(16)
#define DFE_MBOX_CTRL_STOP_REQ  BIT(20)
#define DFE_MBOX_CTRL_RUN       BIT(28)

struct Cxd90045DfeState {
    SysBusDevice parent_obj;

    MemoryRegion mr_irq_vic;
    MemoryRegion mr_leo;
    MemoryRegion mr_mphy;
    MemoryRegion mr_mbox;
    MemoryRegion mr_syncseq;
    MemoryRegion mr_timer;
    MemoryRegion mr_dfetop;
    MemoryRegion mr_sgc;
    MemoryRegion mr_aif;
    MemoryRegion mr_rdcw;
    MemoryRegion mr_slvsec;

    MemoryRegion *sysmem;
    DeviceState *p2s1;
    const Cxd90045LiveViewSource *lv_src;

    qemu_irq irq_main;
    qemu_irq irq_err;
    qemu_irq p2s_sync;

    QEMUTimer *ext_trig_timer;
    QEMUTimer *sgc0_timer;

    /* Register backing arrays */
    uint32_t irq_vic_regs[DFE_IRQ_VIC_SIZE >> 2];
    uint32_t leo_regs[DFE_LEO_SIZE >> 2];
    uint32_t mphy_regs[DFE_MPHY_SIZE >> 2];
    uint32_t mbox_regs[DFE_MBOX_SIZE >> 2];
    uint32_t syncseq_regs[DFE_SYNCSEQ_SIZE >> 2];
    uint32_t timer_regs[DFE_TIMER_SIZE >> 2];
    uint32_t dfetop_regs[DFE_TOP_SIZE >> 2];
    uint32_t sgc_regs[DFE_SGC_SIZE >> 2];
    uint32_t aif_regs[DFE_AIF_SIZE >> 2];
    uint32_t rdcw_regs[DFE_RDCW_SIZE >> 2];
    uint32_t slvsec_regs[DFE_SLVSEC_SIZE >> 2];

    /* Unified DfeTop & VIC state */
    uint32_t core_en;
    uint32_t irq_en;
    uint32_t irq_stat;
    uint32_t irq_en0;
    uint32_t irq_stat0;
    uint32_t irq_en1;
    uint32_t irq_stat1;
    uint32_t irq_en2;
    uint32_t irq_stat2;
    uint32_t vic_en;
    uint32_t vic_raw;

    /* Mailbox state */
    bool mbox_f8_written;
    uint32_t mbox_fw_hdr;

    /* SyncSeq & DrvSGC0 runtime state */
    bool sysv_vd_level;
    bool ext_sync_level;
    int64_t last_vd_ns;
    int64_t sgc0_start_ns;
    bool sgc0_active;
    bool sgc0_waiting_vd;
    bool sgc0_waiting_ext;
    uint32_t sgc0_fcnt;

    /* RdcW0 telemetry */
    uint32_t rdcw0_frames;
};

static uint64_t dfe_ticks_to_ns(uint64_t ticks)
{
    uint64_t ns = (ticks * 1000ULL) / 72ULL;
    return ns > 0 ? ns : 1ULL;
}

static bool dfe_valid_dram_range(uint64_t pa, uint64_t len)
{
    if (len == 0 || len > DFE_RDCW0_MAX_BYTES) {
        return false;
    }
    if (pa < 0x40000000ULL && pa + len <= 0x40000000ULL) {
        return true;
    }
    if (pa >= 0x80000000ULL && pa < 0xBF800000ULL &&
        pa + len <= 0xBF800000ULL) {
        return true;
    }
    return false;
}

static void dfe_update_irq(Cxd90045DfeState *s)
{
    bool pending = ((s->irq_stat & s->irq_en) != 0) ||
                   ((s->irq_stat0 & s->irq_en0) != 0) ||
                   ((s->irq_stat1 & s->irq_en1) != 0) ||
                   ((s->irq_stat2 & s->irq_en2) != 0) ||
                   ((s->vic_raw & s->vic_en) != 0);
    qemu_set_irq(s->irq_main, pending ? 1 : 0);
}

static void dfe_raise_irq(Cxd90045DfeState *s, uint32_t dfetop_mask,
                          uint32_t vic_mask)
{
    s->irq_stat |= dfetop_mask;
    s->irq_stat0 |= dfetop_mask;
    s->vic_raw |= vic_mask;
    dfe_update_irq(s);
}

static bool dfe_slvsec_locked(Cxd90045DfeState *s)
{
    bool gate_on = ((s->core_en & DFE_CORE_SLVSEC) != 0) ||
                   ((s->slvsec_regs[0x050 >> 2] & 1u) != 0);
    return gate_on && s->sgc0_active &&
           cxd90014_p2s_imx284_streaming(s->p2s1);
}

/* -------------------------------------------------------------------------
 * DrvSGC0 timing & state machine (0xFC0A1100..0xFC0A1BFF)
 * ------------------------------------------------------------------------- */

static bool sgc0_gates_ok(const Cxd90045DfeState *s)
{
    return !(s->leo_regs[0x024 >> 2] & LEO_RST_SGC0) &&
           ((s->core_en & DFE_CORE_SGC0) != 0) &&
           ((s->sgc_regs[0x100 >> 2] & 1u) != 0);
}

static uint64_t sgc0_frame_ticks(const Cxd90045DfeState *s)
{
    uint32_t hv = s->sgc_regs[0x120 >> 2];
    uint64_t h = (hv & 0x3FFFu) + 1ULL;
    uint64_t v = ((hv >> 16) & 0x3FFFu) + 1ULL;
    uint64_t ticks = h * v;
    uint32_t nlr_cfg = s->sgc_regs[0x1A8 >> 2];
    uint32_t nlr_frac = s->sgc_regs[0x1AC >> 2];

    if (nlr_cfg & 1u) {
        int64_t v_alt = (int64_t)(((nlr_cfg >> 16) & 0x3FFFu) + 1u);
        uint32_t den = nlr_frac & 0xFFu;
        uint32_t num = (nlr_frac >> 16) & 0xFFu;
        if (den > 0) {
            int64_t adj = ((int64_t)h * (v_alt - (int64_t)v) *
                           (int64_t)num) / (int64_t)den;
            if ((int64_t)ticks + adj > 0) {
                ticks = (uint64_t)((int64_t)ticks + adj);
            }
        }
    }
    return ticks > 0 ? ticks : 1ULL;
}

static void sgc0_stop(Cxd90045DfeState *s, bool clear_req)
{
    s->sgc0_active = false;
    s->sgc0_waiting_vd = false;
    s->sgc0_waiting_ext = false;
    timer_del(s->sgc0_timer);
    if (clear_req) {
        s->sgc_regs[0x110 >> 2] = 0;
    } else {
        s->sgc_regs[0x110 >> 2] &= ~(1u << 16);
    }
    s->sgc_regs[0x118 >> 2] = DFE_HVCNT_IDLE;
}

static void sgc0_activate(Cxd90045DfeState *s, int64_t now)
{
    uint64_t frame_ns = dfe_ticks_to_ns(sgc0_frame_ticks(s));

    s->sgc0_active = true;
    s->sgc0_waiting_vd = false;
    s->sgc0_waiting_ext = false;
    s->sgc0_start_ns = now;
    s->sgc_regs[0x110 >> 2] = 0x00010001u;
    timer_mod(s->sgc0_timer, now + (int64_t)frame_ns);
}

/* -------------------------------------------------------------------------
 * RdcW0 deterministic RGGB Bayer RAW synthesis & DMA writer
 * ------------------------------------------------------------------------- */

static uint16_t dfe_sample_bayer_pixel(const Cxd90045DfeState *s,
                                       uint32_t x, uint32_t y,
                                       uint32_t w, uint32_t h,
                                       uint32_t max_val)
{
    /* RGGB phase: (0,0)=R, (1,0)=Gr, (0,1)=Gb, (1,1)=B */
    unsigned phase = ((y & 1u) << 1) | (x & 1u);
    uint32_t black = (max_val >= 0x3FFFu) ? 0x0200u : 0x0040u;
    uint32_t span = max_val - black;
    uint32_t val;

    if (s->lv_src && s->lv_src->kind != CXD90045_LV_NONE) {
        uint32_t luma = cxd90045_liveview_sample(s->lv_src, x, y, w, h);
        static const uint32_t gain_num[4] = { 16, 15, 14, 12 };
        val = black + ((luma * span * gain_num[phase]) / (255u * 16u));
    } else {
        /* Deterministic 8-bar SMPTE-like RGGB pattern + spatial gradient */
        static const uint16_t bar_rggb[8][4] = {
            { 0x3A00, 0x3A00, 0x3A00, 0x3A00 }, /* White */
            { 0x3800, 0x3400, 0x3400, 0x0600 }, /* Yellow */
            { 0x0600, 0x3200, 0x3200, 0x3600 }, /* Cyan */
            { 0x0600, 0x3000, 0x3000, 0x0600 }, /* Green */
            { 0x3400, 0x0800, 0x0800, 0x3400 }, /* Magenta */
            { 0x3600, 0x0600, 0x0600, 0x0600 }, /* Red */
            { 0x0600, 0x0600, 0x0600, 0x3600 }, /* Blue */
            { 0x0400, 0x0480, 0x0440, 0x0500 }, /* Pedestal */
        };
        uint32_t bar = (w > 0) ? (((uint64_t)x * 8ULL) / w) : 0;
        uint32_t grad = ((x * 7u + y * 5u + s->rdcw0_frames * 17u) & 0xFFu);
        uint32_t base14 = bar_rggb[bar & 7u][phase] + grad;
        val = (max_val == 0x3FFFu) ? base14 :
              ((uint64_t)base14 * max_val) / 0x3FFFu;
    }
    return (uint16_t)MIN(val, max_val);
}

static void dfe_dma_write_bayer(Cxd90045DfeState *s, uint32_t dst_pa,
                                uint32_t width, uint32_t height,
                                uint32_t stride_bytes, uint32_t cfg_bits)
{
    uint32_t max_val = 0x3FFFu;
    size_t row_bytes = (size_t)width * 2u;
    g_autofree uint8_t *row_buf = NULL;

    if ((cfg_bits & 0xFFu) == 12u) {
        max_val = 0x0FFFu;
    } else if ((cfg_bits & 0xFFu) == 16u) {
        max_val = 0xFFFFu;
    }

    row_buf = g_malloc(row_bytes);
    for (uint32_t y = 0; y < height; y++) {
        for (uint32_t x = 0; x < width; x++) {
            uint16_t px = dfe_sample_bayer_pixel(s, x, y, width, height,
                                                 max_val);
            row_buf[2u * x] = (uint8_t)(px & 0xFFu);
            row_buf[2u * x + 1u] = (uint8_t)(px >> 8);
        }
        address_space_write(&address_space_memory,
                            (hwaddr)dst_pa + (hwaddr)y * stride_bytes,
                            MEMTXATTRS_UNSPECIFIED, row_buf, row_bytes);
    }
}

static void dfe_process_rdcw0_frame(Cxd90045DfeState *s)
{
    if (!dfe_slvsec_locked(s)) {
        return;
    }

    /*
     * Path A: Standalone RdcW0 register block (0xFC0E1100..0xFC0E11FF).
     * Requires DfeTop BIT(19), SLVS-EC LINK_LOCK == 1, EN (+0x1100[0]),
     * START/ARM (+0x1110[0]), and valid DRAM destination.
     */
    if ((s->core_en & DFE_CORE_RDCW0) &&
        (s->rdcw_regs[0x1100 >> 2] & 1u) &&
        (s->rdcw_regs[0x1110 >> 2] & 1u)) {
        uint32_t dst_pa = s->rdcw_regs[0x1150 >> 2];
        uint32_t lb = s->rdcw_regs[0x1154 >> 2];
        uint32_t r20 = s->rdcw_regs[0x1120 >> 2];
        uint32_t r24 = s->rdcw_regs[0x1124 >> 2];
        uint32_t r28 = s->rdcw_regs[0x1128 >> 2];
        uint32_t r2c = s->rdcw_regs[0x112C >> 2];
        uint32_t r30 = s->rdcw_regs[0x1130 >> 2];
        uint32_t r34 = s->rdcw_regs[0x1134 >> 2];
        uint32_t cfg = s->rdcw_regs[0x110C >> 2];
        uint32_t width = 0, height = 0, stride_bytes = 0;

        if (r2c != 0 && r34 != 0) {
            width = r2c & 0xFFFFu;
            height = r34 & 0xFFFFu;
        } else if (r28 != 0 && r30 != 0) {
            width = r28 & 0xFFFFu;
            height = r30 & 0xFFFFu;
        } else if (r20 != 0 && r24 != 0 && (r20 >> 16) == 0) {
            width = r20 & 0xFFFFu;
            height = r24 & 0xFFFFu;
        } else if (r20 != 0 && (r20 >> 16) != 0) {
            uint32_t w0 = r20 & 0xFFFFu;
            uint32_t h0 = (r20 >> 16) & 0xFFFFu;
            if (lb >= 2u && (w0 + 1u) * 2u == lb) {
                width = w0 + 1u;
                height = h0 + 1u;
            } else {
                width = w0;
                height = h0;
            }
        } else if (lb >= 2u && r2c != 0) {
            width = lb / 2u;
            height = r2c & 0xFFFFu;
        } else if (lb >= 2u) {
            width = lb / 2u;
            height = r34 ? (r34 & 0xFFFFu) : 16u;
        }

        stride_bytes = lb ? lb : (width * 2u);
        if (width >= 1u && width <= 8192u && height >= 1u && height <= 8192u &&
            stride_bytes >= width * 2u) {
            uint64_t total = (uint64_t)(height - 1u) * stride_bytes +
                             (uint64_t)width * 2ULL;
            if (dfe_valid_dram_range(dst_pa, total)) {
                dfe_dma_write_bayer(s, dst_pa, width, height, stride_bytes, cfg);
                s->rdcw0_frames++;
                s->rdcw_regs[0x1158 >> 2] = s->rdcw0_frames;
                s->rdcw_regs[0x115C >> 2] = (uint32_t)(dst_pa + total);
                if (!((s->rdcw_regs[0x1110 >> 2] & BIT(8)) || (cfg & BIT(8)))) {
                    s->rdcw_regs[0x1110 >> 2] &= ~(1u | BIT(16));
                }
                dfe_raise_irq(s, DFE_IRQ_RDCW0, BIT(DFE_VIC_CH_DFE_EVENT));
            }
        }
    }

    /*
     * Path B: DFE AIF RdcW0 channel (0xFC0A9200..0xFC0A927F, cxd90057_hw.h /
     * av-cam 0x63780224).
     */
    if ((s->core_en & (DFE_CORE_RDC | DFE_CORE_AIF | DFE_CORE_RDCW0)) &&
        (s->aif_regs[0x1200 >> 2] & 1u)) {
        uint32_t dst_pa = s->aif_regs[0x1220 >> 2];
        uint32_t lb = s->aif_regs[0x1224 >> 2];
        uint32_t gap = s->aif_regs[0x1228 >> 2];
        uint32_t height = (s->aif_regs[0x122C >> 2] & 0x3FFFu) + 1u;
        uint32_t width = lb / 2u;
        uint32_t stride_bytes = lb + gap;

        if (width >= 1u && width <= 8192u && height >= 1u && height <= 8192u &&
            stride_bytes >= width * 2u) {
            uint64_t total = (uint64_t)(height - 1u) * stride_bytes +
                             (uint64_t)width * 2ULL;
            if (dfe_valid_dram_range(dst_pa, total)) {
                dfe_dma_write_bayer(s, dst_pa, width, height, stride_bytes, 14u);
                s->rdcw0_frames++;
                s->aif_regs[0x1210 >> 2] =
                    (uint32_t)(dst_pa + (uint64_t)height * stride_bytes);
                s->aif_regs[0x1200 >> 2] &= ~1u;
                s->aif_regs[0x1204 >> 2] &= ~0x80000000u;
                dfe_raise_irq(s, DFE_IRQ_RDCW0, BIT(DFE_VIC_CH_DFE_EVENT));
            }
        }
    }
}

/* -------------------------------------------------------------------------
 * Timers & GPIO Callbacks
 * ------------------------------------------------------------------------- */

static void dfe_ext_trig_tick(void *opaque)
{
    Cxd90045DfeState *s = opaque;

    s->syncseq_regs[0x104 >> 2] |= 1u;
    s->syncseq_regs[0x10C >> 2] |= 1u;
    if (s->syncseq_regs[0x114 >> 2] & 1u) {
        s->syncseq_regs[0x110 >> 2] |= 1u;
    }
    dfe_raise_irq(s, DFE_IRQ_SYNCSEQ_0 | DFE_IRQ_SYNCSEQ_1,
                  BIT(DFE_VIC_CH_SYNCSEQ_AUX) | BIT(DFE_VIC_CH_SYNCSEQ_SPI));
    qemu_irq_pulse(s->p2s_sync);
}

static void dfe_schedule_ext_trig(Cxd90045DfeState *s, int64_t now)
{
    uint32_t ticks = s->syncseq_regs[0x0F4 >> 2];
    uint64_t delay_ns = dfe_ticks_to_ns(ticks ? ticks : 1u);

    timer_mod(s->ext_trig_timer, now + (int64_t)delay_ns);
}

static void dfe_sgc0_tick(void *opaque)
{
    Cxd90045DfeState *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (!s->sgc0_active || !sgc0_gates_ok(s)) {
        sgc0_stop(s, false);
        return;
    }

    s->sgc0_fcnt = (s->sgc0_fcnt + 1u) & 0xFFu;

    /* Deliver SGC0_VD0 & downstream SLVS-EC / RdcW0 frame if unmasked */
    if (!(s->sgc_regs[0x13C >> 2] & 1u) &&
        (s->sgc_regs[0x10C >> 2] & 1u) &&
        (s->sgc_regs[0x198 >> 2] & (1u << 16))) {
        dfe_raise_irq(s, DFE_IRQ_SGC0_VD0, BIT(DFE_VIC_CH_SGC0_VD0));
        dfe_process_rdcw0_frame(s);
    }

    /* Release P2S Ch 1 frame-sync when running standalone without SyncSeq */
    if ((s->sgc_regs[0x108 >> 2] & 1u) && !timer_pending(s->ext_trig_timer)) {
        qemu_irq_pulse(s->p2s_sync);
    }

    if (s->sgc_regs[0x12C >> 2] & 1u) {
        s->sgc0_start_ns = now;
        timer_mod(s->sgc0_timer,
                  now + (int64_t)dfe_ticks_to_ns(sgc0_frame_ticks(s)));
    } else {
        sgc0_stop(s, true);
    }
}

static void dfe_on_sysv_vd(void *opaque, int n, int level)
{
    Cxd90045DfeState *s = opaque;
    bool high = (level != 0);
    bool rising = high && !s->sysv_vd_level;
    s->sysv_vd_level = high;

    if (!rising) {
        return;
    }

    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    uint64_t period_ticks = DFE_1V_TICKS_60HZ;
    if (s->last_vd_ns > 0 && now > s->last_vd_ns) {
        uint64_t dt_ns = (uint64_t)(now - s->last_vd_ns);
        period_ticks = (dt_ns * 72ULL + 500ULL) / 1000ULL;
    }
    s->last_vd_ns = now;

    uint32_t limit = s->syncseq_regs[0x028 >> 2];
    if (limit > 0 && period_ticks > limit) {
        period_ticks = limit;
    }
    s->syncseq_regs[0x030 >> 2] = (uint32_t)period_ticks;

    if (s->sgc0_waiting_vd && sgc0_gates_ok(s)) {
        sgc0_activate(s, now);
    }

    dfe_schedule_ext_trig(s, now);
}

static void dfe_on_ext_sync(void *opaque, int n, int level)
{
    Cxd90045DfeState *s = opaque;
    bool high = (level != 0);
    bool rising = high && !s->ext_sync_level;
    s->ext_sync_level = high;

    if (rising && s->sgc0_waiting_ext && sgc0_gates_ok(s)) {
        sgc0_activate(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    }
}

/* -------------------------------------------------------------------------
 * MMIO Handlers
 * ------------------------------------------------------------------------- */

static void dfe_mbox_complete_kick(Cxd90045DfeState *s)
{
    uint64_t fw_pa = ((uint64_t)s->mbox_regs[0x0F8 >> 2]) << 8;

    if (dfe_valid_dram_range(fw_pa, 4)) {
        uint32_t hdr = 0;
        address_space_read(&address_space_memory, fw_pa,
                           MEMTXATTRS_UNSPECIFIED, &hdr, sizeof(hdr));
        s->mbox_fw_hdr = hdr;
    }
    s->mbox_regs[0x0F4 >> 2] = 0xFFu;
    s->timer_regs[0x808 >> 2] = 0x003C0006u;
    s->timer_regs[0x908 >> 2] = 0x00020000u;
}

static uint64_t dfe_irq_vic_read(void *opaque, hwaddr off, unsigned size)
{
    Cxd90045DfeState *s = opaque;

    if (off == 0x1F00) {
        uint32_t active = s->vic_raw & s->vic_en;
        if (!active) {
            return 0xFFu;
        }
        for (unsigned ch = 0; ch < 32; ch++) {
            if (active & BIT(ch)) {
                return s->irq_vic_regs[(0x1100 >> 2) + ch] & 0xFFu;
            }
        }
        return 0xFFu;
    }
    return s->irq_vic_regs[off >> 2];
}

static void dfe_irq_vic_write(void *opaque, hwaddr off, uint64_t val,
                              unsigned size)
{
    Cxd90045DfeState *s = opaque;
    uint32_t v = (uint32_t)val;

    s->irq_vic_regs[off >> 2] = v;

    if (off == 0x0018 && (v & DFE_DOORBELL_BIT)) {
        uint32_t ctrl = s->mbox_regs[0x0FC >> 2];
        if (ctrl & DFE_MBOX_CTRL_RUN) {
            s->mbox_regs[0x0F8 >> 2] = 0xFFu << 8;
            s->mbox_regs[0x0F4 >> 2] = 0xFFu;
            s->timer_regs[0x808 >> 2] = 0x003C0006u;
            s->timer_regs[0x908 >> 2] = 0x00020000u;
        } else if (ctrl & DFE_MBOX_CTRL_ABORT) {
            s->mbox_regs[0x0F8 >> 2] = 0;
            s->mbox_regs[0x0FC >> 2] &= ~DFE_MBOX_CTRL_ABORT;
        } else if (ctrl & DFE_MBOX_CTRL_STOP_REQ) {
            s->mbox_regs[0x0F8 >> 2] = 0xFEu << 8;
        } else if (s->mbox_f8_written || (ctrl & 1u)) {
            dfe_mbox_complete_kick(s);
            s->mbox_regs[0x0FC >> 2] &= ~1u;
        }
        return;
    }

    switch (off) {
    case 0x1010:
        s->vic_en |= v;
        dfe_update_irq(s);
        break;
    case 0x1014:
        s->vic_en &= ~v;
        dfe_update_irq(s);
        break;
    case 0x101C:
        s->vic_raw &= ~v;
        dfe_update_irq(s);
        break;
    case 0x1F00:
        if (v & 1u) {
            uint32_t active = s->vic_raw & s->vic_en;
            if (active) {
                s->vic_raw &= ~(active & -active);
                dfe_update_irq(s);
            }
        }
        break;
    default:
        break;
    }
}

static uint64_t dfe_leo_read(void *opaque, hwaddr off, unsigned size)
{
    Cxd90045DfeState *s = opaque;
    uint32_t v = s->leo_regs[off >> 2];

    if (off == 0x050 || off == 0x054 || off == 0x058) {
        v |= 1u;
    }
    return v;
}

static void dfe_leo_write(void *opaque, hwaddr off, uint64_t val,
                          unsigned size)
{
    Cxd90045DfeState *s = opaque;
    uint32_t v = (uint32_t)val;

    s->leo_regs[off >> 2] = v;
    if (off == 0x024 && (v & LEO_RST_SGC0)) {
        sgc0_stop(s, false);
    }
}

static uint64_t dfe_mphy_read(void *opaque, hwaddr off, unsigned size)
{
    Cxd90045DfeState *s = opaque;

    if (off == 0x050) {
        return s->mphy_regs[0x05C >> 2] & 0x0Fu;
    }
    if (off == 0x080) {
        return s->mphy_regs[0x080 >> 2] | 1u;
    }
    return s->mphy_regs[off >> 2];
}

static void dfe_mphy_write(void *opaque, hwaddr off, uint64_t val,
                           unsigned size)
{
    Cxd90045DfeState *s = opaque;
    s->mphy_regs[off >> 2] = (uint32_t)val;
}

static uint64_t dfe_mbox_read(void *opaque, hwaddr off, unsigned size)
{
    Cxd90045DfeState *s = opaque;
    return s->mbox_regs[off >> 2];
}

static void dfe_mbox_write(void *opaque, hwaddr off, uint64_t val,
                           unsigned size)
{
    Cxd90045DfeState *s = opaque;
    uint32_t v = (uint32_t)val;

    s->mbox_regs[off >> 2] = v;
    if (off == 0x0F8) {
        s->mbox_f8_written = true;
    } else if (off == 0x0FC && (v & 1u)) {
        dfe_mbox_complete_kick(s);
        s->mbox_regs[0x0FC >> 2] &= ~1u;
    }
}

static uint64_t dfe_syncseq_read(void *opaque, hwaddr off, unsigned size)
{
    Cxd90045DfeState *s = opaque;
    return s->syncseq_regs[off >> 2];
}

static void dfe_syncseq_write(void *opaque, hwaddr off, uint64_t val,
                              unsigned size)
{
    Cxd90045DfeState *s = opaque;
    uint32_t v = (uint32_t)val;

    switch (off) {
    case 0x014:
        s->syncseq_regs[0x014 >> 2] = v & 1u;
        if (v & 1u) {
            int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
            uint32_t limit = s->syncseq_regs[0x028 >> 2];
            uint32_t period = DFE_1V_TICKS_60HZ;
            if (limit > 0 && period > limit) {
                period = limit;
            }
            s->syncseq_regs[0x030 >> 2] = period;
            if (s->sgc0_waiting_vd && sgc0_gates_ok(s)) {
                sgc0_activate(s, now);
            }
            dfe_schedule_ext_trig(s, now);
        }
        return;
    case 0x104:
    case 0x108:
    case 0x110:
        s->syncseq_regs[off >> 2] &= ~v;
        return;
    default:
        s->syncseq_regs[off >> 2] = v;
        return;
    }
}

static uint64_t dfe_timer_read(void *opaque, hwaddr off, unsigned size)
{
    Cxd90045DfeState *s = opaque;
    if (off == 0x80C) {
        return 0;
    }
    return s->timer_regs[off >> 2];
}

static void dfe_timer_write(void *opaque, hwaddr off, uint64_t val,
                            unsigned size)
{
    Cxd90045DfeState *s = opaque;
    uint32_t v = (uint32_t)val;

    if (off == 0x808) {
        s->timer_regs[0x808 >> 2] |= v;
        return;
    }
    if (off == 0x80C) {
        s->timer_regs[0x808 >> 2] &= ~v;
        return;
    }
    s->timer_regs[off >> 2] = v;
}

static uint64_t dfe_top_read(void *opaque, hwaddr off, unsigned size)
{
    Cxd90045DfeState *s = opaque;

    switch (off) {
    case 0x000:
        return s->core_en;
    case 0x004:
    case 0x014:
    case 0x020:
    case 0x02C:
    case 0x204:
        return 0;
    case 0x010:
        return s->irq_stat0;
    case 0x018:
        return s->irq_en0;
    case 0x01C:
        return s->irq_stat1;
    case 0x024:
        return s->irq_en1;
    case 0x028:
        return s->irq_stat2;
    case 0x030:
        return s->irq_en2;
    case 0x200:
        return s->irq_en;
    case 0x208:
        return s->irq_stat;
    default:
        return s->dfetop_regs[off >> 2];
    }
}

static void dfe_top_write(void *opaque, hwaddr off, uint64_t val,
                          unsigned size)
{
    Cxd90045DfeState *s = opaque;
    uint32_t v = (uint32_t)val;

    switch (off) {
    case 0x000:
        s->core_en |= v;
        return;
    case 0x004:
        s->core_en &= ~v;
        if (v & DFE_CORE_SGC0) {
            sgc0_stop(s, false);
        }
        return;
    case 0x014:
        s->irq_stat0 &= ~v;
        s->irq_stat &= ~v;
        dfe_update_irq(s);
        return;
    case 0x018:
        s->irq_en0 = v;
        dfe_update_irq(s);
        return;
    case 0x020:
        s->irq_stat1 &= ~v;
        s->irq_stat &= ~v;
        dfe_update_irq(s);
        return;
    case 0x024:
        s->irq_en1 = v;
        dfe_update_irq(s);
        return;
    case 0x02C:
        s->irq_stat2 &= ~v;
        s->irq_stat &= ~v;
        dfe_update_irq(s);
        return;
    case 0x030:
        s->irq_en2 = v;
        dfe_update_irq(s);
        return;
    case 0x200:
        s->irq_en = v;
        dfe_update_irq(s);
        return;
    case 0x204:
        s->irq_stat &= ~v;
        s->irq_stat0 &= ~v;
        dfe_update_irq(s);
        return;
    case 0x208:
        return;
    default:
        s->dfetop_regs[off >> 2] = v;
        return;
    }
}

static uint64_t dfe_sgc_read(void *opaque, hwaddr off, unsigned size)
{
    Cxd90045DfeState *s = opaque;
    return s->sgc_regs[off >> 2];
}

static void dfe_sgc_write(void *opaque, hwaddr off, uint64_t val,
                          unsigned size)
{
    Cxd90045DfeState *s = opaque;
    uint32_t v = (uint32_t)val;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    switch (off) {
    case 0x100:
        s->sgc_regs[0x100 >> 2] = v;
        if (!(v & 1u)) {
            sgc0_stop(s, false);
        }
        return;
    case 0x110:
        if (!(v & 1u)) {
            sgc0_stop(s, true);
            return;
        }
        s->sgc_regs[0x110 >> 2] = (s->sgc_regs[0x110 >> 2] & (1u << 16)) | 1u;
        if (!sgc0_gates_ok(s)) {
            s->sgc0_active = false;
            s->sgc_regs[0x110 >> 2] = 1u;
            return;
        }
        if (s->sgc_regs[0x108 >> 2] & 1u) {
            /* trig_mode == 0: internal immediate self-trigger */
            sgc0_activate(s, now);
        } else if ((s->sgc_regs[0x130 >> 2] & (1u << 16)) == 0) {
            /* trig_mode == 1: wait for SYSV / SyncSeq VD0 pulse */
            s->sgc0_active = false;
            s->sgc0_waiting_vd = true;
            s->sgc0_waiting_ext = false;
            s->sgc_regs[0x110 >> 2] = 1u;
        } else {
            /* trig_mode == 2: wait for EXT_SYNC rising edge */
            s->sgc0_active = false;
            s->sgc0_waiting_vd = false;
            s->sgc0_waiting_ext = true;
            s->sgc_regs[0x110 >> 2] = 1u;
        }
        return;
    case 0x114:
        s->sgc_regs[0x114 >> 2] = v & ~1u;
        if (v & 1u) {
            if (s->sgc0_active) {
                uint32_t hv = s->sgc_regs[0x120 >> 2];
                uint64_t h = (hv & 0x3FFFu) + 1ULL;
                uint64_t fticks = sgc0_frame_ticks(s);
                uint64_t elapsed = (now > s->sgc0_start_ns) ?
                                   (uint64_t)(now - s->sgc0_start_ns) : 0ULL;
                uint64_t tin = ((elapsed * 72ULL) / 1000ULL) % fticks;
                uint32_t v_cnt = (uint32_t)((tin / h) & 0x3FFFu);
                uint32_t h_cnt = (uint32_t)((tin % h) & 0x3FFFu);
                s->sgc_regs[0x118 >> 2] = (v_cnt << 16) | h_cnt;
            } else {
                s->sgc_regs[0x118 >> 2] = DFE_HVCNT_IDLE;
            }
        }
        return;
    case 0x118:
        return;
    case 0x11C:
        s->sgc_regs[0x11C >> 2] = v;
        if (v & 1u) {
            s->sgc_regs[0x128 >> 2] =
                (s->sgc_regs[0x128 >> 2] & ~0xFFu) | (s->sgc0_fcnt & 0xFFu);
        }
        return;
    default:
        s->sgc_regs[off >> 2] = v;
        return;
    }
}

static uint64_t dfe_aif_read(void *opaque, hwaddr off, unsigned size)
{
    Cxd90045DfeState *s = opaque;
    return s->aif_regs[off >> 2];
}

static void dfe_aif_write(void *opaque, hwaddr off, uint64_t val,
                          unsigned size)
{
    Cxd90045DfeState *s = opaque;
    uint32_t v = (uint32_t)val;

    if (off == 0x1204 && (v & BIT(30))) {
        s->aif_regs[0x1200 >> 2] &= ~1u;
        s->aif_regs[0x1204 >> 2] = v & ~(3u << 30);
        return;
    }
    s->aif_regs[off >> 2] = v;
}

static uint64_t dfe_rdcw_read(void *opaque, hwaddr off, unsigned size)
{
    Cxd90045DfeState *s = opaque;
    return s->rdcw_regs[off >> 2];
}

static void dfe_rdcw_write(void *opaque, hwaddr off, uint64_t val,
                           unsigned size)
{
    Cxd90045DfeState *s = opaque;
    uint32_t v = (uint32_t)val;

    if (off == 0x1100) {
        s->rdcw_regs[0x1100 >> 2] = v;
        if (!(v & 1u)) {
            s->rdcw_regs[0x1110 >> 2] &= ~(1u | BIT(16));
        }
        return;
    }
    if (off == 0x1110) {
        if (v & 1u) {
            uint32_t act = (s->rdcw_regs[0x1100 >> 2] & 1u) ? BIT(16) : 0u;
            s->rdcw_regs[0x1110 >> 2] = (v & ~BIT(16)) | act;
        } else {
            s->rdcw_regs[0x1110 >> 2] = v & ~(1u | BIT(16));
        }
        return;
    }
    s->rdcw_regs[off >> 2] = v;
}

static uint64_t dfe_slvsec_read(void *opaque, hwaddr off, unsigned size)
{
    Cxd90045DfeState *s = opaque;

    if (off == 0x0B0 || off == 0x1B0) {
        return dfe_slvsec_locked(s) ? 1u : 0u;
    }
    return s->slvsec_regs[off >> 2];
}

static void dfe_slvsec_write(void *opaque, hwaddr off, uint64_t val,
                             unsigned size)
{
    Cxd90045DfeState *s = opaque;
    s->slvsec_regs[off >> 2] = (uint32_t)val;
}

#define DEFINE_DFE_OPS(prefix)                                                \
    static const MemoryRegionOps prefix##_ops = {                             \
        .read = prefix##_read,                                                \
        .write = prefix##_write,                                              \
        .endianness = DEVICE_LITTLE_ENDIAN,                                   \
        .valid = { .min_access_size = 1, .max_access_size = 4 },              \
        .impl  = { .min_access_size = 4, .max_access_size = 4 },              \
    }

DEFINE_DFE_OPS(dfe_irq_vic);
DEFINE_DFE_OPS(dfe_leo);
DEFINE_DFE_OPS(dfe_mphy);
DEFINE_DFE_OPS(dfe_mbox);
DEFINE_DFE_OPS(dfe_syncseq);
DEFINE_DFE_OPS(dfe_timer);
DEFINE_DFE_OPS(dfe_top);
DEFINE_DFE_OPS(dfe_sgc);
DEFINE_DFE_OPS(dfe_aif);
DEFINE_DFE_OPS(dfe_rdcw);
DEFINE_DFE_OPS(dfe_slvsec);

/* -------------------------------------------------------------------------
 * QOM Lifecycle, Reset, Inspection Properties & Factory
 * ------------------------------------------------------------------------- */

static void cxd90045_dfe_reset(DeviceState *dev)
{
    Cxd90045DfeState *s = CXD90045_DFE(dev);

    timer_del(s->ext_trig_timer);
    timer_del(s->sgc0_timer);

    memset(s->irq_vic_regs, 0, sizeof(s->irq_vic_regs));
    memset(s->leo_regs, 0, sizeof(s->leo_regs));
    memset(s->mphy_regs, 0, sizeof(s->mphy_regs));
    memset(s->mbox_regs, 0, sizeof(s->mbox_regs));
    memset(s->syncseq_regs, 0, sizeof(s->syncseq_regs));
    memset(s->timer_regs, 0, sizeof(s->timer_regs));
    memset(s->dfetop_regs, 0, sizeof(s->dfetop_regs));
    memset(s->sgc_regs, 0, sizeof(s->sgc_regs));
    memset(s->aif_regs, 0, sizeof(s->aif_regs));
    memset(s->rdcw_regs, 0, sizeof(s->rdcw_regs));
    memset(s->slvsec_regs, 0, sizeof(s->slvsec_regs));

    s->core_en = 0;
    s->irq_en = 0;
    s->irq_stat = 0;
    s->irq_en0 = 0;
    s->irq_stat0 = 0;
    s->irq_en1 = 0;
    s->irq_stat1 = 0;
    s->irq_en2 = 0;
    s->irq_stat2 = 0;
    s->vic_en = 0;
    s->vic_raw = 0;
    s->mbox_f8_written = false;
    s->mbox_fw_hdr = 0;

    s->sysv_vd_level = false;
    s->ext_sync_level = false;
    s->last_vd_ns = 0;
    s->sgc0_start_ns = 0;
    s->sgc0_active = false;
    s->sgc0_waiting_vd = false;
    s->sgc0_waiting_ext = false;
    s->sgc0_fcnt = DFE_FCNT_COLD;
    s->rdcw0_frames = 0;

    /* Hardware cold-boot register state */
    s->syncseq_regs[0x028 >> 2] = DFE_SYNCSEQ_DEF_LIMIT;
    s->syncseq_regs[0x030 >> 2] = DFE_SYNCSEQ_COLD_VAL;
    s->sgc_regs[0x118 >> 2] = DFE_HVCNT_IDLE;
    s->sgc_regs[0x128 >> 2] = DFE_FCNT_COLD;

    dfe_update_irq(s);
    qemu_set_irq(s->irq_err, 0);
}

static void cxd90045_dfe_init(Object *obj)
{
    Cxd90045DfeState *s = CXD90045_DFE(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->mr_irq_vic, obj, &dfe_irq_vic_ops, s,
                          "cxd90045.dfe-irq", DFE_IRQ_VIC_SIZE);
    memory_region_init_io(&s->mr_leo, obj, &dfe_leo_ops, s,
                          "cxd90045.dfe-leo", DFE_LEO_SIZE);
    memory_region_init_io(&s->mr_mphy, obj, &dfe_mphy_ops, s,
                          "cxd90045.dfe-mphy", DFE_MPHY_SIZE);
    memory_region_init_io(&s->mr_mbox, obj, &dfe_mbox_ops, s,
                          "cxd90045.dfe-mbox", DFE_MBOX_SIZE);
    memory_region_init_io(&s->mr_syncseq, obj, &dfe_syncseq_ops, s,
                          "cxd90045.dfe-syncseq", DFE_SYNCSEQ_SIZE);
    memory_region_init_io(&s->mr_timer, obj, &dfe_timer_ops, s,
                          "cxd90045.dfe-timer", DFE_TIMER_SIZE);
    memory_region_init_io(&s->mr_dfetop, obj, &dfe_top_ops, s,
                          "cxd90045.dfe-top", DFE_TOP_SIZE);
    memory_region_init_io(&s->mr_sgc, obj, &dfe_sgc_ops, s,
                          "cxd90045.dfe-sgc", DFE_SGC_SIZE);
    memory_region_init_io(&s->mr_aif, obj, &dfe_aif_ops, s,
                          "cxd90045.dfe-aif", DFE_AIF_SIZE);
    memory_region_init_io(&s->mr_rdcw, obj, &dfe_rdcw_ops, s,
                          "cxd90045.dfe-rdcw", DFE_RDCW_SIZE);
    memory_region_init_io(&s->mr_slvsec, obj, &dfe_slvsec_ops, s,
                          "cxd90045.dfe-slvsec", DFE_SLVSEC_SIZE);

    sysbus_init_irq(sbd, &s->irq_main);
    sysbus_init_irq(sbd, &s->irq_err);

    qdev_init_gpio_in_named(DEVICE(obj), dfe_on_sysv_vd, "sysv-vd", 1);
    qdev_init_gpio_in_named(DEVICE(obj), dfe_on_ext_sync, "ext-sync", 1);
    qdev_init_gpio_out_named(DEVICE(obj), &s->p2s_sync, "p2s-sync", 1);

    s->ext_trig_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, dfe_ext_trig_tick, s);
    s->sgc0_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, dfe_sgc0_tick, s);
}

static void cxd90045_dfe_realize(DeviceState *dev, Error **errp)
{
    Cxd90045DfeState *s = CXD90045_DFE(dev);

    if (!s->sysmem) {
        return;
    }
    memory_region_add_subregion_overlap(s->sysmem, DFE_IRQ_VIC_BASE,
                                        &s->mr_irq_vic, DFE_OVERLAP_PRIO);
    memory_region_add_subregion_overlap(s->sysmem, DFE_LEO_BASE,
                                        &s->mr_leo, DFE_OVERLAP_PRIO);
    memory_region_add_subregion_overlap(s->sysmem, DFE_MPHY_BASE,
                                        &s->mr_mphy, DFE_OVERLAP_PRIO);
    memory_region_add_subregion_overlap(s->sysmem, DFE_MBOX_BASE,
                                        &s->mr_mbox, DFE_OVERLAP_PRIO);
    memory_region_add_subregion_overlap(s->sysmem, DFE_SYNCSEQ_BASE,
                                        &s->mr_syncseq, DFE_OVERLAP_PRIO);
    memory_region_add_subregion_overlap(s->sysmem, DFE_TIMER_BASE,
                                        &s->mr_timer, DFE_OVERLAP_PRIO);
    memory_region_add_subregion_overlap(s->sysmem, DFE_TOP_BASE,
                                        &s->mr_dfetop, DFE_OVERLAP_PRIO);
    memory_region_add_subregion_overlap(s->sysmem, DFE_SGC_BASE,
                                        &s->mr_sgc, DFE_OVERLAP_PRIO);
    memory_region_add_subregion_overlap(s->sysmem, DFE_AIF_BASE,
                                        &s->mr_aif, DFE_OVERLAP_PRIO);
    memory_region_add_subregion_overlap(s->sysmem, DFE_RDCW_BASE,
                                        &s->mr_rdcw, DFE_OVERLAP_PRIO);
    memory_region_add_subregion_overlap(s->sysmem, DFE_SLVSEC_BASE,
                                        &s->mr_slvsec, DFE_OVERLAP_PRIO);
}

static bool dfe_get_sgc0_active(Object *obj, Error **errp)
{
    return CXD90045_DFE(obj)->sgc0_active;
}

static bool dfe_get_slvsec_locked(Object *obj, Error **errp)
{
    return dfe_slvsec_locked(CXD90045_DFE(obj));
}

static void dfe_get_int_prop(Object *obj, Visitor *v, const char *name,
                             void *opaque, Error **errp)
{
    Cxd90045DfeState *s = CXD90045_DFE(obj);
    int64_t val = 0;

    if (strcmp(name, "rdcw0-frames") == 0) {
        val = s->rdcw0_frames;
    } else if (strcmp(name, "core-en") == 0) {
        val = s->core_en;
    }
    visit_type_int64(v, name, &val, errp);
}

static const VMStateDescription vmstate_cxd90045_dfe = {
    .name = "cxd90045-dfe",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(irq_vic_regs, Cxd90045DfeState, DFE_IRQ_VIC_SIZE >> 2),
        VMSTATE_UINT32_ARRAY(leo_regs, Cxd90045DfeState, DFE_LEO_SIZE >> 2),
        VMSTATE_UINT32_ARRAY(mphy_regs, Cxd90045DfeState, DFE_MPHY_SIZE >> 2),
        VMSTATE_UINT32_ARRAY(mbox_regs, Cxd90045DfeState, DFE_MBOX_SIZE >> 2),
        VMSTATE_UINT32_ARRAY(syncseq_regs, Cxd90045DfeState, DFE_SYNCSEQ_SIZE >> 2),
        VMSTATE_UINT32_ARRAY(timer_regs, Cxd90045DfeState, DFE_TIMER_SIZE >> 2),
        VMSTATE_UINT32_ARRAY(dfetop_regs, Cxd90045DfeState, DFE_TOP_SIZE >> 2),
        VMSTATE_UINT32_ARRAY(sgc_regs, Cxd90045DfeState, DFE_SGC_SIZE >> 2),
        VMSTATE_UINT32_ARRAY(aif_regs, Cxd90045DfeState, DFE_AIF_SIZE >> 2),
        VMSTATE_UINT32_ARRAY(rdcw_regs, Cxd90045DfeState, DFE_RDCW_SIZE >> 2),
        VMSTATE_UINT32_ARRAY(slvsec_regs, Cxd90045DfeState, DFE_SLVSEC_SIZE >> 2),
        VMSTATE_UINT32(core_en, Cxd90045DfeState),
        VMSTATE_UINT32(irq_en, Cxd90045DfeState),
        VMSTATE_UINT32(irq_stat, Cxd90045DfeState),
        VMSTATE_UINT32(irq_en0, Cxd90045DfeState),
        VMSTATE_UINT32(irq_stat0, Cxd90045DfeState),
        VMSTATE_UINT32(irq_en1, Cxd90045DfeState),
        VMSTATE_UINT32(irq_stat1, Cxd90045DfeState),
        VMSTATE_UINT32(irq_en2, Cxd90045DfeState),
        VMSTATE_UINT32(irq_stat2, Cxd90045DfeState),
        VMSTATE_UINT32(vic_en, Cxd90045DfeState),
        VMSTATE_UINT32(vic_raw, Cxd90045DfeState),
        VMSTATE_BOOL(mbox_f8_written, Cxd90045DfeState),
        VMSTATE_UINT32(mbox_fw_hdr, Cxd90045DfeState),
        VMSTATE_BOOL(sysv_vd_level, Cxd90045DfeState),
        VMSTATE_BOOL(ext_sync_level, Cxd90045DfeState),
        VMSTATE_INT64(last_vd_ns, Cxd90045DfeState),
        VMSTATE_INT64(sgc0_start_ns, Cxd90045DfeState),
        VMSTATE_BOOL(sgc0_active, Cxd90045DfeState),
        VMSTATE_BOOL(sgc0_waiting_vd, Cxd90045DfeState),
        VMSTATE_BOOL(sgc0_waiting_ext, Cxd90045DfeState),
        VMSTATE_UINT32(sgc0_fcnt, Cxd90045DfeState),
        VMSTATE_UINT32(rdcw0_frames, Cxd90045DfeState),
        VMSTATE_TIMER_PTR(ext_trig_timer, Cxd90045DfeState),
        VMSTATE_TIMER_PTR(sgc0_timer, Cxd90045DfeState),
        VMSTATE_END_OF_LIST()
    }
};

static void cxd90045_dfe_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = cxd90045_dfe_realize;
    dc->vmsd = &vmstate_cxd90045_dfe;
    device_class_set_legacy_reset(dc, cxd90045_dfe_reset);

    object_class_property_add_bool(klass, "sgc0-active",
                                   dfe_get_sgc0_active, NULL);
    object_class_property_add_bool(klass, "slvsec-locked",
                                   dfe_get_slvsec_locked, NULL);
    object_class_property_add(klass, "rdcw0-frames", "int",
                              dfe_get_int_prop, NULL, NULL, NULL);
    object_class_property_add(klass, "core-en", "int",
                              dfe_get_int_prop, NULL, NULL, NULL);
}

static const TypeInfo cxd90045_dfe_info = {
    .name = TYPE_CXD90045_DFE,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(Cxd90045DfeState),
    .instance_init = cxd90045_dfe_init,
    .class_init = cxd90045_dfe_class_init,
};

static void cxd90045_dfe_register_types(void)
{
    type_register_static(&cxd90045_dfe_info);
}
type_init(cxd90045_dfe_register_types)

DeviceState *cxd90045_dfe_create(MachineState *machine,
                                 MemoryRegion *sysmem,
                                 DeviceState *p2s1,
                                 DeviceState *vph,
                                 const Cxd90045LiveViewSource *lv_src,
                                 qemu_irq irq_main,
                                 qemu_irq irq_err)
{
    DeviceState *dev = qdev_new(TYPE_CXD90045_DFE);
    Cxd90045DfeState *s = CXD90045_DFE(dev);

    s->sysmem = sysmem;
    s->p2s1 = p2s1;
    s->lv_src = lv_src;

    object_property_add_child(OBJECT(machine), "cxd90045-dfe", OBJECT(dev));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(dev), &error_fatal);

    if (irq_main) {
        sysbus_connect_irq(SYS_BUS_DEVICE(dev), 0, irq_main);
    }
    if (irq_err) {
        sysbus_connect_irq(SYS_BUS_DEVICE(dev), 1, irq_err);
    }
    if (vph) {
        qdev_connect_gpio_out_named(vph, "sysv-vd", 0,
                                    qdev_get_gpio_in_named(dev, "sysv-vd", 0));
    }
    if (p2s1) {
        qdev_connect_gpio_out_named(dev, "p2s-sync", 0,
                                    qdev_get_gpio_in_named(p2s1, "frame-sync",
                                                           0));
    }
    return dev;
}
