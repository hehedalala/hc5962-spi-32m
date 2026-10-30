# HC5962 / B70 — OpenWrt SPI 32MB

GitHub Actions 编译 HC5962 硬改 32MiB SPI-NOR 版本，使用整套官方 OpenWrt main 内核、mac80211 与 mt76，不混用旧版厂商无线驱动。

## 版本

- OpenWrt：`5700101d7c85381889d890dc1d3d001d0b4b6446`（2026-10-29）。
- mt76：`0936617dc529ff9da0385221d3fa6de732231177`（2026-10-28）。
- 软件包 feeds 固定提交，见 `config/feeds.conf`。
- LuCI 中文、HTTPS、双频无线、USB 存储与 EXT4 基础支持。
- 开发快照；不预置用户密码、Wi-Fi 密码、拨号账号或无线校准数据。

## 编译与下载

提交构建文件后会自动触发一次编译。也可打开 **Actions → Build HC5962 SPI 32MB → Run workflow** 手动运行。

编译成功后，在对应运行页面的 **Artifacts** 下载：

- `hc5962-spi-32m-运行编号`：固件、SHA256SUMS、完整配置与版本记录。
- `hc5962-spi-32m-logs-运行编号`：编译日志及源码信息；失败时也上传。

固件名包含 `hiwifi_hc5962-spi`。构建脚本检查 uImage 头和 32448KiB 上限，缺失正确镜像会报错。
工作流不会自动创建 Release，也不会连接或刷写路由器。

## 硬件布局假设

已知用户使用 Newifi D1 Breed。**该 Breed 型号本身不能证明闪存分区匹配。** 本适配采用旧 HC5962 SPI 项目的布局：

| 分区 | 偏移 | 长度 |
|---|---|---|
| u-boot | 0x0 | 0x30000 |
| u-boot-env | 0x30000 | 0x10000 |
| factory | 0x40000 | 0x10000 |
| firmware | 0x50000 | 0x1fb0000 |

factory 的 2.4GHz/5GHz EEPROM 偏移分别为 `0x0` / `0x8000`，LAN/WAN MAC 偏移为 `0xe000` / `0xe006`。
前面三个分区在设备树中为只读。适配保留 HC5962 官方 LED/reset 引脚及三 LAN、一 WAN 的 DSA 拓扑。

**编译通过不等于已通过实机测试。** 刷写前需备份完整闪存与 factory，并核对 Breed 中 firmware 起点为 `0x50000`、factory 数据与上述偏移一致。布局不同则先修改适配，不能强刷。
本配置生成 SPI sysupgrade 镜像，不生成原厂 NAND factory/UBI 镜像。由旧网络架构迁移至 DSA 时应重新配置网络。

## 来源

- 官方源码：https://github.com/openwrt/openwrt/tree/5700101d7c85381889d890dc1d3d001d0b4b6446
- SPI 适配参考：https://github.com/lon91ong/OpenWRT_B70_SPI_16M
- OpenWrt 构建文档：https://openwrt.org/docs/guide-developer/toolchain/use-buildsystem

源码及派生文件遵循各自上游许可证；OpenWrt 的许可证见其 `LICENSES/` 及相关源码文件。
