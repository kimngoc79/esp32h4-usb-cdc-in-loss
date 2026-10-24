"""Summarise a USBPcap capture of tools/usb_in_test.py: what the host controller received for every reply.

Needs Wireshark's tshark (reading a .pcap file does not need Npcap). For every request (bulk OUT {magic, size}) the
bulk IN URB completions that follow are collected, with their USBD status. It answers: did the device send every
reply byte-exact, and which URBs did the host controller fail (e.g. USBD_STATUS_BABBLE_DETECTED)? usbser.sys drops
the data of a failed URB, so the application then sees only the bytes of the successful URBs.

    python tools/usb_in_pcap.py capture.pcap
    python tools/usb_in_pcap.py capture.pcap --tshark "C:/Program Files/Wireshark/tshark.exe"
"""
import argparse
import struct
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from usb_in_test import REQUEST_MAGIC, expected_reply  # noqa: E402

FIELDS = ["usb.endpoint_address", "usb.transfer_type", "usb.irp_info.direction", "usb.usbd_status", "usb.capdata"]
USBD_OK = "0x00000000"
USBD_NAMES = {"0xc0000012": "BABBLE_DETECTED", "0xc0000011": "XACT_ERROR", "0xc0000004": "STALL_PID",
              "0xc0000030": "ENDPOINT_HALTED", "0xc0010000": "CANCELED", USBD_OK: "SUCCESS"}


def read_urbs(pcap, tshark):
    """Bulk URB records of the capture as dicts of FIELDS."""
    out = subprocess.run([tshark, "-r", str(pcap), "-T", "fields", "-E", "separator=|", "-E", "occurrence=f"]
                         + sum((["-e", f] for f in FIELDS), []), capture_output=True, text=True, check=True).stdout
    return [dict(zip(FIELDS, line.split("|"))) for line in out.splitlines() if line]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("pcap", type=Path)
    parser.add_argument("--tshark", default=r"C:\Program Files\Wireshark\tshark.exe")
    args = parser.parse_args()

    urbs = read_urbs(args.pcap, args.tshark)
    replies, current = [], None
    status_all = Counter()
    for urb in urbs:
        if urb["usb.transfer_type"] != "0x03":  # bulk only
            continue
        ep = int(urb["usb.endpoint_address"] or "0", 16)
        data = bytes.fromhex(urb["usb.capdata"].replace(":", "")) if urb["usb.capdata"] else b""
        if not ep & 0x80 and len(data) == 8 and struct.unpack("<I", data[:4])[0] == REQUEST_MAGIC:
            current = {"size": struct.unpack("<I", data[4:])[0], "urbs": []}
            replies.append(current)
        elif ep & 0x80 and urb["usb.irp_info.direction"] == "0x01" and current:  # IN completion
            current["urbs"].append((data, urb["usb.usbd_status"]))
            status_all[urb["usb.usbd_status"]] += 1

    print("capture %s: %d requests" % (args.pcap.name, len(replies)))
    print("bulk IN URB completions by USBD status: %s" % ", ".join(
        "%s %s=%d" % (s, USBD_NAMES.get(s, "?"), n) for s, n in status_all.most_common()))
    print("\n%-6s %-9s %-14s %-12s %-12s %-14s" % ("Size", "Replies", "Host got all", "All URBs OK", "Failed URBs",
                                                "App-visible OK"))
    for size in sorted({r["size"] for r in replies}):
        group = [r for r in replies if r["size"] == size]
        expected = expected_reply(size)
        got_all = sum(b"".join(d for d, _ in r["urbs"]) == expected for r in group)
        all_ok = sum(all(s == USBD_OK for _, s in r["urbs"]) for r in group)
        failed = sum(sum(s != USBD_OK for _, s in r["urbs"]) for r in group)
        visible = sum(b"".join(d for d, s in r["urbs"] if s == USBD_OK) == expected for r in group)
        print("%-6d %-9d %-14d %-12d %-12d %-14d" % (size, len(group), got_all, all_ok, failed, visible))
    tails = [r for r in replies if any(s != USBD_OK for _, s in r["urbs"])]
    tail_ok = sum(expected_reply(r["size"]).endswith(b"".join(d for d, s in r["urbs"] if s == USBD_OK))
                  for r in tails)
    first_fail = sum(r["urbs"][0][1] != USBD_OK for r in tails if r["urbs"])
    print("\nreplies with a failed URB: %d; their successful URBs carry the expected tail: %d; first URB failed: %d"
          % (len(tails), tail_ok, first_fail))
    print("Host got all = the IN data of all URBs (failed ones included) is the expected reply, byte-exact.")
    print("App-visible OK = the data of the successful URBs alone is the whole reply (what usbser.sys delivers).")


if __name__ == "__main__":
    main()
