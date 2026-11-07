#include <linux/errno.h>
#include <linux/hermit_backend.h>
#include <linux/huge_mm.h>
#include <linux/module.h>
#include <linux/printk.h>
#include <linux/swapops.h>

#include "rswap_dram.h"
#include "rswap_ops.h"

static bool force_local;
module_param(force_local, bool, 0644);
MODULE_PARM_DESC(force_local,
		 "force native swap writes (diagnostic validation only)");

static int rswap_hermit_store(struct hermit_io *io)
{
	int ret;

	if (force_local)
		return -EOPNOTSUPP;

	io->fallback = io->folio_order > 0;
	ret = rswap_dram_write_folio(io->folio, (size_t)io->remote_offset << PAGE_SHIFT);
	if (unlikely(ret))
		pr_err_ratelimited("rswap_dram: store failed for entry 0x%lx: %d\n",
				   io->entry.val, ret);

	return ret;
}

static int rswap_hermit_load(struct hermit_io *io, bool async)
{
	int ret;

	(void)async;

	io->fallback = io->folio_order > 0;
	ret = rswap_dram_read_folio(io->folio, (size_t)io->remote_offset << PAGE_SHIFT);
	if (unlikely(ret && ret != -ENOENT))
		pr_err_ratelimited("rswap_dram: load failed for entry 0x%lx: %d\n",
				   io->entry.val, ret);

	return ret;
}

static int rswap_hermit_poll(struct hermit_io *io, bool wait)
{
	(void)io;
	(void)wait;
	return 0;
}

/* DRAM has no WR coalescing: report and complete each base page. */
static int rswap_hermit_batch(struct hermit_batch *batch)
{
	unsigned int i;

	for (i = 0; i < batch->nr; i++) {
		size_t off = (size_t)batch->offsets[i] << PAGE_SHIFT;

		batch->status[i] = batch->write ?
			(force_local ? -EOPNOTSUPP :
			 rswap_dram_write(folio_page(batch->folios[i], 0), off)) :
			rswap_dram_read(folio_page(batch->folios[i], 0), off);
	}
	return 0;
}

static int rswap_hermit_batch_poll(struct hermit_batch *batch, bool wait)
{
	return 0;
}

static struct hermit_backend_ops rswap_hermit_ops = {
	.supported_order_mask = BIT(0),
	.load = rswap_hermit_load,
	.store = rswap_hermit_store,
	.poll = rswap_hermit_poll,
	.submit_batch = rswap_hermit_batch,
	.poll_batch = rswap_hermit_batch_poll,
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

	rswap_hermit_ops.capacity_pages = (unsigned long)mem_size << (30 - PAGE_SHIFT);
	rswap_hermit_ops.boundary_pages = 1UL << (30 - PAGE_SHIFT);
	return rswap_init_local_dram(mem_size);
}

void rswap_client_exit(void)
{
	rswap_remove_local_dram();
}
