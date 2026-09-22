"""Manual rectangular pixel masks ("人工像素遮罩").

The user draws rectangles on a down-scaled thumbnail of an *output* image
(the metadata-anonymised instance).  Coordinates are converted back to the
original pixel grid by the front end, so this module only ever deals with
``[x, y, width, height]`` values expressed in **original pixels**, clipped to
the image extent.

Only *uncompressed* transfer syntaxes are supported: pixel data is edited
in-place as a raw numpy buffer, which guarantees that:

* multiframe dimensions / frame count never change,
* window centre/width (0028,1050/1051) and photometric interpretation are
  preserved untouched,
* the transfer syntax UID is never rewritten (compressed pixel data would
  require a codec and could not be edited at arbitrary offsets).

Masked regions are filled with a photometric *safe background*:

* ``MONOCHROME2`` -> 0 (black), ``MONOCHROME1`` -> max stored value
  (also rendered as black because of the inverted VOI),
* ``RGB`` / ``YBR_FULL`` -> per-sample zero/background
  (black in RGB; YBR backgrounds map to black after YBR->RGB conversion).

Files that cannot be decoded (missing/inconsistent pixel descriptors,
unexpected pixel-data length) or that use a compressed transfer syntax raise
:class:`MaskUnsupported` -- the caller turns that into a locatable error and
blocks ZIP generation.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

import numpy as np
import pydicom
from pydicom.dataset import Dataset
from pydicom.uid import (
    DeflatedExplicitVRLittleEndian,
    ExplicitVRBigEndian,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
)

# ---------------------------------------------------------------------------
# Supported transfer syntaxes (native, uncompressed pixel data only)
# ---------------------------------------------------------------------------

UNCOMPRESSED_TRANSFER_SYNTAXES = frozenset({
    ImplicitVRLittleEndian,
    ExplicitVRLittleEndian,
    ExplicitVRBigEndian,
})

# DeflatedExplicitVRLittleEndian stores the whole dataset zlib-deflated;
# pydicom transparently inflates it on read and re-deflates on save, but the
# pixel stream itself is native Explicit VR LE, so editing is still safe.
DEFLATED_TRANSFER_SYNTAX = DeflatedExplicitVRLittleEndian

SUPPORTED_PHOTOMETRICS = frozenset({
    "MONOCHROME1", "MONOCHROME2", "RGB", "YBR_FULL",
})

# YBR_FULL_422 has a packed 4:2:2 native layout (Y1 Y2 Cb Cr per pixel pair)
# and almost always travels with a JPEG transfer syntax; it is deliberately
# not supported for raw in-place rectangle editing.
UNSUPPORTED_COLOR_PHOTOMETRICS = frozenset({
    "YBR_FULL_422", "YBR_ICT", "YBR_RCT", "PALETTE COLOR",
})


class MaskUnsupported(Exception):
    """Raised when a file cannot carry pixel masks (locatable per file)."""


class MaskValidationError(Exception):
    """Raised when a submitted rectangle/frame spec is invalid."""


class MaskBlocked(Exception):
    """At least one requested mask could not be applied -- packaging must
    stop and every offending file must be reported with a locatable reason."""

    def __init__(self, entries: list[dict]):
        self.entries = entries
        super().__init__(
            "像素遮罩无法应用：" + "；".join(
                f"文件 #{e['file_index'] + 1}（{e['source_path']}）："
                f"{e['reason']}" for e in entries))


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @classmethod
    def from_payload(cls, data: dict) -> "Rect":
        try:
            x = int(data["x"])
            y = int(data["y"])
            width = int(data["width"])
            height = int(data["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MaskValidationError(
                "矩形必须包含整数坐标 x / y / width / height。") from exc
        if width <= 0 or height <= 0:
            raise MaskValidationError(
                f"矩形宽高必须为正整数（收到 {width}×{height}）。")
        if x < 0 or y < 0:
            raise MaskValidationError(
                f"矩形起点不能为负（收到 x={x}, y={y}）。")
        return cls(x=x, y=y, width=width, height=height)

    def clip(self, columns: int, rows: int) -> "Rect | None":
        """Clip to the image extent; return None when the rect is outside."""
        x0 = max(0, self.x)
        y0 = max(0, self.y)
        x1 = min(columns, self.x + self.width)
        y1 = min(rows, self.y + self.height)
        if x1 <= x0 or y1 <= y0:
            return None
        return Rect(x0, y0, x1 - x0, y1 - y0)


@dataclass(frozen=True)
class FrameRange:
    """1-based inclusive frame span; ``all_frames`` covers every frame."""
    all_frames: bool = False
    start: int = 1
    end: int = 1

    @classmethod
    def from_payload(cls, data: dict, frame_count: int) -> "FrameRange":
        flag = data.get("all_frames", False)
        if flag:
            return cls(all_frames=True, start=1, end=frame_count)
        try:
            start = data.get("frame_start", 1)
            start = int(start) if start is not None else 1
            end = data.get("frame_end", start)
            end = int(end) if end is not None else start
        except (TypeError, ValueError) as exc:
            raise MaskValidationError("帧范围必须为整数。") from exc
        if start < 1 or end < 1 or end < start:
            raise MaskValidationError(
                f"帧范围非法：{start}–{end}（帧号从 1 开始且结束帧不小于起始帧）。")
        if end > frame_count:
            raise MaskValidationError(
                f"帧范围 {start}–{end} 超出影像总帧数 {frame_count}。")
        return cls(all_frames=False, start=start, end=end)

    def indices(self) -> tuple[int, int]:
        """0-based half-open ``(start, stop)`` index pair."""
        return self.start - 1, self.end


# ---------------------------------------------------------------------------
# Image inspection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ImageGeometry:
    rows: int
    columns: int
    frame_count: int
    samples_per_pixel: int
    bits_allocated: int
    planar_configuration: int
    photometric: str
    transfer_syntax_uid: str
    pixel_length: int               # expected raw byte count of PixelData
    frame_size: int                 # bytes per frame
    dtype: np.dtype

    @property
    def is_color(self) -> bool:
        return self.samples_per_pixel == 3


def transfer_syntax_uid(ds: Dataset):
    meta = getattr(ds, "file_meta", None)
    return getattr(meta, "TransferSyntaxUID", None)


def describe_transfer_syntax(uid) -> str:
    try:
        name = pydicom.uid.UID_dictionary.get(str(uid))
        if name:
            return f"{uid}（{name[0]}）"
    except Exception:  # noqa: BLE001
        pass
    return str(uid)


def inspect_image(ds: Dataset) -> ImageGeometry:
    """Read the pixel descriptor and validate that raw editing is possible."""
    tsuid = transfer_syntax_uid(ds)
    if tsuid is None:
        raise MaskUnsupported("缺少 Transfer Syntax UID (0002,0010)。")
    if str(tsuid) not in UNCOMPRESSED_TRANSFER_SYNTAXES and \
            str(tsuid) != str(DEFLATED_TRANSFER_SYNTAX):
        raise MaskUnsupported(
            "压缩传输语法不支持像素遮罩："
            f"{describe_transfer_syntax(tsuid)}；请先转换为未压缩格式。")

    for keyword in ("Rows", "Columns", "BitsAllocated", "SamplesPerPixel"):
        if not getattr(ds, keyword, None):
            raise MaskUnsupported(f"缺少像素描述属性 {keyword}。")
    rows = int(ds.Rows)
    columns = int(ds.Columns)
    bits_allocated = int(ds.BitsAllocated)
    if bits_allocated not in (8, 16, 32):
        raise MaskUnsupported(
            f"不支持的 BitsAllocated={bits_allocated}（仅支持 8/16/32 位）。")

    samples = int(ds.SamplesPerPixel)
    photometric = str(getattr(ds, "PhotometricInterpretation", "") or "")
    if not photometric:
        raise MaskUnsupported("缺少 PhotometricInterpretation (0028,0004)。")
    if photometric not in SUPPORTED_PHOTOMETRICS:
        raise MaskUnsupported(
            f"不支持的 PhotometricInterpretation：{photometric}。")
    if samples == 1 and photometric not in ("MONOCHROME1", "MONOCHROME2"):
        raise MaskUnsupported(
            f"SamplesPerPixel=1 与 {photometric} 不匹配。")
    if samples == 3 and photometric not in ("RGB", "YBR_FULL"):
        raise MaskUnsupported(
            f"三采样影像仅支持 RGB/YBR_FULL（当前 {photometric}）。")
    if samples not in (1, 3):
        raise MaskUnsupported(
            f"不支持的 SamplesPerPixel={samples}（仅支持 1 或 3）。")

    planar = int(getattr(ds, "PlanarConfiguration", 0) or 0)
    if samples == 3 and planar not in (0, 1):
        raise MaskUnsupported(
            f"不支持的 PlanarConfiguration={planar}。")

    try:
        frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
    except (TypeError, ValueError) as exc:
        raise MaskUnsupported("NumberOfFrames 无法解析。") from exc
    if frames < 1:
        raise MaskUnsupported(f"NumberOfFrames 非法：{frames}。")

    endian = "<" if str(tsuid) != str(ExplicitVRBigEndian) else ">"
    dtype = np.dtype(
        endian + ("u1" if bits_allocated == 8
                  else "u2" if bits_allocated == 16 else "u4"))

    frame_size = rows * columns * samples * (bits_allocated // 8)
    pixel_length = frame_size * frames
    return ImageGeometry(
        rows=rows, columns=columns, frame_count=frames,
        samples_per_pixel=samples, bits_allocated=bits_allocated,
        planar_configuration=planar, photometric=photometric,
        transfer_syntax_uid=str(tsuid), pixel_length=pixel_length,
        frame_size=frame_size, dtype=dtype)


def _pixel_bytes(ds: Dataset) -> bytes:
    raw = getattr(ds, "PixelData", None)
    if raw is None:
        raise MaskUnsupported("缺少 PixelData (7FE0,0010)，无法编辑像素。")
    return bytes(raw)


def _frame_array(buf: bytes, geom: ImageGeometry, frame_index: int
                 ) -> np.ndarray:
    """Reshape one frame of raw bytes into a writable native-endian array."""
    start = frame_index * geom.frame_size
    frame = np.frombuffer(buf, dtype=geom.dtype, count=geom.frame_size
                          // geom.dtype.itemsize, offset=start)
    # byteswap to native endian if needed (copy makes the array writable)
    if geom.dtype.byteorder not in ("=", "|",
                                    "<" if np.little_endian else ">"):
        frame = frame.byteswap()
    frame = np.array(frame, copy=True)
    if geom.is_color:
        if geom.planar_configuration == 0:
            # pixel-interleaved: rows x cols x samples
            return frame.reshape(geom.rows, geom.columns,
                                 geom.samples_per_pixel)
        # planar: samples x rows x cols -> rows x cols x samples
        planar = frame.reshape(geom.samples_per_pixel, geom.rows,
                               geom.columns)
        return np.transpose(planar, (1, 2, 0)).copy()
    return frame.reshape(geom.rows, geom.columns)


def _flatten_frame(arr: np.ndarray, geom: ImageGeometry) -> np.ndarray:
    if not geom.is_color:
        return arr.reshape(-1)
    if geom.planar_configuration == 0:
        return arr.reshape(-1)
    planar = np.transpose(arr, (2, 0, 1)).copy()
    return planar.reshape(-1)


def _grayscale_background(ds: Dataset, geom: ImageGeometry) -> int:
    """Raw stored value rendered as black for a grayscale frame."""
    if geom.photometric == "MONOCHROME1":
        # Inverted VOI: the largest storable value maps to black.
        return (1 << geom.bits_allocated) - 1
    # MONOCHROME2: zero is black for unsigned pixels; signed pixels
    # (PixelRepresentation=1) use their representable minimum instead.
    if int(getattr(ds, "PixelRepresentation", 0) or 0) == 1:
        bits_stored = int(getattr(ds, "BitsStored", geom.bits_allocated)
                          or geom.bits_allocated)
        return (1 << (bits_stored - 1))
    return 0


def apply_masks(ds: Dataset, groups: list[dict]) -> dict:
    """Fill the requested frame rectangles with the safe background.

    ``groups`` is a list of ``{"rect": {...}, "frame_start", "frame_end",
    "all_frames"}`` payload fragments (already validated as Rect/FrameRange).
    Returns a compact summary ``{"frames": "1-3", "rectangles": n}``.
    Mutates ``ds.PixelData`` in place.
    """
    if not groups:
        return {"frames": "", "rectangles": 0}

    geom = inspect_image(ds)
    raw = _pixel_bytes(ds)
    if len(raw) < geom.pixel_length:
        raise MaskUnsupported(
            f"PixelData 长度 {len(raw)} 小于声明尺寸所需 "
            f"{geom.pixel_length} 字节，影像无法解码。")
    pad = len(raw) - geom.pixel_length
    if pad > geom.dtype.itemsize:
        raise MaskUnsupported(
            f"PixelData 长度 {len(raw)} 与声明尺寸不符（期望 "
            f"{geom.pixel_length} 字节）。")
    padding = raw[geom.pixel_length:]

    touched_frames: set[int] = set()
    # Decode each affected frame once, apply all of its rectangles, re-encode.
    frame_cache: dict[int, np.ndarray] = {}
    parsed: list[tuple[int, Rect]] = []
    for group in groups:
        rect = group["rect"]
        frange: FrameRange = group["range"]
        clipped = rect.clip(geom.columns, geom.rows)
        if clipped is None:
            raise MaskValidationError(
                f"矩形 ({rect.x},{rect.y},{rect.width}x{rect.height}) "
                f"完全位于影像 {geom.columns}x{geom.rows} 之外。")
        start, stop = frange.indices()
        for fi in range(start, stop):
            parsed.append((fi, clipped))

    for frame_index, rect in parsed:
        arr = frame_cache.get(frame_index)
        if arr is None:
            arr = _frame_array(raw, geom, frame_index)
            frame_cache[frame_index] = arr
        x0, y0 = rect.x, rect.y
        x1, y1 = x0 + rect.width, y0 + rect.height
        if not geom.is_color:
            arr[y0:y1, x0:x1] = np.asarray(
                _grayscale_background(ds, geom)).astype(arr.dtype)
        elif geom.photometric == "YBR_FULL":
            arr[y0:y1, x0:x1, 0] = 0
            if geom.bits_allocated == 8:
                arr[y0:y1, x0:x1, 1:] = 128
            else:  # 16-bit YBR (full-scale offsets: 32768)
                arr[y0:y1, x0:x1, 1:] = 32768
        else:  # RGB
            arr[y0:y1, x0:x1] = 0
        touched_frames.add(frame_index)

    pieces: list[bytes] = []
    for fi in range(geom.frame_count):
        if fi in frame_cache:
            flat = _flatten_frame(frame_cache[fi], geom)
            # restore original on-disk endianness
            if geom.dtype.byteorder not in ("=", "|",
                                            "<" if np.little_endian else ">"):
                flat = flat.byteswap()
            pieces.append(np.ascontiguousarray(flat).tobytes())
        else:
            pieces.append(
                raw[fi * geom.frame_size:(fi + 1) * geom.frame_size])
    pieces.append(padding)
    ds.PixelData = b"".join(pieces)

    if touched_frames == set(range(geom.frame_count)) and geom.frame_count > 1:
        frame_desc = "all"
    else:
        shown = sorted(i + 1 for i in touched_frames)
        frame_desc = _compress_frame_list(shown)
    # Each fragment is one user-drawn rectangle (a multi-frame scope still
    # counts as one rectangle, applied to every frame in scope).
    return {"frames": frame_desc, "rectangles": len(groups)}


def _compress_frame_list(frames: list[int]) -> str:
    """[1,2,3,5] -> '1-3,5'."""
    if not frames:
        return ""
    ranges: list[str] = []
    start = prev = frames[0]
    for f in frames[1:]:
        if f == prev + 1:
            prev = f
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = f
    ranges.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(ranges)


def build_frames(
    payload_groups: list, frame_count: int
) -> tuple[list[dict], list[Rect], set[int]]:
    """Expand API MaskGroup models into per-rectangle fragments.

    Returns ``(fragments, rects, touched_frame_indices)`` where fragments are
    consumed by :func:`apply_masks` and the other two values support the
    preview overlay.
    """
    fragments: list[dict] = []
    rects: list[Rect] = []
    touched: set[int] = set()
    for group in payload_groups:
        if not group.rectangles:
            continue
        frange = FrameRange.from_payload(group.model_dump(), frame_count)
        start, stop = frange.indices()
        touched.update(range(start, stop))
        for rect_model in group.rectangles:
            rect = Rect(
                x=rect_model.x, y=rect_model.y,
                width=rect_model.width, height=rect_model.height)
            # Coordinates are validated against the real image extent inside
            # apply_masks() (clip raises when a rectangle is fully outside).
            fragments.append({"rect": rect, "range": frange})
            rects.append(rect)
    return fragments, rects, touched


# ---------------------------------------------------------------------------
# Rendering: raw frame -> RGB uint8 for preview/thumbnail
# ---------------------------------------------------------------------------

def _frame_rgb(ds: Dataset, geom: ImageGeometry, frame_index: int) -> np.ndarray:
    raw = _pixel_bytes(ds)
    arr = _frame_array(raw, geom, frame_index).astype(np.float64)

    if not geom.is_color:
        bits_stored = int(getattr(ds, "BitsStored", geom.bits_allocated)
                          or geom.bits_allocated)
        pixel_rep = int(getattr(ds, "PixelRepresentation", 0) or 0)
        arr_unsigned = arr.copy()
        if pixel_rep == 1:
            sign_bit = 1 << (bits_stored - 1)
            arr = np.where(arr_unsigned >= sign_bit,
                           arr_unsigned - (1 << bits_stored),
                           arr_unsigned)
        # Window/level when present
        wc = getattr(ds, "WindowCenter", None)
        ww = getattr(ds, "WindowWidth", None)
        try:
            if wc is not None and ww is not None:
                wc0 = float(np.atleast_1d(np.asarray(wc))[0])
                ww0 = float(np.atleast_1d(np.asarray(ww))[0])
                if ww0 > 0:
                    lo, hi = wc0 - ww0 / 2.0, wc0 + ww0 / 2.0
                else:
                    lo, hi = float(arr.min()), float(arr.max())
            else:
                lo, hi = float(arr.min()), float(arr.max())
        except (TypeError, ValueError):
            lo, hi = float(arr.min()), float(arr.max())
        if hi <= lo:
            # Uniform image (or an invalid zero-width window): render the
            # constant value as mid-gray so black/white mask fills stay
            # visible against it.
            norm = np.full_like(arr, 0.5)
        else:
            norm = np.clip((arr - lo) / (hi - lo), 0.0, 1.0)
        if geom.photometric == "MONOCHROME1":
            norm = 1.0 - norm
        gray = (norm * 255.0 + 0.5).astype(np.uint8)
        return np.repeat(gray[:, :, np.newaxis], 3, axis=2)

    rgb = arr[:, :, :3].copy()
    photometric = geom.photometric
    if photometric == "YBR_FULL":
        if geom.bits_allocated == 8:
            y = rgb[:, :, 0].astype(np.float64)
            cb = rgb[:, :, 1].astype(np.float64) - 128.0
            cr = rgb[:, :, 2].astype(np.float64) - 128.0
        else:
            y = rgb[:, :, 0].astype(np.float64) / 257.0
            cb = rgb[:, :, 1].astype(np.float64) / 257.0 - 128.0
            cr = rgb[:, :, 2].astype(np.float64) / 257.0 - 128.0
        r = y + 1.402 * cr
        g = y - 0.344136 * cb - 0.714136 * cr
        b = y + 1.772 * cb
        rgb = np.stack([r, g, b], axis=2)
    scale = 255.0 / float((1 << geom.bits_allocated) - 1) \
        if geom.bits_allocated > 8 else 1.0
    rgb = np.clip(rgb * scale + 0.5, 0, 255).astype(np.uint8)
    return rgb


def render_frame_rgb(ds: Dataset, frame_index: int = 0) -> tuple[np.ndarray,
                                                                 ImageGeometry]:
    """Decode one frame into an HxWx3 uint8 RGB array (no codec needed)."""
    geom = inspect_image(ds)
    if not 0 <= frame_index < geom.frame_count:
        raise MaskValidationError(
            f"帧号 {frame_index + 1} 超出总帧数 {geom.frame_count}。")
    return _frame_rgb(ds, geom, frame_index), geom


# ---------------------------------------------------------------------------
# Minimal dependency-free PNG encoder (8-bit grayscale or RGB)
# ---------------------------------------------------------------------------

def _png_chunk(tag: bytes, data: bytes) -> bytes:
    chunk = tag + data
    return struct.pack(">I", len(data)) + chunk + \
        struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)


def encode_png(rgb: np.ndarray) -> bytes:
    """Encode an HxWx3 uint8 array as a truecolour PNG."""
    if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.uint8:
        raise ValueError("encode_png expects an HxWx3 uint8 array")
    height, width, _ = rgb.shape
    # prefix each scanline with filter type 0 (None)
    flat = np.concatenate([
        np.zeros((height, 1), dtype=np.uint8),
        np.ascontiguousarray(rgb).reshape(height, -1)
    ], axis=1).tobytes()
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(flat, 6))
            + _png_chunk(b"IEND", b""))


def downscale(rgb: np.ndarray, max_edge: int) -> np.ndarray:
    height, width = rgb.shape[:2]
    scale = min(1.0, max_edge / float(max(height, width)))
    if scale >= 1.0:
        return rgb
    new_w = max(1, int(round(width * scale)))
    new_h = max(1, int(round(height * scale)))
    ys = (np.linspace(0, 1, new_h, endpoint=False) * height).astype(int)
    xs = (np.linspace(0, 1, new_w, endpoint=False) * width).astype(int)
    return rgb[ys[:, None], xs[None, :], :].copy()


# ---------------------------------------------------------------------------
# Feasibility probe (cheap metadata-only check used by the preview payload)
# ---------------------------------------------------------------------------

def probe(ds: Dataset) -> dict:
    """Return mask-capability metadata for the preview file list."""
    try:
        geom = inspect_image(ds)
    except MaskUnsupported as exc:
        return {
            "maskable": False,
            "reason": str(exc),
            "rows": None, "columns": None, "frame_count": None,
            "photometric_interpretation":
                str(getattr(ds, "PhotometricInterpretation", "") or None),
        }
    return {
        "maskable": True,
        "reason": None,
        "rows": geom.rows,
        "columns": geom.columns,
        "frame_count": geom.frame_count,
        "photometric_interpretation": geom.photometric,
    }
