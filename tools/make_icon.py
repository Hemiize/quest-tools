"""Builds docs/questtools.ico (VR headset on a purple background) without an imaging library."""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

N = 256


def rounded_rect(x, y, x0, y0, x1, y1, r):
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def pixel(x: float, y: float):
    # background: rounded square with a gradient
    if not rounded_rect(x, y, 8, 8, 248, 248, 52):
        return (0, 0, 0, 0)
    t = (x + y) / (2 * N)
    bg = (int(40 + 30 * t), int(24 + 10 * t), int(70 + 40 * t), 255)
    # headset body
    if rounded_rect(x, y, 40, 84, 216, 176, 38):
        # lenses
        for lx in (92, 164):
            d = math.hypot(x - lx, y - 130)
            if d < 28:
                return (20, 16, 34, 255)
            if d < 33:
                return (225, 200, 255, 255)
        # nose cut-out
        if math.hypot(x - 128, y - 182) < 26:
            return bg
        g = (y - 84) / 92
        return (int(190 - 60 * g), int(140 - 50 * g), 255, 255)
    # head strap
    if 64 <= y <= 76 and 56 <= x <= 200:
        return (150, 110, 230, 255)
    return bg


def png(size: int) -> bytes:
    rows = []
    s = N / size
    for yy in range(size):
        row = bytearray([0])
        for xx in range(size):
            # 2x2 supersampling for smooth edges
            acc = [0, 0, 0, 0]
            for oy in (0.25, 0.75):
                for ox in (0.25, 0.75):
                    p = pixel((xx + ox) * s, (yy + oy) * s)
                    for i in range(4):
                        acc[i] += p[i]
            row += bytes(v // 4 for v in acc)
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def main():
    sizes = [256, 64, 48, 32, 16]
    imgs = [png(sz) for sz in sizes]
    header = struct.pack("<HHH", 0, 1, len(sizes))
    off = 6 + 16 * len(sizes)
    entries = b""
    for sz, data in zip(sizes, imgs):
        entries += struct.pack("<BBBBHHII", sz % 256, sz % 256, 0, 0, 1, 32, len(data), off)
        off += len(data)
    out = Path(__file__).resolve().parent.parent / "docs" / "questtools.ico"
    out.write_bytes(header + entries + b"".join(imgs))
    (out.with_suffix(".png")).write_bytes(imgs[0])
    print(out)


if __name__ == "__main__":
    main()
