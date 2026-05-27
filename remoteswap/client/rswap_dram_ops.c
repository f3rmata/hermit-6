#include <linux/errno.h>
#include <linux/hermit_backend.h>
#include <linux/printk.h>
#include <linux/swapops.h>

#include "rswap_dram.h"
#include "rswap_ops.h"

static int rswap_hermit_store(swp_entry_t entry, struct page *page, int cpu,
			      bool async)
{
	size_t roffset;
	int ret;

	(void)cpu;
	(void)async;

	ret = rswap_dram_prepare_store(entry, &roffset);
	if (ret)
		return ret;

	ret = rswap_dram_write(page, roffset);
	if (unlikely(ret)) {
		rswap_dram_invalidate_page(entry);
		pr_err_ratelimited("rswap_dram: store failed for entry 0x%lx: %d\n",
				   entry.val, ret);
	}

	return ret;
}

static int rswap_hermit_load(swp_entry_t entry, struct page *page, int cpu,
			     bool async)
{
	size_t roffset;
	int ret;

	(void)cpu;
	(void)async;

	ret = rswap_dram_prepare_load(entry, &roffset);
	if (ret)
		return ret;

	ret = rswap_dram_read(page, roffset);
	if (unlikely(ret && ret != -ENOENT))
		pr_err_ratelimited("rswap_dram: load failed for entry 0x%lx: %d\n",
				   entry.val, ret);

	return ret;
}

static int rswap_hermit_poll_load(int cpu)
{
	(void)cpu;
	return 0;
}

static int rswap_hermit_peek_load(int cpu)
{
	(void)cpu;
	return 1;
}

static int rswap_hermit_poll_store(int cpu)
{
	(void)cpu;
	return 0;
}

static int rswap_hermit_peek_store(int cpu)
{
	(void)cpu;
	return 0;
}

static const struct hermit_backend_ops rswap_hermit_ops = {
	.load = rswap_hermit_load,
	.store = rswap_hermit_store,
	.invalidate_page = rswap_dram_invalidate_page,
	.invalidate_area = rswap_dram_invalidate_area,
	.poll_load = rswap_hermit_poll_load,
	.peek_load = rswap_hermit_peek_load,
	.poll_store = rswap_hermit_poll_store,
	.peek_store = rswap_hermit_peek_store,
};

int rswap_register_backend(void)
{
	int ret;

	ret = hermit_register_backend(&rswap_hermit_ops);
	if (ret)
		return ret;

	pr_info("rswap_dram: Hermit backend registered\n");
	return 0;
}

void rswap_unregister_backend(void)
{
	hermit_unregister_backend(&rswap_hermit_ops);
	pr_info("rswap_dram: Hermit backend unregistered\n");
}

int rswap_client_init(char *server_ip, int server_port, int mem_size)
{
	(void)server_ip;
	(void)server_port;

	return rswap_init_local_dram(mem_size);
}

void rswap_client_exit(void)
{
	rswap_remove_local_dram();
}
