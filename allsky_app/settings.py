from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    image_dir: str = "~/Pictures/allsky"
    indi_driver: str = "indi_asi_ccd"
    device_name: str = "ZWO CCD ASI120MM"
    exposure_seconds: float = 15.0
    delay_seconds: float = 1.0
    safety_script: str = ""
    safety_check_minutes: float = 5.0
    auto_gain: bool = True
    manual_gain: float | None = None
    indi_port: int = 7625
    save_fits: bool = True
    save_jpg: bool = True
    max_image_dir_gb: float = 5.0
    s3_upload: bool = False
    s3_upload_interval_minutes: float | None = None
    s3_bucket_name: str = ""
    s3_object_key: str = ""
    s3_access_key_id: str = ""
    s3_secret: str = ""
    s3_endpoint: str = ""

    def validate(self) -> Settings:
        if not self.image_dir.strip():
            raise ValueError("Image directory cannot be empty")
        if not self.indi_driver.strip() or not self.device_name.strip():
            raise ValueError("INDI driver and device name cannot be empty")
        if not math.isfinite(self.exposure_seconds) or not 0 < self.exposure_seconds <= 3600:
            raise ValueError("Exposure must be between 0 and 3600 seconds")
        if not math.isfinite(self.delay_seconds) or not 0 <= self.delay_seconds <= 3600:
            raise ValueError("Delay must be between 0 and 3600 seconds")
        if (
            not math.isfinite(self.safety_check_minutes)
            or not 0.1 <= self.safety_check_minutes <= 1440
        ):
            raise ValueError("Safety check interval must be between 0.1 and 1440 minutes")
        if not math.isfinite(self.max_image_dir_gb) or not 0.1 <= self.max_image_dir_gb <= 100_000:
            raise ValueError("Image directory limit must be between 0.1 and 100000 GB")
        if (
            not 1024 <= self.indi_port <= 65535
            or self.indi_port == 7624
        ):
            raise ValueError("INDI port must be 1024-65535 and cannot be 7624")
        if self.manual_gain is not None and (
            not math.isfinite(self.manual_gain) or self.manual_gain < 0
        ):
            raise ValueError("Manual gain cannot be negative")
        if not self.auto_gain and self.manual_gain is None:
            raise ValueError("Set a manual gain value when auto gain is disabled")
        if self.s3_upload:
            required = {
                "S3 upload interval": self.s3_upload_interval_minutes,
                "S3 bucket name": self.s3_bucket_name,
                "S3 object key": self.s3_object_key,
                "S3 access key ID": self.s3_access_key_id,
                "S3 secret": self.s3_secret,
                "S3 endpoint": self.s3_endpoint,
            }
            missing = [
                name
                for name, value in required.items()
                if value is None or (isinstance(value, str) and not value.strip())
            ]
            if missing:
                raise ValueError(f"Required when S3 upload is enabled: {', '.join(missing)}")
            if self.s3_upload_interval_minutes is None or (
                not math.isfinite(self.s3_upload_interval_minutes)
                or self.s3_upload_interval_minutes <= 0
            ):
                raise ValueError("S3 upload interval must be greater than zero minutes")
            endpoint = urlsplit(self.s3_endpoint)
            if endpoint.scheme not in ("http", "https") or not endpoint.netloc:
                raise ValueError("S3 endpoint must be an http:// or https:// URL")
        elif self.s3_upload_interval_minutes is not None and (
            not math.isfinite(self.s3_upload_interval_minutes)
            or self.s3_upload_interval_minutes <= 0
        ):
            raise ValueError("S3 upload interval must be greater than zero minutes")
        return self

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> Settings:
        allowed = {item.name for item in fields(cls)}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")

        merged = asdict(cls())
        merged.update(values)
        for key in ("image_dir", "indi_driver", "device_name", "safety_script"):
            if not isinstance(merged[key], str):
                raise ValueError(f"{key} must be text")
        for key in ("auto_gain", "save_fits", "save_jpg", "s3_upload"):
            if type(merged[key]) is not bool:
                raise ValueError(f"{key} must be a boolean")
        for key in (
            "s3_bucket_name",
            "s3_object_key",
            "s3_access_key_id",
            "s3_secret",
            "s3_endpoint",
        ):
            if not isinstance(merged[key], str):
                raise ValueError(f"{key} must be text")
        try:
            merged["exposure_seconds"] = float(merged["exposure_seconds"])
            merged["delay_seconds"] = float(merged["delay_seconds"])
            merged["safety_check_minutes"] = float(merged["safety_check_minutes"])
            merged["max_image_dir_gb"] = float(merged["max_image_dir_gb"])
            if merged["s3_upload_interval_minutes"] not in (None, ""):
                merged["s3_upload_interval_minutes"] = float(
                    merged["s3_upload_interval_minutes"]
                )
            else:
                merged["s3_upload_interval_minutes"] = None
            port = merged["indi_port"]
            if isinstance(port, bool):
                raise ValueError("INDI port must be an integer")
            port_as_float = float(port)
            if not math.isfinite(port_as_float) or not port_as_float.is_integer():
                raise ValueError("INDI port must be an integer")
            merged["indi_port"] = int(port_as_float)
            if merged["manual_gain"] not in (None, ""):
                merged["manual_gain"] = float(merged["manual_gain"])
            else:
                merged["manual_gain"] = None
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid numeric setting: {exc}") from exc
        return cls(**merged).validate()


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        try:
            values: Any = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read settings file {self.path}: {exc}") from exc
        if not isinstance(values, dict):
            raise ValueError(f"Settings file {self.path} must contain a JSON object")
        return Settings.from_mapping(cast(dict[str, Any], values))

    def save(self, settings: Settings) -> None:
        settings.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=f".{self.path.name}.", delete=False,
            ) as temp_file:
                temp_name = temp_file.name
                json.dump(asdict(settings), temp_file, indent=2)
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
        finally:
            if temp_name is not None and os.path.exists(temp_name):
                os.unlink(temp_name)
