#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t reload_requested;

static unsigned long diff_ms(const struct timespec *start,
                             const struct timespec *end)
{
    time_t sec = end->tv_sec - start->tv_sec;
    long nsec = end->tv_nsec - start->tv_nsec;

    if (nsec < 0) {
        sec--;
        nsec += 1000000000L;
    }

    return (unsigned long)sec * 1000UL + (unsigned long)nsec / 1000000UL;
}

static void handle_reload_signal(int signo)
{
    (void)signo;
    reload_requested = 1;
}

static int write_ready_file(void)
{
    const char *ready_file = getenv("MEMHOG_READY_FILE");
    FILE *fp;

    if (!ready_file || !ready_file[0])
        return 0;

    fp = fopen(ready_file, "w");
    if (!fp) {
        perror("fopen MEMHOG_READY_FILE");
        return -1;
    }

    fprintf(fp, "%ld\n", (long)getpid());
    if (fclose(fp) != 0) {
        perror("fclose MEMHOG_READY_FILE");
        return -1;
    }

    return 0;
}

static unsigned long touch_buffer(char *buf, size_t bytes,
                                  unsigned long long *checksum_out)
{
    size_t i;
    unsigned long long checksum = 0;
    struct timespec touch_start;
    struct timespec touch_end;

    clock_gettime(CLOCK_MONOTONIC, &touch_start);
    for (i = 0; i < bytes; i += 4096) {
        buf[i] = (char)(i >> 12);
        checksum += (unsigned char)buf[i];
    }
    clock_gettime(CLOCK_MONOTONIC, &touch_end);

    *checksum_out = checksum;
    return diff_ms(&touch_start, &touch_end);
}

static unsigned long reload_buffer(const char *buf, size_t bytes,
                                   unsigned long long *checksum_out)
{
    size_t i;
    unsigned long long checksum = 0;
    struct timespec reload_start;
    struct timespec reload_end;

    clock_gettime(CLOCK_MONOTONIC, &reload_start);
    for (i = 0; i < bytes; i += 4096)
        checksum += (unsigned char)buf[i];
    clock_gettime(CLOCK_MONOTONIC, &reload_end);

    *checksum_out = checksum;
    return diff_ms(&reload_start, &reload_end);
}

int main(int argc, char **argv)
{
    size_t mib;
    size_t bytes;
    char *buf;
    int reload_on_signal = 0;
    int ret;
    unsigned long delta_ms;
    unsigned long long expected_checksum;

    if (argc != 2 && argc != 3) {
        fprintf(stderr, "usage: %s <MiB> [reload-on-signal]\n", argv[0]);
        return 2;
    }

    if (argc == 3) {
        if (strcmp(argv[2], "reload-on-signal") != 0) {
            fprintf(stderr, "unknown mode: %s\n", argv[2]);
            return 2;
        }
        reload_on_signal = 1;
    }

    errno = 0;
    mib = strtoull(argv[1], NULL, 10);
    if (errno) {
        perror("strtoull");
        return 2;
    }

    bytes = mib * 1024ULL * 1024ULL;
    ret = posix_memalign((void **)&buf, 2ULL * 1024 * 1024, bytes);
    if (ret != 0) {
        fprintf(stderr, "posix_memalign: %s\n", strerror(ret));
        return 1;
    }
    if (madvise(buf, bytes, MADV_HUGEPAGE) != 0)
        perror("madvise MADV_HUGEPAGE");

    delta_ms = touch_buffer(buf, bytes, &expected_checksum);
    fprintf(stdout, "memhog touched %zu MiB\n", mib);
    fprintf(stdout,
            "MEMHOG_TIMING: step=touch_pages_write bytes=%zu delta_ms=%lu checksum=%llu\n",
            bytes, delta_ms, expected_checksum);
    fflush(stdout);

    if (!reload_on_signal) {
        sleep(20);
        free(buf);
        return 0;
    }

    {
        struct sigaction sa;

        memset(&sa, 0, sizeof(sa));
        sa.sa_handler = handle_reload_signal;
        sigemptyset(&sa.sa_mask);
        if (sigaction(SIGUSR1, &sa, NULL) != 0) {
            perror("sigaction");
            free(buf);
            return 1;
        }
    }

    fprintf(stdout, "MEMHOG_STATE: waiting_for_reload_signal pid=%ld\n",
            (long)getpid());
    fflush(stdout);
    if (write_ready_file() != 0) {
        free(buf);
        return 1;
    }

    while (!reload_requested)
        pause();

    {
        unsigned long long checksum;

        delta_ms = reload_buffer(buf, bytes, &checksum);
        fprintf(stdout,
                "MEMHOG_TIMING: step=reload_pages_read bytes=%zu delta_ms=%lu checksum=%llu\n",
                bytes, delta_ms, checksum);
        if (checksum != expected_checksum) {
            fprintf(stdout,
                    "MEMHOG_CHECKSUM: status=fail expected=%llu actual=%llu\n",
                    expected_checksum, checksum);
            fflush(stdout);
            free(buf);
            return 3;
        }
        fprintf(stdout,
                "MEMHOG_CHECKSUM: status=pass expected=%llu actual=%llu\n",
                expected_checksum, checksum);
        fflush(stdout);
    }
    free(buf);
    return 0;
}
