#!/usr/bin/env python3
"""Read text out of a screenshot when the agent's model cannot see images.

`vision_analyze` needs a vision-capable model. When the active provider is text-only
(agy serves stream-json text; deepseek is text-only) a screenshot is a black box —
this CLI gives the operator a text path instead, so witness screenshots can still be
verified instead of guessed.

Models are cached on first run (~100 MB). Thai + English are both loaded because
LINE cards mix Thai labels with English tickers.

Usage
-----
    ~/.venvs/ocr/bin/python scripts/ocr_image.py IMAGE [--region x1,y1,x2,y2]
                                        [--min-conf 0.3] [--json]

Setup (one time, no sudo needed):
    python3 -m venv ~/.venvs/ocr && ~/.venvs/ocr/bin/pip install easyocr
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LANGS = ["th", "en"]


def _parse_region(raw: str) -> tuple[int, int, int, int]:
    parts = [int(float(p)) for p in raw.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("region must be x1,y1,x2,y2")
    x1, y1, x2, y2 = parts
    if x2 <= x1 or y2 <= y1:
        raise argparse.ArgumentTypeError("region needs x2>x1 and y2>y1")
    return x1, y1, x2, y2


def ocr(image_path: str, region: tuple[int, int, int, int] | None = None,
        min_conf: float = 0.3) -> list[dict]:
    import easyocr  # imported late so --help works without the heavy deps
    from PIL import Image

    path = Path(image_path).expanduser()
    if not path.exists():
        raise SystemExit(f"image not found: {path}")

    img = Image.open(path).convert("RGB")
    if region:
        img = img.crop(region)
    # easyocr wants a path or ndarray; ndarray keeps the crop in memory
    import numpy as np

    reader = easyocr.Reader(LANGS, gpu=False, verbose=False)
    hits = reader.readtext(np.array(img), detail=1, paragraph=False)

    rows = []
    for box, text, conf in hits:
        if conf < min_conf or not text.strip():
            continue
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        rows.append({
            "text": text.strip(),
            "conf": round(float(conf), 3),
            "top": int(min(ys)),
            "left": int(min(xs)),
        })
    # reading order: top-to-bottom, then left-to-right (LINE cards stack vertically)
    rows.sort(key=lambda r: (r["top"] // 12, r["left"]))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="OCR a screenshot into text (Thai + English)")
    ap.add_argument("image")
    ap.add_argument("--region", type=_parse_region, default=None,
                    help="crop box x1,y1,x2,y2 in original pixels")
    ap.add_argument("--min-conf", type=float, default=0.3)
    ap.add_argument("--json", action="store_true", help="emit rows as JSON")
    args = ap.parse_args()

    rows = ocr(args.image, args.region, args.min_conf)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        for r in rows:
            print(f"{r['conf']:>5.2f}  {r['text']}")
    return 0 if rows else 2


if __name__ == "__main__":
    sys.exit(main())
