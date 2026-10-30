"""Generate ``assets/enginesave.ico`` with the standard library only (plan section 17 M11).

A rounded teal square with a white floppy disk, drawn with 4x4 supersampling at 48, 32 and 16
pixels. Each image is a 32-bit BGRA bitmap (bottom-up, alpha channel) followed by the 1-bit AND
mask, inside a plain ICO container. Run from the repository root:

    .venv\\Scripts\\python tools\\make_icon.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

SIZES = (48, 32, 16)
SAMPLES = 4
TEAL = (30, 110, 140)
SHUTTER = (20, 78, 101)
WHITE = (244, 247, 249)
LABEL = (191, 227, 238)
Pixel = tuple[int, int, int, int]


def in_round_rect(
    u: float, v: float, box: tuple[float, float, float, float], radius: float
) -> bool:
    x0, y0, x1, y1 = box
    if not (x0 <= u <= x1 and y0 <= v <= y1):
        return False
    cx = min(max(u, x0 + radius), x1 - radius)
    cy = min(max(v, y0 + radius), y1 - radius)
    return (u - cx) ** 2 + (v - cy) ** 2 <= radius**2


def color_at(u: float, v: float) -> tuple[int, int, int] | None:
    """Colour at a point of the unit square (``None`` = transparent)."""
    if not in_round_rect(u, v, (0.04, 0.04, 0.96, 0.96), 0.2):
        return None
    if not in_round_rect(u, v, (0.24, 0.2, 0.76, 0.8), 0.07):
        return TEAL
    if 0.36 <= u <= 0.64 and v <= 0.42:
        return WHITE if 0.53 <= u <= 0.59 and 0.25 <= v <= 0.37 else SHUTTER
    if 0.32 <= u <= 0.68 and 0.52 <= v <= 0.72:
        return LABEL
    return WHITE


def render(size: int) -> list[list[Pixel]]:
    """Rows top to bottom of straight-alpha RGBA pixels."""
    rows = []
    for py in range(size):
        row: list[Pixel] = []
        for px in range(size):
            red = green = blue = covered = 0
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    u = (px + (sx + 0.5) / SAMPLES) / size
                    v = (py + (sy + 0.5) / SAMPLES) / size
                    color = color_at(u, v)
                    if color is not None:
                        red, green, blue = red + color[0], green + color[1], blue + color[2]
                        covered += 1
            if not covered:
                row.append((0, 0, 0, 0))
                continue
            alpha = round(255 * covered / SAMPLES**2)
            row.append((round(red / covered), round(green / covered), round(blue / covered), alpha))
        rows.append(row)
    return rows


def bitmap(rows: list[list[Pixel]]) -> bytes:
    """BITMAPINFOHEADER + BGRA rows (bottom-up) + AND mask (1 = transparent)."""
    size = len(rows)
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    pixels = b"".join(bytes((b, g, r, a)) for row in reversed(rows) for r, g, b, a in row)
    stride = (size + 31) // 32 * 4
    mask = bytearray()
    for row in reversed(rows):
        bits = bytearray(stride)
        for x, pixel in enumerate(row):
            if pixel[3] == 0:
                bits[x // 8] |= 0x80 >> (x % 8)
        mask += bits
    return header + pixels + bytes(mask)


def icon(images: list[tuple[int, bytes]]) -> bytes:
    directory = struct.pack("<HHH", 0, 1, len(images))
    offset = len(directory) + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        blobs += data
    return directory + entries + blobs


def main() -> int:
    out = Path(__file__).resolve().parent.parent / "assets" / "enginesave.ico"
    out.parent.mkdir(exist_ok=True)
    data = icon([(size, bitmap(render(size))) for size in SIZES])
    out.write_bytes(data)
    print(f"{out} ({len(data)} bytes, sizes {', '.join(map(str, SIZES))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
