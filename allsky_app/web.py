from __future__ import annotations

import html
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from .settings import Settings, SettingsStore
from .star_store import StarStore
from .worker import CaptureWorker

logger = logging.getLogger(__name__)


class Dashboard:
    def __init__(
        self,
        settings: SettingsStore,
        stars: StarStore,
        worker: CaptureWorker,
    ) -> None:
        self.settings = settings
        self.stars = stars
        self.worker = worker

    def latest_image(self) -> Path | None:
        image_dir = Path(self.settings.load().image_dir).expanduser()
        if not image_dir.is_dir():
            return None
        images = list(image_dir.glob("allsky-*.jpg"))
        return max(images, key=lambda path: path.stat().st_mtime) if images else None


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "AllSkyCamera/0.1"
    dashboard: Dashboard

    def do_GET(self) -> None:
        if self.path == "/":
            self._send_html(self._render_page())
        elif self.path == "/latest.jpg":
            image = self.dashboard.latest_image()
            if image is None:
                self.send_error(404, "No captured image is available")
                return
            try:
                content = image.read_bytes()
            except OSError:
                logger.exception("Could not read latest image %s", image)
                self.send_error(500, "Could not read latest image")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
        elif self.path == "/api/status":
            self._send_json({"worker": self.dashboard.worker.status(),
                             "recent_stars": self.dashboard.stars.recent(20)})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path != "/settings":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16_384:
                raise ValueError("Invalid form size")
            form = parse_qs(
                self.rfile.read(length).decode("utf-8"),
                keep_blank_values=True,
                strict_parsing=True,
            )
            settings = Settings.from_mapping({
                "image_dir": self._form_value(form, "image_dir"),
                "indi_driver": self._form_value(form, "indi_driver"),
                "device_name": self._form_value(form, "device_name"),
                "exposure_seconds": self._form_value(form, "exposure_seconds"),
                "delay_seconds": self._form_value(form, "delay_seconds"),
                "safety_script": self._form_value(form, "safety_script"),
                "safety_check_minutes": self._form_value(form, "safety_check_minutes"),
                "auto_gain": "auto_gain" in form,
                "manual_gain": self._form_value(form, "manual_gain"),
                "indi_port": self._form_value(form, "indi_port"),
            })
            self.dashboard.settings.save(settings)
        except (UnicodeDecodeError, ValueError, OSError) as exc:
            self._send_html(self._render_page(error=str(exc)), status=400)
            return
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        logger.info("%s - %s", self.address_string(), format % args)

    @staticmethod
    def _form_value(form: dict[str, list[str]], name: str) -> str:
        values = form.get(name)
        if not values:
            raise ValueError(f"Missing field: {name}")
        return values[-1].strip()

    def _render_page(self, error: str | None = None) -> str:
        settings = self.dashboard.settings.load()
        status = self.dashboard.worker.status()
        latest = self.dashboard.latest_image()
        checked = "checked" if settings.auto_gain else ""
        image_html = (
            '<img class="latest" src="/latest.jpg" alt="Latest all-sky image">'
            if latest else '<p class="empty">No image captured yet.</p>'
        )
        error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
        rows = "".join(
            "<tr><td>{}</td><td>{}</td></tr>".format(
                html.escape(str(row["captured_at"])),
                html.escape(str(row["star_count"])),
            )
            for row in self.dashboard.stars.recent(20)
        )
        if not rows:
            rows = '<tr><td colspan="2">No measurements yet.</td></tr>'
        status_text = html.escape(str(status["phase"]))
        safety_text = "not configured" if status["safety_open"] is None else (
            "open" if status["safety_open"] else "closed / blocked"
        )
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>All-sky camera</title>
<style>
body{{font:16px system-ui,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;color:#17212b;background:#f4f7fa}}
h1,h2{{margin-bottom:.5rem}} section{{background:white;padding:1.2rem;margin:1rem 0;border-radius:8px;box-shadow:0 1px 5px #ccd3da}}
label{{display:block;margin:.75rem 0 .25rem}} input{{box-sizing:border-box;max-width:100%;padding:.45rem;width:30rem}}
input[type=checkbox]{{width:auto}}button{{margin-top:1rem;padding:.65rem 1rem;background:#174f78;color:white;border:0;border-radius:4px}}
.latest{{width:100%;height:auto}}.error{{color:#a00}}.empty{{padding:3rem;text-align:center;background:#111;color:#ddd}}
table{{border-collapse:collapse;width:100%}}td,th{{padding:.5rem;border-bottom:1px solid #ddd;text-align:left}}
.muted{{color:#52616d}}
</style></head><body>
<h1>All-sky camera</h1>
<p class="muted">Capture service: <strong>{status_text}</strong> · Roof safety: <strong>{html.escape(safety_text)}</strong></p>
{error_html}
<section><h2>Latest image</h2>{image_html}<p class="muted">{html.escape(str(latest) if latest else '')}</p></section>
<section><h2>Settings</h2>
<form method="post" action="/settings">
<label for="image_dir">Image directory</label><input id="image_dir" name="image_dir" required value="{html.escape(settings.image_dir, quote=True)}">
<label for="indi_driver">INDI driver</label><input id="indi_driver" name="indi_driver" required value="{html.escape(settings.indi_driver, quote=True)}">
<label for="device_name">Camera device name</label><input id="device_name" name="device_name" required value="{html.escape(settings.device_name, quote=True)}">
<label for="indi_port">Private INDI server port (7624 reserved for observatory)</label><input id="indi_port" name="indi_port" type="number" min="1024" max="65535" required value="{settings.indi_port}">
<label for="exposure_seconds">Exposure (seconds)</label><input id="exposure_seconds" name="exposure_seconds" type="number" min="0.01" max="3600" step="0.1" required value="{settings.exposure_seconds:g}">
<label for="delay_seconds">Delay between exposures (seconds)</label><input id="delay_seconds" name="delay_seconds" type="number" min="0" max="3600" step="0.1" required value="{settings.delay_seconds:g}">
<label><input type="checkbox" name="auto_gain" value="yes" {checked}> Leave automatic/default gain enabled</label>
<label for="manual_gain">Manual gain (required when auto gain is disabled)</label><input id="manual_gain" name="manual_gain" type="number" min="0" step="any" value="{'' if settings.manual_gain is None else settings.manual_gain}">
<label for="safety_script">Optional roof safety script (exit 0=open, 1=closed)</label><input id="safety_script" name="safety_script" value="{html.escape(settings.safety_script, quote=True)}">
<label for="safety_check_minutes">Safety check/cache interval (minutes)</label><input id="safety_check_minutes" name="safety_check_minutes" type="number" min="0.1" max="1440" step="0.1" required value="{settings.safety_check_minutes:g}">
<button type="submit">Save settings</button></form></section>
<section><h2>Star-count history</h2><table><thead><tr><th>Captured at (UTC)</th><th>Detected stars</th></tr></thead><tbody>{rows}</tbody></table>
<p class="muted">Counts are approximate local-maximum detections, useful for tracking sky conditions.</p></section>
</body></html>"""

    def _send_html(self, content: str, status: int = 200) -> None:
        encoded = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, value: object) -> None:
        encoded = json.dumps(value).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


def serve(dashboard: Dashboard, host: str, port: int) -> None:
    bound_dashboard = dashboard

    class BoundDashboardHandler(DashboardHandler):
        dashboard = bound_dashboard

    server = ThreadingHTTPServer(
        (host, port),
        BoundDashboardHandler,
    )
    server.daemon_threads = True
    logger.info("Web UI available at http://%s:%d", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
