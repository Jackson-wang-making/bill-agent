/* 航运智能账单助手 · 前端
 * 所有后端/LLM 文本一律 textContent 渲染，不拼 innerHTML。
 */
(() => {
  "use strict";

  const MAX_FILE_BYTES = 50 * 1024 * 1024;
  const MAX_BILLS_BYTES = 5 * 1024 * 1024;
  const ACCEPTED = [".eml", ".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff",
    ".xls", ".xlsx", ".doc", ".docx"];

  const el = (id) => document.getElementById(id);

  const ui = {
    fileInput: el("file-input"),
    dropzone: el("dropzone"),
    fileList: el("file-list"),
    entity: el("entity"),
    billsInput: el("bills-input"),
    billsButton: el("bills-button"),
    billsName: el("bills-name"),
    billsClear: el("bills-clear"),
    submit: el("submit"),
    formError: el("form-error"),
    healthDot: el("health-dot"),
    healthText: el("health-text"),
    statusbar: el("statusbar"),
    spinner: el("spinner"),
    statusText: el("status-text"),
    statusElapsed: el("status-elapsed"),
    empty: el("empty"),
    errorBox: el("error-box"),
    resultArea: el("result-area"),
    summary: el("summary"),
    records: el("records"),
    raw: el("raw"),
    download: el("download"),
    showRaw: el("show-raw"),
  };

  const state = {
    files: [],
    bills: null,
    jobId: null,
    timer: null,
    failures: 0,
    result: null,
    busy: false,
  };

  // ---------- 工具 ----------
  const extOf = (name) => {
    const i = String(name).lastIndexOf(".");
    return i >= 0 ? String(name).slice(i).toLowerCase() : "";
  };

  const fmtBytes = (n) => {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
  };

  async function errorDetail(res) {
    try {
      const body = await res.json();
      const d = body && body.detail;
      if (typeof d === "string") return d;
      if (Array.isArray(d)) {
        return d.map((x) => (x && (x.msg || x.message)) || JSON.stringify(x)).join("；");
      }
    } catch (_) { /* 忽略非 JSON 错误体 */ }
    return `请求失败（HTTP ${res.status}）`;
  }

  function setStatus(text, elapsedMs, spinning) {
    ui.statusbar.hidden = false;
    ui.statusText.textContent = text;
    ui.statusElapsed.textContent =
      Number.isFinite(elapsedMs) ? `· 已用时 ${(elapsedMs / 1000).toFixed(1)}s` : "";
    ui.spinner.hidden = !spinning;
  }

  function showError(message) {
    ui.empty.hidden = true;
    ui.errorBox.textContent = message;
    ui.errorBox.hidden = false;
  }

  function clearError() {
    ui.errorBox.textContent = "";
    ui.errorBox.hidden = true;
  }

  function showFormError(message) {
    ui.formError.textContent = message;
    ui.formError.hidden = false;
  }

  function clearFormError() {
    ui.formError.textContent = "";
    ui.formError.hidden = true;
  }

  function resetResultView() {
    clearError();
    state.result = null;
    ui.summary.replaceChildren();
    ui.records.replaceChildren();
    ui.raw.textContent = "";
    ui.raw.hidden = true;
    ui.showRaw.textContent = "原始 JSON";
    ui.resultArea.hidden = true;
    ui.empty.hidden = false;
  }

  function finishJob() {
    state.busy = false;
    if (state.timer) {
      clearTimeout(state.timer);
      state.timer = null;
    }
    ui.spinner.hidden = true;
    updateSubmitState();
  }

  function updateSubmitState() {
    ui.submit.disabled = state.busy || state.files.length === 0;
  }

  // ---------- 文件选择 ----------
  function addFiles(fileList) {
    const rejected = [];
    for (const file of Array.from(fileList)) {
      const ext = extOf(file.name);
      if (!ACCEPTED.includes(ext)) {
        rejected.push(`${file.name}（不支持的类型）`);
        continue;
      }
      if (file.size === 0) {
        rejected.push(`${file.name}（空文件）`);
        continue;
      }
      if (file.size > MAX_FILE_BYTES) {
        rejected.push(`${file.name}（超过 50MB）`);
        continue;
      }
      const dup = state.files.some((f) => f.name === file.name && f.size === file.size);
      if (!dup) state.files.push(file);
    }
    renderFiles();
    updateSubmitState();
    if (rejected.length) showFormError(`已跳过：${rejected.join("；")}`);
    else clearFormError();
  }

  function renderFiles() {
    const items = state.files.map((file, index) => {
      const li = document.createElement("li");
      li.className = "file-chip";

      const name = document.createElement("span");
      name.className = "name";
      name.textContent = file.name;
      name.title = file.name;

      const size = document.createElement("span");
      size.className = "size";
      size.textContent = fmtBytes(file.size);

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "remove";
      remove.textContent = "×";
      remove.setAttribute("aria-label", `移除 ${file.name}`);
      remove.addEventListener("click", () => {
        state.files.splice(index, 1);
        renderFiles();
        updateSubmitState();
      });

      li.append(name, size, remove);
      return li;
    });
    ui.fileList.replaceChildren(...items);
  }

  function setBillsFile(file) {
    if (!file) {
      state.bills = null;
      ui.billsName.textContent = "未选择";
      ui.billsClear.hidden = true;
      return;
    }
    if (extOf(file.name) !== ".json") {
      showFormError("已有账单必须是 .json 文件");
      return;
    }
    if (file.size > MAX_BILLS_BYTES) {
      showFormError("已有账单 JSON 超过 5MB");
      return;
    }
    state.bills = file;
    ui.billsName.textContent = `${file.name}（${fmtBytes(file.size)}）`;
    ui.billsClear.hidden = false;
    clearFormError();
  }

  // ---------- 提交与轮询 ----------
  async function startJob() {
    if (state.busy || state.files.length === 0) return;
    clearFormError();
    resetResultView();
    state.busy = true;
    updateSubmitState();
    setStatus("正在上传…", null, true);

    const form = new FormData();
    for (const file of state.files) form.append("files", file, file.name);
    if (state.bills) form.append("bills", state.bills, state.bills.name);
    form.append("accounting_entity", ui.entity.value.trim());

    try {
      const res = await fetch("/api/jobs", { method: "POST", body: form });
      if (!res.ok) throw new Error(await errorDetail(res));
      const data = await res.json();
      state.jobId = data.job_id;
      state.failures = 0;
      setStatus("排队中", 0, true);
      state.timer = setTimeout(poll, 400);
    } catch (err) {
      finishJob();
      setStatus("提交失败", null, false);
      showError(err.message);
    }
  }

  async function poll() {
    const jobId = state.jobId;
    if (!jobId) return;
    try {
      const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
      if (res.status === 404) {
        finishJob();
        setStatus("任务已失效", null, false);
        showError("任务不存在或已过期（服务可能已重启），请重新上传文件。");
        return;
      }
      if (!res.ok) throw new Error(await errorDetail(res));

      const job = await res.json();
      state.failures = 0;
      const running = job.status === "queued" || job.status === "running";
      setStatus(job.stage || job.status, job.elapsed_ms, running);

      if (job.status === "succeeded") {
        await loadResult(job);
        return;
      }
      if (job.status === "failed") {
        finishJob();
        showError(job.error || "处理失败，请查看服务端日志。");
        return;
      }
      state.timer = setTimeout(poll, 1000);
    } catch (err) {
      state.failures += 1;
      if (state.failures >= 5) {
        finishJob();
        setStatus("连接中断", null, false);
        showError(`无法获取任务状态：${err.message}`);
        return;
      }
      state.timer = setTimeout(poll, 1500);
    }
  }

  async function loadResult(job) {
    try {
      const res = await fetch(`/api/jobs/${encodeURIComponent(state.jobId)}/result`);
      if (!res.ok) throw new Error(await errorDetail(res));
      const data = await res.json();
      state.result = Array.isArray(data) ? data : [];
      renderSummary(job.summary || summarize(state.result));
      renderTable(state.result);
      ui.empty.hidden = true;
      ui.resultArea.hidden = false;
      setStatus(`完成 · ${state.result.length} 条记录`, job.elapsed_ms, false);
    } catch (err) {
      showError(`获取结果失败：${err.message}`);
      setStatus("获取结果失败", job.elapsed_ms, false);
    } finally {
      finishJob();
    }
  }

  function summarize(records) {
    const count = (op) => records.filter((r) => r.operation === op).length;
    return {
      total: records.length,
      create: count("CREATE"),
      update: count("UPDATE"),
      no_change: count("NO_CHANGE"),
      pending_review: count("PENDING_REVIEW"),
    };
  }

  // ---------- 渲染 ----------
  function renderSummary(s) {
    const cards = [
      ["总记录", s.total || 0, ""],
      ["CREATE", s.create || 0, "create"],
      ["UPDATE", s.update || 0, "update"],
      ["NO_CHANGE", s.no_change || 0, "nochange"],
      ["PENDING_REVIEW", s.pending_review || 0, "pending"],
    ];
    const nodes = cards.map(([label, value, cls]) => {
      const card = document.createElement("div");
      card.className = `card ${cls}`.trim();
      const l = document.createElement("div");
      l.className = "label";
      l.textContent = label;
      const v = document.createElement("div");
      v.className = "value";
      v.textContent = String(value);
      card.append(l, v);
      return card;
    });
    ui.summary.replaceChildren(...nodes);
  }

  function makeCell(lines) {
    const td = document.createElement("td");
    lines.forEach((line, i) => {
      if (line == null || line === "") return;
      const div = document.createElement("div");
      div.className = i === 0 ? "cell-main" : "cell-sub";
      div.textContent = String(line);
      td.append(div);
    });
    if (!td.childNodes.length) td.textContent = "—";
    return td;
  }

  function buildRecordRows(record) {
    const doc = record.document_info || {};
    const party = record.transaction_party || {};
    const charge = record.charge_details || {};
    const fin = record.financial_attributes || {};
    const audit = record.audit_trail || {};

    const main = document.createElement("tr");
    main.className = "main-row";
    main.tabIndex = 0;
    main.title = "点击展开 / 收起审计轨迹";
    main.setAttribute("aria-expanded", "false");

    const opCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `badge ${record.operation || ""}`;
    badge.textContent = record.operation || "—";
    opCell.append(badge);

    const chargeLines = [charge.original_charge_item || charge.charge_item];
    if (charge.charge_item && charge.charge_item !== charge.original_charge_item) {
      chargeLines.push(`→ 标准：${charge.charge_item}`);
    }
    if (charge.quantity != null || charge.unit_price != null) {
      const qty = charge.quantity != null ? `数量 ${charge.quantity}` : "";
      const price = charge.unit_price != null ? `单价 ${charge.unit_price}` : "";
      chargeLines.push([qty, price].filter(Boolean).join(" · "));
    }

    const amountLines = [];
    if (charge.amount != null) {
      amountLines.push(`${charge.currency || ""} ${charge.amount}`.trim());
    }
    if (charge.payment_term) amountLines.push(charge.payment_term);

    const arCell = document.createElement("td");
    if (fin.posting_type) {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = fin.posting_type;
      arCell.append(tag);
    } else {
      const tag = document.createElement("span");
      tag.className = "tag none";
      tag.textContent = "待人工";
      arCell.append(tag);
    }

    main.append(
      opCell,
      makeCell([doc.document_type, doc.reference_no, doc.document_date]),
      makeCell([party.issuer, party.recipient ? `→ ${party.recipient}` : null]),
      makeCell(chargeLines),
      makeCell(amountLines),
      arCell,
    );

    const detailRow = document.createElement("tr");
    detailRow.className = "detail-row";
    detailRow.hidden = true;
    const detailCell = document.createElement("td");
    detailCell.colSpan = 6;

    const detail = document.createElement("div");
    detail.className = "detail";

    const reasoning = Array.isArray(audit.ai_reasoning) ? audit.ai_reasoning : [];
    if (reasoning.length) {
      const h = document.createElement("h4");
      h.textContent = "AI 推理与审计轨迹";
      const ul = document.createElement("ul");
      reasoning.forEach((line) => {
        const li = document.createElement("li");
        li.textContent = String(line);
        ul.append(li);
      });
      detail.append(h, ul);
    }

    if (audit.original_text) {
      const h = document.createElement("h4");
      h.textContent = "原文片段";
      const quote = document.createElement("blockquote");
      quote.textContent = String(audit.original_text);
      detail.append(h, quote);
    }

    const source = document.createElement("div");
    source.className = "source";
    const bits = [];
    if (audit.source_file) bits.push(`来源文件：${audit.source_file}`);
    if (fin.accounting_entity) bits.push(`记账主体：${fin.accounting_entity}`);
    source.textContent = bits.join("  ·  ") || "无更多信息";
    detail.append(source);

    detailCell.append(detail);
    detailRow.append(detailCell);

    const toggle = () => {
      detailRow.hidden = !detailRow.hidden;
      main.setAttribute("aria-expanded", String(!detailRow.hidden));
    };
    main.addEventListener("click", toggle);
    main.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        toggle();
      }
    });

    return [main, detailRow];
  }

  function renderTable(records) {
    const table = ui.records;
    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    ["操作", "单据", "交易方", "费用", "金额", "AR/AP"].forEach((title) => {
      const th = document.createElement("th");
      th.textContent = title;
      headRow.append(th);
    });
    thead.append(headRow);

    const tbody = document.createElement("tbody");
    if (!records.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 6;
      td.className = "muted";
      td.textContent = "本次未产生账单记录。";
      tr.append(td);
      tbody.append(tr);
    } else {
      records.forEach((record) => tbody.append(...buildRecordRows(record)));
    }
    table.replaceChildren(thead, tbody);
  }

  // ---------- 事件绑定 ----------
  ui.fileInput.addEventListener("change", () => {
    addFiles(ui.fileInput.files);
    ui.fileInput.value = "";
  });

  ui.dropzone.addEventListener("dragover", (event) => {
    event.preventDefault();
    ui.dropzone.classList.add("dragover");
  });
  ui.dropzone.addEventListener("dragleave", () => {
    ui.dropzone.classList.remove("dragover");
  });
  ui.dropzone.addEventListener("drop", (event) => {
    event.preventDefault();
    ui.dropzone.classList.remove("dragover");
    if (event.dataTransfer && event.dataTransfer.files.length) {
      addFiles(event.dataTransfer.files);
    }
  });

  // 防止文件被拖到页面其他位置时浏览器直接打开
  window.addEventListener("dragover", (event) => event.preventDefault());
  window.addEventListener("drop", (event) => event.preventDefault());

  ui.billsButton.addEventListener("click", () => ui.billsInput.click());
  ui.billsInput.addEventListener("change", () => {
    setBillsFile(ui.billsInput.files[0] || null);
    ui.billsInput.value = "";
  });
  ui.billsClear.addEventListener("click", () => setBillsFile(null));

  ui.submit.addEventListener("click", startJob);

  ui.download.addEventListener("click", () => {
    if (!state.jobId || !state.result) return;
    const link = document.createElement("a");
    link.href = `/api/jobs/${encodeURIComponent(state.jobId)}/download`;
    link.download = `bill_result_${state.jobId}.json`;
    document.body.append(link);
    link.click();
    link.remove();
  });

  ui.showRaw.addEventListener("click", () => {
    if (!state.result) return;
    if (ui.raw.hidden) {
      ui.raw.textContent = JSON.stringify(state.result, null, 2);
      ui.raw.hidden = false;
      ui.showRaw.textContent = "收起 JSON";
    } else {
      ui.raw.hidden = true;
      ui.showRaw.textContent = "原始 JSON";
    }
  });

  // ---------- 健康检查 ----------
  async function checkHealth() {
    try {
      const res = await fetch("/api/health");
      if (!res.ok) throw new Error("health check failed");
      const data = await res.json();
      if (data.ocr_ready) {
        ui.healthDot.className = "dot ok";
        ui.healthText.textContent = "OCR 就绪";
      } else {
        ui.healthDot.className = "dot warn";
        ui.healthText.textContent = "OCR 未配置 · 扫描件转人工";
      }
    } catch (_) {
      ui.healthDot.className = "dot bad";
      ui.healthText.textContent = "服务不可用";
    }
  }

  checkHealth();
  window.addEventListener("focus", checkHealth);
  renderFiles();
  updateSubmitState();
})();
