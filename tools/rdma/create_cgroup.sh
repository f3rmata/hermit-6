#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

usage() {
  cat <<'USAGE'
usage:
  create_cgroup.sh create
  create_cgroup.sh set-limit <MB|max>
  create_cgroup.sh add-pid <pid>
  create_cgroup.sh path

Environment:
  CGROUP_NAME=mc
USAGE
}

action=${1:-create}

case "$action" in
  create)
    setup_cgroup
    rdma_log "created $(cgroup_path)"
    ;;
  set-limit)
    [ $# -ge 2 ] || rdma_die "set-limit requires <MB|max>"
    setup_cgroup
    set_cgroup_limit_mb "$2"
    rdma_log "set $(cgroup_path) memory limit to $2"
    ;;
  add-pid)
    [ $# -ge 2 ] || rdma_die "add-pid requires <pid>"
    setup_cgroup
    move_pid_to_cgroup "$2"
    rdma_log "moved pid $2 to $(cgroup_path)"
    ;;
  path)
    cgroup_path
    printf '\n'
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage
    exit 2
    ;;
esac
