"""USB CDC IN (device to host) integrity test for the ESP32-H4 tusb_serial_device firmware of this repository.

For every reply size the host sends an 8-byte request {magic "TSIN", size} (2 x u32 little endian); the device
answers `size` bytes of fixed test data: the 16-byte ID 04C8F4CF7D5EB04FB6FFC5D67D966357, then zeros (shorter replies
are cut). The host checks that every reply arrives complete and byte-exact.

Sizes (default): 16, 32, 64, ... 2048, 4096, 8192 bytes, --count (default 100) replies each.

Timeouts: a reply ends when no byte arrives for --idle-timeout s after its first byte (it is checked, the test goes
on). When no byte at all arrives for --stall-timeout s (default 30) after a request, a reply is still incomplete
after that long, or the device takes no request bytes, the test stops and the report is written anyway.

--log-port (the board UART console, e.g. COM40) also reads the device log: one `reply #n size S crc32 C` line per
reply the device queued, which tells a request lost on the way to the device from a reply lost on the way back.

Output in --report-dir (default test_reports/): usb_in_test_<time>.md (report), _pc.log (one line per reply),
_fail.log (hex of every failed reply), _uart.log (raw device log, with --log-port).

    python tools/usb_in_test.py                              # all sizes, 100 replies each, port found by VID:PID
    python tools/usb_in_test.py --port COM44 --log-port COM40
    python tools/usb_in_test.py --sizes 2048 --count 1000
"""
import argparse
import datetime
import re
import struct
import sys
import threading
import time
import zlib
from pathlib import Path

import serial
from serial.tools.list_ports import comports

ROOT = Path(__file__).resolve().parents[1]
USB_VID_PID = "303A:4001"  # esp_tinyusb default descriptor with 1 CDC port
REQUEST_MAGIC = 0x4E495354  # main/tusb_serial_device_main.c IN_TEST_MAGIC ("TSIN")
REPLY_HEAD = bytes.fromhex("04C8F4CF7D5EB04FB6FFC5D67D966357")  # main/tusb_serial_device_main.c in_test_head
MAX_SIZE = 8192  # CONFIG_TINYUSB_CDC_TX_BUFSIZE
PACKET_SIZE = 64  # full speed bulk packet
DEFAULT_SIZES = [16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192]
REPLY_LOG = re.compile(r"reply #(\d+) size (\d+) crc32 ([0-9A-F]{8})")
WARNING_LOG = re.compile(r"^[EW] \(", re.M)
PANIC_LOG = re.compile(r"Guru Meditation Error: (.*)")


# --------------------------------------------------------------------------- data
def expected_reply(size):
    """Fixed test data of a size-byte reply: the 16-byte ID, then zeros."""
    return (REPLY_HEAD + bytes(size))[:size]


def classify(received, expected):
    """One-line diagnosis of a reply that is not byte-exact."""
    got, want = len(received), len(expected)
    if got == 0:
        return "no reply"
    if got < want and expected.endswith(received):
        return "head lost: %d bytes (%d packets of %d)" % (want - got, -(-(want - got) // PACKET_SIZE),
                                                          -(-want // PACKET_SIZE))
    if got < want and expected.startswith(received):
        return "tail lost: %d bytes" % (want - got)
    if got > want and received[:4] == received[4:8] and received[4:] == expected:
        return "%d extra bytes: first 32-bit word repeated" % (got - want)
    if got > want:
        return "%d extra bytes" % (got - want)
    first = next((i for i, (a, b) in enumerate(zip(received, expected)) if a != b), min(got, want))
    return "corrupt: %d/%d bytes, first difference at offset %d (packet %d)" % (got, want, first,
                                                                                first // PACKET_SIZE)


# --------------------------------------------------------------------------- serial ports
def find_data_port():
    """The CDC port of the board, by VID:PID."""
    ports = [p for p in comports() if USB_VID_PID in (p.hwid or "").upper()]
    return ports[0].device if ports else None


class LogReader(threading.Thread):
    """Collect the device UART log while the test runs (DTR / RTS kept low: no reset)."""

    def __init__(self, port):
        super().__init__(daemon=True)
        self.port = serial.Serial()
        self.port.port, self.port.baudrate, self.port.timeout = port, 115200, 0.05
        self.port.dtr = False
        self.port.rts = False
        self.port.open()
        self.data = bytearray()
        self.stopping = threading.Event()

    def run(self):
        while not self.stopping.is_set():
            self.data += self.port.read(max(1, self.port.in_waiting))

    def stop(self, settle=0.5):
        time.sleep(settle)
        self.stopping.set()
        self.join()
        self.port.close()


def read_reply(port, size, idle_timeout, stall_timeout):
    """Read up to size bytes. Returns (data, stalled): the reply ends when no byte arrives for idle_timeout s after
    the first one; stalled when no byte arrives for stall_timeout s, or the reply is incomplete after that long."""
    data = bytearray()
    begin = last = time.perf_counter()
    while len(data) < size:
        chunk = port.read(max(1, min(port.in_waiting, size - len(data))))
        now = time.perf_counter()
        if chunk:
            data += chunk
            last = now
        elif data and now - last > idle_timeout:
            return bytes(data), False
        if now - begin > stall_timeout:
            return bytes(data), True
    return bytes(data), False


def drain(port, settle):
    """Whatever is still pending on the port after settle s."""
    time.sleep(settle)
    data = bytearray()
    while port.in_waiting:
        data += port.read(port.in_waiting)
        time.sleep(0.01)
    return bytes(data)


# --------------------------------------------------------------------------- run
def run(args, out, fail_log):
    data_port = args.port or find_data_port()
    if not data_port:
        raise SystemExit("no %s port found, use --port" % USB_VID_PID)
    log = LogReader(args.log_port) if args.log_port else None
    if log:
        log.start()
    out("Data port %s, log port %s, sizes %s, %d replies per size, stall timeout %.0f s"
        % (data_port, args.log_port or "-", args.sizes, args.count, args.stall_timeout))
    results = []
    expected_log = []  # (size, crc32) of every reply the device should log, in order
    aborted = None
    start = time.time()
    try:
        port = serial.Serial(data_port, 115200, timeout=0.02, write_timeout=args.stall_timeout)
        port.dtr = True
    except serial.SerialException as error:
        port = None
        aborted = "cannot open %s (%s); see the device log for a panic" % (data_port, error)
        out("  test stopped: " + aborted)
    stale = drain(port, 0.2) if port else b""
    if stale:
        out("  note: %d stale bytes on the data port before the test, discarded" % len(stale))

    for size in args.sizes if port else []:
        expected = expected_reply(size)
        group = {"size": size, "replies": [], "late": 0}
        results.append(group)
        for iteration in range(args.count):
            late = drain(port, 0) if port.in_waiting else b""
            group["late"] += len(late)
            begin = time.perf_counter()
            try:
                port.write(struct.pack("<II", REQUEST_MAGIC, size))
            except serial.SerialTimeoutException:
                aborted = "device took no request bytes for %.0f s at size %d request #%d" % (
                    args.stall_timeout, size, iteration + 1)
                break
            expected_log.append((size, zlib.crc32(expected) & 0xFFFFFFFF))
            received, stalled = read_reply(port, size, args.idle_timeout, args.stall_timeout)
            round_trip = time.perf_counter() - begin
            if not stalled and args.extra_wait > 0:
                received += drain(port, args.extra_wait)
            ok = received == expected
            diagnosis = "" if ok else classify(received, expected)
            if stalled:
                diagnosis += "; IN stalled: reply incomplete after %.0f s" % args.stall_timeout
            group["replies"].append({"iteration": iteration, "received": len(received), "round_trip": round_trip,
                                     "pass": ok, "diagnosis": diagnosis})
            line = ("  size %4d  #%03d  rx %4d/%4d  %8.1f ms  %s %s"
                    % (size, iteration + 1, len(received), size, round_trip * 1000, "PASS" if ok else "FAIL",
                       diagnosis))
            out(line + ("  (%d late bytes before this request)" % len(late) if late else ""))
            if not ok:
                fail_log += [line.strip(), "    rx " + (received.hex(" ") or "-"), ""]
            if stalled:
                aborted = "IN stalled at size %d reply #%d" % (size, iteration + 1)
                break
        if aborted:
            out("  test stopped: " + aborted)
            break
    if port:
        port.close()
    elapsed = time.time() - start

    log_result = None
    if log:
        log.stop()
        raw = bytes(log.data)
        text = raw.decode("ascii", errors="replace")
        replies = [(int(m.group(1)), int(m.group(2)), int(m.group(3), 16)) for m in REPLY_LOG.finditer(text)]
        numbers = [n for n, _, _ in replies]
        log_result = {
            "port": args.log_port, "raw": raw, "lines": len(replies), "requests": len(expected_log),
            "first": numbers[0] if numbers else None, "last": numbers[-1] if numbers else None,
            "gaps": sum(b - a - 1 for a, b in zip(numbers, numbers[1:]) if b > a + 1),
            "mismatch": sum(1 for (_, sz, crc), want in zip(replies, expected_log) if (sz, crc) != want),
            "warnings": [line for line in text.splitlines() if WARNING_LOG.match(line)],
            "panics": [m.group(1).strip() for m in PANIC_LOG.finditer(text)],
            "reboots": text.count("rst:"),
        }
        log_result["ok"] = (log_result["lines"] == log_result["requests"] and not log_result["gaps"]
                            and not log_result["mismatch"] and not log_result["warnings"]
                            and not log_result["panics"] and not log_result["reboots"])
    return data_port, results, elapsed, aborted, log_result


# --------------------------------------------------------------------------- report
def write_report(path, data_port, results, elapsed, aborted, log_result, args):
    total = sum(len(g["replies"]) for g in results)
    passed = sum(r["pass"] for g in results for r in g["replies"])
    planned = len(args.sizes) * args.count
    verdict = "PASS" if passed == planned and not aborted else "FAIL"
    lines = [
        "# USB CDC IN test report", "",
        "- Date: %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "- Data port: %s (%s)" % (data_port, USB_VID_PID),
        "- Reply data: 16-byte ID %s, then zeros" % REPLY_HEAD.hex().upper(),
        "- Sizes: %s bytes, %d replies each" % (args.sizes, args.count),
        "- Timeouts: idle %.1f s after the first byte, stall %.0f s" % (args.idle_timeout, args.stall_timeout),
        "- Duration: %.1f s" % elapsed,
        "- **Verdict: %s** (%d/%d replies complete and byte-exact, %d planned)" % (verdict, passed, total, planned),
    ]
    if aborted:
        lines.append("- **Stopped early:** %s" % aborted)
    lines += ["", "## Replies per size", "",
              "| Size (B) | Replies OK | Head lost | Other errors | Bytes RX / expected | Late bytes | "
              "Round trip min/avg/max (ms) |",
              "|---:|---:|---:|---:|---:|---:|---:|"]
    for g in results:
        replies = g["replies"]
        if not replies:
            continue
        times = [r["round_trip"] * 1000 for r in replies]
        head = sum(r["diagnosis"].startswith("head lost") for r in replies)
        lines.append("| %d | %d/%d | %d | %d | %d / %d | %d | %.1f / %.1f / %.1f |"
                     % (g["size"], sum(r["pass"] for r in replies), len(replies), head,
                        sum(not r["pass"] for r in replies) - head, sum(r["received"] for r in replies),
                        g["size"] * len(replies), g["late"], min(times), sum(times) / len(times), max(times)))
    lines += ["", "A reply is OK when exactly the expected bytes arrive, nothing missing and nothing added. "
              "Late bytes: bytes pending before a request was sent (the tail of an earlier reply).", "",
              "## Failed replies", ""]
    failed = [(g["size"], r) for g in results for r in g["replies"] if not r["pass"]]
    if not failed:
        lines.append("None.")
    else:
        lines += ["| Size (B) | Reply | RX (B) | Diagnosis |", "|---:|---:|---:|---|"]
        lines += ["| %d | %d | %d | %s |" % (size, r["iteration"] + 1, r["received"], r["diagnosis"])
                  for size, r in failed]
        lines += ["", "Hex of every failed reply: `%s_fail.log`." % path.stem]
    if log_result:
        r = log_result
        lines += ["", "## Device log (%s)" % r["port"], "",
                  "One `reply #n size S crc32 C` line per reply the device queued.", "",
                  "| Check | Value |", "|---|---|",
                  "| Reply lines / requests sent | %d / %d |" % (r["lines"], r["requests"]),
                  "| First # / last # | %s / %s |" % (r["first"], r["last"]),
                  "| Missing numbers | %d |" % r["gaps"],
                  "| Size or CRC32 differs from the expected reply | %d |" % r["mismatch"],
                  "| E / W lines | %d |" % len(r["warnings"]),
                  "| Panics | %s |" % ("; ".join(r["panics"]) or "0"),
                  "| Reboots (rst: lines) | %d |" % r["reboots"],
                  "| Result | %s |" % ("PASS" if r["ok"] else "FAIL")]
        if r["warnings"]:
            lines += ["", "```"] + r["warnings"][:20] + ["```"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return verdict


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", help="CDC data port (default: the %s port)" % USB_VID_PID)
    parser.add_argument("--log-port", help="device UART console port, e.g. COM40 (default: not read)")
    parser.add_argument("--sizes", type=int, nargs="+", default=DEFAULT_SIZES,
                        help="reply sizes in bytes, 1..%d" % MAX_SIZE)
    parser.add_argument("--count", type=int, default=100, help="replies per size")
    parser.add_argument("--idle-timeout", type=float, default=1.0, help="reply ends after this silence (s)")
    parser.add_argument("--stall-timeout", type=float, default=30.0, help="stop the test after this wait (s)")
    parser.add_argument("--extra-wait", type=float, default=0.02, help="wait for unexpected bytes after a reply (s)")
    parser.add_argument("--report-dir", type=Path, default=ROOT / "test_reports")
    args = parser.parse_args()
    if any(not 0 < size <= MAX_SIZE for size in args.sizes):
        parser.error("sizes must be 1..%d" % MAX_SIZE)

    args.report_dir.mkdir(parents=True, exist_ok=True)
    base = args.report_dir / ("usb_in_test_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    console, fail_log = [], []

    def out(text):
        print(text, flush=True)
        console.append(text)

    data_port, results, elapsed, aborted, log_result = run(args, out, fail_log)
    report = base.with_suffix(".md")
    verdict = write_report(report, data_port, results, elapsed, aborted, log_result, args)
    out("\n%-6s %-11s %-10s" % ("Size", "Replies OK", "Head lost"))
    for g in results:
        if g["replies"]:
            out("%-6d %-11s %d" % (g["size"], "%d/%d" % (sum(r["pass"] for r in g["replies"]), len(g["replies"])),
                                   sum(r["diagnosis"].startswith("head lost") for r in g["replies"])))
    if log_result:
        out("\nDevice log %s: %d/%d reply lines (#%s..#%s), %d missing, %d size/crc mismatch, %d E/W lines, "
            "%d panics, %d reboots -> %s"
            % (log_result["port"], log_result["lines"], log_result["requests"], log_result["first"],
               log_result["last"], log_result["gaps"], log_result["mismatch"], len(log_result["warnings"]),
               len(log_result["panics"]), log_result["reboots"], "PASS" if log_result["ok"] else "FAIL"))
        for panic in log_result["panics"]:
            out("  device panic: " + panic)
        (base.parent / (base.name + "_uart.log")).write_bytes(log_result["raw"])
    if fail_log:
        (base.parent / (base.name + "_fail.log")).write_text("\n".join(fail_log) + "\n", encoding="utf-8")
    out("\nVerdict: %s%s\nReport: %s" % (verdict, " (stopped early: %s)" % aborted if aborted else "", report))
    (base.parent / (base.name + "_pc.log")).write_text("\n".join(console) + "\n", encoding="utf-8")
    sys.exit(0 if verdict == "PASS" else 1)


if __name__ == "__main__":
    main()
