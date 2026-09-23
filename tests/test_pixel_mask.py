"""Unit tests for the manual rectangular pixel-mask module."""
from __future__ import annotations

import base64
import struct
import zlib

import pytest
from pydicom.dataset import Dataset, FileDataset
from pydicom.encaps import encapsulate
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    JPEGBaseline8Bit,
    generate_uid,
)

from app import pixel_mask as pm
from app.pixel_mask import MaskGroup, PixelMaskError, Rect


def _write(tmp_path, name, *, rows=8, cols=8, bits=16, spp=1,
           photometric="MONOCHROME2", planar=0, signed=False,
           frames=1, pixel_bytes=None, compressed=False,
           omit_pixel=False, window=None):
    path = tmp_path / name
    meta = Dataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = (JPEGBaseline8Bit if compressed
                              else ExplicitVRLittleEndian)
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.PatientName = "Wang^Wu"
    ds.PatientID = "PID-1"
    ds.Modality = "CT"
    ds.Rows = rows
    ds.Columns = cols
    ds.BitsAllocated = bits
    ds.BitsStored = bits
    ds.HighBit = bits - 1
    ds.SamplesPerPixel = spp
    ds.PhotometricInterpretation = photometric
    ds.PixelRepresentation = 1 if signed else 0
    if frames > 1:
        ds.NumberOfFrames = frames
    if window:
        ds.WindowWidth, ds.WindowCenter = window
    if spp == 3:
        ds.PlanarConfiguration = planar
    if not omit_pixel:
        if pixel_bytes is not None:
            data = pixel_bytes
        else:
            count = rows * cols * spp * frames
            if bits == 8:
                data = bytes((i * 7) % 256 for i in range(count))
            else:
                data = b"".join(
                    ((i * 101 + 500) % 60000).to_bytes(2, "little")
                    for i in range(count))
        ds.PixelData = encapsulate([data[:32]]) if compressed \
            else data
    ds.save_as(path, enforce_file_format=True)
    return path


# ---------------------------------------------------------------------------
# Inspection errors
# ---------------------------------------------------------------------------

def test_compressed_transfer_syntax_rejected(tmp_path):
    path = _write(tmp_path, "jpg.dcm", compressed=True)
    with pytest.raises(PixelMaskError) as exc:
        pm.load_for_mask(path)
    assert exc.value.code == "compressed_transfer_syntax"
    assert "压缩传输语法" in str(exc.value)


def test_missing_pixel_data_rejected(tmp_path):
    path = _write(tmp_path, "nopx.dcm", omit_pixel=True)
    with pytest.raises(PixelMaskError) as exc:
        pm.load_for_mask(path)
    assert exc.value.code == "no_pixel_data"


def test_truncated_pixel_data_rejected(tmp_path):
    rows = cols = 8
    path = _write(tmp_path, "trunc.dcm", rows=rows, cols=cols,
                  pixel_bytes=b"\x00" * 10)
    with pytest.raises(PixelMaskError) as exc:
        pm.load_for_mask(path)
    assert exc.value.code == "pixel_data_truncated"


def test_unsupported_bits_rejected(tmp_path):
    path = _write(tmp_path, "b32.dcm", bits=8,
                  pixel_bytes=b"\x00" * 64)
    ds = __import__("pydicom").dcmread(path)
    ds.BitsAllocated = 32
    ds.BitsStored = 32
    ds.HighBit = 31
    ds.PixelData = b"\x00" * (8 * 8 * 4)
    ds.save_as(path, enforce_file_format=True)
    with pytest.raises(PixelMaskError, match="8/16"):
        pm.load_for_mask(path)


# ---------------------------------------------------------------------------
# Filling behaviour
# ---------------------------------------------------------------------------

def test_monochrome2_fill_zero_single_frame(tmp_path):
    path = _write(tmp_path, "m2.dcm")
    ds, layout = pm.load_for_mask(path)
    records = pm.apply_masks(ds, layout, [MaskGroup(
        file_index=0, rectangles=(Rect(2, 3, 2, 2),),
        frame_start=0, frame_end=0)])
    assert records == [{
        "frame_start": 1, "frame_end": 1,
        "frame_count": 1, "rectangle_count": 1,
    }]
    b = ds.PixelData
    # (col=2,row=3) -> index 3*8+2 = 26 -> zero; neighbour (3,3) also zero.
    assert b[26 * 2:26 * 2 + 2] == b"\x00\x00"
    assert b[27 * 2:27 * 2 + 2] == b"\x00\x00"
    # (col=1,row=3) outside the rect stays a non-zero gradient value.
    assert b[25 * 2:25 * 2 + 2] != b"\x00\x00"
    # geometry / photometric untouched
    assert ds.PhotometricInterpretation == "MONOCHROME2"
    assert ds.Rows == 8 and ds.Columns == 8
    assert "NumberOfFrames" not in ds
    # still uncompressed
    assert str(ds.file_meta.TransferSyntaxUID) == ExplicitVRLittleEndian


def test_monochrome1_fill_maximum(tmp_path):
    path = _write(tmp_path, "m1.dcm", photometric="MONOCHROME1")
    ds, layout = pm.load_for_mask(path)
    assert layout.fill_value == 65535
    pm.apply_masks(ds, layout, [MaskGroup(
        file_index=0, rectangles=(Rect(0, 0, 4, 1),))])
    b = ds.PixelData
    assert b[0:2] == b"\xff\xff"
    assert b[3 * 2:3 * 2 + 2] == b"\xff\xff"
    assert b[4 * 2:4 * 2 + 2] != b"\xff\xff"


def test_multiframe_frame_ranges_and_sizes(tmp_path):
    path = _write(tmp_path, "mf.dcm", frames=5)
    ds, layout = pm.load_for_mask(path)
    assert layout.frames == 5
    pm.apply_masks(ds, layout, [
        MaskGroup(file_index=0, rectangles=(Rect(1, 1, 1, 1),),
                  frame_start=1, frame_end=3),
        MaskGroup(file_index=0, rectangles=(Rect(4, 4, 1, 1),),
                  frame_start=4, frame_end=None),
    ])
    assert ds.NumberOfFrames == 5
    frame_bytes = 8 * 8 * 2
    b = ds.PixelData
    assert len(b) == frame_bytes * 5
    # frame 0 untouched
    off0 = (1 * 8 + 1) * 2
    assert b[off0:off0 + 2] != b"\x00\x00"
    # frame 1 (second frame, 0-based 1) has the fill
    off1 = frame_bytes + off0
    assert b[off1:off1 + 2] == b"\x00\x00"
    # frame 3 (0-based 3 = fourth frame) also filled
    off3 = 3 * frame_bytes + off0
    assert b[off3:off3 + 2] == b"\x00\x00"
    # frame 4 has only the (4,4) rect
    p44 = (4 * 8 + 4) * 2
    assert b[4 * frame_bytes + p44:4 * frame_bytes + p44 + 2] == b"\x00\x00"
    assert b[4 * frame_bytes + off0:4 * frame_bytes + off0 + 2] != b"\x00\x00"


def test_frame_out_of_range(tmp_path):
    path = _write(tmp_path, "f.dcm", frames=2)
    ds, layout = pm.load_for_mask(path)
    with pytest.raises(PixelMaskError) as exc:
        pm.apply_masks(ds, layout, [MaskGroup(
            file_index=0, rectangles=(Rect(0, 0, 1, 1),),
            frame_start=2, frame_end=2)])
    assert exc.value.code == "frame_out_of_range"


def test_rect_out_of_range_rejected(tmp_path):
    path = _write(tmp_path, "r.dcm")
    ds, layout = pm.load_for_mask(path)
    with pytest.raises(PixelMaskError, match="超出影像边界"):
        pm.apply_masks(ds, layout, [MaskGroup(
            file_index=0, rectangles=(Rect(6, 6, 4, 4),))])
    with pytest.raises(PixelMaskError, match="正宽高"):
        pm.apply_masks(ds, layout, [MaskGroup(
            file_index=0, rectangles=(Rect(0, 0, 0, 2),))])


def test_rgb_planar_configurations(tmp_path):
    for planar in (0, 1):
        path = _write(tmp_path, f"rgb{planar}.dcm", spp=3,
                      photometric="RGB", planar=planar, bits=8)
        ds, layout = pm.load_for_mask(path)
        pm.apply_masks(ds, layout, [MaskGroup(
            file_index=0, rectangles=(Rect(1, 1, 2, 2),))])
        b = ds.PixelData
        n = 64
        if planar == 0:
            # (col=1,row=1) -> pixel 9, sample offsets 27,28,29
            assert b[27:30] == b"\x00\x00\x00"
            assert b[24:27] != b"\x00\x00\x00"  # pixel 8 untouched
        else:
            assert b[n + 9] == 0 and b[2 * n + 9] == 0 and b[9] == 0
            assert b[8] != 0
        assert ds.PhotometricInterpretation == "RGB"
        assert ds.PlanarConfiguration == planar


def test_signed_sixteen_bit_fill(tmp_path):
    path = _write(tmp_path, "signed.dcm", signed=True)
    ds, layout = pm.load_for_mask(path)
    pm.apply_masks(ds, layout, [MaskGroup(
        file_index=0, rectangles=(Rect(0, 0, 1, 1),))])
    assert ds.PixelData[0:2] == b"\x00\x00"


# ---------------------------------------------------------------------------
# Thumbnail / preview rendering
# ---------------------------------------------------------------------------

def _decode_png(data_url: str) -> tuple[int, int, bytes]:
    raw = base64.b64decode(data_url.split(",", 1)[1])
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", raw[16:24])
    # collect IDAT and inflate for a spot check
    pos = 8
    payload = b""
    while pos < len(raw):
        length = struct.unpack(">I", raw[pos:pos + 4])[0]
        kind = raw[pos + 4:pos + 8]
        body = raw[pos + 8:pos + 8 + length]
        if kind == b"IDAT":
            payload += body
        pos += 12 + length
    return w, h, zlib.decompress(payload)


def test_thumbnail_png_geometry_and_window(tmp_path):
    path = _write(tmp_path, "t.dcm", rows=64, cols=32,
                  window=(100, 40))
    ds, layout = pm.load_for_mask(path)
    resp = pm.render_thumbnail(ds, layout, frame_no=0)
    w, h, _ = _decode_png(resp["image_data_url"])
    assert (resp["thumbnail_width"], resp["thumbnail_height"]) == (w, h)
    assert resp["width"] == 32 and resp["height"] == 64
    assert resp["frame_count"] == 1
    assert w == 32 and h == 64


def test_thumbnail_downscales_large_frames(tmp_path):
    path = _write(tmp_path, "big.dcm", rows=2048, cols=1024, bits=8)
    ds, layout = pm.load_for_mask(path)
    resp = pm.render_thumbnail(ds, layout, frame_no=0)
    assert max(resp["thumbnail_width"], resp["thumbnail_height"]) \
        <= pm.MAX_THUMBNAIL_EDGE
    assert resp["width"] == 1024 and resp["height"] == 2048


def test_mask_preview_outlines(tmp_path):
    path = _write(tmp_path, "p.dcm", rows=16, cols=16, bits=8,
                  photometric="MONOCHROME2")
    ds, layout = pm.load_for_mask(path)
    resp = pm.render_mask_preview(ds, layout, 0, [Rect(2, 2, 4, 4)])
    assert resp["rectangle_count"] == 1
    w, h, raw = _decode_png(resp["image_data_url"])
    assert w == 16 and h == 16
    # top edge of the rect at y=2: pixels from x=2..5 should be reddened.
    stride = 1 + 16 * 3  # filter byte + one scanline
    row2 = raw[2 * stride + 1:]
    idx = 2 * 3
    r, g, b = row2[idx], row2[idx + 1], row2[idx + 2]
    assert r > g and r > b


def test_palette_color_render_and_mask(tmp_path):
    path = _write(tmp_path, "pal.dcm", rows=4, cols=4, bits=16,
                  photometric="PALETTE COLOR",
                  pixel_bytes=b"".join(
                      v.to_bytes(2, "little") for v in range(16)))
    ds = __import__("pydicom").dcmread(path)
    import pydicom as _pd
    _tag = _pd.datadict.tag_for_keyword
    for desc in ("RedPaletteColorLookupTableDescriptor",
                 "GreenPaletteColorLookupTableDescriptor",
                 "BluePaletteColorLookupTableDescriptor"):
        ds.add_new(_tag(desc), "SS", [16, 0, 8])
    for attr in ("RedPaletteColorLookupTableData",
                 "GreenPaletteColorLookupTableData",
                 "BluePaletteColorLookupTableData"):
        ds.add_new(_tag(attr), "OW", bytes([0] * 16))
    ds.RedPaletteColorLookupTableData = bytes(
        [min(255, v * 16) for v in range(16)])
    ds.save_as(path, enforce_file_format=True)

    loaded, layout = pm.load_for_mask(path)
    assert layout.photometric == "PALETTE COLOR"
    resp = pm.render_thumbnail(loaded, layout, 0)
    assert resp["frame_count"] == 1
    records = pm.apply_masks(loaded, layout, [MaskGroup(
        file_index=0, rectangles=(Rect(0, 0, 1, 1),))])
    assert records[0]["rectangle_count"] == 1
    assert loaded.PixelData[0:2] == b"\x00\x00"  # index 0 fill
    assert loaded.PhotometricInterpretation == "PALETTE COLOR"
