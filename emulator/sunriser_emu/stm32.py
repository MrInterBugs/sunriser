"""STM32F107 (SunRiser 8) and STM32F407 (SunRiser 10) peripherals, modelled at register level as far as the firmware
uses them. Unmodelled registers keep what is written. Times are CPU cycles (Machine.now())."""
import calendar
import time

MASKS = {1: 0xFF, 2: 0xFFFF, 4: 0xFFFFFFFF}


class Regs:
    """Registers as storage; subclasses intercept single 32-bit registers in rd() and wr()."""

    def __init__(self, m, name):
        self.m = m
        self.name = name
        self.r = {}
        self.reset()

    def reset(self):
        self.r = {}

    def read(self, off, size):
        v = self.rd(off & ~3)
        if size != 4 or off & 3:
            v = (v >> ((off & 3) * 8)) & MASKS[size]
        return v

    def read_cb(self, uc, off, size, _):
        v = self.rd(off & ~3)
        if size != 4 or off & 3:
            v = (v >> ((off & 3) * 8)) & MASKS[size]
        return v

    def write_cb(self, uc, off, size, value, _):
        self.write(off, size, value)

    def write(self, off, size, value):
        a = off & ~3
        if size != 4 or off & 3:
            sh = (off & 3) * 8
            mask = MASKS[size] << sh
            value = (self.r.get(a, 0) & ~mask) | ((value << sh) & mask)
        self.wr(a, value)

    def rd(self, a):
        return self.r.get(a, 0)

    def wr(self, a, v):
        self.r[a] = v


class Rcc(Regs):
    """Clocks are ready as soon as they are switched on; the reset flags and the backup domain control register
    belong to the machine (they survive a system reset)."""

    def __init__(self, m, name, f4):
        self.f4 = f4
        self.CFGR, self.BDCR, self.CSR = (0x08, 0x70, 0x74) if f4 else (0x04, 0x20, 0x24)
        self.RESETS = {0x10: 'AHB1', 0x20: 'APB1', 0x24: 'APB2'} if f4 else {0x28: 'AHB', 0x10: 'APB1', 0x0C: 'APB2'}
        super().__init__(m, name)

    def reset(self):
        self.r = {0x00: 0x83}

    def rd(self, a):
        v = self.r.get(a, 0)
        if a == 0x00:
            return v | ((v & 0x15010001) << 1)
        if a == self.CFGR:
            return (v & ~0xC) | ((v & 3) << 2)
        if a == self.BDCR:
            return self.m.backup.bdcr | ((self.m.backup.bdcr & 1) << 1)
        if a == self.CSR:
            return self.m.reset_flags | (v & 1) | ((v & 1) << 1)
        return v

    def wr(self, a, v):
        if a == self.BDCR:
            if v & (1 << 16):
                self.m.backup.domain_reset()
            self.m.backup.bdcr = v & ~((1 << 16) | 2)
            return
        if a == self.CSR and v & (1 << 24):
            self.m.reset_flags = 0
        if a in self.RESETS:
            for bit in range(32):
                if v & (1 << bit) and not self.r.get(a, 0) & (1 << bit):
                    p = self.m.reset_lines.get((self.RESETS[a], bit))
                    if p:
                        p.reset()
        self.r[a] = v


class Flash(Regs):
    """The flash interface: unlock, page (F1) or sector (F4) erase, programming by stores into the flash array."""
    KEYS = (0x45670123, 0xCDEF89AB)

    def __init__(self, m, name, f4):
        self.f4 = f4
        super().__init__(m, name)

    def reset(self):
        self.r = {}
        self.locked = True
        self.key_step = 0
        self.sr = 0
        self.cr = 0

    def rd(self, a):
        if self.m.fixups:
            self.m._apply_fixups()
        if a == 0x0C:
            return self.sr
        if a == 0x10:
            return self.cr | ((1 << 31 if self.f4 else 1 << 7) if self.locked else 0)
        return self.r.get(a, 0)

    def wr(self, a, v):
        if a == 0x04:
            if v == self.KEYS[self.key_step]:
                self.key_step += 1
                if self.key_step == 2:
                    self.locked = False
                    self.key_step = 0
            else:
                self.key_step = 0
        elif a == 0x0C:
            self.sr &= ~(v & (0xF3 if self.f4 else 0x34))
        elif a == 0x10:
            lock = (1 << 31) if self.f4 else (1 << 7)
            if v & lock:
                self.locked = True
            if self.locked:
                self.cr = 0
                return
            self.cr = v & ~lock & ~(1 << 16 if self.f4 else 1 << 6)
            if v & (1 << 16 if self.f4 else 1 << 6):
                self._start(v)
        else:
            self.r[a] = v

    def _start(self, cr):
        m = self.m
        if self.f4:
            if cr & 4:
                m.flash_erase(0, m.board.flash_size)
            elif cr & 2:
                start, size = self.sector((cr >> 3) & 0xF)
                m.flash_erase(start, size)
        else:
            if cr & 4:
                m.flash_erase(0, m.board.flash_size)
            elif cr & 2:
                page = (self.r.get(0x14, 0) - m.board.flash_base) & ~0x7FF
                m.flash_erase(page, 0x800)
        self.sr |= 1 if self.f4 else 0x20      # EOP

    @staticmethod
    def sector(n):
        if n < 4:
            return n * 0x4000, 0x4000
        if n == 4:
            return 0x10000, 0x10000
        return 0x20000 * (n - 4), 0x20000

    def program(self, old, new):
        """The bytes a programming store leaves in the array, given the bytes there before and the bytes stored."""
        if self.locked or not self.cr & 1:
            self.sr |= 0x10 if self.f4 else 0x04
            self.m.log(f'flash: store while not programming ({"locked" if self.locked else "PG clear"}), ignored')
            return old
        if not self.f4 and old != b'\xff' * len(old) and new != bytes(len(new)):
            self.sr |= 0x04                    # PGERR: not erased
            return old
        self.sr |= 1 if self.f4 else 0x20
        return bytes(a & b for a, b in zip(old, new))


class Gpio(Regs):
    """A GPIO port. Inputs read high (the board's pull-ups) unless a device drives them."""

    def __init__(self, m, name, f4):
        self.f4 = f4
        self.IDR, self.ODR, self.BSRR = (0x10, 0x14, 0x18) if f4 else (0x08, 0x0C, 0x10)
        self.watch = {}                         # pin -> callback(level), on output changes
        self.ext = 0xFFFF
        super().__init__(m, name)

    def reset(self):
        self.r = {} if self.f4 else {0x00: 0x44444444, 0x04: 0x44444444}
        self.odr = 0

    def outputs(self):
        mask = 0
        if self.f4:
            moder = self.r.get(0x00, 0)
            for pin in range(16):
                if (moder >> (2 * pin)) & 3 == 1:
                    mask |= 1 << pin
        else:
            cfg = self.r.get(0x00, 0x44444444) | (self.r.get(0x04, 0x44444444) << 32)
            for pin in range(16):
                if (cfg >> (4 * pin)) & 3:
                    mask |= 1 << pin
        return mask

    def rd(self, a):
        if a == self.IDR:
            out = self.outputs()
            return (self.odr & out) | (self.ext & ~out & 0xFFFF)
        if a == self.ODR:
            return self.odr
        return self.r.get(a, 0)

    def _set(self, odr):
        changed = (odr ^ self.odr) & 0xFFFF
        self.odr = odr & 0xFFFF
        for pin, cb in self.watch.items():
            if changed & (1 << pin):
                cb(bool(self.odr & (1 << pin)))

    def wr(self, a, v):
        if a == self.ODR:
            self._set(v)
        elif a == self.BSRR:
            self._set((self.odr & ~(v >> 16)) | (v & 0xFFFF))
        elif not self.f4 and a == 0x14:         # BRR
            self._set(self.odr & ~v)
        else:
            self.r[a] = v


class Timer(Regs):
    """A timer: the counter follows the device's time; update events and their interrupt; CCR values are the PWM
    outputs."""

    def __init__(self, m, name, clock, irq=None, bits=16, on_ccr=None):
        self.clock = clock
        self.irq = irq
        self.top = (1 << bits) - 1
        self.on_ccr = on_ccr
        super().__init__(m, name)

    def reset(self):
        self.r = {}
        self.base = 0              # counter value at self.start
        self.start = self.m.now()
        self.sr = 0
        if self.irq is not None:
            self.m.scs.set_irq(self.irq, False)

    def _ticks(self, now):
        return (now - self.start) * self.clock // self.m.board.hz // (self.r.get(0x28, 0) + 1)

    def _period(self):
        return (self.r.get(0x2C, self.top) or self.top) + 1

    def count(self):
        if not self.r.get(0x00, 0) & 1:
            return self.base
        return (self.base + self._ticks(self.m.now())) % self._period()

    def _rebase(self):
        now = self.m.now()
        if self.r.get(0x00, 0) & 1:
            self.base = (self.base + self._ticks(now)) % self._period()
        self.start = now

    def _irq(self):
        if self.irq is not None:
            self.m.scs.set_irq(self.irq, bool(self.sr & self.r.get(0x0C, 0) & 1))

    def next_event(self):
        if self.irq is None or not (self.r.get(0x00, 0) & 1 and self.r.get(0x0C, 0) & 1):
            return None
        ticks = self._period() - self.base % self._period()
        return self.start + -(-ticks * (self.r.get(0x28, 0) + 1) * self.m.board.hz // self.clock)

    def advance(self, now):
        t = self.next_event()
        if t is not None and now >= t:
            self._rebase()
            self.sr |= 1
            self._irq()

    def rd(self, a):
        if a == 0x24:
            return self.count()
        if a == 0x10:
            return self.sr
        return self.r.get(a, 0)

    def wr(self, a, v):
        if a in (0x00, 0x28, 0x2C):
            self._rebase()
            self.r[a] = v
        elif a == 0x24:
            self.base, self.start = v, self.m.now()
        elif a == 0x10:
            self.sr &= v
            self._irq()
        elif a == 0x14:
            if v & 1:                            # UG
                self.base, self.start = 0, self.m.now()
                if not self.r.get(0x00, 0) & 4:  # URS clear: UG sets UIF
                    self.sr |= 1
                    self._irq()
        elif a == 0x0C:
            self.r[a] = v
            self._irq()
        else:
            old = self.r.get(a)
            self.r[a] = v
            if 0x34 <= a <= 0x40 and self.on_ccr and old != v:
                self.on_ccr(self, (a - 0x34) // 4, v)


class Spi(Regs):
    """SPI master, full duplex, 8 bit: a write to DR exchanges a byte with the selected device at once."""

    def __init__(self, m, name, device=None):
        self.device = device
        super().__init__(m, name)

    def reset(self):
        self.r = {}
        self.rx = 0
        self.rxne = False

    def rd(self, a):
        if a == 0x08:
            return 0x02 | self.rxne
        if a == 0x0C:
            self.rxne = False
            return self.rx
        return self.r.get(a, 0)

    def wr(self, a, v):
        if a == 0x0C:
            enabled = self.r.get(0x00, 0) & 0x40
            self.rx = self.device.xfer(v & 0xFF) if enabled and self.device else 0xFF
            self.rxne = True
        else:
            self.r[a] = v


class I2c(Regs):
    """I2C master (the STM32F1/F4 I2C v1) with devices by 7-bit address."""

    def __init__(self, m, name, devices):
        self.devices = devices
        super().__init__(m, name)

    def reset(self):
        self.r = {}
        self.sr1 = 0
        self.sr2 = 0
        self.dev = None
        self.mode = None          # None, 'tx' or 'rx'
        self.addr_pending = False
        self.sr1_read = False
        self.data = None          # byte in DR (receiving)
        self.shift = None         # byte behind it

    def rd(self, a):
        if a == 0x14:
            self.sr1_read = True
            return self.sr1
        if a == 0x18:
            if self.sr1 & 2 and self.sr1_read:
                self.sr1 &= ~2
                if self.mode == 'rx':
                    self._fetch()
                else:
                    self.sr1 |= 0x80
            self.sr1_read = False
            return self.sr2
        if a == 0x10:
            v = self.data if self.data is not None else 0xFF
            self.data, self.shift = self.shift, None
            if self.data is not None and self.r.get(0x00, 0) & (1 << 10) and not self.r.get(0x00, 0) & (1 << 9):
                self.shift = self.dev.read() if self.dev else 0xFF
            self._flags()
            return v
        return self.r.get(a, 0)

    def _fetch(self):
        self.data = self.dev.read() if self.dev else 0xFF
        self.shift = self.dev.read() if self.dev and self.r.get(0x00, 0) & (1 << 10) else None
        self._flags()

    def _flags(self):
        self.sr1 &= ~0x44
        if self.data is not None:
            self.sr1 |= 0x40
            if self.shift is not None:
                self.sr1 |= 0x04

    def wr(self, a, v):
        if a == 0x00:
            if v & (1 << 15):
                self.reset()
                self.r[0x00] = v
                return
            if v & 1 and v & (1 << 8):       # START
                self.sr1 = (self.sr1 & ~0x46) | 1
                self.sr2 |= 3
                self.data = self.shift = None
                v &= ~(1 << 8)
            if v & 1 and v & (1 << 9):       # STOP
                if self.dev:
                    self.dev.stop()
                self.dev = None
                self.mode = None
                self.sr2 &= ~7
                self.sr1 &= ~0x84
                v &= ~(1 << 9)
            if not v & 1:
                self.sr2 &= ~7
            self.r[0x00] = v
        elif a == 0x10:
            if self.sr1 & 1:                  # address phase
                self.sr1 &= ~1
                dev = self.devices.get(v >> 1)
                if dev and dev.start(bool(v & 1)):
                    self.dev = dev
                    self.mode = 'rx' if v & 1 else 'tx'
                    self.sr1 |= 2
                    self.sr2 = (self.sr2 & ~4) | (0 if v & 1 else 4)
                else:
                    self.dev = None
                    self.sr1 |= 0x400         # AF
            elif self.mode == 'tx' and self.dev:
                self.sr1 |= 0x84 if self.dev.write(v & 0xFF) else 0x400
        elif a == 0x14:
            self.sr1 &= v | ~0xDF00            # rc_w0 error flags
        else:
            self.r[a] = v


class Eeprom24:
    """A 24AA02E48: 256 bytes, the EUI-48 at 0xFA."""

    def __init__(self, mac):
        self.mem = bytearray(b'\xff' * 256)
        self.mem[0xFA:0x100] = mac
        self.ptr = 0
        self.first = False

    def start(self, read):
        self.first = not read
        return True

    def write(self, b):
        if self.first:
            self.ptr = b
            self.first = False
        else:
            self.mem[self.ptr] = b if self.ptr < 0x80 else self.mem[self.ptr]   # upper half is write protected
            self.ptr = (self.ptr + 1) & 0xFF
        return True

    def read(self):
        v = self.mem[self.ptr]
        self.ptr = (self.ptr + 1) & 0xFF
        return v

    def stop(self):
        pass


class Usart(Regs):
    """A USART: transmitted bytes go to the attached device (or the console), received bytes come from it."""

    def __init__(self, m, name, irq, device=None):
        self.irq = irq
        self.device = device
        super().__init__(m, name)

    def reset(self):
        self.r = {}
        self.rxq = bytearray()

    def receive(self, data):
        self.rxq += data
        self._irq()

    def _irq(self):
        cr1 = self.r.get(0x0C, 0)
        self.m.scs.set_irq(self.irq, bool(cr1 & (1 << 13) and cr1 & (1 << 5) and self.rxq))

    def rd(self, a):
        if a == 0x00:
            return 0xC0 | (0x20 if self.rxq else 0)
        if a == 0x04:
            v = self.rxq.pop(0) if self.rxq else 0
            self._irq()
            return v
        return self.r.get(a, 0)

    def wr(self, a, v):
        if a == 0x04:
            if self.device:
                self.device.tx(self, v & 0xFF)
        else:
            self.r[a] = v
            if a == 0x0C:
                self._irq()


class Exti(Regs):
    def rd(self, a):
        return self.r.get(a, 0)

    def wr(self, a, v):
        if a == 0x14:
            self.r[a] = self.r.get(a, 0) & ~v
        else:
            self.r[a] = v


class Syscfg(Regs):
    def rd(self, a):
        v = self.r.get(a, 0)
        if a == 0x20:                           # CMPCR: compensation cell ready once powered
            return v | ((v & 1) << 8)
        return v


class Backup:
    """The battery-backed domain: the RTC, backup registers and RCC_BDCR. The RTC counts in the device's time: its
    seconds follow the CPU clock, starting at a value carried over from the state file."""

    def __init__(self, m, state):
        self.m = m
        self.bdcr = state.get('bdcr', 0)
        self.regs = {int(k): v for k, v in state.get('regs', {}).items()}
        saved = state.get('rtc')
        # while the emulator was stopped the battery kept the RTC running
        self.epoch = (saved + max(0, int(time.time()) - state.get('saved_at', int(time.time())))
                      if saved is not None else None)
        self.epoch_cycle = 0

    def domain_reset(self):
        self.bdcr = 0
        self.regs = {}
        self.epoch = None
        self.m.log('backup domain reset')

    def seconds(self):
        """The RTC's current time in seconds (float), or None if it was never started."""
        if self.epoch is None:
            return None
        return self.epoch + (self.m.now() - self.epoch_cycle) / self.m.board.hz

    def set_seconds(self, s):
        self.epoch = s
        self.epoch_cycle = self.m.now()

    def state(self):
        s = self.seconds()
        return {'bdcr': self.bdcr, 'regs': {str(k): v for k, v in self.regs.items()},
                'rtc': int(s) if s is not None else None, 'saved_at': int(time.time())}


class RtcF1(Regs):
    """The STM32F1 RTC: a 32-bit seconds counter with second and alarm flags."""

    def __init__(self, m, name, irq):
        self.irq = irq
        self.last = None
        super().__init__(m, name)

    def reset(self):
        self.r = {}
        self.crh = 0
        self.crl = 0x20
        self.cnf = None

    def _counter(self):
        s = self.m.backup.seconds()
        if s is None:
            self.m.backup.set_seconds(0)
            s = 0
        return int(s)

    def next_event(self):
        s = self.m.backup.seconds()
        if s is None or not self.m.backup.bdcr & (1 << 15):
            return None
        return self.m.now() + max(1, int((int(s) + 1 - s) * self.m.board.hz))

    def advance(self, now):
        if not self.m.backup.bdcr & (1 << 15):
            return
        c = self._counter()
        if self.last is not None and c != self.last:
            self.crl |= 1
            if c == ((self.m.backup.regs.get(-1, 0xFFFFFFFF)) & 0xFFFFFFFF):
                self.crl |= 2
        self.last = c
        self._irq()

    def _irq(self):
        self.m.scs.set_irq(self.irq, bool(self.crh & self.crl & 7))

    def rd(self, a):
        if a == 0x00:
            return self.crh
        if a == 0x04:
            return self.crl | 0x28                       # RTOFF, RSF
        if a in (0x18, 0x1C):
            c = self._counter()
            return (c >> 16) & 0xFFFF if a == 0x18 else c & 0xFFFF
        if a == 0x14:                                    # DIVL: counts down within the second
            s = self.m.backup.seconds() or 0
            return int((1 - (s - int(s))) * 32767) & 0xFFFF
        return self.r.get(a, 0)

    def wr(self, a, v):
        if a == 0x00:
            self.crh = v & 7
            self._irq()
        elif a == 0x04:
            if self.crl & 0x10 and not v & 0x10 and self.cnf:   # leaving configuration mode applies writes
                c = self._counter()
                hi, lo = self.cnf.get(0x18, c >> 16), self.cnf.get(0x1C, c & 0xFFFF)
                if 0x18 in self.cnf or 0x1C in self.cnf:
                    self.m.backup.set_seconds((hi << 16) | lo)
                    self.last = (hi << 16) | lo
                if 0x20 in self.cnf or 0x24 in self.cnf:
                    old = self.m.backup.regs.get(-1, 0xFFFFFFFF)
                    self.m.backup.regs[-1] = ((self.cnf.get(0x20, old >> 16) << 16) | self.cnf.get(0x24, old & 0xFFFF))
                self.cnf = None
            if v & 0x10:
                self.cnf = self.cnf or {}
            self.crl = (self.crl & ~7 & ~0x10) | (self.crl & v & 7) | (v & 0x10)
            self._irq()
        elif a in (0x18, 0x1C, 0x20, 0x24):
            if self.crl & 0x10:
                self.cnf[a] = v & 0xFFFF
        else:
            self.r[a] = v


class BkpF1(Regs):
    """STM32F1 backup registers (DR1..DR42)."""

    def rd(self, a):
        if 0x04 <= a <= 0x28 or 0x40 <= a <= 0xBC:
            return self.m.backup.regs.get(a, 0)
        return self.r.get(a, 0)

    def wr(self, a, v):
        if 0x04 <= a <= 0x28 or 0x40 <= a <= 0xBC:
            if self.m.pwr_dbp():
                self.m.backup.regs[a] = v & 0xFFFF
        else:
            self.r[a] = v


def _bcd(n):
    return ((n // 10) << 4) | (n % 10)


def _unbcd(b):
    return (b >> 4) * 10 + (b & 0xF)


class RtcF4(Regs):
    """The STM32F4 RTC: a BCD calendar (two-digit year), initialization mode, the wakeup timer and backup registers."""

    def __init__(self, m, name, irq):
        self.irq = irq
        super().__init__(m, name)

    def reset(self):
        self.r = {}
        self.isr = 0x07 | 0x20
        self.locked = True
        self.wpr_step = 0
        self.pending = {}
        self.wut_next = None

    def _now(self):
        s = self.m.backup.seconds()
        if s is None:
            self.m.backup.set_seconds(calendar.timegm((2000, 1, 1, 0, 0, 0)))
            s = self.m.backup.seconds()
        return s

    def _wut_period(self):
        cr = self.r.get(0x08, 0)
        if not cr & (1 << 10):
            return None
        wutr = self.r.get(0x14, 0xFFFF) + 1
        sel = cr & 7
        if sel >= 4:
            secs = wutr + (65536 if sel >= 6 else 0)
        else:
            secs = wutr * (16 >> sel) / 32768
        return max(1, int(secs * self.m.board.hz))

    def next_event(self):
        p = self._wut_period()
        if p is None:
            self.wut_next = None
            return None
        if self.wut_next is None:
            self.wut_next = self.m.now() + p
        return self.wut_next

    def advance(self, now):
        t = self.next_event()
        if t is not None and now >= t:
            self.isr |= 1 << 10
            self.wut_next = now + self._wut_period()
            self._irq()

    def _irq(self):
        cr = self.r.get(0x08, 0)
        self.m.scs.set_irq(self.irq, bool(self.isr & (1 << 10) and cr & (1 << 14)))

    def rd(self, a):
        if a in (0x00, 0x04):
            t = time.gmtime(int(self._now()))
            if a == 0x00:
                return (_bcd(t.tm_hour) << 16) | (_bcd(t.tm_min) << 8) | _bcd(t.tm_sec)
            return ((_bcd(t.tm_year % 100) << 16) | ((t.tm_wday + 1) << 13) | (_bcd(t.tm_mon) << 8)
                    | _bcd(t.tm_mday))
        if a == 0x0C:
            v = self.isr | 0x20 | (0x10 if self.m.backup.epoch is not None else 0)
            if not self.r.get(0x08, 0) & (1 << 10):
                v |= 4                                   # WUTWF
            return v
        if a == 0x28:
            s = self._now()
            prediv_s = self.r.get(0x10, 0x7F00FF) & 0x7FFF
            return int((1 - (s - int(s))) * prediv_s) & 0xFFFF
        if 0x50 <= a < 0xA0:
            return self.m.backup.regs.get(a, 0)
        return self.r.get(a, 0)

    def wr(self, a, v):
        if a == 0x24:
            if (self.wpr_step, v) in ((0, 0xCA), (1, 0x53)):
                self.wpr_step += 1
                self.locked = self.wpr_step < 2
            else:
                self.wpr_step = 0
                self.locked = True
            return
        if 0x50 <= a < 0xA0:
            self.m.backup.regs[a] = v
            return
        if a == 0x0C:
            if not self.locked:
                if v & 0x80 and not self.isr & 0x80:
                    self.pending = {}
                if self.isr & 0x80 and not v & 0x80 and self.pending:
                    self._apply()
                self.isr = (self.isr & ~0x80) | (v & 0x80)
                self.isr = (self.isr & ~0x40) | (0x40 if v & 0x80 else 0)
            for flag in (5, 8, 9, 10, 11, 12, 13, 14):  # rc_w0 flags
                if not v & (1 << flag):
                    self.isr &= ~(1 << flag)
            self._irq()
            return
        if self.locked:
            return
        if a in (0x00, 0x04) and self.isr & 0x80:
            self.pending[a] = v
            return
        self.r[a] = v
        if a in (0x08, 0x14):
            self.wut_next = None
            self._irq()

    def _apply(self):
        t = time.gmtime(int(self._now()))
        tr = self.pending.get(0x00)
        dr = self.pending.get(0x04)
        hour, minute, sec = ((_unbcd((tr >> 16) & 0x3F), _unbcd((tr >> 8) & 0x7F), _unbcd(tr & 0x7F))
                             if tr is not None else (t.tm_hour, t.tm_min, t.tm_sec))
        year, month, day = ((2000 + _unbcd((dr >> 16) & 0xFF), _unbcd((dr >> 8) & 0x1F), _unbcd(dr & 0x3F))
                            if dr is not None else (t.tm_year, t.tm_mon, t.tm_mday))
        try:
            self.m.backup.set_seconds(calendar.timegm((year, month, day, hour, minute, sec)))
        except (ValueError, OverflowError):
            self.m.log(f'rtc: invalid calendar written: {year}-{month}-{day} {hour}:{minute}:{sec}')
        self.pending = {}


class Pwr(Regs):
    pass
