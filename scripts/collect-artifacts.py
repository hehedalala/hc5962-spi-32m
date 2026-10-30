#!/usr/bin/env python3
"""Collect firmware and build provenance; diagnostics also work after a failed build."""
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
    result = subprocess.run(["git", "-C", str(directory), "rev-parse", "HEAD"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None

def read_if_present(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""

mt76 = read_if_present(src / "package/kernel/mt76/Makefile")
driver = dict(re.findall(r"^(PKG_SOURCE_(?:DATE|VERSION)|PKG_MIRROR_HASH)\s*:?=\s*(\S+)", mt76, re.MULTILINE))
metadata = {
    "device": "hiwifi,hc5962-spi",
    "flash_bytes": 32 * 1024 * 1024,
    "firmware_offset": "0x50000",
    "factory_offset": "0x40000",
    "image_limit_bytes": 32448 * 1024,
    "source_commit_requested": args.source_commit,
    "source_commit_git": git_head(src),
    "source_archive_provenance": read_if_present(src / ".hc5962-source-provenance").splitlines(),
    "mt76": driver,
    "feeds": {entry.name: git_head(entry) for entry in sorted((src / "feeds").glob("*")) if entry.is_dir() and (entry / ".git").exists()},
    "collected_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "hardware_tested": False,
    "layout_note": "SPI layout must match the existing Breed flash layout and factory calibration data before flashing.",
}
(logs / "build-info.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
for source, name in [(src / ".config", "full-config.txt"), (src / "feeds.conf.default", "feeds.conf"), (src / "package/kernel/mt76/Makefile", "mt76-Makefile.txt")]:
    if source.is_file():
        shutil.copy2(source, logs / name)
for source in (src / "logs").glob("**/log.txt"):
    destination = logs / "package-logs" / source.relative_to(src / "logs")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
if args.diagnostics_only:
    raise SystemExit(0)

target = src / "bin/targets/ramips/mt7621"
images = sorted(target.glob("*hiwifi_hc5962-spi*sysupgrade.bin"))
if len(images) != 1:
    raise SystemExit(f"Expected exactly one SPI sysupgrade image, found {len(images)} in {target}")
image = images[0]
if not 0 < image.stat().st_size <= 32448 * 1024:
    raise SystemExit("The sysupgrade image exceeds the SPI firmware partition.")
if image.read_bytes()[:4] != bytes.fromhex("27051956"):
    raise SystemExit("Unexpected sysupgrade header: expected a uImage for this SPI target.")
deliverables = images + sorted(target.glob("*hiwifi_hc5962-spi*initramfs*.bin"))
deliverables += [path for path in [target / "config.buildinfo", target / "feeds.buildinfo", target / "version.buildinfo", target / "profiles.json"] if path.is_file()]
deliverables += sorted(target.glob("*hiwifi_hc5962-spi*.manifest"))
for source in deliverables:
    shutil.copy2(source, firmware / source.name)
shutil.copy2(logs / "build-info.json", firmware / "build-info.json")
shutil.copy2(logs / "full-config.txt", firmware / "full-config.txt")
checksums = []
for path in sorted(firmware.iterdir()):
    if path.is_file() and path.name != "SHA256SUMS":
        checksums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
(firmware / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="ascii")
print(f"Verified {image.name}: {image.stat().st_size} bytes; {len(deliverables)} build outputs collected.")
