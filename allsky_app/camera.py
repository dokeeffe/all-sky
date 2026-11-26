from __future__ import annotations

import atexit
import base64
import binascii
import logging
import socket
import subprocess
import time
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime
from pathlib import Path
from subprocess import Popen
from typing import BinaryIO, Callable
from xml.sax.saxutils import quoteattr

import numpy as np
from PIL import Image, ImageDraw

from .settings import Settings
from .stars import count_stars

logger = logging.getLogger(__name__)
_server_process: Popen[bytes] | None = None
_server_log: BinaryIO | None = None
_server_port: int | None = None


class IndiClient:
    """Minimal INDI XML client for downloading images from a single camera."""

    def __init__(self, device: str, port: int) -> None:
        self.device = device
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.settimeout(0.5)
        self.parser = ET.XMLPullParser(events=("start", "end"))
        self.parser.feed("<indi>")
        self.depth = 0
        self.props: dict[str, dict[str, str]] = {}
        self.states: dict[str, str] = {}
        self.blobs: list[tuple[str, str]] = []
        self.send("<getProperties version='1.7'/>")
        self.enable_blobs()

    def close(self) -> None:
        self.sock.close()

    def send(self, message: str) -> None:
        self.sock.sendall(message.encode("utf-8"))

    def enable_blobs(self) -> None:
        self.send(f"<enableBLOB device={quoteattr(self.device)}>Also</enableBLOB>")

    def set_switch(self, name: str, element: str) -> None:
        device = quoteattr(self.device)
        self.send(
            f"<newSwitchVector device={device} name={quoteattr(name)}>"
            f"<oneSwitch name={quoteattr(element)}>On</oneSwitch></newSwitchVector>"
        )

    def set_numbers(self, name: str, **values: float) -> None:
        device = quoteattr(self.device)
        elements = "".join(
            f"<oneNumber name={quoteattr(key)}>{value}</oneNumber>"
            for key, value in values.items()
        )
        self.send(
            f"<newNumberVector device={device} name={quoteattr(name)}>"
            f"{elements}</newNumberVector>"
        )

    def pump(self) -> None:
        try:
            data = self.sock.recv(1 << 20)
        except socket.timeout:
            return
        if not data:
            raise ConnectionError("indiserver closed the INDI connection")
        self.parser.feed(data)
        for event, element in self.parser.read_events():
            if event == "start":
                self.depth += 1
                continue
            self.depth -= 1
            if self.depth == 1:
                self.handle(element)
                element.clear()

    def handle(self, element: ET.Element) -> None:
        tag = element.tag
        if tag == "message":
            if element.get("device") in (None, self.device):
                logger.warning("INDI driver: %s", element.get("message", ""))
            return
        if element.get("device") != self.device:
            return
        if tag == "setBLOBVector":
            for blob in element.findall("oneBLOB"):
                self.blobs.append((blob.get("format", ".fits"), blob.text or ""))
            return
        if not tag.startswith(("def", "set")) or not tag.endswith(
            ("NumberVector", "SwitchVector", "TextVector")
        ):
            return
        name = element.get("name")
        if name is None:
            return
        self.states[name] = element.get("state", "")
        values = self.props.setdefault(name, {})
        for child in element:
            key = child.get("name")
            if key is not None:
                values[key] = (child.text or "").strip()

    def wait(self, condition: Callable[[], bool], timeout: float, description: str) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for {description}")
            self.pump()


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def start_indi_server(port: int, driver: str) -> None:
    global _server_process, _server_log, _server_port
    if port == 7624:
        raise ValueError("Port 7624 is reserved for the observatory's main INDI server")
    if _server_process is not None and (
        _server_process.poll() is not None or _server_port != port
    ):
        stop_indi_server()
    if _port_open(port):
        return

    run_dir = Path("~/.local/state/allsky-camera/indi").expanduser()
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / f"indiserver-{port}.log"
    log_handle = log_path.open("ab")
    try:
        process = subprocess.Popen(
            ["indiserver", "-v", "-p", str(port), driver],
            stdout=log_handle,
            stderr=log_handle,
            stdin=subprocess.DEVNULL,
        )
    except OSError:
        log_handle.close()
        raise
    _server_process = process
    _server_log = log_handle
    _server_port = port

    for _ in range(40):
        if process.poll() is not None:
            stop_indi_server()
            raise RuntimeError(f"indiserver exited; inspect {log_path}")
        if _port_open(port):
            logger.info("Started private indiserver on port %d (PID %d)", port, process.pid)
            return
        time.sleep(0.25)
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    stop_indi_server()
    raise RuntimeError(f"indiserver did not start; inspect {log_path}")


def stop_indi_server() -> None:
    global _server_process, _server_log, _server_port
    process = _server_process
    _server_process = None
    _server_port = None
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    if _server_log is not None:
        _server_log.close()
        _server_log = None


atexit.register(stop_indi_server)


def fits_to_uint8(data: bytes, stretch: bool = True) -> np.ndarray:
    """Decode a simple 2-D FITS image into 8-bit pixels, oriented top-row first."""
    header: dict[str, str] = {}
    offset = 0
    found_end = False
    while not found_end:
        block = data[offset:offset + 2880]
        if len(block) != 2880:
            raise ValueError("Truncated FITS header")
        offset += 2880
        for index in range(0, 2880, 80):
            card = block[index:index + 80].decode("ascii", "replace")
            key = card[:8].strip()
            if key == "END":
                found_end = True
                break
            if card[8:10] == "= ":
                header[key] = card[10:].split("/", 1)[0].strip()

    try:
        bitpix = int(header["BITPIX"])
        axes = int(header["NAXIS"])
        width = int(header["NAXIS1"])
        height = int(header["NAXIS2"])
    except (KeyError, ValueError) as exc:
        raise ValueError(f"Invalid or unsupported FITS image header: {exc}") from exc
    dtypes = {8: "u1", 16: ">i2", 32: ">i4", -32: ">f4"}
    if axes != 2 or bitpix not in dtypes or width <= 0 or height <= 0:
        raise ValueError(f"Unsupported FITS image dimensions or BITPIX={bitpix}")
    expected = width * height
    try:
        array = np.frombuffer(data, dtype=dtypes[bitpix], count=expected, offset=offset)
    except (ValueError, TypeError) as exc:
        raise ValueError("Truncated or invalid FITS image data") from exc
    if array.size != expected:
        raise ValueError("Truncated FITS image data")
    array = array.reshape(height, width).astype(np.float32)
    array += float(header.get("BZERO", "0"))
    array = np.flipud(array)

    if stretch:
        low, high = np.percentile(array, (0.5, 99.5))
        array = (array - low) / max(float(high - low), 1e-6) * 255
    elif bitpix == 8:
        pass
    elif bitpix == 16:
        array /= 256.0
    else:
        array = array / max(float(array.max()), 1.0) * 255
    return np.clip(array, 0, 255).astype(np.uint8)


def save_outputs(
    fits_data: bytes,
    base_path: Path,
    stretch: bool = True,
    *,
    save_fits: bool = True,
    save_jpg: bool = True,
) -> None:
    if not save_fits and not save_jpg:
        return
    base_path.parent.mkdir(parents=True, exist_ok=True)
    if save_fits:
        base_path.with_suffix(".fits").write_bytes(fits_data)
    if save_jpg:
        image = Image.fromarray(fits_to_uint8(fits_data, stretch))
        image.save(base_path.with_suffix(".jpg"), format="JPEG", quality=92)


def decode_blob(encoded_blob: str, blob_format: str) -> bytes:
    try:
        encoded = encoded_blob.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("INDI camera image BLOB contains non-ASCII data") from exc
    encoded = encoded.translate(None, b" \t\r\n")
    try:
        data = base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise ValueError("INDI camera image BLOB contains invalid base64 data") from exc
    if blob_format.endswith(".z"):
        try:
            data = zlib.decompress(data)
        except zlib.error as exc:
            raise ValueError("INDI camera image BLOB has invalid compressed data") from exc
    return data


def capture_frame(settings: Settings) -> tuple[Path, datetime, int]:
    settings.validate()
    start_indi_server(settings.indi_port, settings.indi_driver)
    client = IndiClient(settings.device_name, settings.indi_port)
    try:
        logger.info("Waiting for camera %s", settings.device_name)
        client.wait(lambda: "CONNECTION" in client.props, 30, "camera to appear")
        if client.props["CONNECTION"].get("CONNECT") != "On":
            client.set_switch("CONNECTION", "CONNECT")
        client.wait(
            lambda: (
                client.props.get("CONNECTION", {}).get("CONNECT") == "On"
                and "CCD_EXPOSURE" in client.props
                and "CCD_INFO" in client.props
                and float(client.props["CCD_INFO"].get("CCD_MAX_X", "0")) > 0
                and float(client.props["CCD_INFO"].get("CCD_MAX_Y", "0")) > 0
                and "UPLOAD_MODE" in client.props
            ),
            30,
            "camera to connect",
        )

        width = int(float(client.props["CCD_INFO"]["CCD_MAX_X"]))
        height = int(float(client.props["CCD_INFO"]["CCD_MAX_Y"]))
        client.set_switch("UPLOAD_MODE", "UPLOAD_CLIENT")
        if "CCD_TRANSFER_FORMAT" in client.props:
            client.set_switch("CCD_TRANSFER_FORMAT", "FORMAT_FITS")
        client.set_numbers("CCD_BINNING", HOR_BIN=1, VER_BIN=1)
        client.set_numbers("CCD_FRAME", X=0, Y=0, WIDTH=width, HEIGHT=height)
        if not settings.auto_gain and settings.manual_gain is not None:
            client.set_numbers("CCD_CONTROLS", Gain=settings.manual_gain)

        settle_deadline = time.monotonic() + 1
        while time.monotonic() < settle_deadline:
            client.pump()

        client.blobs.clear()
        captured_at = datetime.now().astimezone().replace(microsecond=0)
        logger.info("Exposing %.2f seconds at %dx%d", settings.exposure_seconds, width, height)
        client.set_numbers("CCD_EXPOSURE", CCD_EXPOSURE_VALUE=settings.exposure_seconds)

        def exposure_complete() -> bool:
            if client.states.get("CCD_EXPOSURE") == "Alert":
                raise RuntimeError("INDI driver reported an exposure error")
            return bool(client.blobs)

        client.wait(exposure_complete, settings.exposure_seconds + 30, "camera image")
        blob_format, encoded_blob = client.blobs[0]
        fits_data = decode_blob(encoded_blob, blob_format)

        filename_stamp = captured_at.strftime("%Y-%m-%d-%H-%M-%S")
        base_path = Path(settings.image_dir).expanduser() / f"allsky-{filename_stamp}"
        save_outputs(
            fits_data,
            base_path,
            stretch=True,
            save_fits=settings.save_fits,
            save_jpg=settings.save_jpg,
        )
        if settings.save_jpg:
            stamp_image(
                base_path.with_suffix(".jpg"),
                captured_at.strftime("%Y-%m-%d %H-%M-%S"),
            )
        star_count = count_stars(fits_to_uint8(fits_data))
        return base_path, captured_at, star_count
    finally:
        client.close()


def stamp_image(image_path: Path, stamp: str) -> None:
    with Image.open(image_path) as source:
        image = source.convert("RGB")
    draw = ImageDraw.Draw(image)
    bounds = draw.textbbox((8, 6), stamp)
    padding = 4
    draw.rectangle(
        (
            bounds[0] - padding,
            bounds[1] - padding,
            bounds[2] + padding,
            bounds[3] + padding,
        ),
        fill=(0, 0, 0),
    )
    draw.text((8, 6), stamp, fill=(255, 255, 255))
    image.save(image_path, format="JPEG", quality=92)
