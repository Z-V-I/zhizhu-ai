#!/usr/bin/env python3
"""生成 PWA 图标。纯标准库，不依赖 Pillow —— 服务器上也能跑。

图形：深色圆角底 + 一个白色的 U（UMF 的 U，也是 "you" 的 U）。
不出彩，但足够干净，而且不会因为找不到字体而崩。

用法：
    python scripts/make-icons.py
产物：
    deploy/web/icons/icon-192.png
    deploy/web/icons/icon-512.png
    deploy/web/icons/icon-maskable-512.png   （Android 自适应图标用，图形缩到安全区）
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "deploy" / "web" / "icons"

BG_TOP = (11, 15, 23)      # #0b0f17
BG_BOTTOM = (23, 35, 58)   # #17233a
FG = (232, 238, 252)       # #e8eefc

SS = 3  # 超采样倍数，用来做抗锯齿


def _lerp(a: int, b: int, k: float) -> int:
    return int(a + (b - a) * k)


def _rounded_contains(x: float, y: float, size: float, r: float) -> bool:
    if r <= 0:
        return True
    cx = min(max(x, r), size - r)
    cy = min(max(y, r), size - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _inside_u(x: float, y: float, cx: float, cy: float, w: float, h: float, t: float) -> bool:
    left, right = cx - w / 2, cx + w / 2
    top, bottom = cy - h / 2, cy + h / 2
    arc_y = bottom - w / 2

    if top <= y <= arc_y:
        if left <= x <= left + t or right - t <= x <= right:
            return True

    dx, dy = x - cx, y - arc_y
    if dy >= 0:
        d = (dx * dx + dy * dy) ** 0.5
        if w / 2 - t <= d <= w / 2:
            return True
    return False


def render(size: int, *, maskable: bool = False) -> list[bytes]:
    canvas = size * SS
    radius = 0.0 if maskable else size * 0.235 * SS
    # maskable 的图形要落在中心 80% 的安全区里，否则被系统裁掉
    scale = 0.78 if maskable else 1.0
    cx = canvas / 2
    # 视觉重心比几何中心高一点才显得稳：U 的开口在下方，重心天然偏下
    cy = canvas / 2 - canvas * 0.032
    w = canvas * 0.42 * scale
    h = canvas * 0.46 * scale
    t = canvas * 0.088 * scale

    rows: list[bytes] = []
    inv = 1.0 / (SS * SS)
    for py in range(size):
        row = bytearray()
        for px in range(size):
            bg_hits = 0
            fg_hits = 0
            for sy in range(SS):
                fy = (py * SS + sy + 0.5) / canvas
                for sx in range(SS):
                    fx = (px * SS + sx + 0.5) / canvas
                    X, Y = fx * canvas, fy * canvas
                    if not _rounded_contains(X, Y, canvas, radius):
                        continue
                    bg_hits += 1
                    if _inside_u(X, Y, cx, cy, w, h, t):
                        fg_hits += 1
            if bg_hits == 0:
                row += bytes((0, 0, 0, 0))
                continue
            k = (py + 0.5) / size
            bg = tuple(_lerp(BG_TOP[i], BG_BOTTOM[i], k) for i in range(3))
            cover = fg_hits / bg_hits
            rgb = tuple(int(bg[i] + (FG[i] - bg[i]) * cover) for i in range(3))
            alpha = int(255 * bg_hits * inv)
            row += bytes((*rgb, alpha))
        rows.append(bytes(row))
    return rows


def write_png(path: Path, size: int, rows: list[bytes]) -> None:
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)
    print(f"{path.relative_to(OUT.parent.parent.parent)}  {size}x{size}  {len(png)/1024:.1f} KB")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    write_png(OUT / "icon-192.png", 192, render(192))
    write_png(OUT / "icon-512.png", 512, render(512))
    write_png(OUT / "icon-maskable-512.png", 512, render(512, maskable=True))


if __name__ == "__main__":
    main()
