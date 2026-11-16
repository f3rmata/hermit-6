// SPDX-License-Identifier: GPL-2.0
/*
 * swapin_granularity - measure swap-in cost as a function of access extent.
 *
 * One thread maps N GiB of anonymous memory, faults every page in, and then
 * touches segments of S pages at random 2 MiB-aligned region starts. The
 * runner (run_swapin_granularity.sh) forces the mapping out to Hermit swap
 * with a cgroup memory.max limit while this process waits, so every segment
 * touch is a swap-in fault. For each trial the program reports wall time,
 * bytes, GiB/s, getrusage() minor/major fault deltas and a data check, and
 * optionally dumps the kernel's per-order swap-in cost table
 * (/sys/kernel/debug/hermit/swapin_cost_stats) around the trial.
 *
 * Protocol, matching tools/rdma/run_gapbs_page_sweep.sh:
 *   READY   populating is done; the driver lowers memory.max and waits for
 *           the store counters to go quiet;
 *   SIGUSR1 runs every (segment size, trial) pair and prints TRIAL lines;
 *   DONE    the driver snapshots a live cgroup, then sends SIGUSR2 to exit.
 *
 * --madv-pageout replaces the cgroup step for standalone use, and
 * --populate-only performs the mapping checks without touching swap.
 */
#include <errno.h>
#include <inttypes.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <sys/mman.h>
#include <sys/resource.h>

/* MADV_PAGEOUT is Linux-specific and not exposed by every libc header. */
#ifndef MADV_PAGEOUT
#define MADV_PAGEOUT 21
#endif

#define MAX_SEGMENTS 16
#define MAX_SEGMENT_PAGES 512

static const char *cost_path = "/sys/kernel/debug/hermit/swapin_cost_stats";
static volatile sig_atomic_t started;
static volatile sig_atomic_t finished;
static uint8_t *mapping;
static size_t nr_pages;
static size_t page_bytes;

static void die(const char *fmt, ...)
{
	va_list args;

	va_start(args, fmt);
	fprintf(stderr, "swapin_granularity: ");
	vfprintf(stderr, fmt, args);
	fprintf(stderr, "\n");
	va_end(args);
	exit(1);
}

static void on_start(int sig)
{
	(void)sig;
	started = 1;
}

static void on_finish(int sig)
{
	(void)sig;
	finished = 1;
}

static void log_line(const char *fmt, ...)
{
	va_list args;

	va_start(args, fmt);
	vprintf(fmt, args);
	va_end(args);
	putchar('\n');
	fflush(stdout);
}

static double monotonic_seconds(void)
{
	struct timespec ts;

	if (clock_gettime(CLOCK_MONOTONIC, &ts))
		die("clock_gettime: %s", strerror(errno));
	return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

static void wait_for_signal(volatile sig_atomic_t *flag)
{
	while (!*flag)
		pause();
}

/* Fixed-width pattern so a wrong swap-in page is detected, not just counted. */
static uint64_t page_pattern(size_t index)
{
	return 0x5a5a000000000000ULL | (uint64_t)index;
}

/* Emit the raw "name value" cost table between markers the runner can parse.
 * The debugfs file is root-only, so an unprivileged run reports that once.
 */
static void dump_cost_table(const char *phase, unsigned int segment, unsigned int trial)
{
	FILE *file;
	char line[256];
	bool warned = false;

	if (!cost_path)
		return;
	file = fopen(cost_path, "r");
	if (!file) {
		if (!warned) {
			log_line("COST-UNREADABLE path=%s errno=%d", cost_path, errno);
			warned = true;
		}
		return;
	}
	log_line("COST-BEGIN phase=%s seg=%u trial=%u", phase, segment, trial);
	while (fgets(line, sizeof(line), file)) {
		line[strcspn(line, "\n")] = '\0';
		if (*line)
			log_line("COST %s", line);
	}
	fclose(file);
	log_line("COST-END phase=%s seg=%u trial=%u", phase, segment, trial);
}

struct rusage_delta {
	long minflt, majflt;
};

static struct rusage_delta usage_now(struct rusage *stamp)
{
	struct rusage current;
	struct rusage_delta delta;

	if (getrusage(RUSAGE_SELF, &current))
		die("getrusage: %s", strerror(errno));
	delta.minflt = current.ru_minflt - stamp->ru_minflt;
	delta.majflt = current.ru_majflt - stamp->ru_majflt;
	*stamp = current;
	return delta;
}

int main(int argc, char **argv)
{
	static const unsigned int default_segments[] = {1, 4, 16, 32, 64, 128};
	unsigned int segments[MAX_SEGMENTS], nr_segments = 0;
	unsigned int trials = 5;
	size_t region_pages = 2048 * 1024 / 4096, size_gib = 4;
	unsigned long seed = 1;
	bool madv_pageout = false, no_wait = false, populate_only = false;
	bool read_cost = true;
	unsigned long long checksum_errors = 0, trial_pages = 0;
	struct sigaction sa;
	struct rusage stamp;
	double started_at, total_seconds;
	unsigned int i;
	int argi;

	for (i = 0; i < sizeof(default_segments) / sizeof(default_segments[0]); i++)
		segments[nr_segments++] = default_segments[i];

	for (argi = 1; argi < argc; argi++) {
		char *arg = argv[argi];
		char *value = strchr(arg, '=');
		if (!strcmp(arg, "--madv-pageout")) {
			madv_pageout = true;
			continue;
		}
		if (!strcmp(arg, "--no-wait")) {
			no_wait = true;
			continue;
		}
		if (!strcmp(arg, "--populate-only")) {
			populate_only = true;
			continue;
		}
		if (!strcmp(arg, "--no-cost")) {
			read_cost = false;
			continue;
		}
		if (!strcmp(arg, "--help") || !strcmp(arg, "-h")) {
			printf("usage: %s [--size-gib N] [--segments P[,P...]] [--trials N] "
			       "[--seed N] [--region-kb N] [--cost-stats PATH|none] "
			       "[--madv-pageout] [--no-wait] [--populate-only] [--no-cost]\n",
			       argv[0]);
			return 0;
		}
		if (value)
			*value++ = '\0';
		else if (argi + 1 < argc)
			value = argv[++argi];
		else
			die("%s needs a value", arg);
		if (!strcmp(arg, "--size-gib"))
			size_gib = strtoul(value, NULL, 0);
		else if (!strcmp(arg, "--trials"))
			trials = strtoul(value, NULL, 0);
		else if (!strcmp(arg, "--seed"))
			seed = strtoul(value, NULL, 0);
		else if (!strcmp(arg, "--region-kb"))
			region_pages = strtoul(value, NULL, 0) * 1024 / 4096;
		else if (!strcmp(arg, "--cost-stats"))
			cost_path = strcmp(value, "none") ? value : NULL;
		else if (!strcmp(arg, "--segments")) {
			char *token, *save = NULL;

			nr_segments = 0;
			for (token = strtok_r(value, ",", &save); token;
			     token = strtok_r(NULL, ",", &save)) {
				unsigned int pages = strtoul(token, NULL, 0);

				if (!pages || pages > MAX_SEGMENT_PAGES ||
				    nr_segments == MAX_SEGMENTS)
					die("invalid --segments entry %s", token);
				segments[nr_segments++] = pages;
			}
			if (!nr_segments)
				die("--segments needs at least one page count");
		} else {
			die("unknown option %s", arg);
		}
	}

	if (!size_gib || !trials || !region_pages)
		die("size, trials and region size must be positive");
	page_bytes = (size_t)getpagesize();
	nr_pages = size_gib * (1024UL * 1024 * 1024) / page_bytes;
	if (!nr_pages)
		die("mapping size rounds to zero pages");
	for (i = 0; i < nr_segments; i++)
		if (segments[i] > region_pages)
			die("segment %u pages exceeds the %zu-page region; segments must be "
			    "2 MiB-aligned", segments[i], region_pages);

	mapping = mmap(NULL, nr_pages * page_bytes, PROT_READ | PROT_WRITE,
		       MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
	if (mapping == MAP_FAILED)
		die("mmap %zu GiB: %s", size_gib, strerror(errno));

	/* Populate: write the pattern so the pages are dirty and swap-eligible. */
	started_at = monotonic_seconds();
	for (size_t page = 0; page < nr_pages; page++)
		*(uint64_t *)(mapping + page * page_bytes) = page_pattern(page);
	log_line("POPULATE pages=%zu size_bytes=%zu sec=%.6f", nr_pages,
		 nr_pages * page_bytes, monotonic_seconds() - started_at);

	memset(&sa, 0, sizeof(sa));
	sa.sa_handler = on_start;
	sigaction(SIGUSR1, &sa, NULL);
	sa.sa_handler = on_finish;
	sigaction(SIGUSR2, &sa, NULL);

	if (madv_pageout) {
		started_at = monotonic_seconds();
		if (madvise(mapping, nr_pages * page_bytes, MADV_PAGEOUT))
			die("madvise(MADV_PAGEOUT): %s", strerror(errno));
		log_line("PAGEOUT sec=%.6f", monotonic_seconds() - started_at);
	}

	if (populate_only) {
		log_line("DONE sec=0.000000 trials=0 pages=0 checksum_errors=0");
		munmap(mapping, nr_pages * page_bytes);
		return 0;
	}

	if (!no_wait) {
		log_line("READY pid=%ld pages=%zu size_bytes=%zu", (long)getpid(),
			 nr_pages, nr_pages * page_bytes);
		wait_for_signal(&started);
	}
	log_line("RUN");

	/* The driver already quiesced stores; from here the reader only faults. */
	srand48(seed);
	getrusage(RUSAGE_SELF, &stamp);
	total_seconds = monotonic_seconds();
	started_at = total_seconds;
	for (i = 0; i < nr_segments; i++) {
		unsigned int trial;

		for (trial = 1; trial <= trials; trial++) {
			size_t regions = nr_pages / region_pages;
			size_t start_page, page;
			struct rusage_delta faults;
			unsigned long long errors = 0;
			double begin, seconds;

			if (regions)
				start_page = (size_t)(lrand48() % (long)regions) * region_pages;
			else
				start_page = (size_t)(lrand48() %
						(long)(nr_pages ? nr_pages : 1));
			if (start_page + segments[i] > nr_pages)
				start_page = nr_pages - segments[i];

			begin = monotonic_seconds();
			for (page = start_page; page < start_page + segments[i]; page++)
				if (*(volatile uint64_t *)(mapping + page * page_bytes) !=
				    page_pattern(page))
					errors++;
			seconds = monotonic_seconds() - begin;
			faults = usage_now(&stamp);
			trial_pages += segments[i];
			checksum_errors += errors;
			log_line("TRIAL seg=%u trial=%u start_page=%zu bytes=%zu sec=%.6f "
				 "gib_per_sec=%.6f minflt=%ld majflt=%ld checksum_errors=%llu",
				 segments[i], trial, start_page,
				 (size_t)segments[i] * page_bytes, seconds,
				 seconds > 0 ? (double)segments[i] * page_bytes /
					      1073741824.0 / seconds : 0.0,
				 faults.minflt, faults.majflt, errors);
			if (read_cost)
				dump_cost_table("trial", segments[i], trial);
		}
	}
	if (read_cost)
		dump_cost_table("cell", 0, 0);
	total_seconds = monotonic_seconds() - started_at;
	log_line("DONE sec=%.6f trials=%u pages=%llu checksum_errors=%llu",
		 total_seconds, nr_segments * trials, trial_pages, checksum_errors);

	if (!no_wait) {
		log_line("WAITING");
		wait_for_signal(&finished);
	}
	if (munmap(mapping, nr_pages * page_bytes))
		fprintf(stderr, "swapin_granularity: munmap: %s\n", strerror(errno));
	return checksum_errors ? 2 : 0;
}
