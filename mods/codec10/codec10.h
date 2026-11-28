/* Codec10: a >8-bit Bayer/grayscale video codec for the ILCE-6400 Cortex-A5 (no NEON).
 * See docs/CODEC_DESIGN.md. Freestanding: only <stdint.h>/<stddef.h>, no libc, no division,
 * no allocation. All buffers belong to the caller.
 */
#ifndef CODEC10_H
#define CODEC10_H
#include <stddef.h>
#include <stdint.h>

#define C10_FILE_MAGIC   0x56303143u /* "C10V" */
#define C10_FRAME_MAGIC  0x304d5246u /* "FRM0" */
#define C10_VERSION      1u
#define C10_FILE_HDR     64u
#define C10_FRAME_HDR    32u
#define C10_STRIPE_HDR   4u
#define C10_RICE_BLOCK   16u
#define C10_RICE_ESCAPE  24u
#define C10_MAX_WIDTH    8192u
#define C10_MAX_HEIGHT   8192u

enum { C10_FMT_BAYER = 1, C10_FMT_GRAY = 2 };
enum { C10_CFA_RGGB = 0, C10_CFA_GRBG = 1, C10_CFA_GBRG = 2, C10_CFA_BGGR = 3 };
enum { C10_CURVE_LINEAR = 0, C10_CURVE_MINIFLOAT = 1 };
enum { C10_MODE_PACK = 0, C10_MODE_RICE_LEFT = 1, C10_MODE_RICE_MED = 2, C10_MODE_BFL_AVG = 3 };
/* BFL_AVG: average predictor (a + b) >> 1, blocks of 16 residuals stored as n-bit two's complement
 * with n = 1 + bitlen(OR of r ^ (r >> 31)) in a 4-bit header (n - 1). LSB-first like PACK.
 * Requires coded_bits <= 15 (residuals fit int16 for the SIMD32 path). */
#define C10_BFL_BLOCK    16u
/* Implementation selector for the encoder kernels (the bitstream is identical). */
enum { C10_IMPL_REF = 0, C10_IMPL_FAST = 1, C10_IMPL_NEON = 2 };

enum {
    C10_OK = 0,
    C10_E_PARAM = -1,
    C10_E_SPACE = -2,
    C10_E_FORMAT = -3,
    C10_E_CRC = -4,
    C10_E_TRUNC = -5,
    C10_E_RANGE = -6,
};

typedef struct {
    uint16_t width, height;
    uint8_t  format;      /* C10_FMT_* */
    uint8_t  cfa;         /* C10_CFA_* (Bayer only) */
    uint8_t  in_bits;     /* sensor sample depth, 8..16 */
    uint8_t  coded_bits;  /* derived by c10_params_init: depth of coded values */
    uint8_t  curve;       /* C10_CURVE_* */
    uint8_t  curve_m;     /* minifloat mantissa bits */
    uint8_t  mode;        /* C10_MODE_* (preferred; a stripe may fall back to PACK) */
    uint8_t  stripe_rows; /* even, 2..254 */
    uint16_t black;       /* sensor black level (DN at in_bits) */
    uint16_t white;       /* sensor white level (DN at in_bits) */
    uint16_t pedestal;    /* coded black: offset = black - pedestal, pedestal <= black */
    uint32_t fps_num, fps_den;
} c10_params;

/* Validate p and derive coded_bits. Returns C10_OK or C10_E_PARAM. */
int c10_params_init(c10_params *p);
uint32_t c10_n_stripes(const c10_params *p);
uint32_t c10_stripe_rows_at(const c10_params *p, uint32_t stripe);
/* Worst-case encoded bytes of one stripe (PACK size + stripe header, 4-byte aligned). */
uint32_t c10_stripe_bound(const c10_params *p, uint32_t rows);
/* Rice attempts need this much scratch beyond the bound (one worst-case row). */
uint32_t c10_stripe_slack(const c10_params *p);
/* Worst-case bytes of one frame record (header + stripe table + all stripes). */
uint32_t c10_frame_bound(const c10_params *p);
/* Per-encoder scratch (three coded rows + one residual row), in bytes. */
uint32_t c10_scratch_bytes(const c10_params *p);

/* Curve helpers (exported for the decoder and tests). */
uint32_t c10_curve_fwd(const c10_params *p, uint32_t x);
uint32_t c10_curve_inv(const c10_params *p, uint32_t c);

/* Encode one stripe of a frame. src points at the frame's first sample, stride in samples.
 * out must hold c10_stripe_bound() + c10_stripe_slack() bytes. scratch: c10_scratch_bytes().
 * Returns bytes written (multiple of 4) or a negative error. */
int32_t c10_encode_stripe(const c10_params *p, int impl, const uint16_t *src, uint32_t stride,
                          uint32_t stripe, uint8_t *out, uint32_t out_cap, uint16_t *scratch);

/* Decode one stripe into coded values (dst = frame base, stride in samples).
 * Returns C10_OK or a negative error. */
int c10_decode_stripe(const c10_params *p, const uint8_t *in, uint32_t in_size, uint32_t stripe,
                      uint16_t *dst, uint32_t stride);

/* Convert coded values back to sensor DN in place (inverse curve + offset). */
void c10_uncode(const c10_params *p, uint16_t *buf, uint32_t count);

/* Container. */
void c10_write_file_header(const c10_params *p, uint8_t out[C10_FILE_HDR]);
int c10_read_file_header(const uint8_t in[C10_FILE_HDR], c10_params *p);
/* Builds the frame header and stripe table (C10_FRAME_HDR + 4*n bytes) for the given stripe
 * buffers. payload_size = sum of sizes. The checksum covers the stripe table and stripes. */
uint32_t c10_write_frame_header(const c10_params *p, uint32_t index, uint64_t pts_us,
                                const uint8_t *const *stripes, const uint32_t *sizes,
                                uint8_t *out);
uint32_t c10_crc32(const uint8_t *p, uint32_t n);
/* Fletcher over little-endian 32-bit words; n must be a multiple of 4. Chainable. */
void c10_fletcher(const uint8_t *p, uint32_t n, uint32_t *a, uint32_t *b);

/* Runtime NEON selection (set by the host program from AT_HWCAP; default: off). */
extern int c10_neon_available;
#endif
