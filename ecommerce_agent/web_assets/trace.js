/* 只读取 /api/runs/<id> 的结构化结果，用 textContent 渲染，不拼接 HTML。 */
"use strict";
const $ = id => document.getElementById(id);
const node = (tag, className = "", text = "") => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== "") element.textContent = text;
  return element;
};
const RUN_ID = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop() || "");
const statusLabels = { completed: "已完成", incomplete: "未完成", failed: "失败", interrupted: "中断",
  cancelled: "已取消", cancelling: "取消中", running: "进行中", queued: "排队" };
const kindLabels = { started: "开始", model: "模型", tool: "工具", tool_rejected: "拒绝", tool_pending: "待返回",
  settled: "结束", note: "决策" };

function duration(ms) {
  if (ms === null || ms === undefined) return "耗时未知";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(2)} s`;
}

function clock(at) {
  if (!at) return "—";
  const value = new Date(at);
  return Number.isNaN(value.getTime()) ? at : value.toLocaleTimeString("zh-CN", { hour12: false });
}

function chip(text, state = "") {
  return node("span", `chip${state ? " " + state : ""}`, text);
}

function checkTable(checks, onlyFailed = false) {
  const rows = (Array.isArray(checks) ? checks : []).filter(item => !onlyFailed || item.status === "failed");
  if (!rows.length) return null;
  const table = node("table", "checks");
  const head = node("tr");
  for (const label of ["证据", "日期", "字段", "期望值", "结果", "原因"]) head.append(node("th", "", label));
  table.append(head);
  for (const item of rows) {
    const row = node("tr", item.status === "failed" ? "failed" : "");
    row.append(node("td", "", item.evidence_id || "—"), node("td", "", item.date || "—"),
      node("td", "", item.field || "—"), node("td", "", item.equals === undefined ? "—" : String(item.equals)),
      node("td", "", item.status === "passed" ? "通过" : "未通过"), node("td", "", item.reason || "—"));
    table.append(row);
  }
  return table;
}

function renderStep(step) {
  const item = node("li", `step ${step.kind}`);
  const head = node("div", "step-head");
  head.append(node("span", "step-kind", kindLabels[step.kind] || step.kind));
  let title = step.label || "";
  if (step.kind === "started") title = `运行开始${step.mode ? " · " + step.mode : ""}`;
  if (step.kind === "model") title = `模型调用 #${step.step}` + (step.tool_count !== null && step.tool_count !== undefined ? ` · 请求 ${step.tool_count} 个工具` : "");
  if (step.kind === "tool") title = `工具 ${step.tool || step.call_id}` + (step.evidence_id ? ` · 证据 ${step.evidence_id}` : "");
  if (step.kind === "tool_rejected") title = `工具 ${step.tool || step.call_id || "调用"} 被拒绝`;
  if (step.kind === "tool_pending") title = `工具调用 ${step.call_id} 尚未返回`;
  if (step.kind === "settled") title = `运行结束${step.status ? " · " + (statusLabels[step.status] || step.status) : ""}` + (step.stop_reason ? " · " + step.stop_reason : "");
  head.append(node("span", "step-title", title),
    node("span", "step-time", `${clock(step.at)}${step.duration_ms !== undefined && step.duration_ms !== null ? " · " + duration(step.duration_ms) : ""}`));
  item.append(head);

  const body = node("div", "step-body");
  if (step.kind === "tool_rejected") {
    body.append(node("p", "error", step.error || "宿主拒绝执行，未返回具体原因。"));
  } else if (step.kind === "tool") {
    body.append(node("p", "", step.ok ? "工具返回成功。" : (step.error || "工具返回失败。")));
  } else if (step.kind === "note") {
    body.append(node("p", "", step.detail || ""));
  }
  if (step.answer) body.append(node("p", "answer", step.answer));
  if (Array.isArray(step.evidence_ids) && step.evidence_ids.length) {
    body.append(node("p", "", `引用证据：${step.evidence_ids.join("、")}`));
  }
  if (step.verification) {
    const verification = step.verification;
    body.append(node("p", "", `字段断言：${verification.status === "passed" ? "通过" : verification.status === "failed" ? "未通过" : "仅有引用"} · 核对 ${verification.checked ?? 0} 项 · 失败 ${verification.failed ?? 0} 项`));
    const table = checkTable(verification.checks, step.kind === "tool_rejected");
    if (table) body.append(table);
  }
  if (body.childNodes.length) item.append(body);
  return item;
}

function renderValidation(validation) {
  const target = $("validation");
  target.replaceChildren();
  if (!validation) {
    target.append(node("p", "empty", "本次运行没有写入字段验收（可能是纯查询、执行失败或旧版本运行）。"));
    return;
  }
  const box = node("div", "validation-box");
  const status = validation.status === "passed" ? "字段一致" : validation.status === "failed" ? "未通过" : "仅有引用，尚无字段断言";
  box.append(node("p", "", `状态：${status} · 核对 ${validation.checked ?? 0} 项 · 失败 ${validation.failed ?? 0} 项`));
  if (validation.scope) box.append(node("p", "", validation.scope));
  const table = checkTable(validation.checks);
  if (table) box.append(table);
  target.append(box);
}

function renderEvidence(rows) {
  const target = $("evidence");
  target.replaceChildren();
  if (!Array.isArray(rows) || !rows.length) {
    target.append(node("p", "empty", "本次运行没有可读的证据文件。"));
    return;
  }
  for (const row of rows) {
    const card = node("article", "");
    card.append(node("strong", "", row.tool || "工具结果"), node("small", "", row.evidence_id || "无证据 ID"));
    if (row.table) card.append(node("small", "", `表 ${row.table}${row.rows === null || row.rows === undefined ? "" : " · " + row.rows + " 条记录"}`));
    target.append(card);
  }
}

function render(trace) {
  const summary = trace.summary || {};
  $("run-title").textContent = `${trace.sku_id || "未知商品"} · ${trace.target_date || "未知日期"}`;
  $("trace-status").textContent = `运行 ${trace.run_id} · ${statusLabels[trace.status] || trace.status || "状态未知"} · ${trace.mode || "模式未知"}`;
  $("trace-status").className = trace.status === "completed" ? "synced" : "changed";
  const meta = $("run-meta");
  meta.replaceChildren(chip(duration(summary.duration_ms ?? trace.duration_ms)),
    chip(`工具 ${summary.tool_calls ?? "未知"} · 模型 ${summary.model_calls ?? "未知"}`));
  const tokens = summary.tokens;
  meta.append(chip(tokens && tokens.total !== null && tokens.total !== undefined ? `tokens ${tokens.total}` : "tokens 未知"));
  meta.append(chip(`拒绝 ${summary.rejections ?? 0}`, summary.rejections ? "bad" : "good"));
  const assertions = summary.assertions;
  meta.append(chip(assertions ? `断言 ${assertions.checked ?? 0} 项 / ${assertions.failed ?? 0} 失败` : "断言 未写入",
    assertions && assertions.failed ? "bad" : assertions ? "good" : ""));
  if (trace.sample_data) meta.append(chip("样例数据", "bad"));
  if (trace.question) meta.append(chip(`问题：${String(trace.question).slice(0, 60)}`));

  const timeline = $("timeline");
  timeline.replaceChildren();
  if (!Array.isArray(trace.steps) || !trace.steps.length) {
    timeline.append(node("li", "empty", "没有可解析的事件记录。"));
  } else {
    for (const step of trace.steps) timeline.append(renderStep(step));
  }
  renderValidation(trace.validation);
  renderEvidence(trace.evidence);
  $("raw").textContent = JSON.stringify(trace, null, 2);
}

async function load() {
  $("trace-status").textContent = "正在读取运行记录…";
  $("trace-status").className = "";
  const headers = {};
  const token = sessionStorage.getItem("signal-access-token");
  if (token) headers.Authorization = `Bearer ${token}`;
  const store = localStorage.getItem("flower-store-id");
  if (store) headers["X-Store-ID"] = store;
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(RUN_ID)}`, { headers });
    if (response.status === 401) throw new Error("请先在工作台连接访问口令，再在同一标签页打开轨迹。");
    if (response.status === 404) throw new Error("运行记录不存在，或该任务没有留下执行目录。");
    if (!response.ok) throw new Error(`读取失败（HTTP ${response.status}）。`);
    render(await response.json());
  } catch (error) {
    $("trace-status").textContent = error.message;
    $("trace-status").className = "changed";
  }
}

$("refresh-trace").addEventListener("click", load);
load();