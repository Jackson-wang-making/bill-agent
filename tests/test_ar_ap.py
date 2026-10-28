"""AR/AP 判定：记账主体角色 + 单据类型 → 方向，LLM 只做佐证。"""
from bill_agent.graph.nodes import ar_ap as mod
from bill_agent.graph.nodes.ar_ap import _direction, _role, ar_ap_node


def test_issuer_is_entity_debit_note():
    assert _role("A Shipping", "A Shipping Co", "B Co") == "issuer"
    assert _direction("Debit Note", "issuer") == "AR"


def test_recipient_is_entity_debit_note():
    assert _role("B Co", "A Shipping Co", "B Co") == "recipient"
    assert _direction("Debit Note", "recipient") == "AP"


def test_credit_note_reverses():
    assert _direction("Credit Note", "issuer") == "AP"
    assert _direction("Credit Note", "recipient") == "AR"


def test_entity_not_a_party_is_undetermined():
    assert _role("C Corp", "A Shipping Co", "B Co") is None
    assert _role("", "A", "B") is None


def _state(entity="A Shipping", issuer="A Shipping Co", recipient="B Co", doc_type="Debit Note"):
    return {
        "accounting_entity": entity,
        "transaction_party": {"issuer": issuer, "recipient": recipient},
        "document_info": {"document_type": doc_type},
        "normalized_charges": [],
        "parsed_documents": [],
    }


def test_node_agrees_with_rule(monkeypatch):
    monkeypatch.setattr(mod, "_ask_llm", lambda *a: ("AR", "签发方请求付款"))
    out = ar_ap_node(_state())
    assert out["posting_type"] == "AR"
    assert "issuer" in out["ar_ap_reasoning"]


def test_node_conflict_goes_to_manual(monkeypatch):
    monkeypatch.setattr(mod, "_ask_llm", lambda *a: ("AP", "模型认为应付"))
    out = ar_ap_node(_state())
    assert out["posting_type"] is None
    assert "冲突" in out["ar_ap_reasoning"]


def test_node_llm_uncertain_goes_to_manual(monkeypatch):
    monkeypatch.setattr(mod, "_ask_llm", lambda *a: (None, "信息不足"))
    assert ar_ap_node(_state())["posting_type"] is None


def test_node_entity_not_party_never_asks_llm(monkeypatch):
    def boom(*a):
        raise AssertionError("主体不是交易双方时不应调用 LLM")

    monkeypatch.setattr(mod, "_ask_llm", boom)
    out = ar_ap_node(_state(entity="C Corp"))
    assert out["posting_type"] is None
    assert "不是单据签发方或接收方" in out["ar_ap_reasoning"]


def test_node_llm_down_falls_back_to_rule(monkeypatch):
    def boom(*a):
        raise RuntimeError("network")

    monkeypatch.setattr(mod, "_ask_llm", boom)
    out = ar_ap_node(_state())
    assert out["posting_type"] == "AR"
    assert "LLM 不可用" in out["ar_ap_reasoning"]


def test_node_conflict_through_get_llm_goes_to_manual(monkeypatch):
    """从 get_llm 边界验证接线：模型与规则冲突同样转人工（不只测 _ask_llm）。"""

    class _Resp:
        content = "AP|模型认为应付"

    class _LLM:
        def invoke(self, prompt):
            return _Resp()

    monkeypatch.setattr(mod, "get_llm", lambda *a, **k: _LLM())
    out = ar_ap_node(_state())
    assert out["posting_type"] is None
    assert "冲突" in out["ar_ap_reasoning"]


def test_node_get_llm_exception_falls_back_to_rule(monkeypatch):
    """get_llm 本身不可用（构造/网络失败）也必须回落到确定性方向。"""

    def boom(*a, **k):
        raise RuntimeError("no network")

    monkeypatch.setattr(mod, "get_llm", boom)
    out = ar_ap_node(_state())
    assert out["posting_type"] == "AR"
    assert "LLM 不可用" in out["ar_ap_reasoning"]


def test_global_fixture_keeps_tests_offline():
    """conftest 的 autouse 夹具必须存在：漏 mock 的测试不得把账单发给真实模型。"""
    llm = mod.get_llm()
    answer = llm.invoke("记账主体在单据中的角色: issuer\n单据类型: Debit Note").content
    assert answer == "AR|mocked"


def test_ask_llm_passes_reasoner_flag(monkeypatch):
    """AR/AP 佐证走 reasoner 强模型（架构 §3.3 模型分层）。"""
    captured = {}

    class _Resp:
        content = "AR|mocked"

    class _LLM:
        def invoke(self, prompt):
            return _Resp()

    monkeypatch.setattr(mod, "get_llm", lambda **kwargs: captured.update(kwargs) or _LLM())

    answer, _ = mod._ask_llm("A Shipping", "issuer", "A Co", "B Co", "Debit Note", [], "doc")
    assert answer == "AR"
    assert captured.get("reasoner") is True
