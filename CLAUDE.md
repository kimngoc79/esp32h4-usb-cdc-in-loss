# CLAUDE.md

Instructions for an AI coding agent (Claude Code) that builds, flashes and runs the ESP32-H4 USB CDC IN test of this
repository. Read `README.md` first: it explains the problem, the protocol and the expected results.

## Rules

- Do not change `main/`, `sdkconfig*` or `tools/` unless the user asks: this repository is a reproduction case and
  its results must stay comparable.
- Never commit anything under `test_reports/`, `build/` or `build_dma/`.
- Flashing needs a person at the board (see "Flash"). Ask the user, with clear options, every time the board must
  enter download mode or be reset, then continue.
- Report results from the files the scripts write; do not guess numbers.

## Environment

- ESP-IDF master with esp32h4 support. Activate it in the shell first (`export.ps1` / `export.sh` of the ESP-IDF
  install). esp32h4 is a preview target: every `idf.py` command needs `--preview`.
- Python packages for the host scripts: `pyserial`. The capture analysis also needs Wireshark (`tshark`) and USBPcap
  (Windows; USBPcap needs a reboot after installation).
- Ports: the board UART console (USB-UART bridge, used for flashing and the device log) and the H4 USB CDC port
  (VID:PID `303A:4001`, found automatically by the test). List them with
  `python -c "import serial.tools.list_ports as l; [print(p.device, p.hwid) for p in l.comports()]"`.

## Build

```
idf.py --preview build                                                       # slave mode (default) -> build/
idf.py --preview -B build_dma -DSDKCONFIG=build_dma/sdkconfig "-DSDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.dma.defaults" build   # DMA -> build_dma/
```

The build must end with "Project build complete" and no warning. Do not run `set-target` when `sdkconfig` already
has `CONFIG_IDF_TARGET="esp32h4"`.

## Flash

1. Ask the user to put the board in download mode: hold BOOT, press and release RESET, release BOOT.
2. From the build directory (`build/` or `build_dma/`):
   `python -m esptool --chip esp32h4 -p <UART port> -b 460800 --before no-reset --after no-reset write-flash @flash_args`
   It must print "Hash of data verified". "No serial data received" means the board is not in download mode: ask
   again.
3. Ask the user to press RESET (without BOOT). Wait until the `303A:4001` port appears, then about 5 s more before
   opening it.

## Run the test

```
python tools/usb_in_test.py --log-port <UART port>
```

- Default: sizes 16..8192 bytes, 100 replies each. Exit code 0 only when every reply is complete and byte-exact.
- Read the summary at the end of the output and the report `test_reports/usb_in_test_<time>.md`.
- "test stopped: IN stalled" (slave mode) or "cannot open ... not functioning" (DMA mode, crash on port open) are
  known results, not script errors. After either one the board needs a RESET before the next run.
- With `--log-port`, the "Device log" line tells whether the device queued every reply (size and CRC32) and whether
  it panicked or rebooted.

## Capture (optional, Windows)

1. Find the board on the USBPcap root hubs:
   `& "C:\Program Files\USBPcap\USBPcapCMD.exe" --extcap-interface \\.\USBPcap<N> --extcap-config`;
   the board is the `{value=<address>_1}` entry "USB Serial Device (COMx)", use `<address>`.
2. Start the capture in the background before the test, stop it after:
   `USBPcapCMD.exe -d \\.\USBPcap<N> --devices <address> -b 16777216 -o capture.pcap`
3. `python tools/usb_in_pcap.py capture.pcap` and report its table and URB status counts.

Capture only the board's device address: capturing a whole root hub records other devices (keyboard, mouse).

## Report back

Give the user: firmware variant (slave / DMA), the per-size table, whether the test stopped early and why, the device
log line, and the capture summary when one was made. Keep the report files local unless the user decides to share
them.
