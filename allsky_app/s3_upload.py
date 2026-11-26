from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import boto3

from .settings import Settings, SettingsStore

logger = logging.getLogger(__name__)
UPLOAD_POLL_SECONDS = 5


def latest_jpeg(settings: Settings, upload_preview_path: Path) -> Path | None:
    if not settings.s3_upload:
        return None
    if settings.save_jpg:
        image_dir = Path(settings.image_dir).expanduser()
        try:
            images = list(image_dir.glob("allsky-*.jpg"))
        except OSError:
            logger.exception("Could not list JPEG images in %s", image_dir)
            return None
        candidates = []
        for image in images:
            try:
                candidates.append((image.stat().st_mtime_ns, image))
            except OSError:
                continue
        if candidates:
            return max(candidates, key=lambda item: item[0])[1]
    if upload_preview_path.is_file():
        return upload_preview_path
    return None


def upload_latest(settings: Settings, image_path: Path) -> None:
    """Upload a JPEG using explicit configured credentials, replacing the object key."""
    settings.validate()
    if not settings.s3_upload:
        raise ValueError("S3 upload is disabled")
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret,
        region_name="auto",
    )
    client.upload_file(str(image_path), settings.s3_bucket_name, settings.s3_object_key)


class S3UploadWorker:
    def __init__(
        self,
        settings: SettingsStore,
        upload_preview_path: Path,
        uploads_allowed: Callable[[], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.upload_preview_path = upload_preview_path.expanduser()
        self.uploads_allowed = uploads_allowed or (lambda: True)
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._status: dict[str, Any] = {
            "phase": "disabled",
            "last_uploaded_at": None,
            "last_error": None,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        config_key: tuple[Any, ...] | None = None
        next_upload = 0.0
        while not self.stop_event.is_set():
            settings: Settings | None = None
            try:
                settings = self.settings.load()
                if not settings.s3_upload:
                    config_key = None
                    self._set_status(phase="disabled", last_error=None)
                    self.stop_event.wait(UPLOAD_POLL_SECONDS)
                    continue

                current_key = (
                    settings.s3_upload_interval_minutes,
                    settings.image_dir,
                    settings.save_jpg,
                    settings.s3_bucket_name,
                    settings.s3_object_key,
                    settings.s3_access_key_id,
                    settings.s3_secret,
                    settings.s3_endpoint,
                )
                if current_key != config_key:
                    config_key = current_key
                    next_upload = time.monotonic()

                now = time.monotonic()
                if now >= next_upload:
                    if not self.uploads_allowed():
                        self._set_status(
                            phase="blocked by roof safety",
                            last_error=None,
                        )
                        self.stop_event.wait(UPLOAD_POLL_SECONDS)
                        continue
                    image = latest_jpeg(settings, self.upload_preview_path)
                    if image is None:
                        self._set_status(
                            phase="waiting for image",
                            last_error="Waiting for the first available JPEG",
                        )
                        self.stop_event.wait(UPLOAD_POLL_SECONDS)
                        continue
                    self._set_status(phase="uploading", last_error=None)
                    upload_latest(settings, image)
                    uploaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    self._set_status(
                        phase="waiting",
                        last_uploaded_at=uploaded_at,
                        last_error=None,
                    )
                    logger.info("Uploaded latest all-sky image to configured S3 object")
                    interval = settings.s3_upload_interval_minutes
                    if interval is None:
                        raise ValueError("S3 upload interval is missing")
                    next_upload = time.monotonic() + interval * 60
                self.stop_event.wait(
                    min(UPLOAD_POLL_SECONDS, max(0.1, next_upload - time.monotonic()))
                )
            except Exception as exc:
                logger.exception("S3 upload cycle failed")
                self._set_status(phase="error", last_error=str(exc))
                interval_minutes = (
                    settings.s3_upload_interval_minutes
                    if settings is not None and settings.s3_upload
                    else None
                )
                retry_delay = (
                    max(UPLOAD_POLL_SECONDS, interval_minutes * 60)
                    if interval_minutes is not None
                    else UPLOAD_POLL_SECONDS
                )
                next_upload = time.monotonic() + retry_delay
                self.stop_event.wait(retry_delay)
        self._set_status(phase="stopped")

    def _set_status(self, **values: Any) -> None:
        with self._lock:
            self._status.update(values)
