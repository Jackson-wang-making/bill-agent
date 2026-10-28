"""提取层 prompt。"""
from __future__ import annotations

SYSTEM_PROMPT = """你是航运账单数据提取专家。从输入的邮件/PDF/图片OCR/Excel/Word 文本中，
提取航运费用账单的结构化字段，并以 JSON 输出。

严格规则：
1. 不得虚构输入材料中不存在的字段；无法确定的字段一律用 null。
2. 金额、数量、单价必须是数字类型；币种统一为三位 ISO 4217 代码（如 USD、CNY）。
3. 保留费用的原始名称到 charge_item（不要自行标准化）。
4. 每个费用项目是 charges 数组中的一项。
5. 修正 OCR 常见识别错误：金额/编号中的 0/O、1/l/I 按上下文判断为数字。
6. document_info 中除单据类型/编号/日期外，尽量提取提单号(bl_no)、订舱号(booking_no)、箱号(container_no)，
   这些是账单匹配的关键字段。
7. payment_term 只能是 PP（预付）或 CC（到付）。
8. 输出必须是合法 JSON，只输出 JSON，不要输出任何解释文字。
"""


def build_user_prompt(doc_text: str, feedback: str | None = None) -> str:
    """构造用户消息：文档内容 + 可选的重试反馈。"""
    text = doc_text if len(doc_text) <= 30000 else doc_text[:30000]
    if feedback:
        return (
            f"上一轮提取存在以下问题，请修正后重新提取：\n{feedback}\n\n"
            f"---- 待提取文档内容 ----\n{text}\n---- 结束 ----\n请输出 JSON。"
        )
    return f"---- 待提取文档内容 ----\n{text}\n---- 结束 ----\n请输出 JSON。"
