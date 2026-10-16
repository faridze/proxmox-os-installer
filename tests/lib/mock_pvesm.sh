#!/usr/bin/env bash
# Shared pvesm mock: supports `status` and `status --content images`
case "$1" in
  status)
    if [[ "${2:-}" == --content ]]; then
      printf '%s\n' 'Name Type Status'
      printf '%s\n' 'local-lvm lvmthin active'
      exit 0
    fi
    printf '%s\n' 'Name Type Status Total Used Available %'
    printf '%s\n' 'local-lvm lvmthin active 10000000 1000 9000000 1%'
    exit 0
    ;;
esac
exit 98
