from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .camera import capture_frame, stop_indi_server
from .image_maintenance import prune_image_directory
from .settings import Settings, SettingsStore
from .star_store import StarStore

logger = logging.getLogger(__name__)
STORAGE_CHECK_INTERVAL_SECONDS = 5 * 60


class CaptureWorker:
    def __init__(self, settings: SettingsStore, stars: StarStore) -> None:
        self.settings = settings
        self.stars = stars
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._status: dict[str, Any] = {
            "phase": "starting",
            "last_image": None,
            "last_star_count": None,
            "last_capture_at": None,
            "last_error": None,
            "safety_open": None,
            "safety_checked_at": None,
        }
        self._safety_key: tuple[str, float] | None = None
        self._safety_checked_monotonic = 0.0
        self._safety_open = True
        self._storage_cleanup_key: tuple[str, float] | None = None
        self._last_storage_cleanup = 0.0

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        try:
            while not self.stop_event.is_set():
                try:
                    settings = self.settings.load()
                    try:
                        self._maybe_prune_images(settings)
                    except OSError as exc:
                        logger.exception("Could not prune the all-sky image directory")
                        self._set_status(last_error=f"Image cleanup failed: {exc}")
                    if self.check_safety(settings):
                        self._set_status(phase="capturing")
                        base, captured_at, stars = capture_frame(settings)
                        image_record = (
                            base.with_suffix(".fits")
                            if settings.save_fits
                            else base.with_suffix(".jpg")
                            if settings.save_jpg
                            else base
                        )
                        self.stars.add(image_record, stars, captured_at)
                        self._set_status(
                            phase="waiting",
                            last_image=(
                                str(base.with_suffix(".jpg"))
                                if settings.save_jpg
                                else str(base.with_suffix(".fits"))
                                if settings.save_fits
                                else None
                            ),
                            last_star_count=stars,
                            last_capture_at=captured_at.isoformat(timespec="seconds"),
                            last_error=None,
                        )
                        logger.info("Captured %s with %d detected stars", base, stars)
                    else:
                        self._set_status(phase="waiting")
                        logger.info("Safety script reports roof closed; capture skipped")
                    delay = settings.delay_seconds
                except Exception as exc:
                    logger.exception("Capture cycle failed")
                    self._set_status(phase="error", last_error=str(exc))
                    delay = 5.0
                self.stop_event.wait(delay)
        finally:
            stop_indi_server()
            self._set_status(phase="stopped")

    def _maybe_prune_images(self, settings: Settings) -> None:
        image_dir = str(Path(settings.image_dir).expanduser())
        key = (image_dir, settings.max_image_dir_gb)
        now = time.monotonic()
        if (
            key != self._storage_cleanup_key
            or now - self._last_storage_cleanup >= STORAGE_CHECK_INTERVAL_SECONDS
        ):
            self._storage_cleanup_key = key
            self._last_storage_cleanup = now
            prune_image_directory(
                Path(image_dir),
                int(settings.max_image_dir_gb * 1_000_000_000),
            )

    def check_safety(self, settings: Settings) -> bool:
        script = settings.safety_script.strip()
        if not script:
            self._safety_key = None
            self._safety_open = True
            self._set_status(safety_open=True, safety_checked_at=None)
            return True

        key = (script, settings.safety_check_minutes)
        now = time.monotonic()
        interval = settings.safety_check_minutes * 60
        if self._safety_key != key or now - self._safety_checked_monotonic >= interval:
            self._safety_key = key
            self._safety_checked_monotonic = now
            try:
                result = subprocess.run(
                    [str(Path(script).expanduser())],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self._safety_open = result.returncode == 0
                if result.returncode not in (0, 1):
                    logger.error(
                        "Safety script %s failed with exit code %d: %s",
                        script, result.returncode, result.stderr.strip(),
                    )
            except (OSError, subprocess.TimeoutExpired) as exc:
                logger.error("Safety script %s could not be checked: %s", script, exc)
                self._safety_open = False
            self._set_status(
                safety_open=self._safety_open,
                safety_checked_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            )
        return self._safety_open

    def _set_status(self, **values: Any) -> None:
        with self._lock:
            self._status.update(values)
