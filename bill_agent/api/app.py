"""FastAPI 应用：上传 → 后台任务 → 结果/下载。

启动：
  uv run uvicorn bill_agent.api.app:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from bill_agent.api import jobs
from bill_agent.harness.redact import clean_error
from bill_agent.parsers.stage import ocr_delivery_ready

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BILLS_BYTES = 5 * 1024 * 1024

app = FastAPI(
    title="航运智能账单助手",
    version="0.1.0",
    description="多格式单据 → 标准账单 JSON（复用 CLI 同一条 LangGraph）",
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    """ocr_ready=false 时页面提示：扫描件/图片会转人工，文本层 PDF 仍可用。"""
    return {"status": "ok", "ocr_ready": ocr_delivery_ready()}


@app.post("/api/jobs", status_code=202)
async def create_job(
    files: list[UploadFile] = File(...),
    bills: UploadFile | None = File(None),
    accounting_entity: str = Form(""),
) -> dict[str, str]:
    jobs.store.cleanup()
    if not files:
        raise HTTPException(status_code=400, detail="至少上传一个文件")

    bills_data = await _read_bills(bills)

    job_id = jobs.new_job_id()
    job_dir = jobs.JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    saved: list[str] = []
    total_bytes = 0
    try:
        for upload in files:
            name = jobs.sanitize_filename(upload.filename or "")
            if Path(name).suffix not in jobs.ALLOWED_EXT:
                raise HTTPException(status_code=400, detail=f"不支持的文件类型：{name}")

            dest = job_dir / name
            if dest.exists():
                dest = job_dir / f"{jobs.new_job_id()[:6]}_{name}"

            size = 0
            with dest.open("wb") as fh:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    total_bytes += len(chunk)
                    if size > jobs.MAX_FILE_BYTES:
                        raise HTTPException(status_code=413, detail=f"文件超过 50MB：{name}")
                    if total_bytes > jobs.MAX_TOTAL_BYTES:
                        raise HTTPException(status_code=413, detail="上传总大小超过 200MB")
                    fh.write(chunk)

            if size == 0:
                raise HTTPException(status_code=400, detail=f"文件为空：{name}")
            saved.append(str(dest))
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception as e:  # noqa: BLE001 - 落盘失败统一转 500，避免半截任务
        shutil.rmtree(job_dir, ignore_errors=True)
        # 响应不得暴露服务器绝对路径：先按审计标准脱敏，再把任务目录换成占位符
        detail = clean_error(str(e))
        for path in {str(job_dir), job_dir.as_posix()}:
            detail = detail.replace(path, "<任务目录>")
        raise HTTPException(status_code=500, detail=f"保存上传文件失败：{detail}") from e

    job = jobs.store.submit(job_id, saved, bills_data, accounting_entity.strip() or None)
    return {"job_id": job.job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    return _require(job_id).public()


@app.get("/api/jobs/{job_id}/result")
def get_result(job_id: str) -> JSONResponse:
    job = _require(job_id)
    if job.status != "succeeded":
        raise HTTPException(status_code=409, detail=f"任务尚未完成（{job.status}）")
    return JSONResponse(job.records)


@app.get("/api/jobs/{job_id}/download")
def download_result(job_id: str) -> Response:
    job = _require(job_id)
    if job.status != "succeeded":
        raise HTTPException(status_code=409, detail=f"任务尚未完成（{job.status}）")
    body = json.dumps(job.records, ensure_ascii=False, indent=2)
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="bill_result_{job_id}.json"',
        },
    )


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict[str, str]:
    if not jobs.store.delete(job_id):
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return {"deleted": job_id}


def _require(job_id: str) -> jobs.Job:
    job = jobs.store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return job


async def _read_bills(upload: UploadFile | None) -> list[dict[str, Any]] | None:
    if upload is None or not upload.filename:
        return None
    raw = await upload.read(MAX_BILLS_BYTES + 1)
    if len(raw) > MAX_BILLS_BYTES:
        raise HTTPException(status_code=413, detail="已有账单 JSON 超过 5MB")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=400, detail=f"已有账单 JSON 解析失败：{e}") from e
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise HTTPException(status_code=400, detail="已有账单 JSON 必须是对象数组")
    return data
