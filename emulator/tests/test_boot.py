"""End to end: a new device boots the firmware of an update file, installs it from its card and serves its web
interface. Needs the file: SUNRISER_BEE=/path/to/NEW.BEE python3 -m unittest discover -s emulator/tests"""
import gzip
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from sunriser_emu import fat  # noqa: E402
from sunriser_emu.bee import Bee  # noqa: E402
from sunriser_emu.machine import Machine, create_state  # noqa: E402
from sunriser_emu.net import Network  # noqa: E402

BEE = os.environ.get('SUNRISER_BEE')


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def fetch(url, data=None, method='GET', timeout=10):
    req = urllib.request.Request(url, data=data, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return r.status, gzip.decompress(body) if r.headers.get('Content-Encoding') == 'gzip' else body


@unittest.skipUnless(BEE, 'set SUNRISER_BEE to an update file')
class BootTest(unittest.TestCase):
    def boot(self, board):
        bee = Bee(BEE)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        create_state(d.name, board, bee)
        port = free_port()
        m = Machine(board, d.name, Network(port, '127.0.0.1', False, lambda msg: None), quiet=True)
        t = threading.Thread(target=m.run, daemon=True)
        t.start()

        def stop():
            m.stop_reason = 'stopped'
            m.stop()
            t.join(10)
        self.addCleanup(stop)
        base = f'http://127.0.0.1:{port}'
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            self.assertIsNone(m.stop_reason)
            try:
                if fetch(base + '/state', timeout=2)[0] == 200:
                    return m, d.name, base
            except OSError:
                time.sleep(0.5)
        self.fail('the web server did not come up')

    def check(self, board):
        m, state, base = self.boot(board)
        status, body = fetch(base + '/')
        self.assertEqual(status, 200)
        self.assertIn(b'<html', body.lower())
        # a configuration value written and read back (msgpack: {"testvalue": 1}, then ["testvalue"])
        self.assertEqual(fetch(base + '/', b'\x81\xa9testvalue\x01', 'PUT')[0], 200)
        status, body = fetch(base + '/', b'\x91\xa9testvalue', 'POST')
        self.assertEqual(status, 200)
        self.assertIn(b'\xa9testvalue\x01', body)
        with open(os.path.join(state, 'sdcard.img'), 'rb') as f:
            v = fat.Volume(f)
            self.assertIsNotNone(v.lookup('FIRMWARE.BEE'))       # installed from NEW.BEE by the firmware
            self.assertIsNotNone(v.lookup('LOGS'))

    def test_sunriser8(self):
        self.check(8)

    def test_sunriser10(self):
        self.check(10)


if __name__ == '__main__':
    unittest.main()
