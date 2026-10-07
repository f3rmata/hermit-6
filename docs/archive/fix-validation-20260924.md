# Hermit 修复与验证（2026-09-24）

基于工作区 `linux-stable` 的 `hermit-6.18` 分支（HEAD `330b5850d223`），
保留原有未提交 PEBS 改动，在其上修复。以下是本轮实际执行的结果。

## 修复

- syscall 472 用户程序改用 `long` 参数及 `%ld`，修复内核写 8 字节到
  4 字节变量导致的栈破坏。
- 回收在解除映射前、持 anon_vma 读锁时计算策略，以栈参数传入写出层；
  无样本使用静态配置，发送前应用 backend 有效 mask。
- RDMA 支持中间 order 分段；部分提交失败先排空，再按 base page 重试。
  新增 `/sys/module/rswap_client/parameters/wr_stats`：实际 WR 数和字节，包含重试。
- DRAM 对大 folio 始终报告 base-page fallback；回归检查有效 mask 为 1。
- remote-only entry 的 backend 不可用时读取报错，不读失效的本地副本。
- PEBS ring 使用真实 allocation page order，处理跨页、回绕、长度检查和
  acquire/release 顺序；采用非覆盖模式。
- perf ring 初始化使用正确引用计数，detach 后不重复 put；采样区域持有
  mm 身份引用，过期区域回退未知；参数读取避免并发除零，region_max=0
  按最小容量 1 处理；采样 syscall 要求 CAP_SYS_ADMIN。
- 合成位图测试复用生产决策函数；新增 ring 与 perf 生命周期测试。

## 已执行检查

| 检查 | 结果 |
|---|---|
| GCC 14 构建 bzImage、brd.ko、DRAM rswap-client.ko | 通过 |
| RDMA `rswap_rdma_ops.o` 使用本仓库内核头文件编译 | 通过；不等价于 OFED 模块链接 |
| `python3 tools/rdma/tests/test_rdma_submission.py` | 通过；ASan/UBSan，验证整体/分段/base-page、非法 order、越界、部分提交排空 |
| `python3 tools/rdma/tests/test_remote_read.py` | 通过；ASan/UBSan，验证 backend 消失和读取错误不回退本地 |
| QEMU direct swap-in 基线 | PASS |
| QEMU 64 KiB + force_order=4 + lazy_poll | PASS；558 次 order-4 store 和 load |
| QEMU 2 MiB + force_order=9 | PASS；99 次 order-9 store，DRAM 回退 4 KiB |
| QEMU 普通 swapcache（bypass=N） | PASS |
| 四个通过用例的 PEBS ring/生命周期、合成位图检查 | PASS |
| 四个通过用例的 checksum、backend errors=0、本地写扇区不变 | PASS |
| 四个通过用例日志中的 WARNING/BUG/panic/stack smashing | 未发现 |

QEMU 使用 KVM、4 vCPU、2560 MiB guest RAM、1 GiB DRAM pool、1 GiB swap、
1200 MiB memhog、400 MiB tmpfs。各用例的其他参数：

```sh
# 所有用例公共设置（先运行 build-qemu-dram.sh）
QEMU_ACCEL=kvm QEMU_CPU=host SKIP_BUILD=1 TIMEOUT_SEC=360 \
GUEST_RAM_MB=2560 RSWAP_MEM_GB=1 SWAP_MB=1024 \
MEMHOG_MB=1200 TMPFS_FILL_MB=400 \
./tools/qemu-dram/validate-qemu-dram.sh

# 64 KiB: 额外设置
THP_SIZE_KB=64 REMOTE_ORDER_MASK=0x11 LAZY_POLL=Y FORCE_ORDER=4
# 2 MiB: 额外设置
THP_SIZE_KB=2048 REMOTE_ORDER_MASK=0x201 FORCE_ORDER=9
# 普通 swapcache: 额外设置
BYPASS_SWAPCACHE=N
```

本轮日志分别位于 `/tmp/hermit-qemu-normal/guest-serial.log`、
`/tmp/hermit-qemu-64k-fixed/guest-serial.log`、
`/tmp/hermit-qemu-2m/guest-serial.log`、
`/tmp/hermit-qemu-swapcache/guest-serial.log`。这些是临时路径，清理 /tmp 后不保留。
首次 64 KiB 回归因旧 effective-mask 断言错误退出，失败日志保留在
`/tmp/hermit-qemu-64k/`；修复断言后的重跑见 `64k-fixed`。

## 尚未覆盖

本机没有匹配目标内核的 OFED 安装及已配置的 RDMA memory server，因此没有
执行完整 RDMA 模块链接和真实 NIC 端到端测试。硬件 PEBS 样本归属、实际
采样开销、自动策略优于静态最佳配置，以及 DMA/CQ 并发行为仍需实机验证。
软件测试直接编译生产函数并模拟依赖，不验证 RDMA 驱动或硬件。
