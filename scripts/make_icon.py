"""Generate assets/food_bot.ico — a bowl-of-noodles app icon.

Pure stdlib: rasterizes RGBA by hand, encodes PNG via zlib, and wraps it in an
ICO container (Vista+ allows a PNG payload inside .ico, so no BMP encoding).
Run once; the .ico is committed. Re-run only to change the artwork.
"""

import math
import struct
import zlib
from pathlib import Path

SIZE = 256
OUT = Path(__file__).parent.parent / "assets" / "food_bot.ico"

BG = (34, 39, 46)          # slate, reads on light and dark taskbars
BOWL = (233, 84, 32)       # warm orange
BOWL_DARK = (188, 62, 20)  # foot of the bowl
BROTH = (247, 201, 72)     # amber
STEAM = (226, 232, 240)


def _over(dst, src, a):
    """Alpha-composite src over dst at coverage a (0..1)."""
    return tuple(round(s * a + d * (1 - a)) for s, d in zip(src, dst))


def _coverage(d, edge, soft=1.2):
    """1 inside, 0 outside, antialiased across `soft` px at the boundary."""
    return max(0.0, min(1.0, (edge - d) / soft + 0.5))


def pixel(x, y):
    cx = x + 0.5
    cy = y + 0.5

    # Rounded-square background.
    r = 48
    dx = max(abs(cx - SIZE / 2) - (SIZE / 2 - r), 0)
    dy = max(abs(cy - SIZE / 2) - (SIZE / 2 - r), 0)
    bg_a = _coverage(math.hypot(dx, dy), r)
    if bg_a <= 0:
        return (0, 0, 0, 0)

    rgb = BG

    # Steam: three sine ribbons rising above the bowl.
    for i, sx in enumerate((92, 128, 164)):
        phase = i * 1.1
        wave = sx + 9 * math.sin((cy - 40) / 15.0 + phase)
        if 44 < cy < 104:
            fade = min(1.0, (cy - 44) / 26.0)
            a = _coverage(abs(cx - wave), 3.4) * 0.72 * fade
            if a > 0:
                rgb = _over(rgb, STEAM, a)

    # Broth: ellipse forming the bowl's rim.
    bowl_cx, rim_y = 128.0, 132.0
    ed = math.hypot((cx - bowl_cx) / 74.0, (cy - rim_y) / 15.0)
    broth_a = _coverage(ed * 74.0, 74.0, soft=1.6)
    if broth_a > 0 and cy <= rim_y + 4:
        rgb = _over(rgb, BROTH, broth_a)

    # Bowl body: lower half of a circle, clipped to below the rim.
    bd = math.hypot(cx - bowl_cx, cy - rim_y)
    body_a = _coverage(bd, 76.0, soft=1.6) if cy >= rim_y else 0.0
    if body_a > 0:
        shade = BOWL_DARK if cy > rim_y + 46 else BOWL
        rgb = _over(rgb, shade, body_a)

    return (*rgb, round(255 * bg_a))


def render_png() -> bytes:
    raw = bytearray()
    for y in range(SIZE):
        raw.append(0)  # PNG filter type 0 (None) per scanline
        for x in range(SIZE):
            raw += bytes(pixel(x, y))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def main() -> None:
    png = render_png()
    # ICONDIR(6) + one ICONDIRENTRY(16); width/height 0 means 256.
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_bytes(header + entry + png)
    print(f"wrote {OUT} ({len(png) + 22} bytes)")


if __name__ == "__main__":
    main()
