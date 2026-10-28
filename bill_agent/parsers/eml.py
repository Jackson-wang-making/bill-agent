"""EML 解析：邮件头 + 正文 + 附件递归解析。

附件按实际格式再路由（PDF/图片/Excel/Word/嵌套 EML）。
"""
from __future__ import annotations

import email
import html
import mimetypes
import re
import shutil
import tempfile
from email import policy
from pathlib import Path
from urllib.parse import quote

_TAG_RE = re.compile(r"<[^>]+>")

# Windows 保留设备名：写 NUL/CON 会静默丢弃载荷，读回可能阻塞；
# AUX/PRN/COM1..9/LPT1..9 直接 ENOENT。加前缀后就是普通文件名
_WIN_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# 分离签名/日历邀请不是单据内容：当附件会误报「无法识别文件类型」，
# 把本来正常的邮件拖进 PENDING_REVIEW；跳过但写入 meta.ignored_parts 留痕
_IGNORED_PART_TYPES = frozenset(
    {
        "application/pgp-signature",
        "application/pkcs7-signature",
        "application/x-pkcs7-signature",
        "text/calendar",
    }
)


def _strip_html(s: str) -> str:
    s = _TAG_RE.sub(" ", s)
    return html.unescape(s)


def _decode_text(payload: bytes, charset: str | None) -> str:
    """按声明 charset 解码；未知 charset（unknown-8bit 等）回退 UTF-8，不能中断整封邮件。"""
    try:
        return payload.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _safe_filename(name: str | None, idx: int) -> str:
    if not name:
        return f"attachment_{idx}"
    # NUL 必须在 Path 之前处理：write_bytes 会抛 ValueError 并中断整封邮件
    name = str(name).replace("\x00", "_")
    # 只保留文件名的 basename，去掉路径与非法字符（含不间断空格 \xa0）
    name = Path(name).name.replace("\xa0", " ")
    name = re.sub(r"\s+", " ", name).strip()
    # Windows 禁止尾点/尾空格；'..' 这类名字会指向目录，写入必失败
    name = re.sub(r'[\\/:*?"<>|]', "_", name).rstrip(". ")
    if name in ("", ".", ".."):
        return f"attachment_{idx}"
    # 'NUL'、'nul.txt'、'con .pdf'（Windows 会忽略扩展名前的尾空格）这类设备名
    # 会静默吞掉附件或让读回阻塞
    if name.split(".")[0].strip().upper() in _WIN_RESERVED:
        name = f"_{name}"
    if len(name) > 120:
        # 超长文件名在 Windows 上 OSError；截断但保留扩展名供路由识别
        suffix = Path(name).suffix[:20]
        name = name[: 120 - len(suffix)] + suffix
        # 截断点可能落在空格上：Windows 会把它与去掉空格的另一个名字视为同一文件
        name = name.rstrip(". ")
    return name or f"attachment_{idx}"


def parse_eml(path: str) -> list[dict]:
    """解析 EML，返回 [正文文档, 附件文档...]，附件递归解析。"""
    src = Path(path)
    with open(src, "rb") as f:
        msg = email.message_from_binary_file(f, policy=policy.default)

    headers = {}
    for k in ("Subject", "From", "To", "Cc", "Date", "Message-ID"):
        if msg[k] is not None:
            headers[k] = str(msg[k])

    body_parts: list[str] = []
    html_bodies: list[str] = []
    attachments: list[dict] = []
    # 含 Content-ID 的图片：可能是 HTML 正文以 cid: 引用的内联图（签名/排版图），
    # 也可能是带 CID 头的真实单据扫描件。收齐 HTML 后按「是否真的被引用」定归属，
    # 不能一律当内联图静默丢弃（旧实现两种误判方向都踩过）
    cid_images: list[dict] = []
    # 分离签名/日历邀请：跳过并留痕，不当作附件解析（见 _IGNORED_PART_TYPES）
    ignored_parts: list[dict] = []

    def _collect(node) -> None:
        """递归收集正文与附件；嵌套 EML（message/rfc822）整体作为附件，不再下钻
        （msg.walk() 会穿透内层邮件，把内层正文混进外层正文）。"""
        ctype = node.get_content_type()
        cd = node.get_content_disposition()
        filename = node.get_filename()
        is_attach = cd == "attachment" or bool(filename and cd is None)

        if ctype in ("message/rfc822", "message/global"):
            # 嵌套 EML：get_payload(decode=True) 对 message/* 恒为 None，
            # 必须取内层 Message 再序列化回 bytes，走常规附件路由递归解析
            inner = node.get_payload()
            if isinstance(inner, list):
                inner = inner[0] if inner else None
            payload = inner.as_bytes() if inner is not None and hasattr(inner, "as_bytes") else None
            attachments.append(
                {
                    "filename": filename or "forwarded.eml",
                    "content_type": "message/rfc822",
                    "payload": payload,
                }
            )
            return

        if node.is_multipart():
            if is_attach:
                # multipart 附件（如整包附上的 multipart/alternative）：整体序列化后
                # 按 EML 路由递归解析；正文不得混进外层邮件、子部件不得被静默丢弃
                payload = node.as_bytes() if hasattr(node, "as_bytes") else None
                attachments.append(
                    {"filename": filename, "content_type": "message/rfc822", "payload": payload}
                )
                return
            for child in node.iter_parts():
                _collect(child)
            return

        if ctype in _IGNORED_PART_TYPES:
            # 分离签名/日历邀请不是单据；返回前先留痕，不做附件解析
            ignored_parts.append({"filename": filename, "content_type": ctype})
            return
        payload = node.get_payload(decode=True)
        if ctype.startswith("image/") and node.get("Content-ID") is not None:
            # 先挂起（含带 attachment 处置的 Foxmail 签名图），等 HTML 收齐再判定
            cid_images.append(
                {
                    "filename": filename,
                    "content_type": ctype,
                    "payload": payload,
                    "cid": str(node.get("Content-ID") or "").strip().strip("<>"),
                }
            )
            return
        # 叶子部件：非正文部件一律作为附件，不能静默丢弃——包括没有文件名、
        # Content-Disposition 为 inline 或完全缺失的 PDF/图片/二进制部件
        # （旧实现只保留「带文件名的非文本」，真实邮件里踩到过整封只有一个
        # inline 且无 filename 的 PDF，最终误报「正文与附件均为空」）。
        # 文本正文即使带 name 参数仍按正文处理。
        is_attach = cd == "attachment" or ctype not in ("text/plain", "text/html")
        if is_attach:
            # payload 为 None（无法解码）也保留为错误附件，转 PENDING_REVIEW 可追溯
            attachments.append({"filename": filename, "content_type": ctype, "payload": payload})
        elif payload is not None:
            text = _decode_text(payload, node.get_content_charset())
            if ctype == "text/html":
                html_bodies.append(text)
                text = _strip_html(text)
            if text.strip():
                body_parts.append(text)

    if msg.is_multipart():
        for child in msg.iter_parts():
            _collect(child)
    elif msg.get_content_type() in ("message/rfc822", "message/global"):
        # 整个 EML 就是 message/rfc822（整封转发）：内层邮件作为附件递归解析
        inner = msg.get_payload()
        if isinstance(inner, list):
            inner = inner[0] if inner else None
        if inner is not None and hasattr(inner, "as_bytes"):
            attachments.append(
                {"filename": "forwarded.eml", "content_type": "message/rfc822", "payload": inner.as_bytes()}
            )
    else:
        # 单部件邮件：正文类型正常解码（HTML 同样要剥标签）；非文本根部件
        # （整封只有一个 PDF/图片）必须按附件路由，不能把二进制当正文喂给 LLM
        ctype = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if ctype in _IGNORED_PART_TYPES:
            ignored_parts.append({"filename": msg.get_filename(), "content_type": ctype})
        elif ctype in ("text/plain", "text/html"):
            if payload:
                text = _decode_text(payload, msg.get_content_charset())
                if ctype == "text/html":
                    html_bodies.append(text)
                    text = _strip_html(text)
                if text.strip():
                    body_parts.append(text)
        else:
            attachments.append(
                {"filename": msg.get_filename(), "content_type": ctype, "payload": payload}
            )

    # 归属判定：HTML 正文以 cid: 引用（含 @ 被 %40 转义的写法）才是内联图；
    # 未被引用的 CID 图片是真实附件（如单据扫描件），按常规附件递归解析
    html_join = "\n".join(html_bodies)
    inline_parts: list[dict] = []
    for part in cid_images:
        cid = part["cid"]
        referenced = bool(cid) and (
            f"cid:{cid}" in html_join or f"cid:{quote(cid, safe='')}" in html_join
        )
        if referenced:
            # 内联图不是附件，但跳过必须有审计标记，不能无声消失
            inline_parts.append({"filename": part["filename"], "content_type": part["content_type"]})
        else:
            attachments.append(
                {"filename": part["filename"], "content_type": part["content_type"], "payload": part["payload"]}
            )

    meta: dict = {"headers": headers, "parent_email_id": headers.get("Message-ID")}
    if inline_parts:
        meta["inline_cid_parts"] = inline_parts
    if ignored_parts:
        meta["ignored_parts"] = ignored_parts
    docs: list[dict] = [
        {
            "type": "eml",
            "source_file": str(src),
            "raw_text": "\n".join(p for p in body_parts if p),
            "tables": [],
            "meta": meta,
        }
    ]

    # 递归解析附件（写入临时目录再按类型解析）
    from .dispatcher import parse_file

    workdir = Path(tempfile.mkdtemp(prefix="bill_att_"))
    try:
        used_names: set[str] = set()
        for idx, att in enumerate(attachments):
            fname = _safe_filename(att["filename"], idx)
            if not Path(fname).suffix:
                # 无文件名的附件（如 inline PDF）：按 MIME 补扩展名，便于路由与审计辨认
                ext = mimetypes.guess_extension(att.get("content_type") or "") or ""
                if ext:
                    fname = f"{fname}{ext}"
            if fname.lower() in used_names:
                # 同名附件并存时追加序号，避免写入同一临时路径（否则并行 fan-in 去重会静默丢内容）；
                # 追加后仍可能撞上已有名字（如 a.pdf、a.pdf、a_2.pdf），必须循环到真正唯一
                p = Path(fname)
                n = idx
                while fname.lower() in used_names:
                    fname = f"{p.stem}_{n}{p.suffix}"
                    n += 1
            used_names.add(fname.lower())
            att_path = workdir / fname
            if att["payload"] is None:
                # 附件无法解码：错误 doc 转 PENDING_REVIEW，不静默跳过
                sub_docs = [
                    {
                        "type": "error",
                        "source_file": fname,
                        "raw_text": "",
                        "tables": [],
                        "meta": {"error": f"附件无法解码: {fname}"},
                    }
                ]
            else:
                try:
                    # 落盘也必须在本附件的 try 内：非法文件名（'..'、超长）写失败时
                    # 只能让该附件转人工，不能中断整封邮件、丢掉正文与其它附件
                    att_path.write_bytes(att["payload"])
                    sub_docs = parse_file(str(att_path), mime=att["content_type"])
                except Exception as e:  # noqa: BLE001 - 单附件失败不阻断整体
                    sub_docs = [
                        {
                            "type": "error",
                            "source_file": str(att_path),
                            "raw_text": "",
                            "tables": [],
                            "meta": {"error": f"附件解析失败: {e}"},
                        }
                    ]
            for d in sub_docs:
                d.setdefault("meta", {})
                if "attachment_filename" not in d["meta"]:
                    # 直接子文档（本附件的解析产物）才打本附件的标签：嵌套 EML 的
                    # 内层附件由内层 parse_eml 自行标注，覆盖会把孙级文档错标成外层附件名
                    d["meta"]["parent_email_id"] = headers.get("Message-ID")
                    # 用 _safe_filename 的结果（无 filename 时为 attachment_{idx}），
                    # 永不写入 None，避免下游回落到临时目录路径
                    d["meta"]["attachment_filename"] = fname
                    # 附件 doc 的 source_file 用「父邮件名/附件名」：不同邮件的同名附件
                    # （跨文件并行 fan-in）靠它区分，否则撞上同一去重键被静默丢弃；
                    # 临时目录路径不得进入审计轨迹（original_text 头部）或 LLM prompt
                    d["source_file"] = f"{src.stem}/{fname}"
            docs.extend(sub_docs)
    finally:
        # 附件在本次调用内同步解析完毕，文档只保留文本/表格：临时目录必须删除，
        # 否则每次解析都在 %TEMP% 残留一个 bill_att_* 目录
        shutil.rmtree(workdir, ignore_errors=True)

    return docs
