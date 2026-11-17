# E0573 — Interactive a6400 rear panel in QEMU

Date: 2026-11-17. Offline follow-up to E0572. The physical SD card and
camera were not accessed by the emulator.

## Rear controls

The browser panel uses the rear-side arrangement in
[Sony's ILCE-6400 Help Guide](https://helpguide.sony.net/ilc/1810/v1/en/contents/TP0002280308.html).
The screen is the QEMU VPH head 0 screendump. The local service translates
each click to a QMP key pulse, then the opt-in machine input handler drives
the emulated hardware. Menu selection and rendering run in the firmware.

The key numbers come from the original guest's
`/proc/udm/input` (E0526) and matching key/name pairs in `libObj.so`
around file offset `0x13e18d4`. The [pinned a5000 implementation](https://github.com/Oein/a5000-emulator/blob/6ad36bd7b0411d55a5920447ff51e3de01f8cb9b/qemu-overlay/hw/arm/sony_a5000.c)
provides the ADC and sub-micro input approach. Its ADC channel assignments
differ from the a6400.

Input SHA-256 identities:
`libObj.so` `60ffd2b0f31f4bc139a7c13a4f62c25cdeb6a531ad5ef35df48471e6e36e88b1`,
`av-cam.bin` `90d515cfec8b936f1ba9a69dcac462816b880297752a652474d67c4bba39125e`.

| Control | Guest-facing signal |
| --- | --- |
| Four directional presses and center | ADC unit 1 channel 3 (global channel `0x0f`), levels 182 / 356 / 520 / 686 / 45 for up / left / down / right / center. |
| Fn and C2/Delete | ADC unit 1 channel 0 (global `0x0c`), levels 520 / 45, codes `0x7095` / `0x1f8`. |
| MENU and playback | Captured slot-A CAkey1 bits 2 / 1 active high. |
| Flash pop-up | Slot-A CAkey1 bit 9 active low. |
| MOVIE | P0.10 active low, code `0x01f0`. |
| AF/MF–AEL lever and press | P0.11 latched toggle, and P0.7 active-low pulse. Firmware catalog names are `KEY_SLI_AF_MF_AEL` and `KEY_OTHER_VER_GRIP_AEL_AFMF`. The P0.7 body mapping is inferred from the combined name and the a6400 GPIO table. |
| Wheel rotation | Host increments/decrements the 16-bit TPU counter at `0xf2404014`. The channel-0 wheel assignment follows the a5000 lead. The a6400 original `ldrh` at `0x63a5ecde` reads `0xf2404014 + (channel << 8)`. The passive plugin sampled channels 0 and 1. |

The slot-A reply changes only the asserted key bits and recalculates the
existing CRC-16/CCITT-FALSE over bytes 0–125. Release uses the original
captured bytes. The E0572 display edit is hash-guarded and applied only to
the run's qcow2 overlay. The relevant input and LCD flags are off in ordinary
`run.sh` runs. The panel enables them by default and accepts `--stock` to
leave display selection to the original firmware.

## Replays

- Browser panel at `http://127.0.0.1:8641/` on the isolated rebuilt
  QEMU binary SHA-256
  `200a3623dfb7580e81f0b777c7956a8b0cfaff4b8f223f8e156a93221502a64d`.
  From the project root using WSL, set `A6400_QEMU_DIR` to this build,
  then run `EVSEL_LATE_US=500 python3 qemu-a6400/tools/rear_panel.py --port 8641 --log NEW_LOG`.
  The phase remains experimental. The flickering/empty frames during
  transitions were captured directly from QEMU.
- MENU opened the original `Quality/Image Size1` menu. The direction
  and center sequence produced different firmware frames, and a MOVIE press
  showed the Sony `No memory card. Cannot record movie.` dialog. Center
  dismissed it. The playback test produced a separate no-card state.
- An attempted `/dev/input/event0` capture failed with `No such device`
  in this boot. The menu proof uses visible firmware frames and the model
  uses the underlying key paths. Separate observations cover the wheel
  counter and lever instead of relying on the static device nodes.
- The follow-up HMP readback changed `0xf2910010` from `0x2c9a` to
  `0x249a` after toggling the lever, which clears bit 11. The wheel word
  read `0x0000 → 0x0001 → 0x0000` after clockwise and counterclockwise
  clicks. The original counter accessor was observed on both channels
  during boot. The bounded run did not observe nonzero firmware reads
  after the host changed the counter, or a visible response to rotation.
  The application-level AF/MF–AEL response also remains unverified.
  The next dial probe is the original `JogTpu` vtable at `0x644e6208`.
  Its `0x63c159b0` method forwards the channel to `0x63d7afb2`, which
  branches to the halfword accessor. Its `0x63c15970` method calculates
  the signed rotation from two counter samples.
- The local build compiled and linked against the existing QEMU checkout
  without editing it. The full Windows suite ran 1225 tests and
  reported 146 errors and 39 skips, matching the previous local totals.

Receipts, original instructions, frames, and verification scripts:
`artifacts/qemu-a6400/rear-controls/`.

The next boundary is the stock selection through the receiver for
`Camera Settings2 → FINDER/MONITOR → Monitor(Manual)`, then the effect of
the two dial channels in original application code.
