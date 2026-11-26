from __future__ import annotations

import base64
import json
import os
import struct
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
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
from allsky_app.image_maintenance import prune_image_directory
from allsky_app.s3_upload import S3UploadWorker, latest_jpeg, upload_latest
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
        self.assertTrue(settings.save_fits)
        self.assertTrue(settings.save_jpg)
        self.assertEqual(settings.max_image_dir_gb, 5.0)

    def test_rejects_observatory_indi_port(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot be 7624"):
            Settings.from_mapping({"indi_port": 7624})

    def test_manual_gain_is_required_when_auto_gain_is_off(self) -> None:
        with self.assertRaisesRegex(ValueError, "manual gain"):
            Settings.from_mapping({"auto_gain": False})

    def test_s3_upload_requires_all_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "Required when S3 upload is enabled"):
            Settings.from_mapping({"s3_upload": True})

    def test_s3_upload_validates_interval_and_endpoint(self) -> None:
        values = {
            "s3_upload": True,
            "s3_upload_interval_minutes": 5,
            "s3_bucket_name": "allsky",
            "s3_object_key": "current.jpg",
            "s3_access_key_id": "access",
            "s3_secret": "secret",
            "s3_endpoint": "https://s3.example.test",
        }
        self.assertEqual(Settings.from_mapping(values).s3_upload_interval_minutes, 5)
        with self.assertRaisesRegex(ValueError, "endpoint must be"):
            Settings.from_mapping({**values, "s3_endpoint": "not-an-endpoint"})

    def test_settings_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            original = Settings(
                image_dir="/tmp/allsky",
                exposure_seconds=18.5,
                save_fits=False,
                max_image_dir_gb=7,
            )
            store.save(original)
            self.assertEqual(store.load(), original)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)


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

    def test_saves_only_selected_image_formats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fits_data = make_fits_image()
            base = root / "fits-only"
            camera.save_outputs(fits_data, base, save_fits=True, save_jpg=False)
            self.assertTrue(base.with_suffix(".fits").exists())
            self.assertFalse(base.with_suffix(".jpg").exists())

            base = root / "jpg-only"
            camera.save_outputs(fits_data, base, save_fits=False, save_jpg=True)
            self.assertFalse(base.with_suffix(".fits").exists())
            self.assertTrue(base.with_suffix(".jpg").exists())

            base = root / "no-files"
            camera.save_outputs(fits_data, base, save_fits=False, save_jpg=False)
            self.assertFalse(base.with_suffix(".fits").exists())
            self.assertFalse(base.with_suffix(".jpg").exists())

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
                settings = Settings(
                    image_dir=directory,
                    save_fits=False,
                    save_jpg=False,
                )
                base_path, captured_at, star_count = camera.capture_frame(settings)

            start_server.assert_called_once_with(7625, "indi_asi_ccd")
            stamp.assert_not_called()
            self.assertEqual(base_path.parent, Path(directory))
            self.assertRegex(base_path.name, r"^allsky-\d{4}-\d\d-\d\d-\d\d-\d\d-\d\d$")
            self.assertFalse(base_path.with_suffix(".fits").exists())
            self.assertFalse(base_path.with_suffix(".jpg").exists())
            self.assertEqual(star_count, 0)
            self.assertIsInstance(captured_at, datetime)

    def test_s3_upload_can_use_a_temporary_jpeg_when_jpg_saving_is_off(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fits_data = make_fits_image()
            preview_path = root / "state" / "latest-upload.jpg"

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

                def close(self) -> None:
                    pass

                def set_switch(self, name: str, element: str) -> None:
                    pass

                def set_numbers(self, name: str, **values: float) -> None:
                    if name == "CCD_EXPOSURE":
                        encoded = base64.b64encode(fits_data).decode("ascii")
                        self.blobs.append((".fits", encoded))

                def pump(self) -> None:
                    pass

                def wait(self, condition, timeout: float, description: str) -> None:
                    if not condition():
                        raise TimeoutError(description)

            settings = Settings(
                image_dir=str(root / "images"),
                save_fits=False,
                save_jpg=False,
                s3_upload=True,
                s3_upload_interval_minutes=5,
                s3_bucket_name="bucket",
                s3_object_key="latest.jpg",
                s3_access_key_id="access",
                s3_secret="secret",
                s3_endpoint="https://s3.example.test",
            )
            with (
                patch.object(camera, "start_indi_server"),
                patch.object(camera, "IndiClient", FakeIndiClient),
            ):
                camera.capture_frame(settings, upload_preview_path=preview_path)

            self.assertTrue(preview_path.is_file())
            self.assertFalse((root / "images").exists())


class StoreAndSafetyTests(unittest.TestCase):
    def test_s3_safety_gate_requires_current_open_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            worker = CaptureWorker(
                store,
                StarStore(Path(directory) / "stars.sqlite3"),
            )
            store.save(Settings(safety_script="/roof-check"))
            self.assertFalse(worker.uploads_allowed())
            with patch(
                "allsky_app.worker.subprocess.run",
                return_value=SimpleNamespace(returncode=1, stderr=""),
            ):
                self.assertFalse(worker.check_safety(store.load()))
            self.assertFalse(worker.uploads_allowed())
            with patch(
                "allsky_app.worker.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stderr=""),
            ):
                worker._safety_checked_monotonic = 0
                self.assertTrue(worker.check_safety(store.load()))
            self.assertTrue(worker.uploads_allowed())

    def test_latest_jpeg_selects_most_recent_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "allsky-first.jpg"
            second = root / "allsky-second.jpg"
            first.write_bytes(b"old")
            second.write_bytes(b"new")
            os.utime(first, (1, 1))
            os.utime(second, (2, 2))
            settings = Settings(
                image_dir=directory,
                s3_upload=True,
                s3_upload_interval_minutes=1,
                s3_bucket_name="bucket",
                s3_object_key="latest.jpg",
                s3_access_key_id="access",
                s3_secret="secret",
                s3_endpoint="https://s3.example.test",
            )
            self.assertEqual(latest_jpeg(settings, root / "fallback.jpg"), second)

    def test_upload_uses_configured_credentials_and_overwrites_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "latest.jpg"
            image.write_bytes(b"jpeg")
            settings = Settings(
                s3_upload=True,
                s3_upload_interval_minutes=5,
                s3_bucket_name="observatory",
                s3_object_key="allsky/current.jpg",
                s3_access_key_id="configured-access",
                s3_secret="configured-secret",
                s3_endpoint="https://objects.example.test",
            )
            with patch("allsky_app.s3_upload.boto3.client") as client_factory:
                upload_latest(settings, image)
            client_factory.assert_called_once_with(
                "s3",
                endpoint_url="https://objects.example.test",
                aws_access_key_id="configured-access",
                aws_secret_access_key="configured-secret",
                region_name="auto",
            )
            client_factory.return_value.upload_file.assert_called_once_with(
                str(image), "observatory", "allsky/current.jpg"
            )

    def test_upload_worker_uploads_immediately_and_reloads_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SettingsStore(root / "settings.json")
            settings = Settings(
                s3_upload=True,
                s3_upload_interval_minutes=5,
                s3_bucket_name="bucket",
                s3_object_key="latest.jpg",
                s3_access_key_id="access",
                s3_secret="secret",
                s3_endpoint="https://s3.example.test",
            )
            store.save(settings)
            preview = root / "latest-upload.jpg"
            preview.write_bytes(b"jpg")
            uploader = S3UploadWorker(store, preview)
            uploaded = threading.Event()

            def upload(_settings: Settings, _path: Path) -> None:
                uploaded.set()
                uploader.stop()

            with patch("allsky_app.s3_upload.upload_latest", side_effect=upload):
                uploader.run()

            self.assertTrue(uploaded.is_set())
            self.assertEqual(uploader.status()["phase"], "stopped")

    def test_s3_upload_waits_until_roof_is_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SettingsStore(root / "settings.json")
            store.save(Settings(
                s3_upload=True,
                s3_upload_interval_minutes=5,
                s3_bucket_name="bucket",
                s3_object_key="latest.jpg",
                s3_access_key_id="access",
                s3_secret="secret",
                s3_endpoint="https://s3.example.test",
            ))
            preview = root / "latest-upload.jpg"
            preview.write_bytes(b"jpg")
            roof = {"open": False}
            uploader = S3UploadWorker(
                store,
                preview,
                uploads_allowed=lambda: roof["open"],
            )

            def wait_for_roof(_timeout: float) -> None:
                roof["open"] = True

            with (
                patch.object(uploader.stop_event, "wait", side_effect=wait_for_roof),
                patch("allsky_app.s3_upload.time.monotonic", return_value=100),
                patch("allsky_app.s3_upload.upload_latest") as upload,
            ):
                upload.side_effect = lambda *_args: uploader.stop()
                uploader.run()

            upload.assert_called_once()

    def test_storage_cleanup_is_rate_limited_to_five_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worker = CaptureWorker(
                SettingsStore(Path(directory) / "settings.json"),
                StarStore(Path(directory) / "stars.sqlite3"),
            )
            settings = Settings(image_dir=directory)
            with (
                patch("allsky_app.worker.time.monotonic", side_effect=[10, 100, 311]),
                patch("allsky_app.worker.prune_image_directory") as prune,
            ):
                worker._maybe_prune_images(settings)
                worker._maybe_prune_images(settings)
                worker._maybe_prune_images(settings)
            self.assertEqual(prune.call_count, 2)

    def test_prunes_oldest_camera_image_sets_in_batches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_fits = root / "allsky-old.fits"
            old_jpg = root / "allsky-old.jpg"
            new_fits = root / "allsky-new.fits"
            new_jpg = root / "allsky-new.jpg"
            unrelated = root / "notes.txt"
            for path, data in (
                (old_fits, b"123"),
                (old_jpg, b"456"),
                (new_fits, b"7890"),
                (new_jpg, b"abcd"),
                (unrelated, b"leave"),
            ):
                path.write_bytes(data)
            os.utime(old_fits, (1, 1))
            os.utime(old_jpg, (1, 1))
            os.utime(new_fits, (2, 2))
            os.utime(new_jpg, (2, 2))

            reclaimed = prune_image_directory(root, max_bytes=10, batch_bytes=5)

            self.assertEqual(reclaimed, 6)
            self.assertFalse(old_fits.exists())
            self.assertFalse(old_jpg.exists())
            self.assertTrue(new_fits.exists())
            self.assertTrue(new_jpg.exists())
            self.assertTrue(unrelated.exists())

    def test_star_counts_are_persisted_in_time_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StarStore(Path(directory) / "stars.sqlite3")
            first = Path(directory) / "first.fits"
            second = Path(directory) / "second.fits"
            from datetime import datetime, timezone

            store.add(first, 4, datetime(2026, 1, 1, tzinfo=timezone.utc))
            store.add(second, 9, datetime(2026, 1, 2, tzinfo=timezone.utc))
            self.assertEqual([row["star_count"] for row in store.recent(2)], [9, 4])

    def test_latest_value_and_six_hour_series(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StarStore(Path(directory) / "stars.sqlite3")
            now = datetime.now(timezone.utc)
            store.add(Path(directory) / "old.fits", 1, now - timedelta(hours=7))
            store.add(Path(directory) / "recent.fits", 12, now - timedelta(hours=2))
            store.add(Path(directory) / "latest.fits", 18, now)

            self.assertEqual(store.latest()["star_count"], 18)
            series = store.since(6)
            self.assertEqual([row["star_count"] for row in series], [12, 18])

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
        self.assertIn("Latest star count", page)
        self.assertIn("Camera settings", page)
        self.assertIn("<details>", page)
        self.assertIn('id="star-chart"', page)
        self.assertNotIn("<table>", page)
        self.assertIn('name="save_fits"', page)
        self.assertIn('name="save_jpg"', page)
        self.assertIn('value="5"', page)
        self.assertIn("batches of about 200 MiB", page)
        self.assertIn("Enable periodic latest-JPEG upload", page)
        image = self.image_dir / "allsky-2026-11-26-21-39-02.jpg"
        image.write_bytes(b"jpeg data")
        response = urlopen(f"{self.base_url}/latest.jpg")
        self.assertEqual(response.headers["Content-Type"], "image/jpeg")
        self.assertEqual(response.read(), b"jpeg data")
        response = urlopen(f"{self.base_url}/latest.jpg?v=unique")
        self.assertEqual(response.read(), b"jpeg data")

    def test_status_api_reports_latest_image_version(self) -> None:
        image = self.image_dir / "allsky-2026-11-26-21-39-02.jpg"
        image.write_bytes(b"jpeg data")
        self.stars.add(
            image.with_suffix(".fits"),
            42,
            datetime.now(timezone.utc),
        )
        response = urlopen(f"{self.base_url}/api/status")
        status = json.loads(response.read())
        self.assertEqual(status["latest_image"], image.name)
        self.assertEqual(status["latest_image_path"], str(image))
        self.assertIsInstance(status["latest_version"], int)
        self.assertEqual(status["latest_star"]["star_count"], 42)
        self.assertEqual(status["star_series"][-1]["star_count"], 42)

    def test_dashboard_has_auto_refresh_polling(self) -> None:
        page = urlopen(self.base_url).read().decode()
        self.assertIn("window.setInterval(checkForNewImage, 3000)", page)
        self.assertIn("drawChart(status.star_series)", page)

    def test_saves_valid_settings(self) -> None:
        data = (
            "image_dir=%2Ftmp%2Fupdated&indi_driver=indi_asi_ccd"
            "&device_name=Camera&indi_port=7625&exposure_seconds=20"
            "&delay_seconds=2&safety_script=&safety_check_minutes=5"
            "&manual_gain=&auto_gain=yes&save_fits=yes&save_jpg=yes"
            "&max_image_dir_gb=5&s3_upload_interval_minutes="
            "&s3_bucket_name=&s3_object_key=&s3_access_key_id=&s3_secret=&s3_endpoint="
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
        self.assertTrue(self.settings.load().save_fits)
        self.assertTrue(self.settings.load().save_jpg)
        self.assertEqual(self.settings.load().max_image_dir_gb, 5)

    def test_settings_form_can_disable_jpeg(self) -> None:
        data = (
            "image_dir=%2Ftmp%2Fupdated&indi_driver=indi_asi_ccd"
            "&device_name=Camera&indi_port=7625&exposure_seconds=20"
            "&delay_seconds=2&safety_script=&safety_check_minutes=5"
            "&manual_gain=&auto_gain=yes&save_fits=yes&max_image_dir_gb=3"
            "&s3_upload_interval_minutes=&s3_bucket_name=&s3_object_key="
            "&s3_access_key_id=&s3_secret=&s3_endpoint="
        )
        request = Request(
            f"{self.base_url}/settings",
            data=data.encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urlopen(request) as response:
            self.assertEqual(response.status, 200)
        saved = self.settings.load()
        self.assertTrue(saved.save_fits)
        self.assertFalse(saved.save_jpg)
        self.assertEqual(saved.max_image_dir_gb, 3)

    def test_s3_settings_are_saved_and_credentials_are_not_rendered(self) -> None:
        data = (
            "image_dir=%2Ftmp%2Fupdated&indi_driver=indi_asi_ccd"
            "&device_name=Camera&indi_port=7625&exposure_seconds=20"
            "&delay_seconds=2&safety_script=&safety_check_minutes=5"
            "&manual_gain=&auto_gain=yes&save_fits=yes&save_jpg=yes"
            "&max_image_dir_gb=5&s3_upload=yes&s3_upload_interval_minutes=10"
            "&s3_bucket_name=observatory&s3_object_key=current.jpg"
            "&s3_access_key_id=sample-access&s3_secret=sample-secret"
            "&s3_endpoint=https%3A%2F%2Fobjects.example.test"
        )
        request = Request(
            f"{self.base_url}/settings",
            data=data.encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urlopen(request) as response:
            self.assertEqual(response.status, 200)
        saved = self.settings.load()
        self.assertTrue(saved.s3_upload)
        self.assertEqual(saved.s3_upload_interval_minutes, 10)
        self.assertEqual(saved.s3_bucket_name, "observatory")
        self.assertEqual(saved.s3_object_key, "current.jpg")
        self.assertEqual(saved.s3_access_key_id, "sample-access")
        self.assertEqual(saved.s3_secret, "sample-secret")
        self.assertEqual(saved.s3_endpoint, "https://objects.example.test")
        page = urlopen(self.base_url).read().decode()
        self.assertNotIn("sample-secret", page)
        self.assertNotIn("sample-access", page)
        self.assertIn("saved; blank keeps current", page)

    def test_enabled_s3_form_rejects_missing_required_configuration(self) -> None:
        data = (
            "image_dir=%2Ftmp%2Fupdated&indi_driver=indi_asi_ccd"
            "&device_name=Camera&indi_port=7625&exposure_seconds=20"
            "&delay_seconds=2&safety_script=&safety_check_minutes=5"
            "&manual_gain=&auto_gain=yes&save_fits=yes&save_jpg=yes"
            "&max_image_dir_gb=5&s3_upload=yes&s3_upload_interval_minutes="
            "&s3_bucket_name=&s3_object_key=&s3_access_key_id=&s3_secret=&s3_endpoint="
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
        self.assertIn(b"Required when S3 upload is enabled", error.exception.read())
        error.exception.close()

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
