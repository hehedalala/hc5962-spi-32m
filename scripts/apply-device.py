#!/usr/bin/env python3
"""Install the isolated HC5962 32 MiB SPI port into pinned OpenWrt sources."""

from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
DEVICE = "hiwifi_hc5962-spi"
BEGIN = "# BEGIN local HC5962-SPI 32M port"
END = "# END local HC5962-SPI 32M port"
PROFILE = """# BEGIN local HC5962-SPI 32M port
define Device/hiwifi_hc5962-spi
  $(Device/dsa-migration)
  $(Device/uimage-lzma-loader)
  IMAGE_SIZE := 32448k
  IMAGES := sysupgrade.bin
  DEVICE_VENDOR := HiWiFi
  DEVICE_MODEL := HC5962-SPI
  DEVICE_VARIANT := 32M
  DEVICE_PACKAGES := kmod-mt7603 kmod-mt76x2 kmod-usb3 -uboot-envtools
  SUPPORTED_DEVICES := hiwifi,hc5962-spi
endef
TARGET_DEVICES += hiwifi_hc5962-spi
# END local HC5962-SPI 32M port
"""


def require(condition, message):
    if not condition:
        raise SystemExit(message)


def main():
    require(len(sys.argv) == 2, "Usage: apply-device.py OPENWRT_SOURCE_DIR")
    source = Path(sys.argv[1]).resolve()
    ramips = source / "target/linux/ramips"
    image = ramips / "image/mt7621.mk"
    network = ramips / "mt7621/base-files/etc/board.d/02_network"
    platform = ramips / "mt7621/base-files/lib/upgrade/platform.sh"
    dts_source = ROOT / "files/mt7621_hiwifi_hc5962-spi.dts"
    dts_target = ramips / "dts/mt7621_hiwifi_hc5962-spi.dts"
    require(dts_source.is_file(), f"Missing port DTS: {dts_source}")
    for path in (image, network, platform):
        require(path.is_file(), f"Not a supported OpenWrt source tree: {path}")
    image_text = image.read_text(encoding="utf-8")
    network_text = network.read_text(encoding="utf-8")
    platform_text = platform.read_text(encoding="utf-8")
    require("define Device/hiwifi_hc5962\n" in image_text,
            "Expected upstream HC5962 profile missing; review the source revision")
    require("hiwifi,hc5962-spi" not in platform_text,
            "SPI board has an explicit upgrade handler; review it before continuing")
    require("default_do_upgrade" in platform_text,
            "Default SPI upgrade handler missing")

    # Compute and validate every change before writing anything.
    if BEGIN in image_text:
        require(image_text.count(BEGIN) == image_text.count(END) == 1,
                "Malformed existing local profile markers")
        before, remainder = image_text.split(BEGIN, 1)
        _, after = remainder.split(END, 1)
        image_text = before.rstrip() + "\n\n" + PROFILE + after.lstrip("\n")
    else:
        require(f"define Device/{DEVICE}\n" not in image_text,
                "A different SPI profile already exists; refusing to overwrite it")
        image_text = image_text.rstrip() + "\n\n" + PROFILE

    stock = "\thiwifi,hc5962|\\\n"
    spi = "\thiwifi,hc5962-spi|\\\n"
    interface_section = network_text.split("ramips_setup_macs()", 1)[0]
    require(interface_section.count(stock) == 1,
            "Expected HC5962 LAN/WAN group missing or ambiguous")
    if spi not in interface_section:
        network_text = network_text.replace(stock, stock + spi, 1)
    require('ucidef_set_interfaces_lan_wan "lan1 lan2 lan3" "wan"' in interface_section,
            "Upstream three-LAN/one-WAN definition missing")

    dts_text = dts_source.read_text(encoding="utf-8")
    require('compatible = "hiwifi,hc5962-spi", "mediatek,mt7621-soc";' in dts_text,
            "DTS compatible does not match image metadata")
    require('reg = <0x50000 0x1fb0000>;' in dts_text,
            "DTS firmware region is not the expected 32 MiB SPI layout")
    require('"bdinfo"' not in dts_text and '"ubi"' not in dts_text,
            "DTS still refers to stock NAND data")
    image.write_text(image_text, encoding="utf-8", newline="\n")
    network.write_text(network_text, encoding="utf-8", newline="\n")
    dts_target.write_text(dts_text, encoding="utf-8", newline="\n")
    print("Installed HC5962-SPI 32M: firmware=0x50000..0x2000000; independent SPI upgrade handler")


if __name__ == "__main__":
    main()
