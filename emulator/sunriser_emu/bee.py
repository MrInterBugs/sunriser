"""Reading a SunRiser update file (.bee): a CDB database whose `___`-prefixed records hold the firmware images and
their metadata as raw bytes."""
import hashlib
import struct

IMAGES = {8: ('bootloader', 'firmware'), 10: ('bootloader10', 'firmware10')}


def records(data):
    """(key, value) of every record, in file order."""
    end = min(struct.unpack_from('<I', data, i * 8)[0] for i in range(256))
    off = 0x800
    while off < end:
        klen, vlen = struct.unpack_from('<II', data, off)
        yield data[off + 8:off + 8 + klen], data[off + 8 + klen:off + 8 + klen + vlen]
        off += 8 + klen + vlen


class Bee:
    def __init__(self, path):
        self.path = str(path)
        with open(path, 'rb') as f:
            self.data = f.read()
        if len(self.data) < 0x800:
            raise ValueError(f'{path}: too short for a .bee file')
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        self.raw = {k[3:].decode(): v for k, v in records(self.data) if k.startswith(b'___')}

    def text(self, name):
        v = self.raw.get(name)
        return v.decode('utf-8', 'replace') if v is not None else None

    @property
    def description(self):
        return self.text('firmware_description') or '(no firmware description)'

    def images(self, board):
        """(bootloader, application) images for a board."""
        names = IMAGES[board]
        missing = [n for n in names if n not in self.raw]
        if missing:
            raise ValueError(f'{self.path}: no {", ".join(missing)} image for the SunRiser {board}')
        return tuple(self.raw[n] for n in names)
