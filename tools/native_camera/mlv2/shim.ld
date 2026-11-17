/* Mod Loader v2 shim: /usr/lib/libtestcmd.so RX zero tail (live 0x0566275c..0x05663000). */
SECTIONS
{
    . = 0x05662760;
    .text : {
        *(.text.slot)
        *(.text.slotb)
        *(.text*)
        *(.rodata*)
        . = ALIGN(4);
    }
    ASSERT(. <= 0x05663000, "shim exceeds the libtestcmd.so cave")
    /DISCARD/ : { *(.ARM.exidx*) *(.ARM.extab*) *(.comment*) *(.note*) *(.data*) *(.bss*) }
}
