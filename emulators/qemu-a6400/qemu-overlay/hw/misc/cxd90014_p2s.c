/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * Adapted for sony-a6400 from Oein/a5000-emulator hw/misc/cxd90014_p2s.c
 * (GPL-2.0, Park SungHyun).  Trace events are the first few stderr lines.
 * The a6400 av-cam.bin contains the same unit-0 bases 0xF3C00300,
 * 0xF3C00800 and 0xF3C00900.
 */

/*
 * CXD90014 P2S ("parallel-to-serial") sensor register writer, unit 0 (a5000).
 *
 * PA 0xF3C00300..0xF3C009FF (the driver's unit table av-cam dword_43B1DC5C
 * = {0xF3C00300, 0xF3C00800, 0xF3C00900}), GIC IRQ 38.  The device covers
 * the whole page 0xF3C00000..0xF3C00FFF: QEMU's TLB cannot map an MMIO
 * region whose start is not page aligned across a page (the in-page offset
 * collides with the section index), and the guest page size is 1 KiB here.
 * 0xF3C00000..0x2FF and 0xA00..0xFFF read back as written (-d unimp).
 * The IMX156 imager is configured only through this write-only serialiser
 * (docs/RE_SENSOR_ISP.md §1.3).  Register use by av-cam.bin 1.10:
 *
 *   0x300 bit0 set at config; 0x304 bit0, 0x308 bits0-2 = sync config;
 *   0x30C low byte = byte count/flags (436AE62C/436AE684)
 *   0x340 + 4*i payload, one byte per word (436AE6A4)
 *   0x800 mode bits0-1, 0x808 kick toggle 1,0,1 (436AE62C/436AE734)
 *   0x904 bit0 transfer-done status; written |=1 before every start and by
 *         the IRQ 38 handler as ack -> write-1-to-clear (436AE758,
 *         436AE784 via ISR 436AE3EC)
 *   0x908 bit0 start / busy: the driver sets it to start a transfer and
 *         refuses a new one while it reads 1 (43A49854, 436AE7A8)
 *
 * Model: a start (0x908 bit0 written 1) completes after a short serial
 * transfer time; then 0x908 bit0 clears, 0x904 bit0 sets and IRQ 38 is
 * raised until 0x904 bit0 is written 1.  The transfer time is a
 * hypothesis: the driver waits at most 1-2 ms (436AE4EC / 436AE458).
 * Everything else reads back as written; accesses outside the registers
 * named above are logged under -d unimp.
 */

#include "qemu/osdep.h"
#include "hw/misc/cxd90045_dfe.h"
#include "hw/core/sysbus.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "migration/vmstate.h"
#include "qapi/error.h"
#include "qapi/visitor.h"
#include "qemu/timer.h"
#include "qemu/log.h"
#include "qemu/module.h"
#include "qom/object.h"

#define TYPE_CXD_P2S "cxd90014-p2s"
OBJECT_DECLARE_SIMPLE_TYPE(P2SState, CXD_P2S)

#define P2S_DEFAULT_BASE 0xF3C00000ULL
#define P2S_SIZE 0x1000
#define XFER_US 50 /* hypothesis: < the 1 ms driver wait */
#define P2S_TRACE_LIMIT 64
#define P2S_DATA_START 0x340
#define P2S_DATA_END 0x740
#define P2S_DATA_WORDS ((P2S_DATA_END - P2S_DATA_START) / 4)
#define IMX284_REG_SPACE 0x1000

typedef struct Imx284ColFingerprint {
    int col;
    const char *desc;
    uint8_t r0000, r0001, r0002, r000f, r0017, r0420, r0803, r0924;
    uint16_t xhs, xvs;
} Imx284ColFingerprint;

static const Imx284ColFingerprint imx284_cols[] = {
    {  0, "still_full_6000x4000_14bit",    0x04, 0x04, 0x00, 0x30, 0x1A, 0x7E, 0x80, 0x00, 0x006A, 0x046C },
    {  1, "still_cont_hi_6000x4000_12bit", 0x0C, 0x04, 0x00, 0x30, 0x1A, 0x02, 0x80, 0x01, 0x006A, 0x046C },
    {  2, "still_silent_6000x4000_12bit",  0x0C, 0x04, 0x00, 0x30, 0x1A, 0x02, 0x22, 0x01, 0x006A, 0x046C },
    {  3, "liveview_1996x570_11bit",       0x2C, 0x40, 0x10, 0x18, 0x04, 0x99, 0x11, 0x30, 0x006A, 0x046C },
    {  4, "liveview_evf_1996x800_11bit",   0x24, 0x40, 0x10, 0x18, 0x06, 0x99, 0x11, 0x30, 0x006A, 0x046C },
    {  5, "draft_af_1996x438_11bit",       0x54, 0x00, 0x10, 0x18, 0x02, 0x99, 0x11, 0x30, 0x006A, 0x046C },
    {  6, "movie_fhd_1996x1122_11bit",     0x1C, 0x41, 0x10, 0x18, 0x04, 0x99, 0x00, 0x30, 0x0152, 0x0472 },
    {  7, "movie_fhd_crop_1752x986_11bit", 0x1C, 0x41, 0x10, 0x10, 0x04, 0x99, 0x11, 0x30, 0x021E, 0x03E6 },
    {  8, "draft_hi_1996x438_11bit",       0x54, 0x40, 0x10, 0x18, 0x02, 0x99, 0x11, 0x30, 0x006A, 0x046C },
    {  9, "movie_120p_1024x684_11bit",     0x0C, 0x41, 0x00, 0x18, 0x1A, 0x99, 0x00, 0x30, 0x001E, 0x02C4 },
    { 10, "movie_4k30_s35_4896x2760_11bit",0x0C, 0x41, 0x00, 0x18, 0x1A, 0x99, 0x00, 0x30, 0x028A, 0x0ADC },
    { 11, "movie_4k24_6k_6000x3376_11bit", 0x0C, 0x41, 0x00, 0x18, 0x1A, 0x99, 0x00, 0x30, 0x0156, 0x0D44 },
    { 12, "liveview_mag_1996x1332_12bit",  0x1C, 0x40, 0x10, 0x18, 0x04, 0x7E, 0x22, 0x01, 0x006A, 0x046C },
};

struct P2SState {
    SysBusDevice parent_obj;
    MemoryRegion mr;
    uint32_t r[P2S_SIZE / 4];
    uint32_t status;            /* +0x904 / +0xF04 */
    bool busy;                  /* +0x908 / +0xF08 bit0 */
    QEMUTimer *done;
    qemu_irq irq;
    uint64_t base;
    uint32_t channel_id;
    bool trace_transfers;
    bool frame_sync_gated;
    bool waiting_frame_sync;
    unsigned trace_count;
    unsigned traced_transfer;

    /* Stateful IMX284 sensor register bank & mode recognizer */
    uint8_t imx_regs[IMX284_REG_SPACE];
    bool imx_valid[IMX284_REG_SPACE];
    uint32_t imx_frames;
    uint32_t imx_writes;
    int32_t imx_active_col;
    const char *imx_mode_desc;
};

#define R(o) (s->r[(o) / 4])

static bool p2s_known(hwaddr off)
{
    return (off >= 0x300 && off <= 0x30C) ||
           (off >= P2S_DATA_START && off < P2S_DATA_END) ||
           off == 0x800 || off == 0x808 ||
           off == 0x904 || off == 0x908 ||
           off == 0xE00 || off == 0xE08 ||
           off == 0xF04 || off == 0xF08;
}

static uint16_t imx284_u16le(const P2SState *s, uint16_t reg)
{
    if (reg + 1 >= IMX284_REG_SPACE ||
        (!s->imx_valid[reg] && !s->imx_valid[reg + 1])) {
        return 0;
    }
    return (uint16_t)(s->imx_valid[reg] ? s->imx_regs[reg] : 0) |
           ((uint16_t)(s->imx_valid[reg + 1] ? s->imx_regs[reg + 1] : 0) << 8);
}

static void p2s_apply_imx284_frame(P2SState *s)
{
    uint8_t raw[P2S_DATA_WORDS];
    unsigned pos = 0;
    unsigned pkts = 0;
    unsigned writes = 0;

    for (unsigned i = 0; i < P2S_DATA_WORDS; i++) {
        raw[i] = R(P2S_DATA_START + 4 * i) & 0xff;
    }
    if (raw[0] != 0x81) {
        return;
    }
    while (pos < P2S_DATA_WORDS) {
        uint8_t sync = raw[pos];
        if (sync == 0x00) {
            break;
        }
        if (sync != 0x81 || pos + 2 > P2S_DATA_WORDS) {
            return;
        }
        uint8_t len = raw[pos + 1];
        if (len < 4 || pos + 2 + len > P2S_DATA_WORDS) {
            return;
        }
        uint8_t chip = raw[pos + 2];
        uint16_t reg = ((uint16_t)raw[pos + 3] << 8) | raw[pos + 4];
        unsigned klen = len - 3;
        if (chip == 0x81) {
            for (unsigned k = 0; k < klen; k++) {
                if (reg + k < IMX284_REG_SPACE) {
                    s->imx_regs[reg + k] = raw[pos + 5 + k];
                    s->imx_valid[reg + k] = true;
                    writes++;
                }
            }
        }
        pkts++;
        pos += 2 + len;
    }
    if (!writes) {
        return;
    }
    s->imx_frames++;
    s->imx_writes += writes;

    if (s->imx_valid[0x0000] && s->imx_valid[0x0001] && s->imx_valid[0x0002] &&
        s->imx_valid[0x000F] && s->imx_valid[0x0017] && s->imx_valid[0x0803]) {
        uint16_t xhs = imx284_u16le(s, 0x000A);
        uint16_t xvs = imx284_u16le(s, 0x000C);
        for (size_t i = 0; i < ARRAY_SIZE(imx284_cols); i++) {
            const Imx284ColFingerprint *fp = &imx284_cols[i];
            if (fp->r0000 != s->imx_regs[0x0000] ||
                fp->r0001 != s->imx_regs[0x0001] ||
                fp->r0002 != s->imx_regs[0x0002] ||
                fp->r000f != s->imx_regs[0x000F] ||
                fp->r0017 != s->imx_regs[0x0017] ||
                fp->r0803 != s->imx_regs[0x0803]) {
                continue;
            }
            if (s->imx_valid[0x0924] && fp->r0924 != s->imx_regs[0x0924]) {
                continue;
            }
            if ((fp->col == 3 || fp->col == 12) && s->imx_valid[0x0420] &&
                fp->r0420 != s->imx_regs[0x0420]) {
                continue;
            }
            if ((fp->col >= 9 && fp->col <= 11) && xhs && fp->xhs != xhs) {
                continue;
            }
            if ((fp->col >= 9 && fp->col <= 11) && xvs && fp->xvs != xvs) {
                continue;
            }
            if (s->imx_active_col != fp->col) {
                s->imx_active_col = fp->col;
                s->imx_mode_desc = fp->desc;
                if (s->trace_transfers) {
                    fprintf(stderr,
                            "[P2S imx284 ch%u] seq=%u pkts=%u writes=%u "
                            "col=%d mode=%s stream=%u xhs=0x%04x xvs=0x%04x "
                            "shs1=0x%04x pgc=0x%04x\n",
                            s->channel_id, s->imx_frames, pkts, writes,
                            s->imx_active_col, s->imx_mode_desc,
                            s->imx_valid[0x00B3] && s->imx_regs[0x00B3] == 0x90,
                            xhs, xvs,
                            imx284_u16le(s, 0x0003) & 0x0FFFu,
                            imx284_u16le(s, 0x0007) & 0x3FFFu);
                }
            }
            break;
        }
    } else if (s->imx_active_col < 0 && s->imx_valid[0x00D4] &&
               s->imx_regs[0x00D4] == 0xCA && s->imx_valid[0x00D8] &&
               s->imx_regs[0x00D8] == 0x30 && s->imx_valid[0x094F]) {
        /* WBI resume streams per-frame LiveView deltas (col 3, 1996x570) */
        s->imx_active_col = 3;
        s->imx_mode_desc = "liveview_1996x570_11bit";
    }
}

static void p2s_update(P2SState *s)
{
    qemu_set_irq(s->irq, s->status & 1);
}

static void p2s_done(void *opaque)
{
    P2SState *s = opaque;
    static int left = 4;

    s->busy = false;
    s->status |= 1;
    if (s->traced_transfer) {
        fprintf(stderr, "[P2S trace] done id=%u status=0x%08x\n",
                s->traced_transfer, s->status);
        s->traced_transfer = 0;
    }
    if (left > 0) {
        left--;
        fprintf(stderr, "[P2S] done\n");
    }
    p2s_update(s);
}

static void p2s_schedule_transfer(P2SState *s, hwaddr mode_off)
{
    if (s->trace_transfers && s->trace_count < P2S_TRACE_LIMIT) {
        char slots[2 * P2S_DATA_WORDS + 1];
        static const char hex[] = "0123456789abcdef";
        unsigned pos = 0;

        for (unsigned off = P2S_DATA_START; off < P2S_DATA_END; off += 4) {
            uint32_t byte = R(off) & 0xff;
            slots[pos++] = hex[byte >> 4];
            slots[pos++] = hex[byte & 0xf];
        }
        slots[pos] = '\0';
        s->traced_transfer = ++s->trace_count;
        fprintf(stderr, "[P2S trace] start id=%u config=0x%08x "
                "mode=0x%08x slots=%s\n",
                s->traced_transfer, R(0x30c), R(mode_off), slots);
        if (s->trace_count == P2S_TRACE_LIMIT) {
            fprintf(stderr, "[P2S trace] transfer budget reached\n");
        }
    }
    timer_mod(s->done, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
              XFER_US * SCALE_US);
}

static void p2s_on_frame_sync(void *opaque, int n, int level)
{
    P2SState *s = opaque;

    if (level && s->waiting_frame_sync) {
        s->waiting_frame_sync = false;
        p2s_apply_imx284_frame(s);
        p2s_schedule_transfer(s, s->channel_id ? 0xE00 : 0x800);
    }
}

static void p2s_start(P2SState *s, hwaddr mode_off)
{
    /* payload words 0x340.. hold one byte each; log the first 8 starts */
    static int left = 8;
    uint32_t b[16];

    for (int i = 0; i < 16; i++) {
        b[i] = R(P2S_DATA_START + 4 * i) & 0xff;
    }
    if (left > 0) {
        left--;
        fprintf(stderr, "[P2S] start n=0x%02x %08x %08x %08x %08x\n",
                R(0x30C) & 0xff,
                b[0] << 24 | b[1] << 16 | b[2] << 8 | b[3],
                b[4] << 24 | b[5] << 16 | b[6] << 8 | b[7],
                b[8] << 24 | b[9] << 16 | b[10] << 8 | b[11],
                b[12] << 24 | b[13] << 16 | b[14] << 8 | b[15]);
    }
    s->busy = true;
    if (s->frame_sync_gated && (R(mode_off) & 3u) == 0) {
        s->waiting_frame_sync = true;
        return;
    }
    p2s_apply_imx284_frame(s);
    p2s_schedule_transfer(s, mode_off);
}

static uint64_t p2s_read(void *opaque, hwaddr off, unsigned size)
{
    P2SState *s = opaque;
    uint32_t v;

    switch (off) {
    case 0x904:
    case 0xF04:
        v = s->status;
        break;
    case 0x908:
    case 0xF08:
        v = (R(off) & ~1u) | s->busy;
        break;
    default:
        v = R(off);
        break;
    }
    if (!p2s_known(off)) {
        qemu_log_mask(LOG_UNIMP, "cxd90014-p2s: rd 0x%08" HWADDR_PRIx
                      " -> 0x%x\n", off + s->base, v);
    }
    return v;
}

static void p2s_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    P2SState *s = opaque;

    switch (off) {
    case 0x904:
    case 0xF04:
        s->status &= ~v;
        p2s_update(s);
        return;
    case 0x908:
    case 0xF08:
        R(off) = v & ~1u;
        if ((v & 1) && !s->busy) {
            p2s_start(s, off == 0xF08 ? 0xE00 : 0x800);
        }
        return;
    }
    if (!p2s_known(off)) {
        qemu_log_mask(LOG_UNIMP, "cxd90014-p2s: wr 0x%08" HWADDR_PRIx
                      " = 0x%" PRIx64 "\n", off + s->base, v);
    }
    R(off) = v;
}

static const MemoryRegionOps p2s_ops = {
    .read = p2s_read, .write = p2s_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
};

static void p2s_reset(DeviceState *dev)
{
    P2SState *s = CXD_P2S(dev);
    memset(s->r, 0, sizeof(s->r));
    memset(s->imx_regs, 0, sizeof(s->imx_regs));
    memset(s->imx_valid, 0, sizeof(s->imx_valid));
    s->imx_frames = 0;
    s->imx_writes = 0;
    s->imx_active_col = -1;
    s->imx_mode_desc = "unconfigured";
    s->status = 0;
    s->busy = false;
    s->waiting_frame_sync = false;
    timer_del(s->done);
    if (s->traced_transfer) {
        fprintf(stderr, "[P2S trace] reset id=%u\n", s->traced_transfer);
        s->traced_transfer = 0;
    }
    p2s_update(s);
}

static char *p2s_get_imx_mode(Object *obj, Error **errp)
{
    P2SState *s = CXD_P2S(obj);
    return g_strdup(s->imx_mode_desc ? s->imx_mode_desc : "unconfigured");
}

static bool p2s_get_imx_streaming(Object *obj, Error **errp)
{
    P2SState *s = CXD_P2S(obj);
    return s->imx_valid[0x00B3] && s->imx_regs[0x00B3] == 0x90;
}

bool cxd90014_p2s_imx284_streaming(DeviceState *dev)
{
    if (!dev) {
        return false;
    }
    P2SState *s = CXD_P2S(dev);
    return s->imx_valid[0x00B3] && s->imx_regs[0x00B3] == 0x90 &&
           !s->waiting_frame_sync;
}

static void p2s_get_imx_int(Object *obj, Visitor *v, const char *name,
                            void *opaque, Error **errp)
{
    P2SState *s = CXD_P2S(obj);
    int64_t val = -1;

    if (strcmp(name, "imx284-active-col") == 0) {
        val = s->imx_active_col;
    } else if (strcmp(name, "imx284-frames") == 0) {
        val = s->imx_frames;
    } else if (strcmp(name, "imx284-writes") == 0) {
        val = s->imx_writes;
    } else if (strcmp(name, "imx284-shs1") == 0) {
        val = imx284_u16le(s, 0x0003) & 0x0FFFu;
    } else if (strcmp(name, "imx284-pgc") == 0) {
        val = imx284_u16le(s, 0x0007) & 0x3FFFu;
    }
    visit_type_int64(v, name, &val, errp);
}

static void p2s_init(Object *obj)
{
    P2SState *s = CXD_P2S(obj);
    s->imx_active_col = -1;
    s->imx_mode_desc = "unconfigured";
    memory_region_init_io(&s->mr, obj, &p2s_ops, s, "p2s", P2S_SIZE);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mr);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);
    qdev_init_gpio_in_named(DEVICE(obj), p2s_on_frame_sync, "frame-sync", 1);
    s->done = timer_new_ns(QEMU_CLOCK_VIRTUAL, p2s_done, s);
}

static const Property p2s_props[] = {
    DEFINE_PROP_UINT64("base", P2SState, base, P2S_DEFAULT_BASE),
    DEFINE_PROP_UINT32("channel-id", P2SState, channel_id, 0),
    DEFINE_PROP_BOOL("trace-transfers", P2SState, trace_transfers, false),
    DEFINE_PROP_BOOL("frame-sync-gated", P2SState, frame_sync_gated, false),
};

static const VMStateDescription vmstate_cxd_p2s = {
    .name = "cxd90014-p2s",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(r, P2SState, P2S_SIZE / 4),
        VMSTATE_UINT32(status, P2SState),
        VMSTATE_BOOL(busy, P2SState),
        VMSTATE_UINT32(trace_count, P2SState),
        VMSTATE_UINT32(traced_transfer, P2SState),
        VMSTATE_TIMER_PTR(done, P2SState),
        VMSTATE_END_OF_LIST()
    }
};

static void p2s_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    dc->vmsd = &vmstate_cxd_p2s;
    device_class_set_props(dc, p2s_props);
    device_class_set_legacy_reset(dc, p2s_reset);
    object_class_property_add_str(klass, "imx284-mode", p2s_get_imx_mode, NULL);
    object_class_property_add_bool(klass, "imx284-streaming",
                                   p2s_get_imx_streaming, NULL);
    object_class_property_add(klass, "imx284-active-col", "int",
                              p2s_get_imx_int, NULL, NULL, NULL);
    object_class_property_add(klass, "imx284-frames", "int",
                              p2s_get_imx_int, NULL, NULL, NULL);
    object_class_property_add(klass, "imx284-writes", "int",
                              p2s_get_imx_int, NULL, NULL, NULL);
    object_class_property_add(klass, "imx284-shs1", "int",
                              p2s_get_imx_int, NULL, NULL, NULL);
    object_class_property_add(klass, "imx284-pgc", "int",
                              p2s_get_imx_int, NULL, NULL, NULL);
}

static const TypeInfo p2s_info = {
    .name = TYPE_CXD_P2S,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(P2SState),
    .instance_init = p2s_init,
    .class_init = p2s_class_init,
};

static void p2s_register(void)
{
    type_register_static(&p2s_info);
}
type_init(p2s_register)
