# External ILCE-7M3 USB identity receipt

Recorded 2026-11-28 from public Linux host reports for the Sony a7 III (ILCE-7M3).

| Profile key | USB VID:PID | Host observation |
| --- | --- | --- |
| `multi_configuration` | `054C:0C01` | `lsusb` reports `ILCE-7M3` with four configurations. Configurations 1 and 3 expose Mass Storage and 2 and 4 expose Imaging. [libgphoto2 issue #805](https://github.com/gphoto/libgphoto2/issues/805) |
| `mass_storage` | `054C:0C02` | The capture from the camera's Mass Storage setting exposes one configuration with a Mass Storage interface. [Mass Storage capture](https://github.com/gphoto/libgphoto2/files/2730403/lsusb_a7m3_mass.txt) |
| `mtp` | `054C:0C03` | The capture from the MTP setting exposes one Imaging configuration and reports `ILCE-7M3`. [MTP capture](https://github.com/gphoto/libgphoto2/files/2730404/lsusb_a7m3_mtp.txt) |
| `pc_remote` | `054C:0C34` | The reporter captured this ID with `lsusb` and used gPhoto to take and download a picture. [libgphoto2 issue #514](https://github.com/gphoto/libgphoto2/issues/514) |

The `0C01`, `0C02`, and `0C03` captures report USB device release `bcdDevice` as `0x0200`. The USB device release field describes the USB descriptor. Firmware version and the Sony `GetModelInfo` product code require their own evidence.

[E0482](../0482-three-stubs-gain-sourced-values.md) records ten stock descriptor IDs from the packaged 4.04 updater, including `0C01` and `0C02`. The host captures establish the configurations listed here. Their camera firmware versions are unknown. The ILCE-7M3 remains a reference profile pending camera-side mod validation.
