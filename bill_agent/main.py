"""CLI 入口：航运智能账单助手。

用法：
  python -m bill_agent a.pdf b.xlsx --bills existing.json --entity "XX 航运" --out result.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from bill_agent.config import config
from bill_agent.graph.builder import build_graph
from bill_agent.harness.budget import get_budget, reset_budget
from bill_agent.harness.logger import get_logger, new_correlation_id


def _config_snapshot() -> dict[str, Any]:
    return {
        "llm_model": config.llm_model,
        "t_high": config.t_high,
        "t_low": config.t_low,
        "max_extract_retries": config.max_extract_retries,
        "max_total_tokens": config.max_total_tokens,
        "max_ocr_calls": config.max_ocr_calls,
        "stage_backend": config.stage.get("backend", "local"),
    }


def build_checkpointer() -> Any:
    """按配置构造 LangGraph checkpointer（none 返回 None，保持无状态单次运行）。"""
    kind = config.checkpointer
    if kind == "none":
        return None
    if kind == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()
    if kind == "sqlite":
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver

        path = config.checkpointer_sqlite_path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # 图节点可能在并行线程里跑：SQLite 连接必须允许跨线程
        conn = sqlite3.connect(path, check_same_thread=False)
        return SqliteSaver(conn)
    raise ValueError(f"未知 checkpointer 配置：{kind}（可选 none / memory / sqlite）")


def run_bill_agent(
    source_files: list[str],
    existing_bills: list[dict[str, Any]] | None = None,
    accounting_entity: str | None = None,
    checkpointer: Any | None = None,
) -> list[dict[str, Any]]:
    """运行整条图，返回标准输出记录列表。"""
    cid = new_correlation_id()
    # 每次运行独立预算：CLI 重复调用与 Web 线程池复用线程时，计数不得跨任务累计
    reset_budget()
    log = get_logger("bill_agent")
    log.info("start bill agent: %d files", len(source_files))

    saver = build_checkpointer() if checkpointer is None else checkpointer
    graph = build_graph().compile(checkpointer=saver)
    initial: dict[str, Any] = {
        "source_files": [{"path": str(f), "mime": None} for f in source_files],
        "existing_bills": existing_bills or [],
        "accounting_entity": accounting_entity or config.accounting_entity or "",
        "config": _config_snapshot(),
        "iteration": 0,
        "retry_count": 0,
        "budget_consumed": {"tokens": 0, "ocr_calls": 0},
    }
    if saver is not None:
        result = graph.invoke(initial, config={"configurable": {"thread_id": cid}})
    else:
        result = graph.invoke(initial)
    log.info(
        "finish bill agent: %d files -> %d records, budget=%s",
        len(source_files),
        len(result.get("output_records", [])),
        get_budget().snapshot(),
    )
    return result.get("output_records", [])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="航运智能账单助手（LangChain + LangGraph）")
    parser.add_argument("files", nargs="+", help="单据文件路径（EML/PDF/图片/XLS/XLSX/DOC/DOCX）")
    parser.add_argument("--bills", help="已有账单 JSON 文件路径")
    parser.add_argument("--entity", help="记账主体（AR/AP 视角基准）")
    parser.add_argument("--out", help="输出 JSON 文件路径（默认打印到 stdout）")
    parser.add_argument("--pretty", action="store_true", help="缩进美化输出")
    args = parser.parse_args(argv)

    existing: list[dict[str, Any]] = []
    if args.bills:
        existing = json.loads(Path(args.bills).read_text(encoding="utf-8"))

    records = run_bill_agent(args.files, existing, args.entity)
    # 题目「五、标准输出格式」是顶层 JSON 数组，直接输出记录列表
    text = json.dumps(records, ensure_ascii=False, indent=2 if args.pretty else None)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"已写入 {out_path}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
