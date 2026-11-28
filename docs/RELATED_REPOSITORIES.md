# Related Research Repositories

An agent-facing discovery map for Sony camera emulation, firmware formats,
hardware contracts, service protocols and application research. Consult the
relevant entries before implementing a new contract or repeating an experiment.

## Scope and provenance

- Initial catalog: 2026-11-16, based on the twelve upstream READMEs linked below.
  Descriptions summarize upstream scope, not findings reproduced in this project.
- 2026-11-27: added voxivoid/a6000-qemu, from its README at commit `62cf3bc`, by
  its author; its machine was ported and re-run here (see the linked receipt).
- 2026-11-28: added [qn06142/zv-e10-re](https://github.com/qn06142/zv-e10-re),
  reviewed at commit `9b5debdd5f33f18166fb347a5245ac0c3bdeb3d7` (`2026-11-28`,
  with consolidated sensor pipeline documentation in
  `docs/hardware/SENSOR_PIPELINE.md` from commit
  `5937639cd3e2c55bb367e0d4ee9ca5d2c8fee330`), licensed under `MIT`
  (`LICENSE.txt`, with `GPL-2.0` headers on derived QEMU/pipeline files under
  `research/device/zve10_ram_boot/`).
- This is a reference catalog, not a dependency manifest, compatibility matrix,
  source audit or authorization to run upstream tools. No upstream tools were
  cloned or executed to create it.
- Links follow upstream `HEAD` and can change. Before relying on a finding,
  record the repository, exact commit, file/section, camera body, firmware
  version and relevant input hashes in a local evidence record.

## How agents should use this catalog

1. Read the [current checkpoint](CURRENT_STATE.md), relevant local
   [evidence](evidence/README.md) and [agent rules](../AGENTS.MD) first.
2. Select references by the contract being investigated, not just camera name:
   - Boot, QEMU, timers, interrupts or display: [emulation](#emulation-and-hardware-contracts).
   - Update packages, partitions or UI containers: [formats](#firmware-formats-and-storage-layouts).
   - USB service protocols or runtime tooling: [service interfaces](#service-interfaces-and-runtime-tooling).
   - Product gates or newer architectures: [cross-generation comparisons](#cross-generation-boot-and-feature-research).
3. Follow the upstream README to the actual code or evidence. Treat upstream
   claims as leads until checked against identity-matched local inputs. Shared
   symbols, product families or SoC names do not prove identical register maps,
   interrupts, ABIs, settings identifiers or UI behavior.
4. Distinguish upstream observations, local observations and inference. If
   sources disagree, preserve their dates and input identities and resolve the
   discrepancy; do not replace newer local evidence with an upstream summary.
   Record material conclusions in [evidence/](evidence/) using the
   [validation rules](VALIDATION.md).
5. Inspect tools before execution. Several references include service-mode
   transitions, persistent settings/preload changes, raw storage writes,
   firmware installation or driver changes. Listing them here grants no
   permission for those operations; local safety boundaries remain in force.
6. Check the license and attribution requirements of each file before reuse.
   Keep proprietary inputs, dumps, credentials and keys out of this repository.
   Summaries and additions here must remain English-only.

## Emulation and hardware contracts

| Repository | Upstream scope | Research starting points | Transfer boundary |
| --- | --- | --- | --- |
| [Oein/a5000-emulator](https://github.com/Oein/a5000-emulator) | ILCE-5000 full-system QEMU emulation | [README](https://github.com/Oein/a5000-emulator/blob/HEAD/README.md): QEMU overlay, device models, boot hand-off and hardware notes | Earlier camera generation. Re-derive each contract for `CXD90045`; emulated or host-injected behavior is not automatically original-firmware behavior. |
| [voxivoid/a6000-qemu](https://github.com/voxivoid/a6000-qemu) | ILCE-6000 full-system QEMU emulation (fork of Oein/a5000-emulator) | [README](https://github.com/voxivoid/a6000-qemu/blob/HEAD/README.md): NAND two-stage boot, USB, NFC, Wi-Fi, HDMI; ported here as `sony-a6000` ([receipt](evidence/external-a6000-emulator/README.md)) | `CXD90014` body. Same transfer limits as the a5000 entry; boots only with the owner's NAND dump. |
| [cokeenet/zve10-qemu](https://github.com/cokeenet/zve10-qemu) | ZV-E10 / `CXD90045` firmware execution, system mapping and menu research | [README](https://github.com/cokeenet/zve10-qemu/blob/HEAD/README.md): QEMU workstream, firmware/system reports, display and menu work | Same SoC family is a comparison lead, not an a6400 profile. Separate stock execution, instrumentation and menu-port results; hardware recovery instructions are not approved procedures here. |
| [qn06142/zv-e10-re](https://github.com/qn06142/zv-e10-re) (pinned commit `9b5debdd5f33f18166fb347a5245ac0c3bdeb3d7`, `MIT` / `GPL-2.0` device headers) | Reverse engineering of Sony `ZV-E10` (`2.02` / `2.03`, `av-cam.bin` `bc6cba98...`): `IMX284` + `CXD90057`/`CXD90045` DFE sensor bring-up (`Leo` PLL/CRG `0xFC021000`, `64 KiB` DFE ARM coprocessor `dfe_dat.bin` `703f457a...` mailbox `0xFC0300F4..FC`, `SyncSeq` `0xFC080000`, `P2S` Ch 1 `0xFC088000`, `DrvSGC0` `0xFC0A1100`, `SLVS-EC RX` `0xFC0F0000`, `RdcW0` Bayer RAW DMA `0xFC0E1100`), OSAL/UIPC vocabulary, UXC resource formats, `ZVOS` bare-metal RAM runtime, and QEMU `11.1.2` `-M zve10` machine | [README](https://github.com/qn06142/zv-e10-re/blob/9b5debdd5f33f18166fb347a5245ac0c3bdeb3d7/README.md) (`README.md`), `docs/hardware/SENSOR_PIPELINE.md` (commit `5937639cd3e2c55bb367e0d4ee9ca5d2c8fee330`), `research/device/zve10_ram_boot/cxd90057_hw.h`, and `research/device/zve10_ram_boot/cxd90057_pipeline.h` | `dfe_dat.bin` (`65,536` B), `bonobo.bin`, `lif_app.bin`, and `0xFC0xxxxx` front-end MMIO bases match `ILCE-6400` / `ILCE-6400A` (`CXD90045`), whereas `av-cam.bin` code addresses differ per firmware build and must be re-verified against local `ILCE-6400` (`90d515cf...`) / `ILCE-6400A` (`3029a174...`) binaries. Upstream USB Senser `/dev/mem` write scripts, `boot.ko` RAM takeover modules, and firmware update attack-surface/staging scripts are **excluded** from local camera authorization (standing read-only + `tools/devmem_policy.py` rules apply). Upstream standalone `cxd90057_pipeline.h` omits upstream `SYSV` (`0xF3402000`) / `SUGI_CLK` (`0xF3400000`) / `CLKGEN` (`0xF2800000`), which local `av-cam.bin` disassembly identifies as the master `VD` clock feeding `SyncSeq` (`0xFC080030`) and `DrvSGC0` (`trig_mode = 1`). |

## Firmware formats and storage layouts

| Repository | Upstream scope | Research starting points | Transfer boundary |
| --- | --- | --- | --- |
| [ma1co/fwtool.py](https://github.com/ma1co/fwtool.py) | Sony firmware unpacking across multiple container and chip families, including `CXD90045` FDAT | [README](https://github.com/ma1co/fwtool.py/blob/HEAD/README.md): supported formats and updater/dump extraction inputs | Confirm the exact container and supported variant. Extraction support does not establish runtime compatibility or safe repacking/flashing. |
| [Lost-week-1025/CXD90045-FPSU](https://github.com/Lost-week-1025/CXD90045-FPSU) | ZV-1F / ZV-E10 package structures and UI/input-layer comparison | [README](https://github.com/Lost-week-1025/CXD90045-FPSU/blob/HEAD/README.md): nested containers, integrity layers, partition tables, UXC resources and touch-input analysis | Static comparison and feasibility analysis, not a hardware-verified UI transplant. Translate research summaries into English and independently verify format/ABI claims. |
| [CliffVale/sony-a6000-firmware-research](https://github.com/CliffVale/sony-a6000-firmware-research) | ILCE-6000 firmware 3.21 unpacking and raw-dump comparison | [README](https://github.com/CliffVale/sony-a6000-firmware-research/blob/HEAD/README.md): partition verification, region mapping and Android userdata analysis | Earlier body and storage layout. Do not generalize its Android runtime, partition offsets or updater-shell procedure to the a6400. |

## Service interfaces and runtime tooling

| Repository | Upstream scope | Research starting points | Transfer boundary |
| --- | --- | --- | --- |
| [DevCrashDay/a6400](https://github.com/DevCrashDay/a6400) | ILCE-6400 firmware 2.00 homebrew and runtime hooks | [README](https://github.com/DevCrashDay/a6400/blob/HEAD/README.md): native ARM execution, preload hooks, camera controls and reported verification limits | Same body still requires exact firmware/library identity checks. Persistent hooks and settings changes are outside ordinary offline/read-only work; compare historical limitations with current local evidence. |
| [masterflai/pmca-re-reloaded](https://github.com/masterflai/pmca-re-reloaded) | Cross-platform Rust Sony USB toolkit spanning multiple device generations | [README](https://github.com/masterflai/pmca-re-reloaded/blob/HEAD/README.md): shared GUI/CLI core, device information, settings memory and device support | Inspect the specific protocol/backend. App installation, firmware updates, settings writes and automatic driver handling are not authorized by this reference. |
| [dino2048/zv-e10-toolkit](https://github.com/dino2048/zv-e10-toolkit) | ZV-E10 Linux service-interface and BootROM/fastboot tooling; reported firmware 2.02/2.03 coverage | [README](https://github.com/dino2048/zv-e10-toolkit/blob/HEAD/README.md): Linux file/backup verification route and separate fastboot route | Keep Linux service observations separate from bare-metal recovery behavior. Bulk reads, mode changes and partition restore require their own scope; upstream hardware receipts do not verify our device. |
| [YuuLuo/a7c2-service-preload](https://github.com/YuuLuo/a7c2-service-preload) | ILCE-7CM2 firmware 2.01 service-session and temporary loader execution research | [README](https://github.com/YuuLuo/a7c2-service-preload/blob/HEAD/README.md): protocol notes, Jiritsu path behavior, dynamic-loader chain and execution evidence | Newer body and firmware-specific behavior. Preload/file writes are not read-only operations; temporary execution does not imply persistent firmware modification or an a6400 execution path. |

For ZV-E10 OSAL/UIPC notes, USB Senser probes, and `ZVOS` / `boot.ko` RAM runtime
references, see the `qn06142/zv-e10-re` entry under
[Emulation and hardware contracts](#emulation-and-hardware-contracts); its
`/dev/mem` write scripts, `boot.ko` RAM takeover modules, and updater staging
tooling are excluded from local camera authorization.

## Cross-generation boot and feature research

These are comparative references, not declarations of `CXD90045` compatibility.

| Repository | Upstream scope | Research starting points | Transfer boundary |
| --- | --- | --- | --- |
| [DavidBuchanan314/ILCE-7M4-RE](https://github.com/DavidBuchanan314/ILCE-7M4-RE) | ILCE-7M4 / A7 IV boot and hardware reverse engineering | [README](https://github.com/DavidBuchanan314/ILCE-7M4-RE/blob/HEAD/README.md): serial boot, fastboot payloads, eMMC organization and memory map | Different platform. Do not transplant addresses, storage/crypto assumptions or BootROM procedures; flash/restore operations are not approved here. |
| [OlliV/fx30_fw](https://github.com/OlliV/fx30_fw) | ILME-FX30 firmware 6.01 extraction and feature survey | [README](https://github.com/OlliV/fx30_fw/blob/HEAD/README.md): UFU/FDAT extraction, application strings, menu survey and feature probes | Different generation/container variant. Separate offline findings from service probes and RAM/settings writes; dormant code is not proof of an available feature. |
| [leogenot/sony-a7iv-feature-research](https://github.com/leogenot/sony-a7iv-feature-research) | ILCE-7M4 firmware 6.02 extraction, product classes and feature availability | [README](https://github.com/leogenot/sony-a7iv-feature-research/blob/HEAD/README.md): research evidence, product-class analysis, feature inventory and runtime gates | Shared implementation or an accepted backup setting does not prove a reachable menu or recording path. Do not carry setting identifiers or hardware verification across models. |
| [ArcaneLantern/a7iii-open-gate-test](https://github.com/ArcaneLantern/a7iii-open-gate-test) | ILCE-7M3 (4.04) squeezed 3:2 Open Gate RAM test kit (`og_m3_v2` / `m3og_v3`) | `K2_TEST.md`, `predictions.json`, and `m3og_design.h` at commit `1644dac1c30a037f76064b410a96712edda7ed1b`: `reglist` Y-window shift, 65 vertical FE record fields scaled into `3840x2160`, lockstep timing `(h, v)` scaling, and `S-Ele Refresh` root cause | Different sensor (`IMX410` vs ILCE-6400 `IMX284`), firmware (`4.04` vs `dd8f8d0c`), timing symmetry (`h1 != h2` vs `h1 == h2`), and OB reglist entries (absent on ILCE-6400). Re-map every address/contract locally and obtain explicit user GO before physical RAM writes. |

## Maintaining the catalog

- Add a row to the closest section with the canonical repository URL, explicit
  body/firmware scope where known, an upstream starting point, concrete research
  topics and a transfer limitation. Mark unknown scope as unknown.
- When changing a description, record its review date and source. The date above
  covers the initial inventory only; it is not a rolling verification date.
- Keep detailed findings and pinned citations in evidence records, not in this
  navigation list. Link any local result that supersedes an upstream assumption
  without silently rewriting historical evidence.
- Do not turn this catalog into automatic cloning, dependency installation,
  firmware downloading or camera probing.
