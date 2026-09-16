# YHS 底盘面板：5 cm 净安全距离

本目录保留大车 `yhs_can_control_qt` 的受控修改文件。

- `warning_threshold_input` 默认值从 `300 mm` 改为 `50 mm`。
- `f40_radar` 启动默认值从 `300 mm` 改为 `50 mm`。
- 原程序会叠加传感器安装偏置：前方 `100 mm`、右侧 `140 mm`、左侧 `180 mm`。因此当前实际触发距离分别约为 `150/190/230 mm`。

车端原始源码和二进制备份位于：

`/home/nvidia/Desktop/bigcar-console/backups/`

该修改只适用于封闭实训室的低速测试，不应用于室外或高速行驶。
