"""HTTP API 契约测试：mock 掉 run_bill_agent，不触发 OCR/LLM。"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bill_agent.api import app as app_module
from bill_agent.api import jobs

SAMPLE_RECORD = {
    "operation": "CREATE",
    "document_info": {
        "document_type": "invoice",
        "reference_no": "INV-001",
        "document_date": "2026-01-01",
    },
    "transaction_party": {"issuer": "发货人", "recipient": "收货人"},
    "charge_details": {
        "charge_item": "Ocean Freight",
        "original_charge_item": "O/F",
        "amount": 1200.0,
        "currency": "USD",
        "payment_term": "PP",
        "quantity": 1,
        "unit_price": 1200.0,
    },
    "financial_attributes": {"accounting_entity": "测试公司", "posting_type": "AR"},
    "audit_trail": {
        "source_file": "账单.pdf",
        "original_text": "OCEAN FREIGHT USD 1200.00",
        "ai_reasoning": ["未匹配到已有账单，新增记录"],
    },
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path / "jobs")
    return TestClient(app_module.app)


def _wait(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/jobs/{job_id}").json()
        if data["status"] in ("succeeded", "failed"):
            return data
        time.sleep(0.02)
    raise AssertionError("任务未在超时时间内结束")


def _pdf(name: str = "账单.pdf") -> tuple:
    return ("files", (name, b"%PDF-1.4\n%test", "application/pdf"))


def test_create_poll_result_and_download(client, monkeypatch):
    calls: dict = {}

    def fake_run(files, bills, entity):
        calls["files"] = files
        calls["bills"] = bills
        calls["entity"] = entity
        return [SAMPLE_RECORD]

    monkeypatch.setattr(jobs, "run_bill_agent", fake_run)

    bills = json.dumps([{"bill_id": "B1", "amount": 100}]).encode("utf-8")
    res = client.post(
        "/api/jobs",
        files=[_pdf(), ("bills", ("existing.json", bills, "application/json"))],
        data={"accounting_entity": "测试公司"},
    )
    assert res.status_code == 202
    job_id = res.json()["job_id"]

    job = _wait(client, job_id)
    assert job["status"] == "succeeded"
    assert job["summary"] == {
        "total": 1, "create": 1, "update": 0, "no_change": 0, "pending_review": 0,
    }
    assert job["files"] == ["账单.pdf"]
    assert calls["entity"] == "测试公司"
    assert calls["bills"] == [{"bill_id": "B1", "amount": 100}]
    assert calls["files"][0].endswith("账单.pdf")

    result = client.get(f"/api/jobs/{job_id}/result")
    assert result.status_code == 200
    assert result.json() == [SAMPLE_RECORD]

    download = client.get(f"/api/jobs/{job_id}/download")
    assert download.status_code == 200
    assert download.headers["content-disposition"] == (
        f'attachment; filename="bill_result_{job_id}.json"'
    )
    assert "charset=utf-8" in download.headers["content-type"]
    assert json.loads(download.content) == [SAMPLE_RECORD]


def test_empty_bills_and_entity_optional(client, monkeypatch):
    seen: dict = {}

    def fake_run(files, bills, entity):
        seen["bills"] = bills
        seen["entity"] = entity
        return []

    monkeypatch.setattr(jobs, "run_bill_agent", fake_run)

    res = client.post("/api/jobs", files=[_pdf()])
    assert res.status_code == 202
    job = _wait(client, res.json()["job_id"])
    assert job["status"] == "succeeded"
    assert job["summary"]["total"] == 0
    assert seen == {"bills": None, "entity": None}


def test_reject_bad_extension(client):
    res = client.post(
        "/api/jobs",
        files=[("files", ("malware.exe", b"MZ", "application/octet-stream"))],
    )
    assert res.status_code == 400
    assert "不支持" in res.json()["detail"]


def test_reject_empty_file(client):
    res = client.post("/api/jobs", files=[("files", ("empty.pdf", b"", "application/pdf"))])
    assert res.status_code == 400
    assert "为空" in res.json()["detail"]


def test_reject_bad_bills_json(client):
    res = client.post(
        "/api/jobs",
        files=[_pdf(), ("bills", ("bad.json", b"{not json", "application/json"))],
    )
    assert res.status_code == 400
    assert "JSON" in res.json()["detail"]


def test_bills_must_be_object_array(client):
    res = client.post(
        "/api/jobs",
        files=[_pdf(), ("bills", ("obj.json", b'{"a": 1}', "application/json"))],
    )
    assert res.status_code == 400
    assert "对象数组" in res.json()["detail"]


def test_unknown_job_returns_404(client):
    assert client.get("/api/jobs/deadbeef").status_code == 404
    assert client.get("/api/jobs/deadbeef/result").status_code == 404
    assert client.get("/api/jobs/deadbeef/download").status_code == 404
    assert client.delete("/api/jobs/deadbeef").status_code == 404


def test_failed_job_reports_error(client, monkeypatch):
    def boom(files, bills, entity):
        raise RuntimeError("LLM 额度不足")

    monkeypatch.setattr(jobs, "run_bill_agent", boom)

    res = client.post("/api/jobs", files=[_pdf()])
    job = _wait(client, res.json()["job_id"])
    assert job["status"] == "failed"
    assert "额度不足" in job["error"]

    assert client.get(f"/api/jobs/{job['job_id']}/result").status_code == 409
    assert client.get(f"/api/jobs/{job['job_id']}/download").status_code == 409


def test_failed_job_error_redacts_url_query(client, monkeypatch):
    """任务失败的 error 字段若带 URL query（token/预签名），进前端前必须脱敏。"""
    def boom(files, bills, entity):
        raise RuntimeError("boom https://gateway.pltplt.com/ai/process?api_token=SECRETTOKEN1")

    monkeypatch.setattr(jobs, "run_bill_agent", boom)

    res = client.post("/api/jobs", files=[_pdf()])
    job = _wait(client, res.json()["job_id"])
    assert job["status"] == "failed"
    assert "gateway.pltplt.com/ai/process" in job["error"], "真实原因保留"
    assert "api_token" not in job["error"] and "SECRETTOKEN1" not in job["error"]


def test_failed_job_error_redacts_temp_path(client, monkeypatch):
    """error 字段带本机临时附件路径（含空格用户名）时，进前端前必须脱敏。"""
    def boom(files, bills, entity):
        raise RuntimeError(
            r"附件解析失败: C:\Users\Zhang Wei\AppData\Local\Temp\bill_att_ab12cd34\data.bin"
        )

    monkeypatch.setattr(jobs, "run_bill_agent", boom)

    res = client.post("/api/jobs", files=[_pdf()])
    job = _wait(client, res.json()["job_id"])
    assert job["status"] == "failed"
    assert "bill_att_" not in job["error"] and "Zhang Wei" not in job["error"]


def test_budget_resets_between_jobs(monkeypatch, tmp_path):
    """线程池复用同一线程连续跑任务：预算计数必须按任务重置，
    否则前序任务消耗会让本任务被误报「预算不足」。"""
    from bill_agent.harness import budget as budget_mod

    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path / "jobs")
    store = jobs.JobStore(max_workers=1)  # 单线程：两次任务必然复用同一线程
    seen: list[int] = []

    def fake_run(files, bills, entity):
        seen.append(budget_mod.get_budget().tokens)
        budget_mod.get_budget().record_tokens(199_990)  # 把预算烧到接近上限
        return []

    monkeypatch.setattr(jobs, "run_bill_agent", fake_run)

    for _ in range(2):
        job = store.submit(jobs.new_job_id(), [str(tmp_path / "x.pdf")], None, None)
        deadline = time.time() + 5
        while time.time() < deadline and job.status not in ("succeeded", "failed"):
            time.sleep(0.02)
        assert job.status == "succeeded", job.error

    assert seen == [0, 0], f"预算跨任务累计了: {seen}"


def test_result_409_while_running(client, monkeypatch):
    import threading

    gate = threading.Event()

    def slow(files, bills, entity):
        gate.wait(5)
        return []

    monkeypatch.setattr(jobs, "run_bill_agent", slow)

    res = client.post("/api/jobs", files=[_pdf()])
    job_id = res.json()["job_id"]
    assert client.get(f"/api/jobs/{job_id}/result").status_code == 409
    gate.set()
    assert _wait(client, job_id)["status"] == "succeeded"


def test_delete_removes_job(client, monkeypatch):
    monkeypatch.setattr(jobs, "run_bill_agent", lambda f, b, e: [])
    res = client.post("/api/jobs", files=[_pdf()])
    job_id = res.json()["job_id"]
    _wait(client, job_id)

    assert client.delete(f"/api/jobs/{job_id}").status_code == 200
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    assert not (jobs.JOBS_DIR / job_id).exists()


def test_filename_is_sanitized(client, monkeypatch):
    monkeypatch.setattr(jobs, "run_bill_agent", lambda f, b, e: [])
    res = client.post(
        "/api/jobs",
        files=[("files", ("../../evil.pdf", b"%PDF", "application/pdf"))],
    )
    assert res.status_code == 202
    job = _wait(client, res.json()["job_id"])
    assert job["files"] == ["evil.pdf"]
    assert (jobs.JOBS_DIR / job["job_id"] / "evil.pdf").is_file()


def test_sanitize_filename_handles_windows_reserved_and_trailing_dots():
    """Windows 保留设备名与尾点/尾空格会落盘失败或静默吞数据（与 EML 附件同一规则）。"""
    assert jobs.sanitize_filename("CON.pdf") == "_CON.pdf"
    assert jobs.sanitize_filename("nul.xlsx") == "_nul.xlsx"
    assert jobs.sanitize_filename("COM1") == "_COM1"
    assert jobs.sanitize_filename("LPT9.doc") == "_LPT9.doc"
    # 'CON .pdf' 在 Windows 上同样解析为设备名：加前缀即可（空格本身合法）
    assert jobs.sanitize_filename("CON .pdf") == "_CON .pdf"
    assert jobs.sanitize_filename("报表.pdf. ") == "报表.pdf"
    assert jobs.sanitize_filename("报表 .pdf") == "报表 .pdf", "内部空格是合法的，保留"
    assert jobs.sanitize_filename("../../evil.pdf") == "evil.pdf"


def test_upload_write_failure_hides_job_dir(client, monkeypatch, tmp_path):
    """落盘失败的 500 详情不得回显任务目录绝对路径（响应只暴露上传文件名）。"""
    real_open = Path.open

    def boom(self, *args, **kwargs):
        if self.suffix == ".pdf":
            raise PermissionError(f"cannot write {self}")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", boom)
    res = client.post("/api/jobs", files=[_pdf()])

    assert res.status_code == 500
    detail = res.json()["detail"]
    assert "<任务目录>" in detail, detail
    assert str(jobs.JOBS_DIR) not in detail, detail
    assert str(tmp_path) not in detail, detail
    assert not any((tmp_path / "jobs").iterdir()), "失败上传的任务目录必须清理"


def test_staged_paths_are_rewritten_to_upload_names(client, monkeypatch):
    staged: dict = {}

    def fake_run(files, bills, entity):
        staged["path"] = files[0]
        return [{
            **SAMPLE_RECORD,
            "audit_trail": {
                "source_file": files[0],
                "original_text": f"=== [pdf_text] {files[0]} ===\nOCEAN FREIGHT 1200",
                "ai_reasoning": [f"解析失败（{files[0]}）：无法识别的文件类型"],
            },
        }]

    monkeypatch.setattr(jobs, "run_bill_agent", fake_run)

    res = client.post("/api/jobs", files=[_pdf("发票.pdf")])
    job = _wait(client, res.json()["job_id"])
    record = client.get(f"/api/jobs/{job['job_id']}/result").json()[0]

    assert record["audit_trail"]["source_file"] == "发票.pdf"
    assert record["audit_trail"]["original_text"].startswith("=== [pdf_text] 发票.pdf ===")
    assert staged["path"] not in record["audit_trail"]["original_text"]
    # 审计轨迹里的落盘路径同样要还原（解析失败记录的 reasoning 含路径）
    assert record["audit_trail"]["ai_reasoning"] == ["解析失败（发票.pdf）：无法识别的文件类型"]


def test_index_and_health(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "航运智能账单助手" in page.text

    health = client.get("/api/health").json()
    assert health["status"] == "ok"
    assert isinstance(health["ocr_ready"], bool)
