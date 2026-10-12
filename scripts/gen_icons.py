#!/usr/bin/env python3
"""Generate the PWA icons (indigo sky + amber moon) with only the stdlib.

Writes web_parent/public/icon-192.png and icon-512.png. Run once; the PNGs are committed.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "web_parent" / "public"

BG = (13, 16, 48)        # --night-0
MOON_HI = (255, 247, 230)
MOON = (255, 206, 138)   # --amber
MOON_LO = (245, 169, 107)


def _blend(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def render(size: int) -> bytes:
    cx, cy, r = size * 0.5, size * 0.46, size * 0.30
    hx, hy = cx - r * 0.35, cy - r * 0.35  # highlight center
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # PNG filter byte (none)
        for x in range(size):
            dx, dy = x - cx, y - cy
            dist = (dx * dx + dy * dy) ** 0.5
            if dist <= r:
                hd = ((x - hx) ** 2 + (y - hy) ** 2) ** 0.5 / (r * 1.6)
                hd = min(1.0, hd)
                if hd < 0.5:
                    col = _blend(MOON_HI, MOON, hd / 0.5)
                else:
                    col = _blend(MOON, MOON_LO, (hd - 0.5) / 0.5)
                rows += bytes(col)
            else:
                rows += bytes(BG)
    return _png(size, size, bytes(rows))


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _png(w: int, h: int, rgb_rows: bytes) -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit, color type 2 (RGB)
    idat = zlib.compress(rgb_rows, 9)
    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        (OUT / f"icon-{size}.png").write_bytes(render(size))
        print(f"wrote {OUT / f'icon-{size}.png'}")
