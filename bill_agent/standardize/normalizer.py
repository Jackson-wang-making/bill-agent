"""费用名标准化：映射表 + 模糊匹配，保留原始名。

config/charge_name_map.yaml 结构：
  mappings:
    - standard: Handling Charge
      aliases: [Handling Fee, H/D Fee, ...]
"""
from __future__ import annotations

import difflib
import re
from datetime import datetime
from email.utils import parsedate_to_datetime

from bill_agent.config import config


def _norm(s: str) -> str:
    """归一化：小写、去空格/下划线/斜杠。"""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _build_alias_map() -> dict[str, str]:
    m: dict[str, str] = {}
    for entry in config.charge_name_map:
        standard = entry.get("standard", "")
        if not standard:
            continue
        m[_norm(standard)] = standard
        for alias in entry.get("aliases", []):
            m[_norm(str(alias))] = standard
    return m


_ALIAS_MAP = _build_alias_map()


def normalize_charge_name(raw_name: str) -> tuple[str, str]:
    """返回 (标准名, 原始名)。未命中映射时标准名=原始名。"""
    key = _norm(raw_name)
    standard = _ALIAS_MAP.get(key)
    if standard:
        return standard, raw_name
    # 模糊：别名作为子串出现在原始名中
    for alias_key, standard in _ALIAS_MAP.items():
        if alias_key and alias_key in key:
            return standard, raw_name
    return raw_name, raw_name


def suggest_charge_name(raw_name: str) -> str | None:
    """未命中映射时的建议标准名（确定性模糊匹配，离线可用）。

    默认关闭；开启后对未命中的原始名与字典标准名/别名做序列相似度，
    达到阈值则返回最接近的标准名，供运营确认后回填字典。
    """
    if not config.standardize_suggest_enabled:
        return None
    raw = _norm(raw_name)
    if not raw:
        return None
    best_standard: str | None = None
    best_ratio = 0.0
    for entry in config.charge_name_map:
        standard = entry.get("standard", "")
        if not standard:
            continue
        for name in (standard, *entry.get("aliases", [])):
            key = _norm(str(name))
            if not key:
                continue
            ratio = difflib.SequenceMatcher(None, raw, key).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_standard = standard
    if best_standard is not None and best_ratio >= config.standardize_suggest_min_ratio:
        return best_standard
    return None


def normalize_currency(code: str | None) -> str | None:
    """币种统一三位大写；无法提取时保持 null，不能变成 'NON'。"""
    if code is None:
        return None
    text = str(code).strip().upper()
    return text[:3] or None


def normalize_payment_term(term: str | None) -> str | None:
    """付款方式归一化 PP / CC。"""
    if not term:
        return None
    t = str(term).strip().upper()
    if "PREPAID" in t or t == "PP" or "预付" in t:
        return "PP"
    if "COLLECT" in t or t == "CC" or "到付" in t:
        return "CC"
    return t


_ISO_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_DMY_RE = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})")
_CN_RE = re.compile(r"^(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def _iso(y: int, m: int, d: int) -> str | None:
    try:
        return datetime(y, m, d).date().isoformat()
    except ValueError:
        return None


def normalize_date(value: str | None) -> str | None:
    """单据日期归一化为 ISO YYYY-MM-DD。

    匹配规则④按日期相等比较，`10-24-2026` 与 `2026-10-24` 必须归一到同一形式。
    两位在前的格式存在日/月歧义：任一位 >12 即可定序，否则按美式 MM-DD-YYYY。
    """
    if not value:
        return value
    s = str(value).strip()
    if not s:
        return value

    m = _ISO_RE.match(s)
    if m:
        return _iso(int(m.group(1)), int(m.group(2)), int(m.group(3))) or value

    m = _DMY_RE.match(s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if a > 12:
            day, month = a, b
        elif b > 12:
            month, day = a, b
        else:
            month, day = a, b  # 默认 MM-DD-YYYY
        return _iso(y, month, day) or value

    m = _CN_RE.match(s)
    if m:
        return _iso(int(m.group(1)), int(m.group(2)), int(m.group(3))) or value

    try:  # RFC 2822（邮件头日期）与 "24 Oct 2026"
        return parsedate_to_datetime(s).date().isoformat()
    except (TypeError, ValueError):
        return value
