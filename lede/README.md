# HC5962 SPI 32M — Lean LEDE + mt76 + WPA3 + MTK SDK HWNAT

本目录是独立的 LEDE 编译方案，原有主线固件与构建文件保留在仓库根目录。

## 启动故障与修复状态

运行 41026505538 的旧 LEDE 镜像已被报告无法正常启动，禁止继续使用该产物。实际成品中的 MT7621 系统控制器、时钟、复位及 PCIe 设备树仍沿用旧绑定，与 Linux 6.18 不匹配。镜像 CRC/SHA256 正确并不能证明硬件能启动。

本次修复完整采用之前已在本机正常启动的 OpenWrt 提交 5700101d7c85381889d890dc1d3d001d0b4b6446 的 common/板级硬件定义，并最小叠加 SDK HNAT 节点。新的 common 文件独立安装，不覆盖 LEDE 的 5.10 设备树。发布前必须从实际内核镜像提取 DTB，通过启动绑定门禁；新产物仍需硬件启动验证。

## 本次构建

- 源码：coolsnowwolf/lede，固定提交 `8867669e23613233dde23cad3efe9d1f5de2cec8`。
- 无线方案：用户选择 mt76，保留 WPA3-Personal 支持。
- 内核：LEDE 自带的 Linux 6.18.58 测试分支。
- 附带 SDK HNAT socket bind 接口兼容补丁，同时修正 hnat.c 和 hnat_mcast.c 的 Linux 6.18 地址参数类型；补丁 SHA256 会写入构建记录。
- mt76：LEDE 对 6.18 分支配套的 2026-09-01 / `be5ce7910521492d4a2e4ce7ee3843680a46c047`，不是宣称上游最新提交。
- 认证组件：wpad-openssl，提供 SAE/PMF 所需功能。实际启用 WPA3 还须配置无线安全选项并使用兼容客户端。
- 硬件加速：`kmod-mtk-hnat` 随固件安装，`mtkhnat.ko` 开机自动加载；不是只安装 LuCI 插件。
- SDK HNAT 使用独立内核 hook；首次启动默认关闭软件/硬件 flow offload，避免争用 PPE。
- firewall4 必须依赖的 nft-offload 模块仍会随镜像安装；模块存在不代表启用了 flow offload。
- LuCI HTTPS 使用 luci-ssl-openssl，和系统默认的 OpenSSL 后端保持一致。
- 保留 Dropbear SSH、htop、中文 LuCI、HTTPS 和 USB 存储支持。
- 完整安装 packages/LuCI 的编译定义来解析源码包全部变体的依赖；实际写入镜像的软件仍由精简 .config 决定。

## SPI 与网络适配

只用于已改 SPI 32MiB 的 HC5962，设备标识 `hiwifi,hc5962-spi`。不是原厂 NAND HC5962 镜像。

- u-boot：0x0–0x30000，只读。
- u-boot-env：0x30000–0x40000，只读。
- factory：0x40000–0x50000，只读。
- firmware：0x50000–0x2000000，32448 KiB 上限。
- 2.4GHz/5GHz EEPROM：factory 内偏移 0 / 0x8000。
- LAN/WAN MAC：factory 内偏移 0xe000 / 0xe006。
- mt76/6.18 方案沿用已工作的双 GMAC：GMAC0 接 DSA LAN1/2/3，GMAC1 直连 PHY4 并命名为 wan。
- 灯和复位 GPIO 采用 HC5962，不能因为使用 Newifi D1 Breed 就套用 D1 硬件定义。

新构建尚未在实机启动。6.18 是 LEDE 测试内核，编译通过不代表无线吞吐、SDK HNAT 命中或配置迁移已经通过实机验证。构建不会自动刷写路由器。

## 云编译

工作流：仓库 `.github/workflows/build-lede.yml`。

本次 `lede-sdk-hwnat` 分支的 push 自动使用 mt76 配置；手动运行也默认 mt76。固件与日志分开上传，构建失败只提供日志。产物同时包含 SHA256SUMS、软件清单、build-info.json 和 boot-dtb-check.json，核验实际包清单确实包含 kmod-mtk-hnat，并检查实际成品的启动关键设备树与内核匹配。

vendor 仅作为备用构建选项：Linux 5.10.272 + MT7603e 4.1.2.0 / MT7612e 3.0.5.0。这份 5GHz 厂商驱动不含 SAE，不能承诺 WPA3，因此不作为本次选择。

## 参考结论

用户提供的恩山帖子讨论的是将原生 HWNAT 换成 MTK SDK HWNAT，不等同于更换无线驱动。LEDE 现已直接包含 SDK HNAT，并对 6.18 提供了接入主线以太网驱动的补丁；不用再把 2022 年的整套旧源码直接搬入。

- [原作者在 OpenWrt 的说明](https://forum.openwrt.org/t/use-mtk-sdk-hwnat-driver-to-replace-hw-flow-offload/128120/1)
- [LEDE HNAT 包定义](https://github.com/coolsnowwolf/lede/blob/8867669e23613233dde23cad3efe9d1f5de2cec8/target/linux/ramips/modules.mk)
- [LEDE 6.18 HNAT 集成](https://github.com/coolsnowwolf/lede/blob/8867669e23613233dde23cad3efe9d1f5de2cec8/target/linux/ramips/patches-6.18/904-net-mediatek-add-hnat-integration.patch)
- [各内核对应的 mt76 版本](https://github.com/coolsnowwolf/lede/blob/8867669e23613233dde23cad3efe9d1f5de2cec8/package/kernel/mt76/Makefile)
