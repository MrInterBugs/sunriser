"""sunriser-emulator: run the firmware of a SunRiser update file (.bee) on an emulated SunRiser 8 or 10."""
import argparse
import json
import os
import signal
import sys

from . import fat
from .bee import Bee
from .machine import BOARDS, Machine, create_state


def parse_mac(s):
    parts = s.replace('-', ':').split(':')
    if len(parts) != 6:
        raise argparse.ArgumentTypeError('MAC address: six hex bytes, e.g. 02:00:00:12:34:56')
    return bytes(int(p, 16) for p in parts)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='sunriser-emulator', description=__doc__)
    ap.add_argument('--bee', help='update file: a new device gets its firmware; an existing one finds it on its card '
                                   'as NEW.BEE and installs it, as a device does after a card update')
    ap.add_argument('--board', type=int, choices=sorted(BOARDS), help='8 or 10 (default: the state\'s, else 8)')
    ap.add_argument('--state', help='device state directory (flash, card image, clock); default state/sr<board>')
    ap.add_argument('--fresh', action='store_true', help='start with a new device (replaces the state)')
    ap.add_argument('--port', type=int, default=8080, help='host port forwarded to the device\'s web server (8080)')
    ap.add_argument('--listen', default='127.0.0.1', help='address of that port (127.0.0.1; 0.0.0.0 in Docker)')
    ap.add_argument('--online', action='store_true', help='let the device reach other networks (default: offline)')
    ap.add_argument('--card-size', type=int, default=512, metavar='MIB', help='size of a new card image (512)')
    ap.add_argument('--mac', type=parse_mac, help='MAC address of a new device (default: random, locally administered)')
    ap.add_argument('--speed', type=float, default=1.0, help='run the device\'s time n times faster (if the host can)')
    ap.add_argument('-q', '--quiet', action='store_true', help='do not print the firmware\'s debug output')
    args = ap.parse_args(argv)

    board = args.board
    state = args.state
    if state and not board and os.path.exists(os.path.join(state, 'state.json')):
        with open(os.path.join(state, 'state.json')) as f:
            board = json.load(f)['board']
    board = board or 8
    state = state or os.path.join('state', f'sr{board}')
    meta_path = os.path.join(state, 'state.json')
    try:
        bee = Bee(args.bee) if args.bee else None
        if bee:
            bee.images(board)
    except (OSError, ValueError) as e:
        ap.error(str(e))

    if args.fresh or not os.path.exists(meta_path):
        if not bee:
            ap.error(f'no device in {state} yet: give the update file to start from with --bee')
        meta = create_state(state, board, bee, args.card_size, args.mac)
        print(f'new SunRiser {board} in {state}: {bee.description} (sha256 {bee.sha256[:16]}...), '
              f'MAC {":".join(meta["mac"][i:i + 2] for i in range(0, 12, 2))}', file=sys.stderr)
    else:
        with open(meta_path) as f:
            meta = json.load(f)
        if meta['board'] != board:
            ap.error(f'{state} holds a SunRiser {meta["board"]}, not a SunRiser {board}')
        if bee:
            card = os.path.join(state, 'sdcard.img')
            if fat.get_file(card, 'FIRMWARE.BEE') == bee.data:
                print(f'{bee.description} is already installed', file=sys.stderr)
            else:
                fat.put_file(card, 'NEW.BEE', bee.data)
                print(f'{bee.description} copied onto the card as NEW.BEE: the device installs it now',
                      file=sys.stderr)

    from .net import Network
    network = Network(args.port, args.listen, args.online, lambda msg: print(f'* {msg}', file=sys.stderr))
    m = Machine(board, state, network, quiet=args.quiet, speed=args.speed)
    print(f'SunRiser {board} ({m.board.mcu}) on http://{args.listen}:{args.port}/ '
          f'({"online" if args.online else "offline"}); Ctrl-C stops it', file=sys.stderr)

    def stop(*_):
        m.stop_reason = 'stopped'
        m.stop()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    reason = m.run()
    print(f'* {reason}', file=sys.stderr)
    return 0 if reason == 'stopped' else 1


if __name__ == '__main__':
    sys.exit(main())
