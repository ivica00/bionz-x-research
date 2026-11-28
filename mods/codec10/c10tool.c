/* c10tool: host-side .c10 reader. Validates every frame (header CRC, size, payload checksum),
 * stops at a torn tail (or resyncs on "FRM0" with --resync) and decodes frames to raw
 * little-endian uint16 sensor DN (or coded values with --coded).
 *
 *   c10tool info  FILE [--resync]
 *   c10tool decode FILE OUTDIR [--coded] [--resync] [--first N] [--count N]
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "codec10.h"

static uint32_t le32(const uint8_t *p) { return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24; }
static uint32_t le16(const uint8_t *p) { return p[0] | p[1] << 8; }

typedef struct { size_t off; uint32_t index, payload, n; uint64_t pts; } frame_ref;

/* Returns 1 when a valid frame starts at off. */
static int frame_ok(const c10_params *p, const uint8_t *d, size_t len, size_t off, frame_ref *fr) {
    if (off + C10_FRAME_HDR > len) return 0;
    const uint8_t *h = d + off;
    if (le32(h) != C10_FRAME_MAGIC || le32(h + 28) != c10_crc32(h, 28)) return 0;
    uint32_t n = le16(h + 20), payload = le32(h + 16);
    if (n != c10_n_stripes(p) || payload < 4u * n || (payload & 3u)) return 0;
    if (off + C10_FRAME_HDR + payload > len) return 0;
    uint32_t a = 0, b = 0, sum = 4u * n;
    const uint8_t *t = h + C10_FRAME_HDR;
    for (uint32_t s = 0; s < n; s++) sum += le32(t + 4u * s);
    if (sum != payload) return 0;
    c10_fletcher(t, payload, &a, &b);
    if (le32(h + 24) != (a ^ (b << 13 | b >> 19))) return 0;
    fr->off = off; fr->index = le32(h + 4); fr->payload = payload; fr->n = n;
    fr->pts = (uint64_t)le32(h + 8) | (uint64_t)le32(h + 12) << 32;
    return 1;
}

int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: c10tool info|decode FILE [OUTDIR] [--coded] [--resync]\n"); return 2; }
    const char *cmd = argv[1], *path = argv[2], *outdir = NULL;
    int coded = 0, resync = 0;
    long first = 0, count = -1;
    for (int i = 3; i < argc; i++) {
        if (!strcmp(argv[i], "--coded")) coded = 1;
        else if (!strcmp(argv[i], "--resync")) resync = 1;
        else if (!strcmp(argv[i], "--first") && i + 1 < argc) first = atol(argv[++i]);
        else if (!strcmp(argv[i], "--count") && i + 1 < argc) count = atol(argv[++i]);
        else outdir = argv[i];
    }
    FILE *f = fopen(path, "rb");
    if (!f) { perror(path); return 2; }
    fseek(f, 0, SEEK_END); long len = ftell(f); fseek(f, 0, SEEK_SET);
    uint8_t *d = malloc((size_t)len + 1);
    if (!d || fread(d, 1, (size_t)len, f) != (size_t)len) { fprintf(stderr, "read failed\n"); return 2; }
    fclose(f);
    c10_params p;
    memset(&p, 0, sizeof p);
    if (len < (long)C10_FILE_HDR || c10_read_file_header(d, &p) != C10_OK) { fprintf(stderr, "bad file header\n"); return 3; }
    size_t px = (size_t)p.width * p.height;
    uint16_t *img = malloc(px * 2);
    size_t off = C10_FILE_HDR;
    long valid = 0, skipped_bytes = 0, decoded = 0, errors = 0;
    frame_ref fr;
    while (off < (size_t)len) {
        if (!frame_ok(&p, d, (size_t)len, off, &fr)) {
            if (!resync) break;
            size_t s = off + 1;
            while (s + 4 <= (size_t)len && le32(d + s) != C10_FRAME_MAGIC) s++;
            skipped_bytes += (long)(s - off);
            off = s;
            if (s + 4 > (size_t)len) break;
            continue;
        }
        if (!strcmp(cmd, "decode") && valid >= first && (count < 0 || decoded < count)) {
            const uint8_t *t = d + fr.off + C10_FRAME_HDR, *sp = t + 4u * fr.n;
            int ok = 1;
            for (uint32_t s = 0; s < fr.n && ok; s++) {
                uint32_t sz = le32(t + 4u * s);
                if (c10_decode_stripe(&p, sp, sz, s, img, p.width) != C10_OK) ok = 0;
                sp += sz;
            }
            if (!ok) { errors++; fprintf(stderr, "frame %u: stripe decode error\n", fr.index); }
            else {
                if (!coded) c10_uncode(&p, img, (uint32_t)px);
                if (outdir) {
                    char name[4096];
                    snprintf(name, sizeof name, "%s/frame_%06u.raw16", outdir, fr.index);
                    FILE *o = fopen(name, "wb");
                    if (!o || fwrite(img, 2, px, o) != px) { perror(name); return 4; }
                    fclose(o);
                }
                decoded++;
            }
        }
        valid++;
        off = fr.off + C10_FRAME_HDR + fr.payload;
    }
    printf("{\"width\":%u,\"height\":%u,\"format\":%u,\"cfa\":%u,\"in_bits\":%u,\"coded_bits\":%u,"
           "\"curve\":%u,\"curve_m\":%u,\"mode\":%u,\"stripe_rows\":%u,\"black\":%u,\"white\":%u,"
           "\"pedestal\":%u,\"fps_num\":%u,\"fps_den\":%u,\"valid_frames\":%ld,\"decoded\":%ld,"
           "\"errors\":%ld,\"tail_bytes\":%ld,\"skipped_bytes\":%ld,\"file_bytes\":%ld}\n",
           p.width, p.height, p.format, p.cfa, p.in_bits, p.coded_bits, p.curve, p.curve_m, p.mode,
           p.stripe_rows, p.black, p.white, p.pedestal, p.fps_num, p.fps_den, valid, decoded, errors,
           (long)((size_t)len > off ? (size_t)len - off : 0), skipped_bytes, len);
    return errors ? 1 : 0;
}
