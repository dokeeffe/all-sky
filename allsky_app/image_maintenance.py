from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

PRUNE_BATCH_BYTES = 200 * 1024 * 1024
MANAGED_SUFFIXES = {".fits", ".jpg"}


def prune_image_directory(
    image_dir: Path,
    max_bytes: int,
    batch_bytes: int = PRUNE_BATCH_BYTES,
) -> int:
    """Prune oldest allsky image sets when their total size exceeds the limit."""
    if max_bytes <= 0 or batch_bytes <= 0:
        raise ValueError("Image size limit and prune batch must be positive")
    if not image_dir.is_dir():
        return 0

    image_sets: dict[str, list[tuple[Path, int, int]]] = defaultdict(list)
    total_bytes = 0
    for path in image_dir.iterdir():
        if not path.name.startswith("allsky-") or path.suffix.lower() not in MANAGED_SUFFIXES:
            continue
        try:
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not path.is_file() or path.is_symlink():
            continue
        total_bytes += info.st_size
        image_sets[path.stem].append((path, info.st_size, info.st_mtime_ns))

    if total_bytes <= max_bytes:
        return 0

    reclaimed = 0
    ordered_sets = sorted(
        image_sets.values(),
        key=lambda entries: min(entry[2] for entry in entries),
    )
    for image_set in ordered_sets:
        for path, size, _mtime in image_set:
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            reclaimed += size
        if reclaimed >= batch_bytes:
            break

    logger.info(
        "Pruned %.1f MiB of oldest all-sky images from %s (limit %.2f GB)",
        reclaimed / (1024 * 1024),
        image_dir,
        max_bytes / 1_000_000_000,
    )
    return reclaimed
