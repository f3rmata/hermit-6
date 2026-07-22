#include <linux/hermit_backend.h>
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

static void write_drain_rdma_queue(struct rswap_rdma_queue *rdma_queue)
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
static void fs_rdma_callback(struct ib_cq *cq, struct ib_wc *wc)
{
	struct fs_rdma_req *rdma_req =
		container_of(wc->wr_cqe, struct fs_rdma_req, cqe);
	struct rswap_rdma_queue *rdma_queue = cq->cq_context;
	struct ib_device *ibdev = rdma_queue->rdma_session->rdma_dev->dev;
	int cpu;
	enum rdma_queue_type type;

	if (unlikely(wc->status != IB_WC_SUCCESS)) {
		pr_err("%s status is not success, it is=%d\n", __func__,
		       wc->status);
		atomic_set(&rdma_queue->rdma_error, -EIO);
	}
	get_rdma_queue_cpu_type(&rdma_session_global, rdma_queue, &cpu, &type);

	atomic_dec(&rdma_queue->rdma_post_counter);
	ib_dma_unmap_page(ibdev, rdma_req->dma_addr, PAGE_SIZE,
			  type == QP_STORE ? DMA_TO_DEVICE : DMA_FROM_DEVICE);
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
		     size_t offset_within_chunk, struct page *page,
		     enum rdma_queue_type type)
{
	int ret = 0;
	enum dma_data_direction dir;
	struct ib_device *dev = rdma_session->rdma_dev->dev;

	rdma_req->page = page;

	dir = type == QP_STORE ? DMA_TO_DEVICE : DMA_FROM_DEVICE;
	rdma_req->dma_addr = ib_dma_map_page(dev, page, 0, PAGE_SIZE, dir);
	if (unlikely(ib_dma_mapping_error(dev, rdma_req->dma_addr))) {
		pr_err("%s, ib_dma_mapping_error\n", __func__);
		ret = -ENOMEM;
		goto out;
	}

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
int rswap_rdma_send(int cpu, pgoff_t offset, struct page *page,
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
	if (chunk_idx >= rdma_session_global.remote_mem_pool.chunk_num) {
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

	ret = fs_build_rdma_wr(&rdma_session_global, rdma_queue, rdma_req,
			       remote_chunk_ptr, offset_within_chunk, page,
			       type);
	if (unlikely(ret)) {
		pr_err("%s, Build rdma_wr failed.\n", __func__);
		kmem_cache_free(rdma_queue->fs_rdma_req_cache, rdma_req);
		goto out;
	}

	ret = fs_enqueue_send_wr(&rdma_session_global, rdma_queue, rdma_req);
	if (unlikely(ret)) {
		pr_err("%s, enqueue rdma_wr failed.\n", __func__);
		ib_dma_unmap_page(rdma_session_global.rdma_dev->dev,
				  rdma_req->dma_addr, PAGE_SIZE,
				  type == QP_STORE ? DMA_TO_DEVICE :
						     DMA_FROM_DEVICE);
		kmem_cache_free(rdma_queue->fs_rdma_req_cache, rdma_req);
		goto out;
	}

out:
	return ret;
}

static int rswap_backend_store(swp_entry_t entry, struct page *page, int cpu,
			       bool async)
{
	struct rswap_rdma_queue *queue;
	int ret;

	ret = rswap_rdma_send(cpu, swp_offset(entry), page, QP_STORE);
	if (ret)
		return ret;
	queue = get_rdma_queue(&rdma_session_global, cpu, QP_STORE);
	if (async)
		return 0;
	write_drain_rdma_queue(queue);
	return atomic_xchg(&queue->rdma_error, 0);
}

static int rswap_backend_load(swp_entry_t entry, struct page *page, int cpu,
			      bool async)
{
	struct rswap_rdma_queue *queue;
	enum rdma_queue_type type = async ? QP_LOAD_ASYNC : QP_LOAD_SYNC;
	int ret;

	ret = rswap_rdma_send(cpu, swp_offset(entry), page, type);
	if (ret)
		return ret;
	queue = get_rdma_queue(&rdma_session_global, cpu, type);
	if (async)
		return 0;
	drain_rdma_queue(queue);
	return atomic_xchg(&queue->rdma_error, 0);
}

static int rswap_backend_poll_load(int cpu)
{
	struct rswap_rdma_queue *queue;

	queue = get_rdma_queue(&rdma_session_global, cpu, QP_LOAD_ASYNC);
	drain_rdma_queue(queue);
	return atomic_xchg(&queue->rdma_error, 0);
}

static int rswap_backend_peek_load(int cpu)
{
	return peek_rdma_queue(get_rdma_queue(&rdma_session_global, cpu,
					     QP_LOAD_ASYNC));
}

static const struct hermit_backend_ops rswap_backend_ops = {
	.load = rswap_backend_load,
	.store = rswap_backend_store,
	.poll_load = rswap_backend_poll_load,
	.peek_load = rswap_backend_peek_load,
};

int rswap_register_backend(void)
{
	int ret = hermit_register_backend(&rswap_backend_ops);

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
