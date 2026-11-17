# Developing camera mods in QEMU

The QEMU guest runs the camera's own libraries and the original Sony menu. Mod Loader v2
installs inside that guest with its unchanged guard, and SMOD v2 mods load from host build
outputs. A rebuilt `.mod` replaces the running copy without restarting QEMU
([E0577](../docs/evidence/0577-qemu-real-mods-relocation-and-hot-reload.md)).

## One-time setup

1. Build QEMU with the current overlay (see [README](README.md)). Use a tree whose path has
   no spaces, for example `~/a6400-qemu-moddev`, and pass it with `A6400_QEMU_DIR`.
2. Prepare the camera-captured inputs (E0524). The default `qemu-a6400/data/emmc.img` holds
   a different `libObj.so` build than the camera, so camera mods do not match it.

   ```powershell
   .\venv\Scripts\python.exe -m tools.research.qemu_boot_inputs prepare --report-dir artifacts\qemu-a6400\inputs-moddev-1 --allow-unacquired-zero-fill
   ```

3. Build the loader and a relocatable mod:

   ```bash
   bash tools/native_camera/mlv2/build_wsl.sh artifacts/tests/NEW_LOADER
   bash mods/menu_touch_full/build_mtf_v2_wsl.sh artifacts/tests/NEW_MTF
   ```

## Run

Double-click `a6400_QEMU.bat` on the Desktop. When `artifacts/qemu-a6400/inputs-moddev-1` and a
`artifacts/tests/mlv2-layout-build-*` loader exist, the launcher starts in mod-development mode
(override with `A6400_MODDEV_INPUTS`, `A6400_MODDEV_LOADER`, or `A6400_MODDEV=0` for the plain panel).

The toolbar at the top left of the panel page:

| Control | Action |
| --- | --- |
| Load mod | Upload `.mod` files into the running guest (or drop them anywhere on the page). Uploads are kept in `~/a6400-moddev/mods`. |
| Watch build… | Enter a Windows or WSL path of a build output; every rebuild of that file is hot-reloaded. |
| Reload all / ↻ per mod | Force a reload now (new mapping, fresh state). |
| ✕ per mod | Stop watching and delete the guest copy. Switch the mod OFF in the Mods menu first. |
| Core | Push the loader core again; the shim loads it at the next menu root. |
| Rescan | Press MENU so the menu root, and with it the Mods list, is rebuilt. |
| Pause / Resume | Stop or continue the emulated CPUs. |
| Restart | Reboot QEMU; the loader and every loaded mod are installed again. |
| Frame | Save the current LCD image. |

Below the camera, the Mods panel lists each mod with its last result, the loader log and events.
A new mod appears in the Sony Mods page from the next menu root setup (Rescan, or MENU twice).

After the install the panel opens the menu, then closes and reopens it once, so the first menu you see
already has the seventh tab. The launcher also loads ModsTabLook (`mods/mods_tab_look/build_mtl_v2_wsl.sh`,
newest `artifacts/tests/mtl-v2-build-*`), the E0522 MODS tab artwork as a relocatable SMOD v2 mod with the
auto-on flag: the core switches it ON the first time it lists it, and the tab shows the MODS icon. Switched
OFF (or skipped with `A6400_MODDEV_TABLOOK=`), the core uses the sixth tab's ★ icon. The Mods tab is the
last tab, so Left from the first tab reaches it.

Manual start from the repository root in WSL:

```bash
A6400_QEMU_DIR=$HOME/a6400-qemu-moddev EVSEL_LATE_US=500 python3 -u qemu-a6400/tools/rear_panel.py \
  --port 8650 --log /tmp/moddev.log --inputs artifacts/qemu-a6400/inputs-moddev-1 \
  --loader artifacts/tests/NEW_LOADER --mod artifacts/tests/NEW_MTF/MenuTouchFull.mod
```

Open `http://127.0.0.1:8650/`. The panel does the following:

- copies the prepared inputs to `~/a6400-inputs/` once, which halves boot time compared
  with reading them from `/mnt/c`;
- boots to the menu checkpoint in about 35 s;
- stages and runs the guard, then pushes the core and the mods;
- opens the menu.

The Mods page appears from the next MENU press. The panel line under the LCD shows the
loader state, and `/moddev/log` returns the guest loader log and the last reload result.
The serial console log is written next to `--log`.

## Iterate

Rebuild the mod into the same path, for example by rerunning the packer or the build
script into a new directory and copying the `.mod` over the watched file. The watcher then:

1. pushes the file over the serial console and checks it with the guard's `fnv` operation;
2. publishes it by rename;
3. writes the reload request.

On the Sony UI thread the core loads and hash-checks the new copy, then checks its targets.
It switches the old copy OFF with readback, and only then switches the new copy ON with fresh
state ([E0576](../docs/evidence/0576-mod-loader-v2-reload-request.md)). If no menu refresh
happens, the panel presses MENU at most twice. A rebuilt loader `core.bin` is pushed the same
way and loaded at the next menu root. Edits to `rear_panel.html` show on a browser refresh.

## Writing mods that run on both targets

Use the ILCE-6400 reference addresses (camera layout) in mod sources and pack with
`--reloc-layout tools/native_camera/mlv2/reference_layout_ilce6400.txt`. The packer lists
every literal word inside a library range. Review `reloc_words` in its JSON output, and use
`--no-reloc VALUE` for a constant that only looks like an address. The loader moves those
words, and the hook targets, by each library's runtime base. The same `.mod` file therefore
runs in QEMU's layout and, unmoved, on the camera. Heap and anonymous-memory addresses are
never moved. A mod that compares such a pointer must still be checked on each target.

## Limits

- The board has no touch-panel input, so touch handlers do not run in QEMU.
- The guest tmpfs at `/tmp_bt` and the `/setting` staging exist only in the disposable overlay.
- A QEMU pass covers the tested contract. Camera use still needs an explicit go-ahead and a
  RAM-only verification for physical peripherals and timing.
