from __future__ import annotations

import numpy as np
from astropy.stats import SigmaClip
from photutils.background import Background2D, MedianBackground
from photutils.detection import DAOStarFinder


def find_sources(image: np.ndarray) -> list[tuple[float, float]]:
    """Return point-source centroids as (x, y) image coordinates."""
    pixels = np.asarray(image, dtype=np.float32)
    if pixels.ndim != 2 or min(pixels.shape) < 3:
        raise ValueError("Star counting requires a 2-D image at least 3x3 pixels")
    if not np.isfinite(pixels).all():
        raise ValueError("Image contains non-finite pixel values")
    if np.ptp(pixels) == 0:
        return []

    box_size = min(64, max(8, min(pixels.shape) // 30))
    background = Background2D(
        pixels,
        box_size=box_size,
        filter_size=(3, 3),
        sigma_clip=SigmaClip(sigma=3.0),
        bkg_estimator=MedianBackground(),
    )
    finder = DAOStarFinder(
        fwhm=3.0,
        threshold=5.0 * background.background_rms,
        sharpness_range=(0.4, 0.8),
        roundness_range=(-0.3, 0.3),
        exclude_border=True,
    )
    sources = finder(pixels - background.background)
    if sources is None:
        return []
    return [
        (float(source["x_centroid"]), float(source["y_centroid"]))
        for source in sources
    ]


def count_stars(image: np.ndarray) -> int:
    return len(find_sources(image))
