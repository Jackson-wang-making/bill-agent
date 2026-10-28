"""OCR 响应映射：用真实 API 响应做回归（本次 P0 的直接防线）。

fixtures/ocr_response_real.json 是 gateway.pltplt.com/ai/process 的真实响应（公共测试 PDF），
固定了响应结构；fixtures/ocr_response_charges.json 是在同结构上补了 chargeList 的样例。
"""
import json
from pathlib import Path

import pytest

from bill_agent.harness.budget import BudgetExceeded, BudgetTracker
from bill_agent.parsers import ocr

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_real_response_is_mapped_not_treated_as_text():
    """回归本次 P0：旧实现会返回 message='文档解析成功'，把账单数据全丢掉。"""
    result = _load("ocr_response_real.json")
    assert ocr._is_success(result) is True

    items = result["data"]["list"]
    assert items, "真实响应必须含 data.list"

    doc = ocr._item_to_doc(items[0], "x.pdf", "http://example.com/x.pdf")
    assert "文档解析成功" not in doc["raw_text"]
    assert doc["meta"]["ocr_file_type"] == items[0]["fileType"]
    # 非空的单据级字段必须进入文本（空值跳过，避免 prompt 噪音）
    assert "fileType: " in doc["raw_text"]
    assert "mblNumber:" not in doc["raw_text"]  # 真实响应里为空
    assert doc["meta"]["ocr_raw"] is items[0]


def test_charge_list_becomes_table():
    result = _load("ocr_response_charges.json")
    item = result["data"]["list"][0]
    doc = ocr._item_to_doc(item, "x.pdf", "http://example.com/x.pdf")

    assert doc["tables"], "chargeList 必须转成表格"
    charge_table = doc["tables"][0]
    assert "amount" in charge_table[0]
    assert "Handling Charge" in charge_table[1]
    assert "--- 费用明细 ---" in doc["raw_text"]
    # 单据级字段（提单号/收发货人）也要进文本，供匹配规则 2/4 使用
    assert "mblNumber: MAEU275752922" in doc["raw_text"]
    assert "shipper: YIWU JINBAIREN" in doc["raw_text"]


def test_failure_response_detected():
    assert ocr._is_success({"success": False, "message": "boom"}) is False
    assert ocr._is_success({"success": True, "error_code": 500}) is False


def test_rows_to_table_keeps_non_dict_rows():
    """chargeList 混合 dict 与非 dict 行：非 dict 行并入表格，不得静默丢弃。"""
    t = ocr._rows_to_table(
        [
            {"feeName": "OCEAN FREIGHT", "amount": "100"},
            ["plain row", 200],
            {"feeName": "THC", "amount": "300"},
        ]
    )
    assert t is not None and len(t) == 4
    assert any("plain row" in " ".join(row) for row in t), "非 dict 行不得被静默丢弃"


def test_ocr_other_hold_fields_reach_raw_text(monkeypatch, tmp_path):
    """other/hold 非空字段必须进 raw_text（LLM 上下文与审计轨迹），不得无条件跳过。"""
    _fake_ocr_env(
        monkeypatch,
        {
            "success": True,
            "error_code": 0,
            "data": {
                "list": [
                    {
                        "fileType": "PAI-COLOADER-NOTICE",
                        "mblNumber": "MBL001",
                        "hold": {"holdType": "CUSTOMS", "holdDesc": "payment required"},
                        "other": [{"fee": "extra 500"}],
                    }
                ]
            },
        },
    )
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n")

    docs = ocr.parse_ocr(str(img))
    text = docs[0]["raw_text"]
    assert "CUSTOMS" in text, "hold 字段内容必须可见"
    assert "extra 500" in text, "other 字段内容必须可见"


def test_unreachable_staging_fails_fast(monkeypatch, tmp_path):
    """没有公网地址时立刻失败（而不是等网关 504 超时）。"""
    monkeypatch.setattr(
        ocr,
        "ocr_delivery_blocker",
        lambda: "stage.local.public_base_url 未配置（config/settings.yaml）",
    )
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n")

    with pytest.raises(RuntimeError, match="public_base_url"):
        ocr.parse_ocr(str(img))


def test_pdf_falls_back_to_text_layer_when_ocr_unavailable(monkeypatch, tmp_path):
    import pymupdf

    pdf = tmp_path / "a.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Handling Charge USD 100")
    doc.save(str(pdf))
    doc.close()

    monkeypatch.setattr(
        ocr,
        "ocr_delivery_blocker",
        lambda: "stage.local.public_base_url 未配置（config/settings.yaml）",
    )
    docs = ocr.parse_ocr(str(pdf))

    assert docs[0]["type"] == "pdf_text"
    assert "Handling Charge" in docs[0]["raw_text"]
    assert "ocr_skipped" in docs[0]["meta"]


def test_pdf_text_route_marks_ocr_skipped_when_ocr_unavailable(monkeypatch, tmp_path):
    """OCR 不可用时 PDF 降级到文本层：必须带具体原因（审计/LLM 上下文可见）。"""
    import pymupdf

    from bill_agent.parsers import dispatcher, stage

    pdf = tmp_path / "t.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Handling Charge USD 100")
    doc.save(str(pdf))
    doc.close()

    monkeypatch.setattr(dispatcher, "_ocr_ready", lambda: False)
    monkeypatch.setattr(
        stage,
        "ocr_delivery_blocker",
        lambda: "OSS 凭据未配置：.env 的 OSS_ACCESS_KEY_ID",
    )
    out = dispatcher.parse_file(str(pdf))

    assert out[0]["type"] == "pdf_text"
    assert "ocr_skipped" in out[0]["meta"]
    assert "降级" in out[0]["meta"]["ocr_skipped"]
    assert "OSS 凭据未配置" in out[0]["meta"]["ocr_skipped"], "降级提示必须点名具体缺口"


def test_ocr_delivery_blocker_reports_specific_config_gaps(monkeypatch):
    """就绪判定与错误提示共用一处：缺凭据 / 缺公网地址 / 缺本地公网基址
    必须各自可区分，不能一律报「staging 地址不可达」（round 14）。"""
    from bill_agent.parsers import stage

    class _Cfg:
        def __init__(self, backend, base=None):
            self.stage = {"backend": backend}
            self.stage_public_base_url = base

    # OSS：endpoint 公网但两个凭据都空 → 点名两个环境变量
    monkeypatch.setattr(stage, "config", _Cfg("oss"))
    monkeypatch.setattr(stage, "_oss_settings", lambda: {
        "endpoint": "https://oss-cn-hangzhou.aliyuncs.com", "access_key": "", "secret_key": "",
    })
    msg = stage.ocr_delivery_blocker()
    assert msg and "OSS_ACCESS_KEY_ID" in msg and "OSS_ACCESS_KEY_SECRET" in msg, msg
    assert stage.ocr_delivery_ready() is False

    # 只缺 secret：只点名缺的那个
    monkeypatch.setattr(stage, "_oss_settings", lambda: {
        "endpoint": "https://oss-cn-hangzhou.aliyuncs.com", "access_key": "AK", "secret_key": "",
    })
    msg = stage.ocr_delivery_blocker()
    assert msg and "OSS_ACCESS_KEY_SECRET" in msg and "OSS_ACCESS_KEY_ID" not in msg, msg

    # OSS endpoint 是私网
    monkeypatch.setattr(stage, "_oss_settings", lambda: {
        "endpoint": "http://127.0.0.1:9000", "access_key": "AK", "secret_key": "SK",
    })
    msg = stage.ocr_delivery_blocker()
    assert msg and "不是国内公网地址" in msg and "127.0.0.1" in msg, msg

    # OSS endpoint 未配置
    monkeypatch.setattr(stage, "_oss_settings", lambda: {
        "endpoint": "", "access_key": "AK", "secret_key": "SK",
    })
    msg = stage.ocr_delivery_blocker()
    assert msg and "stage.oss.endpoint 未配置" in msg, msg

    # s3：public_endpoint 回退到本机 endpoint
    monkeypatch.setattr(stage, "config", _Cfg("s3"))
    monkeypatch.setattr(stage, "_public_s3_endpoint", lambda: "http://localhost:9000")
    msg = stage.ocr_delivery_blocker()
    assert msg and "stage.s3.public_endpoint" in msg, msg

    # local：缺 public_base_url / 私网 base / 就绪
    monkeypatch.setattr(stage, "config", _Cfg("local", None))
    msg = stage.ocr_delivery_blocker()
    assert msg and "public_base_url" in msg, msg

    monkeypatch.setattr(stage, "config", _Cfg("local", "http://192.168.1.10:8787"))
    msg = stage.ocr_delivery_blocker()
    assert msg and "不是国内公网地址" in msg and "192.168.1.10" in msg, msg

    monkeypatch.setattr(stage, "config", _Cfg("local", "https://cdn.example.com"))
    assert stage.ocr_delivery_blocker() is None
    assert stage.ocr_delivery_ready() is True


def test_ocr_error_message_uses_blocker_reason(monkeypatch, tmp_path):
    """配置缺口必须原样出现在错误里：缺凭据 ≠ 地址不可达。"""
    monkeypatch.setattr(
        ocr, "ocr_delivery_blocker", lambda: "OSS 凭据未配置：.env 的 OSS_ACCESS_KEY_ID"
    )
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n")

    with pytest.raises(RuntimeError) as ei:
        ocr.parse_ocr(str(img))
    msg = str(ei.value)
    assert "OSS 凭据未配置" in msg, msg
    assert "504" not in msg and "public_base_url" not in msg, msg


def test_corrupt_pdf_with_ocr_unavailable_reports_open_error(monkeypatch, tmp_path):
    """打不开的 PDF + OCR 不可用：必须报真实打开错误，不能误报 staging 地址不可达。"""
    from bill_agent.parsers import dispatcher

    monkeypatch.setattr(dispatcher, "_ocr_ready", lambda: False)
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")

    with pytest.raises(Exception) as ei:
        dispatcher.parse_file(str(bad))
    msg = str(ei.value)
    assert "public_base_url" not in msg, "根因是 PDF 打不开，不是 OCR 配置"
    assert "Failed to open" in msg or "无法打开" in msg


def test_ocr_budget_enforced(monkeypatch, tmp_path):
    # 预算检查在 staging 就绪之后：先固定就绪，测试才确定走 reserve_ocr 分支
    monkeypatch.setattr(ocr, "ocr_delivery_blocker", lambda: None)
    monkeypatch.setattr(ocr, "get_budget", lambda: BudgetTracker(1000, 0))
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n")

    with pytest.raises(BudgetExceeded):
        ocr.parse_ocr(str(img))


def test_ocr_config_failure_does_not_consume_budget(monkeypatch, tmp_path):
    """staging 未配置是配置问题、没有发起网络调用：不得消耗 OCR 额度，
    否则多次配置失败后错误会变成误导的「预算上限」。"""
    tracker = BudgetTracker(1000, 1)
    monkeypatch.setattr(ocr, "get_budget", lambda: tracker)
    monkeypatch.setattr(
        ocr,
        "ocr_delivery_blocker",
        lambda: "stage.local.public_base_url 未配置（config/settings.yaml）",
    )
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n")

    with pytest.raises(RuntimeError, match="public_base_url"):
        ocr.parse_ocr(str(img))
    assert tracker.ocr_calls == 0, "没有网络调用就不该占用 OCR 预算"


def test_private_and_loopback_hosts_are_not_public():
    """网关在公网，RFC1918/回环地址一律视为不可达（实测 60s 后 504）。"""
    from bill_agent.parsers.stage import is_public_host

    assert is_public_host("http://127.0.0.1:8787") is False
    assert is_public_host("http://localhost:9000") is False
    assert is_public_host("http://192.168.31.83:9000") is False
    assert is_public_host("http://10.0.0.5:9000") is False
    assert is_public_host("http://172.16.0.2:9000") is False
    assert is_public_host("https://example.com/file.pdf") is True


def test_object_name_is_url_safe_and_unique():
    from bill_agent.parsers.stage import _object_name

    a = _object_name("PDF 示例账单2.pdf")
    b = _object_name("PDF 示例账单2.pdf")
    assert a != b
    assert all(c.isalnum() or c in "._-" for c in a)


def test_oss_backend_uploads_and_presigns(monkeypatch, tmp_path):
    """OSS 后端：oss2 原生签名（SigV2），expires 单位为秒。"""
    import sys
    import types

    from bill_agent.parsers import stage

    fake_oss2 = types.ModuleType("oss2")
    calls: dict = {}

    class FakeNoSuchBucket(Exception):
        pass

    class FakeAuth:
        def __init__(self, ak, sk):
            calls["auth"] = (ak, sk)

    class FakeBucket:
        def __init__(self, auth, endpoint, bucket):
            calls["bucket"] = (endpoint, bucket)

        def put_object_from_file(self, name, path):
            calls["put"] = (name, str(path))

        def sign_url(self, method, name, expires):
            calls["sign"] = (method, name, expires)
            return "https://bill-files.oss-cn-hangzhou.aliyuncs.com/x.pdf?sign=abc"

    fake_oss2.Auth = FakeAuth
    fake_oss2.Bucket = FakeBucket
    fake_oss2.exceptions = types.SimpleNamespace(NoSuchBucket=FakeNoSuchBucket)
    monkeypatch.setitem(sys.modules, "oss2", fake_oss2)
    monkeypatch.setattr(stage, "_oss_settings", lambda: {
        # 不带 scheme：验证代码强制补 https://（oss2 默认补 http://，明文不安全）
        "endpoint": "oss-cn-hangzhou.aliyuncs.com/",
        "access_key": "AK", "secret_key": "SK",
        "bucket": "bill-files", "presign_expiry_hours": 24,
    })

    f = tmp_path / "账单.pdf"
    f.write_bytes(b"%PDF")

    url = stage._stage_oss(str(f))
    assert url.startswith("https://bill-files.oss-cn-hangzhou.aliyuncs.com/")
    assert calls["auth"] == ("AK", "SK")
    # endpoint 只去掉末尾斜杠、保留 scheme 后传给 oss2（oss2 接受带 scheme 的 endpoint）
    assert calls["bucket"] == ("https://oss-cn-hangzhou.aliyuncs.com", "bill-files")
    assert calls["put"][1] == str(f)
    assert calls["sign"] == ("GET", calls["put"][0], 86400)


def test_oss_backend_requires_credentials(monkeypatch, tmp_path):
    from bill_agent.parsers import stage

    monkeypatch.setattr(stage, "_oss_settings", lambda: {
        "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
        "access_key": "", "secret_key": "",
        "bucket": "bill-files", "presign_expiry_hours": 24,
    })
    with pytest.raises(RuntimeError, match="凭据未配置"):
        stage._stage_oss(str(tmp_path))


def test_oss_delivery_ready_requires_public_endpoint(monkeypatch):
    from bill_agent.parsers import stage

    class FakeConfig:
        stage = {"backend": "oss", "oss": {"endpoint": "http://127.0.0.1:9000"}}
        stage_public_base_url = None

    monkeypatch.setattr(stage, "config", FakeConfig())
    monkeypatch.setattr(stage, "_oss_settings", lambda: {
        "endpoint": "http://127.0.0.1:9000", "access_key": "AK", "secret_key": "SK",
    })
    assert stage.ocr_delivery_ready() is False

    class FakeConfig2:
        stage = {"backend": "oss", "oss": {"endpoint": "https://oss-cn-hangzhou.aliyuncs.com"}}
        stage_public_base_url = None

    monkeypatch.setattr(stage, "config", FakeConfig2())
    monkeypatch.setattr(stage, "_oss_settings", lambda: {
        "endpoint": "https://oss-cn-hangzhou.aliyuncs.com", "access_key": "AK", "secret_key": "SK",
    })
    assert stage.ocr_delivery_ready() is True


def test_oss_delivery_ready_requires_credentials(monkeypatch):
    """endpoint 配了但凭据没填时，OCR 就绪灯不得点亮。"""
    from bill_agent.parsers import stage

    class FakeConfig:
        stage = {"backend": "oss", "oss": {"endpoint": "https://oss-cn-hangzhou.aliyuncs.com"}}
        stage_public_base_url = None

    monkeypatch.setattr(stage, "config", FakeConfig())
    monkeypatch.setattr(stage, "_oss_settings", lambda: {"access_key": "", "secret_key": ""})
    assert stage.ocr_delivery_ready() is False


def test_oss_access_denied_is_translated(monkeypatch, tmp_path):
    """RAM 用户缺 PutObject 权限 → 明确的中文报错，而不是英文 SDK 异常。"""
    import sys
    import types

    from bill_agent.parsers import stage

    fake_oss2 = types.ModuleType("oss2")

    class FakeAccessDenied(Exception):
        pass

    class FakeNoSuchBucket(Exception):
        pass

    class FakeAuth:
        def __init__(self, ak, sk):
            pass

    class FakeBucket:
        def __init__(self, auth, endpoint, bucket):
            pass

        def put_object_from_file(self, name, path):
            raise FakeAccessDenied("AccessDenied: you are not authorized")

    fake_oss2.Auth = FakeAuth
    fake_oss2.Bucket = FakeBucket
    fake_oss2.exceptions = types.SimpleNamespace(
        NoSuchBucket=FakeNoSuchBucket, AccessDenied=FakeAccessDenied
    )
    monkeypatch.setitem(sys.modules, "oss2", fake_oss2)
    monkeypatch.setattr(stage, "_oss_settings", lambda: {
        "endpoint": "https://oss-cn-shanghai.aliyuncs.com",
        "access_key": "AK", "secret_key": "SK",
        "bucket": "rag-data-2026", "presign_expiry_hours": 24,
    })

    with pytest.raises(RuntimeError, match="oss:PutObject"):
        stage._stage_oss(str(tmp_path))


def test_stage_router_supports_oss(monkeypatch, tmp_path):
    from bill_agent.parsers import stage

    monkeypatch.setattr(stage, "_stage_oss", lambda p: "oss://" + p)

    class FakeConfig:
        stage = {"backend": "oss"}

    monkeypatch.setattr(stage, "config", FakeConfig())
    f = str(tmp_path / "a.pdf")
    assert stage.stage_local_file(f) == "oss://" + f


def _fake_ocr_env(monkeypatch, response):
    """绕过 budget/stage/网络，直接喂网关响应给 parse_ocr。"""
    monkeypatch.setattr(ocr, "get_budget", lambda: BudgetTracker(1000, 5))
    monkeypatch.setattr(ocr, "ocr_delivery_blocker", lambda: None)
    monkeypatch.setattr(ocr, "stage_local_file", lambda p: "https://x.example.com/f.png?sig=SECRET")
    monkeypatch.setattr(ocr, "call_ocr", lambda url, task=None: response)


def test_ocr_empty_list_raises_clear_error(monkeypatch, tmp_path):
    """网关 success 但 data.list 为空：必须报错进 parse_errors，不能静默吞掉。"""
    _fake_ocr_env(monkeypatch, {"success": True, "error_code": 0, "data": {"list": []}})
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n")

    with pytest.raises(RuntimeError, match="未返回 list"):
        ocr.parse_ocr(str(img))


def test_ocr_non_dict_items_raise_clear_error(monkeypatch, tmp_path):
    """data.list 非空但全是非 dict 条目：报错并带载荷摘要（旧实现静默返回 []）。"""
    _fake_ocr_env(monkeypatch, {"success": True, "error_code": 0, "data": {"list": ["not-a-dict", None]}})
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n")

    with pytest.raises(RuntimeError, match="无有效单据条目") as ei:
        ocr.parse_ocr(str(img))
    assert "not-a-dict" in str(ei.value)


def test_ocr_all_empty_items_raise_real_cause(monkeypatch, tmp_path):
    """data.list 全是空条目（{} / 仅 page_index）：真实原因是网关未识别出内容，
    不能产出空 doc 让下游误报「文件无内容可提取」。"""
    _fake_ocr_env(monkeypatch, {
        "success": True, "error_code": 0,
        "data": {"list": [{}, {"page_index": 0}]},
    })
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n")

    with pytest.raises(RuntimeError, match="均无可提取内容"):
        ocr.parse_ocr(str(img))


def test_ocr_placeholder_items_are_skipped(monkeypatch, tmp_path):
    """空/占位条目（chargeList 只有空行、suspicious:false）不算内容：
    真实原因是网关未识别出单据，不能被当成「有内容」送 LLM。"""
    _fake_ocr_env(monkeypatch, {
        "success": True, "error_code": 0,
        "data": {"list": [{"chargeList": [{}], "suspicious": False}, {}]},
    })
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n")

    with pytest.raises(RuntimeError, match="均无可提取内容"):
        ocr.parse_ocr(str(img))


def test_ocr_real_schema_placeholder_items_are_skipped(monkeypatch, tmp_path):
    """真实网关把标记编码成 'N' 字符串、hold 是全空 dict：只含这些字段
    （外加 fileType 标签）的条目不算内容（旧门禁只认 Python bool，真实响应
    永远命中不了；把 fileType 当内容则垃圾条目会遮蔽 PDF 文本层）。"""
    import copy

    item = copy.deepcopy(_load("ocr_response_real.json")["data"]["list"][0])
    assert ocr._item_has_content(item) is False, "只剩 fileType/标记/空 hold 不算内容"
    item["mblNumber"] = "OOLU4055331281"
    assert ocr._item_has_content(item) is True, "出现真实字段才算内容"
    item["mblNumber"] = ""

    _fake_ocr_env(monkeypatch, {"success": True, "error_code": 0, "data": {"list": [item]}})
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n")

    with pytest.raises(RuntimeError, match="均无可提取内容"):
        ocr.parse_ocr(str(img))


def test_ocr_junk_items_fall_back_to_pdf_text_layer(monkeypatch, tmp_path):
    """OCR 只返回 fileType/布尔/Y-N 标记：不能把垃圾条目送 LLM，也不能因此
    丢掉 PDF 自带文本层（真实内容被网关噪声遮蔽）。"""
    import pymupdf

    pdf = tmp_path / "a.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Handling Charge USD 100")
    doc.save(str(pdf))
    doc.close()

    _fake_ocr_env(monkeypatch, {
        "success": True, "error_code": 0,
        "data": {"list": [{"fileType": "INVOICE", "isReefer": False},
                          {"isDetention": "N", "suspicious": "N"},
                          # 非 marker 字段上的 Y/N/yes/no/true/false 字符串同样不算内容
                          {"remark": "N"}]},
    })
    docs = ocr.parse_ocr(str(pdf))

    assert docs[0]["type"] == "pdf_text"
    assert "Handling Charge" in docs[0]["raw_text"]
    assert docs[0]["meta"]["ocr_fallback"] == "pdf_text"
    assert "均无可提取内容" in docs[0]["meta"]["ocr_error"]


def test_ocr_staging_failure_degrades_and_releases_budget(monkeypatch, tmp_path):
    """staging 失败没有发起 OCR 调用：PDF 降级文本层，且额度必须回滚，
    否则后续文件会误报「预算上限」把真实 staging 故障遮蔽。"""
    import pymupdf

    pdf = tmp_path / "a.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Handling Charge USD 100")
    doc.save(str(pdf))
    doc.close()

    tracker = BudgetTracker(1000, 2)
    monkeypatch.setattr(ocr, "get_budget", lambda: tracker)
    monkeypatch.setattr(ocr, "ocr_delivery_blocker", lambda: None)

    def boom(_path):
        raise OSError("staging boom")

    monkeypatch.setattr(ocr, "stage_local_file", boom)
    docs = ocr.parse_ocr(str(pdf))

    assert docs[0]["type"] == "pdf_text"
    assert "Handling Charge" in docs[0]["raw_text"], "降级必须真的取到文本层，而不是空 doc"
    assert "staging boom" in docs[0]["meta"]["ocr_error"]
    assert tracker.ocr_calls == 0, "staging 失败没有发起 OCR，额度必须回滚"


def test_ocr_staging_error_meta_uses_full_redaction(monkeypatch, tmp_path):
    """staging 失败降级时 meta.ocr_error 必须按 parse_errors 同一标准脱敏：
    临时路径与 URL 凭据（含无 scheme 的请求路径）都不得进入 prompt/审计轨迹。"""
    import pymupdf

    from bill_agent.extraction.extractor import docs_to_text

    pdf = tmp_path / "a.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Handling Charge USD 100")
    doc.save(str(pdf))
    doc.close()

    tracker = BudgetTracker(1000, 2)
    monkeypatch.setattr(ocr, "get_budget", lambda: tracker)
    monkeypatch.setattr(ocr, "ocr_delivery_blocker", lambda: None)

    def boom(_path):
        raise OSError(
            f"cannot stage {tmp_path}/bill_att_test123/a.pdf "
            "via https://user:pass@x.example.com/ai/process?api_token=SECRET123"
        )

    monkeypatch.setattr(ocr, "stage_local_file", boom)
    docs = ocr.parse_ocr(str(pdf))

    err = docs[0]["meta"]["ocr_error"]
    assert "bill_att_" not in err and "test123" not in err, err
    assert "SECRET123" not in err and "user:pass" not in err, err
    text = docs_to_text(docs)
    assert "bill_att_" not in text and "SECRET123" not in text, "meta 提示是第二条审计通道"


def test_ocr_staging_failure_image_keeps_original_error(monkeypatch, tmp_path):
    """图片没有文本层可降级：staging 失败必须原样上抛同一个异常（且不占预算），
    不能被改写成误导的 OCR/staging 提示或新异常。"""
    tracker = BudgetTracker(1000, 2)
    monkeypatch.setattr(ocr, "get_budget", lambda: tracker)
    monkeypatch.setattr(ocr, "ocr_delivery_blocker", lambda: None)

    err = OSError("staging boom")

    def boom(_path):
        raise err

    monkeypatch.setattr(ocr, "stage_local_file", boom)
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n")

    with pytest.raises(OSError) as ei:
        ocr.parse_ocr(str(img))
    assert ei.value is err, "必须原样上抛 staging 的异常对象"
    assert tracker.ocr_calls == 0


def test_ocr_budget_exhausted_pdf_falls_back_to_text(monkeypatch, tmp_path):
    """预算耗尽的 PDF 仍可本地提取；而且不得先 staging（额度已满不发上传）。"""
    import pymupdf

    pdf = tmp_path / "a.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Handling Charge USD 100")
    doc.save(str(pdf))
    doc.close()

    tracker = BudgetTracker(1000, 0)
    monkeypatch.setattr(ocr, "get_budget", lambda: tracker)
    monkeypatch.setattr(ocr, "ocr_delivery_blocker", lambda: None)

    def no_stage(_path):
        raise AssertionError("预算已满时不应 staging")

    monkeypatch.setattr(ocr, "stage_local_file", no_stage)
    docs = ocr.parse_ocr(str(pdf))

    assert docs[0]["type"] == "pdf_text"
    assert "Handling Charge" in docs[0]["raw_text"], "降级必须真的取到文本层"
    assert "预算上限" in docs[0]["meta"]["ocr_error"]
    assert tracker.ocr_calls == 0


def test_redact_urls_is_case_insensitive():
    """URL scheme 大小写混写（HTTPS://）也要去掉 query：预签名参数不得进入审计文本。"""
    out = ocr._redact_urls("HTTPS://x.example.com/y?api_token=SECRET123")
    assert out == "HTTPS://x.example.com/y"
    assert "SECRET123" not in out


def test_redact_urls_covers_schemeless_request_and_userinfo():
    """urllib3 的连接错误里请求 URL 没有 scheme（/ai/process?api_token=...），
    userinfo（user:pass@）同样不得进入审计文本。"""
    msg = (
        "Max retries exceeded with url: /ai/process?api_token=SECRETTOKEN123 "
        "(Caused by NewConnectionError('https://user:pass@x.example.com/f?sig=SIGVAL'))"
    )
    out = ocr._redact_urls(msg)
    assert "SECRETTOKEN123" not in out and "SIGVAL" not in out
    assert "user:pass" not in out
    assert "/ai/process" in out, "保留路径便于定位故障，只去掉凭据"


def test_redact_urls_covers_fragment_and_protocol_relative_forms():
    """无 scheme 的 fragment token（#api_token=）与协议相对 URL 的 userinfo
    （//user:pass@host）同样是凭据；URL 后紧跟的中文说明不得被一并吞掉。"""
    out = ocr._redact_urls("POST /ai/process#api_token=FRAGTOKEN987 failed")
    assert "FRAGTOKEN987" not in out, out
    assert "/ai/process" in out, "保留路径便于定位故障"

    out = ocr._redact_urls("boom //sinkuser:SINKPASS_ABC123@gateway.invalid/p?x=1")
    assert "SINKPASS_ABC123" not in out and "sinkuser" not in out, out
    assert "gateway.invalid" in out, "保留 host 便于定位故障"

    out = ocr._redact_urls("https://h/p?token=SECRET；后续说明")
    assert "SECRET" not in out
    assert "后续说明" in out, "URL 之后的说明文字不是 URL 的一部分"


def test_redact_urls_keeps_chinese_after_schemeless_query():
    """无 scheme 的请求路径按「空白 + CJK 标点」切 token：只删到 token 结束，
    中文说明保留（与 scheme URL 在 CJK 标点处停止的语义一致）。
    六个 CJK 标点逐个钉住：只测一个，其余五个的过脱敏回归会漏网。"""
    for sep in "；，。：、！？":
        out = ocr._redact_urls(f"POST /ai/process?api_token=SECRET123{sep}后续说明")
        assert out == f"POST /ai/process{sep}后续说明", (sep, out)


def test_flag_only_values_are_not_content():
    """Y/N/yes/no/true/false 字符串与布尔都不是单据内容；含这些字段的行同样是占位。"""
    assert ocr._item_has_content({}) is False
    assert ocr._item_has_content({"isReefer": False}) is False
    assert ocr._item_has_content({"remark": False}) is False
    assert ocr._item_has_content({"remark": "N"}) is False
    assert ocr._item_has_content({"remark": "yes"}) is False
    assert ocr._item_has_content({"remark": "TRUE"}) is False
    assert ocr._item_has_content({"chargeList": [{"amount": "false"}]}) is False
    assert ocr._item_has_content({"chargeList": [{"fileType": "charge"}]}) is False
    assert ocr._item_has_content({"page_index": 0, "chargeList": [{"page_index": 0}]}) is False
    assert ocr._item_has_content({"remark": "N", "mblNumber": "M1"}) is True


def test_ocr_budget_cap_is_shared_across_run_threads(monkeypatch):
    """LangGraph 并行分支线程共享同一预算上下文：OCR 上限按任务计，不是每线程一份。"""
    import contextvars
    import threading

    from bill_agent.harness import budget as budget_mod

    real = budget_mod.BudgetTracker
    monkeypatch.setattr(budget_mod, "BudgetTracker", lambda *a, **k: real(1000, 1))
    prev = budget_mod._CURRENT.get()  # 测试结束必须还原：否则后续测试继承 1 次额度
    try:
        budget_mod.reset_budget()  # 本任务预算：只允许 1 次 OCR 调用

        # 模拟 LangGraph 的 Send 分支：把当前上下文副本交给工作线程
        contexts = [contextvars.copy_context() for _ in range(2)]
        errors: list[str] = []

        def worker():
            try:
                budget_mod.get_budget().reserve_ocr()
            except budget_mod.BudgetExceeded as e:
                errors.append(str(e))

        threads = [threading.Thread(target=c.run, args=(worker,)) for c in contexts]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 1, f"并发分支必须共享同一个 OCR 上限，实际: {errors}"
    finally:
        budget_mod._CURRENT.set(prev)


def test_ocr_non_dict_result_is_not_attribute_error(monkeypatch, tmp_path):
    """网关返回非 dict（如 JSON 数组）：报 OCR 返回失败并带载荷，而非误导的 AttributeError。"""
    _fake_ocr_env(monkeypatch, ["oops"])
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n")

    with pytest.raises(RuntimeError, match="OCR 返回失败") as ei:
        ocr.parse_ocr(str(img))
    assert "oops" in str(ei.value)


def test_mixed_list_keeps_valid_docs_and_marks_skipped(monkeypatch, tmp_path):
    """混合 list（有效 dict + 垃圾条目）：有效单据保留，垃圾条目打标记不静默丢弃。"""
    from bill_agent.extraction.extractor import docs_to_text

    _fake_ocr_env(monkeypatch, {
        "success": True, "error_code": 0,
        "data": {"list": [{"fileType": "INVOICE", "invoiceNumber": "X1"}, "junk", 123]},
    })
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n")

    docs = ocr.parse_ocr(str(img))
    assert len(docs) == 1
    assert docs[0]["meta"]["ocr_skipped_items"] == 2
    # 标记进入文本上下文（LLM prompt 与审计轨迹 original_text 同源）
    assert "2 个条目无法结构化" in docs_to_text(docs)


def test_ocr_error_message_redacts_presigned_query(monkeypatch, tmp_path):
    """错误信息里的预签名 query 参数不得进入审计轨迹。"""
    _fake_ocr_env(monkeypatch, {
        "success": False,
        "message": "fail https://x.oss-cn-shanghai.aliyuncs.com/f?OSSAccessKeyId=AKID&Signature=SIG",
    })
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n")

    with pytest.raises(RuntimeError) as ei:
        ocr.parse_ocr(str(img))
    assert "OSSAccessKeyId" not in str(ei.value)
    assert "Signature" not in str(ei.value)


def test_ocr_error_paths_scan_only_a_bounded_prefix(monkeypatch, tmp_path):
    """网关 message / list 长度不可控：只扫描有界窗口，且窗口必须保住 marker。

    窗口太小会把 bill_att_ marker 切到窗口外，路径前缀就成了「没有 marker
    的普通文本」漏过脱敏（round 13：2,100 字组件把 marker 推到 2000 字外，
    可见摘要泄漏 C:\\Users\\<用户>）。_SCAN_MAX 远大于任何真实路径上限。
    """
    import pymupdf

    from bill_agent.harness.redact import clean_error as real_clean_error

    seen: list[int] = []

    def spy(text):
        seen.append(len(text))
        return real_clean_error(text)

    monkeypatch.setattr(ocr, "clean_error", spy)

    pdf = tmp_path / "a.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Handling Charge USD 100")
    doc.save(str(pdf))
    doc.close()

    # 真实路径前缀落在可见的 500/200 字内，marker 却在 2000 字之后：
    # 按 2000 字切窗口时整条路径不带 marker，脱敏完全失效
    long_path = "C:\\Users\\Jack\\" + "a" * 2100 + "\\bill_att_x\\a.pdf"

    # 通道 1：网关失败 message
    _fake_ocr_env(monkeypatch, {"success": False, "message": long_path})
    docs = ocr.parse_ocr(str(pdf))
    assert docs[0]["type"] == "pdf_text"
    err = docs[0]["meta"]["ocr_error"]
    assert "Jack" not in err and "bill_att_" not in err, err
    assert "<临时附件路径>" in err
    assert len(err) <= len("OCR 返回失败: ") + 500, "可见摘要仍须限长"

    # 通道 2：data.list 非空但全是无效条目（第二个元素是超长路径）
    _fake_ocr_env(
        monkeypatch,
        {
            "success": True,
            "error_code": 0,
            "data": {"list": [{"page_index": 1}, long_path]},
        },
    )
    docs = ocr.parse_ocr(str(pdf))
    assert docs[0]["type"] == "pdf_text"
    err = docs[0]["meta"]["ocr_error"]
    assert "Jack" not in err and "bill_att_" not in err, err
    assert "<临时附件路径>" in err

    # 通道 3：超过窗口的无 marker 文本。窗口上限若被去掉（整段扫描），
    # 这条会收到 len(giant) 而不是 _SCAN_MAX —— 上一条断言否则形同虚设
    giant = "x" * (ocr._SCAN_MAX + 50_000)
    _fake_ocr_env(monkeypatch, {"success": False, "message": giant})
    docs = ocr.parse_ocr(str(pdf))
    assert docs[0]["type"] == "pdf_text"
    assert len(docs[0]["meta"]["ocr_error"]) <= len("OCR 返回失败: ") + 500

    assert seen, "两条错误通道都必须经过 clean_error"
    assert max(seen) <= ocr._SCAN_MAX, f"clean_error 不应收到超长文本: {max(seen)}"
    assert max(seen) < len(giant), "窗口上限未生效：整段不可信文本都进了扫描"


def test_call_ocr_retries_transient_then_succeeds(monkeypatch):
    """瞬时错误（连接失败）按配置重试：重试次数用尽前成功则返回响应。"""
    import requests
    from types import SimpleNamespace

    from bill_agent.harness import error_handler

    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(url)
        if len(calls) < 3:
            raise requests.exceptions.ConnectionError("transient")
        resp = SimpleNamespace()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"success": True, "error_code": 0, "data": {"list": []}}
        return resp

    monkeypatch.setattr(ocr.requests, "post", fake_post)
    monkeypatch.setattr(error_handler, "time", SimpleNamespace(sleep=lambda s: None))

    out = ocr.call_ocr("https://x.example.com/f.pdf")
    assert out["success"] is True
    assert len(calls) == 3, "前两次瞬时失败必须重试"


def test_call_ocr_does_not_retry_client_errors(monkeypatch):
    """4xx 是客户端错误，重试不会成功：立即上抛，不浪费 OCR 次数。"""
    import requests
    from types import SimpleNamespace

    from bill_agent.harness import error_handler

    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(url)
        resp = SimpleNamespace(status_code=400, reason="Bad Request")
        raise requests.exceptions.HTTPError("400 Bad Request", response=resp)

    monkeypatch.setattr(ocr.requests, "post", fake_post)
    monkeypatch.setattr(error_handler, "time", SimpleNamespace(sleep=lambda s: None))

    with pytest.raises(requests.exceptions.HTTPError):
        ocr.call_ocr("https://x.example.com/f.pdf")
    assert len(calls) == 1, "4xx 不得重试"
