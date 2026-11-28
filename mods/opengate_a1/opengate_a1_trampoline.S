.syntax unified
.thumb

/* Mod Loader v2 hook slots (patched at load time with original code + jump back) */
.global ml_orig_tkhd
.type ml_orig_tkhd, %function
ml_orig_tkhd:
    nop
    nop
    nop
    nop
    nop
    nop
    nop
    nop

.global ml_orig_stsd
.type ml_orig_stsd, %function
ml_orig_stsd:
    nop
    nop
    nop
    nop
    nop
    nop
    nop
    nop

/* Hook at libmpr.so +0x8612AC (captured camera build; the 2.00 updater build has it at +0x87072C) (r0=model, r1=w_16_16, r2=h_16_16, r3=kind) */
.global tkhd_hook_tramp
.type tkhd_hook_tramp, %function
.thumb_func
tkhd_hook_tramp:
    push {r4, lr}
    adr r4, ml_orig_tkhd
    orr r4, r4, #1
    bl tkhd_hook_c
    pop {r4, pc}

/* Hook at libmpr.so +0x8615F4 (captured camera build; updater +0x870A74) (r0=ctx, r1=desc_ptr, r2=arg2) */
.global stsd_hook_tramp
.type stsd_hook_tramp, %function
.thumb_func
stsd_hook_tramp:
    push {r4, lr}
    adr r4, ml_orig_stsd
    orr r4, r4, #1
    bl stsd_hook_c
    pop {r4, pc}
