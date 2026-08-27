// SPDX-License-Identifier: GPL-2.0
/* Hold and verify an anonymous working set for sparse swap-in tests. */

#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

enum access_order {
	ACCESS_SEQUENTIAL,
	ACCESS_RANDOM,
};

enum access_locality {
	LOCALITY_HIGH,
	LOCALITY_LOW,
};

struct access_config {
	size_t folio_bytes;
	size_t pages_per_folio;
	size_t pages_to_access;
	size_t folio_count;
	size_t accessed_pages;
	size_t random_mask;
	size_t random_shift;
	size_t random_multiplier1;
	size_t random_multiplier2;
	size_t random_xor1;
	size_t random_xor2;
	unsigned int ratio_basis_points;
	int one_page_per_folio;
	enum access_order order;
	enum access_locality locality;
	uint64_t seed;
	const char *ratio_name;
	const char *order_name;
	const char *locality_name;
};

enum worker_action {
	WORKER_POPULATE,
	WORKER_SCAN,
};

struct worker_context {
	volatile unsigned char *buf;
	const struct access_config *config;
	size_t page_size;
	size_t first_folio;
	size_t last_folio;
	uint64_t checksum;
	uint64_t expected_checksum;
	enum worker_action action;
};

static double elapsed_seconds(const struct timespec *start,
			      const struct timespec *end)
{
	return (double)(end->tv_sec - start->tv_sec) +
	       (double)(end->tv_nsec - start->tv_nsec) / 1000000000.0;
}

static uint64_t mix64(uint64_t value)
{
	value += UINT64_C(0x9e3779b97f4a7c15);
	value = (value ^ (value >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
	value = (value ^ (value >> 27)) * UINT64_C(0x94d049bb133111eb);
	return value ^ (value >> 31);
}

static size_t gcd_size(size_t left, size_t right)
{
	while (right) {
		size_t remainder = left % right;

		left = right;
		right = remainder;
	}
	return left;
}

static unsigned char page_value(size_t page_index)
{
	return (unsigned char)(page_index * 131U + 17U);
}

static void usage(const char *name)
{
	fprintf(stderr,
		"usage: %s <MiB> <huge|base> <folio-KiB> "
		"<100|50|25|6.25|1p> <sequential|random> <high|low> "
		"[seed] [threads]\n",
		name);
}

static int parse_u64(const char *text, uint64_t *value)
{
	char *tail;
	unsigned long long parsed;

	errno = 0;
	parsed = strtoull(text, &tail, 0);
	if (errno || *tail)
		return -1;
	*value = (uint64_t)parsed;
	return 0;
}

static int parse_ratio(const char *text, struct access_config *config)
{
	char *tail;
	double percent;

	config->ratio_name = text;
	if (!strcmp(text, "1p") || !strcmp(text, "1page")) {
		config->one_page_per_folio = 1;
		config->ratio_basis_points = 0;
		return 0;
	}
	errno = 0;
	percent = strtod(text, &tail);
	if (errno || *tail || percent <= 0.0 || percent > 100.0)
		return -1;
	config->ratio_basis_points = (unsigned int)(percent * 100.0 + 0.5);
	return config->ratio_basis_points ? 0 : -1;
}

static size_t coprime_stride(size_t desired, size_t modulus)
{
	size_t stride;

	if (modulus <= 1)
		return 0;
	stride = desired % modulus;
	if (!stride)
		stride = 1;
	while (gcd_size(stride, modulus) != 1) {
		stride++;
		if (stride == modulus)
			stride = 1;
	}
	return stride;
}

static size_t folio_at(const struct access_config *config, size_t ordinal)
{
	size_t value;

	if (config->order == ACCESS_SEQUENTIAL || config->folio_count <= 1)
		return ordinal;
	value = ordinal;
	do {
		value ^= config->random_xor1;
		value = (value * config->random_multiplier1) &
			config->random_mask;
		value ^= value >> config->random_shift;
		value = (value * config->random_multiplier2) &
			config->random_mask;
		value ^= config->random_xor2;
		value &= config->random_mask;
	} while (value >= config->folio_count);
	return value;
}

static void *workset_worker(void *opaque)
{
	struct worker_context *worker = opaque;
	const struct access_config *config = worker->config;
	size_t folio_ordinal, page_ordinal;

	worker->checksum = 0;
	worker->expected_checksum = 0;
	for (folio_ordinal = worker->first_folio;
	     folio_ordinal < worker->last_folio;
	     folio_ordinal++) {
		size_t folio = folio_at(config, folio_ordinal);
		size_t first_page = folio * config->pages_per_folio;
		uint64_t hash = mix64(config->seed ^ (uint64_t)folio);
		size_t start, stride = 1;

		if (worker->action == WORKER_POPULATE) {
			for (page_ordinal = 0;
			     page_ordinal < config->pages_per_folio;
			     page_ordinal++) {
				size_t page = first_page + page_ordinal;

				worker->buf[page * worker->page_size] =
					page_value(page);
			}
			continue;
		}

		if (config->locality == LOCALITY_HIGH) {
			size_t starts = config->pages_per_folio -
					config->pages_to_access + 1;

			start = (size_t)(hash % starts);
		} else {
			size_t desired = config->pages_per_folio /
					 config->pages_to_access + 1;

			stride = coprime_stride(desired,
						 config->pages_per_folio);
			start = (size_t)(hash % config->pages_per_folio);
		}

		for (page_ordinal = 0;
		     page_ordinal < config->pages_to_access; page_ordinal++) {
			size_t page_in_folio;
			size_t page;

			if (config->locality == LOCALITY_HIGH)
				page_in_folio = start + page_ordinal;
			else
				page_in_folio =
					(start + page_ordinal * stride) %
					config->pages_per_folio;
			page = first_page + page_in_folio;

			worker->checksum += worker->buf[page * worker->page_size];
			worker->expected_checksum += page_value(page);
		}
	}
	return NULL;
}

static int run_workers(volatile unsigned char *buf, size_t page_size,
		       const struct access_config *config, unsigned int threads,
		       enum worker_action action, double *seconds,
		       uint64_t *checksum, uint64_t *expected_checksum)
{
	struct worker_context *workers;
	pthread_t *thread_ids;
	struct timespec start, end;
	unsigned int created = 0, index;
	int error = 0;

	workers = calloc(threads, sizeof(*workers));
	thread_ids = calloc(threads, sizeof(*thread_ids));
	if (!workers || !thread_ids) {
		free(workers);
		free(thread_ids);
		errno = ENOMEM;
		return -1;
	}
	if (clock_gettime(CLOCK_MONOTONIC, &start)) {
		free(workers);
		free(thread_ids);
		return -1;
	}
	for (index = 0; index < threads; index++) {
		size_t folios_per_thread = config->folio_count / threads;
		size_t remainder = config->folio_count % threads;
		size_t extra_before = index < remainder ? index : remainder;

		workers[index].buf = buf;
		workers[index].config = config;
		workers[index].page_size = page_size;
		workers[index].first_folio =
			folios_per_thread * index + extra_before;
		workers[index].last_folio =
			workers[index].first_folio + folios_per_thread +
			(index < remainder);
		workers[index].action = action;
		error = pthread_create(&thread_ids[index], NULL, workset_worker,
				       &workers[index]);
		if (error)
			break;
		created++;
	}
	for (index = 0; index < created; index++) {
		int join_error = pthread_join(thread_ids[index], NULL);

		if (!error && join_error)
			error = join_error;
	}
	if (!error && clock_gettime(CLOCK_MONOTONIC, &end))
		error = errno;
	if (!error) {
		*checksum = 0;
		*expected_checksum = 0;
		for (index = 0; index < threads; index++) {
			*checksum += workers[index].checksum;
			*expected_checksum += workers[index].expected_checksum;
		}
		*seconds = elapsed_seconds(&start, &end);
	}
	free(workers);
	free(thread_ids);
	if (error) {
		errno = error;
		return -1;
	}
	return 0;
}

int main(int argc, char **argv)
{
	const long system_page_size = sysconf(_SC_PAGESIZE);
	unsigned long long mib, folio_kib;
	uint64_t parsed_threads;
	size_t bytes, offset, page_size;
	volatile unsigned char *buf;
	struct access_config config = { 0 };
	sigset_t signals;
	char *tail;
	int advice, signal_number;
	uint64_t checksum, expected_checksum;
	double populate_seconds, scan_seconds;
	unsigned int threads = 1;

	if ((argc < 7 || argc > 9) || system_page_size <= 0) {
		usage(argv[0]);
		return 2;
	}
	page_size = (size_t)system_page_size;
	errno = 0;
	mib = strtoull(argv[1], &tail, 10);
	if (errno || *tail || !mib || mib > SIZE_MAX / (1024ULL * 1024ULL)) {
		fprintf(stderr, "invalid MiB value: %s\n", argv[1]);
		return 2;
	}
	if (!strcmp(argv[2], "huge"))
		advice = MADV_HUGEPAGE;
	else if (!strcmp(argv[2], "base"))
		advice = MADV_NOHUGEPAGE;
	else {
		usage(argv[0]);
		return 2;
	}
	errno = 0;
	folio_kib = strtoull(argv[3], &tail, 10);
	if (errno || *tail || !folio_kib ||
	    folio_kib > SIZE_MAX / 1024ULL) {
		fprintf(stderr, "invalid folio KiB value: %s\n", argv[3]);
		return 2;
	}
	if (parse_ratio(argv[4], &config)) {
		fprintf(stderr, "invalid access ratio: %s\n", argv[4]);
		return 2;
	}
	if (!strcmp(argv[5], "sequential")) {
		config.order = ACCESS_SEQUENTIAL;
		config.order_name = "sequential";
	} else if (!strcmp(argv[5], "random")) {
		config.order = ACCESS_RANDOM;
		config.order_name = "random";
	} else {
		fprintf(stderr, "invalid access order: %s\n", argv[5]);
		return 2;
	}
	if (!strcmp(argv[6], "high")) {
		config.locality = LOCALITY_HIGH;
		config.locality_name = "high";
	} else if (!strcmp(argv[6], "low")) {
		config.locality = LOCALITY_LOW;
		config.locality_name = "low";
	} else {
		fprintf(stderr, "invalid access locality: %s\n", argv[6]);
		return 2;
	}
	config.seed = UINT64_C(1);
	if (argc >= 8 && parse_u64(argv[7], &config.seed)) {
		fprintf(stderr, "invalid seed: %s\n", argv[7]);
		return 2;
	}
	if (argc == 9) {
		if (parse_u64(argv[8], &parsed_threads) || !parsed_threads ||
		    parsed_threads > UINT32_MAX) {
			fprintf(stderr, "invalid thread count: %s\n", argv[8]);
			return 2;
		}
		threads = (unsigned int)parsed_threads;
	}
	bytes = (size_t)mib * 1024 * 1024;
	config.folio_bytes = (size_t)folio_kib * 1024;
	if (config.folio_bytes < page_size ||
	    config.folio_bytes % page_size || bytes % config.folio_bytes) {
		fprintf(stderr, "folio size must be page-aligned and divide workset\n");
		return 2;
	}
	config.pages_per_folio = config.folio_bytes / page_size;
	config.folio_count = bytes / config.folio_bytes;
	if (threads > config.folio_count) {
		fprintf(stderr,
			"thread count %u exceeds folio count %zu\n",
			threads, config.folio_count);
		return 2;
	}
	if (config.one_page_per_folio) {
		config.pages_to_access = 1;
	} else {
		config.pages_to_access =
			(config.pages_per_folio * config.ratio_basis_points +
			 9999) / 10000;
		if (!config.pages_to_access)
			config.pages_to_access = 1;
		if (config.pages_to_access > config.pages_per_folio)
			config.pages_to_access = config.pages_per_folio;
	}
	config.accessed_pages = config.folio_count * config.pages_to_access;
	config.random_mask = 1;
	while (config.random_mask < config.folio_count - 1)
		config.random_mask = (config.random_mask << 1) | 1;
	for (offset = config.random_mask; offset; offset >>= 1)
		config.random_shift++;
	config.random_shift = (config.random_shift + 1) / 2;
	config.random_multiplier1 = (size_t)mix64(config.seed) | 1;
	config.random_multiplier2 =
		(size_t)mix64(config.seed ^ UINT64_C(0xd1b54a32d192ed03)) | 1;
	config.random_xor1 = (size_t)mix64(config.seed) & config.random_mask;
	config.random_xor2 =
		(size_t)mix64(config.seed ^ UINT64_C(0x94d049bb133111eb)) &
		config.random_mask;

	sigemptyset(&signals);
	sigaddset(&signals, SIGUSR1);
	sigaddset(&signals, SIGUSR2);
	sigaddset(&signals, SIGTERM);
	sigaddset(&signals, SIGINT);
	if (sigprocmask(SIG_BLOCK, &signals, NULL)) {
		perror("sigprocmask");
		return 1;
	}

	setvbuf(stdout, NULL, _IOLBF, 0);
	printf("WAITING pid=%ld bytes=%zu folio_bytes=%zu threads=%u ratio=%s "
	       "order=%s locality=%s seed=%" PRIu64 "\n",
	       (long)getpid(), bytes, config.folio_bytes, threads,
	       config.ratio_name,
	       config.order_name, config.locality_name, config.seed);
	do {
		if (sigwait(&signals, &signal_number)) {
			perror("sigwait");
			return 1;
		}
	} while (signal_number != SIGUSR1 && signal_number != SIGTERM &&
		 signal_number != SIGINT);
	if (signal_number != SIGUSR1)
		return 0;

	buf = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (buf == MAP_FAILED) {
		perror("mmap");
		return 1;
	}
	if (madvise((void *)buf, bytes, advice)) {
		perror("madvise");
		munmap((void *)buf, bytes);
		return 1;
	}

	if (run_workers(buf, page_size, &config, threads, WORKER_POPULATE,
			&populate_seconds, &checksum, &expected_checksum)) {
		perror("populate_workset");
		munmap((void *)buf, bytes);
		return 1;
	}
	printf("READY pid=%ld bytes=%zu threads=%u populate_sec=%.6f "
	       "pages_per_folio=%zu accessed_pages=%zu accessed_bytes=%zu "
	       "actual_access_pct=%.6f\n",
	       (long)getpid(), bytes, threads, populate_seconds,
	       config.pages_per_folio, config.accessed_pages,
	       config.accessed_pages * page_size,
	       100.0 * config.pages_to_access / config.pages_per_folio);

	for (;;) {
		if (sigwait(&signals, &signal_number)) {
			perror("sigwait");
			break;
		}
		if (signal_number == SIGTERM || signal_number == SIGINT)
			break;
		if (signal_number != SIGUSR2)
			continue;
		if (run_workers(buf, page_size, &config, threads, WORKER_SCAN,
				&scan_seconds, &checksum, &expected_checksum)) {
			perror("scan_workset");
			break;
		}
		printf("SCAN bytes=%zu threads=%u accessed_pages=%zu accessed_bytes=%zu "
		       "actual_access_pct=%.6f sec=%.6f checksum=%" PRIu64
		       " expected=%" PRIu64 " checksum_errors=%u\n",
		       bytes, threads, config.accessed_pages,
		       config.accessed_pages * page_size,
		       100.0 * config.pages_to_access / config.pages_per_folio,
		       scan_seconds, checksum, expected_checksum,
		       checksum != expected_checksum);
	}

	munmap((void *)buf, bytes);
	printf("EXIT\n");
	return 0;
}
