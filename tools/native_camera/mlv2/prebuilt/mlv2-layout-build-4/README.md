# Mod Loader v2 prebuilt (`mlv2-layout-build-4`)

Installable RAM-only loader for `ILCE-6400` `im.elf`. This is the layout-aware build used by the QEMU mod panel and by the camera run in E0633. `core.bin` SHA-256 is `f58335566dd7f26552ca3e05b0fbf8407406b4fbd6354bde4e38d1bc5ee971ab`. Shim and request hashes are in `build.json`. `SHA256SUMS.txt` covers every file except this note.

Install from a fresh Mass Storage boot:

```powershell
.\venv\Scripts\python.exe -m tools.research.install_mlv2 --build tools/native_camera/mlv2/prebuilt/mlv2-layout-build-4 --mod mods/arri_isp/arrivederci/Arrivederci.mod
```

`Arrivederci.mod` in that path is the v5 build (129444 bytes, SHA-256 `46c41981e7b37231a6cadb0d033f514c5b05a17ecb38c85f68d909d64a0b64b7`). Turn Arrivederci OFF before power-off.
