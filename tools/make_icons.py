#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成主屏幕 App 图标（PNG，纯标准库）。
满版：apple-touch-icon.png / icon-192.png / icon-512.png
安全区：icon-maskable-512.png（glyph 缩到中心 80%，供 maskable）
用法：python3 make_icons.py [输出目录]
"""
import math
import os
import struct
import sys
import zlib

# 竖向渐变背景（顶部亮蓝 → 底部深蓝黑）
TOP = (52, 120, 248)
BOTTOM = (9, 22, 64)
WHITE = (244, 246, 250)
LENS_DARK = (10, 22, 58)
LENS_BLUE = (96, 158, 255)
LENS_CORE = (196, 222, 255)


def lerp(a, b, t):
    return a + (b - a) * t


def sample(nx, ny, gscale):
    """以单位正方形坐标 (0~1) 采样一个像素点颜色。gscale: glyph 整体缩放。"""
    # 背景：竖向渐变 + 中心偏上径向光晕
    t = ny
    r = lerp(TOP[0], BOTTOM[0], t)
    g = lerp(TOP[1], BOTTOM[1], t)
    b = lerp(TOP[2], BOTTOM[2], t)
    d = math.hypot(nx - 0.42, ny - 0.34) / 0.8
    glow = max(0.85, 1.22 - 0.42 * d)
    r, g, b = r * glow, g * glow, b * glow

    # glyph 坐标以 (0.5, 0.55) 为中心做缩放
    cx, cy = 0.5, 0.555

    def X(v):
        return cx + (v - cx) * gscale

    def Y(v):
        return cy + (v - cy) * gscale

    def inside_rrect(px, py, rc_x, rc_y, hw, hh, rad):
        dx = max(abs(px - rc_x) - (hw - rad), 0.0)
        dy = max(abs(py - rc_y) - (hh - rad), 0.0)
        return math.hypot(dx, dy) <= rad

    def inside_circle(px, py, cc_x, cc_y, rad):
        return math.hypot(px - cc_x, py - cc_y) <= rad

    body_x, body_y = X(cx), Y(cy)
    body_hw, body_hh = 0.30 * gscale, 0.205 * gscale
    bump_hw, bump_hh = 0.105 * gscale, 0.052 * gscale
    # 取景器凸起（叠在机身顶部）
    bump_y = body_y - body_hh + bump_hh * 0.55
    lens_R = 0.155 * gscale
    flash_d = 0.115 * gscale
    flash_R = 0.026 * gscale

    body = (inside_rrect(nx, ny, body_x, body_y, body_hw, body_hh, 0.075 * gscale)
            or inside_rrect(nx, ny, body_x, bump_y, bump_hw, bump_hh, 0.03 * gscale))
    if body:
        r, g, b = WHITE
        # 闪光灯小孔
        fx = body_x + 0.205 * gscale
        fy = body_y - 0.085 * gscale
        if inside_circle(nx, ny, fx, fy, flash_R):
            r, g, b = LENS_DARK
        # 镜头：深底圆 + 蓝色中环 + 亮芯
        dist_l = math.hypot(nx - body_x, ny - body_y)
        if dist_l <= lens_R:
            r, g, b = LENS_DARK
            if lens_R * 0.52 <= dist_l <= lens_R * 0.80:
                r, g, b = LENS_BLUE
            if dist_l <= lens_R * 0.30:
                r, g, b = LENS_CORE
    return (min(255, r), min(255, g), min(255, b))


def render(size, gscale):
    """2×2 超采样渲染。"""
    out = bytearray(size * size * 3)
    step = 1.0 / size
    off = step * 0.25
    for y in range(size):
        ny = y * step
        row = y * size * 3
        for x in range(size):
            nx = x * step
            c0 = sample(nx + off, ny + off, gscale)
            c1 = sample(nx + step - off, ny + off, gscale)
            c2 = sample(nx + off, ny + step - off, gscale)
            c3 = sample(nx + step - off, ny + step - off, gscale)
            i = row + x * 3
            out[i] = int((c0[0] + c1[0] + c2[0] + c3[0]) * 0.25)
            out[i + 1] = int((c0[1] + c1[1] + c2[1] + c3[1]) * 0.25)
            out[i + 2] = int((c0[2] + c1[2] + c2[2] + c3[2]) * 0.25)
    return bytes(out)


def write_png(path, rgb, size):
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    raw = bytearray()
    stride = size * 3
    for y in range(size):
        raw.append(0)  # filter type 0
        raw += rgb[y * stride:(y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else \
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
    os.makedirs(out_dir, exist_ok=True)
    targets = [
        ("apple-touch-icon.png", 180, 1.0),
        ("icon-192.png", 192, 1.0),
        ("icon-512.png", 512, 1.0),
        ("icon-maskable-512.png", 512, 0.78),
    ]
    for name, size, gs in targets:
        data = render(size, gs)
        write_png(os.path.join(out_dir, name), data, size)
        print("生成 %s (%dx%d)" % (name, size, size))


if __name__ == "__main__":
    main()
