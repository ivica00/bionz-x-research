"""Minimal Unicorn machine runner checks (E0416)."""
import struct
import unittest

from unicorn.arm_const import UC_ARM_REG_CPSR

from src.a6400.execution.arm_atags import build_atags
from src.a6400.execution.unicorn_machine import UnicornMachine

MACHINE_ID = 0x0DA9


def synthetic_kernel_program():
    """A tiny 'kernel': reads PL011 FR, writes 'AB' to DR, then faults on purpose."""
    image = bytearray(0x200)
    words = [
        0xE321F0D3,  # msr cpsr_c, #0xd3  (required ARM kernel head word)
        0xE59F0028,  # ldr r0, [pc, #0x28] -> 0x34 UART0
        0xE5904018,  # ldr r4, [r0, #0x18]  FR read (MMIO hook)
        0xE59F1024,  # ldr r1, [pc, #0x24] -> 0x38 'A'
        0xE5801000,  # str r1, [r0]
        0xE59F2020,  # ldr r2, [pc, #0x20] -> 0x3c 'B'
        0xE5802000,  # str r2, [r0]
        0xE59F301C,  # ldr r3, [pc, #0x1c] -> 0x40 unmapped address
        0xE5831000,  # str r1, [r3]        (faults -> run stops)
        0xEAFFFFFE,  # b .
    ]
    for index, word in enumerate(words):
        struct.pack_into('<I', image, index * 4, word)
    struct.pack_into('<4I', image, 0x34, 0xF2000000, 0x41, 0x42, 0xDEAD0000)
    return bytes(image)


class UnicornMachineTests(unittest.TestCase):
    def make_machine(self, **kwargs):
        return UnicornMachine(
            synthetic_kernel_program(), b'\x00' * 64,
            build_atags('console=ttyAM0', [(0, 0x40000000)]),
            machine_id=MACHINE_ID, instruction_limit=1000, data_abort_delivery=False, **kwargs)

    def test_uart_serial_capture_and_unmapped_stop(self):
        report = self.make_machine().run()
        self.assertEqual(report['serial_text'], 'AB')
        self.assertEqual(report['stop_reason'], 'unmapped_access')
        self.assertEqual(len(report['unmapped']), 1)
        self.assertEqual(report['unmapped'][0]['address'], '0xdead0000')
        self.assertEqual(report['machine_id'], MACHINE_ID)
        # The PL011 FR register is pre-seeded and read once by the program.
        self.assertEqual(report['stub_hits'], {})

    def test_labelled_mmio_read_stub_is_reported(self):
        machine = self.make_machine(mmio_read_stubs={0xF2000018: 0x90})
        report = machine.run()
        self.assertEqual(report['serial_text'], 'AB')
        self.assertIn('0xf2000018', report['stub_hits'])
        self.assertGreater(report['stub_hits']['0xf2000018'], 0)

    def test_kernel_head_word_is_required(self):
        with self.assertRaises(Exception):
            UnicornMachine(b'\x00' * 0x100, b'\x00', b'', machine_id=MACHINE_ID)

    def test_async_abort_mask_is_opt_in_and_survives_kernel_head(self):
        for enabled, expected in ((False, 0xD3), (True, 0x1D3)):
            with self.subTest(enabled=enabled):
                machine = self.make_machine(mask_async_abort_at_entry=enabled)
                self.assertEqual(machine.uc.reg_read(UC_ARM_REG_CPSR), expected)
                machine.uc.emu_start(machine.kernel_load, machine.kernel_load + 4, count=1)
                self.assertEqual(machine.uc.reg_read(UC_ARM_REG_CPSR), expected)

    def test_vfp_d16_mvfr0_emulation_is_opt_in(self):
        import struct
        from unicorn.arm_const import UC_ARM_REG_R1
        # Put a vmrs r1, mvfr0 (0xEEF71A10) at kernel_load + 0x100
        kernel = bytearray(b'\xD3\xF0\x21\xE3' + b'\x00' * 0x200)
        struct.pack_into('<I', kernel, 0x100, 0xEEF71A10)
        symbols = {'__boottime_vfp_init': 0x60108000 + 0xE0}
        default_machine = UnicornMachine(
            bytes(kernel), b'\x00', b'', machine_id=MACHINE_ID, symbols=symbols
        )
        self.assertFalse(default_machine.vfp_d16_mvfr0)
        self.assertEqual(default_machine.vfp_mvfr0_sites, [])

        parity_machine = UnicornMachine(
            bytes(kernel), b'\x00', b'', machine_id=MACHINE_ID, symbols=symbols,
            qemu_cpu_parity=True,
        )
        self.assertTrue(parity_machine.vfp_d16_mvfr0)
        self.assertTrue(parity_machine.mask_async_abort_on_irq)
        self.assertEqual(parity_machine.entry_cpsr, 0x1D3)
        self.assertEqual(parity_machine.entry_scr, 0x31)
        self.assertEqual(parity_machine.vfp_mvfr0_sites, [f'0x{0x60108100:08x}'])
        parity_machine.uc.reg_write(UC_ARM_REG_R1, 0)
        # __boottime_vfp_init runs with MMU on at virtual 0x60108100.
        parity_machine.uc.mem_map(0x60108000, 0x1000)
        parity_machine.uc.mem_write(0x60108100, struct.pack('<I', 0xEEF71A10))
        parity_machine.uc.emu_start(0x60108100, 0x60108104, count=1)
        self.assertEqual(parity_machine.uc.reg_read(UC_ARM_REG_R1), 0x10110221)
        self.assertEqual(parity_machine.vfp_mvfr0_d16_hits, 1)

    def test_oracle_mmio_trace_replays_sequence_and_holds_last(self):
        import struct
        from unicorn.arm_const import UC_ARM_REG_R0
        # ldr r0, [pc, #0x10] (addr 0xF2916000); ldr r1, [r0]; ldr r2, [r0]; ldr r3, [r0]
        kernel = bytearray(b'\xD3\xF0\x21\xE3' + b'\x00' * 0x100)
        struct.pack_into('<5I', kernel, 0x40,
                         0xE59F0008,  # ldr r0, [pc, #8] -> 0x00108050
                         0xE5901000,  # ldr r1, [r0]
                         0xE5902000,  # ldr r2, [r0]
                         0xE5903000,  # ldr r3, [r0]
                         0xF2916000)  # literal
        machine = UnicornMachine(
            bytes(kernel), b'\x00', b'', machine_id=MACHINE_ID,
            oracle_mmio_trace={0xF2916000: [0x11, 0x22]},
        )
        machine.uc.emu_start(0x00108040, 0x00108050, count=4)
        from unicorn.arm_const import UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3
        self.assertEqual(machine.uc.reg_read(UC_ARM_REG_R1), 0x11)
        self.assertEqual(machine.uc.reg_read(UC_ARM_REG_R2), 0x22)
        self.assertEqual(machine.uc.reg_read(UC_ARM_REG_R3), 0x22)
        self.assertEqual(machine.oracle_mmio_hits[0xF2916000], 3)


if __name__ == '__main__':
    unittest.main()
