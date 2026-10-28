"""LLM 结构化输出 Pydantic schema（提取层）。

强类型约束金额/币种等字段，满足「金额必须数字、币种三位」等处理要求。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class DocumentInfo(BaseModel):
    """单据头 + 匹配键。bl_no/booking_no/container_no 是匹配规则 2/3/4 的键。"""

    document_type: Optional[str] = Field(default=None, description="Debit Note / Credit Note / Invoice 等")
    reference_no: Optional[str] = Field(default=None, description="单据编号")
    document_date: Optional[str] = Field(default=None, description="单据日期 YYYY-MM-DD")
    bl_no: Optional[str] = Field(default=None, description="提单号")
    booking_no: Optional[str] = Field(default=None, description="订舱号")
    container_no: Optional[str] = Field(default=None, description="箱号")


class TransactionParty(BaseModel):
    issuer: Optional[str] = Field(default=None, description="签发方")
    recipient: Optional[str] = Field(default=None, description="接收方")


class ChargeItem(BaseModel):
    charge_item: str = Field(description="原始费用名称（保留原文）")
    quantity: Optional[float] = Field(default=None, description="数量")
    unit_price: Optional[float] = Field(default=None, description="单价")
    amount: float = Field(description="金额（数字类型）")
    currency: str = Field(description="三位币种代码，如 USD/CNY")
    payment_term: Optional[str] = Field(default=None, description="PP(预付)/CC(到付)")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="该项提取置信度 0-1")


class ExtractionResult(BaseModel):
    """extract_node 的单次结构化输出。"""

    document_info: DocumentInfo
    transaction_party: TransactionParty
    charges: list[ChargeItem]
    subtotal: Optional[float] = Field(default=None, description="小计")
    tax: Optional[float] = Field(default=None, description="税额")
    total: Optional[float] = Field(default=None, description="总计")
    overall_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
