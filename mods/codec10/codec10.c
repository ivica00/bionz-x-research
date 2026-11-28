/* Codec10 core: curve, tight pack, MED/LEFT prediction + block-adaptive Rice, container.
 * Freestanding C99: no libc, no allocation, no division. See docs/CODEC_DESIGN.md.
 */
#include "codec10.h"

#if defined(__ARM_FEATURE_SIMD32) && __ARM_FEATURE_SIMD32
#include <arm_acle.h>
#define C10_SIMD32 1
#endif

#ifndef C10_SMOD
int c10_neon_available = 0;   /* SMOD v2 forbids .bss; mods never select NEON */
#endif
#ifdef C10_HAVE_NEON
void c10_code_row_neon(const uint16_t *src, uint16_t *dst, uint32_t n, uint32_t offset,
                       uint32_t in_bits, uint32_t curve, uint32_t m);
#endif

typedef uint32_t __attribute__((may_alias)) c10_u32a;

/* ---------- small helpers ---------- */
static inline uint32_t bitlen32(uint32_t v) { return v ? 32u - (uint32_t)__builtin_clz(v) : 0u; }
static inline void put_le16(uint8_t *p, uint32_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static inline void put_le32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}
static inline uint32_t get_le16(const uint8_t *p) { return (uint32_t)p[0] | (uint32_t)p[1] << 8; }
static inline uint32_t get_le32(const uint8_t *p) {
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
static inline uint32_t bswap32(uint32_t v) { return __builtin_bswap32(v); }
static void zero_bytes(uint8_t *p, uint32_t n) { while (n--) *p++ = 0; }

/* Saturating offset + unsigned clamp, identical to UQSUB16 followed by min(x, 2^in_bits - 1). */
static inline uint32_t sat_offset(uint32_t x, uint32_t offset, uint32_t in_bits) {
    uint32_t v = x > offset ? x - offset : 0u;
    uint32_t maxv = (1u << in_bits) - 1u;
    return v > maxv ? maxv : v;
}

static inline uint32_t mf_fwd(uint32_t x, uint32_t m) {
    int32_t e = (int32_t)(31u - (uint32_t)__builtin_clz(x | 1u)) - (int32_t)m;
    e &= ~(e >> 31);                      /* max(e, 0), one BIC with ASR on ARM */
    return ((uint32_t)e << m) + (x >> e);
}

/* Two-halfword SIMD32 operations (ARMv6 media instructions; portable C elsewhere). */
#ifdef C10_SIMD32
#define UHADD16(a, b) __uhadd16((a), (b))
#define SSUB16(a, b)  __ssub16((a), (b))
#define UQSUB16(a, b) __uqsub16((a), (b))
#define USUB16(a, b)  __usub16((a), (b))
#else
static inline uint32_t UHADD16(uint32_t a, uint32_t b) {
    return (((a & 0xffffu) + (b & 0xffffu)) >> 1) | (((a >> 16) + (b >> 16)) >> 1) << 16;
}
static inline uint32_t SSUB16(uint32_t a, uint32_t b) {
    return ((a - b) & 0xffffu) | (((a >> 16) - (b >> 16)) & 0xffffu) << 16;
}
static inline uint32_t UQSUB16(uint32_t a, uint32_t b) {
    uint32_t lo = (a & 0xffffu) > (b & 0xffffu) ? (a & 0xffffu) - (b & 0xffffu) : 0u;
    uint32_t hi = (a >> 16) > (b >> 16) ? (a >> 16) - (b >> 16) : 0u;
    return lo | hi << 16;
}
#define USUB16(a, b) SSUB16((a), (b))
#endif

uint32_t c10_curve_fwd(const c10_params *p, uint32_t x) {
    return p->curve == C10_CURVE_MINIFLOAT ? mf_fwd(x, p->curve_m) : x;
}

uint32_t c10_curve_inv(const c10_params *p, uint32_t c) {
    if (p->curve != C10_CURVE_MINIFLOAT) return c;
    uint32_t m = p->curve_m;
    int32_t e = (int32_t)(c >> m) - 1;
    e &= ~(e >> 31);
    uint32_t mant = c - ((uint32_t)e << m);
    return (mant << e) + (e ? (1u << (e - 1)) : 0u);
}

/* ---------- parameters ---------- */
int c10_params_init(c10_params *p) {
    if (!p) return C10_E_PARAM;
    if (p->format != C10_FMT_BAYER && p->format != C10_FMT_GRAY) return C10_E_PARAM;
    if (p->width < 4u || p->height < 2u || p->width > C10_MAX_WIDTH || p->height > C10_MAX_HEIGHT)
        return C10_E_PARAM;
    if ((p->width & 1u) || (p->height & 1u)) return C10_E_PARAM;
    if (p->format == C10_FMT_BAYER && p->cfa > C10_CFA_BGGR) return C10_E_PARAM;
    if (p->in_bits < 8u || p->in_bits > 16u) return C10_E_PARAM;
    if (p->stripe_rows < 2u || (p->stripe_rows & 1u)) return C10_E_PARAM;
    if (p->mode > C10_MODE_BFL_AVG) return C10_E_PARAM;
    if (p->fps_den == 0u || p->fps_num == 0u) return C10_E_PARAM;
    uint32_t maxin = (1u << p->in_bits) - 1u;
    if (p->black > maxin || p->pedestal > p->black) return C10_E_PARAM;
    if (p->white == 0u) p->white = (uint16_t)maxin;
    if (p->white > maxin || p->white <= p->black) return C10_E_PARAM;
    if (p->curve == C10_CURVE_LINEAR) {
        p->curve_m = 0;
        p->coded_bits = p->in_bits;
    } else if (p->curve == C10_CURVE_MINIFLOAT) {
        if (p->curve_m < 1u || p->curve_m + 2u > p->in_bits) return C10_E_PARAM;
        uint32_t emax = p->in_bits - 1u - p->curve_m;
        uint32_t maxc = (emax << p->curve_m) + ((2u << p->curve_m) - 1u);
        p->coded_bits = (uint8_t)bitlen32(maxc);
    } else {
        return C10_E_PARAM;
    }
    if (p->mode == C10_MODE_BFL_AVG && p->coded_bits > 15u) return C10_E_PARAM;
    return C10_OK;
}

uint32_t c10_n_stripes(const c10_params *p) {
    uint32_t n = 0;
    for (uint32_t r = 0; r < p->height; r += p->stripe_rows) n++;
    return n;
}

uint32_t c10_stripe_rows_at(const c10_params *p, uint32_t stripe) {
    uint32_t y0 = stripe * p->stripe_rows;
    if (y0 >= p->height) return 0;
    uint32_t left = p->height - y0;
    return left < p->stripe_rows ? left : p->stripe_rows;
}

static uint32_t pack_bytes(const c10_params *p, uint32_t rows) {
    uint32_t bits = rows * p->width * p->coded_bits;
    return ((bits + 31u) >> 5) << 2;
}

uint32_t c10_stripe_bound(const c10_params *p, uint32_t rows) {
    return C10_STRIPE_HDR + pack_bytes(p, rows);
}

uint32_t c10_stripe_slack(const c10_params *p) {
    uint32_t blocks = (p->width + C10_RICE_BLOCK - 1u) >> 4;
    uint32_t bits = p->width * (C10_RICE_ESCAPE + p->coded_bits + 1u) + blocks * 4u;
    return (((bits + 31u) >> 5) << 2) + 8u;
}

uint32_t c10_frame_bound(const c10_params *p) {
    uint32_t n = c10_n_stripes(p), total = C10_FRAME_HDR + 4u * n;
    for (uint32_t s = 0; s < n; s++) total += c10_stripe_bound(p, c10_stripe_rows_at(p, s));
    return total;
}

uint32_t c10_scratch_bytes(const c10_params *p) { return 4u * 2u * p->width; }

/* ---------- row coding (offset + clamp + curve) ---------- */
static void code_row_ref(const c10_params *p, const uint16_t *src, uint16_t *dst, uint32_t n) {
    uint32_t off = (uint32_t)p->black - p->pedestal;
    for (uint32_t i = 0; i < n; i++)
        dst[i] = (uint16_t)c10_curve_fwd(p, sat_offset(src[i], off, p->in_bits));
}

static void code_row_fast(const c10_params *p, const uint16_t *src, uint16_t *dst, uint32_t n) {
    if (((uintptr_t)src | (uintptr_t)dst) & 3u) { code_row_ref(p, src, dst, n); return; }
    uint32_t off = (uint32_t)p->black - p->pedestal, m = p->curve_m, nb = p->in_bits;
    const c10_u32a *s = (const c10_u32a *)(const void *)src;
    c10_u32a *d = (c10_u32a *)(void *)dst;
    uint32_t pairs = n >> 1;
#ifdef C10_SIMD32
    uint32_t off2 = off | off << 16, maxv = (1u << nb) - 1u, max2 = maxv | maxv << 16;
    /* min(x, max) per halfword: x - uqsub(x, max), all ARMv6 SIMD32. */
    if (p->curve == C10_CURVE_MINIFLOAT) {
        for (uint32_t i = 0; i < pairs; i++) {
            uint32_t w = __uqsub16(s[i], off2);
            w = __usub16(w, __uqsub16(w, max2));
            d[i] = mf_fwd(w & 0xffffu, m) | mf_fwd(w >> 16, m) << 16;
        }
    } else if (off == 0u && nb == 16u) {
        for (uint32_t i = 0; i < pairs; i++) d[i] = s[i];
    } else {
        for (uint32_t i = 0; i < pairs; i++) {
            uint32_t w = __uqsub16(s[i], off2);
            d[i] = __usub16(w, __uqsub16(w, max2));
        }
    }
#else
    for (uint32_t i = 0; i < pairs; i++) {
        uint32_t w = s[i];
        uint32_t lo = sat_offset(w & 0xffffu, off, nb), hi = sat_offset(w >> 16, off, nb);
        if (p->curve == C10_CURVE_MINIFLOAT) { lo = mf_fwd(lo, m); hi = mf_fwd(hi, m); }
        d[i] = lo | hi << 16;
    }
#endif
    if (n & 1u) code_row_ref(p, src + n - 1u, dst + n - 1u, 1u);
}

static void code_row(const c10_params *p, int impl, const uint16_t *src, uint16_t *dst, uint32_t n) {
#ifdef C10_HAVE_NEON
    if (impl == C10_IMPL_NEON && c10_neon_available) {
        c10_code_row_neon(src, dst, n, (uint32_t)p->black - p->pedestal, p->in_bits, p->curve,
                          p->curve_m);
        return;
    }
#endif
    if (impl == C10_IMPL_REF) code_row_ref(p, src, dst, n);
    else code_row_fast(p, src, dst, n);
}

/* ---------- PACK: LSB-first little-endian words ---------- */
typedef struct { uint32_t *w; uint64_t acc; uint32_t n; } packw;

static inline void pack_put(packw *b, uint32_t v, uint32_t len) {
    b->acc |= (uint64_t)v << b->n;
    b->n += len;
    if (b->n >= 32u) { *b->w++ = (uint32_t)b->acc; b->acc >>= 32; b->n -= 32u; }
}

static void pack_row(packw *b, const uint16_t *c, uint32_t n, uint32_t bits, int fast) {
    uint32_t i = 0;
    if (fast && b->n == 0u && !((uintptr_t)c & 3u)) {
        const c10_u32a *q = (const c10_u32a *)(const void *)c;
        uint32_t *w = b->w;
        if (bits == 10u) {
            for (; i + 16u <= n; i += 16u, q += 8) {
                uint32_t p0 = q[0], p1 = q[1], p2 = q[2], p3 = q[3];
                uint32_t p4 = q[4], p5 = q[5], p6 = q[6], p7 = q[7];
                uint32_t c0 = p0 & 0x3ffu, c1 = p0 >> 16, c2 = p1 & 0x3ffu, c3 = p1 >> 16;
                uint32_t c4 = p2 & 0x3ffu, c5 = p2 >> 16, c6 = p3 & 0x3ffu, c7 = p3 >> 16;
                uint32_t c8 = p4 & 0x3ffu, c9 = p4 >> 16, c10 = p5 & 0x3ffu, c11 = p5 >> 16;
                uint32_t c12 = p6 & 0x3ffu, c13 = p6 >> 16, c14 = p7 & 0x3ffu, c15 = p7 >> 16;
                w[0] = c0 | c1 << 10 | c2 << 20 | c3 << 30;
                w[1] = c3 >> 2 | c4 << 8 | c5 << 18 | c6 << 28;
                w[2] = c6 >> 4 | c7 << 6 | c8 << 16 | c9 << 26;
                w[3] = c9 >> 6 | c10 << 4 | c11 << 14 | c12 << 24;
                w[4] = c12 >> 8 | c13 << 2 | c14 << 12 | c15 << 22;
                w += 5;
            }
        } else if (bits == 12u) {
            for (; i + 8u <= n; i += 8u, q += 4) {
                uint32_t p0 = q[0], p1 = q[1], p2 = q[2], p3 = q[3];
                uint32_t c0 = p0 & 0xfffu, c1 = p0 >> 16, c2 = p1 & 0xfffu, c3 = p1 >> 16;
                uint32_t c4 = p2 & 0xfffu, c5 = p2 >> 16, c6 = p3 & 0xfffu, c7 = p3 >> 16;
                w[0] = c0 | c1 << 12 | c2 << 24;
                w[1] = c2 >> 8 | c3 << 4 | c4 << 16 | c5 << 28;
                w[2] = c5 >> 4 | c6 << 8 | c7 << 20;
                w += 3;
            }
        } else if (bits == 16u) {
            for (; i + 2u <= n; i += 2u) *w++ = *q++;
        }
        b->w = w;
    }
    for (; i < n; i++) pack_put(b, c[i], bits);
}

/* 32-bit-accumulator put for the fast paths (same LSB-first stream as pack_put).
 * Requires b->acc < 2^b->n, which pack_put also leaves behind. len 1..32, v < 2^len. */
static inline void pack_put32(packw *b, uint32_t v, uint32_t len) {
    uint32_t acc = (uint32_t)b->acc | v << b->n, nn = b->n + len;
    if (nn >= 32u) { *b->w++ = acc; nn -= 32u; acc = nn ? v >> (len - nn) : 0u; }
    b->acc = acc; b->n = nn;
}

/* Fused offset + clamp + curve + pack for one row (10-bit: w % 16 == 0, 12-bit: w % 8 == 0),
 * so coded values never go through memory. Returns 0 when the row does not qualify. */
#define C10_CODE2(word, lo, hi) do {                                              \
        uint32_t v_ = UQSUB16((word), off2);                                      \
        v_ = USUB16(v_, UQSUB16(v_, max2));                                       \
        lo = v_ & 0xffffu; hi = v_ >> 16;                                         \
        if (mf) { lo = mf_fwd(lo, m); hi = mf_fwd(hi, m); }                       \
    } while (0)

static int pack_row_fused(const c10_params *p, const uint16_t *src, packw *b) {
    uint32_t w = p->width, cb = p->coded_bits;
    if (b->n || ((uintptr_t)src & 3u) || !((cb == 10u && !(w & 15u)) || (cb == 12u && !(w & 7u))))
        return 0;
    uint32_t off = (uint32_t)p->black - p->pedestal, maxv = (1u << p->in_bits) - 1u;
    uint32_t off2 = off | off << 16, max2 = maxv | maxv << 16, m = p->curve_m;
    int mf = p->curve == C10_CURVE_MINIFLOAT;
    const c10_u32a *q = (const c10_u32a *)(const void *)src;
    uint32_t *o = b->w;
    if (cb == 10u) {
        for (uint32_t i = 0; i < w; i += 16u, q += 8, o += 5) {
            __builtin_prefetch((const void *)(q + 32));
            uint32_t c0, c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11, c12, c13, c14, c15;
            C10_CODE2(q[0], c0, c1); C10_CODE2(q[1], c2, c3); C10_CODE2(q[2], c4, c5);
            C10_CODE2(q[3], c6, c7); C10_CODE2(q[4], c8, c9); C10_CODE2(q[5], c10, c11);
            C10_CODE2(q[6], c12, c13); C10_CODE2(q[7], c14, c15);
            o[0] = c0 | c1 << 10 | c2 << 20 | c3 << 30;
            o[1] = c3 >> 2 | c4 << 8 | c5 << 18 | c6 << 28;
            o[2] = c6 >> 4 | c7 << 6 | c8 << 16 | c9 << 26;
            o[3] = c9 >> 6 | c10 << 4 | c11 << 14 | c12 << 24;
            o[4] = c12 >> 8 | c13 << 2 | c14 << 12 | c15 << 22;
        }
    } else {
        for (uint32_t i = 0; i < w; i += 8u, q += 4, o += 3) {
            __builtin_prefetch((const void *)(q + 32));
            uint32_t c0, c1, c2, c3, c4, c5, c6, c7;
            C10_CODE2(q[0], c0, c1); C10_CODE2(q[1], c2, c3);
            C10_CODE2(q[2], c4, c5); C10_CODE2(q[3], c6, c7);
            o[0] = c0 | c1 << 12 | c2 << 24;
            o[1] = c2 >> 8 | c3 << 4 | c4 << 16 | c5 << 28;
            o[2] = c5 >> 4 | c6 << 8 | c7 << 20;
        }
    }
    b->w = o;
    return 1;
}

static void pack_flush(packw *b) {
    if (b->n) { *b->w++ = (uint32_t)b->acc; b->acc = 0; b->n = 0; }
}

static void unpack_stream(const uint8_t *in, uint32_t count, uint32_t bits, uint16_t *dst,
                          uint32_t width, uint32_t stride) {
    uint64_t acc = 0; uint32_t n = 0, x = 0, mask = (1u << bits) - 1u;
    const uint8_t *p = in;
    for (uint32_t i = 0; i < count; i++) {
        if (n < bits) { acc |= (uint64_t)get_le32(p) << n; p += 4; n += 32u; }
        dst[x] = (uint16_t)(acc & mask);
        acc >>= bits; n -= bits;
        if (++x == width) { x = 0; dst += stride; }
    }
}

/* ---------- RICE: MSB-first big-endian words ---------- */
typedef struct { uint32_t *w; uint64_t acc; uint32_t n; } bitw;

static inline void bw_put(bitw *b, uint32_t v, uint32_t len) {  /* len <= 32 */
    b->acc = (b->acc << len) | v;
    b->n += len;
    if (b->n >= 32u) { b->n -= 32u; *b->w++ = bswap32((uint32_t)(b->acc >> b->n)); }
}

static void bw_flush(bitw *b) {
    if (b->n) bw_put(b, 0, 32u - b->n);
}

static inline uint32_t predict(const uint16_t *cur, const uint16_t *up, uint32_t i, uint32_t y,
                               uint32_t d, uint32_t med, uint32_t mid) {
    if (y < d) return i < d ? mid : cur[i - d];
    if (i < d) return up[i];
    uint32_t a = cur[i - d];
    if (!med) return a;
    uint32_t b = up[i], c = up[i - d];
    uint32_t mn = a < b ? a : b, mx = a < b ? b : a;
    int32_t g = (int32_t)(a + b) - (int32_t)c;
    if (g < (int32_t)mn) g = (int32_t)mn;
    if (g > (int32_t)mx) g = (int32_t)mx;
    return (uint32_t)g;
}

static inline uint32_t rice_k(uint32_t sum) {
    uint32_t k = bitlen32(sum >> 5);
    return k > 15u ? 15u : k;
}

static void rice_row(bitw *b, const uint16_t *cur, const uint16_t *up, uint32_t w, uint32_t y,
                     uint32_t d, uint32_t med, uint32_t cb) {
    uint32_t mid = 1u << (cb - 1u), u[C10_RICE_BLOCK];
    for (uint32_t i0 = 0; i0 < w; i0 += C10_RICE_BLOCK) {
        uint32_t nblk = w - i0 < C10_RICE_BLOCK ? w - i0 : C10_RICE_BLOCK, sum = 0;
        if (y >= d && i0 >= d) {          /* interior: no boundary cases */
            for (uint32_t j = 0; j < nblk; j++) {
                uint32_t i = i0 + j, a = cur[i - d], pr = a;
                if (med) {
                    uint32_t bb = up[i], c = up[i - d];
                    uint32_t mn = a < bb ? a : bb, mx = a < bb ? bb : a;
                    int32_t g = (int32_t)(a + bb) - (int32_t)c;
                    g = g < (int32_t)mn ? (int32_t)mn : g;
                    g = g > (int32_t)mx ? (int32_t)mx : g;
                    pr = (uint32_t)g;
                }
                int32_t r = (int32_t)cur[i] - (int32_t)pr;
                uint32_t z = (uint32_t)(r << 1) ^ (uint32_t)(r >> 31);
                u[j] = z; sum += z;
            }
        } else {
            for (uint32_t j = 0; j < nblk; j++) {
                uint32_t i = i0 + j;
                int32_t r = (int32_t)cur[i] - (int32_t)predict(cur, up, i, y, d, med, mid);
                uint32_t z = (uint32_t)(r << 1) ^ (uint32_t)(r >> 31);
                u[j] = z; sum += z;
            }
        }
        uint32_t k = rice_k(sum);
        bw_put(b, k, 4u);
        uint32_t kmask = (1u << k) - 1u;
        for (uint32_t j = 0; j < nblk; j++) {
            uint32_t z = u[j], q = z >> k;
            if (q < C10_RICE_ESCAPE) {
                uint32_t len = q + 1u + k;
                if (len <= 32u) bw_put(b, (1u << k) | (z & kmask), len);
                else { bw_put(b, 0, q); bw_put(b, (1u << k) | (z & kmask), k + 1u); }
            } else {
                bw_put(b, 0, C10_RICE_ESCAPE);
                bw_put(b, z, cb + 1u);
            }
        }
    }
}

/* ---------- BFL_AVG: average predictor, block fixed-length two's complement ---------- */
static inline uint32_t pred_avg(const uint16_t *cur, const uint16_t *up, uint32_t i, uint32_t y,
                                uint32_t d, uint32_t mid) {
    if (y < d) return i < d ? mid : cur[i - d];
    if (i < d) return up[i];
    return ((uint32_t)cur[i - d] + up[i]) >> 1;
}

static void bfl_row_ref(packw *b, const uint16_t *cur, const uint16_t *up, uint32_t w, uint32_t y,
                        uint32_t d, uint32_t cb) {
    uint32_t mid = 1u << (cb - 1u);
    int32_t r[C10_BFL_BLOCK];
    for (uint32_t i0 = 0; i0 < w; i0 += C10_BFL_BLOCK) {
        uint32_t nblk = w - i0 < C10_BFL_BLOCK ? w - i0 : C10_BFL_BLOCK, t = 0;
        for (uint32_t j = 0; j < nblk; j++) {
            r[j] = (int32_t)cur[i0 + j] - (int32_t)pred_avg(cur, up, i0 + j, y, d, mid);
            t |= (uint32_t)(r[j] ^ (r[j] >> 31));
        }
        uint32_t n = bitlen32(t) + 1u, mask = (1u << n) - 1u;
        pack_put(b, n - 1u, 4u);
        for (uint32_t j = 0; j < nblk; j++) pack_put(b, (uint32_t)r[j] & mask, n);
    }
}

/* Fused offset + clamp + curve + BFL_AVG for Bayer (d = 2), y >= 2, 4-byte aligned rows,
 * coded_bits <= 15. Keeps coded row in cur (needed for y + 2) and bit-writer state in locals,
 * eliminating the separate code_row and res[] passes. */
#define C10_CODE2W(word, dst_w) do {                                              \
        uint32_t v_ = UQSUB16((word), off2);                                      \
        v_ = USUB16(v_, UQSUB16(v_, max2));                                       \
        if (mf) {                                                                 \
            uint32_t lo_ = mf_fwd(v_ & 0xffffu, m), hi_ = mf_fwd(v_ >> 16, m);    \
            v_ = lo_ | (hi_ << 16);                                               \
        }                                                                         \
        (dst_w) = v_;                                                             \
    } while (0)

#ifdef C10_SIMD32
#define BFL_ACC(r, acc) do {                                                      \
        uint32_t s_ = SSUB16(0u, ((r) & sign_mask) >> 15);                        \
        (acc) |= (r) ^ s_;                                                        \
    } while (0)
#else
#define BFL_ACC(r, acc) do {                                                      \
        (acc) |= (r) ^ ((((r) >> 15) & 0x00010001u) * 0xffffu);                   \
    } while (0)
#endif

#define BFL_PUT(val, len) do {                                                    \
        uint32_t v_ = (val), l_ = (len);                                          \
        uint64_t t_ = (uint64_t)b_acc | ((uint64_t)v_ << b_n);                    \
        b_n += l_;                                                                \
        if (b_n >= 32u) { *b_w++ = (uint32_t)t_; t_ >>= 32; b_n -= 32u; }         \
        b_acc = (uint32_t)t_;                                                     \
    } while (0)

static void bfl_row_fused(const c10_params *p, packw *b, const uint16_t *src, uint16_t *cur,
                          const uint16_t *up) {
    uint32_t w = p->width, words = w >> 1;
    uint32_t off = (uint32_t)p->black - p->pedestal, maxv = (1u << p->in_bits) - 1u;
    uint32_t off2 = off | (off << 16), max2 = maxv | (maxv << 16), m = p->curve_m;
    int mf = p->curve == C10_CURVE_MINIFLOAT;
#ifdef C10_SIMD32
    uint32_t sign_mask = 0x80008000u;
#endif
    const c10_u32a *S = (const c10_u32a *)(const void *)src;
    const c10_u32a *U = (const c10_u32a *)(const void *)up;
    c10_u32a *C = (c10_u32a *)(void *)cur;
    uint32_t b_acc = (uint32_t)b->acc, b_n = b->n, *b_w = b->w;
    uint32_t k0 = 0, prev_c = 0;

    for (; k0 + 8u <= words; k0 += 8u) {
        __builtin_prefetch((const void *)(S + k0 + 32));
        uint32_t c0, c1, c2, c3, c4, c5, c6, c7;
        C10_CODE2W(S[k0 + 0u], c0); C10_CODE2W(S[k0 + 1u], c1);
        C10_CODE2W(S[k0 + 2u], c2); C10_CODE2W(S[k0 + 3u], c3);
        C10_CODE2W(S[k0 + 4u], c4); C10_CODE2W(S[k0 + 5u], c5);
        C10_CODE2W(S[k0 + 6u], c6); C10_CODE2W(S[k0 + 7u], c7);
        C[k0 + 0u] = c0; C[k0 + 1u] = c1; C[k0 + 2u] = c2; C[k0 + 3u] = c3;
        C[k0 + 4u] = c4; C[k0 + 5u] = c5; C[k0 + 6u] = c6; C[k0 + 7u] = c7;

        uint32_t r0 = SSUB16(c0, k0 ? UHADD16(prev_c, U[k0]) : U[0]);
        uint32_t r1 = SSUB16(c1, UHADD16(c0, U[k0 + 1u]));
        uint32_t r2 = SSUB16(c2, UHADD16(c1, U[k0 + 2u]));
        uint32_t r3 = SSUB16(c3, UHADD16(c2, U[k0 + 3u]));
        uint32_t r4 = SSUB16(c4, UHADD16(c3, U[k0 + 4u]));
        uint32_t r5 = SSUB16(c5, UHADD16(c4, U[k0 + 5u]));
        uint32_t r6 = SSUB16(c6, UHADD16(c5, U[k0 + 6u]));
        uint32_t r7 = SSUB16(c7, UHADD16(c6, U[k0 + 7u]));
        prev_c = c7;

        uint32_t acc = 0;
        BFL_ACC(r0, acc); BFL_ACC(r1, acc); BFL_ACC(r2, acc); BFL_ACC(r3, acc);
        BFL_ACC(r4, acc); BFL_ACC(r5, acc); BFL_ACC(r6, acc); BFL_ACC(r7, acc);
        uint32_t n = bitlen32((acc | (acc >> 16)) & 0xffffu) + 1u;
        uint32_t mask = (1u << n) - 1u, n2 = 2u * n;
        BFL_PUT(n - 1u, 4u);
        BFL_PUT((r0 & mask) | (((r0 >> 16) & mask) << n), n2);
        BFL_PUT((r1 & mask) | (((r1 >> 16) & mask) << n), n2);
        BFL_PUT((r2 & mask) | (((r2 >> 16) & mask) << n), n2);
        BFL_PUT((r3 & mask) | (((r3 >> 16) & mask) << n), n2);
        BFL_PUT((r4 & mask) | (((r4 >> 16) & mask) << n), n2);
        BFL_PUT((r5 & mask) | (((r5 >> 16) & mask) << n), n2);
        BFL_PUT((r6 & mask) | (((r6 >> 16) & mask) << n), n2);
        BFL_PUT((r7 & mask) | (((r7 >> 16) & mask) << n), n2);
    }
    if (k0 < words) {
        uint32_t r[8], nw = words - k0, acc = 0;
        for (uint32_t j = 0; j < nw; j++) {
            uint32_t ck;
            C10_CODE2W(S[k0 + j], ck);
            C[k0 + j] = ck;
            uint32_t pred = (k0 + j == 0u) ? U[0] : UHADD16(prev_c, U[k0 + j]);
            prev_c = ck;
            r[j] = SSUB16(ck, pred);
            BFL_ACC(r[j], acc);
        }
        uint32_t n = bitlen32((acc | (acc >> 16)) & 0xffffu) + 1u;
        uint32_t mask = (1u << n) - 1u, n2 = 2u * n;
        BFL_PUT(n - 1u, 4u);
        for (uint32_t j = 0; j < nw; j++)
            BFL_PUT((r[j] & mask) | (((r[j] >> 16) & mask) << n), n2);
    }
    b->acc = b_acc; b->n = b_n; b->w = b_w;
}

/* Bayer only (d = 2), 4-byte aligned rows, coded_bits <= 15: two samples per SIMD32 operation. */
static void bfl_row_fast(packw *b, const uint16_t *cur, const uint16_t *up, uint32_t w, uint32_t y,
                         uint32_t cb, uint32_t *res) {
    const c10_u32a *C = (const c10_u32a *)(const void *)cur, *U = (const c10_u32a *)(const void *)up;
    uint32_t words = w >> 1, mid = 1u << (cb - 1u);
    if (y >= 2u) {
        res[0] = SSUB16(C[0], U[0]);
        for (uint32_t k = 1; k < words; k++) res[k] = SSUB16(C[k], UHADD16(C[k - 1], U[k]));
    } else {
        res[0] = SSUB16(C[0], mid | mid << 16);
        for (uint32_t k = 1; k < words; k++) res[k] = SSUB16(C[k], C[k - 1]);
    }
    for (uint32_t k0 = 0; k0 < words; k0 += C10_BFL_BLOCK / 2u) {
        uint32_t nw = words - k0 < C10_BFL_BLOCK / 2u ? words - k0 : C10_BFL_BLOCK / 2u, acc = 0;
        const uint32_t *R = res + k0;
        for (uint32_t j = 0; j < nw; j++) acc |= R[j] ^ (((R[j] >> 15) & 0x00010001u) * 0xffffu);
        uint32_t n = bitlen32((acc | acc >> 16) & 0xffffu) + 1u, mask = (1u << n) - 1u;
        pack_put32(b, n - 1u, 4u);
        for (uint32_t j = 0; j < nw; j++)
            pack_put32(b, (R[j] & mask) | ((R[j] >> 16) & mask) << n, 2u * n);
    }
}

/* ---------- stripe encode / decode ---------- */
static int32_t encode_pack(const c10_params *p, int impl, const uint16_t *src, uint32_t stride,
                           uint32_t rows, uint8_t *out, uint16_t *scratch) {
    packw b = { (uint32_t *)(void *)(out + C10_STRIPE_HDR), 0, 0 };
    uint32_t w = p->width;
    for (uint32_t y = 0; y < rows; y++) {
        if (impl == C10_IMPL_FAST && pack_row_fused(p, src + y * stride, &b)) continue;
        code_row(p, impl, src + y * stride, scratch, w);
        pack_row(&b, scratch, w, p->coded_bits, impl != C10_IMPL_REF);
    }
    pack_flush(&b);
    out[0] = C10_MODE_PACK; out[1] = 0; put_le16(out + 2, rows);
    return (int32_t)((uint8_t *)b.w - out);
}

int32_t c10_encode_stripe(const c10_params *p, int impl, const uint16_t *src, uint32_t stride,
                          uint32_t stripe, uint8_t *out, uint32_t out_cap, uint16_t *scratch) {
    uint32_t rows = c10_stripe_rows_at(p, stripe);
    if (!rows || ((uintptr_t)out & 3u) || stride < p->width) return C10_E_PARAM;
    uint32_t bound = c10_stripe_bound(p, rows);
    src += stripe * p->stripe_rows * stride;
    if (p->mode == C10_MODE_PACK) {
        if (out_cap < bound) return C10_E_SPACE;
        return encode_pack(p, impl, src, stride, rows, out, scratch);
    }
    if (out_cap < bound + c10_stripe_slack(p)) return C10_E_SPACE;
    if (p->mode == C10_MODE_BFL_AVG) {
        uint32_t w = p->width, d = p->format == C10_FMT_BAYER ? 2u : 1u, cb = p->coded_bits;
        uint16_t *ring[3] = { scratch, scratch + w, scratch + 2u * w };
        uint32_t *res = (uint32_t *)(void *)(scratch + 3u * w);
        int fast = impl != C10_IMPL_REF && d == 2u && !((uintptr_t)scratch & 3u);
        packw b = { (uint32_t *)(void *)(out + C10_STRIPE_HDR), 0, 0 };
        uint32_t *limit = (uint32_t *)(void *)(out + bound);
        for (uint32_t y = 0; y < rows; y++) {
            uint16_t *cur = ring[y % 3u], *up = ring[(y + 3u - d) % 3u];
            const uint16_t *srow = src + y * stride;
            if (fast && y >= 2u && !((uintptr_t)srow & 3u)) {
                bfl_row_fused(p, &b, srow, cur, up);
            } else {
                code_row(p, impl, srow, cur, w);
                if (fast) bfl_row_fast(&b, cur, up, w, y, cb, res);
                else bfl_row_ref(&b, cur, up, w, y, d, cb);
            }
            if (b.w >= limit) return encode_pack(p, impl, src, stride, rows, out, scratch);
        }
        pack_flush(&b);
        if (b.w > limit) return encode_pack(p, impl, src, stride, rows, out, scratch);
        out[0] = C10_MODE_BFL_AVG; out[1] = 0; put_le16(out + 2, rows);
        return (int32_t)((uint8_t *)b.w - out);
    }
    uint32_t w = p->width, d = p->format == C10_FMT_BAYER ? 2u : 1u;
    uint32_t med = p->mode == C10_MODE_RICE_MED, cb = p->coded_bits;
    uint16_t *ring[3] = { scratch, scratch + w, scratch + 2u * w };
    bitw b = { (uint32_t *)(void *)(out + C10_STRIPE_HDR), 0, 0 };
    uint32_t *limit = (uint32_t *)(void *)(out + bound);
    for (uint32_t y = 0; y < rows; y++) {
        uint16_t *cur = ring[y % 3u], *up = ring[(y + 3u - d) % 3u];
        code_row(p, impl, src + y * stride, cur, w);
        rice_row(&b, cur, up, w, y, d, med, cb);
        if (b.w >= limit) return encode_pack(p, impl, src, stride, rows, out, scratch);
    }
    bw_flush(&b);
    if (b.w > limit) return encode_pack(p, impl, src, stride, rows, out, scratch);
    out[0] = p->mode; out[1] = 0; put_le16(out + 2, rows);
    return (int32_t)((uint8_t *)b.w - out);
}

typedef struct { const uint8_t *p, *end; uint64_t acc; uint32_t n, pad; } bitr;

static inline void br_fill(bitr *r) {
    while (r->n <= 32u) {
        uint32_t w = 0;
        if (r->p + 4 <= r->end) { w = (uint32_t)r->p[0] << 24 | (uint32_t)r->p[1] << 16 |
                                      (uint32_t)r->p[2] << 8 | r->p[3]; r->p += 4; }
        else r->pad += 32u;   /* zero padding past the stream; checked by br_overrun */
        r->acc |= (uint64_t)w << (32u - r->n);
        r->n += 32u;
    }
}

static inline int br_overrun(const bitr *r) { return r->pad > r->n; }

static inline uint32_t br_get(bitr *r, uint32_t len) {  /* len 1..32 */
    if (r->n < len) br_fill(r);
    uint32_t v = (uint32_t)(r->acc >> (64u - len));
    r->acc <<= len; r->n -= len;
    return v;
}

static inline uint32_t br_unary(bitr *r) {  /* zeros before a 1, capped at the escape */
    if (r->n < C10_RICE_ESCAPE + 1u) br_fill(r);
    uint32_t top = (uint32_t)(r->acc >> 32);
    uint32_t q = top ? (uint32_t)__builtin_clz(top) : 32u;
    if (q >= C10_RICE_ESCAPE) { r->acc <<= C10_RICE_ESCAPE; r->n -= C10_RICE_ESCAPE; return C10_RICE_ESCAPE; }
    r->acc <<= q + 1u; r->n -= q + 1u;
    return q;
}

int c10_decode_stripe(const c10_params *p, const uint8_t *in, uint32_t in_size, uint32_t stripe,
                      uint16_t *dst, uint32_t stride) {
    uint32_t rows = c10_stripe_rows_at(p, stripe);
    if (!rows || in_size < C10_STRIPE_HDR || (in_size & 3u)) return C10_E_FORMAT;
    if (get_le16(in + 2) != rows || in[1] != 0) return C10_E_FORMAT;
    uint32_t mode = in[0], w = p->width, cb = p->coded_bits;
    dst += stripe * p->stripe_rows * stride;
    if (mode == C10_MODE_PACK) {
        if (in_size != c10_stripe_bound(p, rows)) return C10_E_FORMAT;
        unpack_stream(in + C10_STRIPE_HDR, rows * w, cb, dst, w, stride);
        return C10_OK;
    }
    if (mode == C10_MODE_BFL_AVG) {
        if (cb > 15u) return C10_E_FORMAT;
        uint32_t d = p->format == C10_FMT_BAYER ? 2u : 1u, mid = 1u << (cb - 1u), lim = 1u << cb;
        const uint8_t *q = in + C10_STRIPE_HDR, *end = in + in_size;
        uint64_t acc = 0;
        uint32_t n = 0;
#define C10_LSB_GET(dst, len) do {                                                  \
            if (n < (len)) { if (q + 4 > end) return C10_E_TRUNC;                   \
                acc |= (uint64_t)get_le32(q) << n; q += 4; n += 32u; }              \
            dst = (uint32_t)acc & ((1u << (len)) - 1u); acc >>= (len); n -= (len);  \
        } while (0)
        for (uint32_t y = 0; y < rows; y++) {
            uint16_t *cur = dst + y * stride;
            const uint16_t *up = y >= d ? cur - d * stride : cur;
            for (uint32_t i0 = 0; i0 < w; i0 += C10_BFL_BLOCK) {
                uint32_t nblk = w - i0 < C10_BFL_BLOCK ? w - i0 : C10_BFL_BLOCK, nb, v;
                C10_LSB_GET(nb, 4u);
                nb += 1u;
                for (uint32_t j = 0; j < nblk; j++) {
                    C10_LSB_GET(v, nb);
                    int32_t r = (int32_t)(v << (32u - nb)) >> (32u - nb);
                    int32_t x = (int32_t)pred_avg(cur, up, i0 + j, y, d, mid) + r;
                    if (x < 0 || (uint32_t)x >= lim) return C10_E_RANGE;
                    cur[i0 + j] = (uint16_t)x;
                }
            }
        }
#undef C10_LSB_GET
        return C10_OK;
    }
    if (mode != C10_MODE_RICE_LEFT && mode != C10_MODE_RICE_MED) return C10_E_FORMAT;
    uint32_t d = p->format == C10_FMT_BAYER ? 2u : 1u, med = mode == C10_MODE_RICE_MED;
    uint32_t mid = 1u << (cb - 1u), lim = 1u << cb;
    bitr r = { in + C10_STRIPE_HDR, in + in_size, 0, 0, 0 };
    for (uint32_t y = 0; y < rows; y++) {
        uint16_t *cur = dst + y * stride;
        const uint16_t *up = y >= d ? cur - d * stride : cur;
        for (uint32_t i0 = 0; i0 < w; i0 += C10_RICE_BLOCK) {
            uint32_t nblk = w - i0 < C10_RICE_BLOCK ? w - i0 : C10_RICE_BLOCK;
            uint32_t k = br_get(&r, 4u);
            for (uint32_t j = 0; j < nblk; j++) {
                uint32_t q = br_unary(&r), z;
                if (q == C10_RICE_ESCAPE) z = br_get(&r, cb + 1u);
                else z = (q << k) | (k ? br_get(&r, k) : 0u);
                int32_t res = (int32_t)(z >> 1) ^ -(int32_t)(z & 1u);
                uint32_t i = i0 + j;
                int32_t v = (int32_t)predict(cur, up, i, y, d, med, mid) + res;
                if (v < 0 || (uint32_t)v >= lim) return C10_E_RANGE;
                cur[i] = (uint16_t)v;
            }
            if (br_overrun(&r)) return C10_E_TRUNC;
        }
    }
    return C10_OK;
}

void c10_uncode(const c10_params *p, uint16_t *buf, uint32_t count) {
    uint32_t off = (uint32_t)p->black - p->pedestal, maxv = p->in_bits == 16u ? 0xffffu : (1u << p->in_bits) - 1u;
    for (uint32_t i = 0; i < count; i++) {
        uint32_t v = c10_curve_inv(p, buf[i]) + off;
        buf[i] = (uint16_t)(v > maxv ? maxv : v);
    }
}

/* ---------- checksums ---------- */
uint32_t c10_crc32(const uint8_t *p, uint32_t n) {
    uint32_t c = 0xffffffffu;
    while (n--) {
        c ^= *p++;
        for (int k = 0; k < 8; k++) c = (c >> 1) ^ (0xedb88320u & -(c & 1u));
    }
    return ~c;
}

void c10_fletcher(const uint8_t *p, uint32_t n, uint32_t *pa, uint32_t *pb) {
    uint32_t a = *pa, b = *pb;
    if (!((uintptr_t)p & 3u)) {
        const c10_u32a *w = (const c10_u32a *)(const void *)p;
        for (uint32_t i = 0; i < (n >> 2); i++) { a += w[i]; b += a; }
    } else {
        for (uint32_t i = 0; i < n; i += 4) { a += get_le32(p + i); b += a; }
    }
    *pa = a; *pb = b;
}

/* ---------- container ---------- */
void c10_write_file_header(const c10_params *p, uint8_t o[C10_FILE_HDR]) {
    zero_bytes(o, C10_FILE_HDR);
    put_le32(o, C10_FILE_MAGIC);
    put_le16(o + 4, C10_VERSION); put_le16(o + 6, C10_FILE_HDR);
    put_le16(o + 8, p->width); put_le16(o + 10, p->height);
    o[12] = p->format; o[13] = p->cfa; o[14] = p->in_bits; o[15] = p->coded_bits;
    o[16] = p->curve; o[17] = p->curve_m; o[18] = p->mode; o[19] = p->stripe_rows;
    put_le16(o + 20, p->black); put_le16(o + 22, p->white); put_le16(o + 24, p->pedestal);
    put_le32(o + 28, p->fps_num); put_le32(o + 32, p->fps_den);
    put_le32(o + 60, c10_crc32(o, 60));
}

int c10_read_file_header(const uint8_t in[C10_FILE_HDR], c10_params *p) {
    if (get_le32(in) != C10_FILE_MAGIC) return C10_E_FORMAT;
    if (get_le32(in + 60) != c10_crc32(in, 60)) return C10_E_CRC;
    if (get_le16(in + 4) != C10_VERSION || get_le16(in + 6) != C10_FILE_HDR) return C10_E_FORMAT;
    p->width = (uint16_t)get_le16(in + 8); p->height = (uint16_t)get_le16(in + 10);
    p->format = in[12]; p->cfa = in[13]; p->in_bits = in[14];
    p->curve = in[16]; p->curve_m = in[17]; p->mode = in[18]; p->stripe_rows = in[19];
    p->black = (uint16_t)get_le16(in + 20); p->white = (uint16_t)get_le16(in + 22);
    p->pedestal = (uint16_t)get_le16(in + 24);
    p->fps_num = get_le32(in + 28); p->fps_den = get_le32(in + 32);
    uint8_t coded = in[15];
    if (c10_params_init(p) != C10_OK || p->coded_bits != coded) return C10_E_FORMAT;
    return C10_OK;
}

uint32_t c10_write_frame_header(const c10_params *p, uint32_t index, uint64_t pts_us,
                                const uint8_t *const *stripes, const uint32_t *sizes, uint8_t *o) {
    uint32_t n = c10_n_stripes(p), payload = 4u * n, a = 0, b = 0;
    uint8_t *table = o + C10_FRAME_HDR;
    for (uint32_t s = 0; s < n; s++) { put_le32(table + 4u * s, sizes[s]); payload += sizes[s]; }
    c10_fletcher(table, 4u * n, &a, &b);
    for (uint32_t s = 0; s < n; s++) c10_fletcher(stripes[s], sizes[s], &a, &b);
    put_le32(o, C10_FRAME_MAGIC); put_le32(o + 4, index);
    put_le32(o + 8, (uint32_t)pts_us); put_le32(o + 12, (uint32_t)(pts_us >> 32));
    put_le32(o + 16, payload); put_le16(o + 20, n); put_le16(o + 22, 0);
    put_le32(o + 24, a ^ (b << 13 | b >> 19));
    put_le32(o + 28, c10_crc32(o, 28));
    return C10_FRAME_HDR + 4u * n;
}
