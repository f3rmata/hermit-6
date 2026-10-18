#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

/* One process/mapping across all phases; stdin commands provide measurement gates. */
#define PAGE 4096UL
#define REGION (2UL * 1024 * 1024)
static double now(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) { perror("clock_gettime"); exit(1); }
    return t.tv_sec + t.tv_nsec * 1e-9;
}
static void fail(const char *s) { perror(s); exit(1); }
static uint64_t rng(uint64_t *s) {
    *s ^= *s << 13; *s ^= *s >> 7; *s ^= *s << 17; return *s;
}
int main(int argc, char **argv) {
    if (argc != 4 || sysconf(_SC_PAGESIZE) != PAGE) return 2;
    size_t bytes = strtoull(argv[1], NULL, 10) * 1024 * 1024;
    uint64_t seed = strtoull(argv[2], NULL, 10);
    if (!bytes || bytes % REGION || !seed) return 2;
    setvbuf(stdout, NULL, _IOLBF, 0);
    printf("{\"event\":\"boot\"}\n");
    char line[128];
    if (!fgets(line, sizeof(line), stdin) || strcmp(line, "init\n")) return 2;
    void *raw = mmap(NULL, bytes + REGION, PROT_READ | PROT_WRITE,
                     MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (raw == MAP_FAILED) fail("mmap");
    uintptr_t aligned = ((uintptr_t)raw + REGION - 1) & ~(REGION - 1);
    size_t prefix = aligned - (uintptr_t)raw;
    if (prefix && munmap(raw, prefix)) fail("munmap prefix");
    size_t suffix = REGION - prefix;
    if (suffix && munmap((void *)(aligned + bytes), suffix)) fail("munmap suffix");
    volatile uint64_t *p = (volatile uint64_t *)aligned;
    if (madvise((void *)p, bytes, !strcmp(argv[3], "base") ? MADV_NOHUGEPAGE : MADV_HUGEPAGE))
        fail("madvise allocation");
    size_t regions = bytes / REGION;
    size_t *order = malloc(regions * sizeof(*order));
    unsigned char *versions = calloc(bytes / PAGE, 1);
    if (!order || !versions) fail("malloc");
    for (size_t r = 0; r < regions; r++) order[r] = r;
    for (size_t r = regions - 1; r > 0; r--) {
        size_t j = rng(&seed) % (r + 1), tmp = order[r]; order[r] = order[j]; order[j] = tmp;
    }
    double t = now();
    for (size_t i = 0; i < bytes / sizeof(*p); i += 8) p[i] = i;
    printf("{\"event\":\"ready\",\"address\":%" PRIuPTR ",\"bytes\":%zu,\"seconds\":%.9f}\n",
           aligned, bytes, now() - t);
    while (fgets(line, sizeof(line), stdin)) {
        if (!strcmp(line, "quit\n")) break;
        if (!strcmp(line, "pageout\n")) {
            t = now();
            if (madvise((void *)p, bytes, MADV_PAGEOUT)) fail("MADV_PAGEOUT");
            printf("{\"event\":\"pageout\",\"seconds\":%.9f}\n", now() - t);
            continue;
        }
        unsigned density, passes, mixed;
        if (sscanf(line, "access %u %u %u", &density, &passes, &mixed) != 3 ||
            !density || density > 512 || !passes || mixed > 1) return 2;
        uint64_t errors = 0, reads = 0, selected = 0;
        t = now();
        for (unsigned pass = 0; pass < passes; pass++) {
            for (size_t j = 0; j < regions; j++) {
                size_t region = order[j];
                unsigned n = mixed ? (region % 2 ? 512 : 1) : density;
                if (!pass) selected += n;
                /* Contiguous subpages, stable offset, shuffled region traversal. */
                for (unsigned k = 0; k < n; k++) {
                    size_t page = region * 512 + k, index = page * PAGE / sizeof(*p);
                    for (size_t w = 0; w < PAGE / sizeof(*p); w += 8) {
                        uint64_t expected = index + w + (w ? 0 : versions[page]);
                        if (p[index + w] != expected) errors++;
                        reads++;
                    }
                    versions[page] = (unsigned char)(versions[page] + 1);
                    p[index] = index + versions[page];
                }
            }
        }
        printf("{\"event\":\"access\",\"seconds\":%.9f,\"selected_pages\":%" PRIu64
               ",\"page_coverage_bytes\":%" PRIu64 ",\"read_instructions\":%" PRIu64
               ",\"passes\":%u,\"checksum_errors\":%" PRIu64 "}\n",
               now() - t, selected, selected * PAGE, reads, passes, errors);
    }
    free(order); free(versions);
    if (munmap((void *)p, bytes)) fail("munmap");
    return 0;
}
