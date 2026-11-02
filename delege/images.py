"""``delege check-images``: the proof of a screenshots-like agent — each image readable, large enough, not blank.
Pillow (``pip install delege[images]``) or OpenCV, whichever is installed."""
from __future__ import annotations


def _load(path) -> tuple[int, int, float] | None:
    """(width, height, standard deviation of the pixels) or None when unreadable."""
    try:
        from PIL import Image, ImageStat
        with Image.open(path) as im:
            im = im.convert("L")
            return im.width, im.height, float(ImageStat.Stat(im).stddev[0])
    except ImportError:
        pass
    except Exception:  # noqa: BLE001  (a corrupt or missing file: unreadable)
        return None
    try:
        import cv2
    except ImportError:
        raise SystemExit("check-images needs Pillow (pip install delege[images]) or OpenCV") from None
    img = cv2.imread(str(path))
    if img is None:
        return None
    h, w = img.shape[:2]
    return w, h, float(img.std())


def check_images(paths, min_side: int = 200) -> list[str]:
    """Problems with images: unreadable, smaller than ``min_side`` px on a side, or blank / one colour."""
    out = []
    for p in paths:
        got = _load(p)
        if got is None:
            out.append(f"{p}: unreadable")
            continue
        w, h, std = got
        if min(w, h) < min_side:
            out.append(f"{p}: {w}×{h}, too small")
        if std < 2.0:
            out.append(f"{p}: blank (one colour)")
    return out
