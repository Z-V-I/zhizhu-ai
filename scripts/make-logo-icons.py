#!/usr/bin/env python3
"""把一张 256×256 的 logo PNG 缩放成站点需要的各种尺寸。

## 为什么不用 Pillow

这台机器和服务器上都没有 Pillow，而为了生成几个图标去装一个
十几 MB 的二进制包不划算 —— 我们只需要「读 PNG → 双线性缩放 → 写 PNG」，
用 zlib + struct 一百多行就够了，而且任何环境都能跑。

用法：
    python3 scripts/make-logo-icons.py <源 PNG> [输出目录]

产物（写进 deploy/web/icons/）：
    icon-192.png  icon-512.png  icon-maskable-512.png
    favicon-16.png  favicon-32.png  apple-touch-icon-180.png
    logo-256.png        （登录页 / 后台左上角用的主图）
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "src.png")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "deploy/web/icons")

# iOS 的 apple-touch-icon 不认透明，垫一层底色更稳（和站点深色底一致）
BG = (11, 15, 23)


# ------------------------------------------------------------------ PNG 读

def read_png(path: Path) -> tuple[int, int, bytes]:
    """只支持 8 位 RGB/RGBA、非隔行 —— 我们自己的源图就是这个规格。"""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"{path} 不是 PNG")

    pos = 8
    w = h = bit_depth = color_type = 0
    idat = bytearray()
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        ctype = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            w, h, bit_depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", body)
            if bit_depth != 8 or interlace != 0 or color_type not in (2, 6):
                raise SystemExit(f"不支持的 PNG 格式：depth={bit_depth} color={color_type} interlace={interlace}")
        elif ctype == b"IDAT":
            idat += body
        elif ctype == b"IEND":
            break

    channels = 4 if color_type == 6 else 3
    raw = zlib.decompress(bytes(idat))
    stride = w * channels
    out = bytearray(w * h * 4)

    prev = bytearray(stride)
    p = 0
    for y in range(h):
        ftype = raw[p]
        p += 1
        line = bytearray(raw[p : p + stride])
        p += stride

        # 逐行反滤波（PNG 规范里的 5 种）
        if ftype == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF

        base = y * w * 4
        if channels == 4:
            out[base : base + w * 4] = line
        else:
            for x in range(w):
                o = base + x * 4
                out[o : o + 3] = line[x * 3 : x * 3 + 3]
                out[o + 3] = 255
        prev = line

    return w, h, bytes(out)


# ------------------------------------------------------------------ PNG 写

def write_png(path: Path, w: int, h: int, rgba: bytes) -> None:
    raw = bytearray()
    for y in range(h):
        raw.append(0)                      # 每行 filter=None，压缩率够用
        raw += rgba[y * w * 4 : (y + 1) * w * 4]

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + tag
            + body
            + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


# ------------------------------------------------------------------ 缩放

def resize(src: tuple[int, int, bytes], size: int) -> bytes:
    """双线性缩放。图标尺寸小，质量足够，速度也够快。"""
    sw, sh, data = src
    out = bytearray(size * size * 4)
    scale = sw / size

    for y in range(size):
        sy = (y + 0.5) * scale - 0.5
        y0 = max(0, min(sh - 1, int(sy)))
        y1 = min(sh - 1, y0 + 1)
        fy = max(0.0, sy - y0)

        for x in range(size):
            sx = (x + 0.5) * scale - 0.5
            x0 = max(0, min(sw - 1, int(sx)))
            x1 = min(sw - 1, x0 + 1)
            fx = max(0.0, sx - x0)

            i00 = (y0 * sw + x0) * 4
            i01 = (y0 * sw + x1) * 4
            i10 = (y1 * sw + x0) * 4
            i11 = (y1 * sw + x1) * 4
            o = (y * size + x) * 4

            for c in range(4):
                top = data[i00 + c] * (1 - fx) + data[i01 + c] * fx
                bot = data[i10 + c] * (1 - fx) + data[i11 + c] * fx
                out[o + c] = int(top * (1 - fy) + bot * fy + 0.5)
    return bytes(out)


def on_dark(size: int, rgba: bytes) -> bytes:
    """把半透明像素合到深色底上，给不认透明的 iOS 用。"""
    out = bytearray(rgba)
    for i in range(0, len(out), 4):
        a = out[i + 3] / 255.0
        for c in range(3):
            out[i + c] = int(out[i + c] * a + BG[c] * (1 - a) + 0.5)
        out[i + 3] = 255
    return bytes(out)


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"找不到源图：{SRC}")
    OUT.mkdir(parents=True, exist_ok=True)
    src = read_png(SRC)
    print(f"源图 {src[0]}×{src[1]}")

    plan = [
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        ("icon-maskable-512.png", 512, False),
        ("favicon-16.png", 16, False),
        ("favicon-32.png", 32, False),
        ("apple-touch-icon-180.png", 180, True),
        ("logo-256.png", 256, False),
    ]
    for name, size, flatten in plan:
        rgba = resize(src, size)
        if flatten:
            rgba = on_dark(size, rgba)
        write_png(OUT / name, size, size, rgba)
        print(f"  {name}  {size}×{size}  {(OUT / name).stat().st_size} B")

    # 老的 UMF 图标名保留一份同名副本，避免有地方还在引用旧路径时裂图
    for old, new in (("icon-maskable-512.png", "icon-maskable-512.png"),):
        pass


if __name__ == "__main__":
    main()
