"""End-to-end HTTP tests for the manual pixel-mask feature."""
from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile

import httpx
import pydicom
import pytest
import pytest_asyncio
from pydicom.dataset import Dataset, FileDataset
from pydicom.encaps import encapsulate
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    JPEGBaseline8Bit,
    generate_uid,
)

_TMP = tempfile.mkdtemp(prefix="dicombench-masktest-")
os.environ["DICOMBENCH_DATA_DIR"] = _TMP

from app.main import app  # noqa: E402


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as c:
        yield c


def _dicom_bytes(*, rows=8, cols=8, frames=1, photometric="MONOCHROME2",
                 compressed=False, bits=16, spp=1, planar=0,
                 window=None) -> bytes:
    buf = io.BytesIO()
    sop = generate_uid()
    meta = Dataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = sop
    meta.TransferSyntaxUID = (JPEGBaseline8Bit if compressed
                              else ExplicitVRLittleEndian)
    ds = FileDataset("in.dcm", {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = sop
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.PatientName = "Wang^Wu"
    ds.PatientID = "PID-9001"
    ds.Modality = "CT"
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
    if frames > 1:
        ds.NumberOfFrames = frames
    if window:
        ds.WindowWidth, ds.WindowCenter = window
    count = rows * cols * spp * frames
    if spp == 3 and bits == 8:
        pixels = bytes([200] * count)
    else:
        pixels = b"".join((1000 + (i % 1000)).to_bytes(bits // 8, "little")
                          for i in range(count))
    ds.PixelData = encapsulate([pixels[:32]]) if compressed else pixels
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


async def _upload(client, entries):
    files = [("files", (name, data, "application/dicom"))
             for name, data in entries]
    r = await client.post("/api/jobs", files=files)
    assert r.status_code == 200, r.text
    return r.json()["job_id"]


@pytest.mark.asyncio
async def test_thumbnail_endpoint_returns_png(client):
    job = await _upload(client, [("a.dcm", _dicom_bytes())])
    r = await client.post(f"/api/jobs/{job}/thumbnail",
                          json={"file_index": 0, "frame": 0})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["width"] == 8 and data["height"] == 8
    assert data["frame_count"] == 1
    assert data["photometric_interpretation"] == "MONOCHROME2"
    assert data["image_data_url"].startswith("data:image/png;base64,")
    assert data["compressed"] is False


@pytest.mark.asyncio
async def test_thumbnail_compressed_returns_locatable_422(client):
    job = await _upload(client, [("jpg.dcm", _dicom_bytes(compressed=True))])
    r = await client.post(f"/api/jobs/{job}/thumbnail",
                          json={"file_index": 0})
    assert r.status_code == 422
    err = r.json()["detail"]["pixel_mask_errors"][0]
    assert err["code"] == "compressed_transfer_syntax"
    assert err["file_index"] == 0
    assert err["source_path"] == "jpg.dcm"
    assert "压缩传输语法" in err["message"]


@pytest.mark.asyncio
async def test_thumbnail_unknown_file_404(client):
    job = await _upload(client, [("a.dcm", _dicom_bytes())])
    r = await client.post(f"/api/jobs/{job}/thumbnail",
                          json={"file_index": 9})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_mask_preview_endpoint(client):
    job = await _upload(client, [("a.dcm", _dicom_bytes())])
    r = await client.post(f"/api/jobs/{job}/mask-preview", json={
        "file_index": 0, "frame": 0,
        "rectangles": [{"x": 1, "y": 1, "width": 3, "height": 2}],
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["rectangle_count"] == 1
    assert data["image_data_url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_process_fills_pixels_and_preserves_attributes(client):
    data_bytes = _dicom_bytes(frames=3, window=(200, 50))
    job = await _upload(client, [("study/a.dcm", data_bytes)])
    body = {
        "profile": "recommended",
        "pixel_masks": [{
            "file_index": 0, "frame_start": 0, "frame_end": None,
            "rectangles": [{"x": 0, "y": 0, "width": 2, "height": 2}],
        }],
    }
    r = await client.post(f"/api/jobs/{job}/process", json=body)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["gate"]["passed"] is True
    masks = result["pixel_masks"]
    assert len(masks) == 1
    region = masks[0]["regions"][0]
    assert region == {"frame_start": 1, "frame_end": 3,
                      "frame_count": 3, "rectangle_count": 1}
    assert masks[0]["frame_count_total"] == 3

    z = await client.get(result["download_url"])
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    dicom_name = [n for n in zf.namelist() if n.endswith(".dcm")][0]
    ds = pydicom.dcmread(io.BytesIO(zf.read(dicom_name)))
    # dimensions / window level / photometric preserved
    assert ds.NumberOfFrames == 3
    assert ds.Rows == 8 and ds.Columns == 8
    assert float(ds.WindowWidth) == 200.0
    assert float(ds.WindowCenter) == 50.0
    assert ds.PhotometricInterpretation == "MONOCHROME2"
    assert str(ds.file_meta.TransferSyntaxUID) == ExplicitVRLittleEndian
    frame_bytes = 8 * 8 * 2
    # every frame has the 2x2 region zeroed
    for f in range(3):
        b = ds.PixelData[f * frame_bytes:(f + 1) * frame_bytes]
        assert b[0:2] == b"\x00\x00"
        assert b[2:4] == b"\x00\x00"
        assert b[16:18] == b"\x00\x00"
        assert b[18:20] == b"\x00\x00"
        # outside the region untouched (non-zero gradient)
        assert b[4:6] != b"\x00\x00"
    # de-identification method records the pixel cleaning option
    methods = "\\".join(str(v) for v in ds.DeidentificationMethod)
    assert "Manual Rectangular Masks" in methods
    assert any(str(item.CodeValue) == "113110"
               for item in ds.DeidentificationMethodCodeSequence)

    # report: only paths / frame range / rectangle counts (no coordinates)
    report = json.loads(zf.read("anonymization-report.json"))
    section = report["pixel_masks"]
    assert len(section) == 1
    entry = section[0]
    assert entry["output_path"].endswith(".dcm")
    assert entry["source_relative_path"] == "study/a.dcm"
    assert entry["frame_count_total"] == 3
    assert entry["regions"] == [{
        "frame_start": 1, "frame_end": 3,
        "frame_count": 3, "rectangle_count": 1,
    }]
    raw = json.dumps(report)
    assert '"x"' not in raw and '"y"' not in raw
    # gate summary still present
    assert report["residue_identifier_gate"]["passed"] is True


@pytest.mark.asyncio
async def test_current_frame_scope_only_fills_one_frame(client):
    job = await _upload(client, [("mf.dcm", _dicom_bytes(frames=4))])
    body = {"profile": "strict", "pixel_masks": [{
        "file_index": 0, "frame_start": 1, "frame_end": 1,
        "rectangles": [{"x": 0, "y": 0, "width": 1, "height": 1}],
    }]}
    r = await client.post(f"/api/jobs/{job}/process", json=body)
    assert r.status_code == 200, r.text
    z = await client.get(r.json()["download_url"])
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    ds = pydicom.dcmread(io.BytesIO(zf.read(
        [n for n in zf.namelist() if n.endswith(".dcm")][0])))
    frame_bytes = 8 * 8 * 2
    assert ds.PixelData[0:2] != b"\x00\x00"            # frame 0 untouched
    assert ds.PixelData[frame_bytes:frame_bytes + 2] == b"\x00\x00"
    assert ds.PixelData[2 * frame_bytes:2 * frame_bytes + 2] != b"\x00\x00"


@pytest.mark.asyncio
async def test_compressed_mask_blocks_packaging(client):
    job = await _upload(client, [("jpg.dcm",
                                  _dicom_bytes(compressed=True))])
    body = {"profile": "recommended", "pixel_masks": [{
        "file_index": 0,
        "rectangles": [{"x": 0, "y": 0, "width": 1, "height": 1}],
    }]}
    r = await client.post(f"/api/jobs/{job}/process", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    err = detail["pixel_mask_errors"][0]
    assert err["code"] == "compressed_transfer_syntax"
    assert err["source_path"] == "jpg.dcm"

    status = await client.get(f"/api/jobs/{job}/status")
    assert status.json()["status"] in ("error", "uploaded")
    dl = await client.get(f"/api/jobs/{job}/download")
    assert dl.status_code == 409


@pytest.mark.asyncio
async def test_out_of_bounds_rectangle_blocks(client):
    job = await _upload(client, [("a.dcm", _dicom_bytes(rows=8, cols=8))])
    body = {"profile": "strict", "pixel_masks": [{
        "file_index": 0,
        "rectangles": [{"x": 6, "y": 6, "width": 5, "height": 5}],
    }]}
    r = await client.post(f"/api/jobs/{job}/process", json=body)
    assert r.status_code == 422
    err = r.json()["detail"]["pixel_mask_errors"][0]
    assert err["code"] == "rect_out_of_range"


@pytest.mark.asyncio
async def test_preview_with_invalid_mask_returns_422(client):
    job = await _upload(client, [("jpg.dcm",
                                  _dicom_bytes(compressed=True))])
    r = await client.post(f"/api/jobs/{job}/preview", json={
        "profile": "strict",
        "pixel_masks": [{"file_index": 0, "rectangles": [
            {"x": 0, "y": 0, "width": 1, "height": 1}]}],
    })
    assert r.status_code == 422
    assert r.json()["detail"]["pixel_mask_errors"][0][
        "code"] == "compressed_transfer_syntax"


@pytest.mark.asyncio
async def test_mask_on_non_dicom_index_blocks(client):
    up = await client.post(
        "/api/jobs",
        files=[("files", ("notes.txt", b"not dicom", "text/plain"))],
    )
    job = up.json()["job_id"]
    r = await client.post(f"/api/jobs/{job}/process", json={
        "profile": "strict",
        "pixel_masks": [{"file_index": 0, "rectangles": [
            {"x": 0, "y": 0, "width": 1, "height": 1}]}],
    })
    assert r.status_code == 422
    assert r.json()["detail"]["pixel_mask_errors"][0]["code"] == "not_dicom"


@pytest.mark.asyncio
async def test_rgb_mask_preserves_color_attributes(client):
    job = await _upload(client, [(
        "rgb.dcm", _dicom_bytes(spp=3, photometric="RGB", bits=8,
                                planar=0))])
    r = await client.post(f"/api/jobs/{job}/process", json={
        "profile": "strict",
        "pixel_masks": [{"file_index": 0, "rectangles": [
            {"x": 0, "y": 0, "width": 2, "height": 1}]}],
    })
    assert r.status_code == 200, r.text
    z = await client.get(r.json()["download_url"])
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    ds = pydicom.dcmread(io.BytesIO(zf.read(
        [n for n in zf.namelist() if n.endswith(".dcm")][0])))
    assert ds.PhotometricInterpretation == "RGB"
    assert ds.SamplesPerPixel == 3
    assert ds.PlanarConfiguration == 0
    assert bytes(ds.PixelData[0:6]) == b"\x00" * 6


@pytest.mark.asyncio
async def test_run_without_masks_unchanged(client):
    job = await _upload(client, [("a.dcm", _dicom_bytes())])
    r = await client.post(f"/api/jobs/{job}/process",
                          json={"profile": "recommended"})
    assert r.status_code == 200, r.text
    assert r.json()["pixel_masks"] == []


@pytest.mark.asyncio
async def test_failed_mask_run_blocks_previous_zip(client):
    # A clean run first produces a downloadable ZIP...
    job = await _upload(client, [
        ("a.dcm", _dicom_bytes()),
        ("z.dcm", _dicom_bytes(compressed=True)),
    ])
    r = await client.post(f"/api/jobs/{job}/process",
                          json={"profile": "recommended"})
    assert r.status_code == 200
    first_url = r.json()["download_url"]
    assert (await client.get(first_url)).status_code == 200

    # ...then a run with an un-applyable mask must invalidate it.
    r = await client.post(f"/api/jobs/{job}/process", json={
        "profile": "recommended",
        "pixel_masks": [{"file_index": 1, "rectangles": [
            {"x": 0, "y": 0, "width": 1, "height": 1}]}],
    })
    assert r.status_code == 422
    status = await client.get(f"/api/jobs/{job}/status")
    assert status.json()["status"] == "error"
    dl = await client.get(first_url)
    assert dl.status_code == 409
