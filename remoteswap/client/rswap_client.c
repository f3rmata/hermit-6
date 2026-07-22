#include <linux/kernel.h>
#include <linux/err.h>
#include <linux/version.h>
#include <linux/module.h>
#include <linux/types.h>
#include <linux/inet.h>
#include <linux/init.h>

#include "rswap_ops.h"
#include "utils.h"

MODULE_AUTHOR("Chenxi Wang, Yifan Qiao");
MODULE_DESCRIPTION("RSWAP, remote memory paging over RDMA");
MODULE_LICENSE("Dual BSD/GPL");
MODULE_VERSION("1.0");

static char server_ip[INET_ADDRSTRLEN];
static int server_port;
static int remote_mem_size;

MODULE_PARM_DESC(sip, "Remote memory server ip address");
MODULE_PARM_DESC(sport, "Remote memory server port");
MODULE_PARM_DESC(rmsize, "Remote memory size in GB");
module_param_string(sip, server_ip, INET_ADDRSTRLEN, 0644);
module_param_named(sport, server_port, int, 0644);
module_param_named(rmsize, remote_mem_size, int, 0644);

// invoked by insmod
static int __init rswap_cpu_init(void)
{
	int ret = 0;

	ret = rswap_client_init(server_ip, server_port, remote_mem_size);
	if (unlikely(ret)) {
		printk(KERN_ERR "%s, rswap_client_init failed.\n", __func__);
		goto out;
	}

	ret = rswap_register_backend();
	if (unlikely(ret)) {
		printk(KERN_ERR "%s, Hermit backend registration failed.\n",
		       __func__);
		rswap_client_exit();
		goto out;
	}

out:
	return ret;
}

// invoked by rmmod
static void __exit rswap_cpu_exit(void)
{
	printk(" Prepare to remove the CPU Server module.\n");
	rswap_unregister_backend();
	rswap_client_exit();
	printk(" Remove CPU Server module DONE.\n");
	return;
}

module_init(rswap_cpu_init);
module_exit(rswap_cpu_exit);
