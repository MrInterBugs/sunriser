"""A SunRiser 8 or 10: the CPU (Unicorn), the memory map, the peripherals, time and the device's state on disk."""
import ctypes
import json
import os
import random
import struct
import sys
import time
from dataclasses import dataclass

from unicorn import (Uc, UcError, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_MEM_WRITE_PROT,
                     UC_PROT_READ, UC_PROT_EXEC, UC_PROT_ALL)
from unicorn.arm_const import (UC_CPU_ARM_CORTEX_M3, UC_CPU_ARM_CORTEX_M4, UC_ARM_REG_PC, UC_ARM_REG_SP,
                               UC_ARM_REG_LR, UC_ARM_REG_XPSR, UC_ARM_REG_CONTROL, UC_ARM_REG_PRIMASK,
                               UC_ARM_REG_BASEPRI, UC_ARM_REG_FAULTMASK, UC_ARM_REG_R0)

from . import cortexm, eth, fat, stm32
from .sdcard import SdCard

APP_OFFSET = 0x8000
RESET_POWER = (1 << 27) | (1 << 26)       # PORRSTF | PINRSTF
RESET_SOFTWARE = 1 << 28                  # SFTRSTF


@dataclass(frozen=True)
class Board:
    number: int
    mcu: str
    hz: int                    # core clock after the firmware's clock setup
    flash_size: int
    ram_size: int
    fpu: bool
    apb1_timer_hz: int
    apb2_timer_hz: int
    flash_base: int = 0x08000000


BOARDS = {8: Board(8, 'STM32F107', 72_000_000, 0x40000, 0x10000, False, 72_000_000, 72_000_000),
          10: Board(10, 'STM32F407', 84_000_000, 0x100000, 0x20000, True, 84_000_000, 84_000_000)}


class Crc(stm32.Regs):
    """The CRC unit: CRC-32 (polynomial 0x04C11DB7, MSB first) over 32-bit words."""

    def reset(self):
        self.r = {}
        self.crc = 0xFFFFFFFF

    def rd(self, a):
        return self.crc if a == 0 else self.r.get(a, 0)

    def wr(self, a, v):
        if a == 0:
            c = self.crc ^ v
            for _ in range(32):
                c = ((c << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if c & 0x80000000 else (c << 1) & 0xFFFFFFFF
            self.crc = c
        elif a == 8 and v & 1:
            self.crc = 0xFFFFFFFF
        else:
            self.r[a] = v


class Rng(stm32.Regs):
    def rd(self, a):
        if a == 4:
            return 1
        if a == 8:
            return random.getrandbits(32)
        return self.r.get(a, 0)


class Console:
    """The firmware's log (from its card), anything it writes to ITM, and the emulator's own messages."""

    def __init__(self, m, quiet):
        self.m = m
        self.quiet = quiet
        self.line = bytearray()

    def stamp(self):
        return f'{self.m.now() / self.m.board.hz:10.3f}'

    def firmware(self, channel, byte):
        if byte == 10:
            if not self.quiet:
                text = self.line.decode('latin-1').rstrip('\r')
                print(f'{self.stamp()}  {text}', flush=True)
            self.line.clear()
        elif len(self.line) < 4096:
            self.line.append(byte)

    def log_line(self, line, error):
        if not self.quiet:
            print(f'{"!" if error else " "} {line}', flush=True)

    def emulator(self, msg):
        print(f'{self.stamp()} * {msg}', file=sys.stderr, flush=True)


class Machine:
    def __init__(self, board, state_dir, network=None, quiet=False, speed=1.0):
        self.board = BOARDS[board]
        self.state_dir = state_dir
        self.speed = speed
        self.rate = BOARDS[board].hz * speed      # device cycles per second of the host's clock
        self.t0 = time.monotonic()
        self.link_up = True
        self.idle = False
        self.stop_reason = None
        self.stopped = False
        self.reset_reason = None
        self.reset_flags = RESET_POWER
        self.fixups = []
        self.console = Console(self, quiet)
        meta = self.meta = _load(state_dir, 'state.json')
        self.uc = uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        uc.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4 if self.board.fpu else UC_CPU_ARM_CORTEX_M3)

        # flash: one buffer at its own address and at the boot alias 0; stores to it are programming operations
        b = self.board
        self.flash = (ctypes.c_ubyte * b.flash_size)()
        with open(os.path.join(state_dir, 'flash.bin'), 'rb') as f:
            data = f.read()
        ctypes.memmove(self.flash, data, min(len(data), b.flash_size))
        self.flash_dirty = False
        for base in (0, b.flash_base):
            uc.mem_map_ptr(base, b.flash_size, UC_PROT_READ | UC_PROT_EXEC, self.flash)
        uc.hook_add(UC_HOOK_MEM_WRITE_PROT, self._flash_store)
        uc.mem_map(0x20000000, b.ram_size)
        if b.number == 10:
            uc.mem_map(0x10000000, 0x10000)                # CCM RAM
        uc.mem_map(0x1FFF0000, 0x10000, UC_PROT_ALL)       # system memory: unique ID and flash size registers
        if b.number == 8:
            uc.mem_write(0x1FFFF7E0, struct.pack('<H', b.flash_size >> 10))
            uc.mem_write(0x1FFFF7E8, bytes.fromhex(meta['uid']))
        else:
            uc.mem_write(0x1FFF7A10, bytes.fromhex(meta['uid']))
            uc.mem_write(0x1FFF7A22, struct.pack('<H', b.flash_size >> 10))

        self.backup = stm32.Backup(self, _load(state_dir, 'backup.json'))
        self.scs = cortexm.SCS(self, b.fpu)
        self.ppb = cortexm.PPB(self, self.console.firmware)
        self.blocks = {}
        self.regions = []
        self.timed = []
        self.reset_lines = {}
        self.pwm = {}
        self.pwm_shown = None
        self.pwm_changed = False
        self.led_changed = False
        self.led_shown = None
        self.sd = SdCard(os.path.join(state_dir, 'sdcard.img'))
        self.network = network
        self.logs = fat.LogFollower(os.path.join(state_dir, 'sdcard.img'), self.console.log_line)
        self._peripherals(bytes.fromhex(meta['mac']))
        if network:
            network.deliver = self.eth.deliver
        mapped = sorted(self.regions)
        for base, size, p in mapped:
            uc.mmio_map(base, size, p.read_cb, None, p.write_cb, None)
        for start, end in ((0x40000000, 0x40080000), (0x50000000, 0x50080000)):  # the rest: plain registers
            cur = start
            for base, size, _ in mapped + [(end, 0, None)]:
                if start <= base <= end and base > cur:
                    gap = stm32.Regs(self, f'{cur:#010x}')
                    uc.mmio_map(cur, base - cur, gap.read_cb, None, gap.write_cb, None)
                    for blk in range(cur >> 10, base >> 10):
                        self.blocks[blk] = (gap, cur)
                if start <= base < end:
                    cur = max(cur, base + size)
        uc.mmio_map(0x42000000, 0x2000000, self._bb_read, 0x40000000, self._bb_write, 0x40000000)
        uc.mmio_map(0x22000000, 0x2000000, self._bb_read, 0x20000000, self._bb_write, 0x20000000)
        uc.mmio_map(0xE0000000, 0x100000, self._ppb_read, None, self._ppb_write, None)
        self._cpu_reset()

    # -- peripherals --
    def _add(self, base, p, size=0x400):
        for blk in range(base >> 10, (base + size) >> 10):
            self.blocks[blk] = (p, base)
        self.regions.append((base, size, p))
        if hasattr(p, 'next_event'):
            self.timed.append(p)
        return p

    def _peripherals(self, mac):
        b = self.board
        f4 = b.number == 10
        eeprom = stm32.Eeprom24(mac)
        apb1, apb2 = b.apb1_timer_hz, b.apb2_timer_hz
        on_ccr = self._pwm_changed
        timers = [('TIM2', 0x40000000, apb1, 28, 32 if f4 else 16), ('TIM3', 0x40000400, apb1, 29, 16),
                  ('TIM4', 0x40000800, apb1, 30, 16), ('TIM5', 0x40000C00, apb1, 50, 32 if f4 else 16),
                  ('TIM6', 0x40001000, apb1, 54, 16), ('TIM7', 0x40001400, apb1, 55, 16),
                  ('TIM1', 0x40010000 if f4 else 0x40012C00, apb2, 25, 16)]
        if f4:
            timers.append(('TIM8', 0x40010400, apb2, 44, 16))
        for name, base, clk, irq, bits in timers:
            self._add(base, stm32.Timer(self, name, clk, irq, bits, on_ccr))
        self.spi_sd = self._add(0x40003C00, stm32.Spi(self, 'SPI3', self.sd))
        for name, base in (('SPI1', 0x40013000), ('SPI2', 0x40003800)):
            self._add(base, stm32.Spi(self, name))
        self.i2c = self._add(0x40005400, stm32.I2c(self, 'I2C1', {0x50: eeprom}))
        self._add(0x40005800, stm32.I2c(self, 'I2C2', {}))
        usarts = [('USART2', 0x40004400, 38), ('USART3', 0x40004800, 39), ('UART4', 0x40004C00, 52),
                  ('UART5', 0x40005000, 53), ('USART1', 0x40011000 if f4 else 0x40013800, 37)]
        if f4:
            usarts.append(('USART6', 0x40011400, 71))
        for name, base, irq in usarts:
            self._add(base, stm32.Usart(self, name, irq))
        self.pwr = self._add(0x40007000, stm32.Pwr(self, 'PWR'))
        self.eth = self._add(0x40028000, eth.Ethernet(self, 'ETH', self.network or _NoNetwork()), 0x2000)
        self.rcc = self._add(0x40023800 if f4 else 0x40021000, stm32.Rcc(self, 'RCC', f4))
        self.flashctl = self._add(0x40023C00 if f4 else 0x40022000, stm32.Flash(self, 'FLASH', f4))
        self._add(0x40023000, Crc(self, 'CRC'))
        if f4:
            self.rtc = self._add(0x40002800, stm32.RtcF4(self, 'RTC', 3))
            self._add(0x40013800, stm32.Syscfg(self, 'SYSCFG'))
            self._add(0x40013C00, stm32.Exti(self, 'EXTI'))
            self.gpio = {p: self._add(0x40020000 + 0x400 * i, stm32.Gpio(self, 'GPIO' + p, True))
                         for i, p in enumerate('ABCDEFGHI')}
            self._add(0x50060800, Rng(self, 'RNG'))
            reset_bits = {('AHB1', 25): self.eth, ('APB1', 15): self.spi_sd, ('APB1', 21): self.i2c}
        else:
            self.rtc = self._add(0x40002800, stm32.RtcF1(self, 'RTC', 3))
            self._add(0x40006C00, stm32.BkpF1(self, 'BKP'))
            self._add(0x40010400, stm32.Exti(self, 'EXTI'))
            self.gpio = {p: self._add(0x40010800 + 0x400 * i, stm32.Gpio(self, 'GPIO' + p, False))
                         for i, p in enumerate('ABCDE')}
            reset_bits = {('AHB', 14): self.eth, ('APB1', 15): self.spi_sd, ('APB1', 21): self.i2c}
        self.reset_lines = reset_bits
        self.gpio['D'].watch[2] = self.sd.chip_select
        if f4:
            self.gpio['E'].watch.update({pin: (lambda level, pin=pin: self._status_led()) for pin in (13, 14, 15)})

    def pwr_dbp(self):
        return self.pwr.r.get(0, 0) & 0x100

    # -- bus --
    def _read(self, addr, size):
        p, pbase = self.blocks[addr >> 10]
        return p.read(addr - pbase, size)

    def _write(self, addr, size, value):
        p, pbase = self.blocks[addr >> 10]
        p.write(addr - pbase, size, value)

    def _bb_read(self, uc, off, size, base):
        word, bit = base + ((off >> 5) & ~3), ((off >> 5) & 3) * 8 + ((off >> 2) & 7)
        if base == 0x20000000:
            v = struct.unpack('<I', bytes(uc.mem_read(word, 4)))[0]
        else:
            v = self._read(word, 4)
        return (v >> bit) & 1

    def _bb_write(self, uc, off, size, value, base):
        word, bit = base + ((off >> 5) & ~3), ((off >> 5) & 3) * 8 + ((off >> 2) & 7)
        if base == 0x20000000:
            v = struct.unpack('<I', bytes(uc.mem_read(word, 4)))[0]
            v = (v & ~(1 << bit)) | ((value & 1) << bit)
            uc.mem_write(word, struct.pack('<I', v))
        else:
            cur = self._read(word, 4)            # the bus does a read-modify-write, as on the device
            self._write(word, 4, (cur & ~(1 << bit)) | ((value & 1) << bit))

    def _ppb_read(self, uc, off, size, _):
        if 0xE000 <= off < 0xF000:
            return self.scs.read(off - 0xE000, size)
        return self.ppb.read(off, size)

    def _ppb_write(self, uc, off, size, value, _):
        if 0xE000 <= off < 0xF000:
            self.scs.write(off - 0xE000, size, value)
        else:
            self.ppb.write(off, size, value)

    # -- flash programming --
    def _flash_store(self, uc, access, addr, size, value, _):
        base = self.board.flash_base if addr >= self.board.flash_base else 0
        off = addr - base
        if not 0 <= off < self.board.flash_size:
            return False
        old = bytes(self.flash[off:off + size])
        new = (value & ((1 << (8 * size)) - 1)).to_bytes(size, 'little')
        result = self.flashctl.program(old, new)
        if result != new:
            self.fixups.append((off, result))
        self.flash_dirty = True
        return True

    def _apply_fixups(self):
        for off, data in self.fixups:
            ctypes.memmove(ctypes.addressof(self.flash) + off, data, len(data))
            self.uc.ctl_remove_cache(self.board.flash_base + off, self.board.flash_base + off + len(data))
        self.fixups = []

    def flash_erase(self, off, size):
        ctypes.memset(ctypes.addressof(self.flash) + off, 0xFF, size)
        self.uc.ctl_remove_cache(self.board.flash_base + off, self.board.flash_base + off + size)
        self.flash_dirty = True

    # -- time and outputs --
    def now(self):
        """The device's time in CPU cycles: the host's clock, scaled by --speed. The CPU runs as fast as the host
        executes it; what it does in a second of device time is what it manages in that time."""
        return int((time.monotonic() - self.t0) * self.rate)

    def log(self, msg):
        self.console.emulator(msg)

    def _pwm_changed(self, timer, channel, value):
        self.pwm[(timer.name, channel)] = (value, timer)
        self.pwm_changed = True

    def _show_pwm(self):
        if not self.pwm_changed:
            return
        self.pwm_changed = False
        parts = []
        for (name, ch), (value, timer) in sorted(self.pwm.items()):
            top = (timer.r.get(0x2C, 0) or 0xFFFF) + 1
            parts.append(f'{name}.{ch + 1}={100 * value / top:5.1f}%')
        line = ' '.join(parts)
        if line != self.pwm_shown:
            self.pwm_shown = line
            self.log(f'PWM {line}')

    COLOURS = {(0, 0, 0): 'off', (1, 0, 0): 'red', (0, 1, 0): 'green', (0, 0, 1): 'blue', (1, 1, 0): 'yellow',
               (0, 1, 1): 'cyan', (1, 0, 1): 'magenta', (1, 1, 1): 'white'}

    def _status_led(self):
        self.led_changed = True

    def _show_led(self):
        """The SunRiser 10's status LED (PE13 red, PE14 green, PE15 blue, active low), once it has settled."""
        if not self.led_changed:
            return
        self.led_changed = False
        odr = self.gpio['E'].odr
        colour = self.COLOURS[tuple(int(not odr & (1 << pin)) for pin in (13, 14, 15))]
        if colour != self.led_shown:
            self.led_shown = colour
            self.log(f'status LED {colour}')

    # -- CPU --
    def _cpu_reset(self):
        uc = self.uc
        sp, pc = struct.unpack('<II', bytes(self.flash[0:8]))
        for r in range(UC_ARM_REG_R0, UC_ARM_REG_R0 + 13):
            uc.reg_write(r, 0)
        uc.reg_write(UC_ARM_REG_XPSR, 0x01000000)
        uc.reg_write(UC_ARM_REG_CONTROL, 0)
        for r in (UC_ARM_REG_PRIMASK, UC_ARM_REG_BASEPRI, UC_ARM_REG_FAULTMASK):
            uc.reg_write(r, 0)
        uc.reg_write(UC_ARM_REG_SP, sp)
        uc.reg_write(UC_ARM_REG_LR, 0xFFFFFFFF)
        uc.reg_write(UC_ARM_REG_PC, pc | 1)
        uc.ctl_flush_tb()

    def stop(self):
        """End the current slice early (after an exception return, a reset request, a fault)."""
        self.stopped = True
        self.uc.emu_stop()

    def request_reset(self, reason):
        self.reset_reason = reason
        self.stop()

    def cpu_fault(self, msg):
        uc = self.uc
        regs = ' '.join(f'{n}={uc.reg_read(r):08x}' for n, r in (('pc', UC_ARM_REG_PC), ('lr', UC_ARM_REG_LR),
                                                                  ('sp', UC_ARM_REG_SP), ('xpsr', UC_ARM_REG_XPSR)))
        self.stop_reason = f'{msg}; {regs}'
        self.stop()

    def system_reset(self):
        self.log(f'reset: {self.reset_reason}')
        self.reset_flags |= RESET_SOFTWARE
        self.reset_reason = None
        self.scs.reset()
        for blk, (p, base) in list(self.blocks.items()):
            if blk == base >> 10 and p not in (self.rcc,):
                p.reset()
        self.rcc.reset()
        self.save()
        self._cpu_reset()

    # -- run --
    def _next_event(self):
        t = None
        for p in self.timed + [self.scs]:
            e = p.next_event()
            if e is not None and (t is None or e < t):
                t = e
        return t

    def run(self):
        uc = self.uc
        ips = 20_000_000            # instructions the host executes per second, measured as it goes
        last_poll = last_show = last_save = time.monotonic()
        while self.stop_reason is None:
            now = self.now()
            nxt = self._next_event()
            seconds = 0.001 if nxt is None else max(0.00002, min(0.001, (nxt - now) / self.rate))
            count = max(200, int(ips * seconds))
            self.stopped = False
            t = time.monotonic()
            # Slices end after a count of instructions, which stops exactly between two instructions (a stop from
            # outside, like uc_emu_start's timeout, can report the start of a translation block instead).
            try:
                uc.emu_start(uc.reg_read(UC_ARM_REG_PC) | 1, 0xFFFFFFFF, count=count)
            except UcError as e:
                self.cpu_fault(f'{e} (memory access at an address nothing is mapped at, or a protection fault)')
            dt = time.monotonic() - t
            if not self.stopped and dt > 0.0002:
                ips = 0.8 * ips + 0.2 * count / dt
            if self.fixups:
                self._apply_fixups()
            if self.reset_reason:
                self.system_reset()
                continue
            now = self.now()
            if self.idle:                      # WFI/WFE: nothing to do until the next event
                self.idle = False
                nxt = self._next_event()
                if nxt is not None and nxt > now:
                    time.sleep(min(0.01, (nxt - now) / self.rate))
                    now = self.now()
            for p in self.timed:
                p.advance(now)
            self.scs.advance(now)
            self.eth.service()
            self.scs.dispatch()
            wall = time.monotonic()
            if self.network and wall - last_poll >= 0.001:
                self.network.poll(0)
                last_poll = wall
            if wall - last_show > 0.5:
                self._show_pwm()
                self._show_led()
                self.logs.poll()
                last_show = wall
            if wall - last_save > 10:
                self.save()
                last_save = wall
        self.save()
        self.logs.poll()
        return self.stop_reason

    def save(self):
        if self.flash_dirty:
            with open(os.path.join(self.state_dir, 'flash.bin.new'), 'wb') as f:
                f.write(bytes(self.flash))
            os.replace(os.path.join(self.state_dir, 'flash.bin.new'), os.path.join(self.state_dir, 'flash.bin'))
            self.flash_dirty = False
        with open(os.path.join(self.state_dir, 'backup.json.new'), 'w') as f:
            json.dump(self.backup.state(), f)
        os.replace(os.path.join(self.state_dir, 'backup.json.new'), os.path.join(self.state_dir, 'backup.json'))


def _load(state_dir, name):
    with open(os.path.join(state_dir, name)) as f:
        return json.load(f)


class _NoNetwork:
    def send(self, frame):
        pass


def create_state(state_dir, board, bee, card_mib=512, mac=None):
    """A new device: the bootloader and application of the update file in its flash, the update file on its card as
    NEW.BEE (to be installed at the first start) and FACTORY.BEE, its clock set to the host's."""
    b = BOARDS[board]
    boot, app = bee.images(board)
    os.makedirs(state_dir, exist_ok=True)
    flash = bytearray(b'\xff' * b.flash_size)
    flash[0:len(boot)] = boot
    flash[APP_OFFSET:APP_OFFSET + len(app)] = app
    with open(os.path.join(state_dir, 'flash.bin'), 'wb') as f:
        f.write(flash)
    fat.make_card(os.path.join(state_dir, 'sdcard.img'), card_mib, {'NEW.BEE': bee.data, 'FACTORY.BEE': bee.data})
    now = int(time.time())
    backup = {'bdcr': 0x0101 | 0x8000, 'regs': {}, 'rtc': now, 'saved_at': now}   # LSE on, RTC on LSE, enabled
    if board == 10:
        backup['regs'] = {str(0x50): 0x52590000 | time.gmtime(now).tm_year}      # 1.007's year marker
    with open(os.path.join(state_dir, 'backup.json'), 'w') as f:
        json.dump(backup, f)
    mac = mac or bytes([0x02] + [random.randrange(256) for _ in range(5)])
    meta = {'board': board, 'mac': mac.hex(), 'uid': os.urandom(12).hex(), 'created': now,
            'bee': {'sha256': bee.sha256, 'description': bee.description}}
    with open(os.path.join(state_dir, 'state.json'), 'w') as f:
        json.dump(meta, f, indent=2)
    return meta
