/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * From voxivoid/a6000-qemu (commit 62cf3bc)
 *   qemu-overlay/hw/misc/cxd90014_jog.c,
 * ported to QEMU 11.1.3.  voxivoid/a6000-qemu adapts
 * Oein/a5000-emulator (GPL-2.0, Park SungHyun; base commit 6ad36bd) to
 * the ILCE-6000.  The original description follows; its docs/reference/
 * paths are files in voxivoid/a6000-qemu.
 */
/*
 * CXD90014 TPU block 0xF2404000, channels 0 and 1 (0x100 apart): the two
 * JogTpu up/down counters.
 *
 * av-cam reads a 16-bit counter at +0x14 of each channel and turns the
 * wrap-around delta into dial events (docs/reference/RE_WHEEL_ZOOM_DATE.md
 * section 1).  Channel 0 (+0x014) is the rear control wheel, channel 1
 * (+0x114) the top control dial of the A6000 (polled about 40 times a
 * second from boot; it used to sit on the io-f0 regfile and never moved).
 * Every other register echoes what was written (as the io-f0 catch-all
 * regfile underneath would).  Host keys move the counters through
 * cxd90014_jog_step_ch().
 */

#include "qemu/osdep.h"
#include "hw/core/sysbus.h"
#include "qemu/log.h"
#include "qemu/module.h"
#include "qom/object.h"
#include "hw/misc/cxd90014_jog.h"

OBJECT_DECLARE_SIMPLE_TYPE(CXDJogState, CXD90014_JOG)

#define JOG_SIZE    0x200
#define JOG_COUNT   0x14
#define JOG_CH(off) ((off) >> 8)

struct CXDJogState {
    SysBusDevice parent_obj;
    MemoryRegion mr;
    uint32_t regs[JOG_SIZE / 4];
    uint16_t count[2];
};

void cxd90014_jog_step_ch(DeviceState *dev, int ch, int n)
{
    CXDJogState *s = CXD90014_JOG(dev);

    s->count[ch & 1] = (uint16_t)(s->count[ch & 1] + n);
}

void cxd90014_jog_step(DeviceState *dev, int n)
{
    cxd90014_jog_step_ch(dev, 0, n);
}

static uint64_t jog_read(void *opaque, hwaddr off, unsigned size)
{
    CXDJogState *s = opaque;
    uint32_t v = s->regs[off >> 2];

    if ((off & 0xFC) == JOG_COUNT) {
        v = s->count[JOG_CH(off)];
    }
    v >>= (off & 3) * 8;
    if (size < 4) {
        v &= (1u << (size * 8)) - 1;
    }
    return v;
}

static void jog_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    CXDJogState *s = opaque;
    unsigned sh = (off & 3) * 8;
    uint32_t mask = size >= 4 ? 0xffffffffu : ((1u << (size * 8)) - 1) << sh;
    uint32_t *r = &s->regs[off >> 2];

    *r = (*r & ~mask) | (((uint32_t)v << sh) & mask);
    if ((off & 0xFC) == JOG_COUNT) {
        s->count[JOG_CH(off)] = *r;
    }
}

static const MemoryRegionOps jog_ops = {
    .read = jog_read, .write = jog_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4 },
    .impl = { .min_access_size = 1, .max_access_size = 4 },
};

static void jog_reset(DeviceState *dev)
{
    CXDJogState *s = CXD90014_JOG(dev);

    memset(s->regs, 0, sizeof(s->regs));
    s->count[0] = s->count[1] = 0;
}

static void jog_realize(DeviceState *dev, Error **errp)
{
    CXDJogState *s = CXD90014_JOG(dev);

    memory_region_init_io(&s->mr, OBJECT(dev), &jog_ops, s, "cxd90014-jog",
                          JOG_SIZE);
    sysbus_init_mmio(SYS_BUS_DEVICE(dev), &s->mr);
}

static void jog_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = jog_realize;
    device_class_set_legacy_reset(dc, jog_reset);
}

static const TypeInfo jog_info = {
    .name = TYPE_CXD90014_JOG,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(CXDJogState),
    .class_init = jog_class_init,
};

static void jog_register(void)
{
    type_register_static(&jog_info);
}
type_init(jog_register)
