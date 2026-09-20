#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成插件图标（icons/zeekr_checkin.png）——纯标准库，不依赖 Pillow。

深色圆角底 + 金色闪电，256×256。改颜色/尺寸：编辑下面的常量后重跑本脚本。
"""

import pathlib
import struct
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "icons" / "zeekr_checkin.png"
SIZE = 256
BG = (14, 27, 44, 255)          # 深海军蓝
BG_INNER = (23, 44, 70, 255)    # 内圈稍亮
BOLT = (245, 197, 24, 255)      # 极氪金
BOLT_EDGE = (255, 226, 122, 255)
RADIUS = 56
SS = 3  # 每个像素的超采样倍率（抗锯齿）


def in_rounded_rect(x, y, size, radius, inset=0.0):
    """点是否落在（可内缩的）圆角矩形里。"""
    left, top = inset, inset
    right, bottom = size - inset, size - inset
    if x < left or x > right or y < top or y > bottom:
        return False
    cx = min(max(x, left + radius), right - radius)
    cy = min(max(y, top + radius), bottom - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius**2


BOLT_PTS = [(0.62, 0.06), (0.28, 0.56), (0.46, 0.56), (0.38, 0.96), (0.74, 0.42), (0.53, 0.42)]


def in_polygon(px, py, points):
    """射线法判断点在多边形内（坐标 0~1 与 0~1 比较）。"""
    inside = False
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        if (y1 > py) != (y2 > py):
            xin = (x2 - x1) * (py - y1) / (y2 - y1) + x1
            if px < xin:
                inside = not inside
    return inside


def sample(px, py):
    """取一个采样点的颜色（含透明）。"""
    x, y = px / SIZE, py / SIZE
    if not in_rounded_rect(px, py, SIZE, RADIUS):
        return (0, 0, 0, 0)
    if in_polygon(x, y, BOLT_PTS):
        return BOLT_EDGE if min(
            abs(x - bx) + abs(y - by) for bx, by in BOLT_PTS
        ) < 0.02 else BOLT
    if in_rounded_rect(px, py, SIZE, RADIUS, inset=SIZE * 0.075):
        return BG_INNER
    return BG


def render():
    """超采样渲染成 RGBA 行。"""
    rows = []
    step = 1.0 / SS
    for py in range(SIZE):
        row = bytearray()
        for px in range(SIZE):
            r = g = b = a = 0
            for sy in range(SS):
                for sx in range(SS):
                    cr, cg, cb, ca = sample(px + (sx + 0.5) * step, py + (sy + 0.5) * step)
                    r += cr * ca
                    g += cg * ca
                    b += cb * ca
                    a += ca
            if a == 0:
                row += bytes((0, 0, 0, 0))
            else:
                row += bytes((r // a, g // a, b // a, a // (SS * SS)))
        rows.append(bytes(row))
    return rows


def write_png(path: pathlib.Path, rows):
    """把 RGBA 行写成 PNG（标准库 zlib + crc32）。"""
    raw = b"".join(b"\x00" + row for row in rows)
    height = len(rows)
    width = len(rows[0]) // 4

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def main() -> int:
    rows = render()
    write_png(OUT, rows)
    print(f"✅ 已生成 {OUT.relative_to(ROOT)}（{len(OUT.read_bytes())} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
