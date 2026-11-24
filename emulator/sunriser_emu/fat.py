"""A new SD card image: an MBR with one FAT32 partition holding a few files in its root directory, as a card formatted
on a PC and with files copied onto it. The image is sparse."""
import struct
import time

SECTOR = 512
PART_START = 8192           # 4 MiB, where SD card formatters put the first partition
RESERVED = 32
CLUSTER_SECTORS = 8         # 4 KiB clusters
EOC = 0x0FFFFFFF


def _dos_datetime(t):
    lt = time.localtime(t)
    date = ((lt.tm_year - 1980) << 9) | (lt.tm_mon << 5) | lt.tm_mday
    clock = (lt.tm_hour << 11) | (lt.tm_min << 5) | (lt.tm_sec // 2)
    return date, clock


def _short_name(name):
    base, _, ext = name.upper().partition('.')
    if not (1 <= len(base) <= 8 and len(ext) <= 3):
        raise ValueError(f'{name}: not an 8.3 file name')
    return (base.ljust(8) + ext.ljust(3)).encode('ascii')


def make_card(path, size_mib, files, label='SUNRISER'):
    """Write a card image of size_mib MiB with files ({8.3 name: bytes}) in the root directory."""
    total = size_mib * 1024 * 1024 // SECTOR
    part = total - PART_START
    fat_sectors = 1
    while True:     # the FAT must cover every cluster of the space left after it
        clusters = (part - RESERVED - 2 * fat_sectors) // CLUSTER_SECTORS
        need = ((clusters + 2) * 4 + SECTOR - 1) // SECTOR
        if need <= fat_sectors:
            break
        fat_sectors = need
    if clusters < 65525:
        raise ValueError(f'{size_mib} MiB is too small for FAT32 with 4 KiB clusters')
    data_start = RESERVED + 2 * fat_sectors
    cluster_bytes = CLUSTER_SECTORS * SECTOR
    now = time.time()
    date, clock = _dos_datetime(now)

    fat = [0x0FFFFFF8, EOC, EOC]       # media, end-of-chain, root directory (cluster 2)
    entries = [label.upper().ljust(11)[:11].encode('ascii') + bytes([0x08]) + bytes(10)
               + struct.pack('<HHHI', clock, date, 0, 0)]
    contents = []
    for name, data in files.items():
        first = len(fat) if data else 0
        n = (len(data) + cluster_bytes - 1) // cluster_bytes
        fat += [len(fat) + i + 1 for i in range(n - 1)] + ([EOC] if n else [])
        entries.append(_short_name(name) + bytes([0x20, 0, 0]) + struct.pack('<HHHHHHHI', clock, date, date,
                       first >> 16, clock, date, first & 0xFFFF, len(data)))
        contents.append((first, data))
    if len(entries) * 32 > cluster_bytes or len(fat) > clusters + 2:
        raise ValueError('too many or too large files for the card')

    jump = bytes([0xEB, 0x58, 0x90]) + b'MSWIN4.1'
    bpb = struct.pack('<HBHBHHBHHHII', SECTOR, CLUSTER_SECTORS, RESERVED, 2, 0, 0, 0xF8, 0, 63, 255,
                      PART_START, part)
    ext = struct.pack('<IHHIHH12sBBBI11s8s', fat_sectors, 0, 0, 2, 1, 6, bytes(12), 0x80, 0, 0x29,
                      int(now) & 0xFFFFFFFF, label.upper().ljust(11)[:11].encode('ascii'), b'FAT32   ')
    vbr = (jump + bpb + ext).ljust(510, b'\0') + b'\x55\xaa'
    free = clusters - (len(fat) - 2)
    fsinfo = (struct.pack('<I', 0x41615252) + bytes(480) + struct.pack('<III', 0x61417272, free, len(fat))
              + bytes(12) + struct.pack('<I', 0xAA550000))
    chs = bytes([0xFE, 0xFF, 0xFF])
    mbr = (bytes(446) + bytes([0x00]) + chs + bytes([0x0C]) + chs + struct.pack('<II', PART_START, part)
           + bytes(48) + b'\x55\xaa')
    fat_bytes = struct.pack(f'<{len(fat)}I', *fat)

    def at(sector):
        return (PART_START + sector) * SECTOR

    def cluster(n):
        return at(data_start + (n - 2) * CLUSTER_SECTORS)

    with open(path, 'wb') as f:
        f.truncate(total * SECTOR)
        for off, data in [(0, mbr), (at(0), vbr), (at(1), fsinfo), (at(6), vbr), (at(7), fsinfo),
                          (at(RESERVED), fat_bytes), (at(RESERVED + fat_sectors), fat_bytes),
                          (cluster(2), b''.join(entries))]:
            f.seek(off)
            f.write(data)
        for first, data in contents:
            if data:
                f.seek(cluster(first))
                f.write(data)


class Volume:
    """The FAT12/16/32 volume on a card image (a partitioned card or a superfloppy), to replace files in its root
    directory while the device is off."""

    def __init__(self, f):
        self.f = f
        vbr = self._sector(0, 0)
        if not self._is_vbr(vbr):
            start = struct.unpack_from('<I', vbr, 0x1C6)[0]
            vbr = self._sector(start, 0)
            if not self._is_vbr(vbr):
                raise ValueError('no FAT file system on the card')
        else:
            start = 0
        self.start = start
        (self.bps, self.spc, self.rsvd, self.nfats, self.root_entries, tot16, _, fat16,
         ) = struct.unpack_from('<HBHBHHBH', vbr, 11)
        tot32 = struct.unpack_from('<I', vbr, 32)[0]
        self.fat_sectors = fat16 or struct.unpack_from('<I', vbr, 36)[0]
        total = tot16 or tot32
        self.root_sectors = (self.root_entries * 32 + self.bps - 1) // self.bps
        self.data_start = self.rsvd + self.nfats * self.fat_sectors + self.root_sectors
        self.clusters = (total - self.data_start) // self.spc
        self.bits = 12 if self.clusters < 4085 else 16 if self.clusters < 65525 else 32
        if self.bits == 12:
            raise ValueError('FAT12 cards are not supported')
        self.root_cluster = struct.unpack_from('<I', vbr, 44)[0] if self.bits == 32 else None
        self.fsinfo = struct.unpack_from('<H', vbr, 48)[0] if self.bits == 32 else None
        raw = self._read(self.rsvd, self.fat_sectors)
        n = self.clusters + 2
        self.fat = list(struct.unpack_from(f'<{n}{"I" if self.bits == 32 else "H"}', raw))
        if self.bits == 32:
            self.fat = [v & 0x0FFFFFFF for v in self.fat]
        self.eoc = 0x0FFFFFF8 if self.bits == 32 else 0xFFF8

    @staticmethod
    def _is_vbr(s):
        return s[510:512] == b'\x55\xaa' and struct.unpack_from('<H', s, 11)[0] in (512, 1024, 2048, 4096) \
            and s[13] in (1, 2, 4, 8, 16, 32, 64, 128) and s[16] in (1, 2)

    def _sector(self, base, n, count=1):
        self.f.seek((base + n) * SECTOR)
        return self.f.read(SECTOR * count)

    def _read(self, sector, count):
        self.f.seek((self.start + sector) * self.bps)
        return self.f.read(count * self.bps)

    def _write(self, sector, data):
        self.f.seek((self.start + sector) * self.bps)
        self.f.write(data)

    def _cluster_sector(self, c):
        return self.data_start + (c - 2) * self.spc

    def _chain(self, c):
        out = []
        while 2 <= c < self.eoc and len(out) <= self.clusters:
            out.append(c)
            c = self.fat[c]
        return out

    def _dir_slots(self):
        """(sector, offset) of every root directory entry."""
        if self.bits == 32:
            for c in self._chain(self.root_cluster):
                for s in range(self.spc):
                    for o in range(0, self.bps, 32):
                        yield self._cluster_sector(c) + s, o
        else:
            for s in range(self.root_sectors):
                for o in range(0, self.bps, 32):
                    yield self.rsvd + self.nfats * self.fat_sectors + s, o

    def _alloc(self, n, after=2):
        free = []
        for c in range(after, self.clusters + 2):
            if self.fat[c] == 0:
                free.append(c)
                if len(free) == n:
                    break
        if len(free) < n:
            raise ValueError('the card is full')
        for a, b in zip(free, free[1:]):
            self.fat[a] = b
        if free:
            self.fat[free[-1]] = 0x0FFFFFFF if self.bits == 32 else 0xFFFF
        return free

    def put(self, name, data):
        """Write a file into the root directory, replacing one of the same name."""
        short = _short_name(name)
        slot = free_slot = None
        for sector, off in self._dir_slots():
            e = self._read(sector, 1)[off:off + 32]
            if e[0] == 0:
                free_slot = free_slot or (sector, off)
                break
            if e[0] == 0xE5:
                free_slot = free_slot or (sector, off)
                continue
            if e[11] & 0x0F == 0x0F or e[11] & 0x08:
                continue
            if e[:11] == short:
                slot = (sector, off)
                first = struct.unpack_from('<H', e, 26)[0] | (struct.unpack_from('<H', e, 20)[0] << 16)
                for c in self._chain(first):
                    self.fat[c] = 0
                break
        slot = slot or free_slot
        if slot is None:
            if self.bits != 32:
                raise ValueError('the root directory is full')
            last = self._chain(self.root_cluster)[-1]
            new = self._alloc(1)[0]
            self.fat[last] = new
            self._write(self._cluster_sector(new), bytes(self.spc * self.bps))
            slot = (self._cluster_sector(new), 0)
        cluster_bytes = self.spc * self.bps
        chain = self._alloc((len(data) + cluster_bytes - 1) // cluster_bytes)
        for i, c in enumerate(chain):
            self._write(self._cluster_sector(c), data[i * cluster_bytes:(i + 1) * cluster_bytes])
        date, clock = _dos_datetime(time.time())
        first = chain[0] if chain else 0
        entry = short + bytes([0x20, 0, 0]) + struct.pack('<HHHHHHHI', clock, date, date, first >> 16, clock, date,
                                                          first & 0xFFFF, len(data))
        sector, off = slot
        block = bytearray(self._read(sector, 1))
        block[off:off + 32] = entry
        self._write(sector, bytes(block))
        if self.bits == 32:
            fat = struct.pack(f'<{len(self.fat)}I', *self.fat)
        else:
            fat = struct.pack(f'<{len(self.fat)}H', *self.fat)
        for i in range(self.nfats):
            self._write(self.rsvd + i * self.fat_sectors, fat)
        if self.fsinfo:
            info = bytearray(self._read(self.fsinfo, 1))
            if info[:4] == b'RRaA':
                info[488:496] = b'\xff' * 8          # free count and next free unknown: FatFs counts again
                self._write(self.fsinfo, bytes(info))

    def get(self, name):
        """A file of the root directory, or None."""
        short = _short_name(name)
        for sector, off in self._dir_slots():
            e = self._read(sector, 1)[off:off + 32]
            if e[0] == 0:
                return None
            if e[0] == 0xE5 or e[11] & 0x0F == 0x0F or e[11] & 0x08 or e[:11] != short:
                continue
            first = struct.unpack_from('<H', e, 26)[0] | (struct.unpack_from('<H', e, 20)[0] << 16)
            size = struct.unpack_from('<I', e, 28)[0]
            data = b''.join(self._read(self._cluster_sector(c), self.spc) for c in self._chain(first))
            return data[:size]
        return None


    def entries(self, cluster=None):
        """(name, attributes, first cluster, size) in a directory (the root directory by default)."""
        if cluster is None:
            slots = self._dir_slots()
        else:
            slots = ((self._cluster_sector(c) + s, o) for c in self._chain(cluster) for s in range(self.spc)
                     for o in range(0, self.bps, 32))
        cache = {}
        for sector, off in slots:
            if sector not in cache:
                cache = {sector: self._read(sector, 1)}
            e = cache[sector][off:off + 32]
            if e[0] == 0:
                return
            if e[0] == 0xE5 or e[11] & 0x0F == 0x0F or e[11] & 0x08 or e[0] == 0x2E:
                continue
            base, ext = e[:8].decode('latin-1').rstrip(), e[8:11].decode('latin-1').rstrip()
            first = struct.unpack_from('<H', e, 26)[0] | (struct.unpack_from('<H', e, 20)[0] << 16)
            yield (base + ('.' + ext if ext else '')), e[11], first, struct.unpack_from('<I', e, 28)[0]

    def lookup(self, path):
        """(attributes, first cluster, size) of a path like LOGS/2026/11/24.LOG, or None."""
        cluster = None
        parts = path.upper().split('/')
        for i, part in enumerate(parts):
            for name, attr, first, size in self.entries(cluster):
                if name == part:
                    if i == len(parts) - 1:
                        return attr, first, size
                    if not attr & 0x10:
                        return None
                    cluster = first
                    break
            else:
                return None

    def read(self, first, start, end):
        """Bytes [start, end) of the file whose chain starts at first."""
        cluster_bytes = self.spc * self.bps
        chain = self._chain(first)
        out = bytearray()
        for i in range(start // cluster_bytes, (end + cluster_bytes - 1) // cluster_bytes):
            if i >= len(chain):
                break
            out += self._read(self._cluster_sector(chain[i]), self.spc)
        skip = start % cluster_bytes
        return bytes(out[skip:skip + end - start])


class LogFollower:
    """Prints the lines the firmware appends to its newest day log files (v1.007: LOGS/YYYY/MM/DD.LOG and .ERR)."""

    def __init__(self, card, out):
        self.card = card
        self.out = out
        self.pos = None
        self.partial = {}

    def _newest(self, v):
        found = v.lookup('LOGS')
        if not found or not found[0] & 0x10:
            return []
        path, cluster = 'LOGS', found[1]
        for _ in range(2):
            dirs = sorted(n for n, a, _, _ in v.entries(cluster) if a & 0x10)
            if not dirs:
                return []
            path += '/' + dirs[-1]
            cluster = v.lookup(path)[1]
        days = sorted(n for n, a, _, _ in v.entries(cluster) if not a & 0x10)
        out = []
        for ext in ('LOG', 'ERR'):
            names = [n for n in days if n.endswith('.' + ext)]
            if names:
                out.append(path + '/' + names[-1])
        return out

    def poll(self):
        try:
            with open(self.card, 'rb') as f:
                v = Volume(f)
                first = self.pos is None
                self.pos = self.pos or {}
                for path in self._newest(v):
                    _, cluster, size = v.lookup(path)
                    pos = self.pos.get(path, size if first else 0)
                    if size > pos:
                        text = self.partial.get(path, b'') + v.read(cluster, pos, size)
                        *lines, rest = text.split(b'\n')
                        self.partial[path] = rest
                        for line in lines:
                            self.out(line.decode('latin-1').rstrip('\r'), path.endswith('.ERR'))
                    self.pos[path] = max(pos, size)
        except (ValueError, OSError, struct.error, TypeError):
            pass        # the card changes under us: try again next time


def put_file(path, name, data):
    with open(path, 'r+b') as f:
        Volume(f).put(name, data)


def get_file(path, name):
    with open(path, 'rb') as f:
        return Volume(f).get(name)
