from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, cast


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
        for key in ("auto_gain", "save_fits", "save_jpg"):
            if type(merged[key]) is not bool:
                raise ValueError(f"{key} must be a boolean")
        try:
            merged["exposure_seconds"] = float(merged["exposure_seconds"])
            merged["delay_seconds"] = float(merged["delay_seconds"])
            merged["safety_check_minutes"] = float(merged["safety_check_minutes"])
            merged["max_image_dir_gb"] = float(merged["max_image_dir_gb"])
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
            os.replace(temp_name, self.path)
        finally:
            if temp_name is not None and os.path.exists(temp_name):
                os.unlink(temp_name)
