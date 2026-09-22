"""Tests for manual rectangular pixel masks (人工像素遮罩).

Covers the raw pixel engine (MONOCHROME1/2, RGB, YBR, multiframe, frame
ranges, safe backgrounds), the locatable blocking of compressed /
undecodable files, the thumbnail + mask-preview HTTP endpoints, ZIP
packaging and the coordinate-free report section.
"""
from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import zipfile

import httpx
import numpy as np
import pydicom
import pytest
import pytest_asyncio
from pydicom.dataset import Dataset, FileDataset
from pydicom.tag import Tag
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRBigEndian,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    RLELossless,
    generate_uid,
)

from app import pixel_masks as pm
from app.schemas import (
    FilePixelMasks,
    MaskGroup,
    MaskRectangle,
)

_TMP = tempfile.mkdtemp(prefix="dicombench-mask-test-")
os.environ["DICOMBENCH_DATA_DIR"] = _TMP

from app.main import app  # noqa: E402


# ---------------------------------------------------------------------------
# DICOM builders
# ---------------------------------------------------------------------------

def make_dicom_bytes(rows=8, cols=10, nf=1, *, photometric="MONOCHROME2",
                     ts=ExplicitVRLittleEndian, bits=16, spp=1,
                     planar=0, fill=1000, include_pixels=True,
                     corrupt_length=False):
    buf = io.BytesIO()
    meta = Dataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ts
    ds = FileDataset("m.dcm", {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SpecificCharacterSet = "ISO_IR 192"
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.PatientName = "Wang^Wu"
    ds.PatientID = "PID-MASK-1"
    ds.PatientBirthDate = "19750505"
    ds.Modality = "CT"
    ds.StudyDate = "20240102"
    ds.Rows = rows
    ds.Columns = cols
    ds.BitsAllocated = bits
    ds.BitsStored = bits
    ds.HighBit = bits - 1
    ds.SamplesPerPixel = spp
    ds.PhotometricInterpretation = photometric
    ds.PixelRepresentation = 0
    if spp == 3:
        ds.PlanarConfiguration = planar
    if nf > 1:
        ds.NumberOfFrames = nf
    if include_pixels:
        if spp == 1:
            arr = np.full((nf, rows, cols), fill,
                          dtype=f"<u{bits // 8}")
        else:
            arr = np.full((nf, rows, cols, 3), 200, dtype=np.uint8)
            if planar == 1:
                arr = np.transpose(arr, (0, 3, 1, 2))
        raw = arr.tobytes()
        if corrupt_length:
            raw = raw[:-7]
        ds.PixelData = raw
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


def rect(x=1, y=1, w=3, h=2) -> MaskRectangle:
    return MaskRectangle(x=x, y=y, width=w, height=h)


# ---------------------------------------------------------------------------
# Engine: raw pixel edits
# ---------------------------------------------------------------------------

def _apply(payload_groups, ds, frame_count):
    fragments, _, _ = pm.build_frames(payload_groups, frame_count)
    return pm.apply_masks(ds, fragments)


def test_monochrome2_fill_is_zero():
    ds = pydicom.dcmread(io.BytesIO(make_dicom_bytes()))
    summary = _apply(
        [MaskGroup(rectangles=[rect(2, 3, 4, 2)])], ds, 1)
    arr = ds.pixel_array
    assert summary == {"frames": "1", "rectangles": 1}
    assert int(arr[3:5, 2:6].max()) == 0
    assert int(arr[0, 0]) == 1000  # outside untouched


def test_monochrome1_fill_is_max_rendered_black():
    data = make_dicom_bytes(photometric="MONOCHROME1", bits=8, fill=100)
    ds = pydicom.dcmread(io.BytesIO(data))
    _apply([MaskGroup(rectangles=[rect(0, 0, 2, 2)])], ds, 1)
    assert int(ds.pixel_array[0, 0]) == 255  # max stored = rendered black
    assert int(ds.pixel_array[0, 4]) == 100
    # the renderer must show it as black
    rgb, _ = pm.render_frame_rgb(ds, 0)
    assert rgb[0, 0].tolist() == [0, 0, 0]
    assert rgb[0, 4].tolist() != [0, 0, 0]


def test_rgb_both_planar_configurations_fill_black():
    for planar in (0, 1):
        ds = pydicom.dcmread(io.BytesIO(
            make_dicom_bytes(photometric="RGB", bits=8, spp=3,
                             planar=planar)))
        _apply([MaskGroup(rectangles=[rect(1, 1, 2, 2)])], ds, 1)
        region = ds.pixel_array[1:3, 1:3]
        assert region.min() == 0 and region.max() == 0
        assert ds.pixel_array[0, 0].tolist() == [200, 200, 200]


def test_ybr_full_background_renders_black():
    ds = pydicom.dcmread(io.BytesIO(
        make_dicom_bytes(photometric="YBR_FULL", bits=8, spp=3)))
    _apply([MaskGroup(rectangles=[rect(0, 0, 2, 2)])], ds, 1)
    rgb, _ = pm.render_frame_rgb(ds, 0)
    assert rgb[0, 0].tolist() == [0, 0, 0]


def test_multiframe_current_and_all_frame_scope():
    data = make_dicom_bytes(nf=4)
    ds = pydicom.dcmread(io.BytesIO(data))
    _apply([
        MaskGroup(frame_start=2, frame_end=3, rectangles=[rect(0, 0, 1, 1)]),
    ], ds, 4)
    arr = ds.pixel_array
    assert arr.shape == (4, 8, 10)  # multiframe dimensions preserved
    assert int(arr[0, 0, 0]) == 1000
    assert int(arr[1, 0, 0]) == 0
    assert int(arr[2, 0, 0]) == 0
    assert int(arr[3, 0, 0]) == 1000

    ds = pydicom.dcmread(io.BytesIO(make_dicom_bytes(nf=3)))
    summary = _apply(
        [MaskGroup(all_frames=True, rectangles=[rect(0, 0, 1, 1)])], ds, 3)
    assert summary["frames"] == "all"
    assert int(ds.pixel_array[:, 0, 0].max()) == 0


def test_big_endian_preserves_encoding():
    # pydicom refuses to byte-swap on save_as, so the big-endian raw path is
    # exercised directly against a dataset whose metadata claims big endian.
    ds = Dataset()
    ds.file_meta = Dataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRBigEndian
    ds.Rows = 4
    ds.Columns = 5
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    ds.PixelData = np.full((4, 5), 100, dtype=">u2").tobytes()
    geom = pm.inspect_image(ds)
    assert str(geom.transfer_syntax_uid) == str(ExplicitVRBigEndian)
    _apply([MaskGroup(rectangles=[rect(0, 0, 1, 1)])], ds, 1)
    raw = bytes(ds.PixelData)
    assert raw[:2] == b"\x00\x00"            # masked value in BE
    assert raw[2:4] == (100).to_bytes(2, "big")
    assert str(ds.file_meta.TransferSyntaxUID) == str(ExplicitVRBigEndian)


def test_implicit_vr_little_endian_supported():
    ds = pydicom.dcmread(io.BytesIO(make_dicom_bytes(
        ts=ImplicitVRLittleEndian, fill=10)))
    _apply([MaskGroup(rectangles=[rect(0, 0, 1, 1)])], ds, 1)
    assert int(ds.pixel_array[0, 0]) == 0


def test_32bit_grayscale_mask_keeps_dtype():
    data = make_dicom_bytes(bits=32, fill=123456789)
    ds = pydicom.dcmread(io.BytesIO(data))
    geom = pm.inspect_image(ds)
    assert geom.dtype.itemsize == 4
    _apply([MaskGroup(rectangles=[rect(0, 0, 2, 2)])], ds, 1)
    assert int(ds.pixel_array[0, 0]) == 0
    assert int(ds.pixel_array[0, 4]) == 123456789


def test_pixel_descriptor_and_photometric_preserved():
    data = make_dicom_bytes(nf=2)
    ds = pydicom.dcmread(io.BytesIO(data))
    ds.WindowCenter = 40
    ds.WindowWidth = 400
    _apply([MaskGroup(frame_start=1, frame_end=2,
                      rectangles=[rect(0, 0, 1, 1)])], ds, 2)
    assert int(ds.Rows) == 8 and int(ds.Columns) == 10
    assert int(ds.NumberOfFrames) == 2
    assert int(ds.WindowCenter) == 40 and int(ds.WindowWidth) == 400
    assert str(ds.PhotometricInterpretation) == "MONOCHROME2"


def test_window_level_rendered_in_thumbnail():
    ds = pydicom.dcmread(io.BytesIO(make_dicom_bytes(fill=1000)))
    ds.WindowCenter = 0
    ds.WindowWidth = 100
    rgb, geom = pm.render_frame_rgb(ds, 0)
    # value 1000 is fully above the window -> white
    assert rgb[0, 0].tolist() == [255, 255, 255]
    assert geom.frame_count == 1


def test_uniform_image_renders_midgray_not_black():
    # A constant image with no window must render mid-gray so a black mask
    # fill remains visible (and white fill in MONOCHROME1 stays visible).
    ds2 = pydicom.dcmread(io.BytesIO(make_dicom_bytes(
        photometric="MONOCHROME2", fill=1000)))
    rgb, _ = pm.render_frame_rgb(ds2, 0)
    assert rgb[0, 0].tolist() == [128, 128, 128]

    ds1 = pydicom.dcmread(io.BytesIO(make_dicom_bytes(
        photometric="MONOCHROME1", bits=8, fill=100)))
    rgb1, _ = pm.render_frame_rgb(ds1, 0)
    assert rgb1[0, 0].tolist() == [128, 128, 128]
    # mask (max stored) is rendered black even on a uniform image
    _apply([MaskGroup(rectangles=[rect(0, 0, 1, 1)])], ds1, 1)
    rgb1m, _ = pm.render_frame_rgb(ds1, 0)
    assert rgb1m[0, 0].tolist() == [0, 0, 0]


# ---------------------------------------------------------------------------
# Engine: locatable errors
# ---------------------------------------------------------------------------

def test_compressed_transfer_syntax_rejected():
    ds = pydicom.dcmread(io.BytesIO(make_dicom_bytes(
        ts=RLELossless, include_pixels=False)))
    with pytest.raises(pm.MaskUnsupported) as exc:
        pm.inspect_image(ds)
    assert "压缩传输语法" in str(exc.value)


def test_palette_color_rejected():
    ds = pydicom.dcmread(io.BytesIO(make_dicom_bytes(
        photometric="PALETTE COLOR")))
    with pytest.raises(pm.MaskUnsupported):
        pm.inspect_image(ds)


def test_ybr_full_422_native_rejected():
    # YBR_FULL_422 native layout is packed 4:2:2 and not safe for raw edits.
    ds = pydicom.dcmread(io.BytesIO(make_dicom_bytes(
        photometric="YBR_FULL_422", bits=8, spp=3)))
    with pytest.raises(pm.MaskUnsupported, match="YBR_FULL_422"):
        pm.inspect_image(ds)


def test_missing_pixel_data_rejected():
    ds = pydicom.dcmread(io.BytesIO(make_dicom_bytes(include_pixels=False)))
    with pytest.raises(pm.MaskUnsupported, match="PixelData"):
        _apply([MaskGroup(rectangles=[rect()])], ds, 1)


def test_truncated_pixel_data_rejected():
    ds = pydicom.dcmread(io.BytesIO(make_dicom_bytes(corrupt_length=True)))
    with pytest.raises(pm.MaskUnsupported, match="无法解码"):
        _apply([MaskGroup(rectangles=[rect()])], ds, 1)


def test_frame_range_out_of_bounds_rejected():
    with pytest.raises(pm.MaskValidationError):
        pm.FrameRange.from_payload(
            {"all_frames": False, "frame_start": 5, "frame_end": 6}, 2)


def test_rectangle_fully_outside_rejected():
    ds = pydicom.dcmread(io.BytesIO(make_dicom_bytes(cols=10, rows=8)))
    with pytest.raises(pm.MaskValidationError):
        _apply([MaskGroup(rectangles=[MaskRectangle(
            x=50, y=50, width=2, height=2)])], ds, 1)


def test_partial_outside_rectangle_is_clipped():
    ds = pydicom.dcmread(io.BytesIO(make_dicom_bytes(cols=10, rows=8)))
    _apply([MaskGroup(rectangles=[MaskRectangle(
        x=8, y=6, width=10, height=10)])], ds, 1)
    assert int(ds.pixel_array[6:8, 8:10].max()) == 0


def test_invalid_rectangle_dimensions():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        MaskRectangle(x=0, y=0, width=0, height=2)
    with pytest.raises(ValidationError):
        MaskRectangle(x=-1, y=0, width=2, height=2)
    # dict-form payloads that bypass schema validation are still caught by
    # the engine-level Rect parser.
    with pytest.raises(pm.MaskValidationError):
        pm.Rect.from_payload({"x": 0, "y": 0, "width": -1, "height": 2})
    with pytest.raises(pm.MaskValidationError):
        pm.Rect.from_payload({"x": -1, "y": 0, "width": 2, "height": 2})


# ---------------------------------------------------------------------------
# PNG encoder
# ---------------------------------------------------------------------------

def test_png_roundtrip_signature_and_size():
    rgb = np.zeros((4, 6, 3), dtype=np.uint8)
    rgb[0, 0] = [255, 0, 0]
    png = pm.encode_png(rgb)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    import struct
    import zlib
    assert png[12:16] == b"IHDR"
    w, h = struct.unpack(">II", png[16:24])
    assert (w, h) == (6, 4)
    # IDAT decompresses to one filter byte + RGB triplets per scanline
    pos, data = 8, b""
    while pos < len(png):
        length = struct.unpack(">I", png[pos:pos + 4])[0]
        tag = png[pos + 4:pos + 8]
        if tag == b"IDAT":
            data += png[pos + 8:pos + 8 + length]
        pos += 12 + length
    raw = zlib.decompress(data)
    assert len(raw) == h * (1 + w * 3)


def test_downscale_keeps_aspect_and_masks_pixels():
    big = np.zeros((100, 200, 3), dtype=np.uint8)
    big[0:20, 0:20] = 255
    small = pm.downscale(big, 50)
    assert small.shape[0] == 25 and small.shape[1] == 50
    assert int(small[0, 0].max()) > 0  # masked bright corner survives


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def job(client):
    r = await client.post(
        "/api/jobs",
        files=[
            ("files", ("uncompressed.dcm", make_dicom_bytes(nf=3),
                       "application/dicom")),
            ("files", ("compressed.dcm",
                       make_dicom_bytes(ts=RLELossless,
                                        include_pixels=False),
                       "application/dicom")),
        ],
    )
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_preview_payload_carries_mask_capability(client, job):
    r = await client.post(f"/api/jobs/{job['job_id']}/preview",
                          json={"profile": "strict"})
    assert r.status_code == 200, r.text
    files = r.json()["files"]
    f0, f1 = files
    assert f0["maskable"] is True
    assert f0["rows"] == 8 and f0["columns"] == 10
    assert f0["frame_count"] == 3
    assert f0["photometric_interpretation"] == "MONOCHROME2"
    assert f1["maskable"] is False
    assert "压缩传输语法" in f1["mask_reason"]


@pytest.mark.asyncio
async def test_thumbnail_returns_png_with_geometry_headers(client, job):
    r = await client.get(
        f"/api/jobs/{job['job_id']}/files/0/thumbnail",
        params={"frame": 2})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert int(r.headers["X-Dicom-Rows"]) == 8
    assert int(r.headers["X-Dicom-Columns"]) == 10
    assert int(r.headers["X-Dicom-Frames"]) == 3
    assert int(r.headers["X-Dicom-Frame"]) == 2


@pytest.mark.asyncio
async def test_thumbnail_compressed_file_422_locatable(client, job):
    r = await client.get(f"/api/jobs/{job['job_id']}/files/1/thumbnail")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["file_index"] == 1
    assert detail["source_path"] == "compressed.dcm"
    assert "压缩传输语法" in detail["message"]


@pytest.mark.asyncio
async def test_thumbnail_bad_frame_400(client, job):
    r = await client.get(
        f"/api/jobs/{job['job_id']}/files/0/thumbnail",
        params={"frame": 99})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_mask_preview_endpoint_applies_rectangles(client, job):
    body = {
        "profile": "strict",
        "frame": 2,
        "pixel_masks": [{
            "file_index": 0,
            "groups": [{
                "all_frames": True,
                "rectangles": [{"x": 1, "y": 1, "width": 3, "height": 2}],
            }],
        }],
    }
    r = await client.post(
        f"/api/jobs/{job['job_id']}/files/0/mask-preview", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["rectangle_count"] == 1
    assert data["frame"] == 2 and data["frame_count"] == 3
    png = base64.b64decode(data["image_png_base64"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_process_applies_masks_and_records_coordinate_free_report(
        client, job):
    body = {
        "profile": "strict",
        "pixel_masks": [{
            "file_index": 0,
            "groups": [
                {"all_frames": False, "frame_start": 1, "frame_end": 2,
                 "rectangles": [
                     {"x": 1, "y": 1, "width": 3, "height": 2}]},
            ],
        }],
    }
    r = await client.post(f"/api/jobs/{job['job_id']}/process", json=body)
    assert r.status_code == 200, r.text
    section = r.json()["pixel_masks"]
    assert section["masked_file_count"] == 1
    entry = section["files"][0]
    assert entry["source_relative_path"] == "uncompressed.dcm"
    assert entry["frame_range"] == "1-2"
    assert entry["rectangle_count"] == 1
    # no coordinates in the API section
    raw = json.dumps(section)
    assert '"x"' not in raw and '"y"' not in raw

    z = await client.get(r.json()["download_url"])
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    report = json.loads(zf.read("anonymization-report.json"))
    assert report["pixel_masks"]["masked_file_count"] == 1
    assert report["pixel_masks"]["files"][0]["rectangle_count"] == 1

    dicoms = [n for n in zf.namelist() if n.endswith(".dcm")]
    masked = None
    for name in dicoms:
        d = pydicom.dcmread(io.BytesIO(zf.read(name)))
        if d.pixel_array.shape == (3, 8, 10):
            masked = d
            break
    assert masked is not None
    arr = masked.pixel_array
    assert arr.shape == (3, 8, 10)
    assert int(arr[0, 1, 1]) == 0 and int(arr[1, 1, 1]) == 0
    assert int(arr[2, 1, 1]) == 1000   # frame 3 outside 1-2
    assert str(masked.PhotometricInterpretation) == "MONOCHROME2"
    # de-identification method records the manual step
    method = "\\".join(masked.DeidentificationMethod)
    assert "Manual rectangular pixel masks" in method


@pytest.mark.asyncio
async def test_process_mixed_all_and_per_frame_groups(client, job):
    # Mirrors the front-end serializer: one all-frames group plus a separate
    # current-frame group for the same file.
    body = {
        "profile": "strict",
        "pixel_masks": [{
            "file_index": 0,
            "groups": [
                {"all_frames": True,
                 "rectangles": [{"x": 0, "y": 0, "width": 2, "height": 2}]},
                {"all_frames": False, "frame_start": 3, "frame_end": 3,
                 "rectangles": [{"x": 4, "y": 4, "width": 2, "height": 2}]},
            ],
        }],
    }
    r = await client.post(f"/api/jobs/{job['job_id']}/process", json=body)
    assert r.status_code == 200, r.text
    zf = zipfile.ZipFile(io.BytesIO(
        await (await client.get(r.json()["download_url"])).aread()))
    name = next(n for n in zf.namelist() if n.endswith(".dcm"))
    arr = pydicom.dcmread(io.BytesIO(zf.read(name))).pixel_array
    # all-frames rectangle applied to every frame
    assert int(arr[:, 0, 0].max()) == 0
    # frame 3 also carries the per-frame rectangle (1-based -> index 2)
    assert int(arr[2, 4:6, 4:6].max()) == 0
    assert int(arr[0, 4, 4]) == 1000
    section = r.json()["pixel_masks"]["files"][0]
    assert section["frame_range"] == "all"
    assert section["rectangle_count"] == 2


@pytest.mark.asyncio
async def test_process_compressed_mask_blocks_zip_422(client, job):
    body = {
        "profile": "strict",
        "pixel_masks": [{
            "file_index": 1,
            "groups": [{
                "all_frames": True,
                "rectangles": [{"x": 0, "y": 0, "width": 2, "height": 2}],
            }],
        }],
    }
    r = await client.post(f"/api/jobs/{job['job_id']}/process", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["pixel_mask_errors"][0]["file_index"] == 1
    assert "压缩传输语法" in detail["pixel_mask_errors"][0]["reason"]

    status = await client.get(f"/api/jobs/{job['job_id']}/status")
    assert status.json()["status"] == "error"
    dl = await client.get(f"/api/jobs/{job['job_id']}/download")
    assert dl.status_code == 409


@pytest.mark.asyncio
async def test_preview_surfaces_locatable_mask_errors(client, job):
    body = {
        "profile": "strict",
        "pixel_masks": [{
            "file_index": 1,
            "groups": [{
                "all_frames": True,
                "rectangles": [{"x": 0, "y": 0, "width": 2, "height": 2}],
            }],
        }],
    }
    r = await client.post(f"/api/jobs/{job['job_id']}/preview", json=body)
    assert r.status_code == 200, r.text  # preview never fails on masks
    errors = r.json()["mask_errors"]
    assert errors[0]["file_index"] == 1
    assert errors[0]["source_path"] == "compressed.dcm"
    assert "压缩传输语法" in errors[0]["reason"]


@pytest.mark.asyncio
async def test_mask_preview_unknown_file_404(client, job):
    r = await client.post(
        f"/api/jobs/{job['job_id']}/files/9/mask-preview",
        json={"profile": "strict", "frame": 1, "pixel_masks": []})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_mask_preview_compressed_file_422(client, job):
    r = await client.post(
        f"/api/jobs/{job['job_id']}/files/1/mask-preview",
        json={"profile": "strict", "frame": 1, "pixel_masks": []})
    assert r.status_code == 422
    assert r.json()["detail"]["file_index"] == 1


@pytest.mark.asyncio
async def test_process_without_masks_ignores_compressed_file(client, job):
    # No masks -> compressed files are anonymised as usual; only an explicit
    # mask request on an unsupported file blocks packaging.
    r = await client.post(f"/api/jobs/{job['job_id']}/process",
                          json={"profile": "strict"})
    assert r.status_code == 200, r.text
    z = await client.get(r.json()["download_url"])
    assert z.status_code == 200


@pytest.mark.asyncio
async def test_process_invalid_frame_range_422(client, job):
    body = {
        "profile": "strict",
        "pixel_masks": [{
            "file_index": 0,
            "groups": [{
                "all_frames": False, "frame_start": 2, "frame_end": 99,
                "rectangles": [{"x": 0, "y": 0, "width": 2, "height": 2}],
            }],
        }],
    }
    r = await client.post(f"/api/jobs/{job['job_id']}/process", json=body)
    assert r.status_code == 422
    assert "帧范围" in r.json()["detail"]["pixel_mask_errors"][0]["reason"]


@pytest.mark.asyncio
async def test_undecodable_pixels_block_packaging(client):
    # Valid metadata + declared pixel geometry but truncated PixelData:
    # the metadata probe says "maskable", but applying the mask must fail
    # with a locatable error and no ZIP.
    r = await client.post(
        "/api/jobs",
        files=[("files", ("broken.dcm",
                          make_dicom_bytes(corrupt_length=True),
                          "application/dicom"))])
    jid = r.json()["job_id"]
    body = {
        "profile": "strict",
        "pixel_masks": [{
            "file_index": 0,
            "groups": [{
                "all_frames": True,
                "rectangles": [{"x": 0, "y": 0, "width": 2, "height": 2}],
            }],
        }],
    }
    r = await client.post(f"/api/jobs/{jid}/process", json=body)
    assert r.status_code == 422
    reason = r.json()["detail"]["pixel_mask_errors"][0]["reason"]
    assert "无法解码" in reason or "PixelData" in reason


@pytest.mark.asyncio
async def test_mask_schema_models():
    fm = FilePixelMasks(file_index=2, groups=[
        MaskGroup(rectangles=[MaskRectangle(x=1, y=2, width=3, height=4)])])
    dumped = fm.model_dump()
    assert dumped["groups"][0]["frame_start"] is None


# ---------------------------------------------------------------------------
# Front-end payload contract (mirrors serializePixelMasks() in app.js)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rectangle_requires_width_height_not_short_keys(client, job):
    # The front end always submits width/height in original pixels; the
    # w/h internal canvas keys must never reach the API (pydantic drops them,
    # leaving a missing required field -> 422).
    body = {
        "profile": "strict",
        "pixel_masks": [{
            "file_index": 0,
            "groups": [{
                "all_frames": True,
                "rectangles": [{"x": 0, "y": 0, "w": 2, "h": 2}],
            }],
        }],
    }
    r = await client.post(f"/api/jobs/{job['job_id']}/process", json=body)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_preview_with_masks_does_not_error(client, job):
    body = {
        "profile": "strict",
        "pixel_masks": [{
            "file_index": 0,
            "groups": [{
                "all_frames": True,
                "rectangles": [{"x": 0, "y": 0, "width": 2, "height": 2}],
            }],
        }],
    }
    r = await client.post(f"/api/jobs/{job['job_id']}/preview", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["mask_errors"] == []
    assert data["pixel_masks"]["masked_file_count"] == 1
    f0 = next(f for f in data["files"] if f["index"] == 0)
    assert f0["mask_rectangles"] == 1
