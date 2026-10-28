"""harness 层：日志（correlation_id）/ 预算（token + OCR 调用上限）。

错误处理策略（重试→降级→转人工）落在 graph 节点与 parsers 内，见 builder 的条件边。
"""
