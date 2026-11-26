from __future__ import annotations

import base64
import struct
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
from PIL import Image

from allsky_app import camera
from allsky_app.camera import stamp_image
from allsky_app.settings import Settings, SettingsStore
from allsky_app.star_store import StarStore
from allsky_app.stars import count_stars
from allsky_app.web import Dashboard, DashboardHandler
from allsky_app.worker import CaptureWorker


class SettingsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        settings = Settings()
        self.assertEqual(settings.image_dir, "~/Pictures/allsky")
        self.assertEqual(settings.indi_port, 7625)
        self.assertEqual(settings.exposure_seconds, 15.0)
        self.assertTrue(settings.auto_gain)

    def test_rejects_observatory_indi_port(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot be 7624"):
            Settings.from_mapping({"indi_port": 7624})

    def test_manual_gain_is_required_when_auto_gain_is_off(self) -> None:
        with self.assertRaisesRegex(ValueError, "manual gain"):
            Settings.from_mapping({"auto_gain": False})

    def test_settings_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            original = Settings(image_dir="/tmp/allsky", exposure_seconds=18.5)
            store.save(original)
            self.assertEqual(store.load(), original)


class StarCountTests(unittest.TestCase):
    def test_counts_isolated_peaks_above_background(self) -> None:
        image = np.zeros((30, 30), dtype=np.uint8)
        image[5, 5] = 100
        image[12, 21] = 200
        image[25, 4] = 150
        self.assertEqual(count_stars(image), 3)

    def test_rejects_non_image_shape(self) -> None:
        with self.assertRaisesRegex(ValueError, "2-D image"):
            count_stars(np.zeros((10, 10, 3)))

    def test_image_timestamp_is_drawn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frame.jpg"
            Image.new("RGB", (200, 80), "black").save(path)
            stamp_image(path, "2026-11-26 21-39-02")
            with Image.open(path) as stamped:
                self.assertGreater(np.asarray(stamped).max(), 0)

    def test_decodes_fits_image(self) -> None:
        image = camera.fits_to_uint8(make_fits_image(), stretch=False)
        self.assertEqual(image.shape, (3, 3))
        self.assertEqual(image.tolist(), [[0, 0, 0], [0, 0, 0], [0, 0, 0]])

    def test_rejects_truncated_fits_header(self) -> None:
        with self.assertRaisesRegex(ValueError, "Truncated FITS header"):
            camera.fits_to_uint8(b"short")


class CameraCaptureTests(unittest.TestCase):
    def test_decodes_indib_blob_with_wrapped_base64(self) -> None:
        fits_data = make_fits_image()
        encoded = base64.b64encode(fits_data).decode("ascii")
        wrapped = f"\n  {encoded[:40]}\r\n{encoded[40:]} \t"
        self.assertEqual(camera.decode_blob(wrapped, ".fits"), fits_data)

    def test_rejects_non_base64_blob_data(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid base64"):
            camera.decode_blob("%%% not base64 %%%", ".fits")

    def test_capture_saves_files_without_loading_sample_script(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fits_data = make_fits_image()

            class FakeIndiClient:
                def __init__(self, device: str, port: int) -> None:
                    self.props = {
                        "CONNECTION": {"CONNECT": "On"},
                        "CCD_EXPOSURE": {"CCD_EXPOSURE_VALUE": "15"},
                        "CCD_INFO": {"CCD_MAX_X": "3", "CCD_MAX_Y": "3"},
                        "UPLOAD_MODE": {"UPLOAD_CLIENT": "On"},
                    }
                    self.states: dict[str, str] = {}
                    self.blobs: list[tuple[str, str]] = []
                    self.closed = False

                def close(self) -> None:
                    self.closed = True

                def set_switch(self, name: str, element: str) -> None:
                    pass

                def set_numbers(self, name: str, **values: float) -> None:
                    if name == "CCD_EXPOSURE":
                        encoded = base64.b64encode(fits_data).decode("ascii")
                        self.blobs.append((".fits", f"\n{encoded[:64]}\n{encoded[64:]}"))

                def pump(self) -> None:
                    pass

                def wait(self, condition, timeout: float, description: str) -> None:
                    if not condition():
                        raise TimeoutError(description)

            with (
                patch.object(camera, "start_indi_server") as start_server,
                patch.object(camera, "IndiClient", FakeIndiClient),
                patch.object(camera, "stamp_image") as stamp,
            ):
                settings = Settings(image_dir=directory)
                base_path, captured_at, star_count = camera.capture_frame(settings)

            start_server.assert_called_once_with(7625, "indi_asi_ccd")
            stamp.assert_called_once()
            self.assertEqual(base_path.parent, Path(directory))
            self.assertRegex(base_path.name, r"^allsky-\d{4}-\d\d-\d\d-\d\d-\d\d-\d\d$")
            self.assertTrue(base_path.with_suffix(".fits").is_file())
            self.assertTrue(base_path.with_suffix(".jpg").is_file())
            self.assertEqual(star_count, 0)
            self.assertIsInstance(captured_at, datetime)


class StoreAndSafetyTests(unittest.TestCase):
    def test_star_counts_are_persisted_in_time_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StarStore(Path(directory) / "stars.sqlite3")
            first = Path(directory) / "first.fits"
            second = Path(directory) / "second.fits"
            from datetime import datetime, timezone

            store.add(first, 4, datetime(2026, 1, 1, tzinfo=timezone.utc))
            store.add(second, 9, datetime(2026, 1, 2, tzinfo=timezone.utc))
            self.assertEqual([row["star_count"] for row in store.recent(2)], [9, 4])

    def test_closed_safety_result_is_cached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worker = CaptureWorker(
                SettingsStore(Path(directory) / "settings.json"),
                StarStore(Path(directory) / "stars.sqlite3"),
            )
            settings = Settings(safety_script="/path/to/roof-check")
            with patch(
                "allsky_app.worker.subprocess.run",
                return_value=SimpleNamespace(returncode=1, stderr=""),
            ) as run:
                self.assertFalse(worker.check_safety(settings))
                self.assertFalse(worker.check_safety(settings))
                run.assert_called_once()
            self.assertFalse(worker.status()["safety_open"])


class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.image_dir = root / "images"
        self.image_dir.mkdir()
        self.settings = SettingsStore(root / "settings.json")
        self.settings.save(Settings(image_dir=str(self.image_dir)))
        self.stars = StarStore(root / "stars.sqlite3")
        self.worker = CaptureWorker(self.settings, self.stars)
        self.dashboard = Dashboard(self.settings, self.stars, self.worker)

        bound_dashboard = self.dashboard

        class TestDashboardHandler(DashboardHandler):
            dashboard = bound_dashboard

        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            TestDashboardHandler,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def test_dashboard_and_latest_image(self) -> None:
        page = urlopen(self.base_url).read().decode()
        self.assertIn("All-sky camera", page)
        image = self.image_dir / "allsky-2026-11-26-21-39-02.jpg"
        image.write_bytes(b"jpeg data")
        response = urlopen(f"{self.base_url}/latest.jpg")
        self.assertEqual(response.headers["Content-Type"], "image/jpeg")
        self.assertEqual(response.read(), b"jpeg data")

    def test_saves_valid_settings(self) -> None:
        data = (
            "image_dir=%2Ftmp%2Fupdated&indi_driver=indi_asi_ccd"
            "&device_name=Camera&indi_port=7625&exposure_seconds=20"
            "&delay_seconds=2&safety_script=&safety_check_minutes=5"
            "&manual_gain=&auto_gain=yes"
        )
        request = Request(
            f"{self.base_url}/settings",
            data=data.encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        response = urlopen(request)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.url, f"{self.base_url}/")
        self.assertEqual(self.settings.load().image_dir, "/tmp/updated")
        self.assertEqual(self.settings.load().exposure_seconds, 20)
        self.assertTrue(self.settings.load().auto_gain)

    def test_rejects_main_observatory_port(self) -> None:
        data = (
            "image_dir=%2Ftmp%2Fimages&indi_driver=indi_asi_ccd"
            "&device_name=Camera&indi_port=7624&exposure_seconds=20"
            "&delay_seconds=2&safety_script=&safety_check_minutes=5"
            "&manual_gain="
        )
        request = Request(
            f"{self.base_url}/settings",
            data=data.encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(self.settings.load().indi_port, 7625)


def make_fits_image() -> bytes:
    cards = [
        "SIMPLE  =                    T",
        "BITPIX  =                   16",
        "NAXIS   =                    2",
        "NAXIS1  =                    3",
        "NAXIS2  =                    3",
        "END",
    ]
    header = "".join(card.ljust(80) for card in cards).ljust(2880).encode("ascii")
    return header + struct.pack(">hhhhhhhhh", 0, 0, 0, 0, 0, 0, 0, 0, 0)


if __name__ == "__main__":
    unittest.main()
