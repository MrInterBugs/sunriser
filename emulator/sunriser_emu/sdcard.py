"""An SDHC card in SPI mode, backed by an image file: the commands a FatFs `mmc_spi` driver sends (CMD0, CMD8,
ACMD41, CMD58, CMD9/10, ACMD13, CMD16/17/18/12/24/25, ACMD23)."""
import collections
import os
import struct

R1_IDLE, R1_ILLEGAL, R1_ADDRESS = 0x01, 0x04, 0x20


class SdCard:
    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR)
        self.blocks = os.fstat(self.fd).st_size // 512
        self.selected = False
        self.idle = True
        self.app = False
        self.out = collections.deque()
        self.cmd = bytearray()
        self.state = 'cmd'
        self.block = None
        self.data = bytearray()
        self.reads = self.writes = 0

    def close(self):
        os.close(self.fd)

    def chip_select(self, level):
        """The chip select line changed (active low)."""
        self.selected = not level
        if not self.selected:
            self.cmd.clear()
            if self.state == 'read_multi':
                self.state = 'cmd'
                self.out.clear()

    def _read_block(self, n):
        self.reads += 1
        data = os.pread(self.fd, 512, n * 512)
        return data.ljust(512, b'\0')

    def _data_packet(self, data):
        return [0xFF, 0xFE, *data, 0xFF, 0xFF]

    def xfer(self, b):
        if not self.selected:
            return 0xFF
        if not self.out and self.state == 'read_multi':
            if self.block < self.blocks:
                self.out.extend(self._data_packet(self._read_block(self.block)))
                self.block += 1
        v = self.out.popleft() if self.out else 0xFF
        if self.state in ('cmd', 'read_multi'):
            if self.cmd or (b & 0xC0) == 0x40:
                self.cmd.append(b)
                if len(self.cmd) == 6:
                    cmd = self.cmd[0] & 0x3F
                    arg = struct.unpack('>I', self.cmd[1:5])[0]
                    self.cmd.clear()
                    self._command(cmd, arg)
        elif self.state in ('write', 'write_multi'):
            if b == 0xFE or (b == 0xFC and self.state == 'write_multi'):
                self.data = bytearray()
                self.state += '_data'
            elif b == 0xFD and self.state == 'write_multi':
                self.out.extend([0xFF, 0x00, 0x00])
                self.state = 'cmd'
        elif self.state.endswith('_data'):
            self.data.append(b)
            if len(self.data) == 514:
                if self.block < self.blocks:
                    os.pwrite(self.fd, bytes(self.data[:512]), self.block * 512)
                    self.writes += 1
                    self.block += 1
                    self.out.extend([0x05, 0x00, 0x00])
                else:
                    self.out.extend([0x0D, 0x00])
                self.state = self.state[:-5]
                if self.state == 'write':
                    self.state = 'cmd'
        return v

    def _r1(self, *extra):
        self.out.clear()
        self.out.extend([0xFF, *extra])

    def _command(self, cmd, arg):
        app, self.app = self.app, False
        idle = R1_IDLE if self.idle else 0
        if self.state == 'read_multi' and cmd != 12:
            return
        if cmd == 0:
            self.idle = True
            self._r1(R1_IDLE)
        elif cmd == 8:
            self._r1(idle, 0x00, 0x00, 0x01, arg & 0xFF)
        elif cmd == 55:
            self.app = True
            self._r1(idle)
        elif cmd == 41 and app:
            self.idle = False
            self._r1(0x00)
        elif cmd == 58:
            self._r1(idle, 0xC0, 0xFF, 0x80, 0x00)
        elif cmd == 16:
            self._r1(idle)
        elif cmd == 9:
            c_size = self.blocks // 1024 - 1
            csd = bytes([0x40, 0x0E, 0x00, 0x32, 0x5B, 0x59, 0x00, (c_size >> 16) & 0x3F, (c_size >> 8) & 0xFF,
                         c_size & 0xFF, 0x7F, 0x80, 0x0A, 0x40, 0x00, 0x01])
            self._r1(0x00, *self._data_packet(csd))
        elif cmd == 10:
            cid = b'\x03SDEMU80\x10\x00\x00\x00\x01\x01\x6a\x01'
            self._r1(0x00, *self._data_packet(cid))
        elif cmd == 13 and app:
            status = bytes([0x00, 0x00, 0x00, 0x00, 0x02, 0x00, 0x00, 0x00, 0x04, 0x00, 0x90, 0x00, 0x0F, 0x05])
            self._r1(0x00, 0x00, *self._data_packet(status.ljust(64, b'\0')))
        elif cmd == 23 and app:
            self._r1(0x00)
        elif cmd == 17:
            if arg >= self.blocks:
                self._r1(R1_ADDRESS)
            else:
                self._r1(0x00, *self._data_packet(self._read_block(arg)))
        elif cmd == 18:
            if arg >= self.blocks:
                self._r1(R1_ADDRESS)
            else:
                self._r1(0x00)
                self.block = arg
                self.state = 'read_multi'
        elif cmd == 12:
            self.state = 'cmd'
            self._r1(0xFF, 0x00, 0x00)
        elif cmd in (24, 25):
            if arg >= self.blocks:
                self._r1(R1_ADDRESS)
            else:
                self._r1(0x00)
                self.block = arg
                self.state = 'write' if cmd == 24 else 'write_multi'
        else:
            self._r1(idle | R1_ILLEGAL)
