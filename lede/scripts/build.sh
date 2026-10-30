#!/usr/bin/env bash
set -Eeuo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source_commit=8867669e23613233dde23cad3efe9d1f5de2cec8
source_date_epoch=1793154452
source_url=https://github.com/coolsnowwolf/lede.git
work_dir=${WORK_DIR:-"$project_dir/work"}
output_dir=${OUTPUT_DIR:-"$project_dir/artifacts"}
jobs=${JOBS:-2}
wifi_driver=${WIFI_DRIVER:-mt76}
mode=${1:-all}

[[ $(uname -s) == Linux ]] || { echo 'This build entry point runs on the GitHub Linux runner.' >&2; exit 2; }
[[ $(id -u) -ne 0 ]] || { echo 'Build LEDE as a normal Linux user.' >&2; exit 2; }
[[ $jobs =~ ^[1-9][0-9]*$ ]] || { echo 'JOBS must be a positive integer.' >&2; exit 2; }
case "$wifi_driver" in vendor|mt76) ;; *) echo 'WIFI_DRIVER must be vendor or mt76.' >&2; exit 2 ;; esac
case "$mode" in all|prepare|download|compile|collect) ;; *) echo 'Usage: bash lede/scripts/build.sh [all|prepare|download|compile|collect]' >&2; exit 2 ;; esac
for program in git make gcc g++ python3 gawk flex bison rsync unzip file wget zstd tar sha256sum; do
  command -v "$program" >/dev/null || { echo "Missing prerequisite: $program" >&2; exit 2; }
done
mkdir -p -- "$work_dir" "$output_dir/logs" "$output_dir/firmware"
work_dir=$(cd -- "$work_dir" && pwd)
output_dir=$(cd -- "$output_dir" && pwd)
source_dir="$work_dir/lede-$wifi_driver"
export SOURCE_DATE_EPOCH="$source_date_epoch"
firmware_collected=0

collect_diagnostics() {
  local status=$?
  trap - EXIT
  if [[ $status -eq 0 && $firmware_collected -eq 1 ]]; then
    exit 0
  fi
  python3 "$project_dir/scripts/collect-artifacts.py" "$source_dir" "$output_dir" "$source_commit" --wifi-driver "$wifi_driver" --diagnostics-only || true
  exit "$status"
}
trap collect_diagnostics EXIT

if [[ $mode == all || $mode == prepare ]]; then
  if [[ ! -d $source_dir ]]; then
    mkdir -- "$source_dir"
    git -C "$source_dir" init
    git -C "$source_dir" remote add origin "$source_url"
    git -C "$source_dir" fetch --depth=1 origin "$source_commit"
    git -C "$source_dir" checkout --detach FETCH_HEAD
  fi
  [[ $(git -C "$source_dir" rev-parse HEAD) == "$source_commit" ]] || { echo 'Existing checkout differs from the pinned source. Select a new WORK_DIR.' >&2; exit 2; }
  printf 'lede-%s\n' "${source_commit:0:12}" > "$source_dir/version"
  printf '%s\n' "$source_date_epoch" > "$source_dir/version.date"
  if [[ $wifi_driver == mt76 ]]; then
    for compatibility_patch in "$project_dir"/patches/kernel-6.18/*.patch; do
      if git -C "$source_dir" apply --check -- "$compatibility_patch"; then
        git -C "$source_dir" apply -- "$compatibility_patch"
      elif git -C "$source_dir" apply --reverse --check -- "$compatibility_patch"; then
        echo "Compatibility patch already applied: ${compatibility_patch##*/}"
      else
        echo "Compatibility patch does not match pinned sources: $compatibility_patch" >&2
        exit 1
      fi
    done
  fi
  python3 "$project_dir/scripts/apply-device.py" "$source_dir" --wifi-driver "$wifi_driver"
  mkdir -p -- "$source_dir/files"
  cp -a -- "$project_dir/files/rootfs/." "$source_dir/files/"
  chmod 0755 "$source_dir/files/etc/uci-defaults/99-hc5962-sdk-hnat"
  cd -- "$source_dir"
  ./scripts/feeds uninstall -a
  cp -- "$project_dir/config/feeds.conf" feeds.conf.default
  ./scripts/feeds update packages luci 2>&1 | tee "$output_dir/logs/feeds-update.log"
  ./scripts/feeds install -p luci luci luci-ssl-openssl luci-compat 2>&1 | tee "$output_dir/logs/feeds-install.log"
  ./scripts/feeds install -p packages htop usbutils 2>&1 | tee -a "$output_dir/logs/feeds-install.log"
  cp -- "$project_dir/config/hc5962-spi-32m-$wifi_driver.config" .config
  make defconfig 2>&1 | tee "$output_dir/logs/defconfig.log"
  if grep -q 'error: recursive dependency detected' "$output_dir/logs/defconfig.log"; then
    echo 'Kconfig contains a recursive dependency; see defconfig.log.' >&2
    exit 1
  fi
  required=(
    CONFIG_TARGET_ramips_mt7621_DEVICE_hiwifi_hc5962-spi
    CONFIG_PACKAGE_kmod-mtk-hnat
    CONFIG_PACKAGE_kmod-ipt-nat CONFIG_PACKAGE_firewall4 CONFIG_PACKAGE_kmod-nft-offload
    CONFIG_PACKAGE_luci CONFIG_PACKAGE_luci-ssl-openssl CONFIG_PACKAGE_luci-compat
    CONFIG_PACKAGE_libustream-openssl
    CONFIG_LUCI_LANG_zh_Hans CONFIG_PACKAGE_luci-i18n-base-zh-cn
    CONFIG_PACKAGE_dropbear CONFIG_PACKAGE_htop
  )
  if [[ $wifi_driver == vendor ]]; then
    required+=(CONFIG_PACKAGE_kmod-mt7603e CONFIG_PACKAGE_kmod-mt76x2e CONFIG_PACKAGE_mt_wifi CONFIG_MTK_CHIP_MT7603E_MT7612E)
    forbidden=(CONFIG_PACKAGE_kmod-mt7603 CONFIG_PACKAGE_kmod-mt76x2 CONFIG_TESTING_KERNEL)
  else
    required+=(CONFIG_PACKAGE_kmod-mt7603 CONFIG_PACKAGE_kmod-mt76x2 CONFIG_PACKAGE_wpad-openssl CONFIG_TESTING_KERNEL CONFIG_LINUX_6_18)
    forbidden=(CONFIG_PACKAGE_kmod-mt7603e CONFIG_PACKAGE_kmod-mt76x2e CONFIG_PACKAGE_mt_wifi)
  fi
  for selection in "${required[@]}"; do
    grep -qx "${selection}=y" .config || { echo "Required selection was not retained by make defconfig: $selection" >&2; exit 1; }
  done
  # firewall4 requires kmod-nft-offload. Having the module installed does not
  # enable a flowtable; the image's UCI defaults disable both flow-offload flags.
  forbidden+=(CONFIG_PACKAGE_kmod-ipt-offload CONFIG_PACKAGE_kmod-fast-classifier CONFIG_PACKAGE_kmod-shortcut-fe CONFIG_PACKAGE_kmod-shortcut-fe-cm CONFIG_PACKAGE_libustream-mbedtls CONFIG_PACKAGE_libustream-wolfssl)
  for selection in "${forbidden[@]}"; do
    if grep -qx "${selection}=y" .config; then
      echo "Unexpected selection conflicts with this SDK HWNAT build: $selection" >&2
      exit 1
    fi
  done
  if [[ $wifi_driver == mt76 ]]; then
    grep -q '^KERNEL_TESTING_PATCHVER:=6\.18$' target/linux/ramips/Makefile || { echo 'Expected the Linux 6.18 testing target for current mt76.' >&2; exit 1; }
  else
    grep -q '^KERNEL_PATCHVER:=5\.10$' target/linux/ramips/Makefile || { echo 'Expected the default Linux 5.10 target for vendor WiFi.' >&2; exit 1; }
  fi
  python3 "$project_dir/scripts/collect-artifacts.py" "$source_dir" "$output_dir" "$source_commit" --wifi-driver "$wifi_driver" --diagnostics-only
fi

[[ -d $source_dir/.git && $(git -C "$source_dir" rev-parse HEAD) == "$source_commit" ]] || { echo 'Prepare the pinned source before building or collecting.' >&2; exit 2; }
if [[ $mode == all || $mode == download ]]; then
  cd -- "$source_dir"
  make download -j8 V=s 2>&1 | tee "$output_dir/logs/download.log"
fi
if [[ $mode == all || $mode == compile ]]; then
  cd -- "$source_dir"
  if ! make -j"$jobs" V=s 2>&1 | tee "$output_dir/logs/build-parallel.log"; then
    echo 'Parallel build failed; retrying with one job for a precise diagnostic.'
    make -j1 V=s 2>&1 | tee "$output_dir/logs/build-single.log"
  fi
fi
if [[ $mode == all || $mode == compile || $mode == collect ]]; then
  python3 "$project_dir/scripts/collect-artifacts.py" "$source_dir" "$output_dir" "$source_commit" --wifi-driver "$wifi_driver"
  firmware_collected=1
  echo "LEDE firmware and checksums: $output_dir/firmware"
fi
