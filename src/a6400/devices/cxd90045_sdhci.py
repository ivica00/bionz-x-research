"""SDHCI / eMMC controller model (0xF0304000, IRQ 227) for CXD90045 UnicornMachine."""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Callable


EMMC0_BASE = 0xF0304000
EMMC0_SIZE = 0x1000
EMMC0_IRQ = 227
EMMC0_CAPAREG = 0x05E830B0
EMMC0_HOST_VERSION = 0x0002  # SDHCI Spec v3.00

# SDHCI register offsets
SDHCI_DMA_ADDRESS = 0x00
SDHCI_BLOCK_SIZE = 0x04
SDHCI_BLOCK_COUNT = 0x06
SDHCI_ARGUMENT = 0x08
SDHCI_TRANSFER_MODE = 0x0C
SDHCI_COMMAND = 0x0E
SDHCI_RESPONSE_0 = 0x10
SDHCI_RESPONSE_1 = 0x14
SDHCI_RESPONSE_2 = 0x18
SDHCI_RESPONSE_3 = 0x1C
SDHCI_BUFFER = 0x20
SDHCI_PRESENT_STATE = 0x24
SDHCI_HOST_CONTROL = 0x28
SDHCI_POWER_CONTROL = 0x29
SDHCI_CLOCK_CONTROL = 0x2C
SDHCI_TIMEOUT_CONTROL = 0x2E
SDHCI_SOFTWARE_RESET = 0x2F
SDHCI_INT_STATUS = 0x30
SDHCI_INT_ENABLE = 0x34
SDHCI_SIGNAL_ENABLE = 0x38
SDHCI_CAPABILITIES = 0x40
SDHCI_CAPABILITIES_1 = 0x44
SDHCI_ADMA_ADDRESS = 0x58
SDHCI_HOST_VERSION = 0xFE

INT_CMD_COMPLETE = 0x0001
INT_XFER_COMPLETE = 0x0002
INT_DMA = 0x0008
INT_BUFFER_READ_READY = 0x0020
INT_TIMEOUT = 0x10000


class CxdSdhciEmmc:
    """Read-only eMMC-backed SDHCI v3 controller matching sony-a6400 QEMU."""

    def __init__(
        self,
        set_irq: Callable[[int, bool], None],
        mem_read: Callable[[int, int], bytes],
        mem_write: Callable[[int, bytes], None],
        *,
        image_path: Path | None = None,
        image_bytes: bytes | None = None,
        user_offset: int = 8 * 1024 * 1024,
        irq: int = EMMC0_IRQ,
    ):
        self.set_irq = set_irq
        self.mem_read = mem_read
        self.mem_write = mem_write
        self.irq = irq
        self.image_path = image_path
        self.image_bytes = image_bytes
        self.user_offset = user_offset
        self._file = image_path.open('rb') if image_path is not None else None
        if image_bytes is not None:
            self._total_bytes = len(image_bytes)
        elif image_path is not None:
            self._total_bytes = image_path.stat().st_size
        else:
            self._total_bytes = 0
        self.regs = bytearray(0x100)
        self._pio_buf = b''
        self._pio_pos = 0
        self.cmd_count = 0
        self.sectors_read = 0
        self._reset(0x01)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def _reset(self, mask: int) -> None:
        if mask & 0x01:
            self.regs[:] = b'\x00' * 0x100
            struct.pack_into('<I', self.regs, SDHCI_CAPABILITIES, EMMC0_CAPAREG)
            struct.pack_into('<H', self.regs, SDHCI_HOST_VERSION, EMMC0_HOST_VERSION)
            # Card inserted, state stable, detect pin high
            struct.pack_into('<I', self.regs, SDHCI_PRESENT_STATE, 0x00070000)
            self._pio_buf = b''
            self._pio_pos = 0
            self._update_irq()

    def _update_irq(self) -> None:
        status = struct.unpack_from('<I', self.regs, SDHCI_INT_STATUS)[0]
        enable = struct.unpack_from('<I', self.regs, SDHCI_INT_ENABLE)[0]
        signal = struct.unpack_from('<I', self.regs, SDHCI_SIGNAL_ENABLE)[0]
        self.set_irq(self.irq, bool(status & enable & signal))

    def _read_backing(self, offset: int, length: int) -> bytes:
        if self.image_bytes is not None:
            chunk = self.image_bytes[offset:offset + length]
            return chunk.ljust(length, b'\x00')
        if self._file is not None:
            self._file.seek(offset)
            chunk = self._file.read(length)
            return chunk.ljust(length, b'\x00')
        return b'\x00' * length

    def _build_ext_csd(self) -> bytes:
        ext = bytearray(512)
        ext[192] = 7  # EXT_CSD_REV = 7 (matches QEMU patch)
        ext[194] = 2  # EXT_CSD_STRUCTURE = 2
        ext[196] = 0b11  # CARD_TYPE
        user_bytes = max(0, self._total_bytes - self.user_offset)
        sectors = user_bytes // 512
        struct.pack_into('<I', ext, 212, sectors)
        return bytes(ext)

    def read(self, offset: int, size: int = 4) -> int:
        if offset >= 0x100:
            return 0
        if offset == SDHCI_BUFFER and self._pio_pos < len(self._pio_buf):
            chunk = self._pio_buf[self._pio_pos:self._pio_pos + size]
            self._pio_pos += len(chunk)
            if self._pio_pos >= len(self._pio_buf):
                st = struct.unpack_from('<I', self.regs, SDHCI_INT_STATUS)[0]
                st &= ~INT_BUFFER_READ_READY
                struct.pack_into('<I', self.regs, SDHCI_INT_STATUS, st | INT_XFER_COMPLETE)
                self._update_irq()
            return int.from_bytes(chunk.ljust(size, b'\x00'), 'little')
        return int.from_bytes(self.regs[offset:offset + size], 'little')

    def write(self, offset: int, value: int, size: int = 4) -> None:
        if offset >= 0x100:
            return
        if offset == SDHCI_INT_STATUS:
            cur = struct.unpack_from('<I', self.regs, SDHCI_INT_STATUS)[0]
            mask = (value & ((1 << (size * 8)) - 1))
            struct.pack_into('<I', self.regs, SDHCI_INT_STATUS, cur & ~mask)
            self._update_irq()
            return
        # Write raw bytes into register file
        raw = (value & ((1 << (size * 8)) - 1)).to_bytes(size, 'little')
        self.regs[offset:offset + size] = raw
        if offset <= SDHCI_CLOCK_CONTROL < offset + size:
            clk = struct.unpack_from('<H', self.regs, SDHCI_CLOCK_CONTROL)[0]
            if clk & 0x01:
                clk |= 0x02  # INT_CLK_STABLE
                struct.pack_into('<H', self.regs, SDHCI_CLOCK_CONTROL, clk)
        if offset <= SDHCI_SOFTWARE_RESET < offset + size:
            rst = self.regs[SDHCI_SOFTWARE_RESET]
            if rst:
                self._reset(rst)
                self.regs[SDHCI_SOFTWARE_RESET] = 0
                return
        if offset in (SDHCI_INT_ENABLE, SDHCI_SIGNAL_ENABLE):
            self._update_irq()
        if offset <= SDHCI_COMMAND < offset + size and (offset + size) >= SDHCI_COMMAND + 2:
            cmdreg = struct.unpack_from('<H', self.regs, SDHCI_COMMAND)[0]
            self._execute_command((cmdreg >> 8) & 0x3F)

    def _set_resp(self, r0: int = 0, r1: int = 0, r2: int = 0, r3: int = 0) -> None:
        struct.pack_into('<4I', self.regs, SDHCI_RESPONSE_0, r0 & 0xFFFFFFFF,
                         r1 & 0xFFFFFFFF, r2 & 0xFFFFFFFF, r3 & 0xFFFFFFFF)

    def _deliver_data(self, data: bytes) -> int:
        xfer_mode = struct.unpack_from('<H', self.regs, SDHCI_TRANSFER_MODE)[0]
        host_ctrl = self.regs[SDHCI_HOST_CONTROL]
        if (xfer_mode & 0x01) and (host_ctrl & 0x18) == 0x10:
            # ADMA2
            desc_addr = struct.unpack_from('<I', self.regs, SDHCI_ADMA_ADDRESS)[0]
            pos = 0
            while pos < len(data):
                attr_len, buf_phys = struct.unpack('<II', self.mem_read(desc_addr, 8))
                valid = (attr_len & 0x01) != 0
                end = (attr_len & 0x02) != 0
                action = attr_len & 0x30
                length = (attr_len >> 16) & 0xFFFF or 65536
                if not valid:
                    break
                if action == 0x20:
                    self.mem_write(buf_phys, data[pos:pos + length])
                    pos += length
                if end:
                    break
                desc_addr += 8
            return INT_CMD_COMPLETE | INT_XFER_COMPLETE
        if xfer_mode & 0x01:
            # SDMA
            dma_addr = struct.unpack_from('<I', self.regs, SDHCI_DMA_ADDRESS)[0]
            self.mem_write(dma_addr, data)
            struct.pack_into('<I', self.regs, SDHCI_DMA_ADDRESS, dma_addr + len(data))
            return INT_CMD_COMPLETE | INT_XFER_COMPLETE
        # PIO
        self._pio_buf = data
        self._pio_pos = 0
        return INT_CMD_COMPLETE | INT_BUFFER_READ_READY

    def _execute_command(self, cmd: int) -> None:
        self.cmd_count += 1
        arg = struct.unpack_from('<I', self.regs, SDHCI_ARGUMENT)[0]
        blk_size = struct.unpack_from('<H', self.regs, SDHCI_BLOCK_SIZE)[0] & 0x0FFF
        blk_cnt = struct.unpack_from('<H', self.regs, SDHCI_BLOCK_COUNT)[0] or 1
        int_flags = INT_CMD_COMPLETE

        if cmd == 0:
            self._set_resp()
        elif cmd == 1:
            # eMMC SEND_OP_COND: honor sector-mode bit 30 (0x40000000)
            ocr = 0x80FF8080 | (0x40000000 if (arg & 0x40000000) else 0)
            self._set_resp(ocr)
        elif cmd in (2, 9, 10):
            # CID / CSD (generic eMMC)
            self._set_resp(0x00010000, 0x00000000, 0x00000000, 0x00D00F00)
        elif cmd == 3:
            self._set_resp(0x00010000)
        elif cmd in (6, 7, 16):
            self._set_resp(0x00000900)
        elif cmd == 13:
            self._set_resp(0x00000900)  # TRAN state, ready for data
        elif cmd == 8:
            # eMMC SEND_EXT_CSD
            int_flags = self._deliver_data(self._build_ext_csd())
        elif cmd in (17, 18):
            count = 1 if cmd == 17 else blk_cnt
            nbytes = count * (blk_size or 512)
            byte_off = self.user_offset + arg * 512
            data = self._read_backing(byte_off, nbytes)
            self.sectors_read += count
            int_flags = self._deliver_data(data)
        elif cmd == 55:
            # APP_CMD: timeout so Linux probes eMMC via CMD1 instead of SD
            int_flags = INT_TIMEOUT
        else:
            self._set_resp(0)

        cur = struct.unpack_from('<I', self.regs, SDHCI_INT_STATUS)[0]
        struct.pack_into('<I', self.regs, SDHCI_INT_STATUS, cur | int_flags)
        self._update_irq()
