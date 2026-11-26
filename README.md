# All-sky camera

A lightweight Python service that continuously captures FITS and JPEG images
from an INDI camera. It keeps the existing `allsky-YYYY-MM-DD-HH-MM-SS`
filename convention, adds a visible timestamp to each JPEG, and stores an
approximate star count for every frame in SQLite.

## Requirements

- Ubuntu/Linux with `indiserver` and the selected INDI camera driver installed.
- `uv` installed for the account that will run the service.
- Camera permissions configured for that account.

The private camera INDI server defaults to port **7625**. Port **7624** is
reserved by this application for the observatory's main INDI server and is
rejected by settings validation.

## Install as a systemd user service

Run from the project directory as the account that owns the camera:

```sh
./install.sh
```

The installer creates/synchronizes `.venv`, creates default settings in
`~/.config/allsky-camera/settings.json`, and enables the
`allsky-camera.service` user unit. To start user services at boot without an
interactive login, enable lingering for the account:

```sh
sudo loginctl enable-linger "$USER"
```

Useful service commands:

```sh
systemctl --user status allsky-camera
systemctl --user restart allsky-camera
journalctl --user -u allsky-camera -f
```

The web interface listens only on `127.0.0.1:8080` by default. Open
<http://127.0.0.1:8080> on the observatory PC. To use it remotely, expose it
through a trusted authenticated reverse proxy rather than binding the
unauthenticated settings interface directly to a public network.

## Run manually

```sh
uv sync
uv run python -m allsky_app
```

Settings are persisted in `~/.config/allsky-camera/settings.json`. The SQLite
time series is stored in `~/.local/state/allsky-camera/stars.sqlite3`. Images
default to `~/Pictures/allsky`; change this and camera settings in the web UI.

The default image capture uses a 15-second exposure and a 1-second delay.
With automatic/default gain enabled, the application leaves the driver's gain
setting alone. If automatic/default gain is disabled, supply a manual gain;
that numeric value is sent using the INDI `CCD_CONTROLS` `Gain` property.

## Roof safety script

Optionally set an executable script path in the web UI. The service runs it
without a shell and interprets exit status `0` as roof open and safe to capture,
`1` as roof closed, and any other exit status, timeout, or execution error as
unsafe. It rechecks at the configured interval (five minutes by default) and
caches that result in between checks. An unset safety script allows capture.

## Star counts and tests

Star counts are approximate detections of isolated image peaks above a robust
background-noise threshold; use them to follow changing sky conditions rather
than as a scientific photometry measurement. Tests do not require a connected
camera:

```sh
uv run python -m unittest discover -s tests -v
```
