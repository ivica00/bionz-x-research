"""Unit tests for ported CXD90045 peripheral models."""
import unittest

from src.a6400.devices.cxd90045_peripherals import (
    CxdPeripheralBus,
    F2A_BASES,
    LDEC_BASE,
    LDEC_IN_BASE,
    LDEC_IRQ,
    LEO_CLK_BASE,
    XDMAC0_BASE,
    XDMAC0_IRQ_BASE,
    ldec_lz77_decode,
)


class CxdPeripheralsTests(unittest.TestCase):
    def test_f2a_clk_and_leo_clk_status_bits(self):
        irqs = {}
        bus = CxdPeripheralBus(
            set_irq=lambda irq, lvl: irqs.__setitem__(irq, lvl),
            mem_read=lambda addr, n: b'\x00' * n,
            mem_write=lambda addr, data: None,
        )
        for base in F2A_BASES:
            self.assertEqual(bus.read(base + 0x04) & 0x1, 0x1)
            self.assertEqual(bus.read(base + 0x2C) & 0x2, 0x2)
            bus.write(base + 0x30, 0x1)
            self.assertEqual(bus.read(base + 0x30), 0)
        self.assertEqual(bus.read(LEO_CLK_BASE + 0x50) & 0x1, 0x1)
        self.assertEqual(bus.read(LEO_CLK_BASE + 0x54) & 0x1, 0x1)
        self.assertEqual(bus.read(LEO_CLK_BASE + 0x58) & 0x1, 0x1)
        # DWC3 unmodelled window must read 0 after writes (matches QEMU E0579)
        self.assertTrue(bus.write(0xF020C704, 0x40000000))
        self.assertEqual(bus.read(0xF020C704), 0)

    def test_ldec_lz77_decode_and_xdmac_transfer(self):
        # Literal block kind=0x0F: 1 skip byte, 4-byte count LE, payload b'SONY'
        compressed = bytes([0x0F, 0x00, 0x04, 0x00]) + b'SONY'
        self.assertEqual(ldec_lz77_decode(compressed, 64), b'SONY')

        ram = bytearray(0x4000)
        ram[0x100:0x100 + len(compressed)] = compressed
        irqs = {}

        def mem_read(addr, n):
            return bytes(ram[addr:addr + n])

        def mem_write(addr, data):
            ram[addr:addr + len(data)] = data

        bus = CxdPeripheralBus(
            set_irq=lambda irq, lvl: irqs.__setitem__(irq, lvl),
            mem_read=mem_read,
            mem_write=mem_write,
        )
        # Program LDEC records=1, dst_phys=0x1000
        bus.write(LDEC_BASE + 0x0C, 1)
        bus.write(LDEC_BASE + 0x10, 0x1000)
        # Program XDMAC0 ch0: TBC=len-1, SSA=0x100, DSA=LDEC_IN_BASE, kick +0x18
        bus.write(XDMAC0_BASE + 0x10 + 0x00, len(compressed) - 1)
        bus.write(XDMAC0_BASE + 0x10 + 0x04, 0x100)
        bus.write(XDMAC0_BASE + 0x10 + 0x08, LDEC_IN_BASE)
        bus.write(XDMAC0_BASE + 0x10 + 0x18, 1)

        self.assertEqual(bytes(ram[0x1000:0x1004]), b'SONY')
        self.assertTrue(irqs.get(LDEC_IRQ))
        self.assertTrue(irqs.get(XDMAC0_IRQ_BASE))
        # Acknowledge LDEC and XDMAC0 ch0 IRQs
        bus.write(LDEC_BASE + 0x30, 0x1)
        bus.write(XDMAC0_BASE + 0x10 + 0x20, 0)
        self.assertFalse(irqs.get(LDEC_IRQ))
        self.assertFalse(irqs.get(XDMAC0_IRQ_BASE))


if __name__ == '__main__':
    unittest.main()
