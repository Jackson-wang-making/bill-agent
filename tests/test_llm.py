"""LLM 路由与结构化输出的模型参数回归。"""


def test_get_llm_disables_thinking_via_extra_body(monkeypatch):
    """thinking=False 必须显式下发 thinking disabled，避免 tool_choice 被拒。"""
    import bill_agent.llm.router as router

    captured = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(router, "ChatDeepSeek", fake_chat)
    router.get_llm(thinking=False)

    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}


def test_get_llm_keeps_thinking_by_default(monkeypatch):
    """未显式关闭时不下发 thinking 参数（AR/AP 仍可保留思考模式）。"""
    import bill_agent.llm.router as router

    captured = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(router, "ChatDeepSeek", fake_chat)
    router.get_llm()
    assert "extra_body" not in captured


def test_structured_llm_passes_thinking_flag(monkeypatch):
    """结构化输出工厂关闭思考，并走 JSON mode（response_format=json_object）。"""
    import bill_agent.llm.structured as structured
    from bill_agent.extraction.schemas import ExtractionResult
    from langchain_core.runnables import RunnableLambda

    captured = {}

    class _FakeLLM:
        def bind(self, **kwargs):
            captured.update(kwargs)
            return RunnableLambda(lambda x: x)

    def fake_get_llm(**kwargs):
        captured.update(kwargs)
        return _FakeLLM()

    monkeypatch.setattr(structured, "get_llm", fake_get_llm)
    chain = structured.structured_llm(ExtractionResult)

    assert chain is not None
    assert captured["thinking"] is False
    assert captured["response_format"] == {"type": "json_object"}
