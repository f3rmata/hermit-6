/* SPDX-License-Identifier: GPL-2.0 */
#include <assert.h>
#include <limits.h>
#include <stdio.h>
#include "../../../linux-stable/include/linux/hermit_reclaim.h"

int main(void)
{
	struct hmt_reclaim_watermarks m;

	/* New controls disabled: preserve existing runs, including small limits. */
	m = hmt_reclaim_watermarks(2048, 0, 128);
	assert(m.low == 2048 && m.high == 2048);
	m = hmt_reclaim_watermarks(0, 64, 512);
	assert(!m.low && !m.high);

	/* The same margin can finish an old round without starting a new one. */
	m = hmt_reclaim_watermarks(32, 64, 512);
	assert(m.low == 32 && m.high == 64);
	assert(!(48 < m.low) && 48 < m.high);
	assert(31 < m.low && !(64 < m.high));

	/* Runtime writes in either order and a shrinking cgroup remain bounded. */
	m = hmt_reclaim_watermarks(64, 32, 512);
	assert(m.low == 64 && m.high == 64);
	m = hmt_reclaim_watermarks(2048, 4096, 128);
	assert(m.low == 127 && m.high == 127);
	m = hmt_reclaim_watermarks(1, 2, 1);
	assert(!m.low && !m.high);
	m = hmt_reclaim_watermarks(1, 2, 0);
	assert(!m.low && !m.high);
	m = hmt_reclaim_watermarks(ULONG_MAX, ULONG_MAX, ULONG_MAX);
	assert(m.low == ULONG_MAX - 1 && m.high == ULONG_MAX - 1);
	puts("reclaim watermark contracts passed");
	return 0;
}
