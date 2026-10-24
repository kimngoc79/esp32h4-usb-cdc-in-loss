# CLAUDE.md

Instructions for an AI coding agent (Claude Code) that builds, flashes and runs the ESP32-H4 USB CDC IN test of this
repository. Read `README.md` first: it explains the problem, the protocol and the expected results.

## Rules

- Do not change `main/`, `sdkconfig*` or `tools/` unless the user asks: this repository is a reproduction case and
  its results must stay comparable.
- Never commit anything under `test_reports/` or `build/`.
- Flashing needs a person at the board (see "Flash"). Ask the user, with clear options, every time the board must
  enter download mode or be reset, then continue.
- Report results from the files the scripts write; do not guess numbers.

## Environment

The full list (hardware, ESP-IDF install, drivers, capture tools) is in `README.md`, section "Requirements". Before
the first build, check each item below; when one is missing, tell the user what to install or connect (installing
ESP-IDF, drivers or USBPcap needs the user's consent: USBPcap needs administrator rights and a Windows reboot).

| Check | How | Expected |
|---|---|---|
| ESP-IDF exported | `idf.py --version` | prints a version (master with esp32h4 support, measured with `383dda6c`) |
| esp32h4 tools installed | `idf.py --preview build` | configures without a toolchain error (else run `install.ps1 esp32h4` / `install.sh esp32h4` in ESP-IDF) |
| Internet on first build | first build | `managed_components/` gets esp_tinyusb 2.4.0 and tinyusb 0.21.0~2 |
| pyserial | `python -c "import serial; print(serial.VERSION)"` | prints a version (else `pip install pyserial`) |
| UART console port | port list below | the board's USB-UART bridge (e.g. CP210x `10C4:EA60`) |
| CDC port under test | port list below, firmware running | `303A:4001` ("USB Serial Device" on Windows, `/dev/ttyACM*` on Linux) |
| Capture tools (optional) | `C:\Program Files\USBPcap\USBPcapCMD.exe`, `C:\Program Files\Wireshark\tshark.exe` exist, `sc query USBPcap` is RUNNING | needed only for the capture step |

List the serial ports with
`python -c "import serial.tools.list_ports as l; [print(p.device, p.hwid) for p in l.comports()]"`.
esp32h4 is a preview target: every `idf.py` command needs `--preview`.

## Build

```
idf.py --preview build          # -> build/ (DMA mode, 1 CDC, TX FIFO 8192)
```

The build must end with "Project build complete" and no warning. Do not run `set-target`: the tracked `sdkconfig`
is already for esp32h4 and `set-target` would reset it. If `sdkconfig` is missing, build with
`idf.py --preview -DIDF_TARGET=esp32h4 build` to recreate it from `sdkconfig.defaults`.

## Flash

1. Ask the user to put the board in download mode: hold BOOT, press and release RESET, release BOOT.
2. From `build/`:
   `python -m esptool --chip esp32h4 -p <UART port> -b 460800 --before no-reset --after no-reset write-flash @flash_args`
   It must print "Hash of data verified". "No serial data received" means the board is not in download mode: ask
   again.
3. Ask the user to press RESET (without BOOT). Wait until the `303A:4001` port appears, then about 5 s more before
   opening it.

## Run the test

1. Ask the user to press RESET, wait until `303A:4001` is enumerated plus about 5 s.
2. Run the test for the whole size table (command below).
3. Optional: one more run with a capture (section "Capture").

```
python tools/usb_in_test.py --log-port <UART port>
```

- Default: sizes 16..8192 bytes, 100 replies each. Exit code 0 only when every reply is complete and byte-exact.
- Read the summary at the end of the output and the report `test_reports/usb_in_test_<time>.md`.
- "cannot open ... not functioning" (the device crashed when the host opened the port) and "test stopped: IN
  stalled" are known results, not script errors. After either one, ask for a RESET and run again.
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

Give the user: the per-size table, whether the test stopped early and why (and how many port-open crashes needed a
RESET before a complete run), the device
log line, and the capture summary when one was made. Keep the report files local unless the user decides to share
them.
