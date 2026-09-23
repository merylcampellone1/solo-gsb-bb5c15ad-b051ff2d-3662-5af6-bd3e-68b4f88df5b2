"""FastAPI application: REST endpoints + static single-page workbench."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, pixel_mask
from .dicom_catalog import DEFAULT_PROFILE, PROFILES
from .jobs import STORE, MaskApplyFailed, _build_mask_groups, resolve_options, run
from .residue_gate import GateBlocked
from .schemas import (
    AnonymizationOptions,
    MaskPreviewRequest,
    ThumbnailRequest,
)

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
    masks = _build_mask_groups(opts_model.pixel_masks)
    return job, profile_name, profile_label, options, salt, masks


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
        "pixel_masks": payload.get("pixel_masks", []),
    }


@app.post("/api/jobs/{job_id}/preview")
def preview(job_id: str, payload: AnonymizationOptions) -> dict:
    job, profile_name, profile_label, options, salt, masks = _prepare(
        job_id, payload.model_dump())
    try:
        result = run(job, profile_name, options, salt, write=False,
                     masks_by_file=masks)
    except MaskApplyFailed as failed:
        raise HTTPException(status_code=422, detail={
            "message": "人工像素遮罩无法应用，已阻止后续打包。",
            "pixel_mask_errors": failed.errors,
        })
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _serialize(result, job_id, profile_label, options)


@app.post("/api/jobs/{job_id}/process")
def process(job_id: str, payload: AnonymizationOptions) -> dict:
    job, profile_name, profile_label, options, salt, masks = _prepare(
        job_id, payload.model_dump())

    # A synchronous route runs in the FastAPI threadpool, so large studies
    # do not block the event loop (preview/status/download stay responsive).
    try:
        run(job, profile_name, options, salt, write=True,
            masks_by_file=masks)
    except MaskApplyFailed as failed:
        raise HTTPException(status_code=422, detail={
            "message": str(failed),
            "pixel_mask_errors": failed.errors,
        })
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

    if job.status == "error":
        raise HTTPException(status_code=400,
                            detail=job.error or "匿名化处理失败。")
    return {
        "job_id": job_id,
        "status": "done",
        "download_url": f"/api/jobs/{job_id}/download",
        "zip_filename": job.zip_filename,
        "files": job.last_result["files"] if job.last_result else [],
        "warnings": job.last_result["warnings"] if job.last_result else [],
        "counts": job.last_result["counts"] if job.last_result else {},
        "gate": job.last_result.get("gate") if job.last_result else None,
        "pixel_masks": (job.last_result.get("pixel_masks")
                        if job.last_result else []),
    }


def _get_job_or_404(job_id: str):
    try:
        return STORE.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在或已过期。")


def _load_pixel_source(job, file_index: int):
    if file_index < 0 or file_index >= len(job.sources):
        raise HTTPException(
            status_code=404,
            detail=f"文件序号 {file_index + 1} 不存在（共 "
                   f"{len(job.sources)} 个上传文件）。")
    try:
        ds, layout = pixel_mask.load_for_mask(job.sources[file_index])
    except pixel_mask.PixelMaskError as exc:
        exc.file_index = file_index
        exc.source_path = job.relpaths[file_index]
        raise HTTPException(status_code=422, detail={
            "message": "该影像无法用于像素遮罩。",
            "pixel_mask_errors": [exc.as_dict()],
        })
    return ds, layout


@app.post("/api/jobs/{job_id}/thumbnail")
def thumbnail(job_id: str, payload: ThumbnailRequest) -> dict:
    """Render one frame of an uploaded image as a PNG data URL."""
    job = _get_job_or_404(job_id)
    ds, layout = _load_pixel_source(job, payload.file_index)
    try:
        return pixel_mask.render_thumbnail(ds, layout, payload.frame)
    except pixel_mask.PixelMaskError as exc:
        exc.file_index = payload.file_index
        exc.source_path = job.relpaths[payload.file_index]
        raise HTTPException(status_code=422, detail={
            "message": "缩略图渲染失败。",
            "pixel_mask_errors": [exc.as_dict()],
        })


@app.post("/api/jobs/{job_id}/mask-preview")
def mask_preview(job_id: str, payload: MaskPreviewRequest) -> dict:
    """Render a frame with red outlines showing the active rectangles."""
    job = _get_job_or_404(job_id)
    ds, layout = _load_pixel_source(job, payload.file_index)
    rects = [pixel_mask.Rect(x=r.x, y=r.y, width=r.width, height=r.height)
             for r in payload.rectangles]
    try:
        return pixel_mask.render_mask_preview(ds, layout, payload.frame,
                                              rects)
    except pixel_mask.PixelMaskError as exc:
        exc.file_index = payload.file_index
        exc.source_path = job.relpaths[payload.file_index]
        raise HTTPException(status_code=422, detail={
            "message": "遮罩预览失败。",
            "pixel_mask_errors": [exc.as_dict()],
        })


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
        # Includes failed manual-pixel-mask runs: packaging was blocked.
        raise HTTPException(
            status_code=409,
            detail=job.error or "处理失败，ZIP 未生成；请修正后重新执行。")
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
