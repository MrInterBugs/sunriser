# SunRiser emulator

Runs the firmware of a SunRiser update file (`.bee`) on an emulated SunRiser 8 (STM32F107) or SunRiser 10
(STM32F407). Nothing answers in place of the device: the bootloader and the application are the ARM images inside
the `.bee`, executed instruction by instruction, and the web interface on http://localhost:8080/ is the one in the
`.bee`, served by that firmware's own HTTP server. The emulator provides only the hardware below it.

```sh
cd emulator
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt    # tested with Python 3.12 and 3.15
.venv/bin/python -m sunriser_emu --bee NEW.BEE                         # SunRiser 8;  --board 10 for the SunRiser 10
open http://localhost:8080/
```

It needs libslirp (Debian/Ubuntu: `apt install libslirp0`, macOS: `brew install libslirp`). The current firmware is
the vendor's http://sunriser.ledaquaristik.de/NEW.BEE (v1.007, SHA-256 `725b39ab…6e2415377`); any other update file
works the same way.

With Docker, from this directory (the update file `./NEW.BEE`, or `BEE=/path/to/file.bee`):

```sh
docker compose up --build            # SunRiser 8 on http://localhost:8080/
BOARD=10 docker compose up           # SunRiser 10
```

## What happens

A new device (in `state/sr8`, or `--state <dir>`) gets the bootloader and application of the `.bee` in its flash and
an SD card image with the `.bee` as `NEW.BEE` and `FACTORY.BEE`. From there the firmware does what a real device
does: it finds `NEW.BEE`, installs it and restarts; the bootloader starts the application, which serves the web
interface out of `FIRMWARE.BEE` on the card. (The first start logs `no replacement firmware found!!! FATAL!!!`: a card
with only `NEW.BEE` on it has no `FIRMWARE.BEE` yet. The firmware then installs it.)

The device keeps its state between runs: flash, card (configuration, logs) and the battery-backed clock. Giving an
existing device another `.bee` with `--bee` copies it onto the card as `NEW.BEE`, as a user does to update a device:
the firmware installs it, and its bootloader programs the new application into flash. `--fresh` starts over.

The console shows the firmware's log (the day files it writes to `LOGS/` on the card), the LED outputs (the PWM duty
cycle of every timer channel) and, on the SunRiser 10, the colour of the status LED.

```
--bee FILE        update file (a new device starts with it; an existing one installs it)
--board 8|10      SunRiser 8 or 10 (default: the state's, else 8)
--state DIR       device state (default state/sr<board>); --fresh replaces it
--port N          host port forwarded to the device's port 80 (8080); --listen ADDR (127.0.0.1)
--online          let the device reach other networks (default: offline, see below)
--mac MAC         MAC address of a new device (default: random, locally administered)
--card-size MIB   size of a new card image (512)
--speed N         run the device's time N times faster
-q                no firmware log on the console
```

## What is emulated

| part | model |
|---|---|
| CPU | Cortex-M3 / Cortex-M4F (Unicorn); exceptions, NVIC priorities, SysTick, the FPU's extended frames |
| time | the device's clock follows the host's (`--speed` scales it); the CPU runs as fast as the host executes it (on an Apple M-series Mac about the speed of the real SunRiser 8; it keeps one core busy, as the firmware's main loop never sleeps) |
| flash | the flash interface: unlock, page (F1) or sector (F4) erase, programming by stores; kept in `flash.bin` |
| SD card | an SDHC card in SPI mode on SPI3 (chip select PD2), backed by `sdcard.img` (FAT32) |
| Ethernet | the MAC's descriptor DMA and a LAN8720 PHY; frames go to libslirp, which answers DHCP and forwards the port |
| MAC address | a 24AA02E48 EEPROM on I2C1 |
| RTC | the F1 seconds counter and the F4 calendar with its wakeup timer; backup registers; kept in `backup.json` |
| timers | counters, update interrupts and the compare registers (the LED outputs) |
| RCC, PWR, GPIO, EXTI, CRC | as far as the firmware uses them; the reset flags show power-on and software resets |

Not emulated: the SunRiser 10's WiFi module (the firmware runs without it), temperature sensors (none found), the
watchdog (v1.006/v1.007 do not enable it), and physical timing. The device is **offline by default**: it gets an
address (10.0.2.15), a gateway and a DNS server by DHCP, but nothing it sends leaves 10.0.2.0/24, so it reaches
neither the vendor's finder service nor NTP. `--online` lifts that.

## Testing a changed web interface

`bin/sunriser_factory` packs the web interface of this repository (`share/`) into a `.bee`, together with the
firmware images it finds in the current directory. Take them from a released update file:

```sh
cd emulator
.venv/bin/python -m sunriser_emu.extract /path/to/NEW.BEE ..   # main.bin, bootloader.bin, main10.bin, bootloader10.bin
cd .. && bin/sunriser_factory DEV.BEE                            # this repository's web interface and those images
cd emulator && .venv/bin/python -m sunriser_emu --bee ../DEV.BEE
```

`t/real.t` runs against the emulator as against a device: `SUNRISER_TEST_REAL=127.0.0.1:8080 prove -l t/real.t`.

## Tests

```sh
python3 -m unittest discover -s emulator/tests                               # the parts
SUNRISER_BEE=NEW.BEE python3 -m unittest discover -s emulator/tests          # and both boards, end to end
```
