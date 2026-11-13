"""Pure-stdlib fast PNG encoder, BGRA crop/downscale, and thumbnail generator.

Zero third-party dependencies: converts raw Win32/OS BGRA or RGB pixel buffers
into valid PNG files and base64 data URLs using CPython slice operations + zlib.
"""

from __future__ import annotations

import base64
import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type)
    crc = zlib.crc32(data, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def encode_rgb_png(rgb: bytes | bytearray | memoryview, width: int, height: int, *, level: int = 6) -> bytes:
    """Encode packed 24-bit RGB bytes (width * height * 3) into a PNG byte string."""
    if width <= 0 or height <= 0:
        raise ValueError(f"Invalid PNG dimensions: {width}x{height}")
    row_stride = width * 3
    if len(rgb) < row_stride * height:
        raise ValueError("RGB buffer smaller than width * height * 3")

    view = memoryview(rgb)
    # Prepend filter byte 0 (None) to each scanline
    scanlines = bytearray((row_stride + 1) * height)
    dst_stride = row_stride + 1
    for y in range(height):
        src_off = y * row_stride
        dst_off = y * dst_stride
        scanlines[dst_off] = 0
        scanlines[dst_off + 1 : dst_off + dst_stride] = view[src_off : src_off + row_stride]

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit Truecolor RGB
    compressed = zlib.compress(bytes(scanlines), level)
    return (
        PNG_SIGNATURE
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", compressed)
        + _png_chunk(b"IEND", b"")
    )


def bgra_to_rgb(bgra: bytes | bytearray | memoryview, width: int, height: int) -> bytearray:
    """Convert top-down 32-bit BGRA buffer into packed 24-bit RGB in C-speed slice steps."""
    total_px = width * height
    view = memoryview(bgra)[: total_px * 4]
    rgb = bytearray(total_px * 3)
    rgb[0::3] = view[2::4]  # R
    rgb[1::3] = view[1::4]  # G
    rgb[2::3] = view[0::4]  # B
    return rgb


def crop_bgra(
    bgra: bytes | bytearray | memoryview,
    src_w: int,
    src_h: int,
    x: int,
    y: int,
    crop_w: int,
    crop_h: int,
) -> tuple[bytearray, int, int]:
    """Clamp and crop a top-down BGRA buffer to (x, y, crop_w, crop_h)."""
    x0 = max(0, min(src_w - 1, x))
    y0 = max(0, min(src_h - 1, y))
    x1 = max(x0 + 1, min(src_w, x + crop_w))
    y1 = max(y0 + 1, min(src_h, y + crop_h))
    out_w = x1 - x0
    out_h = y1 - y0
    if x0 == 0 and y0 == 0 and out_w == src_w and out_h == src_h:
        return bytearray(memoryview(bgra)[: src_w * src_h * 4]), out_w, out_h

    view = memoryview(bgra)
    src_stride = src_w * 4
    dst_stride = out_w * 4
    out = bytearray(out_w * out_h * 4)
    for row in range(out_h):
        s_off = (y0 + row) * src_stride + x0 * 4
        d_off = row * dst_stride
        out[d_off : d_off + dst_stride] = view[s_off : s_off + dst_stride]
    return out, out_w, out_h


def downscale_bgra(
    bgra: bytes | bytearray | memoryview,
    src_w: int,
    src_h: int,
    max_dim: int,
) -> tuple[bytes | bytearray | memoryview, int, int]:
    """Downscale a top-down BGRA image so max(width, height) <= max_dim."""
    if max_dim <= 0 or (src_w <= max_dim and src_h <= max_dim):
        return bgra, src_w, src_h

    scale = max_dim / float(max(src_w, src_h))
    dst_w = max(1, int(round(src_w * scale)))
    dst_h = max(1, int(round(src_h * scale)))

    # Fast integer stride path when integer decimation is close, else nearest-grid sample
    view = memoryview(bgra)
    src_stride = src_w * 4
    dst_stride = dst_w * 4
    out = bytearray(dst_w * dst_h * 4)

    x_map = [min(src_w - 1, (dx * src_w) // dst_w) * 4 for dx in range(dst_w)]
    for dy in range(dst_h):
        sy = min(src_h - 1, (dy * src_h) // dst_h)
        row_view = view[sy * src_stride : (sy + 1) * src_stride]
        d_row = dy * dst_stride
        for dx, sx4 in enumerate(x_map):
            d_off = d_row + dx * 4
            out[d_off : d_off + 4] = row_view[sx4 : sx4 + 4]
    return out, dst_w, dst_h


def encode_bgra_png(
    bgra: bytes | bytearray | memoryview,
    width: int,
    height: int,
    *,
    max_dim: int = 0,
    level: int = 2,
) -> tuple[bytes, int, int]:
    """Optionally downscale a BGRA buffer and return (png_bytes, out_width, out_height)."""
    buf, out_w, out_h = downscale_bgra(bgra, width, height, max_dim) if max_dim > 0 else (bgra, width, height)
    rgb = bgra_to_rgb(buf, out_w, out_h)
    return encode_rgb_png(rgb, out_w, out_h, level=level), out_w, out_h


def bgra_to_thumbnail_data_url(
    bgra: bytes | bytearray | memoryview,
    width: int,
    height: int,
    *,
    max_dim: int = 320,
) -> str:
    """Create a compact PNG data URL thumbnail from a raw BGRA buffer."""
    png_bytes, _, _ = encode_bgra_png(bgra, width, height, max_dim=max_dim, level=2)
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


def png_bytes_to_data_url(png_bytes: bytes, *, max_bytes: int = 4 * 1024 * 1024) -> str | None:
    if not png_bytes or len(png_bytes) > max_bytes:
        return None
    if not png_bytes.startswith(PNG_SIGNATURE):
        return None
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


def read_png_dimensions(png_bytes: bytes) -> tuple[int, int] | None:
    """Extract (width, height) from a PNG IHDR header."""
    if len(png_bytes) < 24 or not png_bytes.startswith(PNG_SIGNATURE):
        return None
    if png_bytes[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", png_bytes[16:24])
    if w <= 0 or h <= 0:
        return None
    return int(w), int(h)


def buffer_is_blank(bgra: bytes | bytearray | memoryview) -> bool:
    """Heuristic check for an all-black / empty PrintWindow render."""
    n = len(bgra)
    if n < 16:
        return True
    view = memoryview(bgra)
    step = max(4, (n // 4 // 512) * 4)
    for idx in range(0, n - 3, step):
        # Check B, G, R channels for any non-zero pixel
        if view[idx] != 0 or view[idx + 1] != 0 or view[idx + 2] != 0:
            return False
    return True
