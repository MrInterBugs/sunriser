"""Unit tests of the emulator's parts: python3 -m unittest discover -s emulator/tests"""
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS  # noqa: E402
from unicorn.arm_const import UC_CPU_ARM_CORTEX_M3, UC_ARM_REG_PC, UC_ARM_REG_SP, UC_ARM_REG_R4  # noqa: E402

from sunriser_emu import cortexm, fat  # noqa: E402
from sunriser_emu.bee import Bee  # noqa: E402
from sunriser_emu.net import offline_pass  # noqa: E402
from sunriser_emu.sdcard import SdCard  # noqa: E402


def make_bee(path, records):
    """A minimal CDB file holding records (the hash tables are not needed to read it in order)."""
    body = b''.join(struct.pack('<II', len(k), len(v)) + k + v for k, v in records)
    end = 0x800 + len(body)
    with open(path, 'wb') as f:
        f.write(struct.pack('<II', end, 0) * 256 + body)


class BeeTest(unittest.TestCase):
    def test_images_and_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'NEW.BEE')
            make_bee(p, [(b'web#index.html#gzip', b'\xc2'), (b'___firmware_description', b'SunRiser X v9'),
                         (b'___firmware', b'A' * 2000), (b'___bootloader', b'B' * 1500)])
            bee = Bee(p)
            self.assertEqual(bee.description, 'SunRiser X v9')
            self.assertEqual(bee.images(8), (b'B' * 1500, b'A' * 2000))
            with self.assertRaises(ValueError):
                bee.images(10)


class FatTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.card = os.path.join(self.dir.name, 'sd.img')

    def tearDown(self):
        self.dir.cleanup()

    def test_new_card(self):
        data = os.urandom(10000)
        fat.make_card(self.card, 512, {'NEW.BEE': data, 'EMPTY.TXT': b''})
        with open(self.card, 'rb') as f:
            v = fat.Volume(f)
            self.assertEqual(v.bits, 32)
            self.assertEqual(v.start, fat.PART_START)
            names = [e[0] for e in v.entries()]
            self.assertEqual(names, ['NEW.BEE', 'EMPTY.TXT'])
            self.assertEqual(v.get('NEW.BEE'), data)
            self.assertEqual(v.get('EMPTY.TXT'), b'')
            self.assertIsNone(v.get('NOPE.BIN'))

    def test_replace_file_frees_old_clusters(self):
        fat.make_card(self.card, 512, {'NEW.BEE': os.urandom(50000), 'FACTORY.BEE': b'f' * 9000})
        with open(self.card, 'rb') as f:
            free_before = fat.Volume(f).fat.count(0)
        new = os.urandom(20000)
        fat.put_file(self.card, 'NEW.BEE', new)
        fat.put_file(self.card, 'OTHER.BIN', b'x' * 5000)
        with open(self.card, 'rb') as f:
            v = fat.Volume(f)
            self.assertEqual(v.get('NEW.BEE'), new)
            self.assertEqual(v.get('FACTORY.BEE'), b'f' * 9000)
            self.assertEqual(v.get('OTHER.BIN'), b'x' * 5000)
            self.assertEqual(free_before - v.fat.count(0), 5 - 13 + 2)  # 50000 B: 13 clusters, now 5 + 2
            self.assertEqual(v.lookup('NEW.BEE')[2], 20000)

    def test_log_follower_reads_new_lines(self):
        fat.make_card(self.card, 512, {})
        lines = []
        follower = fat.LogFollower(self.card, lambda line, err: lines.append((line, err)))
        follower.poll()
        self.assertEqual(lines, [])


class SdCardTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        path = os.path.join(self.dir.name, 'sd.img')
        with open(path, 'wb') as f:
            f.truncate(4 * 1024 * 1024)
            f.seek(512 * 7)
            f.write(bytes(range(256)) * 2)
        self.sd = SdCard(path)
        self.sd.chip_select(False)

    def tearDown(self):
        self.sd.close()
        self.dir.cleanup()

    def x(self, b=0xFF):
        return self.sd.xfer(b)

    def cmd(self, n, arg=0):
        for b in bytes([0x40 | n]) + struct.pack('>I', arg) + b'\x95':
            self.x(b)
        for _ in range(10):
            r = self.x()
            if not r & 0x80:
                return r
        self.fail(f'no response to CMD{n}')

    def block(self, n):
        while self.x() != 0xFE:
            pass
        data = bytes(self.x() for _ in range(n))
        self.x(), self.x()
        return data

    def test_init_like_fatfs(self):
        self.assertEqual(self.cmd(0), 1)
        self.assertEqual(self.cmd(8, 0x1AA), 1)
        self.assertEqual([self.x() for _ in range(4)], [0, 0, 1, 0xAA])
        self.assertEqual(self.cmd(55), 1)
        self.assertEqual(self.cmd(41, 1 << 30), 0)
        self.assertEqual(self.cmd(58), 0)
        self.assertEqual(self.x() & 0x40, 0x40)        # CCS: block addressing
        self.assertEqual(self.cmd(9), 0)
        csd = self.block(16)
        blocks = (((csd[7] & 63) << 16 | csd[8] << 8 | csd[9]) + 1) << 10
        self.assertEqual(blocks, 8192)

    def test_read_write(self):
        self.cmd(0)
        self.assertEqual(self.cmd(17, 7), 0)
        self.assertEqual(self.block(512), bytes(range(256)) * 2)
        self.assertEqual(self.cmd(24, 3), 0)
        self.x(), self.x(0xFE)
        for b in b'\x5a' * 512 + b'\xff\xff':
            self.x(b)
        self.assertEqual(self.x() & 0x1F, 0x05)
        while self.x() != 0xFF:
            pass
        self.assertEqual(self.cmd(18, 3), 0)          # multiple block read, stopped by CMD12
        self.assertEqual(self.block(512), b'\x5a' * 512)
        self.assertEqual(self.block(512), bytes(512))
        self.assertEqual(self.cmd(12), 0)
        while self.x() != 0xFF:
            pass
        self.assertEqual(self.cmd(25, 10), 0)         # multiple block write
        for value in (1, 2):
            self.x(), self.x(0xFC)
            for b in bytes([value]) * 512 + b'\xff\xff':
                self.x(b)
            self.assertEqual(self.x() & 0x1F, 0x05)
            while self.x() != 0xFF:
                pass
        self.x(0xFD)
        while self.x() != 0xFF:
            pass
        self.cmd(17, 11)
        self.assertEqual(self.block(512), b'\x02' * 512)

    def test_deselected_card_is_silent(self):
        self.sd.chip_select(True)
        self.assertEqual(self.x(0x40), 0xFF)


def frame(proto, dst, sport=1234, dport=80, flags=0x10):
    ip = bytes([0x45, 0, 0, 40, 0, 0, 0, 0, 64, proto, 0, 0, 10, 0, 2, 15]) + bytes(dst)
    l4 = struct.pack('>HH', sport, dport) + bytes(8) + bytes([0x50, flags]) + bytes(6)
    return bytes(12) + b'\x08\x00' + ip + l4


class OfflineTest(unittest.TestCase):
    def test_policy(self):
        self.assertTrue(offline_pass(bytes(12) + b'\x08\x06' + bytes(28)))                    # ARP
        self.assertTrue(offline_pass(frame(17, (255, 255, 255, 255), 68, 67)))                 # DHCP
        self.assertTrue(offline_pass(frame(6, (10, 0, 2, 2), 80, 51000, 0x12)))               # SYN-ACK
        self.assertFalse(offline_pass(frame(6, (10, 0, 2, 2), 51000, 80, 0x02)))              # own SYN
        self.assertFalse(offline_pass(frame(17, (10, 0, 2, 3), 5000, 53)))                     # DNS
        self.assertFalse(offline_pass(frame(6, (93, 184, 216, 34), 80, 51000, 0x10)))         # elsewhere


class FakeMachine:
    class board:
        hz = 1_000_000
        flash_base = 0x08000000

    def __init__(self, uc):
        self.uc = uc
        self.t = 0
        self.stopped = False

    def now(self):
        return self.t

    def stop(self):
        self.stopped = True
        self.uc.emu_stop()

    def cpu_fault(self, msg):
        raise AssertionError(msg)


class ExceptionTest(unittest.TestCase):
    # main: timer = 25; while (timer); done = 1; for (;;) r4++     handler: ticks++; if (timer) timer--
    MAIN = bytes.fromhex('054a192111601368002bfcd1034a012111600134fde7000004000020' '08000020')
    HANDLER = bytes.fromhex('4ff00050016801310160416809b1013941607047')

    def test_handler_counts_down_a_loop_interrupted_anywhere(self):
        uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        uc.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M3)
        uc.mem_map(0x08000000, 0x1000)
        uc.mem_map(0x20000000, 0x1000)
        uc.mem_write(0x08000000, struct.pack('<II', 0x20001000, 0x08000101) + bytes(13 * 4)
                     + struct.pack('<I', 0x08000181))
        uc.mem_write(0x08000100, self.MAIN)
        uc.mem_write(0x08000180, self.HANDLER)
        m = FakeMachine(uc)
        scs = cortexm.SCS(m, fpu=False)
        scs.write(0x14, 4, 999)                     # SysTick every 1000 cycles
        scs.write(0x10, 4, 7)
        uc.reg_write(UC_ARM_REG_SP, 0x20001000)
        uc.reg_write(UC_ARM_REG_PC, 0x08000101)
        for i in range(400):                        # a tick every third slice; slices of 7 to 19 instructions
            m.t += 350
            scs.advance(m.t)
            scs.dispatch()
            uc.emu_start(uc.reg_read(UC_ARM_REG_PC) | 1, 0xFFFFFFFF, count=7 + i % 13)
        ticks, timer, done = struct.unpack('<III', bytes(uc.mem_read(0x20000000, 12)))
        self.assertEqual(timer, 0)
        self.assertEqual(done, 1)
        self.assertEqual(ticks, 140)                # 400 slices of 350 cycles, a tick every 1000
        self.assertEqual(uc.reg_read(UC_ARM_REG_SP), 0x20001000)
        self.assertGreater(uc.reg_read(UC_ARM_REG_R4), 0)
        self.assertEqual(scs.active, [])


if __name__ == '__main__':
    unittest.main()
