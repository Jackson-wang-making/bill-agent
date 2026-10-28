"""validate_final_node：最终输出 JSON 合法性 + schema 校验。

确定性组装失败重跑无意义，故失败直接转 PENDING_REVIEW（不空转重试）。
"""
from __future__ import annotations

from typing import Any

from bill_agent.output.schema import OutputRecord


def validate_final_node(state: dict[str, Any]) -> dict[str, Any]:
    records = state.get("output_records", [])
    issues: list[str] = []
    try:
        _ = [OutputRecord(**r) for r in records]
    except Exception as e:  # noqa: BLE001
        issues.append(f"输出 JSON 非法或不符合 schema: {e}")
    if not records:
        issues.append("无输出记录")

    status = "pass" if not issues else "fail"
    # 终态校验失败就是最终原因：轮次戳必须大于任何重试轮（extract/validate_extract
    # 戳的是它本轮写入的 iteration），否则 pending_review 的「只保留最后一轮」过滤
    # 会丢掉它，让早先轮次的旧错误冒充最终原因
    iteration = state.get("iteration", 0) + 1
    return {
        "validation_status": status,
        "validation_issues": [{"msg": i, "iteration": iteration} for i in issues],
    }
