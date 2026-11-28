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
rejected by settings validation. Keep the EKOS/observatory server and the
all-sky camera server on different ports; for example, use 7624 for the
observatory server and 7625 for all-sky. All-sky gives its INDI server a
separate local connection socket so it can coexist with EKOS's local server.
The same physical camera cannot be controlled concurrently by both servers.

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
FITS and JPEG saving can be enabled independently in Camera settings; star
counts are still calculated from the downloaded FITS frame even when FITS is
not saved. Both formats are enabled by default. The image-storage limit
defaults to 5 GB. Every five minutes, the service checks its managed
`allsky-*.fits` and `allsky-*.jpg` files and, if they exceed the configured
limit, removes oldest image sets in batches of approximately 200 MiB. Other
files in the image directory are not included in the limit or deleted.

## S3-compatible uploads

Enable periodic JPEG uploads in Camera settings and provide an upload interval
in minutes, bucket name, object key, access key ID, secret access key, and
endpoint URL. The service uses the supplied credentials directly with boto3
(it does not read AWS credentials from environment variables) and uploads to
the same bucket/key each time, replacing that object with the latest JPEG.
With JPEG saving disabled, a single temporary JPEG is kept in the application
state directory for uploads instead of saving JPEGs alongside the captures.
The settings JSON, including credentials, is written with owner-only
permissions; keep it private and use a bucket key with only the required
write permissions. When a roof safety script is configured, uploads are
paused unless its most recently checked result confirms the roof is open.

## Roof safety script

Optionally set an executable script path in the web UI. The service runs it
without a shell and interprets exit status `0` as roof open and safe to capture,
`1` as roof closed, and any other exit status, timeout, or execution error as
unsafe. It rechecks at the configured interval (five minutes by default) and
caches that result in between checks. An unset safety script allows capture.

For the observatory roof endpoint returning `{"state": "OPEN"}` or
`{"state": "CLOSED"}`, save this as an executable script such as
`~/bin/check-observatory-roof.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

response="$(curl --fail --silent --show-error --max-time 10 \
  http://192.168.1.228:8080/roof)"
state="$(jq -er '.state | select(. == "OPEN" or . == "CLOSED")' <<<"$response")"

if [[ "$state" == "OPEN" ]]; then
    exit 0
fi

exit 1
```

Make it executable with `chmod +x ~/bin/check-observatory-roof.sh` and enter
that full path in the Safety script setting. This example requires `curl` and
`jq` (`sudo apt install curl jq`). `OPEN` returns `0`, `CLOSED` returns `1`;
curl failures, timeouts, invalid JSON, or unexpected state values return a
different nonzero status, which the camera service treats as unsafe.

## Star counts and tests

The dashboard shows the latest recorded star count beside the live image and
plots the count history for the most recent six hours. Both refresh with new
captures; camera settings are tucked into a collapsed panel. Counts use
Photutils' DAOStarFinder with a locally estimated, sigma-clipped background and
filters for point-source sharpness and roundness. They remain approximate (the
detector does not perform photometric calibration) and are intended for
following sky conditions, not scientific measurement. A compressed FITS sample
containing stars and foreground obstructions is included in the tests. Tests do
not require a connected camera:

```sh
uv run python -m unittest discover -s tests -v
```
