"""Manual rectangular pixel masks ("人工像素遮罩").

The tag-level engine never touches pixel data; this module adds the one
manual channel the anonymisation profile cannot provide: a reviewer draws
rectangles over burned-in identity (name bands, bed labels, faces) on a
rendered thumbnail, and at execution time the corresponding regions are
filled with a photometrically *safe background*:

* ``MONOCHROME2`` / ``RGB`` / ``YBR_FULL`` / ``PALETTE COLOR`` -> minimum
  stored value (black);
* ``MONOCHROME1`` -> maximum storable value for ``BitsAllocated`` (black in
  the inverted photometric interpretation).

Only **uncompressed transfer syntaxes** are modified in place; compressed
pixel data is encapsulated and cannot be edited without a decoder/encoder,
so such files raise :class:`PixelMaskError` with the file index/path so the
caller can block packaging and show a locatable message.  Multi-frame
datasets keep their exact dimensions, window/level attributes and
``PhotometricInterpretation`` -- only ``PixelData`` bytes change.

The module also renders 8-bit PNG thumbnails (pure standard library: zlib
+ struct) and red-overlay mask previews; no third-party imaging library is
required.
"""
from __future__ import annotations

import base64
import struct
import zlib
from dataclasses import dataclass

import pydicom
from pydicom.dataset import Dataset

# ---------------------------------------------------------------------------
# Transfer syntaxes whose native pixel data we can edit / render
# ---------------------------------------------------------------------------

_IMPLICIT_VR_LE = "1.2.840.10008.1.2"
_EXPLICIT_VR_LE = "1.2.840.10008.1.2.1"
_DEFLATED_EXPLICIT_VR_LE = "1.2.840.10008.1.2.1.99"
_EXPLICIT_VR_BE = "1.2.840.10008.1.2.2"

# Everything else (JPEG/JPEG-LS/JPEG2000 family ...) is encapsulated.
UNCOMPRESSED_TRANSFER_SYNTAXES = {
    _IMPLICIT_VR_LE,
    _EXPLICIT_VR_LE,
    _DEFLATED_EXPLICIT_VR_LE,
    # Retired, but still encountered in legacy archives:
    _EXPLICIT_VR_BE,
}

# Photometric interpretations for which index 0 is a safe (black) fill.
_MIN_ZERO_PMS = {"MONOCHROME2", "RGB", "YBR_FULL", "YBR_RCT",
                 "PALETTE COLOR", "ARGB", "CMYK"}
# Rendering of every supported flavour except RGB/MONOCHROME* would be
# guesswork; they are still *maskable* (fill byte is unambiguous), but the
# thumbnail endpoint reports a locatable error instead of a wrong image.
RENDERABLE_PMS = {"MONOCHROME1", "MONOCHROME2", "RGB", "YBR_FULL",
                  "PALETTE COLOR"}

MAX_THUMBNAIL_EDGE = 1024


class PixelMaskError(Exception):
    """A mask cannot be rendered/applied for a locatable file.

    ``file_index`` / ``source_path`` let the UI point at the offending
    upload; ``code`` distinguishes transfer-syntax problems from decode /
    geometry problems.
    """

    def __init__(self, message: str, *, file_index: int | None = None,
                 source_path: str | None = None,
                 code: str = "pixel_mask_error"):
        super().__init__(message)
        self.file_index = file_index
        self.source_path = source_path
        self.code = code

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "file_index": self.file_index,
            "source_path": self.source_path,
            "message": str(self),
        }


@dataclass(frozen=True)
class Rect:
    """Rectangle in **original pixel coordinates** (column,row inclusive?).

    Bounds are half-open pixel ranges ``[x, x+width) x [y, y+height)``.
    """

    x: int
    y: int
    width: int
    height: int

    def clamp(self, cols: int, rows: int) -> "Rect | None":
        x0 = max(0, self.x)
        y0 = max(0, self.y)
        x1 = min(cols, self.x + self.width)
        y1 = min(rows, self.y + self.height)
        if x1 <= x0 or y1 <= y0:
            return None
        return Rect(x0, y0, x1 - x0, y1 - y0)


@dataclass(frozen=True)
class MaskGroup:
    """One file's rectangles over an inclusive frame range."""

    file_index: int
    rectangles: tuple[Rect, ...]
    frame_start: int = 0          # 0-based, inclusive
    frame_end: int | None = None  # 0-based, inclusive; None -> last frame


# ---------------------------------------------------------------------------
# Dataset inspection
# ---------------------------------------------------------------------------

def _transfer_syntax(ds: Dataset) -> str:
    meta = getattr(ds, "file_meta", None)
    ts = getattr(meta, "TransferSyntaxUID", None) if meta is not None else None
    return str(ts) if ts else _IMPLICIT_VR_LE


@dataclass
class PixelLayout:
    rows: int
    columns: int
    frames: int
    samples_per_pixel: int
    bits_allocated: int
    bits_stored: int
    pixel_representation: int
    planar_configuration: int
    photometric: str
    big_endian: bool
    frame_bytes: int
    frame_samples: int

    @property
    def fill_value(self) -> int:
        if self.samples_per_pixel == 1 and self.photometric == "MONOCHROME1":
            # Black in MONOCHROME1 is the maximum *stored* value range.
            return (1 << self.bits_allocated) - 1
        return 0

    @property
    def sample_bytes(self) -> int:
        return self.bits_allocated // 8


def _int_attr(ds: Dataset, keyword: str, default=None):
    value = getattr(ds, keyword, None)
    if value is None:
        return default
    try:
        # DS/IS elements arrive as str/pydicom MV; take the first component.
        if isinstance(value, (list, pydicom.multival.MultiValue)):
            value = value[0]
        return int(value)
    except (TypeError, ValueError):
        return default


def inspect_dataset(ds: Dataset) -> PixelLayout:
    """Validate that pixel masks are possible and describe the pixel layout."""
    ts = _transfer_syntax(ds)
    if ts not in UNCOMPRESSED_TRANSFER_SYNTAXES:
        raise PixelMaskError(
            f"该影像使用压缩传输语法（{ts}），本工具不内置像素编解码器，"
            "无法在保证图像可解码的前提下修改像素；请先用 DICOM 工具"
            "（如 dcmdjpeg）解压为未压缩传输语法后再上传，或跳过此文件。",
            code="compressed_transfer_syntax")
    # Deflated little-endian is uncompressed *after* pydicom inflates it.
    if not hasattr(ds, "PixelData"):
        raise PixelMaskError("该 DICOM 文件不含 PixelData（像素数据），"
                             "无法绘制像素遮罩。", code="no_pixel_data")

    rows = _int_attr(ds, "Rows")
    columns = _int_attr(ds, "Columns")
    if not rows or not columns:
        raise PixelMaskError("缺少有效的 Rows/Columns，无法定位像素区域。",
                             code="invalid_geometry")

    spp = _int_attr(ds, "SamplesPerPixel", 1) or 1
    ba = _int_attr(ds, "BitsAllocated")
    bs = _int_attr(ds, "BitsStored", ba if ba is not None else None)
    pr = _int_attr(ds, "PixelRepresentation", 0) or 0
    pc = _int_attr(ds, "PlanarConfiguration", 0) or 0
    photometric = str(getattr(ds, "PhotometricInterpretation", "")
                      or "").strip().upper()
    if not photometric:
        raise PixelMaskError("缺少 PhotometricInterpretation，无法确定"
                             "安全背景色。", code="invalid_photometric")

    if ba not in (8, 16):
        raise PixelMaskError(
            f"仅支持 8/16 位像素的遮罩（当前 BitsAllocated={ba}）。",
            code="unsupported_bits")
    if spp not in (1, 3):
        raise PixelMaskError(
            f"仅支持单色或 RGB 像素（SamplesPerPixel={spp}）。",
            code="unsupported_samples")
    if spp == 1 and photometric not in ("MONOCHROME1", "MONOCHROME2",
                                        "PALETTE COLOR"):
        raise PixelMaskError(
            f"不支持的单色 PhotometricInterpretation：{photometric}。",
            code="unsupported_photometric")
    if spp == 3 and photometric not in ("RGB", "YBR_FULL", "YBR_RCT"):
        raise PixelMaskError(
            f"不支持的彩色 PhotometricInterpretation：{photometric}。",
            code="unsupported_photometric")

    n_frames = _int_attr(ds, "NumberOfFrames", 1) or 1
    frame_samples = rows * columns * spp
    frame_bytes = frame_samples * (ba // 8)
    expected = frame_bytes * n_frames
    actual = len(ds.PixelData)
    if actual < expected:
        raise PixelMaskError(
            f"像素数据无法解码：期望至少 {expected} 字节，实际 {actual} 字节"
            "（PixelData 长度与 Rows×Columns×帧数不符）。",
            code="pixel_data_truncated")

    return PixelLayout(
        rows=rows, columns=columns, frames=n_frames,
        samples_per_pixel=spp, bits_allocated=ba, bits_stored=bs or ba,
        pixel_representation=pr, planar_configuration=pc,
        photometric=photometric, big_endian=(ts == _EXPLICIT_VR_BE),
        frame_bytes=frame_bytes, frame_samples=frame_samples)


def load_for_mask(path) -> tuple[Dataset, PixelLayout]:
    """Read and validate a source file, attaching file location to errors."""
    try:
        ds = pydicom.dcmread(path, force=True)
    except Exception as exc:  # noqa: BLE001 - surfaced as locatable error
        raise PixelMaskError(f"无法解码该 DICOM 文件：{exc}",
                             code="decode_failed") from exc
    return ds, inspect_dataset(ds)


# ---------------------------------------------------------------------------
# Sample reading
# ---------------------------------------------------------------------------

def _read_sample(buf: bytes, offset: int, layout: PixelLayout) -> int:
    n = layout.sample_bytes
    if layout.big_endian:
        raw = int.from_bytes(buf[offset:offset + n], "big", signed=False)
    else:
        raw = int.from_bytes(buf[offset:offset + n], "little", signed=False)
    if layout.pixel_representation == 1 and layout.bits_allocated > 1:
        sign_bit = 1 << (layout.bits_allocated - 1)
        if raw & sign_bit:
            raw -= 1 << layout.bits_allocated
    return raw


def _fill_bytes(layout: PixelLayout) -> bytes:
    value = layout.fill_value
    if layout.pixel_representation == 1 and value >= (1 << (
            layout.bits_allocated - 1)):
        value -= 1 << layout.bits_allocated
    value &= (1 << layout.bits_allocated) - 1
    return value.to_bytes(layout.sample_bytes,
                          "big" if layout.big_endian else "little")


# ---------------------------------------------------------------------------
# Mask application
# ---------------------------------------------------------------------------

def apply_masks(ds: Dataset, layout: PixelLayout, groups: list[MaskGroup]
                ) -> list[dict]:
    """Fill rectangle regions in-place; return report-ready records.

    Each record intentionally contains only the file path, frame range and
    rectangle count -- never coordinates (which could map to anatomy).
    """
    if not groups:
        return []

    buf = bytearray(ds.PixelData)
    n_frames = layout.frames
    rows, cols = layout.rows, layout.columns
    spp = layout.samples_per_pixel
    sample_bytes = layout.sample_bytes
    fill = _fill_bytes(layout)
    records: list[dict] = []

    for group in groups:
        if not 0 <= group.frame_start < n_frames:
            raise PixelMaskError(
                f"帧范围越界：起始帧 {group.frame_start + 1} 超出总帧数 "
                f"{n_frames}（帧号从 1 开始）。",
                code="frame_out_of_range")
        raw_end = (n_frames - 1 if group.frame_end is None
                   else group.frame_end)
        if not group.frame_start <= raw_end < n_frames:
            raise PixelMaskError(
                f"帧范围越界：结束帧 {raw_end + 1} 超出总帧数 {n_frames}"
                "（帧号从 1 开始）。", code="frame_out_of_range")

        clipped: list[Rect] = []
        for rect in group.rectangles:
            if rect.x < 0 or rect.y < 0 or rect.width <= 0 \
                    or rect.height <= 0:
                raise PixelMaskError(
                    "遮罩矩形必须为正宽高、且位于影像范围内（"
                    f"收到 x={rect.x}, y={rect.y}, "
                    f"w={rect.width}, h={rect.height}）。",
                    code="rect_out_of_range")
            c = rect.clamp(cols, rows)
            if c is None:
                raise PixelMaskError(
                    f"遮罩矩形（x={rect.x}, y={rect.y}, w={rect.width}, "
                    f"h={rect.height}）完全位于影像 "
                    f"{cols}×{rows} 之外。",
                    code="rect_out_of_range")
            if c != rect:
                raise PixelMaskError(
                    f"遮罩矩形（x={rect.x}, y={rect.y}, w={rect.width}, "
                    f"h={rect.height}）超出影像边界 {cols}×{rows}。",
                    code="rect_out_of_range")
            clipped.append(c)

        if not clipped:
            continue

        for frame_no in range(group.frame_start, raw_end + 1):
            base = frame_no * layout.frame_bytes
            if spp == 1 or layout.planar_configuration == 0:
                # MONOCHROME/PALETTE, or color-by-pixel RGB: whole samples.
                row_span = cols * spp * sample_bytes
                for r in clipped:
                    line = fill * (r.width * spp)
                    for row in range(r.y, r.y + r.height):
                        start = base + row * row_span \
                            + r.x * spp * sample_bytes
                        buf[start:start + len(line)] = line
            else:
                # Color-by-plane: fill the same window in each of the 3
                # planes with that plane's (identical, zero) fill byte.
                plane_bytes = rows * cols * sample_bytes
                row_span = cols * sample_bytes
                for r in clipped:
                    line = fill * r.width
                    for plane in range(spp):
                        p_base = base + plane * plane_bytes
                        for row in range(r.y, r.y + r.height):
                            start = p_base + row * row_span \
                                + r.x * sample_bytes
                            buf[start:start + len(line)] = line

        records.append({
            "frame_start": group.frame_start + 1,   # 1-based for the report
            "frame_end": raw_end + 1,
            "frame_count": raw_end - group.frame_start + 1,
            "rectangle_count": len(clipped),
        })

    # Keep PixelData uncompressed: it must not become encapsulated.
    pd_elem = ds["PixelData"]
    pd_elem.value = bytes(buf)
    if getattr(pd_elem, "is_undefined_length", False):
        pd_elem.is_undefined_length = False
    return records


# ---------------------------------------------------------------------------
# Rendering: frame -> 8-bit RGB raster
# ---------------------------------------------------------------------------

def _first_number(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (list, pydicom.multival.MultiValue)):
        value = value[0] if value else None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iter_samples(buf: bytes, layout: PixelLayout, base: int, count: int):
    sb = layout.sample_bytes
    signed = layout.pixel_representation == 1 and layout.bits_allocated > 1
    for i in range(count):
        offset = base + i * sb
        if layout.big_endian:
            raw = int.from_bytes(buf[offset:offset + sb], "big")
        else:
            raw = int.from_bytes(buf[offset:offset + sb], "little")
        if signed and raw & (1 << (layout.bits_allocated - 1)):
            raw -= 1 << layout.bits_allocated
        yield raw


def _grayscale_frame(buf: bytes, layout: PixelLayout, ds: Dataset,
                     frame_no: int) -> bytearray:
    """Window/level (or min-max) grayscale samples into a 0..255 raster."""
    n = layout.rows * layout.columns
    base = frame_no * layout.frame_bytes

    slope = _first_number(getattr(ds, "RescaleSlope", 1))
    intercept = _first_number(getattr(ds, "RescaleIntercept", 0))
    if slope is None:
        slope = 1.0
    if intercept is None:
        intercept = 0.0
    window = _first_number(getattr(ds, "WindowWidth", None))
    level = _first_number(getattr(ds, "WindowCenter", None))

    if window is not None and level is not None and window > 0:
        lo = level - window / 2.0
        scale = 255.0 / window
        out = bytearray(n)
        for i, raw in enumerate(_iter_samples(buf, layout, base, n)):
            g = (raw * slope + intercept - lo) * scale
            out[i] = 0 if g < 0 else 255 if g > 255 else int(g)
    else:
        # Two passes (min/max, then scale) without holding the full frame of
        # Python ints in memory.
        vmin = None
        vmax = None
        for raw in _iter_samples(buf, layout, base, n):
            v = raw * slope + intercept
            if vmin is None or v < vmin:
                vmin = v
            if vmax is None or v > vmax:
                vmax = v
        span = (vmax - vmin) or 1.0
        out = bytearray(n)
        for i, raw in enumerate(_iter_samples(buf, layout, base, n)):
            g = (raw * slope + intercept - vmin) / span * 255.0
            out[i] = 0 if g < 0 else 255 if g > 255 else int(g)

    if layout.photometric == "MONOCHROME1":
        for i in range(n):
            out[i] = 255 - out[i]
    return out


def _palette_component(ds: Dataset, data_attr: str, desc_attr: str):
    """Build an index->8-bit lookup for one PALETTE COLOR component.

    The descriptor is ``(number_of_entries, first_index_mapped, bits)``;
    number of entries 0 means 65536 and bits may be the string ``"x16"``.
    """
    lut = getattr(ds, data_attr, None)
    if lut is None:
        raise PixelMaskError(
            f"PALETTE COLOR 影像缺少 {data_attr} 查找表，无法渲染缩略图。",
            code="palette_lut_missing")
    desc = getattr(ds, desc_attr, None)
    entries, first, bits = 0, 0, 16
    if desc is not None and len(desc) >= 3:
        try:
            entries = int(desc[0])
        except (TypeError, ValueError):
            entries = 0
        try:
            first = int(desc[1])
        except (TypeError, ValueError):
            first = 0
        bits = 16 if str(desc[2]).strip().lower() == "x16" \
            else _safe_int(desc[2], 16)
    if entries <= 0:
        entries = 65536 if bits > 8 else 256
    entry_bytes = max(1, bits // 8)
    if len(lut) < entries * entry_bytes:
        raise PixelMaskError(
            f"{data_attr} 查找表长度与描述符条目数不符，无法渲染。",
            code="palette_lut_bad")

    def _value(index: int) -> int:
        if entry_bytes == 1:
            return int(lut[index])
        raw = int(lut[index * entry_bytes]) \
            | (int(lut[index * entry_bytes + 1]) << 8)
        return raw >> (bits - 8) if bits > 8 else raw

    return _value, entries, first


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _palette_frame(buf: bytes, layout: PixelLayout, ds: Dataset,
                   frame_no: int) -> bytearray:
    r_lut, r_entries, first_r = _palette_component(
        ds, "RedPaletteColorLookupTableData",
        "RedPaletteColorLookupTableDescriptor")
    g_lut, g_entries, _ = _palette_component(
        ds, "GreenPaletteColorLookupTableData",
        "GreenPaletteColorLookupTableDescriptor")
    b_lut, b_entries, _ = _palette_component(
        ds, "BluePaletteColorLookupTableData",
        "BluePaletteColorLookupTableDescriptor")
    n_entries = min(r_entries, g_entries, b_entries)
    base = frame_no * layout.frame_bytes
    n = layout.rows * layout.columns
    rgb = bytearray(n * 3)
    for i in range(n):
        idx = _read_sample(buf, base + i * layout.sample_bytes, layout) \
            - first_r
        if idx < 0:
            idx = 0
        elif idx >= n_entries:
            idx = n_entries - 1
        rgb[i * 3] = r_lut(idx)
        rgb[i * 3 + 1] = g_lut(idx)
        rgb[i * 3 + 2] = b_lut(idx)
    return rgb


def _ybr_to_rgb(y: int, cb: int, cr: int) -> tuple[int, int, int]:
    r = y + 1.402 * (cr - 128)
    g = y - 0.344136 * (cb - 128) - 0.714136 * (cr - 128)
    b = y + 1.772 * (cb - 128)
    return (0 if r < 0 else 255 if r > 255 else int(r),
            0 if g < 0 else 255 if g > 255 else int(g),
            0 if b < 0 else 255 if b > 255 else int(b))


def _color_frame(buf: bytes, layout: PixelLayout, frame_no: int
                 ) -> bytearray:
    base = frame_no * layout.frame_bytes
    n = layout.rows * layout.columns
    rgb = bytearray(n * 3)
    spp = 3
    if layout.bits_allocated == 8:
        if layout.planar_configuration == 0:
            if layout.photometric == "RGB":
                rgb[:] = bytes(buf[base:base + n * 3])
            else:
                for i in range(n):
                    y = buf[base + i * 3]
                    cb = buf[base + i * 3 + 1]
                    cr = buf[base + i * 3 + 2]
                    rgb[i * 3], rgb[i * 3 + 1], rgb[i * 3 + 2] = \
                        _ybr_to_rgb(y, cb, cr)
        else:
            plane = n
            for i in range(n):
                y = buf[base + i]
                cb = buf[base + plane + i]
                cr = buf[base + 2 * plane + i]
                if layout.photometric == "RGB":
                    rgb[i * 3], rgb[i * 3 + 1], rgb[i * 3 + 2] = y, cb, cr
                else:
                    rgb[i * 3], rgb[i * 3 + 1], rgb[i * 3 + 2] = \
                        _ybr_to_rgb(y, cb, cr)
        return rgb

    # 16-bit color: take the high byte of each stored sample.
    sb = layout.sample_bytes
    scale_bits = layout.bits_stored - 8

    def _sample8(sample_index: int) -> int:
        v = _read_sample(buf, base + sample_index * sb, layout)
        if scale_bits > 0:
            v >>= scale_bits
        return v & 0xFF

    if layout.planar_configuration == 0:
        for i in range(n):
            s = i * 3
            vals = (_sample8(s), _sample8(s + 1), _sample8(s + 2))
            if layout.photometric == "RGB":
                rgb[s:s + 3] = bytes(vals)
            else:
                rgb[s:s + 3] = bytes(_ybr_to_rgb(*vals))
    else:
        plane = n
        for i in range(n):
            vals = (_sample8(i), _sample8(plane + i),
                    _sample8(2 * plane + i))
            if layout.photometric == "RGB":
                rgb[i * 3:i * 3 + 3] = bytes(vals)
            else:
                rgb[i * 3:i * 3 + 3] = bytes(_ybr_to_rgb(*vals))
    return rgb


def render_frame_rgb(ds: Dataset, layout: PixelLayout,
                     frame_no: int = 0) -> tuple[bytearray, int, int]:
    """Return an (RGB bytes, width, height) raster at original resolution."""
    if layout.photometric not in RENDERABLE_PMS:
        raise PixelMaskError(
            f"缩略图暂不支持 PhotometricInterpretation="
            f"{layout.photometric} 的渲染；遮罩填充仍可执行。",
            code="render_unsupported_photometric")
    if not 0 <= frame_no < layout.frames:
        raise PixelMaskError(
            f"帧 {frame_no + 1} 超出总帧数 {layout.frames}。",
            code="frame_out_of_range")

    if layout.samples_per_pixel == 1:
        if layout.photometric == "PALETTE COLOR":
            rgb = _palette_frame(ds.PixelData, layout, ds, frame_no)
        else:
            gray = _grayscale_frame(ds.PixelData, layout, ds, frame_no)
            rgb = bytearray(layout.rows * layout.columns * 3)
            rgb[0::3] = rgb[1::3] = rgb[2::3] = gray
    else:
        rgb = _color_frame(ds.PixelData, layout, frame_no)
    return rgb, layout.columns, layout.rows


def downscale(rgb: bytearray, width: int, height: int,
              max_edge: int = MAX_THUMBNAIL_EDGE
              ) -> tuple[bytearray, int, int]:
    """Nearest-neighbour downscale so the longest edge <= ``max_edge``.

    Uses ``floor`` (truncation) so the result matches ``HTMLCanvasElement``
    sampling on the front end exactly; otherwise a 1-px size disagreement
    could shift the far edge of a drawn rectangle by a source pixel.
    """
    longest = max(width, height)
    if longest <= max_edge:
        return rgb, width, height
    step = longest / max_edge
    new_w = max(1, int(width / step))
    new_h = max(1, int(height / step))
    out = bytearray(new_w * new_h * 3)
    for oy in range(new_h):
        sy = min(height - 1, int(oy * step))
        src_row = sy * width * 3
        dst_row = oy * new_w * 3
        for ox in range(new_w):
            sx = min(width - 1, int(ox * step))
            s = src_row + sx * 3
            d = dst_row + ox * 3
            out[d] = rgb[s]
            out[d + 1] = rgb[s + 1]
            out[d + 2] = rgb[s + 2]
    return out, new_w, new_h


# ---------------------------------------------------------------------------
# PNG encoding (8-bit grayscale or RGB, standard library only)
# ---------------------------------------------------------------------------

def _png_chunk(kind: bytes, data: bytes) -> bytes:
    chunk = kind + data
    crc = zlib.crc32(chunk) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk + struct.pack(">I", crc)


def encode_png(rgb: bytearray, width: int, height: int) -> bytes:
    """Encode an 8-bit RGB raster as a PNG (filter type 0 per scanline)."""
    raw = bytearray()
    stride = width * 3
    for row in range(height):
        raw.append(0)
        raw.extend(rgb[row * stride:(row + 1) * stride])
    compressed = zlib.compress(bytes(raw), 9)
    out = b"\x89PNG\r\n\x1a\n"
    out += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height,
                                          8, 2, 0, 0, 0))
    out += _png_chunk(b"IDAT", compressed)
    out += _png_chunk(b"IEND", b"")
    return out


def render_thumbnail(ds: Dataset, layout: PixelLayout, frame_no: int = 0
                     ) -> dict:
    """Full thumbnail response: data-URL plus geometry/frame metadata."""
    rgb, width, height = render_frame_rgb(ds, layout, frame_no)
    rgb, tw, th = downscale(rgb, width, height)
    png = encode_png(rgb, tw, th)
    return {
        "image_data_url":
            "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
        "width": width,
        "height": height,
        "thumbnail_width": tw,
        "thumbnail_height": th,
        "frame_count": layout.frames,
        "frame_number": frame_no + 1,
        "photometric_interpretation": layout.photometric,
        "samples_per_pixel": layout.samples_per_pixel,
        "bits_allocated": layout.bits_allocated,
        "compressed": False,
    }


def render_mask_preview(ds: Dataset, layout: PixelLayout, frame_no: int,
                        rectangles: list[Rect]) -> dict:
    """Thumbnail with red outlines/fills for the active frame's rects."""
    rgb, width, height = render_frame_rgb(ds, layout, frame_no)
    rgb, tw, th = downscale(rgb, width, height)
    sx = tw / width
    sy = th / height

    for rect in rectangles:
        c = rect.clamp(width, height)
        if c is None:
            continue
        x0 = int(round(c.x * sx))
        y0 = int(round(c.y * sy))
        x1 = max(x0 + 1, int(round((c.x + c.width) * sx)))
        y1 = max(y0 + 1, int(round((c.y + c.height) * sy)))
        x0 = max(0, min(tw - 1, x0))
        x1 = max(x0 + 1, min(tw, x1))
        y0 = max(0, min(th - 1, y0))
        y1 = max(y0 + 1, min(th, y1))

        def _blend(px: int, py: int) -> None:
            i = (py * tw + px) * 3
            rgb[i] = (rgb[i] + 255) // 2
            rgb[i + 1] //= 3
            rgb[i + 2] //= 3

        for x in range(x0, x1):
            _blend(x, y0)
            _blend(x, y1 - 1)
        for y in range(y0, y1):
            _blend(x0, y)
            _blend(x1 - 1, y)

    png = encode_png(rgb, tw, th)
    return {
        "image_data_url":
            "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
        "width": width,
        "height": height,
        "thumbnail_width": tw,
        "thumbnail_height": th,
        "frame_count": layout.frames,
        "frame_number": frame_no + 1,
        "rectangle_count": len(rectangles),
    }
