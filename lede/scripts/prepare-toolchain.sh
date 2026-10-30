#!/usr/bin/env bash
set -Eeuo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source_commit=8867669e23613233dde23cad3efe9d1f5de2cec8
work_dir=${WORK_DIR:-"$project_dir/work"}
output_dir=${OUTPUT_DIR:-"$project_dir/artifacts"}
wifi_driver=${WIFI_DRIVER:-mt76}
jobs=${JOBS:-2}

[[ $(uname -s) == Linux ]] || { echo 'Toolchain preparation runs on the GitHub Linux runner.' >&2; exit 2; }
case "$wifi_driver" in mt76|vendor) ;; *) echo 'WIFI_DRIVER must be mt76 or vendor.' >&2; exit 2 ;; esac
[[ $jobs =~ ^[1-9][0-9]*$ ]] || { echo 'JOBS must be a positive integer.' >&2; exit 2; }
source_dir="$work_dir/lede-$wifi_driver"
[[ -d $source_dir/.git && -f $source_dir/.config ]] || { echo 'Configure the source before restoring or preparing the toolchain.' >&2; exit 2; }
[[ $(git -C "$source_dir" rev-parse HEAD) == "$source_commit" ]] || { echo 'Toolchain source differs from the pinned LEDE commit.' >&2; exit 2; }
mkdir -p -- "$output_dir/logs"
output_dir=$(cd -- "$output_dir" && pwd)
cd -- "$source_dir"
export SOURCE_DATE_EPOCH=1793154452

# Always run make even after an exact cache hit: its stamps and dependency
# checks decide what is reusable, rather than skipping a required build step.
if ! make -j"$jobs" tools/install V=s 2>&1 | tee "$output_dir/logs/tools-install.log"; then
  make -j1 tools/install V=s 2>&1 | tee "$output_dir/logs/tools-install-single.log"
fi
if ! make -j"$jobs" toolchain/install V=s 2>&1 | tee "$output_dir/logs/toolchain-install.log"; then
  make -j1 toolchain/install V=s 2>&1 | tee "$output_dir/logs/toolchain-install-single.log"
fi
