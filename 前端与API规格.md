# 前端与 API 规格（Spec）

> 状态：已实现（2026-10-27），验证情况见 §10。本文是决策记录，不是新的讨论。实现若与本文冲突，先改本文。

## 1. 目标

为现有 CLI（`bill_agent.main.run_bill_agent`）增加一个本机 Web 界面：

1. 浏览器上传单据（EML/PDF/图片/Excel/Word，可多选）；
2. 可选上传已有账单 JSON、填写记账主体；
3. 后端调用**同一条 LangGraph**（不复制业务逻辑）；
4. 页面展示标准化结果（operation / 单据 / 交易方 / 费用 / AR-AP / 审计轨迹）；
5. 一键下载题目要求格式的顶层 JSON 数组。

## 2. 已确认的决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | 前端用**原生 HTML/CSS/JS**，由 FastAPI 同源托管 | 无 Node 构建；`uv run uvicorn ...` 一条命令跑起；评审环境最稳 |
| D2 | 仅监听 `127.0.0.1`，无鉴权 | 考试演示与本地使用；上传文件不出本机 |
| D3 | API 直接调 `run_bill_agent(...)`，CLI 与 Web 共用同一入口 | 行为一致，避免两套逻辑漂移 |
| D4 | `POST /api/jobs` 立即返回 `job_id`，处理放后台线程池；前端 1s 轮询 | 单次处理 3–10s，OCR 可达 300s；同步请求会超时 |
| D5 | 任务存内存 + 30 分钟 TTL，临时目录随任务删除 | 不引入数据库；重启即清空可接受 |
| D6 | `PENDING_REVIEW` 是**正常结果**，HTTP 200，页面黄色标记 | 题目把它定义为一等公民终态，不是错误 |
| D7 | 下载内容 = 顶层 JSON 数组，与 CLI `--out` 同一 schema | 题目「五、标准输出格式」要求；`audit_trail.source_file` 与 `original_text` 头部的落盘路径会还原为上传时的文件名 |
| D8 | 上传校验前置：扩展名白名单、单文件 50MB、空文件拒绝 | 快速失败，避免把无效文件送进 OCR/LLM |
| D9 | 真实客户单据**不上传任何公网服务**；页面只在本机跑 | 已确认：真实 ZIM 发票等不得外传 |

## 3. 非目标（Out of scope）

- 用户体系、登录、多租户；
- 任务持久化与历史列表（刷新/重启后任务消失）；
- WebSocket/SSE 实时进度（轮询足够）；
- 在线编辑识别结果后重新入库；
- CSV/Excel 导出（题目只要求 JSON）；
- 云端部署、HTTPS、反向代理、公网访问；
- 前端自动化 E2E（手动浏览器验收）；
- 修改现有解析/提取/匹配逻辑。

## 4. API 契约

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/` | — | `index.html` |
| GET | `/api/health` | — | `{"status":"ok","ocr_ready":bool}` |
| POST | `/api/jobs` | multipart：`files`（1..N）、`bills`（可选 `.json`）、`accounting_entity`（可选） | `{"job_id":"<hex>"}` |
| GET | `/api/jobs/{id}` | — | `{"job_id","status","stage","elapsed_ms","error","files","summary"}` |
| GET | `/api/jobs/{id}/result` | — | 顶层 JSON 数组（仅 `succeeded`） |
| GET | `/api/jobs/{id}/download` | — | 附件 `bill_result_{id}.json`，`application/json; charset=utf-8` |
| DELETE | `/api/jobs/{id}` | — | `{"deleted":"<id>"}` |

**状态机**：`queued → running → succeeded | failed`。

**summary**：`{"total":n,"create":n,"update":n,"no_change":n,"pending_review":n}`。

**错误**：FastAPI 标准 `{"detail":"..."}`；400 参数/文件非法（缺少 `files` 字段时由 FastAPI 返回 422），404 任务不存在，409 任务未完成，413 文件过大。

## 5. 页面结构与交互

单页、双栏（桌面）/ 纵向（窄屏）：

1. **顶栏**：标题 + OCR 就绪状态点（`/api/health`）。
2. **左栏 · 控制**
   - 拖拽/点选上传区；文件 chips（名称、大小、移除）；
   - 记账主体输入；
   - 已有账单 JSON 选择（显示文件名）；
   - 「开始处理」主按钮（无文件或处理中禁用）。
3. **右栏 · 结果**
   - 状态条：阶段文案 + 已用时 + 动画；
   - 汇总卡片：总数 / CREATE / UPDATE / NO_CHANGE / PENDING_REVIEW；
   - 记录表：operation 徽标、单据类型/编号/日期、签发方/接收方、费用名（原→标准）、金额/币种/付款方式、AR/AP；
   - 行展开：审计轨迹（`ai_reasoning` 列表）、原文片段（`original_text`）、`source_file`；
   - 「下载 JSON」与「原始 JSON」按钮；
   - 空状态与错误提示。

**视觉**：浅色底、系统字体；operation 颜色 CREATE 绿 / UPDATE 蓝 / NO_CHANGE 灰 / PENDING_REVIEW 琥珀；断点 980px。所有来自后端/LLM 的文本一律用 `textContent` 渲染，禁止 `innerHTML` 拼接。

## 6. 实现落点

```
bill_agent/api/
  __init__.py
  app.py            # FastAPI 路由、静态托管、上传校验
  jobs.py           # Job 模型、线程池、状态机、TTL 清理
  static/
    index.html
    styles.css
    app.js
tests/test_api.py   # HTTP 契约测试
```

新增依赖：`fastapi`、`uvicorn[standard]`、`python-multipart`；测试用 `httpx`（FastAPI TestClient 依赖）。

## 7. 测试 seam（唯一）

**HTTP API 契约**，用 `fastapi.testclient.TestClient` 验证：

1. `POST /api/jobs`（1 个 PDF + 1 个 bills JSON）→ 轮询到 `succeeded` → `GET /result` 返回顶层数组；
2. mock 掉 `bill_agent.api.jobs.run_bill_agent`，不触发 LLM/OCR；
3. 下载响应含 `Content-Disposition`，JSON 可解析；
4. 非法扩展名 / 空文件 → 400；未知 job → 404；处理抛异常 → `status=failed` + `error`；
5. `GET /` 返回页面且含标题。

前端不做自动化测试，手动验收清单见下。

## 8. 验收清单

- [ ] `uv run uvicorn bill_agent.api.app:app --host 127.0.0.1 --port 8000` 后打开 `http://127.0.0.1:8000` 可用；
- [ ] 上传 `PDF 示例账单2.pdf` + 记账主体，页面出 3 条记录并显示 AR；
- [ ] 下载的 JSON 与 `uv run python -m bill_agent "PDF 示例账单2.pdf" --entity ... --out out.json` 内容一致；
- [ ] 无 API Key 时任务 `failed`，页面展示原因且不白屏；
- [ ] 处理中刷新页面不崩溃（任务丢失时提示 404）；
- [ ] 任务结束 30 分钟后临时目录被清理。

## 9. 安全

- 默认只绑定 `127.0.0.1`；
- 上传文件存 `output/api_jobs/{job_id}/`（已 gitignore），任务结束按 TTL 删除；
- 响应只返回原始文件名，不暴露服务器绝对路径；
- 同源静态资源，无 CORS；
- 不调用任何外部存储/公网服务（OCR 配置沿用 `settings.yaml`，未配置时自动降级）。

## 10. 实现与验证状态

已验证（自动化）：

- `tests/test_api.py` 13 条 HTTP 契约测试：创建→轮询→结果/下载、下载头与 JSON 可解析、非法扩展名/空文件/坏 bills JSON → 400、未知任务 → 404、处理抛异常 → `failed` + `error`、未完成取结果 → 409、删除任务、文件名清洗、落盘路径还原、首页与健康检查；
- `uv run pytest -q` 全量 202 passed（含后续新增的 OSS staging、解析失败可追溯、OCR 网关异常响应、嵌套 EML 递归解析、fan-in 去重不丢内容、.doc 转换降级可追溯、附件处置判定与错误文本脱敏、任务间预算隔离与并发上限、临时路径脱敏（从行内**可信**路径起点起脱敏的保守策略：起点之前不能出现路径分隔符、左邻必须是外部边界，同行更早的普通路径也会被隐去/含 mkdtemp 下划线后缀/POSIX 与 `：`、`=`、`->` 后无空格起点/UNC 与 repr/JSON 四反斜杠根/Windows 根相对/含空格与 , ; : 的组件/大写 marker/生成式根×组件×包装组合矩阵（含 ensure_ascii=False 与 `见`/`see` 粘连根）/marker 后紧跟空白或引号的精确终点/marker 与终点同时密集时的线性复杂度）、EML 内联与单部件附件、非法文件名（含 Windows 设备名、尾点/尾空格）与未知 charset 不中断整封邮件、附件落盘失败隔离、EML/Word 临时目录清理（成功与异常路径）、解析提示限长与换行清洗（聚合总数在截断后仍可见；文件名与 content_type 元数据同样过 clean_error）、分离签名/日历邀请跳过留痕、同名附件序号循环去重、OCR 真实 schema 空占位判定（fileType/布尔/Y-N 标记不算内容、chargeList 行内标签/页码不算内容）、垃圾条目回退 PDF 文本层、staging 失败降级并回滚 OCR 预算、预算耗尽 PDF 仍走文本层、URL 凭据脱敏（scheme 大小写/无 scheme 请求路径/`?` 与 `#` 取最早者/协议相对 userinfo/七个 CJK 标点后的说明均保留）、meta 提示与 parse_errors 同标准脱敏、OCR 错误通道先截有界扫描窗口再脱敏再限长（spy 钉住窗口上限与 marker 不被切断）、PDF 打开错误透传、按实际 prompt 估算预算、语义复核空响应与 OCR 降级可追溯测试、OCR 配置缺口按具体原因区分（缺凭据/缺公网地址）、上传文件名过滤 Windows 保留设备名与尾点/尾空格、落盘失败 500 详情隐藏任务目录、OCR 网关瞬时错误重试与 4xx 不重试、AR/AP 走 reasoner 强模型、OCR 结果缓存、可选 checkpointer 与节点追踪、OCR 降采样二次识别、费用名建议、思考模式 JSON mode 结构化提取、PDF 文本层补充、PENDING_REVIEW 保留已提取费用）；`tests/conftest.py` 的 autouse 夹具确保整套测试不发起真实 LLM 调用。

已验证（手工，`uv run uvicorn ... --port 8000`）：

- `GET /`、`/static/styles.css`、`/static/app.js` 均 200；`/api/health` 返回 `{"status":"ok","ocr_ready":true}`（`.env` 已填 OSS 凭据；真实 OSS 桶与网关拉取仍需按 README「OCR 配置」实测）；
- 真实 `PDF 示例账单2.pdf` + 记账主体 `NEPTUNE SHIPPING LIMITED (LAX)` → 3 条记录、`posting_type=AR`、下载 JSON 的 `source_file` 为 `PDF 示例账单2.pdf`；
- 无头 Chrome + CDP 跑通页面全流程：选择文件后按钮解禁 → 提交 → 轮询 → 汇总卡片 / 3 行记录 / 展开审计轨迹均正确渲染，无控制台异常。

待人工：拖拽上传、窄屏布局、处理中刷新后的 404 提示、30 分钟 TTL 清理。

已知限制：任务存内存，uvicorn 重启即丢；`/api/jobs/{id}/result` 在 `failed` 时返回 409（错误原因只在 `GET /api/jobs/{id}` 的 `error` 字段）。
