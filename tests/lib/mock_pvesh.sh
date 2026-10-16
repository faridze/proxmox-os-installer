#!/usr/bin/env bash
# Mock pvesh for installer tests (cluster resources + nextid).
case "$1" in
  get)
    case "$2" in
      /cluster/resources*) printf '[]\n'; exit 0 ;;
      /cluster/nextid*) printf '"152"\n'; exit 0 ;;
    esac
    ;;
esac
printf '"200"\n'
