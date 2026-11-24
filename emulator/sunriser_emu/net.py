"""The device's network: libslirp (user-mode networking, through ctypes) answers its DHCP request and forwards a host
port to its web server. Offline (the default) nothing the device sends leaves 10.0.2.0/24 - like a LAN whose router
has no uplink: it gets a gateway and a DNS server, but its DNS queries and connections of its own go nowhere."""
import ctypes
import ctypes.util
import os
import select
import socket
import threading
import time

GUEST = '10.0.2.15'
POLL_IN, POLL_OUT, POLL_PRI, POLL_ERR, POLL_HUP = 1, 2, 4, 8, 16


class InAddr(ctypes.Structure):
    _fields_ = [('s_addr', ctypes.c_uint32)]


class In6Addr(ctypes.Structure):
    _fields_ = [('a', ctypes.c_uint32 * 4)]


class SlirpConfig(ctypes.Structure):     # libslirp's SlirpConfig, fields of version 1
    _fields_ = [('version', ctypes.c_uint32), ('restricted', ctypes.c_int), ('in_enabled', ctypes.c_bool),
                ('vnetwork', InAddr), ('vnetmask', InAddr), ('vhost', InAddr), ('in6_enabled', ctypes.c_bool),
                ('vprefix_addr6', In6Addr), ('vprefix_len', ctypes.c_uint8), ('vhost6', In6Addr),
                ('vhostname', ctypes.c_char_p), ('tftp_server_name', ctypes.c_char_p),
                ('tftp_path', ctypes.c_char_p), ('bootfile', ctypes.c_char_p), ('vdhcp_start', InAddr),
                ('vnameserver', InAddr), ('vnameserver6', In6Addr), ('vdnssearch', ctypes.c_void_p),
                ('vdomainname', ctypes.c_char_p), ('if_mtu', ctypes.c_size_t), ('if_mru', ctypes.c_size_t),
                ('disable_host_loopback', ctypes.c_bool), ('enable_emu', ctypes.c_bool)]


SEND = ctypes.CFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p)
ERROR = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_void_p)
CLOCK = ctypes.CFUNCTYPE(ctypes.c_int64, ctypes.c_void_p)
TIMER_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
TIMER_NEW = ctypes.CFUNCTYPE(ctypes.c_void_p, TIMER_CB, ctypes.c_void_p, ctypes.c_void_p)
TIMER_FREE = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
TIMER_MOD = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int64, ctypes.c_void_p)
FD = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_void_p)
NOTIFY = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
ADD_POLL = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
GET_REVENTS = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_void_p)


class SlirpCb(ctypes.Structure):
    _fields_ = [('send_packet', SEND), ('guest_error', ERROR), ('clock_get_ns', CLOCK), ('timer_new', TIMER_NEW),
                ('timer_free', TIMER_FREE), ('timer_mod', TIMER_MOD), ('register_poll_fd', FD),
                ('unregister_poll_fd', FD), ('notify', NOTIFY), ('init_completed', ctypes.c_void_p),
                ('timer_new_opaque', ctypes.c_void_p), ('register_poll_socket', ctypes.c_void_p),
                ('unregister_poll_socket', ctypes.c_void_p)]


def _load():
    names = [os.environ.get('SUNRISER_LIBSLIRP'), ctypes.util.find_library('slirp'), 'libslirp.so.0',
             'libslirp.dylib', '/opt/homebrew/lib/libslirp.dylib', '/usr/local/lib/libslirp.dylib']
    for name in filter(None, names):
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError:
            continue
    else:
        raise RuntimeError('libslirp not found (Debian/Ubuntu: apt install libslirp0, macOS: brew install libslirp;'
                           ' or set SUNRISER_LIBSLIRP to the library)')
    lib.slirp_new.restype = ctypes.c_void_p
    lib.slirp_new.argtypes = [ctypes.POINTER(SlirpConfig), ctypes.POINTER(SlirpCb), ctypes.c_void_p]
    lib.slirp_input.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    lib.slirp_pollfds_fill.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ADD_POLL, ctypes.c_void_p]
    lib.slirp_pollfds_poll.argtypes = [ctypes.c_void_p, ctypes.c_int, GET_REVENTS, ctypes.c_void_p]
    lib.slirp_add_hostfwd.argtypes = [ctypes.c_void_p, ctypes.c_int, InAddr, ctypes.c_int, InAddr, ctypes.c_int]
    return lib


def _addr(s):
    return InAddr(int.from_bytes(socket.inet_aton(s), 'little'))


def offline_pass(f):
    """What the device may send offline: ARP, DHCP, ICMP and the segments of connections that came in through the
    forwarded port, all within 10.0.2.0/24."""
    if len(f) < 34:
        return len(f) >= 14 and f[12:14] == b'\x08\x06'
    if f[12:14] == b'\x08\x06':
        return True
    if f[12:14] != b'\x08\x00' or f[14] >> 4 != 4:
        return False
    ihl = (f[14] & 15) * 4
    dst = f[30:34]
    if ihl < 20 or not (dst == b'\xff\xff\xff\xff' or dst[:3] == b'\x0a\x00\x02'):
        return False
    l4 = f[14 + ihl:]
    proto = f[23]
    if proto == 1:
        return True
    if proto == 6:
        return len(l4) >= 20 and (l4[13] & 0x12) != 0x02
    if proto == 17:
        return len(l4) >= 8 and int.from_bytes(l4[2:4], 'big') == 67
    return False


class Network:
    def __init__(self, port, listen, online, log):
        self.lib = _load()
        self.log = log
        self.online = online
        self.deliver = None                     # set by the machine: frame -> device
        self.timers = {}
        self.pollfds = []
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_r, False)
        self.frames_blocked = 0
        cfg = SlirpConfig(version=1, in_enabled=True, vnetwork=_addr('10.0.2.0'), vnetmask=_addr('255.255.255.0'),
                          vhost=_addr('10.0.2.2'), vdhcp_start=_addr(GUEST), vnameserver=_addr('10.0.2.3'),
                          vhostname=b'sunriser-emulator', disable_host_loopback=not online)
        self._cb = SlirpCb(send_packet=SEND(self._send_packet), guest_error=ERROR(self._guest_error),
                           clock_get_ns=CLOCK(lambda _: time.monotonic_ns()), timer_new=TIMER_NEW(self._timer_new),
                           timer_free=TIMER_FREE(self._timer_free), timer_mod=TIMER_MOD(self._timer_mod),
                           register_poll_fd=FD(lambda fd, _: None), unregister_poll_fd=FD(lambda fd, _: None),
                           notify=NOTIFY(lambda _: None))
        self._cfg = cfg
        self._add_poll = ADD_POLL(self._add_poll_cb)
        self._get_revents = GET_REVENTS(self._get_revents_cb)
        self.slirp = self.lib.slirp_new(ctypes.byref(cfg), ctypes.byref(self._cb), None)
        if not self.slirp:
            raise RuntimeError('slirp_new failed')
        loopback = listen.startswith('127.')
        fwd_port = port if loopback else self._relay(listen, port)
        if self.lib.slirp_add_hostfwd(self.slirp, 0, _addr(listen if loopback else '127.0.0.1'), fwd_port,
                                      _addr(GUEST), 80) != 0:
            raise RuntimeError(f'cannot listen on {listen}:{port} (already in use?)')

    # libslirp callbacks
    def _send_packet(self, buf, n, _):
        if self.deliver:
            self.deliver(ctypes.string_at(buf, n))
        return n

    def _guest_error(self, msg, _):
        self.log(f'network: {msg.decode(errors="replace")}')

    def _timer_new(self, cb, opaque, _):
        key = len(self.timers) + 1
        while key in self.timers:
            key += 1
        self.timers[key] = [cb, opaque, None]
        return key

    def _timer_free(self, key, _):
        self.timers.pop(key, None)

    def _timer_mod(self, key, expire_ms, _):
        if key in self.timers:
            self.timers[key][2] = expire_ms

    def _add_poll_cb(self, fd, events, _):
        self.pollfds.append([fd, events, 0])
        return len(self.pollfds) - 1

    def _get_revents_cb(self, idx, _):
        return self.pollfds[idx][2]

    def send(self, frame):
        """A frame from the device."""
        if not self.online and not offline_pass(frame):
            self.frames_blocked += 1
            return
        self.lib.slirp_input(self.slirp, frame, len(frame))

    def poll(self, timeout):
        """Serve the host side for up to timeout seconds (less if something arrives)."""
        tmo = ctypes.c_uint32(max(0, int(timeout * 1000)))
        self.pollfds = []
        self.lib.slirp_pollfds_fill(self.slirp, ctypes.byref(tmo), self._add_poll, None)
        p = select.poll()
        p.register(self.wake_r, select.POLLIN)
        for fd, ev, _ in self.pollfds:
            mask = ((select.POLLIN if ev & POLL_IN else 0) | (select.POLLOUT if ev & POLL_OUT else 0)
                    | (select.POLLPRI if ev & POLL_PRI else 0))
            p.register(fd, mask)
        wait = min(int(timeout * 1000), tmo.value)
        try:
            ready = dict(p.poll(wait))
            err = 0
        except InterruptedError:
            ready, err = {}, 1
        if self.wake_r in ready:
            try:
                os.read(self.wake_r, 64)
            except BlockingIOError:
                pass
        for item in self.pollfds:
            r = ready.get(item[0], 0)
            item[2] = ((POLL_IN if r & select.POLLIN else 0) | (POLL_OUT if r & select.POLLOUT else 0)
                       | (POLL_PRI if r & select.POLLPRI else 0) | (POLL_ERR if r & select.POLLERR else 0)
                       | (POLL_HUP if r & select.POLLHUP else 0))
        self.lib.slirp_pollfds_poll(self.slirp, err, self._get_revents, None)
        now = time.monotonic_ns() // 1_000_000
        for key, (cb, opaque, expire) in list(self.timers.items()):
            if expire is not None and expire <= now and key in self.timers:
                self.timers[key][2] = None
                cb(opaque)

    def _relay(self, listen, port):
        """Connections on a non-loopback address are relayed through a loopback forward: libslirp shows the device
        the real peer address otherwise, and offline its answers to that peer would not leave its network."""
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            ls.bind((listen, port))
        except OSError as e:
            raise RuntimeError(f'cannot listen on {listen}:{port}: {e}') from e
        ls.listen(16)
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        target = probe.getsockname()[1]
        probe.close()

        def pump(a, b):
            try:
                while True:
                    data = a.recv(16384)
                    if not data:
                        break
                    b.sendall(data)
            except OSError:
                pass
            for s in (a, b):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        def conn(c):
            try:
                b = socket.create_connection(('127.0.0.1', target))
            except OSError:
                c.close()
                return
            threading.Thread(target=pump, args=(b, c), daemon=True).start()
            pump(c, b)
            c.close()
            b.close()

        def accept():
            while True:
                c, _ = ls.accept()
                threading.Thread(target=conn, args=(c,), daemon=True).start()

        threading.Thread(target=accept, daemon=True).start()
        return target
