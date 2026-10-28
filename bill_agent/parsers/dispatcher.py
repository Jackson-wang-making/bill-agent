"""文件路由：MIME/扩展名/内容嗅探（代码规则，非 LLM）。"""
from __future__ import annotations

from pathlib import Path

from bill_agent.config import config

# 扩展名 -> route（image 与扫描 PDF 都走 OCR）
_EXT_MAP = {
    ".eml": "eml",
    ".xls": "excel",
    ".xlsx": "excel",
    ".doc": "word",
    ".docx": "word",
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".tif": "image",
    ".tiff": "image",
    ".pdf": "pdf",
}


def _inspect_pdf(path: str) -> tuple[bool, str | None]:
    """PDF 是否含文本层；打不开时返回真实原因（不能一律当成「无文本层」）。"""
    try:
        import pymupdf

        doc = pymupdf.open(path)
        try:
            for page in doc:
                if page.get_text().strip():
                    return True, None
            return False, None
        finally:
            doc.close()
    except Exception as e:  # noqa: BLE001 - 打开失败的原因必须上抛给审计
        return False, str(e)


def _pdf_has_text(path: str) -> bool:
    """PDF 是否含文本层：有则免 OCR 直接提取，无则走 OCR。"""
    return _inspect_pdf(path)[0]


def _ocr_ready() -> bool:
    from .stage import ocr_delivery_ready

    return ocr_delivery_ready()


def classify_pdf(path: str) -> str:
    """PDF 路由：默认按题目要求走 OCR；OCR 不可用或配置 scanned_only 时用文本层。"""
    ocr_ready = _ocr_ready()
    if config.ocr_pdf_mode != "scanned_only" and ocr_ready:
        return "pdf_ocr"
    has_text, err = _inspect_pdf(path)
    if err is not None:
        # 打不开的 PDF：OCR 可用时交给网关尝试；OCR 不可用时交给文本层解析器
        # 抛出真实打开错误（否则审计会把根因误报成「staging 地址不可达」）
        return "pdf_ocr" if ocr_ready else "pdf_text"
    # OCR 不可达：有文本层则降级本地提取，扫描件仍走 OCR（届时快速失败转人工）
    return "pdf_text" if has_text else "pdf_ocr"


def classify(path: str, mime: str | None = None) -> str:
    """返回 route：eml / pdf_text / pdf_ocr / image / excel / word / unknown。"""
    ext = Path(path).suffix.lower()
    if ext in _EXT_MAP:
        route = _EXT_MAP[ext]
        if route == "pdf":
            return classify_pdf(path)
        return route
    # 扩展名未知：内容嗅探
    if mime:
        if mime.startswith("image/"):
            return "image"
        if mime == "application/pdf":
            return classify_pdf(path)
        if mime.startswith("message/"):
            return "eml"
    return "unknown"


def parse_file(path: str, mime: str | None = None) -> list[dict]:
    """按类型解析单个文件，返回 parsed_documents 列表（EML 可能递归返回多条）。"""
    route = classify(path, mime)

    if route == "eml":
        from .eml import parse_eml

        return parse_eml(path)

    if route == "pdf_text":
        from .pdf_text import parse_pdf_text

        doc = parse_pdf_text(path)
        if not _ocr_ready():
            # OCR 不可用时的降级必须留痕：LLM 上下文与审计轨迹都要说明「未走 OCR」，
            # 并带上具体缺口（缺凭据 / 缺公网地址），不能一律报「地址不可达」
            from .stage import ocr_delivery_blocker

            reason = ocr_delivery_blocker()
            doc.setdefault("meta", {})["ocr_skipped"] = (
                "OCR 未启用，PDF 已降级为本地文本提取"
                + (f"：{reason}" if reason else "（staging 地址不可达）")
            )
        return [doc]

    if route in ("pdf_ocr", "image"):
        from .ocr import parse_ocr

        return parse_ocr(path)

    if route == "excel":
        from .excel import parse_excel

        return [parse_excel(path)]

    if route == "word":
        from .word import parse_word

        return [parse_word(path)]

    return [
        {
            "type": "unknown",
            "source_file": str(path),
            "raw_text": "",
            "tables": [],
            # 只用 basename：附件临时目录路径不得进入审计轨迹
            "meta": {"error": f"无法识别文件类型: {Path(path).name}"},
        }
    ]
