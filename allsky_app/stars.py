from __future__ import annotations

import numpy as np


def count_stars(image: np.ndarray) -> int:
    """Estimate stellar sources as isolated local maxima above robust image noise."""
    pixels = np.asarray(image, dtype=np.float32)
    if pixels.ndim != 2 or min(pixels.shape) < 3:
        raise ValueError("Star counting requires a 2-D image at least 3x3 pixels")
    if not np.isfinite(pixels).all():
        raise ValueError("Image contains non-finite pixel values")

    median = float(np.median(pixels))
    noise = float(np.median(np.abs(pixels - median))) * 1.4826
    threshold = median + max(5.0 * noise, 3.0)
    maxima = pixels.copy()
    for row_offset in (-1, 0, 1):
        for column_offset in (-1, 0, 1):
            if row_offset == 0 and column_offset == 0:
                continue
            shifted = np.roll(pixels, (row_offset, column_offset), axis=(0, 1))
            maxima = np.maximum(maxima, shifted)

    peaks = (pixels >= maxima) & (pixels > threshold)
    peaks[[0, -1], :] = False
    peaks[:, [0, -1]] = False
    return int(np.count_nonzero(peaks))
