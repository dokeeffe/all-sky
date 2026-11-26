from __future__ import annotations

import argparse
import logging
import signal
import threading
from pathlib import Path

from .settings import SettingsStore
from .s3_upload import S3UploadWorker
from .star_store import StarStore
from .web import Dashboard, serve
from .worker import CaptureWorker


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the all-sky camera service")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("~/.config/allsky-camera/settings.json").expanduser(),
        help="settings JSON path",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path("~/.local/state/allsky-camera").expanduser(),
        help="directory for persistent star-count history",
    )
    parser.add_argument("--host", default="127.0.0.1", help="web bind address")
    parser.add_argument("--port", type=int, default=8080, help="web bind port")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = SettingsStore(args.config)
    settings.load().validate()
    stars = StarStore(args.state_dir / "stars.sqlite3")
    worker = CaptureWorker(settings, stars)
    uploader = S3UploadWorker(settings, args.state_dir / "latest-upload.jpg")
    threads = [
        threading.Thread(target=worker.run, name="capture-worker", daemon=True),
        threading.Thread(target=uploader.run, name="s3-upload-worker", daemon=True),
    ]
    for thread in threads:
        thread.start()

    def stop_service(signum: int, frame: object) -> None:
        del frame
        logging.info("Received shutdown signal %d", signum)
        worker.stop()
        uploader.stop()
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_service)
    signal.signal(signal.SIGINT, stop_service)
    try:
        serve(Dashboard(settings, stars, worker, uploader), args.host, args.port)
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop()
        uploader.stop()
        for thread in threads:
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
