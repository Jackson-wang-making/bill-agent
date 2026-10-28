"""审计文本脱敏：URL 凭据（query/fragment/userinfo）与临时落盘路径。

parse_errors、meta 提示（ocr_error/table_error）、job.error 都从这里过一道，
保证同一段文本在 LLM prompt、ai_reasoning、original_text 中一致脱敏——
任何一条旁路漏掉都会把预签名参数或本机用户名带进审计轨迹。
"""
from __future__ import annotations

import re

# 1) 完整 URL（scheme 大小写混写同样命中）：去掉 query/fragment/userinfo。
#    scheme 名限长（现实中最长也就十几个字符）：不限长会在长单词的每个起点
#    向后扫到词尾，32k 个字母实测 1s（二次方）。
# 2) 协议相对 URL（//user:pass@host/...，没有 scheme）：authority 带 userinfo 才匹配。
# 匹配在 CJK 标点处停止：中文报错里 URL 后面的「；后续说明」不是 URL 的一部分，
# 一律吞掉会把说明文字也删掉（过脱敏）。
# 无 scheme 的请求路径（/ai/process?... 或 /ai/process#api_token=...）不走正则：
# 对每个起点向后找 '?'/'#' 会在 "/+"*n 这类 token 上退化为二次方，
# 改为按「空白 + CJK 标点」切 token 逐段处理（见 _strip_schemeless_queries）。
_URL_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9+.-]{0,31}://[^\s\"'<>；，。：、！？]+"
    r"|//(?=[^\s\"'<>/]{0,256}@)[^\s\"'<>；，。：、！？]+"
)
_TOKEN_RE = re.compile(r"[^\s；，。：、！？]+")


def _strip_url(url: str) -> str:
    """保留 scheme/host/path，去掉 userinfo、query 与 fragment。"""
    url = url.partition("?")[0].partition("#")[0]
    scheme, sep, rest = url.partition("://")
    if not sep:
        # 协议相对 URL（//user:pass@host/path）没有 scheme，但 userinfo 同样要剥
        if url.startswith("//"):
            head, slash, tail = url[2:].partition("/")
            if "@" in head:
                head = head.rsplit("@", 1)[1]
            return f"//{head}{slash}{tail}"
        return url
    # authority 里可能带 user:pass@：只保留 host[:port]
    head, slash, tail = rest.partition("/")
    if "@" in head:
        head = head.rsplit("@", 1)[1]
    return f"{scheme}{sep}{head}{slash}{tail}"


def _strip_schemeless_queries(text: str) -> str:
    """去掉无 scheme 请求路径（/ai/process?token=...#...）的 query/fragment。

    按空白 + CJK 标点切 token，每个 token 只做一次线性查找。两个细节：
    - '?' 与 '#' 取最早者：'#api_token=...?x=1' 里 fragment 在 query 之前，
      只看 '?' 会把 fragment 里的凭据留在 token 里（round 13 回归）；
    - CJK 标点当分隔符：'…?token=x；后续说明' 只删到 token 结束，说明文字保留
      （与 scheme URL 在 CJK 标点处停止的语义一致）。
    """
    out: list[str] = []
    last = 0
    for m in _TOKEN_RE.finditer(text):
        out.append(text[last:m.start()])
        token = m.group(0)
        q = token.find("?")
        h = token.find("#")
        cut = q if q >= 0 and (h < 0 or q <= h) else h
        if cut >= 0 and "/" in token[:cut]:
            token = token[:cut]
        out.append(token)
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def redact_urls(text: str) -> str:
    """去掉文本里 URL 的凭据部分（预签名参数、api_token、userinfo）。"""
    return _strip_schemeless_queries(_URL_RE.sub(lambda m: _strip_url(m.group(0)), text))


# 临时附件/转换落盘路径（bill_att_xxx、bill_doc_xxx）不得进入审计轨迹。
# 逐行扫描：先定位 marker，再找**可信**的路径起点，从那里开始脱敏；找不到
# 可信起点就整行脱敏（相对路径没有可验证的根，局部切分可能留下前缀）。
#
# 起点左边界只排除「可能是路径名续字符」的字符：ASCII 词字符、. ~ % - + 与
# 分隔符。CJK 等非 ASCII 字符不排除——'失败：见/home/...' 里路径根直接粘在
# 中文后面，左边太严会把根起点漏掉。认错方向的代价不对称：多认一个起点只
# 导致过脱敏（删得更多），少认一个起点才会把用户名留在明文。
#
# 可信起点（round 14）：起点之前不能出现任何路径分隔符，且左邻必须是外部
# 边界（行首、空白、标点、CJK 标点）。路径组件可以含空格、括号、逗号、冒号
# （'Program Files (x86)'、'Smith, John'、'ci:run'），所以「起点之前已有
# 分隔符」时那个斜杠只是路径内部的分隔符，不能当起点——否则根被前一个词粘住
# 时（'见/home/jack/...'、'see/home/...'）路径内部的斜杠会成为最早起点，
# 用户名前缀留在明文。没有可信起点时整行脱敏，宁可过脱敏。
#
# 不做「语句边界切分」：任何「边界之后有新起点就从句首切开」的规则都能被路径
# 本身骗过——组件里出现空格后紧跟分隔符（'CI, Build Agent \\bill_att_x'）时，
# 该分隔符看起来就是一个新起点，前面的用户名整段留下（round 13 blocker）。
_LEFT_RE = r"(?:\A|(?<![0-9A-Za-z_.~%/\-+]))"
_PATH_START_RE = re.compile(
    _LEFT_RE
    # POSIX 起点不匹配 '//'：避免把 https:// 的 authority 当成路径起点
    # （否则同一行里的 URL 会被整段吞掉）；UNC 由反斜杠分支负责。
    # UNC 不排除后随反斜杠：repr/JSON 里 UNC 根是四个反斜杠（'\\\\srv'），
    # 排除后随反斜杠会让根起点认不出，最早起点退到路径内部（round 13 回归）。
    + r"(?:[A-Za-z]:[\\/]|\\\\(?=[^\s])|\\(?=[^\s\\])|/(?=[^/\s]))"
)
# 起点左界：只有「不会出现在路径组件末尾」的字符才被信任。
# 词字符（含 CJK）、')' ']' '}' ',' ';' 都不信任——组件可以以它们结尾
# （'Program Files (x86)/bill_att_x'、'Smith, John'），此时该斜杠只是
# 路径内部的分隔符，从那里起删会把前缀留在明文。
_TRUSTED_LEFT = frozenset("\"'`:=([{<>|")
_CJK_PUNCT = frozenset("；，。：、！？")


def _trusted_start(line: str, anchors: list[int]) -> int | None:
    """行内第一个可信的路径起点（没有则 None，退化为整行脱敏）。

    可信 = 起点之前没有出现任何路径分隔符（否则起点在路径内部——组件可以
    含空格/括号/冒号，切在哪里都可能留下前缀），且左邻是外部边界。anchors
    有序，起点之前一旦有分隔符，后面的只会更晚，直接判定没有可信起点。
    """
    first_sep = min(
        (i for i in (line.find("/"), line.find("\\")) if i >= 0),
        default=None,
    )
    for a in anchors:
        if a == 0:
            return 0
        if first_sep is not None and first_sep < a:
            return None
        left = line[a - 1]
        if left.isspace() or left in _TRUSTED_LEFT or left in _CJK_PUNCT:
            return a
    return None
# Windows 工具链会把路径转成大写：marker 必须大小写不敏感
_TMP_MARKER_RE = re.compile(r"bill_(?:att|doc)_\w+", re.IGNORECASE)
# 替换终点：空白/引号/行尾（与旧 _REDACT_TAIL_RE 同语义）。
# 用一次 finditer + 单调指针求每个 marker 的终点：marker 密集且没有空白分隔时
# （'bill_att_a/'*n），逐 marker 用正则向后扫整行会退化为二次方（200KB 实测 5.5s）。
_TAIL_STOP_RE = re.compile(r"[ \r\n\"']")


def _line_spans(line: str) -> list[tuple[int, int]]:
    """一行内所有临时路径的替换区间（从行内最早路径起点到 marker 尾部）。

    markers/anchors/tail 都按行单次扫描，指针单调右移，整体线性。
    """
    markers = list(_TMP_MARKER_RE.finditer(line))
    if not markers:
        return []
    anchors = [m.start() for m in _PATH_START_RE.finditer(line)]
    stops = [s.start() for s in _TAIL_STOP_RE.finditer(line)]
    trusted = _trusted_start(line, anchors)

    spans: list[tuple[int, int]] = []
    si = 0  # 下一个可能的替换终点
    for m in markers:
        # 没有可信起点（相对路径、根被前一个词粘住）：整行脱敏。
        # 相对路径没有可验证的起点，从 token/标点起算都可能把前一段
        # 路径留在明文（'run/ci, dir/bill_att_x'），只有行首是安全下界。
        start = trusted if trusted is not None and trusted < m.start() else 0
        while si < len(stops) and stops[si] < m.end():
            si += 1
        tail_end = stops[si] if si < len(stops) else len(line)
        spans.append((start, tail_end))
    return spans


def redact_temp_paths(text: str) -> str:
    """把临时落盘路径替换为占位符；没有 marker 时零成本跳过。"""
    if not _TMP_MARKER_RE.search(text):
        return text
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        spans = _line_spans(line)
        if not spans:
            out.append(line)
            continue
        merged: list[tuple[int, int]] = []
        for s, e in sorted(spans):
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        pieces: list[str] = []
        last = 0
        for s, e in merged:
            pieces.append(line[last:s])
            pieces.append("<临时附件路径>")
            last = e
        pieces.append(line[last:])
        out.append("".join(pieces))
    return "".join(out)


def clean_error(text: str) -> str:
    """审计文本的唯一脱敏收口：URL 凭据与临时附件路径。"""
    return redact_temp_paths(redact_urls(text))
