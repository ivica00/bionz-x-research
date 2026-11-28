/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * From voxivoid/a6000-qemu (commit 62cf3bc)
 *   qemu-overlay/include/hw/misc/cxd90014_jog.h,
 * ported to QEMU 11.1.3.  voxivoid/a6000-qemu adapts
 * Oein/a5000-emulator (GPL-2.0, Park SungHyun; base commit 6ad36bd) to
 * the ILCE-6000.  The original description follows; its docs/reference/
 * paths are files in voxivoid/a6000-qemu.
 */
#ifndef HW_MISC_CXD90014_JOG_H
#define HW_MISC_CXD90014_JOG_H

#include "hw/core/qdev.h"

#define TYPE_CXD90014_JOG "cxd90014-jog"

/* Rotate the control wheel by n detents (positive = CW). */
void cxd90014_jog_step(DeviceState *dev, int n);
/* Channel 0 = rear control wheel, 1 = top control dial. */
void cxd90014_jog_step_ch(DeviceState *dev, int ch, int n);

#endif
