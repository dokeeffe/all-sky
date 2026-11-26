#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
CONFIG_DIR="$HOME/.config/allsky-camera"
STATE_DIR="$HOME/.local/state/allsky-camera"
UNIT_DIR="$HOME/.config/systemd/user"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required. Install it from https://docs.astral.sh/uv/ first." >&2
    exit 1
fi

if ! command -v indiserver >/dev/null 2>&1; then
    echo "indiserver is required. Install the INDI server and selected camera driver." >&2
    exit 1
fi

uv sync --project "$PROJECT_DIR" --no-dev
mkdir -p "$CONFIG_DIR" "$STATE_DIR" "$UNIT_DIR"

if [[ ! -e "$CONFIG_DIR/settings.json" ]]; then
    cat > "$CONFIG_DIR/settings.json" <<'JSON'
{
  "image_dir": "~/Pictures/allsky",
  "indi_driver": "indi_asi_ccd",
  "device_name": "ZWO CCD ASI120MM",
  "exposure_seconds": 15.0,
  "delay_seconds": 1.0,
  "safety_script": "",
  "safety_check_minutes": 5.0,
  "auto_gain": true,
  "manual_gain": null,
  "indi_port": 7625
}
JSON
fi

cat > "$UNIT_DIR/allsky-camera.service" <<UNIT
[Unit]
Description=All-sky camera capture and web interface
After=network.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/.venv/bin/python -m allsky_app --config $CONFIG_DIR/settings.json --state-dir $STATE_DIR
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now allsky-camera.service
echo "All-sky camera service installed. Web UI: http://127.0.0.1:8080"
