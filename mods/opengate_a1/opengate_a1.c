/* OpenGateA1: SMOD v2 Path A1 (3:2 Open Gate) mod for Sony ILCE-6400 (FW 2.00).
 *
 * 1. av-cam.bin RAM (A5 4896x3376): patches fe_rec_0a, fe_rec_19, rcv_desc,
 *    size_rows[16], and rt_t14[16] (337 entries, proven ref=0/rcv=0 on live HW).
 * 2. libmpr.so MP4 container hooks (tkhd +0x8612AC & stsd +0x8615F4, captured camera build bb31351c; E0757): sets
 *    3840x2160 display/sample-entry height to 2560 (3840 / 1.5 = 2560).
 * 3. viewUnified2.so P1 (0x77D110..0x77D5D0): changes Movie 16:9 rear LCD box
 *    (0, 60, 640, 360) to 3:2 (0, 26, 640, 428).
 *
 * Fail-closed: ON writes only if stock bytes match; OFF restores exact stock.
 * Logs to /tmp_bt/mods/opengate_a1.log: one ON/OFF summary line (unchanged format), up to 8
 * DIAG lines for av-cam sites that did not switch, and one HOOK line per tkhd/stsd call.
 */
#include <stdint.h>

static void hook_note(int tag, uint32_t a, uint32_t b, uint32_t c, uint32_t r);

typedef uint32_t (*tkhd_fn_t)(uint32_t model, uint32_t w, uint32_t h, uint32_t kind);
typedef uint32_t (*stsd_fn_t)(uint32_t ctx, const void *desc, uint32_t arg2);

__attribute__((visibility("default"))) uint32_t tkhd_hook_c(
    uint32_t model, uint32_t w, uint32_t h, uint32_t kind) {
    /* r4 holds ml_orig_tkhd | 1 from trampoline */
    register uint32_t orig_addr __asm__("r4");
    tkhd_fn_t orig = (tkhd_fn_t)(uintptr_t)orig_addr;
    if (*(const volatile uint32_t *)(uintptr_t)(model + 0xd8u) != 0u) {
        return 4u;
    }
    uint32_t h_in = h;
    if (kind == 5u && w == (3840u << 16) && h == (2160u << 16)) {
        h = (2560u << 16);
    }
    uint32_t r = orig(model, w, h, kind);
    hook_note('t', kind, w >> 16, ((h_in >> 16) << 16) | (h >> 16), r);
    return r;
}

__attribute__((visibility("default"))) uint32_t stsd_hook_c(
    uint32_t ctx, const void *desc, uint32_t arg2) {
    register uint32_t orig_addr __asm__("r4");
    stsd_fn_t orig = (stsd_fn_t)(uintptr_t)orig_addr;
    if (desc) {
        const uint16_t *d16 = (const uint16_t *)desc;
        if (d16[4] == 3840u && d16[5] == 2160u) {
            uint32_t tmp[8];
            const uint32_t *s32 = (const uint32_t *)desc;
            for (int i = 0; i < 8; i++) tmp[i] = s32[i];
            ((uint16_t *)tmp)[5] = 2560u;
            uint32_t r = orig(ctx, tmp, arg2);
            hook_note('s', 1u, 3840u, (2160u << 16) | 2560u, r);
            return r;
        }
        uint32_t w = d16[4], h = d16[5];
        uint32_t r = orig(ctx, desc, arg2);
        hook_note('s', 0u, w, (h << 16) | h, r);
        return r;
    }
    return orig(ctx, desc, arg2);
}

static long sc3(long n, long a, long b, long c) {
    register long r0 __asm__("r0") = a, r1 __asm__("r1") = b, r2 __asm__("r2") = c;
    register long r7 __asm__("r7") = n;
    __asm__ volatile("svc #0" : "+r"(r0) : "r"(r1), "r"(r2), "r"(r7) : "memory");
    return r0;
}

static long sc6(long n, long a, long b, long c, long d, long e, long f) {
    register long r0 __asm__("r0") = a, r1 __asm__("r1") = b, r2 __asm__("r2") = c,
                  r3 __asm__("r3") = d, r4 __asm__("r4") = e, r5 __asm__("r5") = f;
    register long r7 __asm__("r7") = n;
    __asm__ volatile("svc #0" : "+r"(r0) : "r"(r1), "r"(r2), "r"(r3), "r"(r4), "r"(r5), "r"(r7) : "memory");
    return r0;
}

static uint32_t find_lib_base(const char *suffix, int slen) {
    long fd = sc3(5 /* open */, (long)"/proc/self/maps", 0 /* O_RDONLY */, 0);
    if (fd < 0) return 0u;
    char buf[256], line[192];
    int lpos = 0, got;
    uint32_t base = 0u;
    while (!base && (got = (int)sc3(3 /* read */, fd, (long)buf, sizeof(buf))) > 0) {
        for (int k = 0; k < got; k++) {
            char ch = buf[k];
            if (ch != '\n') {
                if (lpos + 1 < (int)sizeof(line)) line[lpos++] = ch;
                continue;
            }
            line[lpos] = 0;
            uint32_t lo = 0u, off = 0u;
            int i = 0;
            while (line[i] && line[i] != '-') {
                char c = line[i++];
                uint32_t d = (c >= '0' && c <= '9') ? (uint32_t)(c - '0') : (uint32_t)((c | 0x20) - 'a' + 10);
                lo = (lo << 4) | d;
            }
            while (line[i] && line[i] != ' ') i++;
            while (line[i] == ' ') i++;
            while (line[i] && line[i] != ' ') i++;
            while (line[i] == ' ') i++;
            while (line[i] && line[i] != ' ') {
                char c = line[i++];
                uint32_t d = (c >= '0' && c <= '9') ? (uint32_t)(c - '0') : (uint32_t)((c | 0x20) - 'a' + 10);
                off = (off << 4) | d;
            }
            if (off == 0u && lpos >= slen) {
                int match = 1;
                for (int s = 0; s < slen; s++) {
                    if (line[lpos - slen + s] != suffix[s]) { match = 0; break; }
                }
                if (match) base = lo;
            }
            lpos = 0;
        }
    }
    sc3(6 /* close */, fd, 0, 0);
    return base;
}

static char swap_phys_u16(long fd, uint32_t pa, uint16_t exp_v, uint16_t tgt_v, uint16_t *seen) {
    long base = sc6(192 /* mmap2 */, 0, 4096, 3 /* PROT_READ|PROT_WRITE */, 1 /* MAP_SHARED */,
                    fd, (long)(pa >> 12));
    if (base < 0 && base > -4096) return 'm';
    volatile uint16_t *p = (volatile uint16_t *)(base + (pa & 0xfffu));
    uint16_t cur = *p;
    char c = 'x';
    if (cur == tgt_v) c = 'a';
    else if (cur == exp_v) {
        *p = tgt_v;
        __asm__ volatile("dsb" ::: "memory");
        cur = *p;
        c = (cur == tgt_v) ? 'w' : 'v';
    }
    *seen = cur;
    sc3(91 /* munmap */, base, 4096, 0);
    return c;
}

static char swap_proc_u32(uint32_t va, uint32_t exp_v, uint32_t tgt_v) {
    volatile uint32_t *p = (volatile uint32_t *)(uintptr_t)va;
    uint32_t cur = p[0];
    if (cur == tgt_v) return 'a';
    if (cur != exp_v) return 'x';
    uint32_t page = va & ~0xfffu;
    if (sc3(125 /* mprotect */, (long)page, 4096, 7 /* RWX */) < 0) return 'p';
    p[0] = tgt_v;
    __asm__ volatile("dsb" ::: "memory");
    cur = p[0];
    sc3(125 /* mprotect */, (long)page, 4096, 5 /* RX */);
    return (cur == tgt_v) ? 'w' : 'v';
}

typedef struct { uint32_t pa; uint16_t stock; uint16_t patched; } av_patch_t;
#define AV_PATCH_COUNT 337
static const av_patch_t AV_PATCHES[AV_PATCH_COUNT] = {
    {0x03E14E94u,  3232u,  2880u}, /* fe_rec_0a +0x000 */
    {0x03E14E98u,    36u,    44u}, /* fe_rec_0a +0x004 */
    {0x03E14E9Cu,  3196u,  2836u}, /* fe_rec_0a +0x008 */
    {0x03E14EA0u,    48u,    56u}, /* fe_rec_0a +0x00c */
    {0x03E14EA4u,  3184u,  2824u}, /* fe_rec_0a +0x010 */
    {0x03E14EA8u,  3016u,  2464u}, /* fe_rec_0a +0x014 */
    {0x03E14EB0u,  3016u,  2464u}, /* fe_rec_0a +0x01c */
    {0x03E14EB8u,  3004u,  2452u}, /* fe_rec_0a +0x024 */
    {0x03E14EBCu,  3096u,  2744u}, /* fe_rec_0a +0x028 */
    {0x03E14EC4u,  3096u,  2744u}, /* fe_rec_0a +0x030 */
    {0x03E14ECCu,  3084u,  2732u}, /* fe_rec_0a +0x038 */
    {0x03E14ED0u,  3168u,  2816u}, /* fe_rec_0a +0x03c */
    {0x03E14ED8u,  3168u,  2816u}, /* fe_rec_0a +0x044 */
    {0x03E14EE0u,  3156u,  2804u}, /* fe_rec_0a +0x04c */
    {0x03E14EE4u,  2016u,  2224u}, /* fe_rec_0a +0x050 */
    {0x03E14EECu,  2016u,  2224u}, /* fe_rec_0a +0x058 */
    {0x03E14EF0u,     8u,    12u}, /* fe_rec_0a +0x05c */
    {0x03E14EF4u,  1968u,  2180u}, /* fe_rec_0a +0x060 */
    {0x03E14F00u,  2020u,  2224u}, /* fe_rec_0a +0x06c */
    {0x03E14F04u,     4u,     0u}, /* fe_rec_0a +0x070 */
    {0x03E14F08u,  2016u,  2224u}, /* fe_rec_0a +0x074 */
    {0x03E14F10u,  1968u,  2180u}, /* fe_rec_0a +0x07c */
    {0x03E14F14u,  3104u,  2832u}, /* fe_rec_0a +0x080 */
    {0x03E14F18u,     0u,    16u}, /* fe_rec_0a +0x084 */
    {0x03E14F1Cu,  3104u,  2816u}, /* fe_rec_0a +0x088 */
    {0x03E14F20u,    12u,    28u}, /* fe_rec_0a +0x08c */
    {0x03E14F24u,  3092u,  2804u}, /* fe_rec_0a +0x090 */
    {0x03E14F28u,  2020u,  2224u}, /* fe_rec_0a +0x094 */
    {0x03E14F2Cu,     4u,     0u}, /* fe_rec_0a +0x098 */
    {0x03E14F30u,  2016u,  2224u}, /* fe_rec_0a +0x09c */
    {0x03E14F38u,  1968u,  2180u}, /* fe_rec_0a +0x0a4 */
    {0x03E14F3Cu,  3168u,  2816u}, /* fe_rec_0a +0x0a8 */
    {0x03E14F44u,  3168u,  2816u}, /* fe_rec_0a +0x0b0 */
    {0x03E14F4Cu,  3156u,  2804u}, /* fe_rec_0a +0x0b8 */
    {0x03E14F50u,  3168u,  2816u}, /* fe_rec_0a +0x0bc */
    {0x03E14F58u,  3168u,  2816u}, /* fe_rec_0a +0x0c4 */
    {0x03E14F60u,  3156u,  2804u}, /* fe_rec_0a +0x0cc */
    {0x03E14F64u,  1968u,  2180u}, /* fe_rec_0a +0x0d0 */
    {0x03E14F68u,   328u,   336u}, /* fe_rec_0a +0x0d4 */
    {0x03E14F70u,   344u,   376u}, /* fe_rec_0a +0x0dc */
    {0x03E14F78u,     4u,    12u}, /* fe_rec_0a +0x0e4 */
    {0x03E14F90u,    36u,    44u}, /* fe_rec_0a +0x0fc */
    {0x03E14F94u,  3196u,  2836u}, /* fe_rec_0a +0x100 */
    {0x03E14F9Eu,  1527u,  1391u}, /* fe_rec_0a +0x10a */
    {0x03E14FAAu,  1519u,  1383u}, /* fe_rec_0a +0x116 */
    {0x03E14FB2u,  1519u,  1383u}, /* fe_rec_0a +0x11e */
    {0x03E14FB4u,     0u,     1u}, /* fe_rec_0a +0x120 */
    {0x03E14FBAu,     7u,   143u}, /* fe_rec_0a +0x126 */
    {0x03E14FC0u,     8u,   144u}, /* fe_rec_0a +0x12c */
    {0x03E14FCCu,   808u,   856u}, /* fe_rec_0a +0x138 */
    {0x03E14FCEu,  1519u,  1383u}, /* fe_rec_0a +0x13a */
    {0x03E14FDAu,   823u,   735u}, /* fe_rec_0a +0x146 */
    {0x03E14FE2u,   807u,   719u}, /* fe_rec_0a +0x14e */
    {0x03E14FEAu,   807u,   719u}, /* fe_rec_0a +0x156 */
    {0x03E14FF2u,   807u,   719u}, /* fe_rec_0a +0x15e */
    {0x03E14FF6u,   819u,   731u}, /* fe_rec_0a +0x162 */
    {0x03E1500Cu,   807u,   719u}, /* fe_rec_0a +0x178 */
    {0x03E15026u,   823u,   735u}, /* fe_rec_0a +0x192 */
    {0x03E1502Eu,   807u,   719u}, /* fe_rec_0a +0x19a */
    {0x03E15032u,     9u,    11u}, /* fe_rec_0a +0x19e */
    {0x03E15036u,   800u,   714u}, /* fe_rec_0a +0x1a2 */
    {0x03E1503Au,     9u,    11u}, /* fe_rec_0a +0x1a6 */
    {0x03E1503Eu,   800u,   714u}, /* fe_rec_0a +0x1aa */
    {0x03E15042u,     9u,    11u}, /* fe_rec_0a +0x1ae */
    {0x03E15046u,   800u,   714u}, /* fe_rec_0a +0x1b2 */
    {0x03E1504Au,     9u,    11u}, /* fe_rec_0a +0x1b6 */
    {0x03E1504Eu,   762u,   626u}, /* fe_rec_0a +0x1ba */
    {0x03E15052u,   754u,   666u}, /* fe_rec_0a +0x1be */
    {0x03E15056u,   816u,   728u}, /* fe_rec_0a +0x1c2 */
    {0x03E1505Au,   821u,   733u}, /* fe_rec_0a +0x1c6 */
    {0x03E1505Eu,     9u,    11u}, /* fe_rec_0a +0x1ca */
    {0x03E15062u,   807u,   719u}, /* fe_rec_0a +0x1ce */
    {0x03E1507Au,   773u,   685u}, /* fe_rec_0a +0x1e6 */
    {0x03E15082u,   773u,   685u}, /* fe_rec_0a +0x1ee */
    {0x03E1508Cu,  3096u,  2744u}, /* fe_rec_0a +0x1f8 */
    {0x03E15094u,  3096u,  2744u}, /* fe_rec_0a +0x200 */
    {0x03E15098u,  3096u,  2744u}, /* fe_rec_0a +0x204 */
    {0x03E1509Cu,  2016u,  2224u}, /* fe_rec_0a +0x208 */
    {0x03E150A0u,  6281u,  5038u}, /* fe_rec_0a +0x20c */
    {0x03E150AEu,  1583u,  1407u}, /* fe_rec_0a +0x21a */
    {0x03E150B6u,  1583u,  1407u}, /* fe_rec_0a +0x222 */
    {0x03E150BEu,  1551u,  1407u}, /* fe_rec_0a +0x22a */
    {0x03E150C2u,  1567u,  1391u}, /* fe_rec_0a +0x22e */
    {0x03E150C6u,  1549u,  1373u}, /* fe_rec_0a +0x232 */
    {0x03E150E6u,  1506u,  1230u}, /* fe_rec_0a +0x252 */
    {0x03E150EAu,  1506u,  1230u}, /* fe_rec_0a +0x256 */
    {0x03E150ECu,  4752u,  4224u}, /* fe_rec_0a +0x258 */
    {0x03E150EEu,  3168u,  2816u}, /* fe_rec_0a +0x25a */
    {0x03E150F8u,  3012u,  2460u}, /* fe_rec_0a +0x264 */
    {0x03E150FCu,  4094u,  4087u}, /* fe_rec_0a +0x268 */
    {0x03E15102u,  2015u,  2223u}, /* fe_rec_0a +0x26e */
    {0x03E1510Au,  2015u,  2223u}, /* fe_rec_0a +0x276 */
    {0x03E15112u,  2015u,  2223u}, /* fe_rec_0a +0x27e */
    {0x03E1511Au,  2015u,  2223u}, /* fe_rec_0a +0x286 */
    {0x03E15122u,  2006u,  2214u}, /* fe_rec_0a +0x28e */
    {0x03E15126u,  1991u,  2199u}, /* fe_rec_0a +0x292 */
    {0x03E15142u,  2012u,  2220u}, /* fe_rec_0a +0x2ae */
    {0x03E1514Cu,  2016u,  2224u}, /* fe_rec_0a +0x2b8 */
    {0x03E1515Cu,   868u,   851u}, /* fe_rec_0a +0x2c8 */
    {0x03E15162u,  1668u,  1638u}, /* fe_rec_0a +0x2ce */
    {0x03E1518Eu,  2019u,  2223u}, /* fe_rec_0a +0x2fa */
    {0x03E15196u,  2019u,  2223u}, /* fe_rec_0a +0x302 */
    {0x03E1519Eu,  2019u,  2223u}, /* fe_rec_0a +0x30a */
    {0x03E151A6u,  1996u,  2200u}, /* fe_rec_0a +0x312 */
    {0x03E151AAu,  1986u,  2190u}, /* fe_rec_0a +0x316 */
    {0x03E151B0u,     1u,     0u}, /* fe_rec_0a +0x31c */
    {0x03E151B4u,     3u,     0u}, /* fe_rec_0a +0x320 */
    {0x03E151C8u,  2020u,  2224u}, /* fe_rec_0a +0x334 */
    {0x03E151E8u, 16380u,     0u}, /* fe_rec_0a +0x354 */
    {0x03E151EEu,  1007u,  1111u}, /* fe_rec_0a +0x35a */
    {0x03E151F6u,  1007u,  1111u}, /* fe_rec_0a +0x362 */
    {0x03E151FEu,  1007u,  1111u}, /* fe_rec_0a +0x36a */
    {0x03E15202u,  1003u,  1107u}, /* fe_rec_0a +0x36e */
    {0x03E15206u,   971u,  1075u}, /* fe_rec_0a +0x372 */
    {0x03E1520Au,   958u,  1062u}, /* fe_rec_0a +0x376 */
    {0x03E15228u,  2016u,  2224u}, /* fe_rec_0a +0x394 */
    {0x03E15248u, 32766u,     0u}, /* fe_rec_0a +0x3b4 */
    {0x03E1524Au, 32768u,     0u}, /* fe_rec_0a +0x3b6 */
    {0x03E1524Eu,   775u,   707u}, /* fe_rec_0a +0x3ba */
    {0x03E15256u,   775u,   707u}, /* fe_rec_0a +0x3c2 */
    {0x03E1525Au,     0u,     4u}, /* fe_rec_0a +0x3c6 */
    {0x03E1525Eu,   773u,   689u}, /* fe_rec_0a +0x3ca */
    {0x03E15262u,   753u,   685u}, /* fe_rec_0a +0x3ce */
    {0x03E15264u,     0u,     1u}, /* fe_rec_0a +0x3d0 */
    {0x03E15268u,     0u,     3u}, /* fe_rec_0a +0x3d4 */
    {0x03E15274u,  3104u,  2832u}, /* fe_rec_0a +0x3e0 */
    {0x03E1528Au,  1583u,  1407u}, /* fe_rec_0a +0x3f6 */
    {0x03E15292u,  1583u,  1407u}, /* fe_rec_0a +0x3fe */
    {0x03E1529Au,  1583u,  1407u}, /* fe_rec_0a +0x406 */
    {0x03E1529Eu,  1522u,  1346u}, /* fe_rec_0a +0x40a */
    {0x03E152A2u,  1536u,  1360u}, /* fe_rec_0a +0x40e */
    {0x03E152B4u,  3168u,  2816u}, /* fe_rec_0a +0x420 */
    {0x03E152DEu,  2019u,  2223u}, /* fe_rec_0a +0x44a */
    {0x03E152E6u,  2019u,  2223u}, /* fe_rec_0a +0x452 */
    {0x03E152EEu,  1979u,  2191u}, /* fe_rec_0a +0x45a */
    {0x03E152F6u,  2019u,  2223u}, /* fe_rec_0a +0x462 */
    {0x03E152FEu,  1979u,  2027u}, /* fe_rec_0a +0x46a */
    {0x03E15302u,  1936u,  2140u}, /* fe_rec_0a +0x46e */
    {0x03E15306u,  1977u,  2181u}, /* fe_rec_0a +0x472 */
    {0x03E1530Au,  1940u,  2144u}, /* fe_rec_0a +0x476 */
    {0x03E1530Eu,  1896u,  2100u}, /* fe_rec_0a +0x47a */
    {0x03E15312u,  1878u,  2082u}, /* fe_rec_0a +0x47e */
    {0x03E15316u,  1934u,  2138u}, /* fe_rec_0a +0x482 */
    {0x03E15322u,  2018u,  2222u}, /* fe_rec_0a +0x48e */
    {0x03E1532Cu,  2020u,  2224u}, /* fe_rec_0a +0x498 */
    {0x03E15330u, 16380u,     0u}, /* fe_rec_0a +0x49c */
    {0x03E15334u, 16380u,     0u}, /* fe_rec_0a +0x4a0 */
    {0x03E15338u, 16380u,     0u}, /* fe_rec_0a +0x4a4 */
    {0x03E1533Cu, 16380u,     0u}, /* fe_rec_0a +0x4a8 */
    {0x03E15340u, 16380u,     0u}, /* fe_rec_0a +0x4ac */
    {0x03E15368u,     8u,    12u}, /* fe_rec_0a +0x4d4 */
    {0x03E1536Cu,  1975u,  2027u}, /* fe_rec_0a +0x4d8 */
    {0x03E15376u,   351u,   385u}, /* fe_rec_0a +0x4e2 */
    {0x03E1537Eu,   341u,   375u}, /* fe_rec_0a +0x4ea */
    {0x03E15386u,   335u,   368u}, /* fe_rec_0a +0x4f2 */
    {0x03E1538Eu,   342u,   376u}, /* fe_rec_0a +0x4fa */
    {0x03E153A6u,     6u,     7u}, /* fe_rec_0a +0x512 */
    {0x03E153ACu,   336u,   369u}, /* fe_rec_0a +0x518 */
    {0x03E153B0u,  2008u,  2212u}, /* fe_rec_0a +0x51c */
    {0x03E153BCu,   352u,   386u}, /* fe_rec_0a +0x528 */
    {0x03E15E54u,  3232u,  2848u}, /* fe_rec_19 +0x000 */
    {0x03E15E5Cu,  3180u,  2788u}, /* fe_rec_19 +0x008 */
    {0x03E15E64u,  3168u,  2776u}, /* fe_rec_19 +0x010 */
    {0x03E15E68u,  3008u,  2456u}, /* fe_rec_19 +0x014 */
    {0x03E15E70u,  3008u,  2456u}, /* fe_rec_19 +0x01c */
    {0x03E15E78u,  2996u,  2444u}, /* fe_rec_19 +0x024 */
    {0x03E15E7Cu,  3096u,  2736u}, /* fe_rec_19 +0x028 */
    {0x03E15E84u,  3096u,  2736u}, /* fe_rec_19 +0x030 */
    {0x03E15E8Cu,  3084u,  2724u}, /* fe_rec_19 +0x038 */
    {0x03E15E90u,  3168u,  2784u}, /* fe_rec_19 +0x03c */
    {0x03E15E98u,  3168u,  2784u}, /* fe_rec_19 +0x044 */
    {0x03E15EA0u,  3156u,  2772u}, /* fe_rec_19 +0x04c */
    {0x03E15EA4u,  2016u,  2224u}, /* fe_rec_19 +0x050 */
    {0x03E15EACu,  2016u,  2224u}, /* fe_rec_19 +0x058 */
    {0x03E15EB0u,    40u,    32u}, /* fe_rec_19 +0x05c */
    {0x03E15EB4u,  1968u,  2180u}, /* fe_rec_19 +0x060 */
    {0x03E15EC0u,  2020u,  2224u}, /* fe_rec_19 +0x06c */
    {0x03E15EC8u,  2016u,  2224u}, /* fe_rec_19 +0x074 */
    {0x03E15ECCu,    40u,    32u}, /* fe_rec_19 +0x078 */
    {0x03E15ED0u,  1968u,  2180u}, /* fe_rec_19 +0x07c */
    {0x03E15ED4u,  3104u,  2800u}, /* fe_rec_19 +0x080 */
    {0x03E15EDCu,  3104u,  2784u}, /* fe_rec_19 +0x088 */
    {0x03E15EE4u,  3092u,  2772u}, /* fe_rec_19 +0x090 */
    {0x03E15EE8u,  2020u,  2224u}, /* fe_rec_19 +0x094 */
    {0x03E15EF0u,  2016u,  2224u}, /* fe_rec_19 +0x09c */
    {0x03E15EF4u,    40u,    32u}, /* fe_rec_19 +0x0a0 */
    {0x03E15EF8u,  1968u,  2180u}, /* fe_rec_19 +0x0a4 */
    {0x03E15EFCu,  3168u,  2784u}, /* fe_rec_19 +0x0a8 */
    {0x03E15F04u,  3168u,  2784u}, /* fe_rec_19 +0x0b0 */
    {0x03E15F0Cu,  3156u,  2772u}, /* fe_rec_19 +0x0b8 */
    {0x03E15F10u,  3168u,  2784u}, /* fe_rec_19 +0x0bc */
    {0x03E15F18u,  3168u,  2784u}, /* fe_rec_19 +0x0c4 */
    {0x03E15F20u,  3156u,  2772u}, /* fe_rec_19 +0x0cc */
    {0x03E15F24u,  1968u,  2180u}, /* fe_rec_19 +0x0d0 */
    {0x03E15F28u,   324u,   326u}, /* fe_rec_19 +0x0d4 */
    {0x03E15F2Cu,   304u,   280u}, /* fe_rec_19 +0x0d8 */
    {0x03E15F30u,   352u,   384u}, /* fe_rec_19 +0x0dc */
    {0x03E15F3Au,    20u,    28u}, /* fe_rec_19 +0x0e6 */
    {0x03E15F3Eu,  2844u,  2132u}, /* fe_rec_19 +0x0ea */
    {0x03E15F40u,  2856u,  2136u}, /* fe_rec_19 +0x0ec */
    {0x03E15F42u,  1912u,  1776u}, /* fe_rec_19 +0x0ee */
    {0x03E15F44u,  1912u,  1776u}, /* fe_rec_19 +0x0f0 */
    {0x03E15F54u,  3180u,  2788u}, /* fe_rec_19 +0x100 */
    {0x03E15F5Eu,  1527u,  1391u}, /* fe_rec_19 +0x10a */
    {0x03E15F6Au,  1519u,  1383u}, /* fe_rec_19 +0x116 */
    {0x03E15F72u,  1519u,  1383u}, /* fe_rec_19 +0x11e */
    {0x03E15F74u,     0u,     1u}, /* fe_rec_19 +0x120 */
    {0x03E15F7Au,     7u,   143u}, /* fe_rec_19 +0x126 */
    {0x03E15F80u,     8u,   144u}, /* fe_rec_19 +0x12c */
    {0x03E15F8Cu,     8u,   144u}, /* fe_rec_19 +0x138 */
    {0x03E15F8Eu,   719u,   679u}, /* fe_rec_19 +0x13a */
    {0x03E15F9Au,   823u,   727u}, /* fe_rec_19 +0x146 */
    {0x03E15FA2u,   807u,   711u}, /* fe_rec_19 +0x14e */
    {0x03E15FAAu,   807u,   711u}, /* fe_rec_19 +0x156 */
    {0x03E15FB2u,   807u,   711u}, /* fe_rec_19 +0x15e */
    {0x03E15FB6u,   819u,   723u}, /* fe_rec_19 +0x162 */
    {0x03E15FCCu,   807u,   711u}, /* fe_rec_19 +0x178 */
    {0x03E15FE6u,   823u,   727u}, /* fe_rec_19 +0x192 */
    {0x03E15FEEu,   807u,   711u}, /* fe_rec_19 +0x19a */
    {0x03E15FF2u,    11u,     9u}, /* fe_rec_19 +0x19e */
    {0x03E15FF6u,   802u,   704u}, /* fe_rec_19 +0x1a2 */
    {0x03E15FFAu,    11u,     9u}, /* fe_rec_19 +0x1a6 */
    {0x03E15FFEu,   802u,   704u}, /* fe_rec_19 +0x1aa */
    {0x03E16002u,    11u,     9u}, /* fe_rec_19 +0x1ae */
    {0x03E16006u,   802u,   704u}, /* fe_rec_19 +0x1b2 */
    {0x03E1600Au,    51u,    91u}, /* fe_rec_19 +0x1b6 */
    {0x03E1600Eu,   802u,   704u}, /* fe_rec_19 +0x1ba */
    {0x03E16012u,   754u,   658u}, /* fe_rec_19 +0x1be */
    {0x03E16016u,   816u,   720u}, /* fe_rec_19 +0x1c2 */
    {0x03E1601Au,   821u,   725u}, /* fe_rec_19 +0x1c6 */
    {0x03E16022u,   802u,   704u}, /* fe_rec_19 +0x1ce */
    {0x03E1603Au,   773u,   683u}, /* fe_rec_19 +0x1e6 */
    {0x03E16042u,   773u,   683u}, /* fe_rec_19 +0x1ee */
    {0x03E1604Cu,  3096u,  2736u}, /* fe_rec_19 +0x1f8 */
    {0x03E16054u,  3096u,  2736u}, /* fe_rec_19 +0x200 */
    {0x03E16058u,  3096u,  2736u}, /* fe_rec_19 +0x204 */
    {0x03E1605Cu,  2016u,  2224u}, /* fe_rec_19 +0x208 */
    {0x03E16060u,  6281u,  5038u}, /* fe_rec_19 +0x20c */
    {0x03E16064u, 16184u,  1824u}, /* fe_rec_19 +0x210 */
    {0x03E1606Eu,  1583u,  1391u}, /* fe_rec_19 +0x21a */
    {0x03E16076u,  1583u,  1391u}, /* fe_rec_19 +0x222 */
    {0x03E1607Au,    32u,     0u}, /* fe_rec_19 +0x226 */
    {0x03E1607Eu,  1583u,  1391u}, /* fe_rec_19 +0x22a */
    {0x03E16082u,  1567u,  1375u}, /* fe_rec_19 +0x22e */
    {0x03E16086u,  1549u,  1357u}, /* fe_rec_19 +0x232 */
    {0x03E16090u,  1428u,  1068u}, /* fe_rec_19 +0x23c */
    {0x03E160A6u,  1506u,  1230u}, /* fe_rec_19 +0x252 */
    {0x03E160AAu,  1506u,  1230u}, /* fe_rec_19 +0x256 */
    {0x03E160ACu,  4752u,  4176u}, /* fe_rec_19 +0x258 */
    {0x03E160AEu,  3168u,  2784u}, /* fe_rec_19 +0x25a */
    {0x03E160B8u,  3012u,  2460u}, /* fe_rec_19 +0x264 */
    {0x03E160BCu,  4094u,  4087u}, /* fe_rec_19 +0x268 */
    {0x03E160C2u,  2015u,  2223u}, /* fe_rec_19 +0x26e */
    {0x03E160CAu,  2015u,  2223u}, /* fe_rec_19 +0x276 */
    {0x03E160D2u,  2015u,  2223u}, /* fe_rec_19 +0x27e */
    {0x03E160DAu,  2015u,  2223u}, /* fe_rec_19 +0x286 */
    {0x03E160E2u,  2006u,  2214u}, /* fe_rec_19 +0x28e */
    {0x03E160E6u,  1991u,  2199u}, /* fe_rec_19 +0x292 */
    {0x03E16102u,  2012u,  2220u}, /* fe_rec_19 +0x2ae */
    {0x03E1610Cu,  2016u,  2224u}, /* fe_rec_19 +0x2b8 */
    {0x03E16118u,    38u,    24u}, /* fe_rec_19 +0x2c4 */
    {0x03E1611Cu, 15100u,  9029u}, /* fe_rec_19 +0x2c8 */
    {0x03E16122u,  1668u,  1638u}, /* fe_rec_19 +0x2ce */
    {0x03E1614Eu,  2019u,  2223u}, /* fe_rec_19 +0x2fa */
    {0x03E16156u,  2019u,  2223u}, /* fe_rec_19 +0x302 */
    {0x03E1615Eu,  2019u,  2223u}, /* fe_rec_19 +0x30a */
    {0x03E16166u,  1996u,  2200u}, /* fe_rec_19 +0x312 */
    {0x03E1616Au,  1986u,  2190u}, /* fe_rec_19 +0x316 */
    {0x03E16172u,     1u,     0u}, /* fe_rec_19 +0x31e */
    {0x03E16176u,  2015u,     0u}, /* fe_rec_19 +0x322 */
    {0x03E16188u,  2020u,  2224u}, /* fe_rec_19 +0x334 */
    {0x03E161A8u,  1912u,  1776u}, /* fe_rec_19 +0x354 */
    {0x03E161AEu,  1007u,  1111u}, /* fe_rec_19 +0x35a */
    {0x03E161B6u,  1007u,  1111u}, /* fe_rec_19 +0x362 */
    {0x03E161BEu,  1007u,  1111u}, /* fe_rec_19 +0x36a */
    {0x03E161C2u,  1003u,  1107u}, /* fe_rec_19 +0x36e */
    {0x03E161C6u,   971u,  1075u}, /* fe_rec_19 +0x372 */
    {0x03E161CAu,   958u,  1062u}, /* fe_rec_19 +0x376 */
    {0x03E161E8u,  2016u,  2224u}, /* fe_rec_19 +0x394 */
    {0x03E16208u,   956u,   888u}, /* fe_rec_19 +0x3b4 */
    {0x03E1620Eu,   775u,   699u}, /* fe_rec_19 +0x3ba */
    {0x03E16216u,   775u,   699u}, /* fe_rec_19 +0x3c2 */
    {0x03E1621Au,     2u,    12u}, /* fe_rec_19 +0x3c6 */
    {0x03E1621Eu,   775u,   695u}, /* fe_rec_19 +0x3ca */
    {0x03E16222u,   753u,   677u}, /* fe_rec_19 +0x3ce */
    {0x03E16226u,     0u,     1u}, /* fe_rec_19 +0x3d2 */
    {0x03E1622Au,     0u,   695u}, /* fe_rec_19 +0x3d6 */
    {0x03E16234u,  3104u,  2800u}, /* fe_rec_19 +0x3e0 */
    {0x03E1624Au,  1583u,  1391u}, /* fe_rec_19 +0x3f6 */
    {0x03E16252u,  1583u,  1391u}, /* fe_rec_19 +0x3fe */
    {0x03E1625Au,  1583u,  1391u}, /* fe_rec_19 +0x406 */
    {0x03E1625Eu,  1522u,  1330u}, /* fe_rec_19 +0x40a */
    {0x03E16262u,  1536u,  1344u}, /* fe_rec_19 +0x40e */
    {0x03E16274u,  3168u,  2784u}, /* fe_rec_19 +0x420 */
    {0x03E1629Eu,  2019u,  2223u}, /* fe_rec_19 +0x44a */
    {0x03E162A6u,  2019u,  2223u}, /* fe_rec_19 +0x452 */
    {0x03E162AAu,    40u,    32u}, /* fe_rec_19 +0x456 */
    {0x03E162AEu,  2007u,  2211u}, /* fe_rec_19 +0x45a */
    {0x03E162B6u,  2007u,  2211u}, /* fe_rec_19 +0x462 */
    {0x03E162BAu,    64u,   252u}, /* fe_rec_19 +0x466 */
    {0x03E162BEu,  2007u,  2211u}, /* fe_rec_19 +0x46a */
    {0x03E162C2u,  1936u,  2140u}, /* fe_rec_19 +0x46e */
    {0x03E162C6u,  1977u,  2181u}, /* fe_rec_19 +0x472 */
    {0x03E162CAu,  1940u,  2144u}, /* fe_rec_19 +0x476 */
    {0x03E162CEu,  1896u,  2100u}, /* fe_rec_19 +0x47a */
    {0x03E162D2u,  1878u,  2082u}, /* fe_rec_19 +0x47e */
    {0x03E162D6u,  1934u,  2138u}, /* fe_rec_19 +0x482 */
    {0x03E162E2u,  2018u,  2222u}, /* fe_rec_19 +0x48e */
    {0x03E162ECu,  2020u,  2224u}, /* fe_rec_19 +0x498 */
    {0x03E162F0u,  1912u,  1776u}, /* fe_rec_19 +0x49c */
    {0x03E162F4u,  1912u,  1776u}, /* fe_rec_19 +0x4a0 */
    {0x03E162F8u,  1912u,  1776u}, /* fe_rec_19 +0x4a4 */
    {0x03E162FCu,  1912u,  1776u}, /* fe_rec_19 +0x4a8 */
    {0x03E16300u,  1912u,  1776u}, /* fe_rec_19 +0x4ac */
    {0x03E16328u,  1976u,  2028u}, /* fe_rec_19 +0x4d4 */
    {0x03E1632Cu,  3919u,  3987u}, /* fe_rec_19 +0x4d8 */
    {0x03E16336u,   351u,   385u}, /* fe_rec_19 +0x4e2 */
    {0x03E1633Eu,   341u,   375u}, /* fe_rec_19 +0x4ea */
    {0x03E16346u,   341u,   374u}, /* fe_rec_19 +0x4f2 */
    {0x03E1634Eu,   342u,   376u}, /* fe_rec_19 +0x4fa */
    {0x03E16350u,     6u,     7u}, /* fe_rec_19 +0x4fc */
    {0x03E16366u,     6u,     7u}, /* fe_rec_19 +0x512 */
    {0x03E16368u,   310u,   287u}, /* fe_rec_19 +0x514 */
    {0x03E1636Cu,   336u,   369u}, /* fe_rec_19 +0x518 */
    {0x03E16370u,  2008u,  2212u}, /* fe_rec_19 +0x51c */
    {0x03E16378u, 13749u,  8381u}, /* fe_rec_19 +0x524 */
    {0x03E1637Cu,   352u,   386u}, /* fe_rec_19 +0x528 */
    {0x03E1979Au,    16u,   568u}, /* rcv_desc+0x7A */
    {0x03E1979Cu,  6000u,  4896u}, /* rcv_desc+0x7C */
    {0x03E197ACu,     4u,   556u}, /* rcv_desc+0x8C */
    {0x03E197AEu,  6024u,  4920u}, /* rcv_desc+0x8E */
    {0x03E25444u,     4u,   556u}, /* size_rows[16]+0x14 */
    {0x03E2544Cu,  6000u,  4896u}, /* size_rows[16]+0x1C */
    {0x03E25454u,    20u,   572u}, /* size_rows[16]+0x24 */
    {0x04201A34u,     4u,   556u}, /* rt_t14[16]+0x28 */
    {0x04201A3Cu,  6000u,  4896u}, /* rt_t14[16]+0x30 */
    {0x04201A44u,    20u,   572u}, /* rt_t14[16]+0x38 */
};

static const uint32_t VU_CELL_OFFS[8] = {
    0x77D0F0u, 0x77D130u, 0x77D210u, 0x77D250u,
    0x77D450u, 0x77D490u, 0x77D570u, 0x77D5B0u,
};

typedef struct { char b[128]; int n; } lbuf_t;
static void pc(lbuf_t *l, char c) { if (l->n < (int)sizeof(l->b) - 1) l->b[l->n++] = c; }
static void ps(lbuf_t *l, const char *s) { while (*s) pc(l, *s++); }
static void ph(lbuf_t *l, uint32_t v) {
    for (int i = 28; i >= 0; i -= 4) {
        uint32_t d = (v >> i) & 0xfu;
        pc(l, (char)(d < 10 ? '0' + d : 'a' + d - 10));
    }
}

static void ph4(lbuf_t *l, uint32_t v) {
    for (int i = 12; i >= 0; i -= 4) {
        uint32_t d = (v >> i) & 0xfu;
        pc(l, (char)(d < 10 ? '0' + d : 'a' + d - 10));
    }
}

static void log_line(lbuf_t *l) {
    pc(l, '\n');
    long logfd = sc3(5 /* open */, (long)"/tmp_bt/mods/opengate_a1.log",
                     01 | 0100 | 02000 /* O_WRONLY|O_CREAT|O_APPEND */, 0644);
    if (logfd >= 0) {
        sc3(4 /* write */, logfd, (long)l->b, (long)l->n);
        sc3(6 /* close */, logfd, 0, 0);
    }
}

/* "HOOK t kind=00000005 w=00000f00 h=08700a00 r=00000000": one line per tkhd/stsd call.
 * h packs (height in << 16) | (height passed on), so 0870 -> 0a00 means 2160 -> 2560.
 * For stsd, the first field is hit=1 when the 3840x2160 entry was rewritten. */
static void hook_note(int tag, uint32_t a, uint32_t b, uint32_t c, uint32_t r) {
    lbuf_t l;
    l.n = 0;
    ps(&l, tag == 't' ? "HOOK t kind=" : "HOOK s hit=");
    ph(&l, a);
    ps(&l, " w=");
    ph(&l, b);
    ps(&l, " h=");
    ph(&l, c);
    ps(&l, " r=");
    ph(&l, r);
    log_line(&l);
}

#define DIAG_MAX 8

static void run_mod(int enable) {
    lbuf_t l;
    l.n = 0;
    ps(&l, enable ? "ON av=" : "OFF av=");
    int ok = 0;

    long fd = sc3(5 /* open */, (long)"/dev/mem", 2 | 04010000 /* O_RDWR | O_SYNC */, 0);
    if (fd < 0) {
        ps(&l, "err");
    } else {
        uint32_t cw = 0, ca = 0, cx = 0;
        for (int i = 0; i < AV_PATCH_COUNT; i++) {
            uint16_t e = enable ? AV_PATCHES[i].stock : AV_PATCHES[i].patched;
            uint16_t t = enable ? AV_PATCHES[i].patched : AV_PATCHES[i].stock;
            uint16_t seen = 0;
            char c = swap_phys_u16(fd, AV_PATCHES[i].pa, e, t, &seen);
            if (c == 'w') { cw++; ok++; }
            else if (c == 'a') { ca++; ok++; }
            else {
                /* "DIAG <c> i=<site index> v=<value found>" for the first DIAG_MAX failures:
                 * x = neither stock nor patched, m = mmap2 failed, v = write did not read back. */
                if (cx < DIAG_MAX) {
                    lbuf_t d;
                    d.n = 0;
                    ps(&d, "DIAG ");
                    pc(&d, c);
                    ps(&d, " i=");
                    ph4(&d, (uint32_t)i);
                    ps(&d, " v=");
                    ph4(&d, c == 'm' ? 0u : seen);
                    log_line(&d);
                }
                cx++;
            }
        }
        pc(&l, 'w'); ph4(&l, cw);
        ps(&l, ",a"); ph4(&l, ca);
        ps(&l, ",x"); ph4(&l, cx);
        sc3(6 /* close */, fd, 0, 0);
    }

    ps(&l, " vu=");
    uint32_t vu_base = find_lib_base("/viewUnified2.so", 16);
    if (!vu_base) {
        ps(&l, "nomap");
    } else {
        for (int i = 0; i < 8; i++) {
            uint32_t cell = vu_base + VU_CELL_OFFS[i];
            char c1 = swap_proc_u32(cell + 0x0Cu,
                                    enable ? 0x404E0000u : 0x403A0000u,
                                    enable ? 0x403A0000u : 0x404E0000u);
            char c2 = swap_proc_u32(cell + 0x1Cu,
                                    enable ? 0x40768000u : 0x407AC000u,
                                    enable ? 0x407AC000u : 0x40768000u);
            pc(&l, c1);
            pc(&l, c2);
            if (c1 == 'w' || c1 == 'a') ok++;
            if (c2 == 'w' || c2 == 'a') ok++;
        }
    }
    ps(&l, " ok=");
    ph(&l, (uint32_t)ok);
    log_line(&l);
}

__attribute__((visibility("default"))) void oga1_on(void) { run_mod(1); }
__attribute__((visibility("default"))) void oga1_off(void) { run_mod(0); }
