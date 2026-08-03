#! /bin/bash


### Macros ###
mem_server_ip="${RSWAP_SERVER_IP:-10.0.0.2}"
mem_server_port="${RSWAP_SERVER_PORT:-9400}"

if [ -z "${HOME}" ]; then
	echo "set home_dir first."
	exit 1
else
	home_dir=${HOME}
fi

swap_file="${RSWAP_SWAP_FILE:-${home_dir}/swapfile}"
swap_dev="${RSWAP_SWAP_DEV:-}"
# The swap target may be a block device (raw partition or loop device) or a
# regular swap file.  The kernel only allocates order>0 (larger than 4 KiB)
# swap slots when SWP_BLKDEV is set, which requires a block device; a swap
# file is always broken down to 4 KiB by the swap subsystem.
swap_target="${swap_dev:-${swap_file}}"
# The swap file/partition size should be equal to the whole size of remote memory
SWAP_PARTITION_SIZE_GB="${RSWAP_MEM_GB:-48}"

swap_is_block() {
	# An explicit RSWAP_SWAP_DEV always means block device; otherwise test the node.
	[ -n "${swap_dev}" ] || [ -b "${swap_target}" ]
}

swap_size_gb() {
	# rmsize is in GB.  Prefer RSWAP_MEM_GB when set; for a block device fall
	# back to the device size, so a loop or partition needs no extra variable.
	if swap_is_block; then
		if [ -n "${RSWAP_MEM_GB:-}" ]; then
			printf '%s' "${RSWAP_MEM_GB}"
		else
			local bytes
			bytes=$(sudo blockdev --getsize64 "${swap_target}") || return 1
			printf '%d' $((bytes / 1024 / 1024 / 1024))
		fi
	else
		printf '%s' "${SWAP_PARTITION_SIZE_GB}"
	fi
}

ensure_backing_file() {
	# Create the swap file if it does not exist yet (loop action backing).
	if [[ -e "${swap_file}" ]]; then
		echo "Reusing existing backing file ${swap_file}"
	else
		echo "Create backing file ${swap_file} with size ${SWAP_PARTITION_SIZE_GB}G"
		sudo fallocate -l ${SWAP_PARTITION_SIZE_GB}G "${swap_file}"
		sudo chmod 600 "${swap_file}"
	fi
}

echo " !! Warning, check the parameters below : "
echo " Assigned memory server IP ${mem_server_ip} Port ${mem_server_port}"
echo " swap target ${swap_target}, size ${SWAP_PARTITION_SIZE_GB} GB"
echo " "
echo " "

### Action ###
action=$1
if [[ -z "${action}" ]]; then
	echo "This shellscipt for Infiniswap pre-configuration."
	echo "Run it with sudo or root"
	echo ""
	echo "Please select what to do: [install | replace | uninstall | create_swap | loop]"

	read action
fi

function close_swap_partition() {
	# /proc/swaps escapes spaces as \040; decode for a literal match and for
	# swapoff.  Only ever close the requested target.
	swap_bd=$(awk -v target="${swap_target}" \
		'NR > 1 { path = $1; gsub(/\\040/, " ", path);
			  if (path == target) { print path; exit } }' /proc/swaps)

	if [[ -z "${swap_bd}" ]]; then
		echo "Nothing to close."
	else
		echo "Swap Partition to close :${swap_bd} "
		if ! sudo swapoff "${swap_bd}"; then
			echo "Failed to swapoff ${swap_bd}; aborting."
			return 1
		fi
	fi

	# Check
	echo "Current swap partition:"
	swapon -s
}

function create_swap_file() {
	if swap_is_block; then
		if [ ! -b "${swap_target}" ]; then
			echo "Block device ${swap_target} does not exist."
			return 1
		fi
		sleep 1
		echo "Prepare ${swap_target} (block device) as swap device"
		sudo mkswap -f "${swap_target}"
		sudo swapon "${swap_target}"
		swapon -s
		return 0
	fi

	expected_size_bytes=$((SWAP_PARTITION_SIZE_GB * 1024 * 1024 * 1024))
	if [[ -e ${swap_file} ]]; then
		cur_size_bytes=$(stat -c %s "${swap_file}")
		if [[ ${cur_size_bytes} -ne "${expected_size_bytes}" ]]; then
			echo "Current ${swap_file}: ${cur_size_bytes} bytes, expected ${expected_size_bytes} bytes"
			if [[ "${RSWAP_RECREATE_SWAP:-0}" != "1" ]]; then
				echo "Refusing to recreate an existing swapfile. Set RSWAP_RECREATE_SWAP=1 after verifying the target."
				return 1
			fi
			echo "Recreating ${swap_file} after explicit RSWAP_RECREATE_SWAP=1"
			sudo rm -- "${swap_file}"

			echo "Create a file, ~/swapfile, with size ${SWAP_PARTITION_SIZE_GB}G as swap device."
			sudo fallocate -l ${SWAP_PARTITION_SIZE_GB}G "${swap_file}"
			sudo chmod 600 "${swap_file}"
		else
			echo "Existing swapfile ${swap_file} has the expected ${SWAP_PARTITION_SIZE_GB} GiB size. Reuse it."
		fi
	else
		# does not exist, create a swapfile
		echo "Create a file, ~/swapfile, with size ${SWAP_PARTITION_SIZE_GB}G as swap device."
		sudo fallocate -l ${SWAP_PARTITION_SIZE_GB}G "${swap_file}"
		sudo chmod 600 "${swap_file}"
		du -sh ${swap_file}
	fi

	sleep 1
	echo "Mount the ${swap_file} as swap device"
	sudo mkswap -f "${swap_file}"
	sudo swapon "${swap_file}"

	# Check
	swapon -s
}

if [[ "${action}" = "install" ]]; then
	echo "Close current swap partition && Create swap file"
	close_swap_partition || exit 1

	create_swap_file || exit 1

	rmsize=$(swap_size_gb) || exit 1
	echo "insmod ./rswap-client.ko sip=${mem_server_ip} sport=${mem_server_port} rmsize=${rmsize}"
	sudo insmod ./rswap-client.ko sip=${mem_server_ip} sport=${mem_server_port} rmsize=${rmsize}

elif [[ "${action}" = "replace" ]]; then
	echo "rmmod rswap-client"
	sudo rmmod rswap-client
	echo "Please restart rswap-server on mem server. Press <Enter> to continue..."

	read
	rmsize=$(swap_size_gb) || exit 1
	echo "insmod ./rswap-client.ko sip=${mem_server_ip} sport=${mem_server_port} rmsize=${rmsize}"
	sudo insmod ./rswap-client.ko sip=${mem_server_ip} sport=${mem_server_port} rmsize=${rmsize}

elif [[ "${action}" = "uninstall" ]]; then
	echo "Close current swap partition"
	close_swap_partition || exit 1

	echo "rmmod rswap-client"
	sudo rmmod rswap-client

	if swap_is_block && [[ "${swap_target}" == /dev/loop* ]]; then
		echo "Detaching loop device ${swap_target}"
		sudo losetup -d "${swap_target}"
	fi

elif [[ "${action}" = "create_swap" ]]; then
	echo "Check the existing swap target"
	close_swap_partition || exit 1

	echo "Create swap"
	create_swap_file || exit 1

elif [[ "${action}" = "loop" ]]; then
	# Bind the swap file to a free loop device so the kernel sees a block
	# device (SWP_BLKDEV) and enables order>0 (larger than 4 KiB) swap
	# allocations.  Then run "install" with RSWAP_SWAP_DEV=<device>.
	if [ -n "${swap_dev}" ]; then
		echo "RSWAP_SWAP_DEV is set; the loop action only applies to a swap file."
		exit 1
	fi
	ensure_backing_file || exit 1
	loop_dev=$(sudo losetup -f) || exit 1
	sudo losetup "${loop_dev}" "${swap_file}" || exit 1
	echo "Bound ${swap_file} to ${loop_dev}"
	echo "Install with:"
	echo "  RSWAP_SWAP_DEV=${loop_dev} $0 install"
	echo "Set RSWAP_MEM_GB to match the device size, or leave it unset to derive"
	echo "the size from blockdev --getsize64.  Tear down with:"
	echo "  RSWAP_SWAP_DEV=${loop_dev} $0 uninstall"

else
	echo "!! Wrong choice : ${action}"
fi
