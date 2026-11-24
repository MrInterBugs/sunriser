"""The Cortex-M3/M4 core peripherals Unicorn leaves out: SysTick, NVIC, SCB, ITM and DWT, and exception entry and
return. Unicorn executes the instructions; exceptions are taken here between runs and returned from in an interrupt
hook (the CPU reports a branch to an EXC_RETURN value as interrupt 8)."""
import struct
from unicorn import UC_HOOK_INTR
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_R12,
                               UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_XPSR, UC_ARM_REG_SP, UC_ARM_REG_CONTROL,
                               UC_ARM_REG_PRIMASK, UC_ARM_REG_BASEPRI, UC_ARM_REG_FAULTMASK, UC_ARM_REG_FPSCR,
                               UC_ARM_REG_S0)

FRAME = (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_R12, UC_ARM_REG_LR, UC_ARM_REG_PC,
         UC_ARM_REG_XPSR)
S_REGS = tuple(UC_ARM_REG_S0 + i for i in range(16))
EXCP_UDEF, EXCP_SWI, EXCP_PREFETCH_ABORT, EXCP_DATA_ABORT, EXCP_BKPT, EXCP_EXCEPTION_EXIT = 1, 2, 3, 4, 7, 8
EXCP_NOCP, EXCP_INVSTATE, EXCP_UNALIGNED, EXCP_HLT = 17, 18, 22, 0x10001
HARDFAULT, SVCALL, PENDSV, SYSTICK = 3, 11, 14, 15
CONTROL_SPSEL, CONTROL_FPCA = 2, 4
NAMES = {2: 'NMI', 3: 'HardFault', 4: 'MemManage', 5: 'BusFault', 6: 'UsageFault', 11: 'SVCall', 14: 'PendSV',
         15: 'SysTick'}


class Fault(Exception):
    pass


class SCS:
    """System control space (0xE000E000-0xE000EFFF) and the exception state of the core."""

    def __init__(self, m, fpu):
        self.m = m
        self.fpu = fpu
        self.uc = m.uc
        self.uc.hook_add(UC_HOOK_INTR, self._intr)
        self.reset()

    def reset(self):
        self.iser = self.ispr = self.levels = 0
        self.ipr = bytearray(96)
        self.shpr = bytearray(16)              # by exception number (4..15)
        self.prigroup = 0
        self.vtor = 0
        self.ccr = 0x200                       # STKALIGN
        self.cpacr = self.fpccr = 0
        self.scr = self.shcsr = self.cfsr = self.hfsr = 0
        self.pend_st = self.pend_sv = False
        self.active = []                        # [(exception, EXC_RETURN)], innermost last
        self.st_csr = self.st_rvr = 0
        self.st_start = self.m.now()            # cycle at which the counter was last reloaded from RVR
        self.st_countflag = False
        self.st_owed = 0                        # ticks due but not yet taken (a slice ran long)

    # -- SysTick --
    def _st_period(self):
        return (self.st_rvr + 1) * (1 if self.st_csr & 4 else 8)

    def next_event(self):
        if self.st_csr & 1 and self.st_rvr:
            return self.st_start + self._st_period()
        return None

    def advance(self, now):
        if not (self.st_csr & 1 and self.st_rvr):
            return
        period = self._st_period()
        if now >= self.st_start + period:
            n = (now - self.st_start) // period
            self.st_start += n * period
            self.st_countflag = True
            if self.st_csr & 2:
                self.st_owed = min(self.st_owed + n, 50)
                self.pend_st = True

    # -- registers --
    def read(self, off, size):
        if 0x400 <= off < 0x460 and size == 1:
            return self.ipr[off - 0x400]
        if 0xD18 <= off < 0xD24 and size == 1:
            return self.shpr[off - 0xD14]
        v = self._read32(off & ~3)
        return (v >> ((off & 3) * 8)) & ((1 << (size * 8)) - 1)

    def _read32(self, a):
        if a == 0x010:
            v = self.st_csr | (0x10000 if self.st_countflag else 0)
            self.st_countflag = False
            return v
        if a == 0x014:
            return self.st_rvr
        if a == 0x018:
            if not (self.st_csr & 1) or not self.st_rvr:
                return 0
            div = 1 if self.st_csr & 4 else 8
            return self.st_rvr - ((self.m.now() - self.st_start) // div) % (self.st_rvr + 1)
        if a == 0x01C:
            return 0x40000000 | (self.m.board.hz // 8000 - 1)  # NOREF clear, TENMS for 1 ms at HCLK/8
        if 0x100 <= a < 0x10C or 0x180 <= a < 0x18C:
            return (self.iser >> ((a & 0x7F) * 8)) & 0xFFFFFFFF
        if 0x200 <= a < 0x20C or 0x280 <= a < 0x28C:
            return ((self.ispr | (self.levels & ~self._active_irqs())) >> ((a & 0x7F) * 8)) & 0xFFFFFFFF
        if 0x300 <= a < 0x30C:
            return (self._active_irqs() >> ((a - 0x300) * 8)) & 0xFFFFFFFF
        if 0x400 <= a < 0x460:
            return int.from_bytes(self.ipr[a - 0x400:a - 0x3FC], 'little')
        if a == 0xD00:
            return 0x410FC241 if self.fpu else 0x412FC231
        if a == 0xD04:
            v = self.active[-1][0] if self.active else 0
            pending = self._pending_list()
            if pending:
                v |= min(pending) << 12
                if any(e >= 16 for e in pending):
                    v |= 1 << 22
            return v | (1 << 26 if self.pend_st else 0) | (1 << 28 if self.pend_sv else 0)
        if a == 0xD08:
            return self.vtor
        if a == 0xD0C:
            return 0xFA050000 | (self.prigroup << 8)
        if a == 0xD10:
            return self.scr
        if a == 0xD14:
            return self.ccr
        if 0xD18 <= a < 0xD24:
            return int.from_bytes(self.shpr[a - 0xD14:a - 0xD10], 'little')
        if a == 0xD24:
            return self.shcsr
        if a == 0xD28:
            return self.cfsr
        if a == 0xD2C:
            return self.hfsr
        if a == 0xD88:
            return self.cpacr
        if a == 0xF34:
            return self.fpccr or (0xC0000000 if self.fpu else 0)
        return 0

    def write(self, off, size, value):
        if 0x400 <= off < 0x460 and size == 1:
            self.ipr[off - 0x400] = value & 0xF0
            return
        if 0xD18 <= off < 0xD24 and size == 1:
            self.shpr[off - 0xD14] = value & 0xF0
            return
        a = off & ~3
        if size != 4:
            sh = (off & 3) * 8
            value = (value & ((1 << (size * 8)) - 1)) << sh
        if a == 0x010:
            if (value & 1) and not (self.st_csr & 1):
                self.st_start = self.m.now()
            self.st_csr = value & 7
        elif a == 0x014:
            self.st_rvr = value & 0xFFFFFF
        elif a == 0x018:
            self.st_start = self.m.now()
            self.st_countflag = False
        elif 0x100 <= a < 0x10C:
            self.iser |= value << ((a - 0x100) * 8)
        elif 0x180 <= a < 0x18C:
            self.iser &= ~(value << ((a - 0x180) * 8))
        elif 0x200 <= a < 0x20C:
            self.ispr |= value << ((a - 0x200) * 8)
        elif 0x280 <= a < 0x28C:
            self.ispr &= ~(value << ((a - 0x280) * 8))
        elif 0x400 <= a < 0x460:
            self.ipr[a - 0x400:a - 0x3FC] = (value & 0xF0F0F0F0).to_bytes(4, 'little')
        elif a == 0xD04:
            if value & (1 << 28):
                self.pend_sv = True
            if value & (1 << 27):
                self.pend_sv = False
            if value & (1 << 26):
                self.pend_st = True
            if value & (1 << 25):
                self.pend_st = False
        elif a == 0xD08:
            self.vtor = value & 0xFFFFFF80
        elif a == 0xD0C:
            if value >> 16 == 0x05FA:
                self.prigroup = (value >> 8) & 7
                if value & 4:
                    self.m.request_reset('software (SYSRESETREQ)')
        elif a == 0xD10:
            self.scr = value
        elif a == 0xD14:
            self.ccr = value
        elif 0xD18 <= a < 0xD24:
            self.shpr[a - 0xD14:a - 0xD10] = (value & 0xF0F0F0F0).to_bytes(4, 'little')
        elif a == 0xD24:
            self.shcsr = value
        elif a == 0xD28:
            self.cfsr &= ~value
        elif a == 0xD2C:
            self.hfsr &= ~value
        elif a == 0xD88:
            self.cpacr = value
        elif a == 0xF34:
            self.fpccr = value
        elif a == 0xF00:
            self.ispr |= 1 << (value & 0x1FF)

    # -- interrupt lines --
    def set_irq(self, irq, level):
        if level:
            self.levels |= 1 << irq
        else:
            self.levels &= ~(1 << irq)

    def _active_irqs(self):
        v = 0
        for e, _ in self.active:
            if e >= 16:
                v |= 1 << (e - 16)
        return v

    def _priority(self, e):
        if e == 2:
            return -2
        if e == HARDFAULT:
            return -1
        return self.shpr[e] if e < 16 else self.ipr[e - 16]

    def _group(self, p):
        return p if p < 0 else p & ((0xFF << (self.prigroup + 1)) & 0xF0)

    def _pending_list(self):
        self.ispr |= self.levels & ~self._active_irqs()
        out = [SYSTICK] if self.pend_st else []
        if self.pend_sv:
            out.append(PENDSV)
        ready = self.ispr & self.iser
        while ready:
            bit = ready & -ready
            out.append(16 + bit.bit_length() - 1)
            ready ^= bit
        return out

    def _execution_priority(self):
        uc = self.uc
        if uc.reg_read(UC_ARM_REG_FAULTMASK) & 1:
            return -1
        p = 256
        for e, _ in self.active:
            p = min(p, self._group(self._priority(e)))
        if uc.reg_read(UC_ARM_REG_PRIMASK) & 1:
            p = min(p, 0)
        basepri = uc.reg_read(UC_ARM_REG_BASEPRI) & 0xF0
        if basepri:
            p = min(p, self._group(basepri))
        return p

    def dispatch(self):
        """Take the most urgent pending exception if it preempts what runs now; repeat for nested preemption."""
        while self.pend_st or self.pend_sv or (self.ispr | self.levels) & self.iser:
            pending = self._pending_list()
            if not pending:
                return
            best = min(pending, key=lambda e: (self._group(self._priority(e)), self._priority(e), e))
            if self._group(self._priority(best)) >= self._execution_priority():
                return
            if best == SYSTICK:
                self.st_owed = max(0, self.st_owed - 1)
                self.pend_st = False
            elif best == PENDSV:
                self.pend_sv = False
            else:
                self.ispr &= ~(1 << (best - 16))
            self.enter(best)

    # -- exception entry and return --
    def _vector(self, e):
        base = self.vtor or self.m.board.flash_base
        return struct.unpack('<I', bytes(self.uc.mem_read(base + 4 * e, 4)))[0]

    def enter(self, e, return_pc=None):
        uc = self.uc
        control = uc.reg_read(UC_ARM_REG_CONTROL)
        thread = not self.active
        if thread and control & CONTROL_SPSEL:
            raise Fault('exception entry from thread mode on the process stack is not modelled')
        fp = self.fpu and bool(control & CONTROL_FPCA)
        size = 0x68 if fp else 0x20
        sp = uc.reg_read(UC_ARM_REG_SP)
        frame = (sp - size) & ~7
        regs = [uc.reg_read(r) for r in FRAME]
        if return_pc is not None:
            regs[6] = return_pc
        regs[6] &= ~1
        regs[7] = (regs[7] & ~(1 << 9)) | ((1 << 9) if (sp - size) & 4 else 0)
        data = struct.pack('<8I', *regs)
        if fp:
            data += struct.pack('<16I', *(uc.reg_read(r) for r in S_REGS))
            data += struct.pack('<II', uc.reg_read(UC_ARM_REG_FPSCR), 0)
        uc.mem_write(frame, data)
        uc.reg_write(UC_ARM_REG_SP, frame)
        exc_return = 0xFFFFFFE1 | (0 if fp else 0x10) | (8 if thread else 0)
        uc.reg_write(UC_ARM_REG_LR, exc_return)
        uc.reg_write(UC_ARM_REG_XPSR, 0x01000000 | e)
        if fp:
            uc.reg_write(UC_ARM_REG_CONTROL, control & ~CONTROL_FPCA)
        handler = self._vector(e)
        if not handler & 1:
            raise Fault(f'vector of exception {e} is {handler:#010x}')
        uc.reg_write(UC_ARM_REG_PC, handler | 1)
        self.active.append((e, exc_return))

    def _exit(self):
        uc = self.uc
        if not self.active:
            raise Fault(f'exception return with no active exception (PC {uc.reg_read(UC_ARM_REG_PC):#010x})')
        e, exc_return = self.active.pop()
        if e == SYSTICK and self.st_owed:
            self.pend_st = True                 # catch up with ticks missed while a slice ran long
        fp = not exc_return & 0x10
        sp = uc.reg_read(UC_ARM_REG_SP)
        regs = list(struct.unpack('<8I', bytes(uc.mem_read(sp, 32))))
        if fp:
            sregs = struct.unpack('<18I', bytes(uc.mem_read(sp + 0x20, 72)))
            for r, v in zip(S_REGS, sregs):
                uc.reg_write(r, v)
            uc.reg_write(UC_ARM_REG_FPSCR, sregs[16])
        xpsr = regs[7]
        for r, v in zip(FRAME[:7], regs[:7]):
            uc.reg_write(r, v)
        uc.reg_write(UC_ARM_REG_XPSR, xpsr & ~(1 << 9))
        uc.reg_write(UC_ARM_REG_PC, regs[6] | 1)
        uc.reg_write(UC_ARM_REG_SP, sp + (0x68 if fp else 0x20) + (4 if xpsr & (1 << 9) else 0))
        if fp and exc_return & 8:
            uc.reg_write(UC_ARM_REG_CONTROL, uc.reg_read(UC_ARM_REG_CONTROL) | CONTROL_FPCA)

    def _intr(self, uc, intno, _):
        # Every case ends the slice: the next one starts where the hook has left the PC.
        try:
            if intno == EXCP_EXCEPTION_EXIT:
                self._exit()
            elif intno == EXCP_HLT:                       # WFI/WFE: time moves on to the next event
                self.m.idle = True
            elif intno == EXCP_SWI:
                self.enter(SVCALL, uc.reg_read(UC_ARM_REG_PC))
            else:
                self.m.cpu_fault(f'CPU exception {intno} (undefined instruction, bad state or no coprocessor)')
        except Fault as f:
            self.m.cpu_fault(str(f))
        self.m.stop()


class PPB:
    """Debug components on the private peripheral bus: ITM (to the console), DWT's cycle counter and the STM32 DBGMCU
    block."""

    def __init__(self, m, output):
        self.m = m
        self.output = output
        self.dwt_ctrl = self.demcr = 0
        self.dwt_base = 0

    def read(self, off, size):
        if off < 0x80:                     # ITM stimulus ports: FIFO always ready
            return 1
        if off == 0x1004:
            return (self.m.now() - self.dwt_base) & 0xFFFFFFFF
        if off == 0x1000:
            return self.dwt_ctrl
        if off == 0x42000:                 # DBGMCU_IDCODE
            return 0x10016418 if self.m.board.number == 8 else 0x10076413
        return 0

    def write(self, off, size, value):
        if off < 0x80:
            self.output(off >> 2, value & 0xFF)
        elif off == 0x1004:
            self.dwt_base = self.m.now() - value
        elif off == 0x1000:
            self.dwt_ctrl = value
