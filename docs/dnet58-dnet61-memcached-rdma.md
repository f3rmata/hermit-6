# dnet-58/dnet-61 Hermit RDMA 与 memcached 测试手册

本文记录 2026-07-26 已验证的测试拓扑、环境配置和完整压测命令。正式测试采用：

```text
dnet-58 (172.16.0.58)              dnet-61 (172.16.0.61)
+----------------------+  IPoIB   +----------------------------------+
| OpenSM               | <------> | Hermit 6.18 + OFED rswap-client |
| rswap-server, 48 GiB |          | memcached: CPU 0-7              |
| 64 client CPUs       |          | mutilate:  CPU 8-15             |
+----------------------+          +----------------------------------+
```

dnet-60 只提供已有脚本和二进制作为参考，不参与正式压测。memcached 和
mutilate 都在 dnet-61 运行，但绑定到同一 NUMA 节点的不同物理核心。这样不会
把外部 TCP 网络延迟混入结果，同时 RDMA NIC、内存和两个负载进程都位于 NUMA 0。

## 1. 已验证状态

### 1.1 dnet-58 memory server

- Ubuntu 26.04，内核 `5.14.0-rc5`；
- MLNX OFED `5.6-2.0.9.0`；
- IB 设备 `mlx5_0/1`，GUID `0xe8ebd303009353a8`；
- OpenSM 已绑定该 GUID，端口为 `ACTIVE`，LID/SM LID 均为 1；
- IPoIB 设备 `ibs3f0`：`172.16.0.58/24`；
- `rswap-server` 已在 `~/hermit-6/remoteswap/server/rswap-server` 构建；
- server 参数为 `172.16.0.58 9400 48 64`；
- dnet-61 接入后 192 条 RDMA 队列连接成功，server 注册约 48 GiB 内存。

dnet-58 的 `ibstat` 存在旧 `libibmad`/`libibumad` symbol version 冲突，但
`ibv_devinfo`、`librdmacm`、`libibverbs` 和实际 rswap 连接均已验证成功。
不要用单独的 `ibstat` 失败否定当前数据通路。

dnet-58 还存在 NSS 显示异常：`id` 能看到 UID/GID 1001，但提示符可能显示
`I have no name!`，SFTP/传统 SCP 会失败。普通 SSH、sudo 和 tar-over-SSH 可用。

### 1.2 dnet-61 Hermit client 与负载端

- Ubuntu 24.04.4，内核 `6.18.38-hermit`；
- DOCA/OFED `OFED-internal-26.04-0.8.6`；
- `mlx5_0/1` 为 `ACTIVE`，LID 2，SM LID 1；
- IPoIB 设备 `ibp59s0f0`：`172.16.0.61/24`；
- `rswap-client.ko` 已针对当前 6.18 内核和 OFED 成功构建、加载；
- module 参数为 `sip=172.16.0.58 sport=9400 rmsize=48`；
- `/swapfile` 已从未使用的 2 GiB 重建为 48 GiB；
- memcached 1.6.42：`~/memcached/memcached`；
- mutilate 0.1：`~/mutilate/mutilate`；
- mutilate 所需 `libzmq5`、`libnorm1t64`、`libpgm-5.3-0t64` 已安装。

当前启动的内核来自 `linux-stable` 基线 `14c1a6daf`，尚不包含工作区中未提交的
THP/mTHP 协议扩展，因此目前没有以下控制项：

```text
/sys/kernel/debug/hermit/remote_order_mask
/sys/kernel/debug/hermit/effective_order_mask
/sys/kernel/debug/hermit/order_stats
```

该环境已经可以验证原有 4 KiB RDMA backend。THP/mTHP 测试必须先安装并启动
包含协议扩展的新内核，再重新构建 rswap client，不能把当前结果标记为大页传输。

## 2. 登录和 SOCKS 代理

本机 SSH 必须显式使用用户配置，dnet-58/dnet-61 经 dnet-59 跳转：

```bash
ssh -F ~/.ssh/config -J dnet-59 dnet-58
ssh -F ~/.ssh/config -J dnet-59 dnet-61
```

dnet-61 的 `127.0.0.1:9871` 已连接两级 SOCKS。安装包时可使用：

```bash
sudo apt-get \
  -o Acquire::http::Proxy='socks5h://127.0.0.1:9871' \
  -o Acquire::https::Proxy='socks5h://127.0.0.1:9871' \
  install PACKAGE
```

## 3. dnet-58：启动 OpenSM 和 memory server

### 3.1 检查或启动 OpenSM

```bash
rdma link show mlx5_0/1
pgrep -a opensm
```

如果 OpenSM 不在运行，dnet-58 的 systemd 当前会报告
`Transport endpoint is not connected`，使用显式 daemon 命令：

```bash
sudo opensm -B \
  -g 0xe8ebd303009353a8 \
  -p 0 \
  -f /home/xwz/hermit-6/logs/opensm.log \
  --pid_file /run/opensm-hermit.pid

sudo ip address replace 172.16.0.58/24 dev ibs3f0
sudo ip link set ibs3f0 up

rdma link show mlx5_0/1
ip -br address show ibs3f0
```

预期看到 `state ACTIVE physical_state LINK_UP`。

### 3.2 构建 rswap-server

```bash
cd ~/hermit-6
make -C remoteswap/server \
  OFA_DIR=/usr/src/ofa_kernel/default

ldd remoteswap/server/rswap-server
```

必须能解析 `librdmacm.so.1` 和 `libibverbs.so.1`。

### 3.3 启动 48 GiB server

client 有 64 个 online CPU，协议为每个 CPU 创建 3 类队列，因此 server 的最后
一个参数必须是 64；成功连接时会看到 192 条队列。

```bash
mkdir -p ~/hermit-6/logs

nohup sudo sh -c '
  ulimit -l unlimited
  exec /home/xwz/hermit-6/remoteswap/server/rswap-server \
    172.16.0.58 9400 48 64
' >~/hermit-6/logs/rswap-server.log 2>&1 &

sleep 3
pgrep -a rswap-server
tail -f ~/hermit-6/logs/rswap-server.log
```

客户端连接后，dnet-58 的约 48 GiB 内存会被注册并占用，这是预期行为：

```bash
free -h
grep -E 'QUERY|REQUEST_CHUNKS|Send available Regions' \
  ~/hermit-6/logs/rswap-server.log | tail
```

## 4. dnet-61：RDMA client 准备

### 4.1 网络和内核预检

```bash
uname -r
ofed_info -s
rdma link show mlx5_0/1

sudo ip address replace 172.16.0.61/24 dev ibp59s0f0
sudo ip link set ibp59s0f0 up
ping -c 3 172.16.0.58
```

### 4.2 使用正确的 OFED 构建目录

`/usr/src/ofa_kernel/default` 仍错误指向 5.14，不能用来构建 6.18 模块。正确目录是：

```text
/usr/src/ofa_kernel-dkms/x86_64/6.18.38-hermit
```

构建命令：

```bash
cd ~/hermit-6

OFA_DIR="/usr/src/ofa_kernel-dkms/x86_64/$(uname -r)"
test -f "$OFA_DIR/Module.symvers"
test -f "$OFA_DIR/include/rdma/ib_verbs.h"

make -C remoteswap/client clean \
  KDIR="/lib/modules/$(uname -r)/build"

make -C remoteswap/client \
  BACKEND=RDMA \
  KDIR="/lib/modules/$(uname -r)/build" \
  OFA_DIR="$OFA_DIR"

modinfo remoteswap/client/rswap-client.ko |
  grep -E '^(filename|vermagic|parm):'
```

### 4.3 加载 client 并检查连接

先启动 dnet-58 server，再执行：

```bash
cd ~/hermit-6

sudo insmod remoteswap/client/rswap-client.ko \
  sip=172.16.0.58 sport=9400 rmsize=48

lsmod | grep rswap
for file in /sys/module/rswap_client/parameters/*; do
  printf '%s=' "$file"
  cat "$file"
done

sudo dmesg --color=never |
  grep -E 'All 192 rdma queues|Got .*chunks|Hermit RDMA backend registered' |
  tail
```

验收必须包含：

```text
All 192 rdma queues are prepared well
Got 6 chunks from memory server
rswap: Hermit RDMA backend registered
```

### 4.4 准备 48 GiB swap slot

先确认 swap 未被使用、磁盘至少有 48 GiB 可用：

```bash
swapon --show
free -h
df -h /
```

仅在 `USED=0` 时重建：

```bash
sudo swapoff /swapfile
sudo fallocate -l 48G /swapfile
sudo chmod 600 /swapfile
sudo mkswap -f /swapfile
sudo swapon /swapfile
swapon --show
```

## 5. dnet-61：memcached 与 mutilate 分核

RDMA NIC 位于 NUMA 0。只使用每个 core 的第一个硬件线程，布局为：

| 组件 | CPU | NUMA | 线程数 |
|---|---:|---:|---:|
| memcached | 0-7 | 0 | 8 |
| mutilate | 8-15 | 0 | 8 |
| SMT/系统与 Hermit 余量 | 32-47 | 0 | 不绑定负载 |

不要同时使用互为 SMT sibling 的 `0-15` 和 `32-47` 做两个负载。

### 5.1 基础验证

```bash
~/memcached/memcached -V
~/mutilate/mutilate --version
ldd ~/mutilate/mutilate | grep 'not found' && exit 1 || true
```

### 5.2 使用项目脚本执行完整负载

先确认 dnet-61 已启动包含异步回收修复的新内核。旧内核没有
`reclaim_headroom_pages`，不能用于 70% hard-limit 压测：

```bash
test -r /sys/kernel/debug/hermit/reclaim_headroom_pages
cat /sys/kernel/debug/hermit/reclaim_headroom_pages
cat /sys/kernel/debug/hermit/reclaim_mode
cat /sys/kernel/debug/hermit/sthd_cnt
```

脚本先在无限制 cgroup 中预装 3200 万条 Facebook ETC 分布记录，再把 cgroup
限制设置为当前占用的 70%，从而产生远端换页。memcached 和 mutilate 使用不同
核心：

```bash
cd ~/hermit-6

export MODE=cgroup-hermit
export KERNEL_TAG=hermit-6.18
export SERVER_ADDR=127.0.0.1
export PORT=11211
export MEMCACHED_BIN="$HOME/memcached/memcached"
export MUTILATE_BIN="$HOME/mutilate/mutilate"

export CORE_LAYOUT=socket
export BENCH_SOCKET=0
export BENCH_NUMA_NODE=0
export BENCH_NUMACTL=1
export MEMCACHED_CORES=0-7
export MUTILATE_CORES=8-15
export MEMCACHED_THREADS=8
export MUTILATE_THREADS=8

export RECORDS=32000000
export MEMCACHED_MEM_MB=16384
export MUTILATE_CONNECTIONS=64
export LOCAL_RATIO_PCT=70
export BENCH_REPEATS=3
export DURATION=40
export LOADS='1000 2000 5000 10000 20000 50000 75000 100000'

export STHD_CNT=4
export RECLAIM_MODE=1
export RECLAIM_HEADROOM_PAGES=65536
export BYPASS_SWAPCACHE=Y
export LAZY_POLL=N
export HERMIT_SWAPOUT_POLICY=exclusive
export RSWAP_REQUIRED_BACKEND=rdma

export RUN_ID="$(date +%Y%m%d-%H%M%S)-hermit-rdma"
export RESULT_DIR="$HOME/hermit-6/tools/rdma/results/$RUN_ID"
export PID_FILE="$RESULT_DIR/memcached.pid"

tools/rdma/memcached_load.sh \
  --mode "$MODE" --kernel "$KERNEL_TAG" \
  --result-dir "$RESULT_DIR" --port "$PORT"

tools/rdma/memcached_bench.sh \
  --mode "$MODE" --kernel "$KERNEL_TAG" \
  --result-dir "$RESULT_DIR" --port "$PORT"
```

低负载验证完成后可改为原主脚本的高负载范围：

```bash
export LOADS='500000 750000 1000000 1250000 1500000 2000000 3000000 4000000'
```

如果只想手工验证 mutilate 参数，使用：

```bash
numactl --cpunodebind=0 --membind=0 \
  taskset -c 8-15 "$HOME/mutilate/mutilate" \
    -s 127.0.0.1:11211 --noload \
    -r 32000000 -T 8 -c 64 \
    --keysize=fb_key --valuesize=fb_value --iadist=fb_ia \
    --update=0.002 -q 100000 -w 30 -t 60
```

## 6. THP/mTHP 协议测试

首先执行强制准入检查：

```bash
test -r /sys/kernel/debug/hermit/remote_order_mask
test -r /sys/kernel/debug/hermit/effective_order_mask
test -r /sys/kernel/debug/hermit/order_stats
```

任何一项失败都表示当前仍是旧的 4 KiB-only 内核，停止 THP/mTHP 对比。

### 6.1 4 KiB 基线

```bash
echo never | sudo tee /sys/kernel/mm/transparent_hugepage/enabled
echo 0x1 | sudo tee /sys/kernel/debug/hermit/remote_order_mask
cat /sys/kernel/debug/hermit/effective_order_mask
```

### 6.2 64 KiB mTHP

64 KiB 是 order 4，因此 mask 为 bit 0 加 bit 4，即 `0x11`：

```bash
for file in /sys/kernel/mm/transparent_hugepage/hugepages-*/enabled; do
  echo never | sudo tee "$file" >/dev/null
done
echo always | sudo tee \
  /sys/kernel/mm/transparent_hugepage/hugepages-64kB/enabled
echo 0x11 | sudo tee /sys/kernel/debug/hermit/remote_order_mask
```

### 6.3 2 MiB THP

2 MiB 是 order 9，因此 mask 为 bit 0 加 bit 9，即 `0x201`：

```bash
echo always | sudo tee /sys/kernel/mm/transparent_hugepage/enabled
echo always | sudo tee \
  /sys/kernel/mm/transparent_hugepage/hugepages-2048kB/enabled
echo 0x201 | sudo tee /sys/kernel/debug/hermit/remote_order_mask
```

每种配置必须重启 memcached、重新预装数据并单独使用新结果目录。测试前后保存：

```bash
cat /sys/kernel/debug/hermit/remote_order_mask
cat /sys/kernel/debug/hermit/effective_order_mask
cat /sys/kernel/debug/hermit/order_stats
grep -E '^(AnonHugePages|SwapTotal|SwapFree):' /proc/meminfo
```

不要使用 `memcached -L` 代替 THP；`-L` 请求的是显式 HugeTLB，语义不同。

## 7. 监控与验收

在 dnet-61 开三个终端：

```bash
PID="$(cat "$RESULT_DIR/memcached.pid")"
pidstat -h -r -u -w -p "$PID" 1 | tee "$RESULT_DIR/pidstat.log"
```

```bash
vmstat -w 1 | tee "$RESULT_DIR/vmstat.log"
```

```bash
sar -n DEV 1 | tee "$RESULT_DIR/network.log"
```

随时检查：

```bash
grep -E '^(pswpin|pswpout|pgmajfault) ' /proc/vmstat
cat /sys/fs/cgroup/mc/memory.current 2>/dev/null
cat /sys/fs/cgroup/mc/memory.max 2>/dev/null
cat /sys/fs/cgroup/mc/memory.swap.current 2>/dev/null
cat /sys/fs/cgroup/mc/memory.events 2>/dev/null
sudo dmesg --color=never | grep -Ei 'rswap|hermit|wc error|post.*fail' | tail
```

`memory.events` 中的 `oom_kill` 在整个 run 内必须保持不变。测试脚本会在每次
mutilate 前检查 memcached PID 和 stats 响应，并在稳定等待期间检测 `oom_kill`；
服务死亡会记录为 `server-dead` 或 `oom-killed` 后立即停止，不再误记为
`stable`。

最低验收条件：

- mutilate `Misses` 接近 0，`Skipped TXs` 可接受；
- memcached 没有意外 eviction/out-of-memory；
- `pswpout`、`pswpin` 在压力阶段增长；
- dmesg 没有 RDMA WC/post error；
- 新协议测试中 `order_stats` 对目标 order 的 store/load 或 fallback 有变化；
- 每档负载至少重复 3 次，使用中位数比较。

## 8. 停止顺序

先在 dnet-61 停止负载和 memcached：

```bash
cd ~/hermit-6
MODE=cgroup-hermit RESULT_DIR="$RESULT_DIR" PID_FILE="$RESULT_DIR/memcached.pid" \
  tools/rdma/memcached.sh stop || true
```

卸载 backend 前必须确保没有活跃远端 swap entry：

```bash
sudo swapoff /swapfile
sudo rmmod rswap_client
sudo swapon /swapfile
```

然后在 dnet-58 停 server：

```bash
sudo kill "$(pgrep -x rswap-server)"
```

如果该 IB fabric 不再需要 Subnet Manager，最后停止 OpenSM：

```bash
sudo kill "$(cat /run/opensm-hermit.pid)"
```

不要在 client 仍连接、swap 仍活跃时先杀 server 或卸载 rswap module。
