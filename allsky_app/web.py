from __future__ import annotations

import html
import io
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PIL import Image

from .camera import fits_to_uint8
from .s3_upload import S3UploadWorker
from .settings import Settings, SettingsStore
from .star_store import StarStore, filter_star_count_outliers
from .worker import CaptureWorker

logger = logging.getLogger(__name__)


class Dashboard:
    def __init__(
        self,
        settings: SettingsStore,
        stars: StarStore,
        worker: CaptureWorker,
        uploader: S3UploadWorker | None = None,
    ) -> None:
        self.settings = settings
        self.stars = stars
        self.worker = worker
        self.uploader = uploader

    def latest_image(self) -> Path | None:
        settings = self.settings.load()
        if not settings.save_fits and not settings.save_jpg:
            return None
        image_dir = Path(settings.image_dir).expanduser()
        if not image_dir.is_dir():
            return None
        suffixes = []
        if settings.save_jpg:
            suffixes.append(".jpg")
        if settings.save_fits:
            suffixes.append(".fits")
        candidates = [
            path
            for suffix in suffixes
            for path in image_dir.glob(f"allsky-*{suffix}")
        ]
        latest: Path | None = None
        latest_mtime = -1
        for path in candidates:
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue
            if mtime > latest_mtime:
                latest = path
                latest_mtime = mtime
        return latest

    @property
    def latest_annotated_image(self) -> Path:
        return self.stars.path.parent / "latest-annotated.jpg"


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "AllSkyCamera/0.1"
    dashboard: Dashboard

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self._send_html(self._render_page())
        elif path == "/latest.jpg":
            image = self.dashboard.latest_image()
            if image is None:
                self.send_error(404, "No captured image is available")
                return
            try:
                image_data = image.read_bytes()
                if image.suffix.lower() == ".fits":
                    preview = Image.fromarray(fits_to_uint8(image_data))
                    buffer = io.BytesIO()
                    preview.save(buffer, format="JPEG", quality=92)
                    content = buffer.getvalue()
                else:
                    content = image_data
            except OSError:
                logger.exception("Could not read latest image %s", image)
                self.send_error(500, "Could not read latest image")
                return
            except (ValueError, KeyError) as exc:
                logger.exception("Could not preview FITS image %s", image)
                self.send_error(500, f"Could not preview FITS image: {exc}")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
        elif path == "/latest-annotated.jpg":
            image = self.dashboard.latest_annotated_image
            try:
                content = image.read_bytes()
            except FileNotFoundError:
                self.send_error(404, "No annotated image is available")
                return
            except OSError:
                logger.exception("Could not read annotated image %s", image)
                self.send_error(500, "Could not read annotated image")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
        elif path == "/api/status":
            latest = self.dashboard.latest_image()
            try:
                latest_version = latest.stat().st_mtime_ns if latest else None
            except OSError:
                latest = None
                latest_version = None
            try:
                annotated_version = (
                    self.dashboard.latest_annotated_image.stat().st_mtime_ns
                )
            except FileNotFoundError:
                annotated_version = None
            except OSError:
                logger.exception(
                    "Could not stat annotated image %s",
                    self.dashboard.latest_annotated_image,
                )
                annotated_version = None
            self._send_json({
                "worker": self.dashboard.worker.status(),
                "s3_upload": (
                    self.dashboard.uploader.status()
                    if self.dashboard.uploader is not None
                    else {"phase": "disabled", "last_uploaded_at": None, "last_error": None}
                ),
                "latest_star": self.dashboard.stars.latest(),
                "star_series": filter_star_count_outliers(
                    self.dashboard.stars.since(6)
                ),
                "latest_image": latest.name if latest else None,
                "latest_image_path": str(latest) if latest else None,
                "latest_version": latest_version,
                "latest_annotated_version": annotated_version,
            })
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
            s3_enabled = "s3_upload" in form
            previous = self.dashboard.settings.load()
            access_key_id = self._form_value(form, "s3_access_key_id")
            secret = self._form_value(form, "s3_secret")
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
                "save_fits": "save_fits" in form,
                "save_jpg": "save_jpg" in form,
                "max_image_dir_gb": self._form_value(form, "max_image_dir_gb"),
                "s3_upload": s3_enabled,
                "s3_upload_interval_minutes": self._form_value(
                    form, "s3_upload_interval_minutes"
                ),
                "s3_bucket_name": self._form_value(form, "s3_bucket_name"),
                "s3_object_key": self._form_value(form, "s3_object_key"),
                "s3_access_key_id": (
                    access_key_id
                    if access_key_id
                    else previous.s3_access_key_id if s3_enabled else ""
                ),
                "s3_secret": (
                    secret if secret else previous.s3_secret if s3_enabled else ""
                ),
                "s3_endpoint": self._form_value(form, "s3_endpoint"),
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
        uploader_status = (
            self.dashboard.uploader.status()
            if self.dashboard.uploader is not None
            else {"phase": "disabled", "last_uploaded_at": None, "last_error": None}
        )
        latest = self.dashboard.latest_image()
        checked = "checked" if settings.auto_gain else ""
        latest_star = self.dashboard.stars.latest()
        s3_interval = (
            f"{settings.s3_upload_interval_minutes:g}"
            if settings.s3_upload_interval_minutes is not None
            else ""
        )
        try:
            latest_version = latest.stat().st_mtime_ns if latest else ""
        except OSError:
            latest = None
            latest_version = ""
        latest_key = f"{latest.name}:{latest_version}" if latest else ""
        image_html = (
            f'<img id="latest-image" class="latest" data-version="{html.escape(latest_key, quote=True)}" '
            f'src="/latest.jpg?v={latest_version}" alt="Latest all-sky image">'
            if latest else '<p class="empty">No image captured yet.</p>'
        )
        error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
        star_count = str(latest_star["star_count"]) if latest_star else "—"
        star_time = (
            html.escape(str(latest_star["captured_at"]))
            if latest_star else "Waiting for first capture"
        )
        status_text = html.escape(str(status["phase"]))
        safety_text = "not configured" if status["safety_open"] is None else (
            "open" if status["safety_open"] else "closed / blocked"
        )
        upload_text = html.escape(str(uploader_status["phase"]))
        upload_time = uploader_status["last_uploaded_at"] or "not uploaded yet"
        upload_error = uploader_status["last_error"]
        upload_error_html = (
            f'<span class="error">{html.escape(str(upload_error))}</span>'
            if upload_error else ""
        )
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>All-sky camera</title>
<style>
body{{font:16px system-ui,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;color:#17212b;background:#f4f7fa}}
h1,h2{{margin-bottom:.5rem}} section{{background:white;padding:1.2rem;margin:1rem 0;border-radius:8px;box-shadow:0 1px 5px #ccd3da}}
.section-heading{{display:flex;align-items:center;justify-content:space-between;gap:1rem}}
.section-heading button{{margin:0;flex-shrink:0}}
label{{display:block;margin:.75rem 0 .25rem}} input{{box-sizing:border-box;max-width:100%;padding:.45rem;width:30rem}}
input[type=checkbox]{{width:auto}}button{{margin-top:1rem;padding:.65rem 1rem;background:#174f78;color:white;border:0;border-radius:4px}}
.latest{{width:100%;height:auto}}.error{{color:#a00}}.empty{{padding:3rem;text-align:center;background:#111;color:#ddd}}
.muted{{color:#52616d}}
.live-grid{{display:grid;grid-template-columns:minmax(0,3fr) minmax(190px,1fr);gap:1rem;align-items:start}}
.star-card{{background:#f4f7fa;padding:1rem;border-radius:8px;text-align:center}}
.star-count{{font-size:clamp(2.5rem,7vw,5rem);font-weight:700;line-height:1.1;color:#174f78}}
.chart{{width:100%;height:auto;min-height:180px}}.chart text{{font:12px system-ui,sans-serif;fill:#52616d}}
details{{background:white;padding:1rem 1.2rem;margin:1rem 0;border-radius:8px;box-shadow:0 1px 5px #ccd3da}}
summary{{cursor:pointer;font-size:1.2rem;font-weight:600}}details form{{padding-top:.75rem}}
@media(max-width:680px){{.live-grid{{grid-template-columns:1fr}}.star-card{{text-align:left}}}}
</style></head><body>
<h1>All-sky camera</h1>
<p class="muted">Capture service: <strong>{status_text}</strong> · Roof safety: <strong>{html.escape(safety_text)}</strong></p>
<p class="muted">S3 upload: <strong id="s3-upload-status">{upload_text}</strong> · Last upload: <span id="s3-upload-time">{html.escape(str(upload_time))}</span> <span id="s3-upload-error" class="error">{html.escape(str(upload_error or ""))}</span></p>
{error_html}
<section><div class="section-heading"><h2>Live sky</h2>
<button id="annotated-toggle" type="button" aria-pressed="false">Show detected sources</button></div><div class="live-grid">
<div><div id="image-container">{image_html}</div>
<p id="latest-path" class="muted">{html.escape(str(latest) if latest else '')}</p></div>
<div class="star-card"><h3>Latest star count</h3>
<div id="latest-star-count" class="star-count">{html.escape(star_count)}</div>
<p class="muted">Updated <time id="latest-star-time">{star_time}</time></p>
<p class="muted">Approximate detections</p></div></div>
<p class="muted">Image and star count update automatically when captures are saved.</p></section>
<section><h2>Star counts · last 6 hours</h2>
<svg id="star-chart" class="chart" viewBox="0 0 800 280" role="img"
aria-label="Star count time series for the last six hours"></svg>
<p id="chart-empty" class="muted">Waiting for star-count measurements.</p>
<p class="muted">Approximate point-source detections; useful for following sky conditions.</p></section>
<details><summary>Camera settings</summary>
<form method="post" action="/settings">
<label for="image_dir">Image directory</label><input id="image_dir" name="image_dir" required value="{html.escape(settings.image_dir, quote=True)}">
<label for="indi_driver">INDI driver</label><input id="indi_driver" name="indi_driver" required value="{html.escape(settings.indi_driver, quote=True)}">
<label for="device_name">Camera device name</label><input id="device_name" name="device_name" required value="{html.escape(settings.device_name, quote=True)}">
<label for="indi_port">Private INDI server port (must be unused; 7624 reserved for observatory)</label><input id="indi_port" name="indi_port" type="number" min="1024" max="65535" required value="{settings.indi_port}">
<label for="exposure_seconds">Exposure (seconds)</label><input id="exposure_seconds" name="exposure_seconds" type="number" min="0.01" max="3600" step="0.1" required value="{settings.exposure_seconds:g}">
<label for="delay_seconds">Delay between exposures (seconds)</label><input id="delay_seconds" name="delay_seconds" type="number" min="0" max="3600" step="0.1" required value="{settings.delay_seconds:g}">
<label><input type="checkbox" name="auto_gain" value="yes" {checked}> Leave automatic/default gain enabled</label>
<label for="manual_gain">Manual gain (required when auto gain is disabled)</label><input id="manual_gain" name="manual_gain" type="number" min="0" step="any" value="{'' if settings.manual_gain is None else settings.manual_gain}">
<fieldset><legend>Save image formats</legend>
<label><input type="checkbox" name="save_fits" value="yes" {"checked" if settings.save_fits else ""}> Save original FITS</label>
<label><input type="checkbox" name="save_jpg" value="yes" {"checked" if settings.save_jpg else ""}> Save stretched JPEG preview</label>
<p class="muted">Star counts are calculated from the downloaded FITS frame even if FITS is not saved.</p>
</fieldset>
<label for="max_image_dir_gb">Maximum camera image storage (GB)</label>
<input id="max_image_dir_gb" name="max_image_dir_gb" type="number" min="0.1" max="100000" step="0.1" required value="{settings.max_image_dir_gb:g}">
<p class="muted">Checked every 5 minutes. When exceeded, oldest all-sky FITS/JPEG image sets are removed in batches of about 200 MiB.</p>
<fieldset><legend>S3-compatible upload</legend>
<label><input id="s3_upload" type="checkbox" name="s3_upload" value="yes" {"checked" if settings.s3_upload else ""}> Enable periodic latest-JPEG upload</label>
<label for="s3_upload_interval_minutes">Upload interval (minutes)</label>
<input id="s3_upload_interval_minutes" name="s3_upload_interval_minutes" type="number" min="0.1" step="0.1" value="{s3_interval}">
<label for="s3_bucket_name">Bucket name</label><input id="s3_bucket_name" name="s3_bucket_name" value="{html.escape(settings.s3_bucket_name, quote=True)}">
<label for="s3_object_key">Object key (remote filename)</label><input id="s3_object_key" name="s3_object_key" value="{html.escape(settings.s3_object_key, quote=True)}">
<label for="s3_access_key_id">Access key ID</label><input id="s3_access_key_id" name="s3_access_key_id" type="password" autocomplete="new-password" data-required-on-enable="{"false" if settings.s3_access_key_id else "true"}" placeholder="{"saved; blank keeps current" if settings.s3_access_key_id else ""}">
<label for="s3_secret">Secret access key</label><input id="s3_secret" name="s3_secret" type="password" autocomplete="new-password" data-required-on-enable="{"false" if settings.s3_secret else "true"}" placeholder="{"saved; blank keeps current" if settings.s3_secret else ""}">
<label for="s3_endpoint">S3 endpoint URL</label><input id="s3_endpoint" name="s3_endpoint" type="url" placeholder="https://..." value="{html.escape(settings.s3_endpoint, quote=True)}">
<p class="muted">Credentials are stored in the local settings file with owner-only permissions. Existing keys are not displayed; leave either credential blank to keep it unchanged.</p>
</fieldset>
<label for="safety_script">Optional roof safety script (exit 0=open, 1=closed)</label><input id="safety_script" name="safety_script" value="{html.escape(settings.safety_script, quote=True)}">
<label for="safety_check_minutes">Safety check/cache interval (minutes)</label><input id="safety_check_minutes" name="safety_check_minutes" type="number" min="0.1" max="1440" step="0.1" required value="{settings.safety_check_minutes:g}">
<button type="submit">Save settings</button></form></details>
<script>
(() => {{
  const s3Checkbox = document.getElementById("s3_upload");
  const s3Fields = [
    "s3_upload_interval_minutes", "s3_bucket_name", "s3_object_key",
    "s3_access_key_id", "s3_secret", "s3_endpoint"
  ].map(id => document.getElementById(id));
  function updateS3Requirements() {{
    for (const field of s3Fields) {{
      field.required = s3Checkbox.checked && field.dataset.requiredOnEnable !== "false";
    }}
  }}
  s3Checkbox.addEventListener("change", updateS3Requirements);
  updateS3Requirements();

  const container = document.getElementById("image-container");
  const pathLabel = document.getElementById("latest-path");
  const annotatedToggle = document.getElementById("annotated-toggle");
  const countLabel = document.getElementById("latest-star-count");
  const timeLabel = document.getElementById("latest-star-time");
  const chart = document.getElementById("star-chart");
  const chartEmpty = document.getElementById("chart-empty");
  const svgNamespace = "http://www.w3.org/2000/svg";
  let showAnnotated = false;
  let latestImageVersion = null;
  let latestAnnotatedVersion = null;

  function updateImage() {{
    const image = container.querySelector("img");
    const version = showAnnotated ? latestAnnotatedVersion : latestImageVersion;
    if (!image || version === null) return;
    const source = showAnnotated
      ? `/latest-annotated.jpg?v=${{encodeURIComponent(version)}}`
      : `/latest.jpg?v=${{encodeURIComponent(version)}}`;
    const key = `${{showAnnotated ? "annotated" : "live"}}:${{version}}`;
    if (image.dataset.version !== key) {{
      image.dataset.version = key;
      image.src = source;
      image.alt = showAnnotated
        ? "Latest all-sky image with detected sources annotated"
        : "Latest all-sky image";
    }}
  }}

  annotatedToggle.addEventListener("click", () => {{
    showAnnotated = !showAnnotated;
    annotatedToggle.setAttribute("aria-pressed", showAnnotated.toString());
    annotatedToggle.textContent = showAnnotated
      ? "Show live sky"
      : "Show detected sources";
    updateImage();
  }});

  function drawChart(points) {{
    chart.replaceChildren();
    if (!points.length) {{
      chartEmpty.hidden = false;
      return;
    }}
    chartEmpty.hidden = true;
    const width = 800, height = 280;
    const left = 58, right = 18, top = 20, bottom = 42;
    const plotWidth = width - left - right;
    const plotHeight = height - top - bottom;
    const now = Date.now();
    const start = now - 6 * 60 * 60 * 1000;
    const maxCount = Math.max(1, ...points.map(point => point.star_count));
    const x = time => left + ((time - start) / (now - start)) * plotWidth;
    const y = count => top + plotHeight - (count / maxCount) * plotHeight;
    function add(tag, attributes, text) {{
      const element = document.createElementNS(svgNamespace, tag);
      for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
      if (text !== undefined) element.textContent = text;
      chart.append(element);
      return element;
    }}
    for (let tick = 0; tick <= 4; tick++) {{
      const value = maxCount * tick / 4;
      const yPosition = y(value);
      add("line", {{x1: left, y1: yPosition, x2: width - right, y2: yPosition, stroke: "#dce3e9"}});
      add("text", {{x: left - 10, y: yPosition + 4, "text-anchor": "end"}}, Math.round(value).toString());
    }}
    add("text", {{x: left, y: height - 10}}, new Date(start).toLocaleTimeString([], {{hour: "2-digit", minute: "2-digit"}}));
    add("text", {{x: width - right, y: height - 10, "text-anchor": "end"}}, "now");
    const coordinates = points.map(point => {{
      const timestamp = Date.parse(point.captured_at);
      return [x(timestamp), y(point.star_count)];
    }}).filter(point => Number.isFinite(point[0]) && Number.isFinite(point[1]));
    if (coordinates.length) {{
      add("polyline", {{
        points: coordinates.map(point => point.join(",")).join(" "),
        fill: "none", stroke: "#174f78", "stroke-width": 3,
        "stroke-linejoin": "round", "stroke-linecap": "round"
      }});
      for (const [pointX, pointY] of coordinates) {{
        add("circle", {{cx: pointX, cy: pointY, r: 4, fill: "#174f78"}});
      }}
    }}
  }}

  async function checkForNewImage() {{
    try {{
      const response = await fetch("/api/status", {{cache: "no-store"}});
      if (!response.ok) throw new Error(`Status request failed: ${{response.status}}`);
      const status = await response.json();
      latestImageVersion = status.latest_version;
      latestAnnotatedVersion = status.latest_annotated_version;
      const uploadStatus = status.s3_upload;
      document.getElementById("s3-upload-status").textContent = uploadStatus.phase;
      document.getElementById("s3-upload-time").textContent =
        uploadStatus.last_uploaded_at || "not uploaded yet";
      const uploadError = document.getElementById("s3-upload-error");
      uploadError.textContent = uploadStatus.last_error || "";
      uploadError.hidden = !uploadStatus.last_error;
      if (status.latest_image && status.latest_version !== null) {{
        let image = container.querySelector("img");
        if (!image) {{
          container.replaceChildren();
          image = document.createElement("img");
          image.className = "latest";
          image.alt = "Latest all-sky image";
          container.append(image);
        }}
        pathLabel.textContent = status.latest_image_path;
        updateImage();
      }}
      if (status.latest_star) {{
        countLabel.textContent = status.latest_star.star_count.toLocaleString();
        timeLabel.textContent = status.latest_star.captured_at;
        timeLabel.dateTime = status.latest_star.captured_at;
      }} else {{
        countLabel.textContent = "—";
        timeLabel.textContent = "Waiting for first capture";
      }}
      drawChart(status.star_series);
    }} catch (error) {{
      console.error("Could not check for a new all-sky image", error);
    }}
  }}
  checkForNewImage();
  window.setInterval(checkForNewImage, 3000);
}})();
</script>
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
