"""The app icon, drawn with Pillow so no binary asset has to be stored in the repository.

    python -m perplexify.icon --ico app.ico --icns app.icns --png app.png
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

PAPER = (227, 228, 232, 255)     # the pages: the dark theme's text colour
INK = (22, 23, 27, 255)          # the tile: the dark theme's page colour
RIM = (58, 60, 67, 255)          # a thin edge so the tile still reads on a dark taskbar
LINES = (138, 140, 145, 255)     # lines of text on the pages
ACCENT = (63, 184, 204, 255)     # the prompt: the theme's teal


def make_icon(size: int = 256) -> Image.Image:
    """An open book over a terminal cursor: documentation you can talk to."""
    s = 1024
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((24, 24, s - 24, s - 24), radius=190, fill=INK, outline=RIM, width=16)
    # open book: two pages meeting at the spine
    cx, top, bottom = s // 2, 250, 650
    d.polygon([(cx - 14, top + 40), (cx - 400, top), (cx - 400, bottom - 40), (cx - 14, bottom)], fill=PAPER)
    d.polygon([(cx + 14, top + 40), (cx + 400, top), (cx + 400, bottom - 40), (cx + 14, bottom)], fill=PAPER)
    for i in range(4):  # lines of text
        y = top + 130 + i * 78
        d.rounded_rectangle((cx - 330, y, cx - 90, y + 20), radius=10, fill=LINES)
        d.rounded_rectangle((cx + 90, y, cx + 330, y + 20), radius=10, fill=LINES)
    # the prompt: a caret and a cursor block
    d.polygon([(cx - 300, 760), (cx - 210, 810), (cx - 300, 860)], fill=ACCENT)
    d.rectangle((cx - 150, 830, cx + 70, 872), fill=ACCENT)
    return img.resize((size, size), Image.LANCZOS)


def save_ico(path: Path) -> None:
    sizes = [16, 24, 32, 48, 64, 128, 256]
    make_icon(256).save(path, format="ICO", sizes=[(n, n) for n in sizes])


def save_icns(path: Path) -> None:
    base = make_icon(1024)
    base.save(path, format="ICNS", append_images=[make_icon(n) for n in (512, 256, 128, 64, 32, 16)])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ico")
    ap.add_argument("--icns")
    ap.add_argument("--png")
    a = ap.parse_args()
    if a.ico:
        save_ico(Path(a.ico))
    if a.icns:
        save_icns(Path(a.icns))
    if a.png:
        make_icon(512).save(a.png)


if __name__ == "__main__":
    main()
