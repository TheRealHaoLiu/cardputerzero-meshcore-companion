#!/usr/bin/env python3
"""Draw assets/icon.png (100x100, the launcher's tile size): three mesh nodes and links."""
from pathlib import Path

from PIL import Image, ImageDraw

S = 400  # draw large, scale down for smooth edges
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((8, 8, S - 8, S - 8), 80, fill=(18, 22, 30, 255))
nodes = [(120, 280), (280, 280), (200, 130)]
for a in nodes:
    for b in nodes:
        if a < b:
            d.line((a, b), fill=(255, 176, 46, 255), width=22)
for x, y in nodes:
    d.ellipse((x - 40, y - 40, x + 40, y + 40), fill=(255, 176, 46, 255))
    d.ellipse((x - 18, y - 18, x + 18, y + 18), fill=(18, 22, 30, 255))
out = Path(__file__).resolve().parent.parent / "assets" / "icon.png"
img.resize((100, 100), Image.LANCZOS).save(out)
print(out)
