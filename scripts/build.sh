#!/usr/bin/env bash
set -Eeuo pipefail

# Ubuntu/Linux build entry point. No privileged commands or cleanup are run here.
# Optional: OPENWRT_ARCHIVE=/path/to/pinned.tar.gz and OPENWRT_ARCHIVE_SHA256=...
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source_commit=5700101d7c85381889d890dc1d3d001d0b4b6446
source_date_epoch=1793308608 # Upstream commit time: 2026-10-29 21:16:48 UTC.
work_dir=${WORK_DIR:-"$project_dir/work"}
output_dir=${OUTPUT_DIR:-"$project_dir/artifacts"}
source_dir="$work_dir/openwrt"
jobs=${JOBS:-2}
mode=${1:-all}

if [[ $(uname -s) != Linux ]]; then
  echo 'Run this script inside Ubuntu/Linux, not a Windows shell.' >&2
  exit 2
fi
if [[ $(id -u) -eq 0 ]]; then
  echo 'Build OpenWrt as a normal Linux user, not root.' >&2
  exit 2
fi
if [[ ! $jobs =~ ^[1-9][0-9]*$ ]]; then
  echo 'JOBS must be a positive integer.' >&2
  exit 2
fi
case "$mode" in all|prepare|download|compile|collect) ;; *) echo 'Usage: bash scripts/build.sh [all|prepare|download|compile|collect]' >&2; exit 2 ;; esac
for program in git make gcc g++ python3 gawk flex bison rsync unzip file wget zstd tar sha256sum; do
  command -v "$program" >/dev/null || { echo "Missing prerequisite: $program" >&2; exit 2; }
done
mkdir -p -- "$work_dir" "$output_dir/logs" "$output_dir/firmware"
work_dir=$(cd -- "$work_dir" && pwd)
output_dir=$(cd -- "$output_dir" && pwd)
source_dir="$work_dir/openwrt"

collect_diagnostics() {
  local status=$?
  trap - EXIT
  python3 "$project_dir/scripts/collect-artifacts.py" "$source_dir" "$output_dir" "$source_commit" --diagnostics-only || true
  exit "$status"
}
trap collect_diagnostics EXIT

if [[ $mode == all || $mode == prepare ]]; then
  if [[ -n ${OPENWRT_ARCHIVE:-} ]]; then
    if [[ ! ${OPENWRT_ARCHIVE_SHA256:-} =~ ^[a-fA-F0-9]{64}$ ]]; then
      echo 'OPENWRT_ARCHIVE requires its verified OPENWRT_ARCHIVE_SHA256.' >&2
      exit 2
    fi
    printf '%s  %s\n' "$OPENWRT_ARCHIVE_SHA256" "$OPENWRT_ARCHIVE" | sha256sum -c -
    if [[ ! -d $source_dir ]]; then
      mkdir -- "$source_dir"
      tar -xzf "$OPENWRT_ARCHIVE" -C "$source_dir" --strip-components=1
      printf '%s\n%s\n' "$source_commit" "$OPENWRT_ARCHIVE_SHA256" > "$source_dir/.hc5962-source-provenance"
    fi
    [[ -f $source_dir/.hc5962-source-provenance ]] || { echo 'Source directory already exists but was not extracted by this script.' >&2; exit 2; }
    expected_provenance=$(printf '%s\n%s' "$source_commit" "$OPENWRT_ARCHIVE_SHA256")
    [[ $(cat "$source_dir/.hc5962-source-provenance") == "$expected_provenance" ]] || { echo 'Existing source provenance differs. Select a new WORK_DIR.' >&2; exit 2; }
  else
    if [[ ! -d $source_dir ]]; then
      mkdir -- "$source_dir"
      git -C "$source_dir" init
      git -C "$source_dir" remote add origin https://github.com/openwrt/openwrt.git
      git -C "$source_dir" fetch --depth=1 origin "$source_commit"
      git -C "$source_dir" checkout --detach FETCH_HEAD
    fi
    [[ $(git -C "$source_dir" rev-parse HEAD) == "$source_commit" ]] || { echo 'Existing source differs from the pinned commit. Select a new WORK_DIR.' >&2; exit 2; }
  fi
  # Official version helpers honor these files for archive and shallow clones.
  printf 'snapshot-%s\n' "${source_commit:0:12}" > "$source_dir/version"
  printf '%s\n' "$source_date_epoch" > "$source_dir/version.date"
  python3 "$project_dir/scripts/apply-device.py" "$source_dir"
  cp -- "$project_dir/config/feeds.conf" "$source_dir/feeds.conf.default"
  cd -- "$source_dir"
  ./scripts/feeds update -a 2>&1 | tee "$output_dir/logs/feeds-update.log"
  ./scripts/feeds install -a 2>&1 | tee "$output_dir/logs/feeds-install.log"
  cp -- "$project_dir/config/hc5962-spi-32m.config" .config
  make defconfig 2>&1 | tee "$output_dir/logs/defconfig.log"
  for selection in CONFIG_TARGET_ramips_mt7621_DEVICE_hiwifi_hc5962-spi CONFIG_PACKAGE_kmod-mt7603 CONFIG_PACKAGE_kmod-mt76x2 CONFIG_PACKAGE_luci-ssl CONFIG_PACKAGE_luci-i18n-base-zh-cn; do
    grep -qx "${selection}=y" .config || { echo "Required selection was not retained by make defconfig: $selection" >&2; exit 1; }
  done
  python3 "$project_dir/scripts/collect-artifacts.py" "$source_dir" "$output_dir" "$source_commit" --diagnostics-only
fi

if [[ $mode == all || $mode == download ]]; then
  cd -- "$source_dir"
  make download -j8 V=s 2>&1 | tee "$output_dir/logs/download.log"
fi

if [[ $mode == all || $mode == compile ]]; then
  cd -- "$source_dir"
  if ! make -j"$jobs" V=s 2>&1 | tee "$output_dir/logs/build-parallel.log"; then
    echo 'Parallel build failed; retrying once with a single job to obtain a precise error.'
    make -j1 V=s 2>&1 | tee "$output_dir/logs/build-single.log"
  fi
fi

if [[ $mode == all || $mode == compile || $mode == collect ]]; then
  python3 "$project_dir/scripts/collect-artifacts.py" "$source_dir" "$output_dir" "$source_commit"
  echo "Firmware and checksums: $output_dir/firmware"
fi
