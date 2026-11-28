/* bench_codec: Codec10 encode benchmark for host, qemu-arm, the sony-a6400 QEMU guest and the
 * ILCE-6400 shell. Pure CPU work on synthetic (or supplied raw16) frames; no device access.
 * Optional --out writes a .c10 container (host use; on the camera only under /tmp_bt).
 *
 *   bench_codec --w 1920 --h 1080 --bits 14 --curve mf --m 7 --mode med --threads 3 --frames 24
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>

#include "codec10.h"

#define MAX_THREADS 4
#define MAX_STRIPES 4096

/* clock_gettime is in librt on the camera's glibc 2.11: call the kernel directly. */
static double now_ms(int clk) {
    struct timespec ts;
    syscall(SYS_clock_gettime, clk, &ts);
    return (double)ts.tv_sec * 1e3 + (double)ts.tv_nsec * 1e-6;
}
#define CLK_MONO 1
#define CLK_THREAD 3

static uint32_t rng = 0x12345678u;
static inline uint32_t xs32(void) { rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5; return rng; }
static double gauss(void) {  /* CLT approximation, mean 0, sigma 1 */
    double s = 0;
    for (int i = 0; i < 4; i++) s += (double)(xs32() & 0xffffu) / 65535.0;
    return (s - 2.0) * 1.7320508;
}

/* No libm on the camera sysroot: small local approximations (synthetic scenes only). */
static double ex2(double x) {           /* 2^x */
    int n = (int)x; if ((double)n > x) n--;
    double f = x - n;
    double r = 1.0 + f * (0.6931472 + f * (0.2402265 + f * (0.0555041 + f * (0.0096181 + f * 0.0013334))));
    for (; n > 0; n--) r *= 2.0;
    for (; n < 0; n++) r *= 0.5;
    return r;
}
static double cosine(double x) {
    const double tp = 6.283185307179586;
    long k = (long)(x / tp + (x >= 0 ? 0.5 : -0.5));
    x -= (double)k * tp;
    double x2 = x * x, r = 1.0, t = 1.0;
    for (int i = 1; i <= 9; i++) { t *= -x2 / ((2.0 * i - 1.0) * (2.0 * i)); r += t; }
    return r;
}
#define pow(two, x) ex2(x)
#define cos(x) cosine(x)
#define sqrt(x) __builtin_sqrt(x)

typedef struct {
    c10_params p;
    int impl, threads, verify;
    uint32_t n_stripes, slot;
    const uint16_t *frame;
    uint8_t *out;                 /* n_stripes slots of `slot` bytes */
    uint32_t sizes[MAX_STRIPES];
    int32_t status[MAX_STRIPES];
    volatile uint32_t next;
    pthread_barrier_t start, done;
    volatile int quit;
    double cpu_ms[MAX_THREADS];
    uint16_t *scratch[MAX_THREADS];
} bench;

typedef struct { bench *b; int id; int cpu; } worker_arg;

static void *worker(void *v) {
    worker_arg *a = v;
    bench *b = a->b;
    if (a->cpu >= 0) {
        cpu_set_t set; CPU_ZERO(&set); CPU_SET(a->cpu, &set);
        sched_setaffinity(0, sizeof(set), &set);
    }
    for (;;) {
        pthread_barrier_wait(&b->start);
        if (b->quit) break;
        double c0 = now_ms(CLK_THREAD);
        for (;;) {
            uint32_t s = __sync_fetch_and_add(&b->next, 1u);
            if (s >= b->n_stripes) break;
            uint8_t *o = b->out + (size_t)s * b->slot;
            int32_t r = c10_encode_stripe(&b->p, b->impl, b->frame, b->p.width, s, o, b->slot,
                                          b->scratch[a->id]);
            b->status[s] = r;
            b->sizes[s] = r > 0 ? (uint32_t)r : 0u;
        }
        b->cpu_ms[a->id] += now_ms(CLK_THREAD) - c0;
        pthread_barrier_wait(&b->done);
    }
    return NULL;
}

static void synth_frame(const c10_params *p, uint16_t *f, double k_dn_per_e, double read_dn,
                        int scene, uint32_t seed) {
    rng = seed ? seed : 1u;
    uint32_t w = p->width, h = p->height, maxv = (1u << p->in_bits) - 1u;
    double range = (double)p->white - p->black;
    static const double cgain[4] = { 0.55, 1.0, 1.0, 0.75 }; /* R G G B for RGGB */
    for (uint32_t y = 0; y < h; y++) {
        for (uint32_t x = 0; x < w; x++) {
            double fx = (double)x / w, fy = (double)y / h, l;
            if (scene == 0) {               /* horizontal log ramp, 14 stops */
                l = pow(2.0, -14.0 * (1.0 - fx));
            } else if (scene == 1) {        /* chart: patches, ramp, zone plate, edges */
                if (fy < 0.25) l = pow(2.0, -12.0 * (1.0 - fx));
                else if (fy < 0.6) {
                    int px = (int)(fx * 8), py = (int)((fy - 0.25) / 0.35 * 3);
                    l = pow(2.0, -(double)((px * 3 + py * 5) % 13));
                } else if (fx < 0.5) {
                    double cx = fx - 0.25, cy = fy - 0.8;
                    l = 0.3 + 0.25 * cos(900.0 * (cx * cx + cy * cy));
                } else {
                    l = (((int)(fx * 40) + (int)(fy * 30)) & 1) ? 0.6 : 0.02;
                }
            } else {                        /* flat mid-grey: pure noise statistics */
                l = 0.18;
            }
            uint32_t ch = p->format == C10_FMT_BAYER ? ((y & 1u) << 1 | (x & 1u)) : 1u;
            double s = l * cgain[ch] * range;
            double sigma = sqrt(s * k_dn_per_e + read_dn * read_dn);
            double v = p->black + s + sigma * gauss();
            if (v < 0) v = 0;
            if (v > maxv) v = maxv;
            f[(size_t)y * w + x] = (uint16_t)(v + 0.5);
        }
    }
}

static int hwcap_neon(void) {
#if !defined(__arm__)
    return 0;
#endif
    int fd = open("/proc/self/auxv", O_RDONLY), neon = 0;
    if (fd < 0) return 0;
    unsigned long v[2];
    while (read(fd, v, sizeof v) == (ssize_t)sizeof v) {
        if (v[0] == 0) break;
        if (v[0] == 16 /* AT_HWCAP */) neon = (int)((v[1] >> 12) & 1u);
    }
    close(fd);
    return neon;
}

static double cpu_mhz_estimate(void) {
#if !defined(__arm__)
    return 0.0;
#else
    /* 4 independent chains of dependent adds; an in-order core retires about one per cycle
     * (Cortex-A5 issues one ALU op per cycle in this pattern). Rough, stated as an estimate. */
    volatile uint32_t sink;
    uint32_t a = 1, b = 2, c = 3, d = 4, n = 20000000u;
    double t0 = now_ms(CLK_THREAD);
    for (uint32_t i = 0; i < n; i++) {
        __asm__ volatile("add %0, %0, %4\n\tadd %1, %1, %4\n\tadd %2, %2, %4\n\tadd %3, %3, %4\n\t"
                         "add %0, %0, %4\n\tadd %1, %1, %4\n\tadd %2, %2, %4\n\tadd %3, %3, %4"
                         : "+r"(a), "+r"(b), "+r"(c), "+r"(d) : "r"(i));
    }
    double t1 = now_ms(CLK_THREAD);
    sink = a + b + c + d; (void)sink;
    /* 8 adds + loop overhead (about 2: add + cmp/branch) per iteration */
    return (double)n * 10.0 / ((t1 - t0) * 1e3);
#endif
}

/* Local parsers: current headers redirect atoi/atof to C23 symbols the camera glibc lacks. */
static int pint(const char *s) {
    int neg = *s == '-', v = 0;
    if (neg) s++;
    while (*s >= '0' && *s <= '9') v = v * 10 + (*s++ - '0');
    return neg ? -v : v;
}
static double pdbl(const char *s) {
    double v = 0, scale = 1;
    int neg = *s == '-';
    if (neg) s++;
    while (*s >= '0' && *s <= '9') v = v * 10 + (*s++ - '0');
    if (*s == '.') for (s++; *s >= '0' && *s <= '9'; s++) { scale *= 0.1; v += (*s - '0') * scale; }
    return neg ? -v : v;
}
#define atoi pint
#define atof pdbl

static int parse_mode(const char *s) {
    if (!strcmp(s, "pack")) return C10_MODE_PACK;
    if (!strcmp(s, "left")) return C10_MODE_RICE_LEFT;
    if (!strcmp(s, "med")) return C10_MODE_RICE_MED;
    if (!strcmp(s, "bfl")) return C10_MODE_BFL_AVG;
    return -1;
}

static void usage(void) {
    puts("bench_codec [--w W] [--h H] [--bits 10..16] [--curve linear|mf] [--m M]\n"
         "            [--mode pack|left|med|bfl] [--impl ref|fast|neon] [--threads 1..4]\n"
         "            [--cpus first] [--frames N] [--stripe R] [--fps N] [--black B]\n"
         "            [--pedestal P] [--gain DN_PER_E] [--read DN] [--scene ramp|chart|flat]\n"
         "            [--input raw16_file] [--verify] [--out file.c10] [--calibrate]");
}

int main(int argc, char **argv) {
    static bench B;
    bench *b = &B;
    c10_params *p = &b->p;
    memset(p, 0, sizeof *p);
    p->width = 1920; p->height = 1080; p->format = C10_FMT_BAYER; p->cfa = C10_CFA_RGGB;
    p->in_bits = 14; p->curve = C10_CURVE_MINIFLOAT; p->curve_m = 7; p->mode = C10_MODE_RICE_MED;
    p->stripe_rows = 16; p->black = 512; p->pedestal = 64; p->fps_num = 24; p->fps_den = 1;
    b->impl = C10_IMPL_FAST; b->threads = 3;
    int frames = 24, scene = 1, calibrate = 0, first_cpu = -1;
    double gain = 1.0, readn = 2.5;
    const char *input = NULL, *outpath = NULL;
    for (int i = 1; i < argc; i++) {
        const char *k = argv[i], *v = i + 1 < argc ? argv[i + 1] : "";
#define ARG(name) (!strcmp(k, name) && ++i)
        if (ARG("--w")) p->width = (uint16_t)atoi(v);
        else if (ARG("--h")) p->height = (uint16_t)atoi(v);
        else if (ARG("--bits")) p->in_bits = (uint8_t)atoi(v);
        else if (ARG("--curve")) p->curve = !strcmp(v, "mf") ? C10_CURVE_MINIFLOAT : C10_CURVE_LINEAR;
        else if (ARG("--m")) p->curve_m = (uint8_t)atoi(v);
        else if (ARG("--mode")) { int m = parse_mode(v); if (m < 0) { usage(); return 2; } p->mode = (uint8_t)m; }
        else if (ARG("--impl")) b->impl = !strcmp(v, "ref") ? C10_IMPL_REF : !strcmp(v, "neon") ? C10_IMPL_NEON : C10_IMPL_FAST;
        else if (ARG("--threads")) b->threads = atoi(v);
        else if (ARG("--cpus")) first_cpu = atoi(v);
        else if (ARG("--frames")) frames = atoi(v);
        else if (ARG("--stripe")) p->stripe_rows = (uint8_t)atoi(v);
        else if (ARG("--fps")) p->fps_num = (uint32_t)atoi(v);
        else if (ARG("--black")) p->black = (uint16_t)atoi(v);
        else if (ARG("--pedestal")) p->pedestal = (uint16_t)atoi(v);
        else if (ARG("--gain")) gain = atof(v);
        else if (ARG("--read")) readn = atof(v);
        else if (ARG("--scene")) scene = !strcmp(v, "ramp") ? 0 : !strcmp(v, "flat") ? 2 : 1;
        else if (ARG("--input")) input = v;
        else if (ARG("--out")) outpath = v;
        else if (!strcmp(k, "--verify")) b->verify = 1;
        else if (!strcmp(k, "--calibrate")) calibrate = 1;
        else if (!strcmp(k, "--gray")) p->format = C10_FMT_GRAY;
        else { usage(); return 2; }
    }
    if (p->curve == C10_CURVE_LINEAR) p->curve_m = 0;
    if (c10_params_init(p) != C10_OK || b->threads < 1 || b->threads > MAX_THREADS || frames < 1) {
        fprintf(stderr, "bad parameters\n"); return 2;
    }
    c10_neon_available = hwcap_neon();
    int impl_used = b->impl == C10_IMPL_NEON && !c10_neon_available ? C10_IMPL_FAST : b->impl;
#ifndef C10_HAVE_NEON
    if (impl_used == C10_IMPL_NEON) impl_used = C10_IMPL_FAST;
#endif
    b->impl = impl_used;
    b->n_stripes = c10_n_stripes(p);
    if (b->n_stripes > MAX_STRIPES) { fprintf(stderr, "too many stripes\n"); return 2; }
    b->slot = (c10_stripe_bound(p, p->stripe_rows) + c10_stripe_slack(p) + 63u) & ~63u;
    size_t px = (size_t)p->width * p->height;
    uint16_t *frame = NULL;
    uint8_t *out = NULL, *ref_out = NULL;
    uint16_t *dec = NULL;
    if (posix_memalign((void **)&frame, 64, px * 2) || posix_memalign((void **)&out, 64, (size_t)b->slot * b->n_stripes)) {
        fprintf(stderr, "out of memory\n"); return 3;
    }
    if (b->verify && (posix_memalign((void **)&ref_out, 64, (size_t)b->slot) ||
                      posix_memalign((void **)&dec, 64, px * 2))) {
        fprintf(stderr, "out of memory (verify)\n"); return 3;
    }
    for (int t = 0; t < b->threads; t++)
        if (posix_memalign((void **)&b->scratch[t], 64, c10_scratch_bytes(p))) return 3;
    uint16_t *ref_scratch = NULL;
    if (posix_memalign((void **)&ref_scratch, 64, c10_scratch_bytes(p) + 2u * p->width)) return 3;

    if (input) {
        int fd = open(input, O_RDONLY);
        if (fd < 0 || read(fd, frame, px * 2) != (ssize_t)(px * 2)) { fprintf(stderr, "cannot read %s\n", input); return 3; }
        close(fd);
    } else {
        synth_frame(p, frame, gain, readn, scene, 0x9e3779b9u);
    }
    b->frame = frame; b->out = out;
    pthread_barrier_init(&b->start, NULL, (unsigned)b->threads + 1u);
    pthread_barrier_init(&b->done, NULL, (unsigned)b->threads + 1u);
    pthread_t th[MAX_THREADS];
    worker_arg wa[MAX_THREADS];
    for (int t = 0; t < b->threads; t++) {
        wa[t].b = b; wa[t].id = t; wa[t].cpu = first_cpu >= 0 ? (first_cpu + t) % 4 : -1;
        pthread_create(&th[t], NULL, worker, &wa[t]);
    }
    int fd_out = -1;
    if (outpath) {
        fd_out = open(outpath, O_WRONLY | O_CREAT | O_TRUNC, 0644);
        if (fd_out < 0) { fprintf(stderr, "cannot create %s\n", outpath); return 3; }
        uint8_t fh[C10_FILE_HDR];
        c10_write_file_header(p, fh);
        if (write(fd_out, fh, sizeof fh) != (ssize_t)sizeof fh) return 3;
    }
    uint8_t *hdr = malloc(C10_FRAME_HDR + 4u * b->n_stripes);
    const uint8_t **sp = malloc(sizeof(*sp) * b->n_stripes);
    struct iovec *iov = malloc(sizeof(*iov) * (b->n_stripes + 1u));
    double wall_sum = 0, wall_min = 1e30, wall_max = 0, bytes_sum = 0;
    uint32_t fallback = 0, bad = 0, verify_fail = 0, ref_mismatch = 0;
    for (int f = 0; f < frames; f++) {
        b->next = 0;
        double t0 = now_ms(CLK_MONO);
        pthread_barrier_wait(&b->start);
        pthread_barrier_wait(&b->done);
        double dt = now_ms(CLK_MONO) - t0;
        wall_sum += dt; if (dt < wall_min) wall_min = dt; if (dt > wall_max) wall_max = dt;
        uint32_t fbytes = C10_FRAME_HDR + 4u * b->n_stripes;
        for (uint32_t s = 0; s < b->n_stripes; s++) {
            if (b->status[s] <= 0) bad++;
            else if (p->mode != C10_MODE_PACK && out[(size_t)s * b->slot] == C10_MODE_PACK) fallback++;
            fbytes += b->sizes[s];
            sp[s] = out + (size_t)s * b->slot;
        }
        bytes_sum += fbytes;
        if (fd_out >= 0) {
            uint32_t hl = c10_write_frame_header(p, (uint32_t)f, (uint64_t)f * 1000000u * p->fps_den / p->fps_num, sp, b->sizes, hdr);
            iov[0].iov_base = hdr; iov[0].iov_len = hl;
            for (uint32_t s = 0; s < b->n_stripes; s++) { iov[s + 1].iov_base = (void *)sp[s]; iov[s + 1].iov_len = b->sizes[s]; }
            if (writev(fd_out, iov, (int)b->n_stripes + 1) != (ssize_t)(hl + fbytes - C10_FRAME_HDR - 4u * b->n_stripes)) { fprintf(stderr, "short write\n"); return 3; }
        }
        if (b->verify && f == 0) {
            /* 1) decoder reproduces the reference coded values exactly
             * 2) the selected implementation's bitstream equals the reference implementation's */
            for (uint32_t s = 0; s < b->n_stripes && !verify_fail; s++) {
                if (c10_decode_stripe(p, sp[s], b->sizes[s], s, dec, p->width) != C10_OK) verify_fail++;
                int32_t r = c10_encode_stripe(p, C10_IMPL_REF, frame, p->width, s, ref_out, b->slot, ref_scratch);
                if (r != (int32_t)b->sizes[s] || memcmp(ref_out, sp[s], (size_t)r)) ref_mismatch++;
            }
            uint16_t *row = ref_scratch + 3u * p->width;
            for (uint32_t y = 0; y < p->height && !verify_fail; y++) {
                for (uint32_t x = 0; x < p->width; x++) {
                    uint32_t off = (uint32_t)p->black - p->pedestal, v = frame[(size_t)y * p->width + x];
                    v = v > off ? v - off : 0u;
                    if (v > (1u << p->in_bits) - 1u) v = (1u << p->in_bits) - 1u;
                    row[x] = (uint16_t)c10_curve_fwd(p, v);
                }
                if (memcmp(row, dec + (size_t)y * p->width, 2u * p->width)) verify_fail++;
            }
        }
    }
    b->quit = 1;
    pthread_barrier_wait(&b->start);
    for (int t = 0; t < b->threads; t++) pthread_join(th[t], NULL);
    if (fd_out >= 0) { fsync(fd_out); close(fd_out); }

    double cpu = 0;
    for (int t = 0; t < b->threads; t++) cpu += b->cpu_ms[t];
    double avg = wall_sum / frames, avg_bytes = bytes_sum / frames;
    double fps = (double)p->fps_num / p->fps_den;
    double pack_bytes = (double)px * p->coded_bits / 8.0;
    static const char *modes[] = { "pack", "left", "med", "bfl" }, *impls[] = { "ref", "fast", "neon" };
    printf("RESULT {\"w\":%u,\"h\":%u,\"in_bits\":%u,\"coded_bits\":%u,\"curve\":\"%s\",\"m\":%u,"
           "\"mode\":\"%s\",\"impl\":\"%s\",\"neon_hwcap\":%d,\"threads\":%d,\"frames\":%d,\"stripe_rows\":%u,"
           "\"scene\":%d,\"gain\":%.3g,\"read\":%.3g,"
           "\"wall_ms_avg\":%.3f,\"wall_ms_min\":%.3f,\"wall_ms_max\":%.3f,\"cpu_ms_per_frame\":%.3f,"
           "\"mpix_s\":%.2f,\"bytes_per_frame\":%.0f,\"ratio_vs_16bit\":%.3f,\"ratio_vs_pack\":%.3f,"
           "\"mb_s_at_fps\":%.2f,\"fps\":%.3f,\"fallback_stripes\":%u,\"bad_stripes\":%u,"
           "\"verify\":\"%s\"}\n",
           p->width, p->height, p->in_bits, p->coded_bits, p->curve ? "mf" : "linear", p->curve_m,
           modes[p->mode], impls[b->impl], c10_neon_available, b->threads, frames, p->stripe_rows,
           scene, gain, readn, avg, wall_min, wall_max, cpu / frames,
           (double)px / (avg * 1e3), avg_bytes, (double)px * 2.0 / avg_bytes, pack_bytes / avg_bytes,
           avg_bytes * fps / 1e6, fps, fallback, bad,
           !b->verify ? "skipped" : (verify_fail || ref_mismatch) ? "FAIL" : "PASS");
    if (calibrate) printf("CPU_MHZ_ESTIMATE %.0f\n", cpu_mhz_estimate());
    return (bad || verify_fail || ref_mismatch) ? 1 : 0;
}
