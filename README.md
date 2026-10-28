# 航运智能账单助手智能体

多格式文档解析（EML / PDF / 图片 / Excel / Word）+ OCR + 大模型语义提取 + 账单匹配与新增/更新，输出供财务系统使用的标准结构化 JSON。

技术栈：**Python 3.11 + LangChain + LangGraph（StateGraph 组织主流程）**，模型 DeepSeek（默认 `deepseek-chat`，AR/AP 佐证默认 `deepseek-reasoner`，可用 `llm.ar_ap_reasoner` 关闭）。

架构设计见 [`航运智能账单助手_架构方案.md`](航运智能账单助手_架构方案.md)。

## 目录结构

```
bill_agent/
  config.py                # 配置加载（.env + config/*.yaml）
  config/                  # settings.yaml（阈值/模型/OCR/预算）+ charge_name_map.yaml（费用名别名）
  graph/                   # LangGraph 节点 + builder（StateGraph 组装）
  parsers/                 # 确定性解析器：EML/PDF文本/OCR/Excel/Word/对象存储 stage
  extraction/              # LLM 结构化提取（Pydantic + DeepSeek JSON mode）
  validation/              # 确定性校验（金额/币种/合计/OCR纠错/重复）+ LLM 语义校验
  standardize/             # 费用名/币种/付款方式标准化
  matching/                # 四规则匹配引擎（CREATE/UPDATE/NO_CHANGE）
  llm/                     # 模型路由 + 结构化输出工厂
  harness/                 # 日志(correlation_id)/预算(token+OCR)/重试
  output/                  # 标准输出 JSON schema
  api/                     # FastAPI + 原生前端（上传 → 后台任务 → 下载 JSON）
  main.py                  # CLI 入口
tests/                     # pytest：确定性逻辑 + mock LLM 端到端
samples/                   # 演示样例生成器 + 样例数据
```

## 快速开始

```bash
uv sync                       # 安装依赖（.venv 内 Python 3.11）
cp .env.example .env          # 填入 DEEPSEEK_API_KEY
uv run python samples/make_samples.py   # 生成样例（debit_note.xlsx + existing_bills.json）
```

## 运行

```bash
# 单个文件：解析 + 提取 + 匹配 + 输出
uv run python -m bill_agent samples/debit_note.xlsx \
  --bills samples/existing_bills.json \
  --entity "Shanghai Ocean Freight Forwarding Co., Ltd." \
  --out output/result.json --pretty

# 多个文件（EML 附件自动递归解析、并行 fan-in）
uv run python -m bill_agent a.eml b.pdf c.jpg d.xlsx --out output/result.json
```

参数：

| 参数 | 说明 |
|------|------|
| `files`（位置） | 单据文件路径，支持 EML/PDF(text+scan)/JPG/PNG/TIFF/XLS/XLSX/DOC/DOCX |
| `--bills` | 已有账单 JSON（用于 UPDATE / NO_CHANGE 匹配） |
| `--entity` | 记账主体（AR/AP 视角基准，可被 `ACCOUNTING_ENTITY` 环境变量/配置覆盖） |
| `--out` | 输出 JSON 文件（默认打印到 stdout） |
| `--pretty` | 缩进美化 |

## Web 界面

```bash
uv run uvicorn bill_agent.api.app:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>：拖拽上传单据（可多选）、填写记账主体、可选上传已有账单 JSON →「开始处理」→ 查看汇总/明细/审计轨迹 →「下载 JSON」。FastAPI 自动接口文档在 `/docs`。

- 只监听 `127.0.0.1`，无鉴权；上传文件存 `output/api_jobs/{job_id}/`，任务结束 30 分钟后清理。
- 后端调用与 CLI 相同的 `run_bill_agent`，输出与 `--out` 同一 schema；`PENDING_REVIEW` 是正常结果（HTTP 200）。
- API 契约与决策记录见 [`前端与API规格.md`](前端与API规格.md)。

## OCR 配置（启用 PDF / 图片识别）

OCR 网关从**公网**拉取 `file` 参数，且出口只覆盖**国内可达**地址（实测：百度 CDN 3.6s 成功；GitHub raw、localhost.run 的 AWS 节点均在 60s 后 504；回环与 RFC1918 地址一律不可达）。启用方式：

**推荐：阿里云 OSS**（国内公网可达，用 oss2 原生签名，不受 MinIO SDK 的 SigV4 限制）

```yaml
# bill_agent/config/settings.yaml
ocr:
  pdf_mode: always          # always=PDF 也走 OCR（题目要求）；scanned_only=仅扫描件走 OCR
stage:
  backend: oss              # 改成 oss
  oss:
    endpoint: https://oss-cn-hangzhou.aliyuncs.com   # 按你的 Bucket 区域改
    access_key: ""          # 或 .env: OSS_ACCESS_KEY_ID
    secret_key: ""          # 或 .env: OSS_ACCESS_KEY_SECRET
    bucket: bill-files      # 需先在阿里云控制台创建
```

改完重启后，页面上方应显示「OCR 就绪」。OSS 桶里的对象不会自动删除，建议在控制台给桶配生命周期规则自动清理（例如 1 天后过期）。

**备选：本机 MinIO + 国内反代**（没有 OSS 时联调用）

```yaml
stage:
  backend: s3               # 或 local
  local:
    public_base_url: https://你的国内反向代理域名   # backend=local 时必填
  s3:
    endpoint: http://localhost:9000                    # 上传走本机
    public_endpoint: https://你的国内OSS/COS/反代域名   # 预签名用，必须国内可达
```

MinIO 本地起法：`docker compose up -d`，凭据读 `.env` 的 `MINIO_ROOT_USER / MINIO_ROOT_PASSWORD`（不要用默认口令）。上传走本机 endpoint，**只有预签名 URL 指向公网 endpoint**——SigV4 只绑定 host，所以本机快传与公网取件互不影响。

未配置国内公网地址时，含文本层的 PDF 自动降级为本地文本提取（`meta.ocr_skipped` 记录**具体缺口**：缺 OSS 凭据 / 缺公网地址，不会一律报「地址不可达」），扫描件/图片快速失败转 `PENDING_REVIEW`，不会干等 300s 超时。

## 测试

```bash
uv run pytest -q     # 201 passed
```

- `test_matching / test_normalize / test_validation / test_parsers / test_output / test_ar_ap / test_ocr`：确定性业务逻辑，含冲突判定、日期归一化、OCR 响应映射（真实 schema 空占位判定；fileType/布尔/Y-N 标记不算内容，垃圾条目回退 PDF 文本层）、S3/OSS staging 路由、EML 内联/单部件附件与扩展名补全、非法文件名（含 Windows 设备名）/未知 charset/签名与日历部件跳过、附件落盘失败隔离、EML/Word 临时目录清理（成功与异常路径）、解析提示限长与单行化（4k 日历部件不再挤掉正文；300 字截断后聚合总数仍可见）、OCR staging 失败回退与预算回滚、OCR 不可用时 PDF 降级与打开错误透传。
- `test_api`：FastAPI HTTP 契约（创建任务→轮询→取结果/下载、上传校验、失败态），mock 掉 `run_bill_agent`，不触发 LLM/OCR。
- `test_e2e_mock`：mock 掉 extract 的 LLM，验证整条图（解析→提取→校验→标准化→匹配→AR/AP→输出）接线正确。
- `tests/conftest.py`：autouse 夹具统一替换 AR/AP 佐证与语义复核的 `get_llm`，并让未 mock 的结构化提取离线失败——**整套测试不发起任何真实 LLM 调用**（不依赖网络状态，也不产生 API 费用）。
- `test_graph`：图编译、中置信度走二次确认、无 key 时降级转 `PENDING_REVIEW`、临时路径脱敏（从行内**可信**路径起点起脱敏的保守策略——起点之前不能出现路径分隔符、左邻必须是外部边界，根被 CJK/ASCII 词粘住时整行脱敏；同行的普通路径也会被一并隐去，换取任何路径形态都不泄漏用户名；覆盖含空格与 , ; : 的组件、`：`/`=`/`->` 后无空格的起点、UNC 与 repr/JSON 四反斜杠根、根相对、大写 marker、生成式 6×7×7×4×9 组合矩阵、`见`/`see` 粘连根与组件以 `(`/`:` 结尾、marker 后紧跟空白/引号的精确终点、marker 与终点同时密集时的线性复杂度）、按实际 prompt 估算 token 预算。
- `tests/fixtures/ocr_response_real.json` 是 OCR API 的真实响应，钉住响应结构，防止解析层再退化。
- `test_ocr` 钉住 OCR 网关瞬时错误（5xx/连接/超时）按 `retry.max_ocr_retries` 指数退避重试、4xx 客户端错误不重试；`test_ar_ap` 钉住 AR/AP 佐证走 `deepseek-reasoner` 强模型。
- `test_ocr` 新增 OCR 结果缓存（完整内容哈希键、TTL 过期、命中不调网关不扣预算）；`test_graph` 新增可选 checkpointer（none/memory/sqlite）与每节点耗时/输出键/预算快照追踪。
- `test_ocr` 新增扫描件/图片 OCR 失败后的降采样二次识别；`test_normalize` 新增费用名建议（未命中时确定性模糊匹配，默认关闭）。

## 说明

- 输出为**顶层 JSON 数组**，每个费用项一条记录。
- 无 `DEEPSEEK_API_KEY` 时，提取节点失败会按 Evaluator-Optimizer 重试上限自动转 `PENDING_REVIEW`（不崩溃、不吞错误），审计轨迹里给出失败原因。
- 解析失败（如 OSS 权限不足、网关 `success:false`、`success` 但 `data.list` 为空或无有效条目、EML 附件解析失败、附件无法解码、LLM 提取异常）同样转 `PENDING_REVIEW`，但真实错误与文件名会写进 `audit_trail`（`解析失败（<文件>）：<原因>`）；部分文件失败时其余文件照常出单，EML 附件失败用附件文件名（不泄漏临时目录路径）。嵌套 EML（`message/rfc822` 附件）整体递归解析，不同邮件的同名附件在并行 fan-in 时按完整内容签名去重，内容不会静默丢失。审计轨迹里的错误文本统一经 URL 凭据（query/fragment/userinfo，`?` 与 `#` 取最早者；含 urllib3 无 scheme 请求路径与协议相对 `//user:pass@host`；URL 后的中文说明保留）与临时落盘路径脱敏收口——`parse_errors`、`meta` 提示（`ocr_error`/`table_error`）、`job.error`、解析提示里的文件名/content_type 同一标准；临时路径从行内**可信**起点开始隐去（起点之前不能出现分隔符、左邻必须是外部边界；根被 `见`/`see` 这类词粘住时整行脱敏——宁可过脱敏，也不让含空格/`;`/`,`/括号的用户名或 repr/JSON 转义路径漏出），不会经第二条通道进入 LLM prompt 或 `ai_reasoning`；OCR 错误通道先截有界扫描窗口（20 万字，远大于任何真实路径上限，保证 marker 不被切掉）再脱敏再限长。OCR/OSS 配置故障不会伪装成「成功转人工」，错误与降级提示按具体缺口报告（缺 `OSS_ACCESS_KEY_ID`/`OSS_ACCESS_KEY_SECRET`、缺 `public_base_url`、私网地址）。
- 标准输出字段见 `bill_agent/output/schema.py`：`operation / document_info / transaction_party / charge_details / financial_attributes / audit_trail`，其中 `charge_details` 额外保留 `original_charge_item`（原始费用名）。
- 同一匹配规则命中多张已有账单 → 该费用输出 `PENDING_REVIEW` 并附候选账单摘要，不静默覆盖。同一单据多行同名费用争用同一账单时，金额唯一一致者认领，其余行判为独立费用；无法唯一判定则全部转人工。
- AR/AP 先按「记账主体在单据中的角色 × 单据类型」给出确定性方向，再由 LLM 结合原文佐证；两者冲突、或记账主体不是签发方/接收方 → `posting_type: null` + 转人工，不猜。
- `config/settings.yaml` 的 `budget.max_total_tokens / max_ocr_calls` 在提取与 OCR 调用前强制检查，超限快速失败而非继续烧 token。
- OCR 网关瞬时错误按 `retry.max_ocr_retries` 指数退避重试，4xx 客户端错误立即上抛；每个图节点记录进入/退出与耗时（correlation_id 结构化日志）。
- OCR 结果按文件内容哈希缓存（`ocr.cache_enabled` / `ocr.cache_ttl_seconds`），同一文件重复处理不重复调网关、不扣 OCR 预算。
- 可选 checkpointer（`persistence.checkpointer = none|memory|sqlite`，默认 `none`）；开启后每次运行以 correlation_id 作为 thread_id 写入 checkpoint，并记录每节点耗时、输出键与 token/OCR 预算快照。
- OCR 失败降级链：PDF 文本层 → 扫描件/图片降采样二次识别（`ocr.degrade_enabled` / `ocr.degrade_scale`）→ 仍失败转人工。
- 费用名未命中字典时，可开启 `standardize.suggest_enabled` 给出建议标准名（离线确定性模糊匹配，供回填字典）。
- DeepSeek V4 思考模式不支持 `tool_choice`：结构化提取关闭思考并走 JSON mode（`llm.structured_thinking: false`），避免 400；AR/AP 仍保留思考模式。
- OCR 成功后对含文本层的 PDF 追加本地全文补充（`ocr.pdf_text_supplement`），补回网关漏掉的币种/后续页内容。
