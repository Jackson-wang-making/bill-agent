# AGENTS.md

- 需求见 `笔试题目和要求.md`，架构决策见 `航运智能账单助手_架构方案.md`。
- 真实样例在仓库根目录：`*.eml`、`PDF 示例账单*.pdf`、`图片示例.png`。改解析层前先用真数据跑一遍，不要只依赖合成 fixtures。
- OCR 响应结构由 `tests/fixtures/ocr_response_real.json` 钉住；改 `bill_agent/parsers/ocr.py` 后必须跑 `uv run pytest tests/test_ocr.py`。
- OCR 的 staging 地址必须**国内公网可达**（私网、境外隧道实测均 504）；配置与 MinIO 启动方式见 README「OCR 配置」。
- Web 层（`bill_agent/api/`）复用 `run_bill_agent`，不得复制业务逻辑；改 API 后跑 `uv run pytest tests/test_api.py`。
- 全量测试：`uv run pytest -q`（当前 196 passed）。测试不得联网：`tests/conftest.py` 的 autouse 夹具替掉 AR/AP 佐证与语义复核的 `get_llm`，未 mock 的结构化提取离线失败。

## Agent skills

### Issue tracker

Issues and specs are stored as local Markdown files under `.scratch/<feature-slug>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

The five triage roles use their default label names. See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repository with `GLOSSARY.md` and `docs/adr/` at the repository root. See `docs/agents/domain.md`.
