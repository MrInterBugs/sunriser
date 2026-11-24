"""The Ethernet MAC of the STM32F107/F407 (Synopsys MAC with descriptor DMA) and a LAN8720 PHY on its management
interface. Frames leave through Network.send() and arrive by deliver(); the MAC appends the frame check sequence
on receipt, as the hardware does."""
import collections
import struct
import zlib

from .stm32 import Regs

OWN = 1 << 31
T_LS, T_FS, T_TER, T_TCH = 1 << 29, 1 << 28, 1 << 21, 1 << 20
R_FS, R_LS = 1 << 9, 1 << 8
R_RER, R_RCH = 1 << 15, 1 << 14
DMASR_TS, DMASR_TBUS, DMASR_RS, DMASR_RBUS, DMASR_NIS, DMASR_AIS = 1, 4, 1 << 6, 1 << 7, 1 << 16, 1 << 15


class Lan8720:
    """PHY at address 0: link up, autonegotiated 100 Mbit/s full duplex, no symbol errors."""

    def __init__(self, m):
        self.m = m
        self.regs = {0: 0x3100, 2: 0x0007, 3: 0xC0F1, 4: 0x01E1, 5: 0xCDE1, 6: 0x0001, 17: 0x0002, 18: 0x60E0,
                     26: 0, 27: 0x000A, 29: 0, 30: 0, 31: 0x1058}

    def read(self, addr, reg):
        if addr != 0:
            return 0xFFFF
        if reg == 1:
            return 0x7809 | (0x24 if self.m.link_up else 0)
        if reg == 29:
            v, self.regs[29] = self.regs[29], 0
            return v
        return self.regs.get(reg, 0)

    def write(self, addr, reg, value):
        if addr != 0:
            return
        if reg == 0:
            value &= ~(0x8000 | 0x0200)          # reset and autonegotiation restart complete at once
        self.regs[reg] = value


class Ethernet(Regs):
    def __init__(self, m, name, network):
        self.network = network
        self.phy = Lan8720(m)
        self.rxq = collections.deque()
        self.frames_in = self.frames_out = self.dropped = 0
        super().__init__(m, name)

    def reset(self):
        self.r = {0x1000: 0x2101}
        self.dmasr = 0
        self.tx_ptr = self.rx_ptr = None
        self.tx_frame = b''

    def _r32(self, addr):
        return struct.unpack('<I', bytes(self.m.uc.mem_read(addr, 4)))[0]

    def _w32(self, addr, v):
        self.m.uc.mem_write(addr, struct.pack('<I', v & 0xFFFFFFFF))

    def rd(self, a):
        if a == 0x1014:
            self.service()
            v = self.dmasr
            v |= (6 << 20) if self.tx_ptr is not None and self.dmasr & DMASR_TBUS else 0
            v |= (4 << 17) if self.rx_ptr is not None and self.dmasr & DMASR_RBUS else (3 << 17) if self.rx_ptr else 0
            return v
        if a == 0x1048:
            return self.tx_ptr or 0
        if a == 0x104C:
            return self.rx_ptr or 0
        if a == 0x1000:
            return self.r.get(a, 0) & ~1
        return self.r.get(a, 0)

    def wr(self, a, v):
        if a == 0x0010:
            if v & 1:
                pa, mr = (v >> 11) & 31, (v >> 6) & 31
                if v & 2:
                    self.phy.write(pa, mr, self.r.get(0x14, 0) & 0xFFFF)
                else:
                    self.r[0x14] = self.phy.read(pa, mr)
            self.r[a] = v & ~1
        elif a == 0x1000:
            if v & 1:
                self.reset()
            self.r[a] = v & ~1
        elif a == 0x1004:
            self._tx()
        elif a == 0x1008:
            self.dmasr &= ~DMASR_RBUS
            self.service()
        elif a == 0x100C:
            self.r[a] = v
            self.rx_ptr = v
        elif a == 0x1010:
            self.r[a] = v
            self.tx_ptr = v
        elif a == 0x1014:
            self.dmasr &= ~(v & 0x1FFFF)
            if not self.dmasr & 0x7FFF & ~(DMASR_TS | DMASR_RS | DMASR_TBUS | DMASR_RBUS):
                self.dmasr &= ~DMASR_AIS
            if not self.dmasr & (DMASR_TS | DMASR_RS | DMASR_TBUS | DMASR_RBUS):
                self.dmasr &= ~DMASR_NIS
        elif a == 0x1018:
            old = self.r.get(a, 0)
            self.r[a] = v & ~(1 << 20)
            if v & (1 << 13) and not old & (1 << 13):
                self.tx_ptr = self.r.get(0x1010, 0)
                self._tx()
            if v & 2 and not old & 2:
                self.rx_ptr = self.r.get(0x100C, 0)
                self.service()
        else:
            self.r[a] = v

    def _tx_enabled(self):
        return self.r.get(0x1018, 0) & (1 << 13) and self.r.get(0x0000, 0) & (1 << 3) and self.tx_ptr is not None

    def _rx_enabled(self):
        return self.r.get(0x1018, 0) & 2 and self.r.get(0x0000, 0) & (1 << 2) and self.rx_ptr is not None

    def _tx(self):
        if not self._tx_enabled():
            return
        for _ in range(64):
            d = self.tx_ptr
            d0 = self._r32(d)
            if not d0 & OWN:
                self.dmasr |= DMASR_TBUS | DMASR_AIS
                return
            self.dmasr &= ~DMASR_TBUS
            d1 = self._r32(d + 4)
            buf = self._r32(d + 8)
            n = d1 & 0x1FFF
            if d0 & T_FS:
                self.tx_frame = b''
            self.tx_frame += bytes(self.m.uc.mem_read(buf, n)) if n else b''
            if d0 & T_LS:
                self.frames_out += 1
                if self.m.link_up:
                    self.network.send(self.tx_frame)
                self.tx_frame = b''
                self.dmasr |= DMASR_TS | DMASR_NIS
            self._w32(d, d0 & ~OWN & ~0xFFFF)
            if d0 & T_TCH:
                self.tx_ptr = self._r32(d + 12)
            elif d0 & T_TER:
                self.tx_ptr = self.r.get(0x1010, 0)
            else:
                self.tx_ptr = d + 16 + ((self.r.get(0x1000, 0) >> 2) & 31) * 4

    def _next_rx(self, d, d1):
        if d1 & R_RCH:
            return self._r32(d + 12)
        if d1 & R_RER:
            return self.r.get(0x100C, 0)
        return d + 16 + ((self.r.get(0x1000, 0) >> 2) & 31) * 4

    def deliver(self, frame):
        """A frame from the network; it waits while the device has no free receive descriptor."""
        if not self.m.link_up:
            return
        if len(self.rxq) > 256:
            self.dropped += 1
            return
        self.rxq.append(frame)
        self.service()

    def service(self):
        while self.rxq and self._rx_enabled():
            if not self._receive(self.rxq[0]):
                return
            self.rxq.popleft()

    def _receive(self, frame):
        frame = frame.ljust(60, b'\0')
        frame += struct.pack('<I', zlib.crc32(frame) & 0xFFFFFFFF)
        chain = []
        d, need = self.rx_ptr, len(frame)
        while need > 0 and len(chain) < 8:
            d0 = self._r32(d)
            if not d0 & OWN:
                self.dmasr |= DMASR_RBUS | DMASR_AIS
                return False
            d1 = self._r32(d + 4)
            size = d1 & 0x1FFF
            if not size:
                return False
            chain.append((d, d1, size))
            need -= size
            d = self._next_rx(d, d1)
        if need > 0:
            self.dmasr |= DMASR_RBUS | DMASR_AIS
            return False
        off = 0
        for i, (d, d1, size) in enumerate(chain):
            part = frame[off:off + size]
            self.m.uc.mem_write(self._r32(d + 8), part)
            off += len(part)
            status = (R_FS if i == 0 else 0) | (R_LS | (len(frame) << 16) if i == len(chain) - 1 else 0)
            self._w32(d, status)
        self.rx_ptr = self._next_rx(*chain[-1][:2])
        self.frames_in += 1
        self.dmasr |= DMASR_RS | DMASR_NIS
        return True
