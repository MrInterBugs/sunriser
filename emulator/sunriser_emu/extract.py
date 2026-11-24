"""Write the firmware images of a .bee as main.bin, bootloader.bin, main10.bin and bootloader10.bin: the files
bin/sunriser_factory packs into a new .bee next to the web interface it publishes.

  python -m sunriser_emu.extract NEW.BEE [directory]"""
import os
import sys

from .bee import Bee

FILES = {'firmware': 'main.bin', 'bootloader': 'bootloader.bin', 'firmware10': 'main10.bin',
         'bootloader10': 'bootloader10.bin'}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not 1 <= len(argv) <= 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    bee = Bee(argv[0])
    out = argv[1] if len(argv) > 1 else '.'
    print(bee.description)
    for name, filename in FILES.items():
        if name in bee.raw:
            path = os.path.join(out, filename)
            with open(path, 'wb') as f:
                f.write(bee.raw[name])
            print(f'{path}: {len(bee.raw[name])} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
