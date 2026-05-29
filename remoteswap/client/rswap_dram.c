#include <linux/errno.h>
#include <linux/bitops.h>
#include <linux/debugfs.h>
#include <linux/err.h>
#include <linux/printk.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/swap.h>
#include <linux/swapops.h>
#include <linux/xarray.h>

#include "rswap_dram.h"
#include "constants.h"

static void *local_dram;
static uint64_t local_mem_size;
static unsigned long local_nr_pages;
static unsigned long *local_dram_used;
static unsigned long *local_dram_valid;
static DEFINE_XARRAY(local_dram_slots);
static DEFINE_SPINLOCK(local_dram_slots_lock);
static struct dentry *rswap_dram_debugfs_dir;

static atomic_t rswap_dram_stores;
static atomic_t rswap_dram_loads;
static atomic_t rswap_dram_load_misses;
static atomic_t rswap_dram_errors;

static void rswap_dram_reset_stats(void)
{
	atomic_set(&rswap_dram_stores, 0);
	atomic_set(&rswap_dram_loads, 0);
	atomic_set(&rswap_dram_load_misses, 0);
	atomic_set(&rswap_dram_errors, 0);
}

static void rswap_dram_debugfs_init(void)
{
	rswap_dram_debugfs_dir = debugfs_create_dir("rswap_dram", NULL);
	if (IS_ERR_OR_NULL(rswap_dram_debugfs_dir)) {
		rswap_dram_debugfs_dir = NULL;
		return;
	}

	debugfs_create_atomic_t("stores", 0444, rswap_dram_debugfs_dir,
				&rswap_dram_stores);
	debugfs_create_atomic_t("loads", 0444, rswap_dram_debugfs_dir,
				&rswap_dram_loads);
	debugfs_create_atomic_t("load_misses", 0444, rswap_dram_debugfs_dir,
				&rswap_dram_load_misses);
	debugfs_create_atomic_t("errors", 0444, rswap_dram_debugfs_dir,
				&rswap_dram_errors);
}

static void rswap_dram_debugfs_remove(void)
{
	debugfs_remove_recursive(rswap_dram_debugfs_dir);
	rswap_dram_debugfs_dir = NULL;
}

static int rswap_dram_check_offset(size_t roffset)
{
	if (!local_dram)
		return -EINVAL;

	if (roffset > local_mem_size || PAGE_SIZE > local_mem_size - roffset)
		return -EINVAL;

	return 0;
}

static unsigned long rswap_dram_offset_to_page(size_t roffset)
{
	return roffset >> PAGE_SHIFT;
}

static int rswap_dram_slot_to_offset(unsigned long slot, size_t *roffset)
{
	if (slot >= local_nr_pages)
		return -EINVAL;

	*roffset = (size_t)slot << PAGE_SHIFT;
	return 0;
}

static int rswap_dram_reserve_slot_locked(swp_entry_t entry,
					  unsigned long *slot)
{
	void *item;
	int ret;

	item = xa_load(&local_dram_slots, entry.val);
	if (item) {
		*slot = xa_to_value(item);
		clear_bit(*slot, local_dram_valid);
		return 0;
	}

	*slot = find_first_zero_bit(local_dram_used, local_nr_pages);
	if (*slot >= local_nr_pages)
		return -ENOSPC;

	set_bit(*slot, local_dram_used);
	clear_bit(*slot, local_dram_valid);
	ret = xa_err(xa_store(&local_dram_slots, entry.val, xa_mk_value(*slot),
			      GFP_ATOMIC));
	if (ret) {
		clear_bit(*slot, local_dram_used);
		return ret;
	}

	return 0;
}

int rswap_dram_prepare_store(swp_entry_t entry, size_t *roffset)
{
	unsigned long flags;
	unsigned long slot;
	int ret;

	if (!local_dram || !local_dram_used || !local_dram_valid)
		return -ENODEV;

	spin_lock_irqsave(&local_dram_slots_lock, flags);
	ret = rswap_dram_reserve_slot_locked(entry, &slot);
	spin_unlock_irqrestore(&local_dram_slots_lock, flags);
	if (ret)
		return ret;

	return rswap_dram_slot_to_offset(slot, roffset);
}

int rswap_dram_prepare_load(swp_entry_t entry, size_t *roffset)
{
	unsigned long flags;
	unsigned long slot;
	void *item;
	int ret = 0;

	if (!local_dram || !local_dram_used || !local_dram_valid)
		return -ENODEV;

	spin_lock_irqsave(&local_dram_slots_lock, flags);
	item = xa_load(&local_dram_slots, entry.val);
	if (!item) {
		ret = -ENOENT;
		goto unlock;
	}

	slot = xa_to_value(item);
	if (slot >= local_nr_pages || !test_bit(slot, local_dram_valid)) {
		ret = -ENOENT;
		goto unlock;
	}

	ret = rswap_dram_slot_to_offset(slot, roffset);
unlock:
	spin_unlock_irqrestore(&local_dram_slots_lock, flags);
	return ret;
}

void rswap_dram_invalidate_page(swp_entry_t entry)
{
	unsigned long flags;
	unsigned long slot;
	void *item;

	spin_lock_irqsave(&local_dram_slots_lock, flags);
	item = xa_erase(&local_dram_slots, entry.val);
	if (item) {
		slot = xa_to_value(item);
		if (slot < local_nr_pages) {
			clear_bit(slot, local_dram_used);
			clear_bit(slot, local_dram_valid);
		}
	}
	spin_unlock_irqrestore(&local_dram_slots_lock, flags);
}

void rswap_dram_invalidate_area(unsigned int type)
{
	unsigned long flags;
	unsigned long index = 0;
	unsigned long slot;
	void *item;

	spin_lock_irqsave(&local_dram_slots_lock, flags);
	while ((item = xa_find(&local_dram_slots, &index, ULONG_MAX,
			       XA_PRESENT))) {
		swp_entry_t entry = { .val = index };

		if (swp_type(entry) != type) {
			index++;
			continue;
		}

		item = xa_erase(&local_dram_slots, index);
		slot = xa_to_value(item);
		if (slot < local_nr_pages) {
			clear_bit(slot, local_dram_used);
			clear_bit(slot, local_dram_valid);
		}
		index++;
	}
	spin_unlock_irqrestore(&local_dram_slots_lock, flags);
}

int rswap_dram_write(struct page *page, size_t roffset)
{
	void *page_vaddr;
	unsigned long pgidx;
	int ret;

	ret = rswap_dram_check_offset(roffset);
	if (unlikely(ret)) {
		atomic_inc(&rswap_dram_errors);
		pr_err_ratelimited("rswap_dram_write offset 0x%zx out of range (size=0x%llx)\n",
				   roffset, local_mem_size);
		return ret;
	}

	pgidx = rswap_dram_offset_to_page(roffset);
	page_vaddr = kmap_atomic(page);
	copy_page((void *)(local_dram + roffset), page_vaddr);
	kunmap_atomic(page_vaddr);

	set_bit(pgidx, local_dram_valid);
	atomic_inc(&rswap_dram_stores);
	return 0;
}

int rswap_dram_read(struct page *page, size_t roffset)
{
	void *page_vaddr;
	unsigned long pgidx;
	int ret;

	VM_BUG_ON_PAGE(!PageLocked(page), page);
	VM_BUG_ON_PAGE(PageUptodate(page), page);

	ret = rswap_dram_check_offset(roffset);
	if (unlikely(ret)) {
		atomic_inc(&rswap_dram_errors);
		pr_err_ratelimited("rswap_dram_read offset 0x%zx out of range (size=0x%llx)\n",
				   roffset, local_mem_size);
		return ret;
	}

	pgidx = rswap_dram_offset_to_page(roffset);
	if (!test_bit(pgidx, local_dram_valid)) {
		atomic_inc(&rswap_dram_load_misses);
		return -ENOENT;
	}

	page_vaddr = kmap_atomic(page);
	copy_page(page_vaddr, (void *)(local_dram + roffset));
	kunmap_atomic(page_vaddr);

	folio_mark_uptodate(page_folio(page));
	atomic_inc(&rswap_dram_loads);
	return 0;
}

/**
 * Allocate a local DRAM pool to debug the Hermit backend.
 */
int rswap_init_local_dram(int _mem_size)
{
	if (_mem_size <= 0)
		return -EINVAL;

	local_mem_size = (uint64_t)_mem_size * ONE_GB;
	local_nr_pages = local_mem_size >> PAGE_SHIFT;
	local_dram_used = vzalloc(BITS_TO_LONGS(local_nr_pages) *
				  sizeof(unsigned long));
	if (!local_dram_used) {
		pr_err("failed to allocate local dram used bitmap for 0x%llx bytes\n",
		       local_mem_size);
		return -ENOMEM;
	}

	local_dram_valid = vzalloc(BITS_TO_LONGS(local_nr_pages) *
				   sizeof(unsigned long));
	if (!local_dram_valid) {
		pr_err("failed to allocate local dram valid bitmap for 0x%llx bytes\n",
		       local_mem_size);
		vfree(local_dram_used);
		local_dram_used = NULL;
		return -ENOMEM;
	}

	local_dram = vzalloc(local_mem_size);
	if (!local_dram) {
		pr_err("failed to allocate local dram 0x%llx bytes for debug\n",
		       local_mem_size);
		vfree(local_dram_valid);
		vfree(local_dram_used);
		local_dram_used = NULL;
		local_dram_valid = NULL;
		local_nr_pages = 0;
		return -ENOMEM;
	}

	rswap_dram_reset_stats();
	rswap_dram_debugfs_init();
	pr_info("Allocate local dram 0x%llx bytes for debug\n", local_mem_size);
	return 0;
}

int rswap_remove_local_dram(void)
{
	rswap_dram_debugfs_remove();
	xa_destroy(&local_dram_slots);
	if (local_dram)
		vfree(local_dram);
	if (local_dram_used)
		vfree(local_dram_used);
	if (local_dram_valid)
		vfree(local_dram_valid);
	local_dram = NULL;
	local_dram_used = NULL;
	local_dram_valid = NULL;
	local_nr_pages = 0;
	pr_info("Free the allocated local_dram 0x%llx bytes \n",
		local_mem_size);
	local_mem_size = 0;
	return 0;
}
