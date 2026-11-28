/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * Sony CXD90045 / CXD90057 Digital Front-End (DFE) Sensor Timing,
 * Coprocessor Mailbox, SLVS-EC Receiver, and RdcW0 Bayer RAW DMA Model.
 */

#ifndef HW_MISC_CXD90045_DFE_H
#define HW_MISC_CXD90045_DFE_H

#include "qemu/osdep.h"
#include "hw/core/boards.h"
#include "hw/core/irq.h"
#include "hw/misc/cxd90045_cam.h"
#include "system/memory.h"

#define TYPE_CXD90045_DFE "cxd90045-dfe"

/*
 * Verified GIC interrupt IDs in ILCE-6400 av-cam.bin IRQ registration table
 * (0x645046F8 / 0x64504708):
 *   - GIC ID 135 (SPI 103): 0x6377DDBC ("IMG:dfe_int_handler0")
 *   - GIC ID 136 (SPI 104): 0x6377DC08 (DFE vector dispatcher via 0xFC01FF00)
 */
#define CXD90045_DFE_IRQ_ERR_GIC  135
#define CXD90045_DFE_IRQ_MAIN_GIC 136

bool cxd90014_p2s_imx284_streaming(DeviceState *dev);

DeviceState *cxd90045_dfe_create(MachineState *machine,
                                 MemoryRegion *sysmem,
                                 DeviceState *p2s1,
                                 DeviceState *vph,
                                 const Cxd90045LiveViewSource *lv_src,
                                 qemu_irq irq_main,
                                 qemu_irq irq_err);

#endif /* HW_MISC_CXD90045_DFE_H */
