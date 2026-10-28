"""配置加载：.env + config/settings.yaml + config/charge_name_map.yaml。

所有阈值、模型路由、OCR/对象存储地址均外置到 YAML，敏感信息走环境变量。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(__file__).resolve().parent / "config"

load_dotenv(PROJECT_ROOT / ".env")


def _load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Config:
    """集中读取配置；缺省值即安全默认。"""

    def __init__(self) -> None:
        self.settings: dict[str, Any] = _load_yaml("settings.yaml")
        self.charge_name_map: list[dict[str, Any]] = _load_yaml("charge_name_map.yaml").get(
            "mappings", []
        )

    # ---- LLM ----
    @property
    def llm_model(self) -> str:
        return self.settings.get("llm", {}).get("model", "deepseek-chat")

    @property
    def llm_reasoner(self) -> str:
        return self.settings.get("llm", {}).get("reasoner_model", "deepseek-reasoner")

    @property
    def llm_api_base(self) -> str:
        return os.getenv("DEEPSEEK_BASE_URL") or self.settings.get("llm", {}).get(
            "api_base", "https://api.deepseek.com"
        )

    @property
    def llm_api_key(self) -> str:
        return os.getenv("DEEPSEEK_API_KEY", "")

    @property
    def llm_temperature(self) -> float:
        return self.settings.get("llm", {}).get("temperature", 0.0)

    @property
    def llm_structured_thinking(self) -> bool:
        """结构化提取是否保留思考模式。

        思考模式不支持 tool_choice；函数调用式结构化输出必须关闭它。
        """
        return self.settings.get("llm", {}).get("structured_thinking", False)

    # ---- 置信度阈值 ----
    @property
    def t_high(self) -> float:
        return self.settings.get("confidence", {}).get("T_high", 0.9)

    @property
    def t_low(self) -> float:
        return self.settings.get("confidence", {}).get("T_low", 0.6)

    # ---- 重试上限（Evaluator-Optimizer 硬约束） ----
    @property
    def max_extract_retries(self) -> int:
        return self.settings.get("retry", {}).get("max_extract_retries", 3)

    @property
    def max_match_retries(self) -> int:
        return self.settings.get("retry", {}).get("max_match_retries", 3)

    @property
    def max_ocr_retries(self) -> int:
        """OCR 网关瞬时错误（5xx/连接/超时）的指数退避重试上限。"""
        return self.settings.get("retry", {}).get("max_ocr_retries", 2)

    @property
    def ocr_retry_backoff(self) -> float:
        return self.settings.get("retry", {}).get("ocr_retry_backoff", 2.0)

    @property
    def ar_ap_reasoner(self) -> bool:
        """AR/AP 佐证用 deepseek-reasoner 强模型（架构 §3.3 模型分层）。"""
        return self.settings.get("llm", {}).get("ar_ap_reasoner", True)

    # ---- OCR ----
    @property
    def ocr_url(self) -> str:
        return self.settings.get("ocr", {}).get(
            "url", "https://gateway.pltplt.com/ai/process"
        )

    @property
    def ocr_task(self) -> str:
        return self.settings.get("ocr", {}).get("task", "orderChange")

    @property
    def ocr_timeout(self) -> tuple[float, float]:
        c = self.settings.get("ocr", {})
        return (c.get("timeout_connect", 10), c.get("timeout_read", 300))

    @property
    def ocr_pdf_mode(self) -> str:
        """always：PDF 也走 OCR（题目要求）；scanned_only：有文本层则本地提取。"""
        return self.settings.get("ocr", {}).get("pdf_mode", "always")

    @property
    def ocr_cache_enabled(self) -> bool:
        """同一文件内容命中 OCR 缓存时不重复调网关、不扣 OCR 预算。"""
        return self.settings.get("ocr", {}).get("cache_enabled", True)

    @property
    def ocr_cache_ttl_seconds(self) -> int:
        return int(self.settings.get("ocr", {}).get("cache_ttl_seconds", 3600))

    @property
    def ocr_degrade_enabled(self) -> bool:
        """扫描件/图片 OCR 失败后是否降采样二次识别。"""
        return self.settings.get("ocr", {}).get("degrade_enabled", True)

    @property
    def ocr_degrade_scale(self) -> float:
        return float(self.settings.get("ocr", {}).get("degrade_scale", 0.5))

    @property
    def ocr_pdf_text_supplement(self) -> bool:
        """OCR 成功后是否追加 PDF 本地文本层全文（补回网关漏掉的字段/页）。"""
        return self.settings.get("ocr", {}).get("pdf_text_supplement", True)

    # ---- 对象存储 stage ----
    @property
    def stage(self) -> dict[str, Any]:
        return self.settings.get("stage", {})

    @property
    def stage_public_base_url(self) -> str | None:
        """staging 目录对外暴露的公网地址（网关可访问）。未配置则 OCR 不可用。"""
        url = self.settings.get("stage", {}).get("local", {}).get("public_base_url")
        return url.rstrip("/") if url else None

    # ---- 预算 ----
    @property
    def max_total_tokens(self) -> int:
        return self.settings.get("budget", {}).get("max_total_tokens", 200000)

    @property
    def max_ocr_calls(self) -> int:
        return self.settings.get("budget", {}).get("max_ocr_calls", 20)

    # ---- 记账主体 ----
    @property
    def accounting_entity(self) -> str:
        return self.settings.get("accounting_entity", "") or os.getenv(
            "ACCOUNTING_ENTITY", ""
        )

    # ---- 标准化 ----
    @property
    def standardize_suggest_enabled(self) -> bool:
        """未命中费用名时是否给出建议标准名（离线确定性模糊匹配）。"""
        return self.settings.get("standardize", {}).get("suggest_enabled", False)

    @property
    def standardize_suggest_min_ratio(self) -> float:
        return float(self.settings.get("standardize", {}).get("suggest_min_ratio", 0.6))

    # ---- 持久化 ----
    @property
    def checkpointer(self) -> str:
        """none | memory | sqlite。默认 none，保持单次运行行为。"""
        return self.settings.get("persistence", {}).get("checkpointer", "none")

    @property
    def checkpointer_sqlite_path(self) -> str:
        return self.settings.get("persistence", {}).get(
            "sqlite_path", "output/checkpoints.sqlite"
        )


config = Config()
