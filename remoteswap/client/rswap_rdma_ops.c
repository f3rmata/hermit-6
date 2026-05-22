#include <linux/swap_stats.h>
#include <linux/hermit.h>
#include <linux/bitops.h>
#include <linux/debugfs.h>
#include <linux/errno.h>
#include <linux/hermit_backend.h>
#include <linux/swapops.h>
#include <linux/vmalloc.h>

#include "rswap_rdma.h"

static unsigned long *rswap_rdma_valid;
static unsigned long rswap_rdma_nr_pages;
static struct dentry *rswap_rdma_debugfs_dir;

static atomic_t rswap_rdma_stores;
static atomic_t rswap_rdma_loads;
static atomic_t rswap_rdma_load_misses;
static atomic_t rswap_rdma_post_errors;
static atomic_t rswap_rdma_wc_errors;
static atomic_t rswap_rdma_poll_loads;
static atomic_t rswap_rdma_poll_stores;

static void rswap_rdma_reset_stats(void)
{
	atomic_set(&rswap_rdma_stores, 0);
	atomic_set(&rswap_rdma_loads, 0);
	atomic_set(&rswap_rdma_load_misses, 0);
	atomic_set(&rswap_rdma_post_errors, 0);
	atomic_set(&rswap_rdma_wc_errors, 0);
	atomic_set(&rswap_rdma_poll_loads, 0);
	atomic_set(&rswap_rdma_poll_stores, 0);
}

static void rswap_rdma_debugfs_init(void)
{
	rswap_rdma_debugfs_dir = debugfs_create_dir("rswap_rdma", NULL);
	if (IS_ERR_OR_NULL(rswap_rdma_debugfs_dir)) {
		rswap_rdma_debugfs_dir = NULL;
		return;
	}

	debugfs_create_atomic_t("stores", 0444, rswap_rdma_debugfs_dir,
				&rswap_rdma_stores);
	debugfs_create_atomic_t("loads", 0444, rswap_rdma_debugfs_dir,
				&rswap_rdma_loads);
	debugfs_create_atomic_t("load_misses", 0444, rswap_rdma_debugfs_dir,
				&rswap_rdma_load_misses);
	debugfs_create_atomic_t("post_errors", 0444, rswap_rdma_debugfs_dir,
				&rswap_rdma_post_errors);
	debugfs_create_atomic_t("wc_errors", 0444, rswap_rdma_debugfs_dir,
				&rswap_rdma_wc_errors);
	debugfs_create_atomic_t("poll_loads", 0444, rswap_rdma_debugfs_dir,
				&rswap_rdma_poll_loads);
	debugfs_create_atomic_t("poll_stores", 0444, rswap_rdma_debugfs_dir,
				&rswap_rdma_poll_stores);
}

static void rswap_rdma_debugfs_remove(void)
{
	debugfs_remove_recursive(rswap_rdma_debugfs_dir);
	rswap_rdma_debugfs_dir = NULL;
}

static int rswap_rdma_valid_init(int mem_size)
{
	u64 remote_bytes;

	if (mem_size <= 0)
		return -EINVAL;

	remote_bytes = (u64)mem_size * ONE_GB;
	rswap_rdma_nr_pages = remote_bytes >> PAGE_SHIFT;
	rswap_rdma_valid = vzalloc(BITS_TO_LONGS(rswap_rdma_nr_pages) *
				   sizeof(unsigned long));
	if (!rswap_rdma_valid) {
		rswap_rdma_nr_pages = 0;
		return -ENOMEM;
	}

	rswap_rdma_reset_stats();
	rswap_rdma_debugfs_init();
	return 0;
}

static void rswap_rdma_valid_exit(void)
{
	rswap_rdma_debugfs_remove();
	vfree(rswap_rdma_valid);
	rswap_rdma_valid = NULL;
	rswap_rdma_nr_pages = 0;
}

static int rswap_rdma_check_offset(pgoff_t offset)
{
	if (!rswap_rdma_valid)
		return -ENODEV;
	if (offset >= rswap_rdma_nr_pages)
		return -EINVAL;

	return 0;
}

static bool rswap_rdma_test_valid(pgoff_t offset)
{
	return rswap_rdma_valid && test_bit(offset, rswap_rdma_valid);
}

static void rswap_rdma_set_valid(pgoff_t offset)
{
	if (rswap_rdma_valid && offset < rswap_rdma_nr_pages)
		set_bit(offset, rswap_rdma_valid);
}

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

void write_drain_rdma_queue(struct rswap_rdma_queue *rdma_queue)
{
	int nr_pending = atomic_read(&rdma_queue->rdma_post_counter);
	int nr_done = 0;

	while (atomic_read(&rdma_queue->rdma_post_counter) > 0) {
		int nr_completed;
		// IB_POLL_BATCH is 16 by default
		nr_completed = ib_process_cq_direct(rdma_queue->cq, 64);
		nr_done += nr_completed;
		if (nr_done >= nr_pending)
			break;
		cpu_relax();
	}
}

static inline int peek_rdma_queue(struct rswap_rdma_queue *rdma_queue)
{
	if (atomic_read(&rdma_queue->rdma_post_counter) > 0)
		ib_process_cq_direct(rdma_queue->cq, 4);
	return atomic_read(&rdma_queue->rdma_post_counter);
}

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
void fs_rdma_callback(struct ib_cq *cq, struct ib_wc *wc)
{
	struct fs_rdma_req *rdma_req =
		container_of(wc->wr_cqe, struct fs_rdma_req, cqe);
	struct rswap_rdma_queue *rdma_queue = cq->cq_context;
	struct ib_device *ibdev = rdma_queue->rdma_session->rdma_dev->dev;
	enum rdma_queue_type type;
	enum dma_data_direction dir;

	if (unlikely(wc->status != IB_WC_SUCCESS)) {
		pr_err("%s status is not success, it is=%d\n", __func__,
		       wc->status);
		rdma_req->status = -EIO;
		atomic_inc(&rswap_rdma_wc_errors);
	} else {
		rdma_req->status = 0;
	}

	type = rdma_req->type;
	if (!rdma_req->status) {
		if (type == QP_STORE) {
			rswap_rdma_set_valid(rdma_req->offset);
			atomic_inc(&rswap_rdma_stores);
		} else {
			folio_mark_uptodate(page_folio(rdma_req->page));
			atomic_inc(&rswap_rdma_loads);
		}
	}

	atomic_dec(&rdma_queue->rdma_post_counter);
	if (rdma_req->dma_mapped) {
		dir = type == QP_STORE ? DMA_TO_DEVICE : DMA_FROM_DEVICE;
		ib_dma_unmap_page(ibdev, rdma_req->dma_addr, PAGE_SIZE, dir);
		rdma_req->dma_mapped = false;
	}

	if (rdma_req->sync)
		complete(&rdma_req->done);
	else
		kmem_cache_free(rdma_queue->fs_rdma_req_cache, rdma_req);
}

int fs_enqueue_send_wr(struct rdma_session_context *rdma_session,
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
				ret = ret ?: -EIO;
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
	return ret ?: -EIO;
}

/**
 * Build a rdma_wr for the Hermit backend data path.
 */
int fs_build_rdma_wr(struct rdma_session_context *rdma_session,
		     struct rswap_rdma_queue *rdma_queue,
		     struct fs_rdma_req *rdma_req,
		     struct remote_chunk *remote_chunk_ptr,
		     size_t offset_within_chunk, struct page *page,
		     enum rdma_queue_type type)
{
	int ret = 0;
	enum dma_data_direction dir;
	struct ib_device *dev = rdma_session->rdma_dev->dev;

	rdma_req->page = page;
	rdma_req->type = type;
	rdma_req->status = -EINPROGRESS;
	rdma_req->dma_mapped = false;

	dir = type == QP_STORE ? DMA_TO_DEVICE : DMA_FROM_DEVICE;
	rdma_req->dma_addr = ib_dma_map_page(dev, page, 0, PAGE_SIZE, dir);
	if (unlikely(ib_dma_mapping_error(dev, rdma_req->dma_addr))) {
		pr_err("%s, ib_dma_mapping_error\n", __func__);
		ret = -ENOMEM;
		goto out;
	}
	rdma_req->dma_mapped = true;

	ib_dma_sync_single_for_device(dev, rdma_req->dma_addr, PAGE_SIZE, dir);

	rdma_req->cqe.done = fs_rdma_callback;

	rdma_req->sge.addr = rdma_req->dma_addr;
	rdma_req->sge.length = PAGE_SIZE;
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
			(size_t)PAGE_SIZE);
	}
#endif

out:
	return ret;
}

/**
 * Enqueue a page into RDMA queue.
 */
static void rswap_rdma_free_unposted_req(struct rswap_rdma_queue *rdma_queue,
					 struct fs_rdma_req *rdma_req)
{
	enum dma_data_direction dir;

	if (!rdma_req)
		return;

	if (rdma_req->dma_mapped) {
		dir = rdma_req->type == QP_STORE ? DMA_TO_DEVICE :
						   DMA_FROM_DEVICE;
		ib_dma_unmap_page(rdma_queue->rdma_session->rdma_dev->dev,
				  rdma_req->dma_addr, PAGE_SIZE, dir);
	}
	kmem_cache_free(rdma_queue->fs_rdma_req_cache, rdma_req);
}

int rswap_rdma_send(int cpu, pgoff_t offset, struct page *page,
		    enum rdma_queue_type type, bool sync,
		    struct fs_rdma_req **sync_req)
{
	int ret = 0;
	size_t page_addr;
	size_t chunk_idx;
	size_t offset_within_chunk;
	struct rswap_rdma_queue *rdma_queue;
	struct fs_rdma_req *rdma_req;
	struct remote_chunk *remote_chunk_ptr;

	if (sync_req)
		*sync_req = NULL;

	ret = rswap_rdma_check_offset(offset);
	if (ret)
		return ret;

	page_addr = pgoff2addr(offset);
	chunk_idx = page_addr >> CHUNK_SHIFT;
	offset_within_chunk = page_addr & CHUNK_MASK;
	if (chunk_idx >= rdma_session_global.remote_mem_pool.chunk_num)
		return -EINVAL;

	rdma_queue = get_rdma_queue(&rdma_session_global, cpu, type);
	rdma_req = (struct fs_rdma_req *)kmem_cache_alloc(
		rdma_queue->fs_rdma_req_cache, GFP_ATOMIC);
	if (!rdma_req) {
		pr_err("%s, get reserved fs_rdma_req failed. \n", __func__);
		ret = -ENOMEM;
		goto out;
	}
	memset(rdma_req, 0, sizeof(*rdma_req));
	init_completion(&rdma_req->done);
	rdma_req->sync = sync;
	rdma_req->offset = offset;

	remote_chunk_ptr =
		&(rdma_session_global.remote_mem_pool.chunks[chunk_idx]);
	if (remote_chunk_ptr->chunk_state != MAPPED ||
	    offset_within_chunk + PAGE_SIZE > remote_chunk_ptr->mapped_size) {
		ret = -ENOENT;
		goto free_req;
	}

	ret = fs_build_rdma_wr(&rdma_session_global, rdma_queue, rdma_req,
			       remote_chunk_ptr, offset_within_chunk, page,
			       type);
	if (unlikely(ret)) {
		pr_err("%s, Build rdma_wr failed.\n", __func__);
		goto free_req;
	}

	ret = fs_enqueue_send_wr(&rdma_session_global, rdma_queue, rdma_req);
	if (unlikely(ret)) {
		pr_err("%s, enqueue rdma_wr failed.\n", __func__);
		goto free_req;
	}
	if (sync_req)
		*sync_req = rdma_req;
	return 0;

free_req:
	atomic_inc(&rswap_rdma_post_errors);
	rswap_rdma_free_unposted_req(rdma_queue, rdma_req);
out:
	return ret;
}

int rswap_rdma_wait_req(struct rswap_rdma_queue *rdma_queue,
			struct fs_rdma_req *rdma_req)
{
	while (!completion_done(&rdma_req->done)) {
		ib_process_cq_direct(rdma_queue->cq, 16);
		cpu_relax();
	}

	return rdma_req->status;
}

static int rswap_rdma_poll_load(int cpu)
{
	struct rswap_rdma_queue *rdma_queue;

	if (unlikely(!online_cores))
		return -ENODEV;

	cpu %= online_cores;
	rdma_queue = get_rdma_queue(&rdma_session_global, cpu, QP_LOAD_SYNC);
	drain_rdma_queue(rdma_queue);
	atomic_inc(&rswap_rdma_poll_loads);
	return 0;
}

static int rswap_rdma_peek_load(int cpu)
{
	struct rswap_rdma_queue *rdma_queue;

	if (unlikely(!online_cores))
		return -ENODEV;

	cpu %= online_cores;
	rdma_queue = get_rdma_queue(&rdma_session_global, cpu, QP_LOAD_SYNC);
	return peek_rdma_queue(rdma_queue);
}

static int rswap_rdma_poll_store(int cpu)
{
	struct rswap_rdma_queue *rdma_queue;

	if (unlikely(!online_cores))
		return -ENODEV;

	cpu %= online_cores;
	rdma_queue = get_rdma_queue(&rdma_session_global, cpu, QP_STORE);
	write_drain_rdma_queue(rdma_queue);
	atomic_inc(&rswap_rdma_poll_stores);
	return 0;
}

static int rswap_rdma_peek_store(int cpu)
{
	struct rswap_rdma_queue *rdma_queue;

	if (unlikely(!online_cores))
		return -ENODEV;

	cpu %= online_cores;
	rdma_queue = get_rdma_queue(&rdma_session_global, cpu, QP_STORE);
	return peek_rdma_queue(rdma_queue);
}

static int rswap_hermit_store(swp_entry_t entry, struct page *page, int cpu,
			      bool async)
{
	int ret;

	(void)async;
	if (unlikely(!online_cores))
		return -ENODEV;

	cpu %= online_cores;
	ret = rswap_rdma_send(cpu, swp_offset(entry), page, QP_STORE, false,
			      NULL);
	if (unlikely(ret)) {
		pr_err_ratelimited("rswap_rdma: store post failed for entry 0x%lx: %d\n",
				   entry.val, ret);
	}

	return ret;
}

static int rswap_hermit_load(swp_entry_t entry, struct page *page, int cpu,
			     bool async)
{
	struct rswap_rdma_queue *rdma_queue;
	struct fs_rdma_req *rdma_req = NULL;
	pgoff_t offset = swp_offset(entry);
	int ret;

	if (unlikely(!online_cores))
		return -ENODEV;

	ret = rswap_rdma_check_offset(offset);
	if (ret)
		return ret;
	if (!rswap_rdma_test_valid(offset)) {
		atomic_inc(&rswap_rdma_load_misses);
		return -ENOENT;
	}

	cpu %= online_cores;
	rdma_queue = get_rdma_queue(&rdma_session_global, cpu, QP_LOAD_SYNC);
	ret = rswap_rdma_send(cpu, offset, page, QP_LOAD_SYNC, !async,
			      async ? NULL : &rdma_req);
	if (unlikely(ret)) {
		pr_err_ratelimited("rswap_rdma: load post failed for entry 0x%lx: %d\n",
				   entry.val, ret);
		return ret;
	}

	if (!async) {
		ret = rswap_rdma_wait_req(rdma_queue, rdma_req);
		kmem_cache_free(rdma_queue->fs_rdma_req_cache, rdma_req);
	}

	return ret;
}

static const struct hermit_backend_ops rswap_hermit_ops = {
	.load = rswap_hermit_load,
	.store = rswap_hermit_store,
	.poll_load = rswap_rdma_poll_load,
	.peek_load = rswap_rdma_peek_load,
	.poll_store = rswap_rdma_poll_store,
	.peek_store = rswap_rdma_peek_store,
};

int rswap_register_backend(void)
{
	int ret;

	ret = hermit_register_backend(&rswap_hermit_ops);
	if (ret)
		return ret;

	pr_info("rswap_rdma: Hermit backend registered\n");
	return 0;
}

void rswap_unregister_backend(void)
{
	hermit_unregister_backend(&rswap_hermit_ops);
	pr_info("rswap_rdma: Hermit backend unregistered\n");
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

	ret = rswap_rdma_valid_init(_mem_size);
	if (unlikely(ret)) {
		pr_err("%s, RDMA valid bitmap init failed: %d\n", __func__,
		       ret);
		goto out;
	}

	// init the rdma session to memory server
	ret = init_rdma_sessions(&rdma_session_global);
	if (unlikely(ret)) {
		pr_err("%s, init_rdma_sessions failed. \n", __func__);
		goto err_valid;
	}

	// Build both the RDMA and Disk driver
	ret = rdma_session_connect(&rdma_session_global);
	if (unlikely(ret)) {
		pr_err("%s, rdma_session_connect failed. \n", __func__);
		goto err_valid;
	}

out:
	return ret;
err_valid:
	rswap_rdma_valid_exit();
	return ret;
}

void rswap_client_exit(void)
{
	int ret;
	ret = rswap_disconnect_and_collect_resource(&rdma_session_global);
	if (unlikely(ret)) {
		pr_err("%s,  failed.\n", __func__);
	}
	rswap_rdma_valid_exit();
	pr_info("%s done.\n", __func__);
}
