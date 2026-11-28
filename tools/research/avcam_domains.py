#!/usr/bin/env python3
"""Domain catalogue for the ``av-cam.bin`` research map (Phase 0 skeleton).

A *domain* is a disjoint slice of the image that one worker can own without
colliding with another: it has a shard file, a set of tag patterns, MMIO ranges,
and the questions that must be answered before the corresponding milestone can
be attempted. This module only classifies; it does not claim that a classified
function is understood.

The ranges below are not guesses about the SoC: every named window is pinned to
an already recorded observation (E0526 SIO/GPIO/ADC, E0561–E0569 CamPig
``0xF3000000``/``0xF3120000``, E0640 work DMA ``0xF0102000``, E0534 Leo PLL
``0xFC021000``, E0700 HDMI/CEC/AudioIp, E0702 RCRAW ``0xF3030000``, E0703 AIF
``0xF3040000``, E0709 fill engine ``0xF30A0000``) or to the image itself (the
measured literal histogram in ``avcam_index_report.md``). Anything not covered
falls to the residue domain ``9R`` so nothing is silently dropped.

Two rules stay explicit because they are the only places where a domain is
chosen by inference rather than by a recorded address:

* a tag is assigned by its name pattern, not by an observed caller;
* an MMIO page is assigned by the first matching declared range, and those
  ranges are window-level, not register-level.

A shard records the assigned domain next to the measured signal so a worker can
overrule it with evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Domain:
    id: str
    shard: str
    title: str
    phase: int
    milestone: str
    summary: str
    tag_patterns: tuple[str, ...]
    mmio_ranges: tuple[tuple[int, int], ...]
    questions: tuple[str, ...]
    deliverable: str

    def tag_matches(self, tag: str) -> bool:
        return any(re.fullmatch(pattern, tag, re.IGNORECASE) for pattern in self.tag_patterns)


DOMAINS: tuple[Domain, ...] = (
    Domain(
        id="1A",
        shard="S1A_oril_boot_rtos",
        title="ORIL boot, RTOS kernel and task scheduler",
        phase=1,
        milestone="M1",
        summary=(
            "Entry path from the nine vector words through the ARM stub into Thumb code, "
            "the RTOS primitives (tasks, semaphores, event flags, timers, IRQ table) and the "
            "allocator over .bss and the LIRO RW copy."
        ),
        tag_patterns=(r"RTOS", r"TASK", r"IRQ", r"BOOT", r"VEC", r"SCHED", r"SYS", r"DBG",
                      r"CTRLCORE"),
        mmio_ranges=(
            (0xF0000000, 0xF00FFFFF),
            (0xF2000000, 0xF23FFFFF),
            (0xF2400000, 0xF2404FFF),
            (0xF2406000, 0xF240FFFF),
            (0xF2800000, 0xF2A3FFFF),
        ),
        questions=(
            "What register state and memory contract does the loader leave at 0x635c6030, "
            "and what does the ARM stub at 0x635c6030..0x635c6044 change before its `bx`?",
            "Where are the vector words used after boot (IRQ dispatch table, task switch, "
            "secondary-core bring-up), and how does E0569's record alignment show up?",
            "What are the task/semaphore/event/timer object layouts, and which .bss ranges do "
            "they occupy?",
            "Which allocator serves .bss and the LIRO RW copy, and what are its block headers?",
        ),
        deliverable="ABI notes for boot + RTOS primitives with a minimal init sequence that "
                    "reaches the first idle loop, and the M1 entry contract.",
    ),
    Domain(
        id="1B",
        shard="S1B_liro_ipc_commands",
        title="Linux-LIRO boundary, UIPC/ADF/SDF and the command dispatcher",
        phase=1,
        milestone="M1",
        summary=(
            "How im.elf and Linux reach av-cam: shared-memory rings, UIPC framing, the "
            "[ADF]/[SDF]/[VDF]/[VEF] message families, and the 285-entry av_command_table."
        ),
        tag_patterns=(r"ADF", r"SDF", r"VDF", r"VEF", r"UIPC", r"LIRO", r"IPC"),
        mmio_ranges=((0xFE000000, 0xFEFFFFFF),),
        questions=(
            "What is the exact shared-memory layout of the LIRO copy and the UIPC ring "
            "(header, slot size, producer/consumer indices, ownership)?",
            "What are the argument and return conventions of an av_command_table handler, and "
            "what does the checker word gate?",
            "How are ADF/SDF/VDF/VEF message families framed and routed, and which of them are "
            "synchronous?",
            "Which command ids does im.elf send during boot (CheckerBootState, ObjMic, "
            "ObjPower), and what does av-cam do differently for a timeout?",
        ),
        deliverable="UIPC/command ABI specification with a byte-level ring contract and a table "
                    "of the 285 command ids with handler signatures. Blocks M2.",
    ),
    Domain(
        id="1C",
        shard="S1C_memory_dma",
        title="DMM memory map, cache and DMA coherency",
        phase=1,
        milestone="M1",
        summary=(
            "DDR-A/DDR-B use from DmmConfig.bin, the work DMA at 0xF0102000, the 0xF30A fill "
            "engine and the cache maintenance that keeps Linux and av-cam coherent."
        ),
        tag_patterns=(r"DMM", r"DMA", r"PL080", r"CACHE", r"MEM", r"WBI"),
        mmio_ranges=(
            (0xF0100000, 0xF01FFFFF),
            (0xF30A0000, 0xF30AFFFF),
            (0xF8000000, 0xF8FFFFFF),
        ),
        questions=(
            "Which physical DRAM extents does av-cam own versus borrow from Linux, and how is "
            "DmmConfig.bin interpreted at run time?",
            "What is the work-DMA descriptor/start/stop/busy contract (E0640), and which "
            "transfers use it rather than a PL080 channel?",
            "What does the 0xF30A fill engine do, and how does a guest observe completion?",
            "Where are the cache clean/invalidate points around shared buffers?",
        ),
        deliverable="Memory and DMA contract: extents, descriptors, completion and coherency "
                    "rules, with the minimal set needed for boot (M1).",
    ),
    Domain(
        id="2A",
        shard="S2A_mode_sequencer",
        title="Mode sequencer, camera state machine and diagnostics",
        phase=2,
        milestone="M3",
        summary=(
            "The stepping state machine observed at steps 12/26/32/42, the state cache, "
            "diagnostic commands and VD_Sequence frame sequencing."
        ),
        tag_patterns=(r"STATECACHE", r"CDIAG", r"DBGCMD", r"SEQ.*", r"MODE", r"PMV", r"RE",
                      r"SEQUENCE"),
        mmio_ranges=(),
        questions=(
            "What are the numbered sequencer steps, their guards and their timeouts, and which "
            "step does a healthy boot stop at?",
            "What does the state cache hold, who writes it, and which reader gates step 32/42?",
            "Which diagnostic commands ([CDIAG], [DBGCMD]) change state machine behaviour, and "
            "which are safe to replay?",
            "How does VD_Sequence relate to VPH frames and the Ydet wait?",
        ),
        deliverable="State-machine table: states, transitions, guards, timeouts and the minimal "
                    "sequence to reach live view. Blocks M3.",
    ),
    Domain(
        id="2B",
        shard="S2B_sensor_readout",
        title="IMX284 sensor, readout timing and P2S",
        phase=2,
        milestone="M3",
        summary=(
            "Sensor interface, Leo PLL/MIPI D-PHY lock, the 19 IMX284 modes, P2S channel "
            "bursts and genlock."
        ),
        tag_patterns=(r"IDT.*", r"NSX_OPD", r"SENS.*", r"GENLOCK", r"P2S", r"IMX.*", r"OPD.*",
                      r"LEO", r"MIPI.*"),
        mmio_ranges=(
            (0xFC000000, 0xFC0FFFFF),
            (0xF3020000, 0xF302FFFF),
        ),
        questions=(
            "What are the 19 mode descriptors (size, bit depth, frame timing), and where are "
            "they stored?",
            "What is the Leo PLL/D-PHY lock sequence (0xFC021000 +0x50/+0x54/+0x58) and which "
            "status bits prove lock?",
            "How does P2S channel 0/1 burst timing map to the 13-column mode tables, and who "
            "consumes the bursts?",
            "How are [IDT] (391 strings) request/response pairs framed and acknowledged?",
        ),
        deliverable="Sensor control sequence and mode catalogue sufficient to start a readout "
                    "without original code; the exact P2S frame contract for M3.",
    ),
    Domain(
        id="2C",
        shard="S2C_isp_imaging",
        title="ISP, imaging pipeline and AE/AF/AWB front end",
        phase=2,
        milestone="M3",
        summary=(
            "CamCore 0xF3000000/0xF3010000 register programming, CC: 3D LUT, EC_Gamma, "
            "dcv_psf/spica/PP_Detail, Ydet statistics, CamPig, face/tracking and adjustment."
        ),
        tag_patterns=(r"CC", r"EC_Gamma", r"dcv_psf", r"spica", r"PP_Detail", r"FACE.*", r"TRK.*",
                      r"ADJ.*", r"Ydet", r"CamPig", r"PQD", r"EE", r"Astra", r"GOLFSHOT",
                      r"Staple", r"CMR", r"CMP"),
        mmio_ranges=(
            (0xF3000000, 0xF301FFFF),
            (0xF3100000, 0xF31FFFFF),
        ),
        questions=(
            "Which 0xF300/0xF301/0xF310..0xF312 windows are programmed for a still versus a "
            "live-view frame, and in what order?",
            "What is the CC: 3D LUT / Q10 matrix / EC_Gamma byte layout and where does the "
            "pipeline read it?",
            "What does the PQD Ydet producer publish, and what makes the step-32 wait pass "
            "legitimately?",
            "Which AE/AF/AWB statistics does av-cam compute, and which are consumed by im.elf?",
        ),
        deliverable="Imaging register sequence and data-format specification for the smallest "
                    "pipeline that produces a live-view frame. Blocks M3.",
    ),
    Domain(
        id="2D",
        shard="S2D_codecs_raw_aif",
        title="Still/video encoders, RAW develop and AIF DMA",
        phase=2,
        milestone="M4",
        summary=(
            "SFRS/SFRA encode statistics, IMLENCSTAT/IMLDECSTAT, RCRAW and 0xF3030000 cRAW2, "
            "the AIF DMA page and the ZIMA/DVENC movie path."
        ),
        tag_patterns=(r"SFRS", r"SFRA", r"SFR", r"IMLENCSTAT", r"IMLDECSTAT", r"ENCHANDLER",
                      r"DECHANDLER", r"RATECONTROL", r"RCRAW", r"AIF", r"ZIMA", r"DVENC",
                      r"CODEC", r"SEG", r"RC"),
        mmio_ranges=((0xF3030000, 0xF304FFFF),),
        questions=(
            "What are the AIF descriptor rings and the page at 0xF3040000 used for during "
            "capture?",
            "Which encoder/decoder ops (JPEG op=0x11, ECS op=0x1B, RC0 UYVY) does av-cam issue, "
            "and with which buffer descriptors?",
            "What is the cRAW2 (0xF3030000) frame layout and completion protocol (ARW 2.3)?",
            "What do SFRS/SFRA and IMLENCSTAT report, and who consumes the statistics?",
        ),
        deliverable="Codec and RAW frame contract with buffer ownership and completion, enough "
                    "for a first still capture. Blocks M4.",
    ),
    Domain(
        id="2E",
        shard="S2E_display_audio",
        title="Display, EVF/LCD, HDMI, CEC and audio",
        phase=2,
        milestone="M3",
        summary=(
            "VPH/SYSV frame clock and scanout, panel and EVF paths, HDMI TX, CEC, AudioIp and "
            "the SYSND/AIP audio routing."
        ),
        tag_patterns=(r"SYSV", r"AVIO", r"PNL", r"AIP", r"SYSND", r"VPH", r"HDMI", r"CEC",
                      r"AUDIO", r"SndINF", r"SndRMC", r"DISPLAY", r"EVF"),
        mmio_ranges=(
            (0xF2A40000, 0xF2A7FFFF),
            (0xF3C00000, 0xF3C2FFFF),
            (0xF3400000, 0xF34FFFFF),
            (0xF3500000, 0xF35FFFFF),
        ),
        questions=(
            "What is the SYSV/VPH frame-clock contract (start, period, phase bit), and how does "
            "scanout read OSD A/B buffers?",
            "How are the rear LCD and EVF panels powered and configured, and what is the "
            "panel-vsync path?",
            "What does the HDMI TX init sequence require (EDID/HPD/TMDS), and what does CEC "
            "share with it?",
            "How are AudioIp DMA rings and [AIP]/[SYSND] message families driven?",
        ),
        deliverable="Display/audio contract: frame clock, buffer formats, panel enable, and the "
                    "minimum for OSD + live view on the rear panel (M3).",
    ),
    Domain(
        id="2F",
        shard="S2F_peripherals_lens",
        title="Peripherals, sub-micro, E-mount lens and touch",
        phase=2,
        milestone="M4",
        summary=(
            "SIO channel 3 and the sub-micro link, E-mount BL lens protocol on UART1/PL080, "
            "GPIO/ADC keys, touch, USB/SDIO glue and SPI/PMIC windows."
        ),
        tag_patterns=(r"BL", r"RcvRMC", r"RcvINF", r"BT", r"SIO", r"UART", r"UPM", r"GPIO",
                      r"ADC", r"LENS", r"PMIC", r"USB", r"TOUCH", r"I2C", r"SPI",
                      r"Glee.*"),
        mmio_ranges=(
            (0xF2405000, 0xF2405FFF),
            (0xF0200000, 0xF03FFFFF),
            (0xF3800000, 0xF39FFFFF),
        ),
        questions=(
            "What are the SIO channel-3 and sub-micro link-buffer message formats, including "
            "the Slot A/B selection and the lens-rail latch?",
            "What is the E-mount BL protocol state machine (UART1 + PL080 + P1.4) for focus, "
            "zoom and aperture?",
            "How are GPIO/ADC keys and the touch IC reported into av-cam, and what debounce "
            "rules apply?",
            "Which USB/SDIO/SPI/PMIC registers does av-cam touch, and which are Linux's?",
        ),
        deliverable="Peripheral contract for lens and sub-micro traffic, plus the key input "
                    "path. Blocks M4.",
    ),
    Domain(
        id="9R",
        shard="S9R_residue",
        title="Residue: unclassified tags, pages and functions",
        phase=2,
        milestone="none",
        summary=(
            "Everything the declared rules did not classify. A large residue is a finding: it "
            "means the domain table needs another window or tag pattern, not that the code is "
            "unimportant."
        ),
        tag_patterns=(),
        mmio_ranges=(),
        questions=(
            "Which residue tags are real subsystems that the catalogue missed?",
            "Which residue MMIO pages form coherent register blocks (same 64 KiB window, same "
            "access width)?",
        ),
        deliverable="Reclassification of residue items into the domains above or a new domain.",
    ),
)

DOMAIN_BY_ID = {domain.id: domain for domain in DOMAINS}

#: The ABI checklist. Nine entries are the measured header vectors; the rest are
#: the contracts Phase 1 must write down before M1 can be attempted. The
#: denominator is this list's length, so adding a contract lowers every score
#: until it is documented -- which is the intended behaviour.
ABI_CONTRACTS: tuple[tuple[str, str], ...] = (
    ("liro-rw-copy", "LIRO RW section copy: source, destination, size, who performs it"),
    ("uipc-ring", "UIPC request/response ring: header, slot size, indices, ownership"),
    ("command-record", "av_command_table record: id/handler/flag/checker and call convention"),
    ("irq-delivery", "IRQ delivery: CamPig 0xF3000000 IRQ 36, 0xF3120000 IRQ 35, SIO 201+ch"),
    ("boot-handshake", "Loader register state at entry and the ARM stub's effect"),
    ("dma-coherency", "Work-DMA / fill-engine completion and cache maintenance contract"),
    ("task-abi", "Task create/switch/exit and semaphore/event ABI"),
    ("timer-tick", "Timer/tick source, unit and wrap behaviour"),
)


def assign_tag(tag: str) -> str:
    """Domain id for a ``[TAG]`` token.

    A tag names either a subsystem (``[ADF]``, ``[IDT]``) or a class
    (``[AdfCmdInputCtrl]``, ``[IdtCam]``), so the test is a case-insensitive
    prefix test and the **longest** matching pattern wins: ``RcvRMC`` must beat
    the shorter ``RC``, and ``SFR`` must not steal ``SFRS``. A tag no pattern
    claims stays in the residue.
    """
    best: tuple[int, str] | None = None
    for domain in DOMAINS:
        if domain.id == "9R":
            continue
        for pattern in domain.tag_patterns:
            if re.match(pattern, tag, re.IGNORECASE):
                if best is None or len(pattern) > best[0]:
                    best = (len(pattern), domain.id)
    return best[1] if best else "9R"


def assign_mmio(page: int) -> str:
    """Domain id for a 4 KiB MMIO page, by the first declared range that contains it."""
    for domain in DOMAINS:
        for low, high in domain.mmio_ranges:
            if low <= page <= high:
                return domain.id
    return "9R"


def assign_function(mmio_domains: dict[str, int], tag_domains: dict[str, int]) -> str | None:
    """Pick the domain with the unique strongest signal, or ``None`` on a tie.

    ``mmio_domains`` and ``tag_domains`` map domain id to a signal count. A
    function is attributed only when one domain holds strictly more signals than
    every other; a tie stays unattributed so the residue keeps it visible.
    """
    totals: dict[str, int] = {}
    for domain_id, count in mmio_domains.items():
        totals[domain_id] = totals.get(domain_id, 0) + count
    for domain_id, count in tag_domains.items():
        totals[domain_id] = totals.get(domain_id, 0) + count
    if not totals:
        return None
    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]
