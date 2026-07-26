#include <linux/hermit_backend.h>
#include <linux/huge_mm.h>
#include <linux/module.h>
#include <linux/slab.h>
#include <linux/swapops.h>

#include "rswap_rdma.h"
#include "rswap_ops.h"

/**
 * Wait for the finish of ALL the outstanding rdma_request
 */
void drain_rdma_queue(struct rswap_rdma_queue *rdma_queue)
{
	int nr_pending = atomic_read(&rdma_queue->rdma_post_counter);
	int nr_done = 0;

	preempt_disable();
	while (atomic_read(&rdma_queue->rdma_post_counter) > 0) {
		int nr_completed;
		// IB_POLL_BATCH is 16 by default
		nr_completed = ib_process_cq_direct(rdma_queue->cq, 4);
		nr_done += nr_completed;
		if (nr_done >= nr_pending)
			break;
		cpu_relax();
	}
	preempt_enable();
}

struct rswap_io_context {
	struct rswap_rdma_queue *queue;
	atomic_t pending;
	atomic_t status;
	bool fallback_attempted;
};

static unsigned int max_order = PMD_ORDER;
module_param(max_order, uint, 0644);
MODULE_PARM_DESC(max_order, "maximum folio order for one RDMA WR");

/**
 * Drain all the outstanding messages for a specific memory server.
 */
void drain_all_rdma_queues(int target_mem_server)
{
	int i;
	struct rdma_session_context *rdma_session = &rdma_session_global;

	for (i = 0; i < num_queues; i++) {
		drain_rdma_queue(&(rdma_session->rdma_queues[i]));
	}
}

/**
 * The callback function for rdma requests.
 */
static void fs_rdma_callback(struct ib_cq *cq, struct ib_wc *wc)
{
	struct fs_rdma_req *rdma_req =
		container_of(wc->wr_cqe, struct fs_rdma_req, cqe);
	struct rswap_rdma_queue *rdma_queue = cq->cq_context;
	struct ib_device *ibdev = rdma_queue->rdma_session->rdma_dev->dev;
	struct rswap_io_context *io = rdma_req->io_context;

	if (unlikely(wc->status != IB_WC_SUCCESS)) {
		pr_err("%s status is not success, it is=%d\n", __func__,
		       wc->status);
		atomic_cmpxchg(&io->status, 0, -EIO);
	}

	atomic_dec(&rdma_queue->rdma_post_counter);
	ib_dma_unmap_page(ibdev, rdma_req->dma_addr, rdma_req->dma_len,
			  rdma_req->dma_dir);
	atomic_dec(&io->pending);
	kmem_cache_free(rdma_queue->fs_rdma_req_cache, rdma_req);
}

static int fs_enqueue_send_wr(struct rdma_session_context *rdma_session,
		       struct rswap_rdma_queue *rdma_queue,
		       struct fs_rdma_req *rdma_req)
{
	int ret = 0;
	const struct ib_send_wr *bad_wr;
	int test;

	rdma_req->rdma_queue = rdma_queue;

	while (1) {
		test = atomic_inc_return(&rdma_queue->rdma_post_counter);
		if (test < RDMA_SEND_QUEUE_DEPTH - 16) {
			ret = ib_post_send(
				rdma_queue->qp,
				(struct ib_send_wr *)&rdma_req->rdma_wr,
				&bad_wr);
			if (unlikely(ret)) {
				pr_err("%s, post 1-sided RDMA send wr failed, "
				       "return value :%d. counter %d \n",
				       __func__, ret, test);
				atomic_dec(&rdma_queue->rdma_post_counter);
				ret = -EIO;
				goto err;
			}

			return ret;
		} else { // RDMA send queue is full, wait for next turn.
			test = atomic_dec_return(
				&rdma_queue->rdma_post_counter);
			cpu_relax();

			drain_rdma_queue(rdma_queue);
			pr_err("%s, back pressure...\n", __func__);
		}
	}
err:
	pr_err(" Error in %s \n", __func__);
	return ret;
}

/**
 * Build a work request for the Hermit backend data path.
 */
static int fs_build_rdma_wr(struct rdma_session_context *rdma_session,
		     struct rswap_rdma_queue *rdma_queue,
		     struct fs_rdma_req *rdma_req,
		     struct remote_chunk *remote_chunk_ptr,
		     size_t offset_within_chunk, struct page *page, size_t len,
		     struct rswap_io_context *io,
		     enum rdma_queue_type type)
{
	int ret = 0;
	enum dma_data_direction dir;
	struct ib_device *dev = rdma_session->rdma_dev->dev;

	rdma_req->page = page;
	rdma_req->io_context = io;
	rdma_req->dma_len = len;

	dir = type == QP_STORE ? DMA_TO_DEVICE : DMA_FROM_DEVICE;
	rdma_req->dma_dir = dir;
	rdma_req->dma_addr = ib_dma_map_page(dev, page, 0, len, dir);
	if (unlikely(ib_dma_mapping_error(dev, rdma_req->dma_addr))) {
		pr_err("%s, ib_dma_mapping_error\n", __func__);
		ret = -ENOMEM;
		goto out;
	}

	ib_dma_sync_single_for_device(dev, rdma_req->dma_addr, len, dir);

	rdma_req->cqe.done = fs_rdma_callback;

	rdma_req->sge.addr = rdma_req->dma_addr;
	rdma_req->sge.length = len;
	rdma_req->sge.lkey = rdma_session->rdma_dev->pd->local_dma_lkey;

	rdma_req->rdma_wr.wr.next = NULL;
	rdma_req->rdma_wr.wr.wr_cqe = &rdma_req->cqe;
	rdma_req->rdma_wr.wr.sg_list = &(rdma_req->sge);
	rdma_req->rdma_wr.wr.num_sge = 1;
	rdma_req->rdma_wr.wr.opcode =
		(dir == DMA_TO_DEVICE ? IB_WR_RDMA_WRITE : IB_WR_RDMA_READ);
	rdma_req->rdma_wr.wr.send_flags = IB_SEND_SIGNALED;
	rdma_req->rdma_wr.remote_addr =
		remote_chunk_ptr->remote_addr + offset_within_chunk;
	rdma_req->rdma_wr.rkey = remote_chunk_ptr->remote_rkey;

// debug
#ifdef DEBUG_MODE_BRIEF
	if (dir == DMA_FROM_DEVICE) {
		pr_info("%s, read data from remote 0x%lx, size 0x%lx \n",
			__func__, (size_t)rdma_req->rdma_wr.remote_addr,
			len);
	}
#endif

out:
	return ret;
}

/**
 * Enqueue a page into RDMA queue.
 */
static int rswap_rdma_send(struct rswap_io_context *io, int cpu,
			   pgoff_t offset, struct page *page, size_t len,
			   enum rdma_queue_type type)
{
	int ret = 0;
	size_t page_addr;
	size_t chunk_idx;
	size_t offset_within_chunk;
	struct rswap_rdma_queue *rdma_queue;
	struct fs_rdma_req *rdma_req;
	struct remote_chunk *remote_chunk_ptr;

	page_addr = pgoff2addr(offset);
	chunk_idx = page_addr >> CHUNK_SHIFT;
	offset_within_chunk = page_addr & CHUNK_MASK;
	if (chunk_idx >= rdma_session_global.remote_mem_pool.chunk_num ||
	    len > (1ULL << CHUNK_SHIFT) - offset_within_chunk) {
		pr_err_ratelimited("rswap: swap offset 0x%lx exceeds remote pool\n",
				   offset);
		return -ERANGE;
	}

	rdma_queue = get_rdma_queue(&rdma_session_global, cpu, type);
	rdma_req = (struct fs_rdma_req *)kmem_cache_alloc(
		rdma_queue->fs_rdma_req_cache, GFP_ATOMIC);
	if (!rdma_req) {
		pr_err("%s, get reserved fs_rdma_req failed. \n", __func__);
		ret = -ENOMEM;
		goto out;
	}

	remote_chunk_ptr =
		&(rdma_session_global.remote_mem_pool.chunks[chunk_idx]);
	if (offset_within_chunk >= remote_chunk_ptr->mapped_size ||
	    len > remote_chunk_ptr->mapped_size - offset_within_chunk) {
		ret = -ERANGE;
		goto free_req;
	}

	ret = fs_build_rdma_wr(&rdma_session_global, rdma_queue, rdma_req,
			       remote_chunk_ptr, offset_within_chunk, page, len, io,
			       type);
	if (unlikely(ret)) {
		pr_err("%s, Build rdma_wr failed.\n", __func__);
		goto free_req;
	}

	atomic_inc(&io->pending);
	ret = fs_enqueue_send_wr(&rdma_session_global, rdma_queue, rdma_req);
	if (unlikely(ret)) {
		pr_err("%s, enqueue rdma_wr failed.\n", __func__);
		ib_dma_unmap_page(rdma_session_global.rdma_dev->dev,
				  rdma_req->dma_addr, len, rdma_req->dma_dir);
		atomic_dec(&io->pending);
		goto free_req;
	}

out:
	return ret;
free_req:
	kmem_cache_free(rdma_queue->fs_rdma_req_cache, rdma_req);
	return ret;
}

static struct rswap_io_context *rswap_io_alloc(int cpu,
				       enum rdma_queue_type type)
{
	struct rswap_io_context *ctx;

	ctx = kzalloc(sizeof(*ctx), GFP_ATOMIC);
	if (!ctx)
		return NULL;
	ctx->queue = get_rdma_queue(&rdma_session_global, cpu, type);
	atomic_set(&ctx->pending, 0);
	atomic_set(&ctx->status, 0);
	return ctx;
}

static int rswap_io_poll(struct rswap_io_context *ctx, bool wait)
{
	do {
		if (!atomic_read(&ctx->pending))
			return atomic_read(&ctx->status);
		ib_process_cq_direct(ctx->queue->cq, 16);
		if (!wait && atomic_read(&ctx->pending))
			return -EAGAIN;
		cpu_relax();
	} while (atomic_read(&ctx->pending));
	return atomic_read(&ctx->status);
}

static int rswap_submit_folio(struct hermit_io *io,
			      struct rswap_io_context *ctx,
			      enum rdma_queue_type type, bool base_pages)
{
	unsigned int i, nr_pages = folio_nr_pages(io->folio);
	int ret;

	if (!base_pages && io->transfer_order == io->folio_order &&
	    io->folio_order) {
		return rswap_rdma_send(ctx, io->cpu, swp_offset(io->entry),
				       &io->folio->page, folio_size(io->folio),
				       type);
	}

	for (i = 0; i < nr_pages; i++) {
		ret = rswap_rdma_send(ctx, io->cpu,
				      swp_offset(io->entry) + i,
				      folio_page(io->folio, i), PAGE_SIZE, type);
		if (ret) {
			atomic_cmpxchg(&ctx->status, 0, ret);
			return ret;
		}
	}
	return 0;
}

static int rswap_retry_base(struct hermit_io *io,
			    struct rswap_io_context *ctx,
			    enum rdma_queue_type type)
{
	io->fallback = true;
	ctx->fallback_attempted = true;
	atomic_set(&ctx->status, 0);
	return rswap_submit_folio(io, ctx, type, true);
}

static int rswap_backend_store(struct hermit_io *io)
{
	struct rswap_io_context *ctx;
	int ret;

	ctx = rswap_io_alloc(io->cpu, QP_STORE);
	if (!ctx)
		return -ENOMEM;
	ctx->fallback_attempted = io->transfer_order == 0;
	io->fallback = io->folio_order && io->transfer_order == 0;
	ret = rswap_submit_folio(io, ctx, QP_STORE, false);
	if (!ret || atomic_read(&ctx->pending))
		ret = rswap_io_poll(ctx, true);
	if (ret && !ctx->fallback_attempted) {
		ret = rswap_retry_base(io, ctx, QP_STORE);
		if (!ret || atomic_read(&ctx->pending))
			ret = rswap_io_poll(ctx, true);
	}
	kfree(ctx);
	return ret;
}

static int rswap_backend_load(struct hermit_io *io, bool async)
{
	struct rswap_io_context *ctx;
	enum rdma_queue_type type = async ? QP_LOAD_ASYNC : QP_LOAD_SYNC;
	int ret;

	ctx = rswap_io_alloc(io->cpu, type);
	if (!ctx)
		return -ENOMEM;
	ctx->fallback_attempted = io->transfer_order == 0;
	io->fallback = io->folio_order && io->transfer_order == 0;
	ret = rswap_submit_folio(io, ctx, type, false);
	if (ret && !atomic_read(&ctx->pending) && !ctx->fallback_attempted)
		ret = rswap_retry_base(io, ctx, type);
	if (ret && !atomic_read(&ctx->pending)) {
		kfree(ctx);
		return ret;
	}
	io->private = ctx;
	if (async)
		return 0;
	ret = rswap_io_poll(ctx, true);
	if (ret && !ctx->fallback_attempted) {
		rswap_retry_base(io, ctx, type);
		ret = rswap_io_poll(ctx, true);
	}
	io->private = NULL;
	kfree(ctx);
	return ret;
}

static int rswap_backend_poll(struct hermit_io *io, bool wait)
{
	struct rswap_io_context *ctx = io->private;
	int ret;

	if (!ctx)
		return 0;
	ret = rswap_io_poll(ctx, wait);
	if (ret == -EAGAIN)
		return ret;
	if (ret && !ctx->fallback_attempted) {
		ret = rswap_retry_base(io, ctx, QP_LOAD_ASYNC);
		if (ret && !atomic_read(&ctx->pending))
			goto done;
		ret = rswap_io_poll(ctx, wait);
		if (ret == -EAGAIN)
			return ret;
	}
done:
	io->private = NULL;
	kfree(ctx);
	return ret;
}

static struct hermit_backend_ops rswap_backend_ops = {
	.load = rswap_backend_load,
	.store = rswap_backend_store,
	.poll = rswap_backend_poll,
};

int rswap_register_backend(void)
{
	int ret;

	max_order = min_t(unsigned int, max_order, PMD_ORDER);
	rswap_backend_ops.supported_order_mask = BIT(0);
	if (max_order >= 2)
		rswap_backend_ops.supported_order_mask |= GENMASK(max_order, 2);
	ret = hermit_register_backend(&rswap_backend_ops);

	if (!ret)
		pr_info("rswap: Hermit RDMA backend registered\n");
	return ret;
}

void rswap_unregister_backend(void)
{
	hermit_unregister_backend(&rswap_backend_ops);
	pr_info("rswap: Hermit RDMA backend unregistered\n");
}

int rswap_client_init(char *_server_ip, int _server_port, int _mem_size)
{
	int ret = 0;
	printk(KERN_INFO "%s, start \n", __func__);

	// online cores decide the parallelism. e.g. number of QP, CP etc.
	online_cores = num_online_cpus();
	num_queues = online_cores * NUM_QP_TYPE;
	server_ip = _server_ip;
	server_port = _server_port;
	rdma_session_global.remote_mem_pool.remote_mem_size = _mem_size;
	rdma_session_global.remote_mem_pool.chunk_num =
		_mem_size / REGION_SIZE_GB;

	pr_info("%s, num_queues : %d (Can't exceed the slots on Memory server) \n",
		__func__, num_queues);

	// init the rdma session to memory server
	ret = init_rdma_sessions(&rdma_session_global);
	if (unlikely(ret)) {
		pr_err("%s, init_rdma_sessions failed: %d\n", __func__, ret);
		goto out;
	}

	ret = rdma_session_connect(&rdma_session_global);
	if (unlikely(ret)) {
		pr_err("%s, rdma_session_connect failed. \n", __func__);
		goto out;
	}

out:
	return ret;
}

void rswap_client_exit(void)
{
	int ret;
	ret = rswap_disconnect_and_collect_resource(&rdma_session_global);
	if (unlikely(ret)) {
		pr_err("%s,  failed.\n", __func__);
	}
	pr_info("%s done.\n", __func__);
}
