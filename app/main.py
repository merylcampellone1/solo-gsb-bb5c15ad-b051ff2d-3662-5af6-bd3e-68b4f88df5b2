"""FastAPI application: REST endpoints + static single-page workbench."""
from __future__ import annotations

import base64
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config, pixel_masks
from .dicom_catalog import DEFAULT_PROFILE, PROFILES
from .jobs import STORE, _attach_masks, resolve_options, run
from .residue_gate import GateBlocked
from .schemas import AnonymizationOptions, MaskPreviewRequest

app = FastAPI(
    title="DICOM 检查匿名化工作台",
    description="上传影像 → 选择匿名化配置 → 预览逐标签变更 → 打包下载",
    version="1.0.0",
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/profiles")
def list_profiles() -> dict:
    return {"default": DEFAULT_PROFILE, "profiles": PROFILES}


@app.post("/api/jobs")
async def create_job(files: list[UploadFile] = File(...)) -> dict:
    if not files:
        raise HTTPException(status_code=400, detail="未收到任何文件。")

    pairs: list[tuple[str, bytes]] = []
    total = 0
    for upload in files:
        data = await upload.read()
        total += len(data)
        if config.MAX_UPLOAD_BYTES and total > config.MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"上传总量超过限制 "
                       f"({config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB)。")
        pairs.append((upload.filename or "unnamed", data))

    job = STORE.create(pairs)
    return {
        "job_id": job.id,
        "file_count": len(pairs),
        "files": [
            {"index": i, "source_path": rel}
            for i, rel in enumerate(job.relpaths)
        ],
    }


def _prepare(job_id: str, payload: dict) -> tuple:
    try:
        job = STORE.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在或已过期。")

    opts_model = AnonymizationOptions(**payload)
    try:
        profile_name, options = resolve_options(
            opts_model.profile, opts_model.overrides())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    salt = opts_model.salt or config.PSEUDONYM_SALT
    profile_label = PROFILES[profile_name]["label"]
    return job, profile_name, profile_label, options, salt


def _serialize(payload: dict, job_id: str, profile_label: str,
               options: dict) -> dict:
    return {
        "job_id": job_id,
        "profile_label": profile_label,
        "options": options,
        "files": payload["files"],
        "changes": payload["changes"],
        "warnings": payload["warnings"],
        "counts": payload["counts"],
        "patients": payload["patients"],
        "gate": payload.get("gate"),
        "pixel_masks": payload.get("pixel_masks"),
        "mask_errors": payload.get("mask_errors", []),
    }


@app.post("/api/jobs/{job_id}/preview")
def preview(job_id: str, payload: AnonymizationOptions) -> dict:
    job, profile_name, profile_label, options, salt = _prepare(
        job_id, payload.model_dump())
    try:
        result = run(job, profile_name, options, salt, write=False,
                     pixel_masks_payload=payload.pixel_masks)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _serialize(result, job_id, profile_label, options)


@app.post("/api/jobs/{job_id}/process")
def process(job_id: str, payload: AnonymizationOptions) -> dict:
    job, profile_name, profile_label, options, salt = _prepare(
        job_id, payload.model_dump())

    # A synchronous route runs in the FastAPI threadpool, so large studies
    # do not block the event loop (preview/status/download stay responsive).
    try:
        run(job, profile_name, options, salt, write=True,
            pixel_masks_payload=payload.pixel_masks)
    except pixel_masks.MaskBlocked as blocked:
        raise HTTPException(
            status_code=422,
            detail={
                "message": str(blocked),
                "pixel_mask_errors": blocked.entries,
            },
        )
    except GateBlocked as blocked:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(blocked),
                "residue_identifier_gate": blocked.result.as_dict(),
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "job_id": job_id,
        "status": "done",
        "download_url": f"/api/jobs/{job_id}/download",
        "zip_filename": job.zip_filename,
        "files": job.last_result["files"] if job.last_result else [],
        "warnings": job.last_result["warnings"] if job.last_result else [],
        "counts": job.last_result["counts"] if job.last_result else {},
        "gate": job.last_result.get("gate") if job.last_result else None,
        "pixel_masks": (job.last_result or {}).get("pixel_masks"),
    }


@app.get("/api/jobs/{job_id}/files/{file_index}/thumbnail")
def thumbnail(job_id: str, file_index: int, frame: int = 1):
    """Return a down-scaled PNG of one *output* frame.

    The dataset is metadata-anonymised in memory (so the user sees exactly
    what the ZIP would contain) but pixel masks are not applied here -- the
    editor overlays rectangles client-side.
    """
    job = _get_job(job_id)
    fi, ds, geom = _anonymised_frame(job, file_index, frame)
    try:
        rgb, _ = pixel_masks.render_frame_rgb(ds, frame - 1)
    except pixel_masks.MaskUnsupported as exc:
        raise HTTPException(
            status_code=422,
            detail={"file_index": file_index,
                    "source_path": fi.source_path, "message": str(exc)})
    except pixel_masks.MaskValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    image = pixel_masks.downscale(rgb, MAX_THUMBNAIL_EDGE)
    png = pixel_masks.encode_png(image)
    return Response(
        content=png, media_type="image/png",
        headers={
            "X-Dicom-Rows": str(geom.rows),
            "X-Dicom-Columns": str(geom.columns),
            "X-Dicom-Frames": str(geom.frame_count),
            "X-Dicom-Frame": str(frame),
            "X-Thumbnail-Width": str(image.shape[1]),
            "X-Thumbnail-Height": str(image.shape[0]),
            "X-Photometric-Interpretation": geom.photometric,
            "Cache-Control": "no-store",
        })


@app.post("/api/jobs/{job_id}/files/{file_index}/mask-preview")
def mask_preview(job_id: str, file_index: int, payload: MaskPreviewRequest):
    """Apply the submitted masks to one frame and return the rendered PNG.

    Runs the full metadata anonymisation in memory first, so the preview
    matches the packaged output byte-for-byte (masked regions included).
    """
    job = _get_job(job_id)
    frame = payload.frame
    matching = [m for m in payload.pixel_masks if m.file_index == file_index]
    fi, ds, geom = _anonymised_frame(
        job, file_index, frame, masks=matching,
        profile=payload.profile, options_payload=payload)
    try:
        rgb, _ = pixel_masks.render_frame_rgb(ds, frame - 1)
    except pixel_masks.MaskUnsupported as exc:
        raise HTTPException(
            status_code=422,
            detail={"file_index": file_index,
                    "source_path": fi.source_path, "message": str(exc)})
    image = pixel_masks.downscale(rgb, MAX_PREVIEW_EDGE)
    png = pixel_masks.encode_png(image)
    rect_count = sum(len(g.rectangles)
                     for m in matching for g in m.groups)
    return {
        "job_id": job_id,
        "file_index": file_index,
        "source_path": fi.source_path,
        "frame": frame,
        "frame_count": geom.frame_count,
        "rows": geom.rows,
        "columns": geom.columns,
        "rectangle_count": rect_count,
        "photometric_interpretation": geom.photometric,
        "image_width": image.shape[1],
        "image_height": image.shape[0],
        "image_png_base64": base64.b64encode(png).decode("ascii"),
    }


def _get_job(job_id: str):
    try:
        return STORE.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在或已过期。")


def _anonymised_frame(job, file_index: int, frame: int,
                      masks=None, profile: str | None = None,
                      options_payload=None):
    """Metadata-anonymise a single source file in memory and return
    ``(FileInfo-like, dataset, geometry)``.  When ``masks`` are given they are
    applied to the anonymised dataset (mask preview)."""
    from .anonymizer import Anonymizer, FileInfo

    if not 0 <= file_index < len(job.sources):
        raise HTTPException(status_code=404,
                            detail=f"文件序号 {file_index} 不存在。")
    if profile is not None and options_payload is not None:
        model = AnonymizationOptions(
            **options_payload.model_dump(exclude={"pixel_masks"}))
        try:
            profile_name, options = resolve_options(
                model.profile, model.overrides())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        salt = model.salt or config.PSEUDONYM_SALT
    else:
        profile_name, options = resolve_options(
            DEFAULT_PROFILE, None)
        salt = config.PSEUDONYM_SALT

    rel = job.relpaths[file_index]
    fi = FileInfo(index=file_index, source_path=rel)
    anon = Anonymizer(options=options, salt=salt)
    infos = [fi]
    anon.prescan(infos, lambda info: job.sources[info.index])
    if not fi.is_dicom:
        raise HTTPException(status_code=422,
                            detail={"file_index": file_index,
                                    "source_path": rel,
                                    "message": fi.error
                                    or "不是可匿名化的 DICOM 文件。"})
    if not fi.maskable:
        raise HTTPException(
            status_code=422,
            detail={"file_index": file_index, "source_path": rel,
                    "message": fi.mask_reason or "该影像不支持像素遮罩。"})
    if not 1 <= frame <= (fi.frame_count or 1):
        raise HTTPException(
            status_code=400,
            detail=f"帧号 {frame} 超出总帧数 {fi.frame_count}。")
    if masks:
        errors = _attach_masks(infos, masks)
        if errors:
            raise HTTPException(
                status_code=400,
                detail={"message": "遮罩参数无效。",
                        "pixel_mask_errors": errors})
    ds = anon.apply_file(fi, job.sources[file_index], None)
    if ds is None:
        raise HTTPException(status_code=422,
                            detail={"file_index": file_index,
                                    "source_path": rel,
                                    "message": fi.error
                                    or "影像无法解码。"})
    try:
        geom = pixel_masks.inspect_image(ds)
    except pixel_masks.MaskUnsupported as exc:
        raise HTTPException(
            status_code=422,
            detail={"file_index": file_index, "source_path": rel,
                    "message": str(exc)})
    return fi, ds, geom


MAX_THUMBNAIL_EDGE = 640
MAX_PREVIEW_EDGE = 720


@app.get("/api/jobs/{job_id}/status")
def job_status(job_id: str) -> dict:
    try:
        job = STORE.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在或已过期。")
    return {
        "job_id": job_id,
        "status": job.status,
        "error": job.error,
        "download_url": (f"/api/jobs/{job_id}/download"
                         if job.status == "done" else None),
        "zip_filename": job.zip_filename,
        "gate": job.last_gate,
    }


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str):
    try:
        job = STORE.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在或已过期。")
    if job.status == "blocked":
        raise HTTPException(
            status_code=409,
            detail="残留标识门禁未通过，ZIP 未生成；请调整匿名化选项后重新执行。")
    if job.status == "error":
        raise HTTPException(
            status_code=409,
            detail=job.error or "匿名化处理失败，ZIP 未生成。")
    if job.status != "done" or not job.zip_path or \
            not job.zip_path.exists():
        raise HTTPException(status_code=409,
                            detail="ZIP 尚未生成，请先执行匿名化。")
    return FileResponse(
        job.zip_path,
        media_type="application/zip",
        filename=job.zip_filename or "anonymized.zip",
    )


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    STORE.remove(job_id)
    return {"status": "deleted"}


if STATIC_DIR.exists():
    app.mount("/static",
              StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")
