# -*- coding: utf-8 -*-
"""生成 pre-26 的 mipmap PNG（adaptive icon 只覆盖 v26+）。
纯标准库：zlib/struct 手写 PNG。几何与 res/drawable/ic_launcher_foreground.xml 保持一致。
重新生成：python scripts/make_icon.py
"""
import os
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "..", "android", "app", "src", "main", "res")

ORANGE = (255, 105, 0, 255)      # FF6900
ORANGE_L = (255, 138, 61, 255)   # FF8A3D 顶部亮带
WHITE = (255, 255, 255, 255)
BOLT = (255, 105, 0, 255)
BAND = (255, 217, 179, 255)      # FFD9B3

# 设计坐标 0..108
BODY = (38, 32, 70, 84, 4)       # 电池身
CAP = (50, 24, 58, 32, 2)        # 电池盖
CHARGE_BAND = (42, 36, 66, 39)   # 电量条
BOLT_POLY = [(57, 42), (48, 60), (54.5, 60), (50, 74), (62, 55), (55.5, 55), (61, 42)]
BAND_Y = 40                      # 背景亮带分界


def in_rounded_rect(x, y, r):
    x0, y0, x1, y1, rad = r
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    for cx, cy in ((x0 + rad, y0 + rad), (x1 - rad, y0 + rad),
                   (x0 + rad, y1 - rad), (x1 - rad, y1 - rad)):
        corner_x = min(max(x, min(x0 + rad, x1 - rad)), max(x0 + rad, x1 - rad))
        corner_y = min(max(y, min(y0 + rad, y1 - rad)), max(y0 + rad, y1 - rad))
    dx = x - (x0 + rad if x < x0 + rad else (x1 - rad if x > x1 - rad else x))
    dy = y - (y0 + rad if y < y0 + rad else (y1 - rad if y > y1 - rad else y))
    return (x0 + rad <= x <= x1 - rad or y0 + rad <= y <= y1 - rad
            or dx * dx + dy * dy <= rad * rad)


def in_circle(cx, cy, r, x, y):
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def in_poly(poly, x, y):
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if xin > x:
                inside = not inside
    return inside


def pixel_color(x, y, round_icon):
    # 背景
    if round_icon:
        if not in_circle(54, 54, 54, x, y):
            return (0, 0, 0, 0)
    else:
        if not in_rounded_rect(x, y, (0, 0, 108, 108, 20)):
            return (0, 0, 0, 0)
    c = ORANGE_L if y < BAND_Y else ORANGE
    # 电池
    if in_rounded_rect(x, y, CAP) or in_rounded_rect(x, y, BODY):
        c = WHITE
    if in_rounded_rect(x, y, (*CHARGE_BAND, 0)):
        c = BAND
    if in_poly(BOLT_POLY, x, y):
        c = BOLT
    return c


def render(size, round_icon):
    ss = 3                                   # 超采样
    big = size * ss
    rows = []
    for py in range(big):
        row = bytearray()
        for px in range(big):
            x = (px + 0.5) * 108.0 / big
            y = (py + 0.5) * 108.0 / big
            row += bytes(pixel_color(x, y, round_icon))
        rows.append(row)
    # 盒式降采样
    out = []
    for oy in range(size):
        orow = bytearray()
        for ox in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(ss):
                base = rows[oy * ss + sy]
                for sx in range(ss):
                    i = (ox * ss + sx) * 4
                    for k in range(4):
                        acc[k] += base[i + k]
            orow += bytes(a // (ss * ss) for a in acc)
        out.append(orow)
    return out


def write_png(path, rows):
    h, w = len(rows), len(rows[0]) // 4
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


DENSITIES = {"mipmap-mdpi": 48, "mipmap-hdpi": 72, "mipmap-xhdpi": 96,
             "mipmap-xxhdpi": 144, "mipmap-xxxhdpi": 192}

jobs = []
for d, size in DENSITIES.items():
    os.makedirs(os.path.join(RES, d), exist_ok=True)
    jobs.append((size, False, os.path.join(RES, d, "ic_launcher.png")))
    jobs.append((size, True, os.path.join(RES, d, "ic_launcher_round.png")))

def run(job):
    size, round_icon, path = job
    write_png(path, render(size, round_icon))
    return path

with ThreadPoolExecutor(max_workers=4) as ex:
    for p in ex.map(run, jobs):
        print("wrote", os.path.normpath(p))
