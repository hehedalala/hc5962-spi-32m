#!/usr/bin/env python3
"""Collect HC5962 SPI LEDE firmware, package evidence and failure diagnostics."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument("source", type=Path)
parser.add_argument("output", type=Path)
parser.add_argument("source_commit")
parser.add_argument("--wifi-driver", choices=("vendor", "mt76"), required=True)
parser.add_argument("--diagnostics-only", action="store_true")
args = parser.parse_args()
src = args.source.resolve()
out = args.output.resolve()
logs = out / "logs"
firmware = out / "firmware"
logs.mkdir(parents=True, exist_ok=True)
firmware.mkdir(parents=True, exist_ok=True)


def git_head(directory: Path) -> str | None:
    if not (directory / ".git").exists():
        return None
    result = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def read_if_present(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def recipe_assignments(content: str) -> dict[str, str]:
    return dict(re.findall(
        r"^(PKG_(?:VERSION|RELEASE|SOURCE_DATE|SOURCE_VERSION|MIRROR_HASH))\s*:?=\s*(\S+)",
        content, re.MULTILINE,
    ))


def package_recipe(path: str) -> dict[str, str]:
    content = read_if_present(src / path)
    if path != "package/kernel/mt76/Makefile":
        return recipe_assignments(content)
    # LEDE pins a different mt76 revision for each kernel family. Evaluate this
    # known source-selection block instead of letting the last assignment win.
    selection = re.search(r"^ifdef CONFIG_LINUX_5_4\n.*?^endif\s*$", content, re.MULTILINE | re.DOTALL)
    if not selection:
        return recipe_assignments(content)
    branches = selection[0]
    selected = ""
    for match in re.finditer(
        r"^(?:ifdef|else ifdef) (CONFIG_LINUX_[0-9_]+)\n(.*?)(?=^else|^endif)",
        branches, re.MULTILINE | re.DOTALL,
    ):
        if match[1] in enabled_config:
            selected = match[2]
            break
    if not selected:
        fallback = re.search(r"^else\n(.*?)^endif", branches, re.MULTILINE | re.DOTALL)
        selected = fallback[1] if fallback else ""
    common = content[:selection.start()] + content[selection.end():]
    return recipe_assignments(common + "\n" + selected)


recipes = {
    "mt76": "package/kernel/mt76/Makefile",
    "mt7603e": "package/lean/mt/drivers/mt7603e/Makefile",
    "mt7612e": "package/lean/mt/drivers/mt7612e/Makefile",
}
full_config = read_if_present(src / ".config")
enabled_config = set(re.findall(r"^(CONFIG_[A-Za-z0-9_-]+)=y$", full_config, re.MULTILINE))
kernel_testing = "CONFIG_TESTING_KERNEL" in enabled_config
kernel_series = "6.18" if "CONFIG_LINUX_6_18" in enabled_config else "5.10"
kernel_recipe_path = src / f"include/kernel-{kernel_series}"
kernel_recipe = read_if_present(kernel_recipe_path)
kernel_match = re.search(r"^LINUX_VERSION-" + re.escape(kernel_series) + r"\s*=\s*(\S+)", kernel_recipe, re.MULTILINE)
metadata = {
    "distribution": "Lean LEDE",
    "source_repository": "https://github.com/coolsnowwolf/lede",
    "source_commit_requested": args.source_commit,
    "source_commit_git": git_head(src),
    "compatibility_patches": [
        {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in sorted((Path(__file__).resolve().parents[1] / "patches/kernel-6.18").glob("*.patch"))
        if args.wifi_driver == "mt76"
    ],
    "kernel_series": kernel_series,
    "kernel_version": kernel_series + kernel_match[1] if kernel_match else None,
    "kernel_testing": kernel_testing,
    "device": "hiwifi,hc5962-spi",
    "flash_bytes": 32 * 1024 * 1024,
    "firmware_offset": "0x50000",
    "factory_offset": "0x40000",
    "image_limit_bytes": 32448 * 1024,
    "wifi_driver": args.wifi_driver,
    "wifi_driver_recipes": {name: package_recipe(path) for name, path in recipes.items()},
    "hardware_acceleration": {
        "implementation": "MediaTek SDK HWNAT",
        "package": "kmod-mtk-hnat",
        "module": "mtkhnat",
        "included_in_firmware": None,
        "runtime_verified": False,
    },
    "wifi_security": {
        "five_ghz": "WPA2-AES; this MT7612 vendor driver has no SAE implementation" if args.wifi_driver == "vendor" else "WPA3-Personal capable through mt76 and wpad-openssl; requires compatible clients and configuration",
        "two_ghz_wpa3_verified": False,
    },
    "feeds": {
        entry.name: git_head(entry)
        for entry in sorted((src / "feeds").glob("*"))
        if entry.is_dir() and (entry / ".git").exists()
    },
    "collected_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "hardware_tested": False,
    "layout_note": "This LEDE build has not been boot-tested. It uses the HC5962 SPI 32MiB layout, not the original NAND image format.",
}
(logs / "build-info.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
for source, name in [
    (src / ".config", "full-config.txt"),
    (src / "feeds.conf.default", "feeds.conf"),
    (kernel_recipe_path, "kernel-version.txt"),
    *[(src / path, f"{name}-Makefile.txt") for name, path in recipes.items()],
]:
    if source.is_file():
        shutil.copy2(source, logs / name)
for source in (src / "logs").glob("**/log.txt"):
    destination = logs / "package-logs" / source.relative_to(src / "logs")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
if args.diagnostics_only:
    raise SystemExit(0)

if metadata["source_commit_git"] != args.source_commit:
    raise SystemExit("The checkout does not match the requested LEDE source commit.")
if args.wifi_driver == "mt76" and (kernel_series != "6.18" or not kernel_testing):
    raise SystemExit("The mt76 build must select the Linux 6.18 testing target.")
if args.wifi_driver == "vendor" and (kernel_series != "5.10" or kernel_testing):
    raise SystemExit("The vendor WiFi build must select the default Linux 5.10 target.")
target = src / "bin/targets/ramips/mt7621"
images = sorted(target.glob("*hiwifi_hc5962-spi*sysupgrade.bin"))
if len(images) != 1:
    raise SystemExit(f"Expected one SPI sysupgrade image, found {len(images)} in {target}")
image = images[0]
if not 0 < image.stat().st_size <= 32448 * 1024:
    raise SystemExit("The sysupgrade image exceeds the SPI firmware partition.")
with image.open("rb") as stream:
    if stream.read(4) != bytes.fromhex("27051956"):
        raise SystemExit("Unexpected image header: the SPI target requires a uImage.")
manifests = sorted(target.glob("*hiwifi_hc5962-spi*.manifest"))
if len(manifests) != 1:
    raise SystemExit("Expected one device package manifest to verify HWNAT and wireless drivers.")
installed = {line.split(" - ", 1)[0] for line in read_if_present(manifests[0]).splitlines() if line}
required_packages = {"kmod-mtk-hnat", "dropbear", "htop"}
required_packages.update({"kmod-mt7603e", "kmod-mt76x2e", "mt_wifi"} if args.wifi_driver == "vendor" else {"kmod-mt7603", "kmod-mt76x2", "wpad-openssl"})
if missing := required_packages - installed:
    raise SystemExit("Missing required firmware packages: " + ", ".join(sorted(missing)))
metadata["manifest_verified_packages"] = sorted(required_packages)
metadata["hardware_acceleration"]["included_in_firmware"] = True
rootfs_candidates = [
    *src.glob("build_dir/target-*/root-ramips"),
    *src.glob("build_dir/target-*/linux-*/root-ramips"),
    *src.glob("staging_dir/target-*/root-ramips"),
]
rootfs_candidates = sorted({path for path in rootfs_candidates if path.is_dir()})
module_files = [
    path
    for rootfs in rootfs_candidates
    for path in rootfs.glob("lib/modules/*/mtkhnat.ko*")
    if path.is_file() and path.stat().st_size > 0
]
if rootfs_candidates and not module_files:
    raise SystemExit("The staged root filesystem is missing the mtkhnat kernel module.")
metadata["hardware_acceleration"]["staged_module_files"] = [str(path.relative_to(src)) for path in module_files]
metadata["hardware_acceleration"]["staged_module_checked"] = bool(rootfs_candidates)
(logs / "build-info.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

deliverables = images + sorted(target.glob("*hiwifi_hc5962-spi*initramfs*.bin")) + manifests
deliverables += [
    path for path in [target / "config.buildinfo", target / "feeds.buildinfo", target / "version.buildinfo", target / "profiles.json"]
    if path.is_file()
]
for source in deliverables:
    shutil.copy2(source, firmware / source.name)
for name in ("build-info.json", "full-config.txt"):
    shutil.copy2(logs / name, firmware / name)
checksums = [
    f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
    for path in sorted(firmware.iterdir())
    if path.is_file() and path.name != "SHA256SUMS"
]
(firmware / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="ascii")
print(f"Verified {image.name}: {image.stat().st_size} bytes, SDK HWNAT and {args.wifi_driver} packages present.")
