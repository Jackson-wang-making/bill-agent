"""任务编排：上传落盘、后台执行、状态机、TTL 清理。

进程内单例；任务不持久化，重启即清空（见《前端与API规格.md》D5）。
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bill_agent.graph.nodes.parse import _clean_error
from bill_agent.harness.budget import reset_budget
from bill_agent.main import run_bill_agent

JOBS_DIR = Path(os.getenv("BILL_AGENT_JOBS_DIR", "output/api_jobs"))
JOB_TTL_SECONDS = 30 * 60
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024

# 与 parsers/dispatcher.py 的扩展名路由保持一致
ALLOWED_EXT = {
    ".eml", ".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff",
    ".xls", ".xlsx", ".doc", ".docx",
}

_UNSAFE = re.compile(r'[<>:"|?*\x00-\x1f]')

# Windows 保留设备名：上传名为 CON.pdf / NUL.xlsx 时落盘会失败或静默吞数据。
# 与 EML 附件落盘同一规则（eml._safe_filename）：加前缀后就是普通文件名。
_WIN_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _rewrite_line(line: Any, mapping: dict[str, str]) -> Any:
    """审计轨迹条目若引用落盘路径，还原成上传文件名（仅字符串替换）。"""
    if not isinstance(line, str):
        return line
    for staged, name in mapping.items():
        line = line.replace(staged, name)
    return line


def _rewrite_sources(records: list[dict[str, Any]], files: list[str]) -> list[dict[str, Any]]:
    """把落盘路径还原成用户上传时的文件名，下载结果里不出现服务端目录。

    `source_file` 与 `original_text` 头部由解析层写入，两者都要换。
    """
    mapping: dict[str, str] = {}
    for path in files:
        staged = str(Path(path))
        name = Path(path).name
        for variant in (staged, staged.replace("\\", "/"), staged.replace("/", "\\")):
            mapping[variant] = name

    for record in records:
        trail = record.get("audit_trail")
        if not isinstance(trail, dict):
            continue
        source = trail.get("source_file")
        if isinstance(source, str):
            trail["source_file"] = mapping.get(str(Path(source)), Path(source).name)
        text = trail.get("original_text")
        if isinstance(text, str):
            for staged, name in mapping.items():
                text = text.replace(staged, name)
            trail["original_text"] = text
        reasoning = trail.get("ai_reasoning")
        if isinstance(reasoning, list):
            trail["ai_reasoning"] = [
                _rewrite_line(line, mapping) for line in reasoning
            ]
    return records


def sanitize_filename(name: str) -> str:
    """只取文件名、去掉危险字符，保留中文；扩展名统一小写。

    Windows 上还有两类名字会落盘失败或指向错误的文件：尾点/尾空格（被系统
    静默去掉，可能与另一个文件同名）与保留设备名（CON/NUL/COM1…，写 NUL
    会静默丢数据）。与 EML 附件落盘采用同一规则（eml._safe_filename）。
    """
    base = Path(str(name).replace("\\", "/")).name.strip()
    base = _UNSAFE.sub("_", base).replace("/", "_").rstrip(". ")
    if not base or base in (".", ".."):
        base = "upload"
    stem, ext = os.path.splitext(base)
    stem = (stem or "upload")[-80:]
    if stem.split(".")[0].strip().upper() in _WIN_RESERVED:
        stem = f"_{stem}"
    return f"{stem or 'upload'}{ext.lower()}"


@dataclass
class Job:
    job_id: str
    status: str = "queued"          # queued | running | succeeded | failed
    stage: str = "排队中"
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    error: str | None = None
    records: list[dict[str, Any]] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    @property
    def elapsed_ms(self) -> int:
        end = self.finished_at if self.finished_at is not None else time.time()
        return int((end - self.created_at) * 1000)

    def summary(self) -> dict[str, int]:
        counts = {"CREATE": 0, "UPDATE": 0, "NO_CHANGE": 0, "PENDING_REVIEW": 0}
        for record in self.records:
            op = record.get("operation")
            if op in counts:
                counts[op] += 1
        return {
            "total": len(self.records),
            "create": counts["CREATE"],
            "update": counts["UPDATE"],
            "no_change": counts["NO_CHANGE"],
            "pending_review": counts["PENDING_REVIEW"],
        }

    def public(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "stage": self.stage,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "files": [Path(p).name for p in self.files],
            # 0 条记录也是合法结果；按状态判断，避免空列表被当成「还没跑完」
            "summary": self.summary() if self.status == "succeeded" else None,
        }


class JobStore:
    def __init__(self, max_workers: int = 2) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="bill-job")

    # ---- 查询 ----
    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    # ---- 提交 ----
    def submit(
        self,
        job_id: str,
        files: list[str],
        bills: list[dict[str, Any]] | None,
        entity: str | None,
    ) -> Job:
        job = Job(job_id=job_id, files=list(files))
        with self._lock:
            self._jobs[job_id] = job
        self._pool.submit(self._run, job, list(files), bills, entity)
        return job

    def _run(
        self,
        job: Job,
        files: list[str],
        bills: list[dict[str, Any]] | None,
        entity: str | None,
    ) -> None:
        job.status, job.stage = "running", "解析与提取中"
        try:
            # 线程池线程会连续跑多个任务：预算计数按任务重置，
            # 否则前序任务消耗的 token/OCR 次数会让本任务被误报「预算不足」
            reset_budget()
            job.records = _rewrite_sources(run_bill_agent(files, bills, entity), files)
            job.status, job.stage = "succeeded", "完成"
        except Exception as e:  # noqa: BLE001 - 任何失败都要变成可展示的状态，不能让线程静默死掉
            job.status, job.stage = "failed", "失败"
            # 异常原文可能带 URL query（预签名/token）或临时落盘路径，进前端前先脱敏
            job.error = _clean_error(str(e)) or type(e).__name__
        finally:
            job.finished_at = time.time()

    # ---- 删除 / 清理 ----
    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)
        return True

    def cleanup(self, ttl_seconds: int = JOB_TTL_SECONDS) -> int:
        """删除已结束且超过 TTL 的任务与临时目录。"""
        cutoff = time.time() - ttl_seconds
        expired = [
            jid
            for jid, job in list(self._jobs.items())
            if job.finished_at is not None and job.finished_at < cutoff
        ]
        for jid in expired:
            self.delete(jid)
        return len(expired)


store = JobStore()


def new_job_id() -> str:
    return uuid.uuid4().hex
