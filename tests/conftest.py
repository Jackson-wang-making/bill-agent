"""全局夹具：测试套件不得发起真实外部调用（LLM）。

ar_ap/语义复核在测试里若漏 mock，会带着真实 DEEPSEEK_API_KEY 把账单文本发给
api.deepseek.com：离线时靠异常兜底侥幸通过，联网时结果取决于模型回答（e2e 的
AR 断言会随机失败）。这里把两个消费点的 get_llm 统一替换成确定性假模型。
"""
from __future__ import annotations

import re

import pytest


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLM:
    """确定性假模型：AR/AP 佐证按规则方向回答，语义复核返回 OK。"""

    def invoke(self, messages):
        if isinstance(messages, str):
            # ar_ap._ask_llm 传的是单条 prompt 字符串
            return _FakeMessage(_direction_answer(messages))
        return _FakeMessage("OK")


def _direction_answer(prompt: str) -> str:
    """复现 ar_ap 的确定性方向，保证假模型不改变节点原有行为。"""
    role = re.search(r"记账主体在单据中的角色: (\w+)", prompt)
    doc_type = re.search(r"单据类型: (.+)", prompt)
    role_value = role.group(1) if role else ""
    is_credit = "credit" in (doc_type.group(1).lower() if doc_type else "")
    if role_value == "issuer":
        answer = "AP" if is_credit else "AR"
    elif role_value == "recipient":
        answer = "AR" if is_credit else "AP"
    else:
        return "UNCERTAIN|mocked"
    return f"{answer}|mocked"


def _offline_llm(*args, **kwargs):
    """结构化提取必须由测试自己 mock structured_llm；漏 mock 时离线失败，不联网。"""
    raise RuntimeError("测试不得调用真实 LLM：请 mock structured_llm")


@pytest.fixture(autouse=True)
def _no_live_llm(monkeypatch):
    from bill_agent.graph.nodes import ar_ap
    from bill_agent.llm import structured
    from bill_agent.validation import llm_validator

    monkeypatch.setattr(ar_ap, "get_llm", lambda *a, **k: _FakeLLM())
    monkeypatch.setattr(llm_validator, "get_llm", lambda *a, **k: _FakeLLM())
    monkeypatch.setattr(structured, "get_llm", _offline_llm)
