"""Deterministic CXD90045 peripheral models ported from QEMU sony_a6400.c."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from src.a6400.devices.cxd90045_sdhci import CxdSdhciEmmc, EMMC0_BASE, EMMC0_SIZE


LDEC_IN_BASE = 0xF0020000
LDEC_IN_SIZE = 0x10000
LDEC_BASE = 0xF2903000
LDEC_IRQ = 171

XDMAC0_BASE = 0xF0000000
XDMAC0_IRQ_BASE = 189
XDMAC1_BASE = 0xF0010000
XDMAC1_IRQ_BASE = 193

F2A_BASES = (
    0xF2A01000,
    0xF2A11000,
    0xF2A12000,
    0xF2A21000,
    0xF2A22000,
    0xF2A30000,
)

LEO_CLK_BASE = 0xFC021000
DFS_CTRL_BASE = 0xF2916000
AUDIOIP_BASE = 0xF3420000
IMAGER_BASE = 0xFC000000
DWC3_BASE = 0xF0200000
DWC3_SIZE = 0x100000


def ldec_lz77_decode(data: bytes, max_out: int) -> bytes | None:
    """Decode a Sony LDEC LZ77 compressed stream (ported from sony_a6400.c)."""
    lengths = (3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 32, 64)
    pos = 0
    in_len = len(data)
    out = bytearray()
    while pos < in_len and len(out) < max_out:
        kind = data[pos]
        pos += 1
        if kind == 0x0F:
            if pos + 3 > in_len:
                return None
            pos += 1  # skip 1 byte
            count = data[pos] | (data[pos + 1] << 8)
            pos += 2
            if pos + count > in_len or len(out) + count > max_out:
                return None
            out.extend(data[pos:pos + count])
            pos += count
        elif kind == 0xF0:
            done = False
            while not done:
                if pos >= in_len:
                    return None
                flags = data[pos]
                pos += 1
                if flags == 0:
                    if pos + 8 > in_len or len(out) + 8 > max_out:
                        return None
                    out.extend(data[pos:pos + 8])
                    pos += 8
                    continue
                for bit in range(8):
                    if (flags >> bit) & 1:
                        if pos + 2 > in_len:
                            return None
                        a = data[pos]
                        b = data[pos + 1]
                        pos += 2
                        dist = ((a & 0x0F) << 8) | b
                        if dist == 0:
                            done = True
                            break
                        length = lengths[a >> 4]
                        if dist > len(out) or len(out) + length > max_out:
                            return None
                        for _ in range(length):
                            out.append(out[-dist])
                    else:
                        if pos >= in_len or len(out) >= max_out:
                            return None
                        out.append(data[pos])
                        pos += 1
        else:
            return None
    return bytes(out)


class CxdRwStub:
    """Plain read-back register file for unmodelled config blocks."""

    def __init__(self, name: str, base: int, size: int, initial: dict[int, int] | None = None):
        self.name = name
        self.base = base
        self.size = size
        self.regs: dict[int, int] = dict(initial or {})

    def read(self, offset: int) -> int:
        return self.regs.get(offset & ~3, 0)

    def write(self, offset: int, value: int) -> None:
        self.regs[offset & ~3] = value & 0xFFFFFFFF


class CxdZeroMmio:
    """Unimplemented-device semantics (reads 0, writes dropped) matching QEMU."""

    def __init__(self, name: str, base: int, size: int):
        self.name = name
        self.base = base
        self.size = size

    def read(self, offset: int) -> int:
        return 0

    def write(self, offset: int, value: int) -> None:
        return None


class CxdF2aClk:
    """CXD90045 F2A clock/reset block (+0x04 bit0, +0x2C bit1, +0x30 self-clear)."""

    def __init__(self, base: int):
        self.base = base
        self.size = 0x1000
        self.regs: dict[int, int] = {}

    def read(self, offset: int) -> int:
        off = offset & ~3
        if off == 0x04:
            return self.regs.get(0x04, 0) | 0x1
        if off == 0x2C:
            return self.regs.get(0x2C, 0) | 0x2
        if off == 0x30:
            return 0
        return self.regs.get(off, 0)

    def write(self, offset: int, value: int) -> None:
        off = offset & ~3
        if off == 0x30:
            return
        self.regs[off] = value & 0xFFFFFFFF


class CxdLeoClk:
    """Leo clock generator at 0xFC021000 (+0x50/+0x54/+0x58 bit0 PLL lock)."""

    def __init__(self, base: int = LEO_CLK_BASE):
        self.base = base
        self.size = 0x1000
        self.regs: dict[int, int] = {}

    def read(self, offset: int) -> int:
        off = offset & ~3
        val = self.regs.get(off, 0)
        if off in (0x50, 0x54, 0x58):
            val |= 1
        return val

    def write(self, offset: int, value: int) -> None:
        self.regs[offset & ~3] = value & 0xFFFFFFFF


@dataclass
class CxdLdec:
    """CXD90045 LDEC LZ77 hardware decompressor (0xF2903000, IRQ 171)."""

    set_irq: Callable[[int, bool], None]
    irq: int = LDEC_IRQ
    regs: list[int] = field(default_factory=lambda: [0] * (0x100 >> 2))
    in_buf: bytearray = field(default_factory=bytearray)

    def read(self, offset: int) -> int:
        idx = offset >> 2
        return self.regs[idx] if 0 <= idx < len(self.regs) else 0

    def write(self, offset: int, value: int) -> None:
        idx = offset >> 2
        if 0 <= idx < len(self.regs):
            self.regs[idx] = value & 0xFFFFFFFF
        if offset == 0x30:
            self.regs[0x34 >> 2] &= ~(value & 0xFFFFFFFF)
            if (self.regs[0x34 >> 2] & 0x7B) == 0:
                self.set_irq(self.irq, False)

    def feed(
        self,
        dsa_off: int,
        ssa: int,
        length: int,
        mem_read: Callable[[int, int], bytes],
        mem_write: Callable[[int, bytes], None],
    ) -> None:
        if length == 0 or length > 16 * 1024 * 1024 or dsa_off > 16 * 1024 * 1024:
            return
        if dsa_off == 0:
            self.in_buf.clear()
        need = dsa_off + length
        if need > len(self.in_buf):
            self.in_buf.extend(b'\x00' * (need - len(self.in_buf)))
        self.in_buf[dsa_off:need] = mem_read(ssa, length)
        records = self.regs[0x0C >> 2]
        dst_phys = self.regs[0x10 >> 2]
        max_out = records * 4096 if 0 < records <= 4096 else 16 * 1024 * 1024
        if dst_phys != 0:
            decoded = ldec_lz77_decode(bytes(self.in_buf), max_out)
            if decoded is not None and len(decoded) > 0:
                mem_write(dst_phys, decoded)
                self.in_buf.clear()
                self.regs[0x34 >> 2] = 0x1
                self.set_irq(self.irq, True)


class CxdXdmac:
    """CXD90045 4-channel XDMAC DMA engine."""

    def __init__(
        self,
        base: int,
        irq_base: int,
        set_irq: Callable[[int, bool], None],
        mem_read: Callable[[int, int], bytes],
        mem_write: Callable[[int, bytes], None],
        ldec: CxdLdec | None = None,
    ):
        self.base = base
        self.size = 0x10000
        self.irq_base = irq_base
        self.set_irq = set_irq
        self.mem_read = mem_read
        self.mem_write = mem_write
        self.ldec = ldec
        self.regs = [0] * (0x100 >> 2)

    def read(self, offset: int) -> int:
        idx = offset >> 2
        return self.regs[idx] if 0 <= idx < len(self.regs) else 0

    def write(self, offset: int, value: int) -> None:
        val32 = value & 0xFFFFFFFF
        idx = offset >> 2
        if 0 <= idx < len(self.regs):
            self.regs[idx] = val32
        if 0x10 <= offset < 0x10 + 4 * 0x30:
            ch = (offset - 0x10) // 0x30
            ch_off = (offset - 0x10) % 0x30
            ch_base = 0x10 + ch * 0x30
            if ch_off == 0x18 and val32 != 0:
                tbc = self.regs[(ch_base + 0x00) >> 2] + 1
                ssa = self.regs[(ch_base + 0x04) >> 2]
                dsa = self.regs[(ch_base + 0x08) >> 2]
                if (
                    self.ldec is not None
                    and LDEC_IN_BASE <= dsa < LDEC_IN_BASE + LDEC_IN_SIZE
                ):
                    self.ldec.feed(dsa - LDEC_IN_BASE, ssa, tbc, self.mem_read, self.mem_write)
                elif 0 < tbc <= 16 * 1024 * 1024:
                    data = self.mem_read(ssa, tbc)
                    self.mem_write(dsa, data)
                self.regs[(ch_base + 0x20) >> 2] = 0x8
                self.set_irq(self.irq_base + ch, True)
            elif ch_off == 0x20 and val32 == 0:
                self.regs[(ch_base + 0x20) >> 2] = 0
                self.set_irq(self.irq_base + ch, False)


class CxdPeripheralBus:
    """Unified MMIO dispatcher for ported QEMU CXD90045 peripherals."""

    def __init__(
        self,
        set_irq: Callable[[int, bool], None],
        mem_read: Callable[[int, int], bytes],
        mem_write: Callable[[int, bytes], None],
        *,
        emmc_image: Path | None = None,
    ):
        self.ldec = CxdLdec(set_irq=set_irq)
        self.xdmac0 = CxdXdmac(
            XDMAC0_BASE, XDMAC0_IRQ_BASE, set_irq, mem_read, mem_write, ldec=self.ldec
        )
        self.xdmac1 = CxdXdmac(
            XDMAC1_BASE, XDMAC1_IRQ_BASE, set_irq, mem_read, mem_write, ldec=None
        )
        self.leo_clk = CxdLeoClk(LEO_CLK_BASE)
        self.f2a = [CxdF2aClk(base) for base in F2A_BASES]
        self.sdhci = (
            CxdSdhciEmmc(set_irq, mem_read, mem_write, image_path=emmc_image)
            if emmc_image is not None
            else None
        )
        self.rw_stubs = [
            CxdRwStub('dfs_ctrl', DFS_CTRL_BASE, 0x1000),
            CxdRwStub('audioip', AUDIOIP_BASE, 0x8000),
            CxdRwStub('imager', IMAGER_BASE, 0x200000),
        ]
        # Ordered from highest priority / most specific to lowest.
        self._devices: list[tuple[int, int, object]] = [
            (XDMAC0_BASE, 0x10000, self.xdmac0),
            (XDMAC1_BASE, 0x10000, self.xdmac1),
            (LDEC_BASE, 0x1000, self.ldec),
            (LEO_CLK_BASE, 0x1000, self.leo_clk),
            (DWC3_BASE, DWC3_SIZE, CxdZeroMmio('dwc3_unmodelled', DWC3_BASE, DWC3_SIZE)),
        ]
        if self.sdhci is not None:
            self._devices.append((EMMC0_BASE, EMMC0_SIZE, self.sdhci))
        for dev in self.f2a:
            self._devices.append((dev.base, dev.size, dev))
        for dev in self.rw_stubs:
            self._devices.append((dev.base, dev.size, dev))

    def _lookup(self, address: int) -> tuple[object, int] | None:
        for base, size, dev in self._devices:
            if base <= address < base + size:
                return dev, address - base
        return None

    def read(self, address: int, size: int = 4) -> int | None:
        hit = self._lookup(address)
        if hit is None:
            return None
        dev, offset = hit
        if isinstance(dev, CxdSdhciEmmc):
            return dev.read(offset, size)
        return dev.read(offset)

    def write(self, address: int, value: int, size: int = 4) -> bool:
        hit = self._lookup(address)
        if hit is None:
            return False
        dev, offset = hit
        if isinstance(dev, CxdSdhciEmmc):
            dev.write(offset, value, size)
            return True
        dev.write(offset, value)
        return True
