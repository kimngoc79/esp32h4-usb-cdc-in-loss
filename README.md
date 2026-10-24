| Supported Targets | ESP32-H4 |
| ----------------- | -------- |

# ESP32-H4 USB CDC: IN (device to host) data loss

This repository reproduces a USB CDC-ACM problem on the **ESP32-H4**: data sent by the device to the host
(bulk IN) is lost, although the device queues every byte correctly. The firmware is the
ESP-IDF `peripherals/usb/device/tusb_serial_device` example with the smallest change needed for a repeatable IN test,
plus a host script that checks every reply byte by byte.

The same application code (fixed-size replies, zero padded) works on an ESP32-S3 with esp_tinyusb 1.x.

## Summary of the findings

1. **Head of the reply lost (host controller reports babble).** USBPcap shows that the device sends every reply
   complete and byte-exact, but the host controller completes the first bulk IN URB(s) of a reply with
   `USBD_STATUS_BABBLE_DETECTED (0xC0000012)`. usbser.sys drops the data of those URBs, so the application gets only
   the tail. The loss grows with the reply length: about 10 % at 128 bytes, 100 % at 8192 bytes (table below).
2. **Load access fault on a SETUP packet.** When the host opens the port, the device sometimes crashes in
   `handle_epout_dma` (`dcd_dwc2.c:1065`) → `dcd_event_setup_received` (`dcd.h:221`): the setup packet address is taken
   from `DOEPDMA0 - 8`, and `DOEPDMA0` reads near zero. The host then reports "A device attached to the system is not
   functioning".

## Test setup

| Item | Value |
|---|---|
| Chip | ESP32-H4, chip revision v0.2 (`ESP-ROM:esp32h4-eco2-20260611`) |
| ESP-IDF | master `383dda6c` (2026-10-19), reports v6.2.0; esp32h4 needs `idf.py --preview` |
| Components | `espressif/esp_tinyusb` 2.4.0, `espressif/tinyusb` 0.21.0~2 (see `dependencies.lock`) |
| USB | internal full-speed PHY, 1 CDC-ACM port, VID:PID `303A:4001`, TinyUSB DCD in DMA mode |
| Host | Windows 11, in-box `usbser.sys`, Python 3.14 + pyserial 3.5 |
| Capture | USBPcap 1.5.4.0, Wireshark/tshark 4.6.9 |

## Changes against the upstream example

| File | Change |
|---|---|
| `main/tusb_serial_device_main.c` | The echo and its application queue are replaced by the IN test: the RX callback (TinyUSB task) reads an 8-byte request `{magic, size}`, `malloc`s `size` bytes, fills them with fixed data, queues the whole buffer with `tinyusb_cdcacm_write_queue`, starts it with `tinyusb_cdcacm_write_flush(itf, 0)` and frees the buffer. The TinyUSB task is the only CDC writer. Each reply is logged on the UART console with its CRC32. |
| `sdkconfig.defaults`, `sdkconfig` | `CONFIG_TINYUSB_CDC_TX_BUFSIZE` 2048 → 8192, so an 8 KB reply is queued at once. `CONFIG_TINYUSB_MODE_SLAVE` → `CONFIG_TINYUSB_MODE_DMA` (the esp_tinyusb default; the example selects slave mode). Everything else is the example's configuration (1 CDC). |
| `tools/usb_in_test.py` | Host test (below). |
| `tools/usb_in_pcap.py` | USBPcap capture analysis (below). |

`pytest_usb_device_serial.py` and `sdkconfig.ci*` are the upstream CI files of the echo example; they do not apply
to this firmware.

## Test protocol

```mermaid
sequenceDiagram
    participant H as Host (tools/usb_in_test.py)
    participant D as ESP32-H4 (TinyUSB task)
    H->>D: bulk OUT: request {magic 0x4E495354 "TSIN", size} (2 x u32 LE)
    D->>D: malloc(size), fill: 16-byte ID, then 0x00
    D->>H: bulk IN: size bytes (whole reply queued at once)
    H->>H: compare with the expected bytes, next request
```

Reply data (fixed): `04 C8 F4 CF 7D 5E B0 4F B6 FF C5 D6 7D 96 63 57`, then zeros up to `size`
(shorter replies are cut). This is the shape of a "read ID" reply of our product: a short ID padded with zeros.

Sizes: 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192 bytes, 100 replies each. A reply ends when the host gets
`size` bytes or no byte for 1 s; when no byte arrives for 30 s (or the device takes no request) the test stops and
the report is still written.

## Requirements

### Hardware

- An ESP32-H4 board (we use chip revision v0.2).
- **Two USB connections to the host PC:**
  - the H4 **USB OTG** pins (D-/D+) to the PC: this is the CDC port under test (`303A:4001`);
  - the board **UART console** (USB-UART bridge, e.g. CP210x) to the PC: used to flash and to read the device log.
  If your board has only one of them wired, wire the other one (see the board schematic for the H4 USB D-/D+ pins).
- A good USB cable, connected directly to a PC port (note the port: results can change with the port or a hub).

### Software (build and flash)

- **ESP-IDF master** with ESP32-H4 support. The results were measured with commit `383dda6c` (2026-10-19); a newer
  master should also work. ESP32-H4 is a preview target, so every `idf.py` command needs `--preview`.
  ```
  git clone --recursive https://github.com/espressif/esp-idf.git
  cd esp-idf
  git checkout 383dda6c && git submodule update --init --recursive    # optional: the exact version we used
  ./install.ps1 esp32h4            # Windows PowerShell   (Linux/macOS: ./install.sh esp32h4)
  . ./export.ps1                   # in every new shell  (Linux/macOS: . ./export.sh)
  ```
- **Internet access on the first build:** the IDF Component Manager downloads `espressif/esp_tinyusb` 2.4.0 and
  `espressif/tinyusb` 0.21.0~2 (versions pinned in `dependencies.lock`) into `managed_components/`.
- A USB-UART driver for the board's bridge if the OS does not have one (e.g. Silicon Labs CP210x).

### Software (test)

- **Python 3.8 or newer** (measured with 3.14) and pyserial: `pip install pyserial`. The ESP-IDF Python environment
  also works (it has pyserial).
- The CDC driver of the OS: Windows 10/11 in-box `usbser.sys` (no installation, the port appears as
  "USB Serial Device (COMx)"); Linux `cdc_acm` (`/dev/ttyACM*`, add the user to the `dialout` group).

### Software (optional USB capture, Windows only)

- **USBPcap** (https://desowin.org/usbpcap/, or `winget install desowin.USBPcap`). It installs a USB filter driver:
  administrator rights, and **reboot Windows** after the installation.
- **Wireshark** (https://www.wireshark.org/, or `winget install WiresharkFoundation.Wireshark`) for `tshark.exe`.
  Npcap is not needed: `tshark` only reads the capture file.

## Build and flash

In a shell where ESP-IDF is exported, from the repository root. On Windows, clone the repository into a short path
(for example `D:\h4usb`): a deep path makes the build exceed the 260-character path limit and fails with
"soc/soc_caps.h: No such file or directory".

```
idf.py --preview build                   # -> build/
idf.py --preview -p <UART port> flash    # or from build/: python -m esptool --chip esp32h4 -p <UART port> write-flash @flash_args
```

`sdkconfig` in the repository is already for esp32h4 (DMA mode, 1 CDC, TX FIFO 8192), so `set-target` is not
needed; running it would reset `sdkconfig` to the defaults. If `sdkconfig` is deleted, build with
`idf.py --preview -DIDF_TARGET=esp32h4 build` to recreate it from `sdkconfig.defaults`.

On our board the automatic reset into download mode does not work: hold BOOT, press RESET, release BOOT, flash,
then press RESET to run. Boards with a working auto-reset can use `idf.py flash` directly.

## Run the test

```
pip install pyserial
python tools/usb_in_test.py --log-port <UART port>       # all sizes, 100 replies each
python tools/usb_in_test.py --port COM4 --sizes 2048 --count 1000
```

The CDC port is found by VID:PID `303A:4001` (or `--port`). `--log-port` reads the device UART log and checks that the
device queued every reply with the expected size and CRC32, and reports any panic or reboot. Output goes to
`test_reports/`: `usb_in_test_<time>.md` (report), `_pc.log`, `_fail.log` (hex of failed replies), `_uart.log`.

### Running it

1. Press RESET on the board and wait about 5 s (until the `303A:4001` port is enumerated).
2. Run the whole size table (16 B to 8 KB). If the device crashes when the host opens the port, the test stops at
   once with "cannot open ... see the device log for a panic": press RESET and run again.
3. Optionally, repeat a run (for example `--sizes 2048 8192 --count 30`) with a USB capture (next section) to see
   what the host controller received.

### Capture on the host (Windows)

1. Install USBPcap (reboot Windows afterwards) and Wireshark.
2. Find the root hub and device address of the board: `USBPcapCMD.exe --extcap-interface \\.\USBPcapN --extcap-config`
   lists the devices; the board is the parent of "USB Serial Device (COMx)".
3. Capture only that device while the test runs:
   `USBPcapCMD.exe -d \\.\USBPcapN --devices <address> -b 16777216 -o capture.pcap`
4. `python tools/usb_in_pcap.py capture.pcap` prints, per size, whether the host controller received every reply
   (failed URBs included), how many URBs failed and with which USBD status, and what the application could see.

## Results (this repository)

`python tools/usb_in_test.py --log-port COM40`, a run where the device did not crash on the port open:

| Size (B) | 16 | 32 | 64 | 128 | 256 | 512 | 1024 | 2048 | 4096 | 8192 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Replies OK /100 | 90 | 100 | 100 | 88 | 75 | 65 | 38 | 20 | 4 | 0 |
| Head lost | 0 | 0 | 0 | 12 | 25 | 35 | 62 | 80 | 96 | 96 |

Device log: 1000/1000 replies queued with the expected size and CRC32, no warning, no panic.

Errors in detail:

- 128 to 8192 bytes: 406 replies "head lost": the ID (first packet) and further bytes are missing, the host gets
  only the end of the reply. Since the data after the ID is zeros, the host test cannot tell which packets were
  lost; the capture below shows that they are the first URBs of the reply.
- 8192 bytes: 4 replies kept the ID but missed 768 to 2944 bytes further on.
- 16 bytes: 10 replies arrived as 20 bytes: the first 32-bit word repeated, then the expected 16 bytes.

USBPcap capture of a run (`--sizes 2048 8192 --count 30`), analysed with `tools/usb_in_pcap.py`:

| Size (B) | Replies captured | Host controller got the whole reply | Replies with all URBs OK | Failed URBs | App-visible OK |
|---:|---:|---:|---:|---:|---:|
| 2048 | 28 | 28 | 5 | 60 | 5 |
| 8192 | 30 | 30 | 0 | 280 | 0 |

The capture missed the first 2 of the 60 requests (it started late), hence 28 replies at 2048 bytes.
Bulk IN URB status: 339 × `USBD_STATUS_BABBLE_DETECTED (0xC0000012)`, 60 × success. In 52 of the 53 replies with a
failed URB, the first URB of the reply failed and the successful URBs carry exactly the expected tail. The host test
saw 5/30 and 0/30 complete replies in that run, which matches "App-visible OK". The device output on the wire is
correct; the babble condition makes the host drop it.

Crash on port open (decoded from the UART panic dump):

```
Guru Meditation Error: Core 1 panic'ed (Load access fault). MCAUSE 0x5, MTVAL near 0
  MEPC dcd_event_setup_received  dcd.h:221
  RA   handle_epout_dma          dcd_dwc2.c:1065
       handle_epin_dma           dcd_dwc2.c:1133
       handle_ep_irq             dcd_dwc2.c:1145
       dcd_int_handler           dcd_dwc2.c:1315
       dwc2_int_handler_wrap     dwc2_esp32.h:110
```

## Repository layout

```
main/tusb_serial_device_main.c   IN test firmware (example + test request handler)
sdkconfig.defaults, sdkconfig    example configuration with DMA mode and CDC TX FIFO 8192
tools/usb_in_test.py             host IN test
tools/usb_in_pcap.py             USBPcap capture analysis
CLAUDE.md                        instructions for running the build and test with an AI coding agent
```
