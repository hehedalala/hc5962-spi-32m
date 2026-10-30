#!/usr/bin/env python3
"""Apply an isolated HC5962 SPI-NOR profile to the pinned Lean LEDE tree."""
from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEVICE = "hiwifi_hc5962-spi"
COMPATIBLE = "hiwifi,hc5962-spi"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def replace_marked(text: str, name: str, block: str, anchor: str | None = None) -> str:
    begin = f"# BEGIN HC5962-SPI {name}"
    end = f"# END HC5962-SPI {name}"
    require(text.count(begin) == text.count(end) <= 1,
            f"Malformed or duplicate HC5962-SPI markers: {name}")
    replacement = begin + "\n" + block.rstrip("\n") + "\n" + end + "\n"
    if begin in text:
        before, remainder = text.split(begin, 1)
        _, after = remainder.split(end, 1)
        return before + replacement + after.removeprefix("\n")
    if anchor is None:
        return text.rstrip() + "\n\n" + replacement
    require(text.count(anchor) == 1, f"Missing or ambiguous source anchor for {name}")
    return text.replace(anchor, anchor + replacement, 1)


def remove_marked(text: str, name: str) -> str:
    begin = f"# BEGIN HC5962-SPI {name}"
    end = f"# END HC5962-SPI {name}"
    require(text.count(begin) == text.count(end) <= 1,
            f"Malformed or duplicate HC5962-SPI markers: {name}")
    if begin not in text:
        return text
    before, remainder = text.split(begin, 1)
    _, after = remainder.split(end, 1)
    return before + after.removeprefix("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--wifi-driver", choices=("vendor", "mt76"), default="mt76")
    args = parser.parse_args()
    source = args.source.resolve()
    ramips = source / "target/linux/ramips"
    paths = {
        "image": ramips / "image/mt7621.mk",
        "network": ramips / "mt7621/base-files/etc/board.d/02_network",
        "platform": ramips / "mt7621/base-files/lib/upgrade/platform.sh",
        "dtsi": ramips / "dts/mt7621.dtsi",
    }
    for path in paths.values():
        require(path.is_file(), f"Unsupported or incomplete LEDE source tree: {path}")
    texts = {key: path.read_text(encoding="utf-8") for key, path in paths.items()}
    require("define Device/hiwifi_hc5962\n" in texts["image"],
            "Upstream HC5962 profile missing; review the source revision")
    require(COMPATIBLE not in texts["platform"],
            "SPI board has an explicit upgrade handler; review it before continuing")
    require('default_do_upgrade "$1"' in texts["platform"],
            "Expected default SPI upgrade handler missing")
    for label in ("gmac0:", "hnat:", "switch0:"):
        require(label in texts["dtsi"], f"SDK Ethernet DT label missing: {label}")
    dts_name = ("mt7621_hiwifi_hc5962-spi-vendor.dts" if args.wifi_driver == "vendor"
                else "mt7621_hiwifi_hc5962-spi.dts")
    dts = (ROOT / "files" / dts_name).read_text(encoding="utf-8")
    for required in (
        'compatible = "hiwifi,hc5962-spi", "mediatek,mt7621-soc";',
        "reg = <0x40000 0x10000>;", "reg = <0x50000 0x1fb0000>;",
    ):
        require(required in dts, f"Unexpected board DT or flash layout: {required}")
    require('"bdinfo"' not in dts and '"ubi"' not in dts,
            "SPI DTS must not reference the original NAND layout")
    require(0x50000 + 0x1FB0000 == 32 * 1024 * 1024,
            "Invalid firmware partition size")

    if args.wifi_driver == "vendor":
        for required in ('mtketh-wan = "eth1";', 'mtketh-lan = "eth0";',
                         "mtketh-max-gmac = <2>;", "/delete-property/ mtkdsa-wan-port;"):
            require(required in dts, f"Unexpected swconfig SDK network DT: {required}")
        require("gsw:" in texts["dtsi"] and "gmac1:" in texts["dtsi"],
                "Vendor variant requires the Linux 5.10 SDK Ethernet DT labels")
        packages = ("kmod-mt7603e kmod-mt76x2e mt_wifi luci-app-mtwifi kmod-usb3 "
                    "-kmod-mt7603 -kmod-mt76x2 -uboot-envtools")
    else:
        for required in ('mtketh-wan = "wan";', 'mtketh-lan = "lan";',
                         "mtketh-max-gmac = <1>;", "mtkdsa-wan-port = <4>;",
                         'label = "lan1";', 'label = "lan2";', 'label = "lan3";'):
            require(required in dts, f"Unexpected DSA SDK network DT: {required}")
        packages = "kmod-mt7603 kmod-mt76x2 kmod-usb3 -uboot-envtools"
    profile = f"""define Device/{DEVICE}
  $(Device/uimage-lzma-loader)
  IMAGE_SIZE := 32448k
  IMAGES := sysupgrade.bin
  DEVICE_VENDOR := HiWiFi
  DEVICE_MODEL := HC5962-SPI
  DEVICE_VARIANT := 32M
  DEVICE_PACKAGES := {packages}
  SUPPORTED_DEVICES := {COMPATIBLE}
endef
TARGET_DEVICES += {DEVICE}
"""
    require(f"define Device/{DEVICE}\n" not in texts["image"] or
            "# BEGIN HC5962-SPI profile" in texts["image"],
            "Unmanaged existing SPI image profile; refusing to overwrite it")
    image = replace_marked(texts["image"], "profile", profile)

    interfaces, separator, macs = texts["network"].partition("ramips_setup_macs()")
    require(bool(separator), "MAC setup function missing")
    require("\thiwifi,hc5962|\\\n" in interfaces,
            "Stock HC5962 interface group missing")
    require("\thiwifi,hc5962)\n" in macs,
            "Stock HC5962 MAC setup missing")
    interface_case = f"""\t{COMPATIBLE})
\t\tucidef_set_interfaces_lan_wan "eth0 ra0 rai0" "eth1"
\t\tucidef_add_switch "switch0" \\
\t\t\t"1:lan:1" "2:lan:2" "3:lan:3" "4:wan" "6u@eth0" "5u@eth1"
\t\t;;
"""
    mac_case = f"""\t{COMPATIBLE})
\t\tlan_mac=$(mtd_get_mac_binary factory 0xe000)
\t\twan_mac=$(mtd_get_mac_binary factory 0xe006)
\t\tlabel_mac=$lan_mac
\t\t;;
"""
    interfaces = remove_marked(interfaces, "interfaces")
    spi_line = f"\t{COMPATIBLE}|\\\n"
    require(interfaces.count(spi_line) <= 1,
            "Duplicate SPI board in the upstream LAN/WAN group")
    interfaces = interfaces.replace(spi_line, "")
    require(COMPATIBLE not in interfaces,
            "Unmanaged SPI interface case; refusing to overwrite it")
    if args.wifi_driver == "vendor":
        interfaces = replace_marked(interfaces, "interfaces", interface_case, "\tcase $board in\n")
    else:
        stock = "\thiwifi,hc5962|\\\n"
        require(interfaces.count(stock) == 1, "Ambiguous HC5962 interface group")
        interfaces = interfaces.replace(stock, stock + spi_line, 1)
    macs = replace_marked(macs, "macs", mac_case, "\tcase $board in\n")
    network = interfaces + separator + macs
    require(network.count(COMPATIBLE) == 2, "Unexpected SPI network case count")

    # Compute all edits before changing any source file. The original NAND target
    # and its NAND upgrade handler remain independent from this SPI target.
    changes = {
        paths["image"]: image,
        paths["network"]: network,
        ramips / "dts/mt7621_hiwifi_hc5962-spi.dts": dts,
    }
    for path, content in changes.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    network_mode = ("SDK HNAT eth0/eth1; Linux 5.10 swconfig LAN1-3/WAN4"
                    if args.wifi_driver == "vendor"
                    else "SDK HNAT; Linux 6.18 DSA lan1/lan2/lan3/wan")
    print(f"Installed HC5962-SPI 32M ({args.wifi_driver}): "
          f"firmware 0x50000..0x2000000; {network_mode}")


if __name__ == "__main__":
    main()
