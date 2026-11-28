/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * Written for the sony-a6000 machine of this directory (first published
 * in Kagander/a6k-emulator); not part of voxivoid/a6000-qemu.
 */
/*
 * CXD90014 digital front end (DFE) of the A6000 (av-cam 3.21): the DFE
 * interrupt source controller and the sync-generator (SGC) start bits.
 *
 * Evidence (av-cam.bin 3.21, VA = file offset + 0x43541000):
 *
 * Interrupt controller, MMIO 0 at 0xFC01F000 (page):
 *   The DFE ISR 436CF279 is registered on GIC 136 (0x88) in av-cam's IRQ
 *   table at 0x43FD5E30 ({2, 0x88, 0, handler}).  It reads the pending
 *   source number from 0xFC01FF00 (43AD6B94 -> 43AD6942: base 0xFC000000
 *   + 0x1FF00), dispatches sources 0..13 through a tbb at 436CF284
 *   (callback table 0x43E7EBF0, registered by 436CF0C8) and ends with a
 *   write of 1 to the same register (43AD6B8A -> 43AD694E).
 *   Sources 0 and 1 are the P2S serialisers DFE0/DFE1 (436D59DC /
 *   436D5A18, "P2S(DFE0)/(DFE1) Callback"), whose units are 0xFC088300 /
 *   0xFC089300 in av-cam's P2S table 0x43BC14E8; their handlers ack the
 *   unit's 0x904 bit 0 (436D5FDC) before the callback.
 *   Model: 14 level-sensitive inputs; +0xF00 reads the lowest asserted
 *   input (0xFFFFFFFF when none, which the ISR rejects as > 13); a write
 *   of +0xF00 is the end of interrupt; the GIC line is high while any
 *   input is asserted.  Only sources 0 and 1 are driven.  Sources 2..13
 *   (4..7: the DFE's DMA engine at 0xFC01D000; 9, 10: path-end status at
 *   0xFC0A0014 / 0xFC0A0020, used by a still capture) are not modelled;
 *   their registers stay on the io-fa catch-all or read back as written.
 *   The other words of the page read back as written.
 *
 * Sync generators, MMIO 1 at 0xFC0A0000 (64 KiB) and MMIO 2 at
 *   0xFC099000 (4 KiB):
 *   DFECore SGC objects ("DrvSGC%d", "imgSGC", "OfSGC", "OutSGC",
 *   "ZpdPreSGC", "BlkSGC%d", "RcvSGC%d") check before each frame that
 *   the previous frame was started: vtable slot +0x14 (43AD758C ->
 *   436D1DAC) returns base+0xA1110 bit 0, and while it is still 1 av-cam
 *   prints "[DFECore]: xxxSGC:Last frame is not started." (436D2058..).
 *   The register blocks are at a 0x100 stride (0xFC0A1100 DrvSGC0,
 *   0xFC0A2100 DrvSGC1 = base 0xFC001000 + 0xA1100, ...); during a half
 *   press av-cam writes +0x10 = 1 on 0xFC0A1100/1400/2300/2600/2700 and
 *   0xFC099100 and polls it.
 *   Model: bit 0 of every +0x10 word is a start request that the device
 *   accepts at the next frame start, i.e. it reads 1 until the next
 *   33367 us frame boundary (the engine frame, docs/reference/
 *   RE_ENGINE_TICK.md in voxivoid/a6000-qemu) and 0 after.  hypothesis:
 *   the clear point (frame start) and the frame period for the
 *   high-speed DFE sequence.  Everything else reads back.
 */

#include "qemu/osdep.h"
#include "hw/core/sysbus.h"
#include "hw/core/irq.h"
#include "qemu/timer.h"
#include "qemu/log.h"
#include "qemu/module.h"
#include "qom/object.h"
#include "trace.h"

#define TYPE_CXD_DFE "cxd90014-dfe"
OBJECT_DECLARE_SIMPLE_TYPE(DfeState, CXD_DFE)

#define DFE_SOURCES   14
#define FRAME_US      33367
#define SGC_A_SIZE    0x10000           /* 0xFC0A0000 */
#define SGC_B_SIZE    0x1000            /* 0xFC099000 */

struct DfeState {
    SysBusDevice parent_obj;
    MemoryRegion intc_mr, sga_mr, sgb_mr;
    uint32_t intc[0x1000 / 4];
    GHashTable *sgc;                    /* sparse readback, key = window|off */
    uint32_t level;                     /* asserted sources */
    qemu_irq irq;
    QEMUTimer *frame;
    unsigned pending_starts;
};

static void dfe_update(DfeState *s)
{
    qemu_set_irq(s->irq, s->level != 0);
}

static void dfe_set_source(void *opaque, int n, int level)
{
    DfeState *s = opaque;
    if (level) {
        s->level |= 1u << n;
    } else {
        s->level &= ~(1u << n);
    }
    trace_cxd_dfe_source(n, level);
    dfe_update(s);
}

static uint64_t intc_read(void *opaque, hwaddr off, unsigned size)
{
    DfeState *s = opaque;
    if (off == 0xF00) {
        return s->level ? ctz32(s->level) : 0xFFFFFFFFu;
    }
    return s->intc[off / 4];
}

static void intc_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    DfeState *s = opaque;
    if (off == 0xF00) {
        dfe_update(s);                  /* end of interrupt */
        return;
    }
    s->intc[off / 4] = v;
}

static const MemoryRegionOps intc_ops = {
    .read = intc_read, .write = intc_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
};

/* ---- SGC windows ---- */

static uint32_t sgc_get(DfeState *s, uint32_t key)
{
    return GPOINTER_TO_UINT(g_hash_table_lookup(s->sgc,
                                                GUINT_TO_POINTER(key)));
}

static void sgc_put(DfeState *s, uint32_t key, uint32_t v)
{
    g_hash_table_insert(s->sgc, GUINT_TO_POINTER(key), GUINT_TO_POINTER(v));
}

static gboolean sgc_accept(gpointer k, gpointer v, gpointer opaque)
{
    DfeState *s = opaque;
    uint32_t key = GPOINTER_TO_UINT(k), val = GPOINTER_TO_UINT(v);
    if ((key & 0xFF) == 0x10 && (val & 1)) {
        g_hash_table_insert(s->sgc, k, GUINT_TO_POINTER(val & ~1u));
        trace_cxd_dfe_sgc_start(key);
    }
    return FALSE;
}

static void dfe_frame(void *opaque)
{
    DfeState *s = opaque;
    /* collect first: inserting while iterating a GHashTable is not safe */
    GList *keys = g_hash_table_get_keys(s->sgc);
    for (GList *l = keys; l; l = l->next) {
        sgc_accept(l->data, g_hash_table_lookup(s->sgc, l->data), s);
    }
    g_list_free(keys);
    s->pending_starts = 0;
}

static void dfe_arm_frame(DfeState *s)
{
    if (!timer_pending(s->frame)) {
        int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        int64_t period = FRAME_US * SCALE_US;
        timer_mod(s->frame, (now / period + 1) * period);
    }
}

static uint64_t sgc_read(DfeState *s, uint32_t win, hwaddr off)
{
    return sgc_get(s, win | off);
}

static void sgc_write(DfeState *s, uint32_t win, hwaddr off, uint64_t v)
{
    sgc_put(s, win | off, v);
    if ((off & 0xFF) == 0x10 && (v & 1)) {
        s->pending_starts++;
        dfe_arm_frame(s);
    }
}

static uint64_t sga_read(void *o, hwaddr off, unsigned size)
{
    return sgc_read(o, 0x00000000, off);
}

static void sga_write(void *o, hwaddr off, uint64_t v, unsigned size)
{
    sgc_write(o, 0x00000000, off, v);
}

static uint64_t sgb_read(void *o, hwaddr off, unsigned size)
{
    return sgc_read(o, 0x00100000, off);
}

static void sgb_write(void *o, hwaddr off, uint64_t v, unsigned size)
{
    sgc_write(o, 0x00100000, off, v);
}

static const MemoryRegionOps sga_ops = {
    .read = sga_read, .write = sga_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
};

static const MemoryRegionOps sgb_ops = {
    .read = sgb_read, .write = sgb_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
};

static void dfe_reset(DeviceState *dev)
{
    DfeState *s = CXD_DFE(dev);
    memset(s->intc, 0, sizeof(s->intc));
    g_hash_table_remove_all(s->sgc);
    s->level = 0;
    s->pending_starts = 0;
    timer_del(s->frame);
    dfe_update(s);
}

static void dfe_init(Object *obj)
{
    DfeState *s = CXD_DFE(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    s->sgc = g_hash_table_new(g_direct_hash, g_direct_equal);
    memory_region_init_io(&s->intc_mr, obj, &intc_ops, s, "dfe-intc", 0x1000);
    memory_region_init_io(&s->sga_mr, obj, &sga_ops, s, "dfe-sgc-a",
                          SGC_A_SIZE);
    memory_region_init_io(&s->sgb_mr, obj, &sgb_ops, s, "dfe-sgc-b",
                          SGC_B_SIZE);
    sysbus_init_mmio(sbd, &s->intc_mr);
    sysbus_init_mmio(sbd, &s->sga_mr);
    sysbus_init_mmio(sbd, &s->sgb_mr);
    sysbus_init_irq(sbd, &s->irq);
    qdev_init_gpio_in(DEVICE(obj), dfe_set_source, DFE_SOURCES);
    s->frame = timer_new_ns(QEMU_CLOCK_VIRTUAL, dfe_frame, s);
}

static void dfe_class_init(ObjectClass *klass, const void *data)
{
    device_class_set_legacy_reset(DEVICE_CLASS(klass), dfe_reset);
}

static const TypeInfo dfe_info = {
    .name = TYPE_CXD_DFE,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(DfeState),
    .instance_init = dfe_init,
    .class_init = dfe_class_init,
};

static void dfe_register(void)
{
    type_register_static(&dfe_info);
}
type_init(dfe_register)
