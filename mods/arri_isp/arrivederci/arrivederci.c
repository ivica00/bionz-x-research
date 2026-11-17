/* Arrivederci: ARRI Classic look (tone curve + color matrix) as one lifecycle mod for the
 * RAM-only Mod Loader (SMOD flag bit 1; requires the lifecycle loader build).
 *
 * The loader calls arri_life(1) after ON and arri_life(0) before OFF. Two independent,
 * guarded swaps in LIRO RAM through /dev/mem (one page each):
 *   gamma : stock EC record 31 (S-Log3), 96 curve words at PA 0x03e5363c
 *   color : per-Color-Mode matrix entry B8 (S-Gamut3.Cine, index 23), 9 int16 at PA 0x0444b1d4
 * ON writes the ARRI data only where the stock data is present; OFF restores stock only
 * where the ARRI data is present; anything else is left untouched. The matrix applies on
 * the camera's next recompute (for example a Color Mode round trip). RAM-only.
 * Log: /tmp_bt/mods/arrivederci.log, "arrivederci on=N gamma=C color=C"
 * (w written, a already, x foreign content untouched, v verify failed, o/m open/mmap,
 *  l written over the previous Arrivederci matrix COLOR_LEGACY, when the data header has one).
 */
#include <stdint.h>
#include "arrivederci_data.h"   /* GAMMA_*[96], COLOR_STOCK/ARRI[9], optional COLOR_LEGACY[9] */

#define GAMMA_PA 0x03e5363cu
#define COLOR_PA 0x0444b1d4u

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

static uint32_t get(const volatile void *p, int sz, int i) {
    return sz == 4 ? ((const volatile uint32_t *)p)[i] : ((const volatile uint16_t *)p)[i];
}

/* Guarded swap of n elements of size sz (2 or 4 bytes) at physical address pa. The target is
 * written only when it holds `from` (or `alt`, if given); otherwise it is left untouched. */
static char swap(uint32_t pa, int sz, int n, const void *from, const void *alt, const void *to) {
    long fd = sc3(5, (long)"/dev/mem", 02 | 04010000 /* O_RDWR | O_SYNC */, 0);
    if (fd < 0) return 'o';
    long base = sc6(192 /* mmap2 */, 0, 4096, 3, 1 /* MAP_SHARED */, fd, (long)(pa >> 12));
    if (base < 0 && base > -4096) { sc3(6, fd, 0, 0); return 'm'; }
    volatile uint8_t *p = (volatile uint8_t *)(base + (pa & 0xfffu));
    int is_to = 1, is_from = 1, is_alt = alt != 0;
    for (int i = 0; i < n; i++) {
        uint32_t v = get(p, sz, i);
        if (v != get(to, sz, i)) is_to = 0;
        if (v != get(from, sz, i)) is_from = 0;
        if (alt && v != get(alt, sz, i)) is_alt = 0;
    }
    char code = 'x';
    if (is_to) {
        code = 'a';
    } else if (is_from || is_alt) {
        for (int i = 0; i < n; i++) {
            if (sz == 4) ((volatile uint32_t *)p)[i] = ((const uint32_t *)to)[i];
            else ((volatile uint16_t *)p)[i] = ((const uint16_t *)to)[i];
        }
        __asm__ volatile("dsb" ::: "memory");
        code = is_from ? 'w' : 'l';
        for (int i = 0; i < n; i++)
            if (get(p, sz, i) != get(to, sz, i)) code = 'v';
    }
    sc3(91 /* munmap */, base, 4096, 0);
    sc3(6, fd, 0, 0);
    return code;
}

__attribute__((section(".text.arri_life"), used))
void arri_life(int on) {
#ifdef COLOR_HAS_LEGACY
    const int16_t *legacy = COLOR_LEGACY;
#else
    const int16_t *legacy = 0;
#endif
    char g = on ? swap(GAMMA_PA, 4, 96, GAMMA_STOCK, 0, GAMMA_ARRI)
                : swap(GAMMA_PA, 4, 96, GAMMA_ARRI, 0, GAMMA_STOCK);
    char c = on ? swap(COLOR_PA, 2, 9, COLOR_STOCK, legacy, COLOR_ARRI)
                : swap(COLOR_PA, 2, 9, COLOR_ARRI, legacy, COLOR_STOCK);
    char line[] = "arrivederci on=? gamma=? color=?\n";
    line[15] = (char)('0' + on);
    line[23] = g;
    line[31] = c;
    long fd = sc3(5, (long)"/tmp_bt/mods/arrivederci.log", 01 | 0100 | 02000, 0600);
    if (fd >= 0) { sc3(4, fd, (long)line, sizeof(line) - 1); sc3(6, fd, 0, 0); }
}
