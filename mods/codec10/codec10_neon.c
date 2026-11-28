/* Optional NEON row coder (offset + clamp + minifloat curve, 8 samples per step).
 * The ILCE-6400 has no NEON (AT_HWCAP 0x1E0D7, E0758); this file is selected only when the
 * host program finds HWCAP_NEON, e.g. on another BIONZ X body or under QEMU cortex-a9.
 * Output is bit-identical to code_row_ref in codec10.c.
 */
#include <arm_neon.h>
#include <stdint.h>

void c10_code_row_neon(const uint16_t *src, uint16_t *dst, uint32_t n, uint32_t offset,
                       uint32_t in_bits, uint32_t curve, uint32_t m);

static uint32_t mf_scalar(uint32_t x, uint32_t m) {
    int32_t e = (int32_t)(31u - (uint32_t)__builtin_clz(x | 1u)) - (int32_t)m;
    e &= ~(e >> 31);
    return ((uint32_t)e << m) + (x >> e);
}

void c10_code_row_neon(const uint16_t *src, uint16_t *dst, uint32_t n, uint32_t offset,
                       uint32_t in_bits, uint32_t curve, uint32_t m) {
    uint32_t maxv = (1u << in_bits) - 1u, i = 0;
    uint16x8_t off = vdupq_n_u16((uint16_t)offset), mx = vdupq_n_u16((uint16_t)maxv);
    uint16x8_t one = vdupq_n_u16(1);
    int16x8_t vm = vdupq_n_s16((int16_t)m), base = vdupq_n_s16((int16_t)(15 - (int32_t)m));
    int16x8_t zero = vdupq_n_s16(0);
    for (; i + 8u <= n; i += 8u) {
        uint16x8_t x = vminq_u16(vqsubq_u16(vld1q_u16(src + i), off), mx);
        if (curve) {
            int16x8_t e = vsubq_s16(base, vreinterpretq_s16_u16(vclzq_u16(vorrq_u16(x, one))));
            e = vmaxq_s16(e, zero);
            x = vaddq_u16(vshlq_u16(vreinterpretq_u16_s16(e), vm), vshlq_u16(x, vnegq_s16(e)));
        }
        vst1q_u16(dst + i, x);
    }
    for (; i < n; i++) {
        uint32_t v = src[i] > offset ? src[i] - offset : 0u;
        if (v > maxv) v = maxv;
        dst[i] = (uint16_t)(curve ? mf_scalar(v, m) : v);
    }
}
