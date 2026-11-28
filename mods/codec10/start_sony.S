@ Entry for binaries linked against the camera's own glibc 2.11 (tools/sony_sysroot) with
@ -nostdlib: the standard ARM crt1 sequence, so argv, atexit and stdio flushing work.
    .syntax unified
    .arm
    .global _start
    .type _start, %function
_start:
    mov fp, #0
    mov lr, #0
    pop {r1}                @ argc
    mov r2, sp              @ argv
    push {r2}               @ stack_end
    push {r0}               @ rtld_fini from the dynamic loader
    mov ip, #0
    push {ip}               @ fini
    mov r3, #0              @ init
    ldr r0, =main
    bl __libc_start_main
    udf #0
    .section .note.GNU-stack,"",%progbits
