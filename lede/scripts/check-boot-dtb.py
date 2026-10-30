#!/usr/bin/env python3
"""Check boot-critical MT7621 bindings in an actual HC5962 SPI uImage.

The baseline is the modern MT7621 DT topology that booted on this HC5962.
This is a targeted structural/driver-match gate, not a hardware boot test.
Only Python's standard library is required.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import lzma
from pathlib import Path
import struct
import sys
import zlib

BOARD = "hiwifi,hc5962-spi"
FDT_MAGIC = 0xD00DFEED
UIMAGE = struct.Struct(">7I4B32s")
MAX_KERNEL = 128 * 1024 * 1024


class InvalidImage(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidImage(message)


def cells(value: bytes) -> list[int]:
    require(len(value) % 4 == 0, "Property is not a complete u32 cell array")
    return list(struct.unpack(">" + "I" * (len(value) // 4), value))


def strings(value: bytes) -> list[str]:
    require(not value or value[-1] == 0, "String property is not NUL terminated")
    return [item.decode("ascii") for item in value.rstrip(b"\0").split(b"\0")] if value else []


class DeviceTree:
    def __init__(self, data: bytes):
        require(len(data) >= 40, "Truncated DTB header")
        header = struct.unpack_from(">10I", data)
        magic, total, structure, names, reserved, version, compatible, cpu, name_size, structure_size = header
        require(magic == FDT_MAGIC and version == 17 and compatible <= 17, "Unsupported DTB header")
        require(40 <= total <= len(data), "DTB total size is outside the buffer")
        require(40 <= structure < total and structure + structure_size <= total, "Invalid DTB structure bounds")
        require(40 <= names < total and names + name_size <= total, "Invalid DTB string-table bounds")
        require(40 <= reserved < total and reserved % 8 == 0, "Invalid DTB reservation-map offset")
        self.data = data[:total]
        self.nodes: dict[str, dict[str, bytes]] = {}
        self.handles: dict[int, str] = {}
        table = data[names:names + name_size]
        pos = structure
        limit = structure + structure_size
        stack: list[str] = []
        ended = False
        while pos + 4 <= limit:
            token = struct.unpack_from(">I", data, pos)[0]
            pos += 4
            if token == 1:
                end = data.find(b"\0", pos, limit)
                require(end >= 0, "Unterminated DTB node name")
                name = data[pos:end].decode("ascii")
                pos = (end + 4) & ~3
                require(pos <= limit, "DTB node name exceeds structure")
                require("/" not in name, "Invalid slash in DTB node name")
                stack.append(name)
                path = "/" + "/".join(item for item in stack if item)
                require(path not in self.nodes, "Duplicate DTB node " + path)
                self.nodes[path] = {}
            elif token == 2:
                require(bool(stack), "DTB END_NODE without BEGIN_NODE")
                stack.pop()
            elif token == 3:
                require(bool(stack) and pos + 8 <= limit, "Invalid DTB property header")
                length, name_offset = struct.unpack_from(">II", data, pos)
                pos += 8
                require(pos + length <= limit and name_offset < len(table), "Invalid DTB property bounds")
                end = table.find(b"\0", name_offset)
                require(end >= 0, "Unterminated DTB property name")
                name = table[name_offset:end].decode("ascii")
                path = "/" + "/".join(item for item in stack if item)
                require(name not in self.nodes[path], "Duplicate DTB property " + path + ":" + name)
                self.nodes[path][name] = data[pos:pos + length]
                pos = (pos + length + 3) & ~3
            elif token == 4:
                continue
            elif token == 9:
                require(not stack, "DTB ended with unclosed nodes")
                ended = True
                break
            else:
                raise InvalidImage(f"Unknown DTB token {token}")
        require(ended and "/" in self.nodes, "DTB structure has no valid END token")
        for path, props in self.nodes.items():
            values = [props[key] for key in ("phandle", "linux,phandle") if key in props]
            if not values:
                continue
            require(all(len(value) == 4 for value in values), "Invalid phandle width at " + path)
            handles = [cells(value)[0] for value in values]
            require(len(set(handles)) == 1, "Inconsistent phandle aliases at " + path)
            handle = handles[0]
            require(handle not in (0, 0xFFFFFFFF) and handle not in self.handles, "Invalid or duplicate phandle")
            self.handles[handle] = path

    def prop(self, path: str, name: str) -> bytes:
        require(path in self.nodes, "Missing node " + path)
        require(name in self.nodes[path], "Missing property " + path + ":" + name)
        return self.nodes[path][name]

    def strlist(self, path: str, name: str) -> list[str]:
        return strings(self.prop(path, name))

    def u32s(self, path: str, name: str) -> list[int]:
        return cells(self.prop(path, name))

    def u32(self, path: str, name: str) -> int:
        value = self.u32s(path, name)
        require(len(value) == 1, "Expected one cell at " + path + ":" + name)
        return value[0]

    def compatible(self, path: str, value: str) -> bool:
        return value in strings(self.nodes[path].get("compatible", b""))

    def enabled(self, path: str) -> bool:
        while True:
            status = strings(self.nodes[path].get("status", b"okay\0"))
            if status != ["okay"] and status != ["ok"]:
                return False
            if path == "/":
                return True
            path = path.rsplit("/", 1)[0] or "/"

    def unique(self, compatible: str, enabled: bool = True) -> str:
        matches = [path for path in self.nodes if self.compatible(path, compatible) and (not enabled or self.enabled(path))]
        require(len(matches) == 1, f"Expected one active {compatible} node, found {len(matches)}")
        return matches[0]

    def children(self, path: str) -> list[str]:
        return [node for node in self.nodes if node != "/" and (node.rsplit("/", 1)[0] or "/") == path]

    def target(self, handle: int) -> str:
        require(handle in self.handles, f"Unresolved phandle {handle:#x}")
        return self.handles[handle]

    def one_ref(self, path: str, name: str) -> str:
        value = self.u32s(path, name)
        require(len(value) == 1, "Expected one phandle at " + path + ":" + name)
        return self.target(value[0])

    def refs(self, path: str, name: str, count_property: str) -> list[dict]:
        raw = self.u32s(path, name)
        require(bool(raw), "Empty reference list at " + path + ":" + name)
        result = []
        cursor = 0
        while cursor < len(raw):
            target = self.target(raw[cursor])
            count = self.u32(target, count_property)
            require(0 <= count <= 16 and cursor + 1 + count <= len(raw), "Invalid provider cells at " + path + ":" + name)
            require(self.enabled(target), "Disabled resource provider " + target)
            result.append({"provider": target, "args": raw[cursor + 1:cursor + 1 + count]})
            cursor += count + 1
        return result


def extract_image(path: Path) -> tuple[bytes, DeviceTree, dict]:
    image = path.read_bytes()
    require(len(image) >= UIMAGE.size, "Truncated uImage")
    magic, hcrc, stamp, size, load, entry, dcrc, os_id, arch, kind, compression, name = UIMAGE.unpack_from(image)
    require(magic == 0x27051956 and os_id == 5 and arch == 5 and kind == 2, "Expected Linux/MIPS/kernel uImage")
    header = bytearray(image[:UIMAGE.size])
    header[4:8] = bytes(4)
    require(zlib.crc32(header) == hcrc, "uImage header CRC mismatch")
    require(0 < size <= len(image) - UIMAGE.size, "Invalid uImage data size")
    payload = image[UIMAGE.size:UIMAGE.size + size]
    require(zlib.crc32(payload) == dcrc, "uImage data CRC mismatch")
    require(compression in (0, 3), "Unsupported ramips uImage compression")
    kernels = []
    if compression == 3:
        offsets = [0]
    else:
        # OpenWrt loader-kernel appends an LZMA-alone stream to a small loader.
        # Filter plausible LZMA properties/dictionary sizes before decompression.
        offsets = []
        for offset in range(max(0, min(len(payload) - 13, 65536))):
            if payload[offset] not in (0x5D, 0x6D):
                continue
            dictionary = struct.unpack_from("<I", payload, offset + 1)[0]
            if dictionary in {1 << power for power in range(12, 28)}:
                offsets.append(offset)
        require(len(offsets) <= 64, "Too many candidate LZMA headers")
    for offset in offsets:
        try:
            decoder = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE, memlimit=256 * 1024 * 1024)
            kernel = decoder.decompress(payload[offset:], max_length=MAX_KERNEL)
            if decoder.eof and len(kernel) > 1024 * 1024:
                kernels.append((offset, kernel))
        except lzma.LZMAError:
            continue
    require(len(kernels) == 1, f"Expected one complete loader LZMA kernel, found {len(kernels)}")
    lzma_offset, kernel = kernels[0]
    candidates = []
    cursor = 0
    while True:
        offset = kernel.find(struct.pack(">I", FDT_MAGIC), cursor)
        if offset < 0:
            break
        cursor = offset + 4
        if offset + 40 > len(kernel):
            continue
        total = struct.unpack_from(">I", kernel, offset + 4)[0]
        if not (40 < total <= len(kernel) - offset):
            continue
        tail = kernel[offset + total:]
        if len(tail) > 4096 or any(tail):
            continue
        try:
            tree = DeviceTree(kernel[offset:offset + total])
            if tree.compatible("/", BOARD):
                candidates.append((offset, tree))
        except (InvalidImage, UnicodeError, struct.error):
            continue
    require(len(candidates) == 1, f"Expected one appended HC5962 SPI DTB, found {len(candidates)}")
    dtb_offset, tree = candidates[0]
    return kernel[:dtb_offset], tree, {
        "sha256": hashlib.sha256(image).hexdigest(),
        "uimage_data_bytes": size,
        "kernel_load": hex(load),
        "kernel_entry": hex(entry),
        "lzma_offset": lzma_offset,
        "kernel_uncompressed_bytes": len(kernel),
        "dtb_offset": dtb_offset,
        "dtb_bytes": len(tree.data),
        "dtb_sha256": hashlib.sha256(tree.data).hexdigest(),
    }


def check_image(path: Path, require_hnat: bool = False) -> dict:
    body, tree, extraction = extract_image(path)
    checks: list[dict] = []
    resources: dict[str, object] = {}

    def check(name, action):
        try:
            detail = action()
            checks.append({"check": name, "result": "PASS", "detail": detail})
        except (InvalidImage, UnicodeError, struct.error) as exc:
            checks.append({"check": name, "result": "FAIL", "error": str(exc)})

    def expect_ref(path, prop, kind, expected):
        actual = tree.refs(path, prop, kind)
        require(actual == expected, f"{path}:{prop} is {actual}, expected {expected}")
        resources[path + ":" + prop] = actual
        return actual

    def sysc_path():
        return tree.unique("mediatek,mt7621-sysc")

    def memc_path():
        return tree.unique("mediatek,mt7621-memc")

    def driver_match(compatible):
        node = tree.unique(compatible)
        require(compatible.encode() + b"\0" in body, "DT compatible is absent from the kernel body: " + compatible)
        return node

    check("MT7621 system-controller DT/kernel match", lambda: driver_match("mediatek,mt7621-sysc"))
    check("MT7621 memory-controller DT/kernel match", lambda: driver_match("mediatek,mt7621-memc"))

    def providers():
        sysc = sysc_path()
        memc = memc_path()
        require(tree.u32(sysc, "#clock-cells") == 1, "sysc must provide one-cell clock indices")
        require(tree.u32(sysc, "#reset-cells") == 1, "sysc must provide one-cell reset indices")
        require(tree.one_ref(sysc, "ralink,memctl") == memc, "sysc ralink,memctl does not reference the memory controller")
        require(tree.u32s(sysc, "reg") == [0, 0x100], "Unexpected sysc register range")
        require(tree.u32s(memc, "reg") == [0x5000, 0x1000], "Unexpected memory-controller register range")
        return {"sysc": sysc, "memc": memc}
    check("Modern shared clock/reset provider", providers)

    def sysc_refs(path, prop, ids):
        return expect_ref(path, prop, "#clock-cells" if prop == "clocks" else "#reset-cells",
                          [{"provider": sysc_path(), "args": [number]} for number in ids])

    def spi_check():
        spi = tree.unique("ralink,mt7621-spi")
        require(tree.u32s(spi, "reg") == [0xB00, 0x100], "Unexpected SPI registers")
        sysc_refs(spi, "clocks", [18])
        sysc_refs(spi, "resets", [18])
        require(tree.strlist(spi, "clock-names") == ["spi"], "SPI clock must be named spi")
        return spi
    check("SPI clock/reset binding", spi_check)
    check("GIC timer CPU clock", lambda: sysc_refs(tree.unique("mti,gic-timer"), "clocks", [1]))

    def uart_check():
        serial = tree.strlist("/aliases", "serial0")
        require(len(serial) == 1 and tree.enabled(serial[0]), "serial0 does not identify an enabled UART")
        sysc_refs(serial[0], "clocks", [19])
        sysc_refs(serial[0], "resets", [19])
        return serial[0]
    check("Boot console clock/reset binding", uart_check)

    def ethernet_check():
        ethernet = tree.unique("mediatek,mt7621-eth")
        sysc_refs(ethernet, "clocks", [9, 22])
        sysc_refs(ethernet, "resets", [6, 23])
        require(tree.one_ref(ethernet, "mediatek,ethsys") == sysc_path(), "Ethernet ethsys does not reference sysc")
        return ethernet
    check("Ethernet clock/reset binding", ethernet_check)

    def pci_check():
        pci = tree.unique("mediatek,mt7621-pci")
        ports = [child for child in tree.children(pci)
                 if strings(tree.nodes[child].get("device_type", b"")) == ["pci"] and tree.enabled(child)]
        require(len(ports) >= 2, "Fewer than two PCIe ports are enabled")
        found = set()
        for port in ports:
            reg = tree.u32s(port, "reg")
            require(len(reg) == 5 and reg[0] in (0, 0x800, 0x1000), "Unexpected PCIe port address")
            index = reg[0] // 0x800
            found.add(index)
            sysc_refs(port, "clocks", [23 + index])
            sysc_refs(port, "resets", [24 + index])
            phys = tree.refs(port, "phys", "#phy-cells")
            require(len(phys) == 1 and tree.compatible(phys[0]["provider"], "mediatek,mt7621-pci-phy"), "PCIe port has no MT7621 PHY")
            require(phys[0]["args"] == ([1] if index < 2 else [0]), "Unexpected PCIe PHY argument")
            sysc_refs(phys[0]["provider"], "clocks", [0])
            resources[port + ":phys"] = phys
        require({0, 1} <= found, "Required WiFi PCIe port 0 or 1 is missing")
        return ports
    check("Per-port PCIe and PHY resources", pci_check)

    def partition(label):
        matches = [node for node, props in tree.nodes.items() if strings(props.get("label", b"")) == [label]]
        require(len(matches) == 1, "Expected one partition labelled " + label)
        return matches[0]

    def flash_check():
        ranges = {"u-boot": [0, 0x30000], "u-boot-env": [0x30000, 0x10000],
                  "factory": [0x40000, 0x10000], "firmware": [0x50000, 0x1FB0000]}
        for name, expected in ranges.items():
            node = partition(name)
            require(tree.u32s(node, "reg") == expected, "Incorrect SPI partition " + name)
            if name != "firmware":
                require("read-only" in tree.nodes[node], name + " is not read-only")
        firmware = partition("firmware")
        flash = firmware.rsplit("/", 2)[0]
        require(tree.compatible(flash, "jedec,spi-nor"), "Firmware is not on a SPI-NOR flash")
        require(tree.enabled(flash), "SPI-NOR flash is disabled")
        return ranges
    check("32MiB SPI partition layout", flash_check)

    def nvmem_cell(consumer, cell_name, expected):
        names = tree.strlist(consumer, "nvmem-cell-names")
        refs = tree.u32s(consumer, "nvmem-cells")
        require(len(names) == len(refs) and names.count(cell_name) == 1, "Ambiguous nvmem cell at " + consumer)
        cell = tree.target(refs[names.index(cell_name)])
        require(tree.u32s(cell, "reg") == expected, "Incorrect nvmem offset/size at " + consumer)
        layout = cell.rsplit("/", 1)[0]
        factory = layout.rsplit("/", 1)[0]
        require(tree.compatible(layout, "fixed-layout"), "nvmem cell is not in fixed-layout")
        require(factory == partition("factory"), "nvmem cell is outside the factory partition")
        return {"consumer": consumer, "cell": cell, "reg": expected}

    def board_network():
        ethernet = tree.unique("mediatek,mt7621-eth")
        macs = {}
        for child in tree.children(ethernet):
            if tree.compatible(child, "mediatek,eth-mac") and tree.enabled(child):
                macs[tree.u32(child, "reg")] = child
        require(set(macs) == {0, 1}, "Expected both LAN and direct-WAN GMACs")
        nvmem_cell(macs[0], "mac-address", [0xE000, 6])
        nvmem_cell(macs[1], "mac-address", [0xE006, 6])
        require(tree.strlist(macs[1], "label") == ["wan"], "GMAC1 must be named wan")
        phy = tree.one_ref(macs[1], "phy-handle")
        require(tree.u32(phy, "reg") == 4, "Direct WAN GMAC must use PHY4")
        switches = [child for child in tree.nodes if tree.compatible(child, "mediatek,mt7621") and child.startswith(ethernet + "/")]
        require(len(switches) == 1, "Expected one MT7530 switch")
        ports = {tree.u32(node, "reg"): node for node in tree.nodes
                 if node.startswith(switches[0] + "/ports/port@") and node.rsplit("/", 1)[0].endswith("/ports")}
        for index in (1, 2, 3):
            require(index in ports and tree.enabled(ports[index]), f"LAN port {index} is not enabled")
            require(tree.strlist(ports[index], "label") == [f"lan{index}"], f"LAN port {index} has the wrong label")
        require(4 in ports and not tree.enabled(ports[4]), "Switch port4 must not compete with direct WAN GMAC1")
        return {"lan_mac": macs[0], "wan_mac": macs[1], "wan_phy": phy}
    check("HC5962 LAN/direct-WAN MAC and nvmem layout", board_network)

    def wifi_check():
        pci = tree.unique("mediatek,mt7621-pci")
        consumers = []
        for index, expected in ((0, [0, 0x400]), (1, [0x8000, 0x200])):
            ports = [child for child in tree.children(pci)
                     if tree.nodes[child].get("reg") and tree.u32s(child, "reg")[0] == index * 0x800]
            require(len(ports) == 1, "Missing WiFi PCIe port")
            radios = [child for child in tree.children(ports[0]) if tree.compatible(child, "mediatek,mt76") and tree.enabled(child)]
            require(len(radios) == 1, "Expected one mt76 radio per PCIe port")
            consumers.append(nvmem_cell(radios[0], "eeprom", expected))
        return consumers
    check("Both WiFi EEPROM nvmem references", wifi_check)

    def obsolete_references():
        selected = []
        for path, props in tree.nodes.items():
            if not tree.enabled(path):
                continue
            for name, count in (("clocks", "#clock-cells"), ("resets", "#reset-cells"), ("phys", "#phy-cells")):
                if name not in props:
                    continue
                for ref in tree.refs(path, name, count):
                    require(ref["provider"] not in ("/pll", "/rstctrl", "/clkctrl"), "Enabled node still references an obsolete provider: " + path + ":" + name)
                    selected.append({"consumer": path, "property": name, **ref})
        return {"resolved_reference_count": len(selected)}
    check("No enabled references to obsolete PLL/clock/reset providers", obsolete_references)

    def hnat_check():
        found = [node for node in tree.nodes if tree.compatible(node, "mediatek,mtk-hnat_v1") and tree.enabled(node)]
        require(len(found) <= 1, "Multiple enabled MT7621 HNAT instances")
        if not found:
            require(not require_hnat, "Required SDK HNAT DT node is missing")
            return {"present": False, "required": False}
        node = found[0]
        require(tree.u32(node, "mtketh-max-gmac") == 2, "HNAT must match the two-GMAC board topology")
        require("mtkdsa-wan-port" not in tree.nodes[node], "DSA WAN selector conflicts with direct GMAC1 WAN topology")
        # These are constraints for this board's LAN DSA/direct-WAN topology,
        # not proof that the SDK acceleration hooks work at runtime.
        for prop, value in (("mtketh-wan", "wan"), ("mtketh-lan", "lan"), ("mtketh-ppd", "eth0")):
            require(tree.strlist(node, prop) == [value], "Unexpected HNAT interface prefix " + prop)
        require(tree.u32s(node, "reg") == [0x1E100000, 0x3000], "Unexpected HNAT register range")
        return {"present": True, "node": node, "max_gmac": 2,
                "cpu_interface": "eth0", "runtime_verified": False}
    check("Optional SDK HNAT topology", hnat_check)

    failures = [item for item in checks if item["result"] == "FAIL"]
    return {
        "result": "FAIL" if failures else "PASS",
        "image": str(path.resolve()),
        "device": BOARD,
        "extraction": extraction,
        "checks": checks,
        "resolved_boot_resources": resources,
        "failures": [item["check"] + ": " + item["error"] for item in failures],
        "hardware_boot_verified": False,
        "scope": "Boot-critical DT structure and known Linux 6.18 driver matches for the HC5962 SPI baseline. This does not prove hardware boot, HNAT runtime behavior or WiFi throughput.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--report", type=Path, help="Write the full JSON report")
    parser.add_argument("--require-hnat", action="store_true", help="Require a compatible SDK HNAT node")
    args = parser.parse_args()
    try:
        report = check_image(args.image, args.require_hnat)
    except (OSError, InvalidImage, UnicodeError, struct.error, EOFError) as exc:
        report = {"result": "FAIL", "image": str(args.image), "failures": [str(exc)], "hardware_boot_verified": False}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "result": report["result"],
        "image": report["image"],
        "checks_passed": sum(item["result"] == "PASS" for item in report.get("checks", [])),
        "checks_failed": len(report.get("failures", [])),
        "failures": report.get("failures", []),
        "hardware_boot_verified": False,
    }, ensure_ascii=False))
    raise SystemExit(0 if report["result"] == "PASS" else 1)


if __name__ == "__main__":
    main()
