#!/usr/bin/env python3
"""allsky.py - capture from the all-sky camera using its own indiserver.

Usage:
    ./allsky.py capture [seconds] [--gain N] [--format fits,jpg,png] [--stretch]
    ./allsky.py convert FILE.fits [--format jpg,png] [--stretch]
    ./allsky.py start | stop | status
"""
import argparse
import base64
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime
from pathlib import Path

# ---- Settings ---------------------------------------------------------------
HOST = "localhost"
PORT = 7625                              # main telescope server stays on 7624
DEVICE = "ZWO CCD ASI120MM"
DRIVER = "indi_asi_ccd"
IMAGE_DIR = Path("/home/dokeeffe/Pictures/allsky")
RUN_DIR = Path.home() / ".allsky-indi"
PIDFILE = RUN_DIR / "indiserver.pid"
LOGFILE = RUN_DIR / "indiserver.log"
JPEG_QUALITY = 92
# -----------------------------------------------------------------------------


# ---- Server management ------------------------------------------------------
def port_open(port: int = PORT) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=1):
            return True
    except OSError:
        return False


def start_server(port: int = PORT, driver: str = DRIVER) -> None:
    if port == 7624:
        raise ValueError("Port 7624 is reserved for the observatory's main INDI server")
    if port_open(port):
        return
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOGFILE, "ab")
    proc = subprocess.Popen(
        ["indiserver", "-v", "-p", str(port), driver],
        stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        start_new_session=True,          # keeps running after this script exits
    )
    PIDFILE.write_text(str(proc.pid))
    for _ in range(40):
        if port_open(port):
            print(f"indiserver started on port {port} (PID {proc.pid})")
            return
        time.sleep(0.25)
    sys.exit(f"indiserver did not start - see {LOGFILE}")


def stop_server() -> None:
    import os
    import signal
    if not PIDFILE.exists():
        print("No PID file - nothing started by this script.")
        return
    try:
        os.kill(int(PIDFILE.read_text()), signal.SIGTERM)
        print("Stopped all-sky indiserver.")
    except ProcessLookupError:
        print("Process was already gone.")
    PIDFILE.unlink(missing_ok=True)


# ---- Minimal INDI client ----------------------------------------------------
class Indi:
    """Tiny INDI XML client. Keeps one connection open for the whole capture."""

    def __init__(self, device: str = DEVICE, port: int = PORT) -> None:
        self.device = device
        self.sock = socket.create_connection((HOST, port), timeout=5)
        self.sock.settimeout(0.5)
        self.parser = ET.XMLPullParser(events=("start", "end"))
        self.parser.feed("<indi>")       # INDI is a stream with no single root
        self.depth = 0
        self.props: dict[str, dict[str, str]] = {}
        self.states: dict[str, str] = {}
        self.blobs: list[tuple[str, str]] = []
        self.send("<getProperties version='1.7'/>")
        self.enable_blobs()

    def close(self) -> None:
        self.sock.close()

    def send(self, xml: str) -> None:
        self.sock.sendall(xml.encode())

    def enable_blobs(self) -> None:
        self.send(f"<enableBLOB device='{self.device}'>Also</enableBLOB>")

    def set_switch(self, name: str, element: str) -> None:
        self.send(f"<newSwitchVector device='{self.device}' name='{name}'>"
                  f"<oneSwitch name='{element}'>On</oneSwitch></newSwitchVector>")

    def set_numbers(self, name: str, **values: float) -> None:
        items = "".join(f"<oneNumber name='{k}'>{v}</oneNumber>" for k, v in values.items())
        self.send(f"<newNumberVector device='{self.device}' name='{name}'>{items}</newNumberVector>")

    def pump(self) -> None:
        try:
            data = self.sock.recv(1 << 20)
        except socket.timeout:
            return
        if not data:
            raise ConnectionError("indiserver closed the connection")
        self.parser.feed(data)
        for event, el in self.parser.read_events():
            if event == "start":
                self.depth += 1
            else:
                self.depth -= 1
                if self.depth == 1:      # a complete top-level INDI message
                    self.handle(el)
                    el.clear()

    def handle(self, el: ET.Element) -> None:
        tag = el.tag
        if tag == "message":
            if el.get("device") in (None, self.device):
                print(f"  [driver] {el.get('message')}", file=sys.stderr)
            return
        if el.get("device") != self.device:
            return
        if tag == "setBLOBVector":
            for b in el.findall("oneBLOB"):
                self.blobs.append((b.get("format", ".fits"), b.text or ""))
        elif tag.endswith(("NumberVector", "SwitchVector", "TextVector")) \
                and tag.startswith(("def", "set")):
            name = el.get("name")
            self.states[name] = el.get("state", "")
            values = self.props.setdefault(name, {})
            for child in el:
                values[child.get("name")] = (child.text or "").strip()

    def wait(self, cond, timeout: float, what: str) -> None:
        end = time.time() + timeout
        while True:
            if cond():
                return
            if time.time() > end:
                raise TimeoutError(f"Timed out waiting for {what}")
            self.pump()


# ---- Image conversion ---------------------------------------------------------
def fits_to_uint8(data: bytes, stretch: bool):
    """Parse a simple 2-D FITS file and return an 8-bit numpy array (top row first)."""
    import numpy as np

    header: dict[str, str] = {}
    pos = 0
    done = False
    while not done:
        block = data[pos:pos + 2880]
        if len(block) < 2880:
            raise ValueError("Truncated FITS header")
        pos += 2880
        for i in range(0, 2880, 80):
            card = block[i:i + 80].decode("ascii", "replace")
            key = card[:8].strip()
            if key == "END":
                done = True
                break
            if card[8:10] == "= ":
                header[key] = card[10:].split("/")[0].strip()

    bitpix = int(header["BITPIX"])
    w, h = int(header["NAXIS1"]), int(header["NAXIS2"])
    dtypes = {8: "u1", 16: ">i2", 32: ">i4", -32: ">f4"}
    arr = np.frombuffer(data, dtype=dtypes[bitpix], count=w * h, offset=pos).reshape(h, w)
    arr = arr.astype(np.float32) + float(header.get("BZERO", 0))
    arr = np.flipud(arr)                 # FITS stores the bottom row first

    if stretch:
        lo, hi = np.percentile(arr, (0.5, 99.5))
        arr = (arr - lo) / max(hi - lo, 1e-6) * 255
    elif bitpix == 8:
        pass                             # already 0-255
    elif bitpix == 16:
        arr = arr / 256.0                # 16-bit -> 8-bit
    else:
        arr = arr / max(float(arr.max()), 1.0) * 255
    return np.clip(arr, 0, 255).astype(np.uint8)


def save_outputs(fits_bytes: bytes, base: Path, formats: list[str], stretch: bool) -> None:
    """Write the requested formats next to each other: base.fits, base.jpg, base.png"""
    base.parent.mkdir(parents=True, exist_ok=True)
    if "fits" in formats:
        dest = base.with_suffix(".fits")
        dest.write_bytes(fits_bytes)
        print(f"Saved {dest}")
    images = [f for f in formats if f in ("jpg", "png")]
    if images:
        try:
            from PIL import Image
        except ImportError:
            sys.exit("JPEG/PNG output needs numpy and Pillow:\n"
                     "  sudo apt install python3-numpy python3-pil")
        img = Image.fromarray(fits_to_uint8(fits_bytes, stretch))
        for fmt in images:
            dest = base.with_suffix("." + fmt)
            if fmt == "jpg":
                img.save(dest, quality=JPEG_QUALITY)
            else:
                img.save(dest)
            print(f"Saved {dest}")


# ---- Capture ------------------------------------------------------------------
def capture(
    seconds: float,
    gain: float | None,
    formats: list[str],
    stretch: bool,
    *,
    image_dir: Path = IMAGE_DIR,
    driver: str = DRIVER,
    device: str = DEVICE,
    port: int = PORT,
    auto_gain: bool = True,
) -> Path:
    if not auto_gain and gain is None:
        raise ValueError("A manual gain value is required when auto gain is disabled")
    start_server(port, driver)
    c = Indi(device, port)
    try:
        print("Waiting for camera...")
        c.wait(lambda: "CONNECTION" in c.props, 30, f"'{device}' to appear (check name/USB)")
        c.enable_blobs()

        if c.props["CONNECTION"].get("CONNECT") != "On":
            c.set_switch("CONNECTION", "CONNECT")
        c.wait(lambda: c.props.get("CONNECTION", {}).get("CONNECT") == "On"
               and "CCD_EXPOSURE" in c.props and "CCD_INFO" in c.props
               and "UPLOAD_MODE" in c.props, 30, "camera to connect")

        w = int(float(c.props["CCD_INFO"]["CCD_MAX_X"]))
        h = int(float(c.props["CCD_INFO"]["CCD_MAX_Y"]))

        c.set_switch("UPLOAD_MODE", "UPLOAD_CLIENT")
        if "CCD_TRANSFER_FORMAT" in c.props:
            c.set_switch("CCD_TRANSFER_FORMAT", "FORMAT_FITS")
        c.set_numbers("CCD_BINNING", HOR_BIN=1, VER_BIN=1)
        c.set_numbers("CCD_FRAME", X=0, Y=0, WIDTH=w, HEIGHT=h)
        if gain is not None:
            c.set_numbers("CCD_CONTROLS", Gain=gain)

        t_end = time.time() + 1.0
        while time.time() < t_end:
            c.pump()

        c.blobs.clear()
        stamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        print(f"Exposing {seconds}s at {w}x{h}...")
        c.set_numbers("CCD_EXPOSURE", CCD_EXPOSURE_VALUE=seconds)

        def done() -> bool:
            if c.states.get("CCD_EXPOSURE") == "Alert":
                raise RuntimeError("Driver reported an error for the exposure")
            return bool(c.blobs)

        c.wait(done, seconds + 30, "the image")
        fmt, text = c.blobs[0]
        data = base64.b64decode(text)
        if fmt.endswith(".z"):           # driver compressed it
            data = zlib.decompress(data)

        base = image_dir / f"allsky-{stamp}"
        save_outputs(data, base, formats, stretch)
        return base
    finally:
        c.close()


def convert(path: Path, formats: list[str], stretch: bool) -> None:
    formats = [f for f in formats if f != "fits"] or ["jpg"]
    save_outputs(path.read_bytes(), path.with_suffix(""), formats, stretch)


def parse_formats(text: str) -> list[str]:
    formats = [f.strip().lower().replace("jpeg", "jpg") for f in text.split(",") if f.strip()]
    bad = [f for f in formats if f not in ("fits", "jpg", "png")]
    if bad or not formats:
        raise argparse.ArgumentTypeError("formats must be a comma list of: fits, jpg, png")
    return formats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    cap = sub.add_parser("capture", help="take one frame")
    cap.add_argument("seconds", nargs="?", type=float, default=0.05)
    cap.add_argument("--gain", type=float, default=None)
    cap.add_argument("--format", type=parse_formats, default=["fits"],
                     help="comma list of fits,jpg,png (default: fits)")
    cap.add_argument("--stretch", action="store_true",
                     help="auto-stretch contrast in JPEG/PNG (useful at night)")

    conv = sub.add_parser("convert", help="convert an existing FITS file")
    conv.add_argument("file", type=Path)
    conv.add_argument("--format", type=parse_formats, default=["jpg"])
    conv.add_argument("--stretch", action="store_true")

    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("status")
    args = ap.parse_args()

    if args.cmd == "capture":
        capture(args.seconds, args.gain, args.format, args.stretch)
    elif args.cmd == "convert":
        convert(args.file, args.format, args.stretch)
    elif args.cmd == "start":
        start_server()
    elif args.cmd == "stop":
        stop_server()
    elif args.cmd == "status":
        print(f"indiserver on port {PORT}: {'running' if port_open() else 'not running'}")


if __name__ == "__main__":
    main()
