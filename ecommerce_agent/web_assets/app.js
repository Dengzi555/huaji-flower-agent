/* 网页适配层：只调用 API 和显示结果，不在浏览器重新计算业务指标。
 * 学习重点：文本统一用 textContent，防止问题/来源里的 HTML 被当成页面代码执行。
 * 页面刷新后以任务 ID 重新获取数据库记录，任务运行不依赖这个标签页是否打开。
 */
"use strict";
const $ = (id) => document.getElementById(id);
const labels = {
  queued: "排队中", running: "调查中", completed: "调查完成", needs_clarification: "需要补充信息",
  needs_data: "缺少业务数据", unsupported: "暂不支持", failed: "执行失败", incomplete: "未完成", interrupted: "执行曾中断",cancelling:'正在停止',cancelled:'已停止'
};
const domains = { sales: "销售", traffic: "流量", inventory: "库存", promotions: "促销", traffic_channels: "渠道归因",sales_trend:"销量趋势",calendar:"经营日历",order_cohorts:"支付订单批次" };
const intentLabels = { investigate_sales_change: "销量变化调查", query_sales_performance: "销量表现与趋势", query_inventory: "库存与缺货查询", query_promotions: "活动状态查询" };
const toolNames = { get_sales_metrics: "查询销售数据", get_traffic_metrics: "查询商品流量", get_inventory_status: "查询库存状态", get_promotion_history: "查询促销记录", analyze_metrics: "计算指标并检查证据" };
const signals = { stockout_signal: "缺货相关信号", traffic_signal: "流量相关信号", promotion_ended_signal: "活动变化信号" };
toolNames.get_channel_breakdown = "查询互斥归因渠道明细";toolNames.get_sales_trend="查询多日销量与基线";toolNames.get_calendar_context="查询节假日与调休日历";
toolNames.get_order_funnel='查询同批订单取消与完成情况';
toolNames.get_product_group_sales='查询商品范围并统计销量';domains.product_group_sales='商品范围销量';
let meta = null, currentTask = null, activeId = localStorage.getItem("signal-task-id"), pollTimer = null, selectedTab = "summary";
let inspectorPreference = null;
let conversation=null, conversationId=sessionStorage.getItem('flower-conversation-id'), conversationBusy=false;
let pendingRequest=null,stopRequested=false,stopInProgress=false;
let currentCase = null;
let currentStore = localStorage.getItem("flower-store-id") || "", storeEpoch = 0, adEpoch = 0, activeModule = "overview", adReport = null;
let storeDirectory = [];

const reviewLabels = {awaiting_human:"等待人工核验",ready_for_recheck:"材料已保存，待复查",rechecking:"正在复查",awaiting_acceptance:"复查完成，待人工验收",resolved:"已验收关闭"};
const correctionFields = {
  inventory: [["out_of_stock_minutes","缺货分钟数","int"],["monitored_minutes","监测分钟数","int"]],
  sales: [["paid_orders","支付订单数","int"],["paid_buyers","支付买家数","int"],["units_sold","支付件数","int"],["paid_revenue","支付金额","float"]],
  traffic: [["visitors","商品访客 UV","int"]],
  promotions: [["active","当天活动是否开启","bool"],["campaign_name","活动名称（无活动可留空）","text"]]
};

function renderCorrectionFields() {
  // 业务表单收集明确指标；SKU 从工单上下文固定，用户不能通过表单换调查对象。
  const table = $("correction-table").value;
  $("correction-fields").replaceChildren(); $("correction-fields").hidden = !table;
  $("correction-date").disabled = !table; $("correction-date").required = !!table;
  if (currentCase) {
    $("correction-date").min = currentCase.scope.comparison_date;
    $("correction-date").max = currentCase.scope.target_date;
    $("correction-date").value = currentCase.scope.target_date;
  }
  for (const [field,title,type] of correctionFields[table] || []) {
    const label = node("label", "", title), input = node(type === "bool" ? "select" : "input");
    input.dataset.correction = field; input.dataset.valueType = type; input.required = type !== "text";
    if (type === "bool") {
      for (const [value,text] of [["","请选择"],["true","开启"],["false","未开启"]]) {const option=node("option","",text);option.value=value;input.append(option);}
    } else if (type !== "text") {input.type="number";input.min="0";input.step=type === "int" ? "1" : "0.01";}
    label.append(input); $("correction-fields").append(label);
  }
}

async function loadReview(caseId) {
  const requestedTaskId = activeId;
  const {case: item} = await api(`/api/reviews/${caseId}`);
  if (activeId !== requestedTaskId || currentTask?.review_case?.id !== caseId || currentCase?.id === caseId && currentCase.revision > item.revision) return;
  const changed = currentCase?.id !== item.id;
  currentCase = item; $("review-panel").hidden = false;
  if (changed) {$("material-form").reset(); $("accept-form").reset(); renderCorrectionFields();}
  $("review-status").textContent = reviewLabels[item.status];
  $("review-scope").textContent = `${item.scope.sku_id} · ${item.scope.target_date} · 版本 ${item.revision} · ${item.runs.length} 次调查记录`;
  $("review-reasons").replaceChildren(...item.reasons.map((text)=>node("p","",text)));
  $("review-materials").replaceChildren();
  for (const material of item.materials) {
    const row = node("div","material-record");
    row.append(node("strong","",material.source),node("p","",material.note),node("small","",`人工提交，未独立核验 · ${material.corrections.length} 条指标修正`));
    $("review-materials").append(row);
  }
  $("material-form").hidden = ["rechecking","resolved"].includes(item.status);
  $("recheck").hidden = item.status !== "ready_for_recheck";
  $("accept-form").hidden = item.status !== "awaiting_acceptance" || currentTask.id !== item.latest_task_id;
  $("review-action-hint").textContent = item.status === "awaiting_human" ? "请先核对并保存材料；仅补说明不会自动消除数据冲突。" : item.status === "resolved" ? "工单已人工验收，原始报告和材料仍保留。" : "每个工单最多复查 3 次；提交后生成关联的新任务。";
  if(item.status === "awaiting_acceptance" && currentTask.id !== item.latest_task_id) $("review-action-hint").textContent="当前显示旧报告，请从下方时间线打开最新复查后再验收。";
  $("review-runs").replaceChildren();
  for (const run of item.runs) {
    const button = node("button","text-button",`${run.id.slice(0,8)} · ${labels[run.status] || run.status} · 数据版本 ${run.fingerprint.slice(0,8)}`);
    button.addEventListener("click",()=>selectTask(run.id)); $("review-runs").append(button);
  }
  const names = {case_opened:"创建核验工单",material_added:"追加人工材料",recheck_requested:"请求新一轮复查",recheck_settled:"复查完成",recheck_interrupted:"复查中断，返回人工核验",case_accepted:"人工验收关闭"};
  $("review-events").replaceChildren(...item.events.map((event)=>node("p","limits",`${new Date(event.created_at).toLocaleString("zh-CN")} · ${names[event.type] || event.type}${event.payload.note ? " · " + event.payload.note : ""}`)));
}

function setInspector(open, remember = false) {
  // 面板只是同一个任务的视图。用户手动关闭后，轮询不能反复把它打开。
  $("inspector").hidden = !open;
  document.body.classList.toggle("inspector-open", open);
  $("toggle-inspector").setAttribute("aria-expanded", String(open));
  if (remember) inspectorPreference = open;
}

function renderInspector(task = null) {
  const trace=task?.parsed.conversation_context;
  $("conversation-trace").replaceChildren();
  if(trace){$("conversation-trace").append(node("strong","",`本次对话 · 第${trace.turn}轮`),node("p","",`沿用条件：${trace.inherited.map(k=>({sku:"商品",date:"日期",intent:"事项"}[k])).join("、")||"无"}。指标与证据未从前轮继承。`));}
  const memories=task?.parsed.store_memory_snapshot||[];
  renderModelRuntime($("conversation-trace"),task?.parsed.planning_runtime,'准入与规划');
  renderModelRuntime($("conversation-trace"),task?.result?.model_runtime,'后台调查');
  const shadowed=memories.filter(m=>m.shadow_pending).length;
  $("context-used-count").textContent=shadowed?`${memories.length}（${shadowed}条涉待裁决遮蔽）`:String(memories.length);
  $("context-used-list").replaceChildren();
  for(const memory of memories){const row=node("div","context-used-item");row.append(node("strong","",`${memory.label} · v${memory.revision}`),node("p","",memory.content),node("small","",`${memory.source} · 有效至 ${memory.expires_on} · ${memory.id.slice(0,8)}`));if(memory.shadow_pending)row.append(node("small","context-shadow-note",memory.shadow_notice||"同事项另有背景未纳入本次上下文。"));$("context-used-list").append(row);}
  if(!memories.length)$("context-used-list").append(node("p","inspector-note","本轮没有命中已确认且适用的背景。"));
  const undisclosed=(task?.parsed.memory_retrieval?.shadowed||[]).filter(entry=>!entry.injected);
  if(undisclosed.length)$("context-used-list").append(node("p","inspector-note",`另有 ${undisclosed.length} 组同事项背景因待裁决未注入，请在账号设置的经营背景中裁决。`));
  $("save-task-context").hidden=!task||task.status!=="completed";

  $("inspector-empty").hidden = !!task;
  // 来源状态来自真实工具事件，不能把“查询成功”直接写成“数据完整”。
  $("detail-sku").textContent = task?.parsed.product_scope?.label || task?.parsed.sku_id || "待确认";
  $("detail-date").textContent = task?.parsed.target_date || "待确认";
  $("detail-comparison").textContent = task?.parsed.comparison_policy === "target_day_only" ? "不适用（单日查询）" : task?.parsed.comparison_date || "—";
  $("detail-id").textContent = task ? task.id.slice(0, 8) : "—";
  const product = task?.parsed.candidates?.find((item)=>item.sku_id === task?.parsed.sku_id) || meta?.products.find((item)=>item.sku_id === task?.parsed.sku_id);
  $("detail-product-title").textContent = product ? `${product.name}${product.listed_at ? ` · 上架 ${product.listed_at} · ${(product.business_tags || []).join(" / ")}` : ""}` : "尚未确定具体商品。";
  $("source-list").replaceChildren();
  let queried = 0;
  const expected = task?.parsed.product_scope ? ['product_group_sales'] : currentTask?.parsed.intent === "query_inventory" ? ["inventory"] : currentTask?.parsed.intent === "query_promotions" ? ["promotions"] : currentTask?.parsed.intent === "query_sales_performance" ? ["sales"] : ["sales", "traffic", "inventory", "promotions"];
  for (const [domain, title] of Object.entries(domains)) {
    const event = (task?.events || []).findLast((entry) => entry.type === "tool_completed" &&
      (entry.payload.result?.data?.table === domain || entry.payload.tool === {
        sales: "get_sales_metrics", traffic: "get_traffic_metrics", inventory: "get_inventory_status", promotions: "get_promotion_history"
      }[domain]));
    const result = event?.payload.result;
    const supplied = result?.data?.records || [];
    const records = task?.parsed.intent === "query_sales_performance" ? supplied.slice(-1) : supplied;
    let label = "待查询", state = "";
    if (!expected.includes(domain) && !event) continue;
    if (event) {
      queried++;
      if (!result?.ok) { label = "查询失败"; state = "warn"; }
      else if (!records.length) { label = "无记录"; state = "warn"; }
      else if (records.some((record) => record.status !== "ok")) { label = "有信息缺口"; state = "warn"; }
      else { label = "已返回"; state = "ok"; }
    }
    const row = node("div", "source-row");
    row.append(node("span", "source-symbol", { sales: "↗", traffic: "⌁", inventory: "▦", promotions: "◇", traffic_channels: "⑂" }[domain]),
      node("span", "", title + "数据"), node("span", `source-status ${state}`, label));
    $("source-list").append(row);
  }
  $("source-count").textContent = `${queried} 类已查询`;
  $("artifact-list").replaceChildren();
  const artifacts = task?.result ? [["report.md", "调查报告"], ["evidence.json", "来源证据"],
    ["events.jsonl", "执行记录"], ["request.json", "原始任务卡"], ...(task.result.validation ? [["validation.json", "字段验收结果"]] : [])] : [];
  let artifactCount = artifacts.length;
  for (const [filename, title] of artifacts) {
    const button = node("button", "artifact-item"), text = node("div");
    text.append(node("strong", "", title), node("small", "", filename));
    button.append(node("span", "artifact-symbol", "▤"), text, node("span", "", "↓"));
    button.addEventListener("click", () => downloadArtifact(filename));
    $("artifact-list").append(button);
  }
  // 轨迹页复用服务端事件与验收产物；没有运行目录的解析卡不显示入口。
  if (task?.result?.run_id) {
    const trace = node("button", "artifact-item"), text = node("div");
    text.append(node("strong", "", "执行轨迹"), node("small", "", "工具 / 证据 / 拒绝原因"));
    trace.append(node("span", "artifact-symbol", "◎"), text, node("span", "", "→"));
    trace.addEventListener("click", () => { location.href = `/runs/${encodeURIComponent(task.result.run_id)}`; });
    $("artifact-list").append(trace);
    artifactCount++;
  }
  $("artifact-count").textContent = String(artifactCount);
  if (!artifactCount) $("artifact-list").append(node("p", "inspector-note", "调查完成后可下载报告和证据。"));
}

function renderModelRuntime(target,runtime,label='本轮请求'){
  // 供应商上报与本地容量预算分开呈现；缺用量的失败/取消请求不能显示成免费。
  if(!runtime)return;
  const detail=node('details','model-runtime'),summary=node('summary','',`${label} · 模型调用 ${runtime.calls} 次`);
  detail.append(summary,node('p','',`供应商上报：输入 ${runtime.reported.prompt_tokens??'未知'} / 输出 ${runtime.reported.completion_tokens??'未知'} / 总计 ${runtime.reported.total_tokens??'未知'} tokens`));
  if(runtime.unknown_usage_calls)detail.append(node('p','',`${runtime.unknown_usage_calls} 次调用用量未完整上报；已知部分 ${runtime.known_totals.total_tokens} tokens，费用未知。`));
  else detail.append(node('p','','费用未知；尚未接入供应商账单。'));
  detail.append(node('p','',`调用预算 ${runtime.calls}/${runtime.budget.max_calls} 次 · 容量预留 ${runtime.reserved_units}/${runtime.budget.max_estimated_tokens} 单位（本地估算，非计费用量）`));
  const purposes={planning:'查询规划',classification:'意图分类',chat:'普通对话',execution:'调查执行',connection_test:'连接测试'};
  for(const record of runtime.records){
    const context=record.context,status={completed:'完成',failed:'失败',cancelled:'已停止',pending:'进行中'}[record.status]||record.status;
    detail.append(node('p','',`#${record.sequence} ${purposes[record.purpose]||'模型调用'} · ${record.model} · ${status} · ${record.usage.total_tokens??'未知'} tokens · 裁剪 ${context.removed_messages} 条历史消息`));
  }
  target.append(detail);
}

function node(tag, className = "", text = "") {
  // 不拼接 innerHTML：外部来源名称、模型文本和用户问题均不能注入 DOM。
  const result = document.createElement(tag);
  result.className = className;
  result.textContent = text;
  return result;
}

function notify(message = "") {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}

async function api(path, options = {}) {
  // 访问口令只随 Authorization 发送，不写 URL，也不发送到第三方服务。
  const token = sessionStorage.getItem("signal-access-token");
  const epoch = storeEpoch;
  const headers = { ...(options.headers || {}), ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(currentStore && path !== "/api/stores" ? {"X-Store-ID": currentStore} : {}) };
  const response = await fetch(path, { ...options, headers });
  if (epoch !== storeEpoch) throw new Error("已切换店铺，上一店铺响应已忽略。");
  if (response.status === 401) $("access-panel").hidden = false;
  if (!response.ok) {
    const body = await response.json();
    const error = new Error(body.error || `请求失败 (${response.status})`);
    error.status = response.status;
    error.errorInfo = body.error_info;
    throw error;
  }
  return response.json();
}

function switchTab(name) {
  selectedTab = name;
  document.querySelectorAll("[data-tab]").forEach((button) => button.setAttribute("aria-selected", String(button.dataset.tab === name)));
  ["summary", "evidence", "events"].forEach((id) => { $(id).hidden = id !== name || !currentTask; });
}

let pendingConfirmation=null;
function resetTask() {
  // 新问题与补充原问题分开；只有后者带 parent_id 保留关联。
  clearTimeout(pollTimer);
  activeId = null; currentTask = null;
  currentCase = null; $("review-panel").hidden = true; $("validation-summary").hidden = true;
  document.body.classList.remove("has-task");
  $("session-title").textContent = "新调查";

  pendingConfirmation=null;
  setInspector(false);
  inspectorPreference = null;
  renderInspector();
  document.querySelectorAll(".history-item.selected").forEach((item) => item.classList.remove("selected"));
  localStorage.removeItem("signal-task-id");

  $("task-status").textContent = "等待发起";
  $("task-status").className = "badge";
  ["task-context", "clarification", "result-toolbar", "summary", "evidence", "events"].forEach((id) => { $(id).hidden = true; });
  $("empty-state").hidden = false;
  notify();
  syncStopButton();
}

async function loadMeta() {
  meta = await api("/api/meta");
  $("access-panel").hidden = true;
  $("mode-badge").hidden = true;
  $("ad-high").value=meta.operating_settings.high_pct; $("ad-low").value=meta.operating_settings.low_pct;
  $("dataset-note").textContent = meta.business_context?.import_provenance || meta.business_context?.provenance || (meta.sample_data ? "学习用合成数据，非真实店铺记录。" : "用户提供的数据，来源真实性未独立核验。");
  const dates = meta.available_dates;
  const unitHint = meta.business_context?.scenario === "flash_flowers" ? " · 销量按成品束/桶，不按单枝计" : "";
  $("date-coverage").textContent = dates.length ? `数据覆盖 ${dates[0]} — ${dates.at(-1)}${unitHint}` : "暂无销售日期";
  // 不预置“流量下降”等结论；实际商品来自当前门店目录，日期由用户明确选择。
  // 不把数据最后一天默认成“昨天”，缺失日期仍交给后端澄清。

}

async function loadHistory() {
  const { tasks } = await api("/api/tasks");
  $("history").replaceChildren();
  if (!tasks.length) $("history").append(node("p", "subtle", "暂无调查记录"));
  for (const task of tasks) {
    const button = node("button", `history-item${task.id === activeId ? " selected" : ""}`);
    const detail = node("small");
    detail.append(node("span", "", labels[task.status] || task.status), node("span", "", new Date(task.created_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })));
    button.append(node("strong", "", task.question), detail);
    button.addEventListener("click", () => selectTask(task.id));
    $("history").append(button);
  }
}

async function selectTask(id) {
  if (activeModule !== "investigation") await showModule("investigation");
  clearTimeout(pollTimer);
  activeId = id;
  localStorage.setItem("signal-task-id", id);
  try {
    const { task } = await api(`/api/tasks/${id}`);
    if (activeId !== id) return; // 避免慢响应覆盖用户刚选中的另一个任务。
    currentTask = task;
    render(task);
    await loadHistory();
   
    if (activeId !== id) return;
    if (task.review_case) await loadReview(task.review_case.id);
    else {currentCase = null; $("review-panel").hidden = true;}
    if (["queued", "running",'cancelling'].includes(task.status)) pollTimer = setTimeout(() => selectTask(id), 1000);
  } catch (error) {
    // 切换独立工作区后，浏览器可能还记着旧任务 ID；只清理失效选择，不删除历史数据。
    if (error.status === 404 && activeId === id) { resetTask(); notify(); }
    else notify(error.message);
  }
}

function renderRecoveryActions(container,problem,question){
  // 恢复动作来自固定类型；重试只恢复原问题，用户发送后才创建新请求和可能的API消费。
  if(!problem)return;
  const actions=node('div','recovery-actions');
  for(const action of problem.recovery_actions||[]){
    if(!['retry','open_settings','edit_question','view_sources'].includes(action.type))continue;
    const button=node('button','candidate-button',action.label);button.type='button';
    button.addEventListener('click',()=>{
      if(conversationBusy)return;
      if(action.type==='open_settings'){loadModelSettings();return;}
      if(action.type==='view_sources'){
        if(currentTask?.result)switchTab('evidence');
        else {const dates=meta?.available_dates||[];notify(dates.length?`当前数据覆盖 ${dates[0]} 至 ${dates.at(-1)}，请核对所查期间。`:'当前没有可用销售数据。');}
        return;
      }
      pendingConfirmation=null;$('question').value=question||'';$('question').focus();
      notify(action.type==='retry'?'已恢复原问题，按回车或点击发送重新请求。':'可以在输入框中修改问题后发送。');
    });actions.append(button);
  }
  container.append(actions);
}

function render(task) {
  syncStopButton();
  if(conversation){const turn=conversation.turns.find(t=>t.task_id===task.id);if(turn)turn.status=task.status;renderConversation();}
  document.body.classList.add("has-task");
  $("session-title").textContent = task.parsed.product_scope?.label || task.parsed.sku_id || "调查会话";
  renderInspector(task);
  if (inspectorPreference === null && window.matchMedia("(min-width: 1121px)").matches) setInspector(true);
  $("empty-state").hidden = true;
  $("result-toolbar").hidden = false;
  $("task-status").textContent = labels[task.status] || task.status;
  $("task-status").className = `badge${["failed", "interrupted"].includes(task.status) ? " error" : ["needs_clarification", "needs_data", "unsupported"].includes(task.status) ? " warn" : ""}`;
  $("task-context").hidden = false;
  const comparisonText = task.parsed.is_window ? `窗口 ${task.parsed.trend_start}—${task.parsed.trend_end}` : task.result?.sales_trend ? `主基线 ${task.parsed.comparison_date}` : task.parsed.comparison_policy === "target_day_only" ? "单日表现" : `对比 ${task.parsed.comparison_date || "—"}`;
  const conversationTurn=conversation?.turns.find(t=>t.task_id===task.id);
  const resultProduct=meta?.products.find(p=>p.sku_id===(conversationTurn?conversationTurn.scope.sku:task.parsed.sku_id));
  $("task-context").replaceChildren(node("strong", "", task.question), node("small", "", `${task.parsed.store_name||"当前门店"} · ${task.parsed.product_scope?.label||(resultProduct?resultProduct.short_name+" · "+resultProduct.variant:"规格待确认")} · ${task.parsed.target_date || "日期待确认"} / ${comparisonText} · 任务 ${task.id.slice(0, 8)}`));
  const validation = task.result?.validation;
  $("validation-summary").hidden = !validation;
  if (validation) $("validation-summary").textContent = `有限字段验收：${validation.status === "passed" ? "字段一致" : validation.status === "references_only" ? "仅有引用，尚无字段断言" : "未通过"} · ${validation.checked} 项。${validation.scope}`;
  $("download-report").hidden = !task.result;
  const analysis=task.result?.analysis;
  const problem=task.error_info||task.parsed.error_info||task.result?.error_info;
  if(problem&&task.status!=='needs_clarification')$('task-status').textContent=problem.title;
  const blocked = ["needs_clarification", "needs_data", "unsupported"].includes(task.status)||!!problem;
  $("clarification").hidden = !blocked;
  $("clarification").replaceChildren();
  if (blocked) {
    // 解析器也服务 CLI；网页将命令参数名翻成实际表单控件，避免要求用户理解终端参数。

    if(problem&&task.status!=='needs_clarification')$('clarification').append(node('strong','',problem.title));
    const issues=problem&&task.status!=='needs_clarification'?[problem.message]:task.parsed.issues;
    for (const issue of issues) $("clarification").append(node("p", "", issue.replaceAll("--sku", "回复中的商品确认卡片").replaceAll("--date", "明确日期或起止时间")));
    renderRecoveryActions($('clarification'),problem,task.question);
    if(task.status==='needs_clarification'&&!task.parsed.target_date){
      const card=node('form','followup-card'),label=node('label','','你想查看哪段时间？'),input=node('input');
      input.type='text';input.required=true;input.maxLength=100;input.placeholder='例如：昨天、近7天、10月1日至15日';input.setAttribute('aria-label','补充查询日期或时间段');
      const send=node('button','candidate-button','补充并继续');send.type='submit';label.append(input);card.append(label,send);
      card.addEventListener('submit',event=>{event.preventDefault();if(conversationBusy||!input.value.trim())return;
        $('question').value=task.question.replace(/最近(?!\s*[零一二两三四五六七八九十\d])/g,'')+' '+input.value.trim();
        pendingConfirmation=null;$('investigation-form').requestSubmit();});$('clarification').append(card);
    }
    if(task.status==='needs_clarification'&&task.parsed.group_candidate&&!task.parsed.product_scope){
      const group=task.parsed.group_candidate,button=node('button','candidate-button',`确认${group.label}（${group.sku_ids.length}款）并查询`);
      button.addEventListener('click',()=>{
        pendingConfirmation={question:task.question,sku:'__all_matched__',intent:'query_sales_performance'};$('question').value=task.question;
        $('investigation-form').requestSubmit();
      });$('clarification').append(button);
    }
    for (const candidate of task.status!=='needs_clarification'||task.parsed.group_candidate||task.parsed.sku_id&&!task.parsed.product_resolution?.suggested_correction?[]:task.parsed.candidates) {
      const button = node("button", "candidate-button", `选择 ${candidate.short_name || candidate.name} · ${candidate.variant} ↗`);
      button.addEventListener("click", () => {
        $("question").value = task.question; pendingConfirmation={question:task.question,sku:candidate.sku_id};
        $("investigation-form").requestSubmit();
        $("question").scrollIntoView({ behavior: "smooth", block: "center" });

        notify();
      });
      $("clarification").append(button);
    }
  }
  renderSummary(task);
  renderOrderFunnel($("summary"),task);
  if(task.parsed?.date_resolution?.length)$("summary").prepend(node('p','evidence-extra',task.parsed.date_resolution.join(' ')));
  if(task.result?.sales_trend)renderSalesTrend($("summary"),task);
  for(const trend of task.result?.selected_sales_trends||[])renderSalesTrend($("summary"),{...task,result:{...task.result,sales_trend:trend}});
  renderEvidence(task);
  $("events").replaceChildren();
  for (const entry of task.events || []) {
    const payload = entry.payload;
    const text = entry.type === "tool_completed" ? `${toolNames[payload.tool] || payload.tool} · ${payload.result.ok ? "已返回观察结果" : "未通过校验"}` :
      ({ task_created: "任务已保存", task_running: "后台开始调查", run_started: "调查运行已创建", run_settled: "调查运行已结束", task_settled: "任务状态已保存", workflow_selected: `工作流：${intentLabels[payload.intent]} · ${payload.reason}`, follow_up_decision: `追加调查决策：${payload.reason}`, follow_up_settled: `追加调查结果：${payload.reason}`, tool_started: `${toolNames[payload.tool] || payload.tool} · 开始` }[entry.type] || entry.type);
    const row = node("div", "event-row"), content = node("div");
    content.append(node("span", "", text), node("small", "", `${new Date(entry.created_at).toLocaleTimeString("zh-CN")} · ${payload.call_id || "任务事件"}`));
    row.append(node("span", "event-dot"), content); $("events").append(row);
  }
  switchTab(selectedTab);
}

function renderOrderFunnel(container,task) {
  // 所有比例与变化由服务端计算；浏览器只展示冻结批次，不把缺失显示成0。
  const data=task.result?.order_funnel;if(!data)return;
  const section=node('section','sales-trend-section');section.id='order-funnel-section';
  section.append(node('p','trend-eyebrow','ORDERS / FULFILLMENT'),node('h2','','支付后，订单流向哪里？'),
    node('p','trend-coverage','按支付日归组 · 订单数（非花束件数） · 取消、完成、待完结为互斥状态'));
  const cards=node('div','baseline-cards');
  for(const row of data.metrics){
    const card=node('article','baseline-card');card.append(node('h3','',`${row.date} 支付批次`));
    if(row.status!=='ok')card.append(node('p','',row.status==='conflicting'?'订单批次与销售表冲突，暂不比较':'该日批次数据缺失'));
    else {
      card.append(node('strong','',row.cancellation_pct===null?'—':`${row.cancellation_pct}%`),node('p','','取消率'),
        node('p','',`支付 ${row.cohort_paid_orders} · 取消 ${row.cancelled_orders} · 完成 ${row.completed_orders} · 待完结 ${row.pending_orders}`),
        node('p','',`其中跨日取消 ${row.late_cancelled_orders} 单，仍归本支付批次`),
        node('small','',`统计截至 ${row.observed_at} · 支付日距截止日期 ${row.age_days} 天`));
      const reasons=Object.entries(row.cancellation_reasons).filter(([,value])=>value>0);
      card.append(node('p','',reasons.length?`记录原因：${reasons.map(([key,value])=>`${key} ${value}单`).join('；')}`:'暂无取消订单'));
    }
    cards.append(card);
  }
  section.append(cards);
  if(data.changes){const delta=data.changes.cancellation_pp;
    section.append(node('p','quality-note',`取消率较基线：${delta===null?'支付基数为零，无法比较':`${delta>0?'+':''}${delta} 个百分点`}；完成订单：${data.changes.completed_orders.before} → ${data.changes.completed_orders.after} 单。`));}
  for(const note of data.notes)section.append(node('p','limits',note));
  section.append(node('small','evidence-extra','证据 orders-source · 原始批次与统计截止时间保存在本轮来源证据中。'));
  container.append(section);
}

function renderSummary(task) {
  const target = $("summary"); target.replaceChildren();
  const analysis = task.result?.analysis;
  if(task.status==='cancelled'&&analysis)target.append(node('p','quality-note','已停止：以下仅为停止前取得的部分数据，不代表查询完成。'));
  if(analysis?.assessment){const a=analysis.assessment,section=node('section',`quality-note assessment-${a.code}`);
    section.append(node('h2','',a.label),node('p','',a.scope||'销量关注分级'));for(const reason of a.reasons)section.append(node('p','',reason));
    section.append(node('small','',`${a.tier} · 可比同星期 ${a.history_used.length} 天 · 策略 ${a.policy_version}`));
    if(a.reference)section.append(node('p','',`历史中位销量 ${a.reference.median_units} · 关注参考线 ${a.reference.lower_attention_boundary} · 当前 ${a.primary.target_units}`));
    const details=node('details'),summary=node('summary','','查看判断依据');details.append(summary,node('p','',a.caveat),node('p','',`绝对损失关注线 ${a.thresholds.min_loss} 件；降幅关注线 ${a.thresholds.min_drop_pct}%；历史波动参考使用MAD。`));
    for(const row of a.history_used)details.append(node('p','',`${row.date}：${row.units_sold} 件，纳入比较`));
    for(const row of a.history_excluded)details.append(node('p','',`${row.date}：${row.reason}，未纳入`));section.append(details);target.append(section);}
  if (!analysis) {
    if(task.error_info&&task.status!=='needs_clarification'){
      target.append(node('p','limits','本次尚未生成分析报告。'));return;
    }
    target.append(node("p", ["queued", "running",'cancelling'].includes(task.status) ? "loading" : "limits", task.error ||
      (["queued", "running"].includes(task.status) ? "正在查询并核对四类数据…" : "补充调查对象或数据后，再生成分析结果。")));
    return;
  }
  if(analysis.guidance)renderOperatingGuidance(target,analysis.guidance);
  renderDecision(target,task);
  if (analysis.kind === "lookup") {
    const cards = node("div", "metrics");
    for (const item of analysis.display_metrics) {
      const card = node("div", "metric");
      card.append(node("span", "metric-label", item.label), node("div", "metric-number lookup-value", `${item.value}${item.unit || ""}`));
      cards.append(card);
    }
    target.append(node("h2", "", analysis.title), node("p", "", analysis.overview), cards);
    if(analysis.group_items){const makeTable=items=>{const wrap=node('div','table-wrap'),table=node('table'),head=node('thead'),header=node('tr');
      const grouped=items.some(item=>item.member_skus);
      for(const text of [grouped?'类别':'商品',grouped?'包含商品':'规格','销量（束/桶）','商品金额（元）',grouped?'商品日覆盖':'可用天数'])header.append(node('th','',text));head.append(header);const body=node('tbody');
      for(const item of items){const row=node('tr');for(const text of [item.name,item.variant,item.units_sold??'—',item.paid_revenue??'—',`${item.available_days}/${item.requested_window_days??item.requested_days}`])row.append(node('td','',text));body.append(row);}table.append(head,body);wrap.append(table);return wrap;};
      if(analysis.ranking){const rank=analysis.ranking,dimension={variety:'花材品种',product_type:'商品类型',series:'系列'}[rank.group_by]||'商品';
        if(rank.coverage_note)target.append(node('p','quality-note warn',rank.coverage_note));
        for(const part of rank.partitions||[rank]){target.append(node('h3','',`销量${part.direction==='asc'?'最低':'最高'}的${dimension}`));
          if(part.rows.length)target.append(makeTable(part.rows));else target.append(node('p','limits','暂无可比记录，需要补充所选期间的销售数据。'));
          if(part.leaders.length>part.rows.length)target.append(node('p','limits',`该端有${part.leaders.length}项并列，表格先展示${part.rows.length}项；全部商品明细可在下方展开。`));}
        const detail=node('details');detail.append(node('summary','','查看全部商品与数据覆盖'),makeTable(analysis.group_items));target.append(detail);
      }else target.append(makeTable(analysis.group_items));}
    if(analysis.selected_comparison){const comparison=analysis.selected_comparison;
      target.append(node('h3','','入选商品 · 同款历史对比'),node('p','',comparison.scope_note));
      const wrap=node('div','table-wrap'),table=node('table'),head=node('thead'),header=node('tr'),body=node('tbody');
      for(const text of ['商品','基线日期','基线销量','目标销量','增减（束/桶）','变化'])header.append(node('th','',text));head.append(header);
      for(const item of comparison.rows){const row=node('tr');for(const text of [item.name+' · '+item.variant,item.date,item.baseline_units??'—',item.target_units??'—',item.absolute_change??'—',item.relative_pct===null?(item.zero_baseline?'基线为0':'数据不足'):`${item.relative_pct>0?'+':''}${item.relative_pct}%`])row.append(node('td','',text));body.append(row);}
      table.append(head,body);wrap.append(table);target.append(wrap,node('p','limits',comparison.chart_note));
      if(!comparison.rows.length)target.append(node('p','','没有具备完整数据的入选商品，暂不能比较。'));
    }
    for (const note of analysis.limitations) target.append(node("p", "limits", note));
    target.append(node("p", "evidence-extra", `证据：${analysis.evidence_ids.join(" / ")}。本次只查询所选数据域。`));
    return;
  }
  if(task.result.model_draft?.summary){
    const draft=node("article","model-answer");draft.append(node("h2","","花迹 · DeepSeek 分析"),node("p","",task.result.model_draft.summary));
    draft.append(node("small","",`模型调用 ${task.result.model_calls} 次 · 工具调用 ${task.result.tool_calls} 次 · 总用量 ${task.result.model_usage?.total_tokens??"未知"} tokens · 下方指标由程序计算`));target.append(draft);
  }
  const metrics = node("div", "metrics");
  for (const [key, title, unit] of [["units_sold", "支付件数", ""], ["visitors", "商品访客 UV", ""], ["payment_conversion_pct", "支付买家转化率", "%"]]) {
    const fact = analysis.facts[key], card = node("div", "metric");
    const label = key === "units_sold" && task.result.business_context?.scenario === "flash_flowers" ? "成品销量（束/桶）" : title;
    card.append(node("span", "metric-label", label), node("div", "metric-number", fact ? `${fact.after.toLocaleString("zh-CN")}${unit}` : "—"));
    const note = node("small", "", fact ? `基线 ${task.parsed.comparison_date}：${fact.before}${unit}` : "数据不足，暂不计算");
    if (fact) note.append(node("span", "metric-change", key === "payment_conversion_pct" ? `${fact.percentage_point_change > 0 ? "+" : ""}${fact.percentage_point_change} pp` : fact.relative_pct === null ? "基线为零" : `${fact.relative_pct > 0 ? "+" : ""}${fact.relative_pct}%`));
    card.append(note); metrics.append(card);
  }
  target.append(metrics);
  const quality = { conflicting: "发现来源冲突，需先核对数据口径。", partial: "部分来源缺失或延迟，当前结论有信息缺口。", complete_for_supplied_daily_fields: "四类日级数据已核对。下列为候选信号，尚未建立因果关系。" };
  target.append(node("div", `quality-note${analysis.data_quality === "complete_for_supplied_daily_fields" ? "" : " warn"}`, quality[analysis.data_quality]));
  for (const hypothesis of analysis.guidance?[]:analysis.hypotheses) {
    const finding = node("article", "finding"), heading = node("div", "finding-label", signals[hypothesis.id] || hypothesis.id);
    heading.append(node("span", "signal-tag", "待进一步核验"));
    finding.append(heading, node("p", "", hypothesis.text), node("small", "", `证据 ${hypothesis.evidence_ids.join(" / ")}`), node("div", "next-check", `下一步 → ${hypothesis.next_check}`));
    target.append(finding);
  }
  if (!analysis.guidance&&!analysis.hypotheses.length) target.append(node("p", "", "当前没有足够的候选信号，不能据此排除其他原因。"));
  for (const gap of analysis.gaps) target.append(node("p", "limits", `${domains[gap.table]}：${gap.reason.includes("Partial-day") ? "库存监测不足全天，不能推断整日情况。" : "对比数据缺失、延迟或不可用，不当作零值。"}`));
  for (const conflict of analysis.conflicts) target.append(node("p", "limits", conflict.kind === "full_day_stockout_with_sales" ? "库存记录显示整日缺货，但销售记录存在成交。需核对预售、补单和归因时点。" : "同口径支付买家数超过访客，需核对归因范围。"));
  target.append(node("p", "limits", "主基线与多日背景见下方对比。日历/活动差异仅为解释线索，尚未控制所有因素；当前工作流只读。"));
  const follow = task.result.follow_up;
  if (follow) {
    const section = node("article", "finding");
    section.append(node("div", "finding-label", "追加调查 · 渠道拆解"), node("p", "", follow.reason));
    for (const row of follow.rows) section.append(node("p", "", `${row.channel}：${row.before} → ${row.after} UV；变化 ${row.absolute > 0 ? "+" : ""}${row.absolute}`));
    section.append(node("small", "", `证据：${follow.evidence_ids.join(" / ")}`));
    for (const note of follow.limitations) section.append(node("p", "limits", note));
    target.append(section);
  }
}

function renderOperatingGuidance(target,guidance){
  // 所有文案使用textContent，平台记录或模型文本不能注入可执行HTML。
  const section=node('section','operating-guidance');
  section.append(node('h2','','经营判断与排查顺序'));
  for(const [key,title] of [['observed','发生了什么'],['supported_signals','证据支持什么'],['uncertainties','还不能确定什么']]){
    const group=node('article','guidance-group');group.append(node('h3','',title));
    for(const item of guidance[key]||[]){group.append(node('p','',item.text));if(item.evidence_ids?.length)group.append(node('small','evidence-extra',`证据：${item.evidence_ids.join(' / ')}`));}
    if(!(guidance[key]||[]).length)group.append(node('p','limits','暂无充分的已观察证据。'));section.append(group);
  }
  const checks=node('article','guidance-group');checks.append(node('h3','','建议先查什么'));
  for(const item of guidance.next_checks||[]){const card=node('div','finding');
    card.append(node('strong','',`${item.priority}. ${item.title}`),node('p','',item.reason),node('p','limits',`所需数据：${item.required_data}`));
    if(item.availability==='needs_additional_data')card.append(node('small','evidence-extra','需要补充对应数据后核查'));checks.append(card);}
  if(!(guidance.next_checks||[]).length)checks.append(node('p','limits','当前查询仅描述经营表现，未据此建议经营调整。'));
  section.append(checks);target.append(section);
}

function renderDecision(target,task){
  // 决策卡只读：审批与干跑都是本地状态机，页面不执行任何店铺写操作。
  if(task.status!=='completed')return;
  const section=node('section','operating-guidance decision-card');
  section.append(node('h2','','决策建议 · 只读干跑'));
  const state=task.decision;
  const send=(path,payload={})=>api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  if(!state){
    section.append(node('p','limits','把本轮结论转成可审批的工作单；只生成核对清单和判定规则，不会写入店铺系统。'));
    const button=node('button','primary','生成决策卡');
    button.onclick=async()=>{button.disabled=true;try{await send(`/api/tasks/${task.id}/decision`);await selectTask(task.id);}catch(error){button.disabled=false;notify(error.message);}};
    section.append(button);target.append(section);return;
  }
  const card=state.card;
  section.append(node('p','',card.summary),node('p','limits',card.verdict.label));
  for(const item of card.items){
    const block=node('article','finding');
    block.append(node('strong','',`${item.priority}. ${item.title}`));
    if(item.rationale)block.append(node('p','',item.rationale));
    block.append(node('p','limits',`判定规则：${item.verification.pass_rule}`));
    if(item.deadline)block.append(node('small','',`建议完成：${item.deadline.replace('T',' ').slice(0,16)}`));
    if(item.availability==='needs_additional_data')block.append(node('small','evidence-extra','需要先补齐对应数据'));
    section.append(block);
  }
  const actions=node('div','decision-actions');
  const statusLabels={proposed:'待审批',approved:'已批准，待干跑',rejected:'已驳回',executed:'已干跑'};
  actions.append(node('span','signal-tag',statusLabels[state.status]||state.status));
  const run=async(method,payload)=>{try{await send(`/api/decisions/${state.id}/${method}`,payload);await selectTask(task.id);}catch(error){notify(error.message);}};
  if(state.status==='proposed'){
    const approve=node('button','primary','批准');approve.onclick=()=>run('approve',{actor:'本地操作员'});
    const reject=node('button','','驳回');reject.onclick=()=>run('reject',{note:'本轮不采纳'});
    actions.append(approve,reject);
  }else if(state.status==='approved'){
    const execute=node('button','primary','干跑执行');execute.onclick=()=>run('execute');
    actions.append(execute);
  }
  section.append(actions);
  if(state.receipt){
    const details=node('details');details.append(node('summary','','查看干跑回执'));
    const receipt=state.receipt;
    details.append(node('p','',`回执号 ${receipt.receipt_id} · 摘要 ${receipt.digest.slice(0,12)}… · 副作用 ${receipt.side_effects.length} 处`));
    for(const item of receipt.items){
      const part=node('div','finding');part.append(node('strong','',item.title));
      for(const step of item.steps)part.append(node('p','',`· ${step}`));
      for(const fact of item.facts)part.append(node('small','evidence-extra',fact.text));
      details.append(part);
    }
    details.append(node('p','limits',receipt.boundary));
    section.append(details);
  }
  target.append(section);
}

function renderEvidence(task) {
  const target = $("evidence"); target.replaceChildren();
  const wrap = node("div", "table-wrap"), table = node("table"), head = node("thead"), heading = node("tr");
  ["证据 ID", "数据域", "业务日期", "状态", "来源"].forEach((title) => heading.append(node("th", "", title)));
  head.append(heading); table.append(head);
  const body = node("tbody");
  for (const entry of task.events || []) {
    const result = entry.payload.result;
    if (entry.type !== "tool_completed" || !result?.ok || !result.data?.records) continue;
    for (const record of result.data.records) {
      const row = node("tr");
      [result.evidence_id, domains[result.data.table], record.date, { ok: "可用", unavailable: "缺失", delayed: "延迟",not_listed:"未上架",schedule_unknown:"年度安排未知" }[record.status]||record.status, (record.source_id || "未提供") + (record.verification_status === "human_supplied_unverified" ? "（人工提交，未独立核验）" : "")].forEach((text) => row.append(node("td", "", text)));
      body.append(row);
    }
  }
  table.append(body); wrap.append(table); target.append(wrap, node("p", "evidence-extra", "来源更新时间与完整原始观察保存在 evidence.json；报告中的引用可回查此处。"));
  if (task.result) {
    const download = node("button", "text-button", "下载完整证据 JSON ↓");
    download.addEventListener("click", () => downloadArtifact("evidence.json")); target.append(download);
  }
}

async function downloadArtifact(name) {
  try {
    const token = sessionStorage.getItem("signal-access-token");
    const response = await fetch(`/api/tasks/${activeId}/artifacts/${name}`, { headers: {...(token ? { Authorization: `Bearer ${token}` } : {}), ...(currentStore ? {"X-Store-ID": currentStore} : {})} });
    if (!response.ok) throw new Error("产物暂时不可下载。");
    const url = URL.createObjectURL(await response.blob()), link = node("a");
    link.href = url; link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) { notify(error.message); }
}

function bindComposerKeyboard(input,form,send,isBusy){
  // 输入法确认候选的回车不能触发发送；长按回车和请求进行中也不能重复提交。
  let composing=false,lastCompositionEnd=-Infinity;
  input.addEventListener('compositionstart',()=>{composing=true;});
  input.addEventListener('compositionend',()=>{composing=false;lastCompositionEnd=Date.now();});
  input.addEventListener('keydown',event=>{
    if(event.key!=='Enter'||event.shiftKey||event.ctrlKey||event.metaKey||event.altKey)return;
    if(composing||event.isComposing||event.keyCode===229||Date.now()-lastCompositionEnd<50)return;
    event.preventDefault();
    if(event.repeat||isBusy()||send.disabled||!input.value.trim())return;
    form.requestSubmit();
  });
}
bindComposerKeyboard($('question'),$('investigation-form'),$('submit'),()=>conversationBusy);

function syncStopButton(){
  const button=$('stop-request');if(!button)return;
  button.hidden=!meta?.capabilities.cancellation||!(conversationBusy||['queued','running','cancelling'].includes(currentTask?.status));
  button.disabled=stopInProgress||stopRequested&&conversationBusy||currentTask?.status==='cancelling';
  button.title=button.disabled?'正在停止并保存已有结果':'停止本次请求';
}
async function stopCurrentRequest(){
  if(stopInProgress)return;
  stopRequested=true;stopInProgress=true;syncStopButton();
  try{
    if(conversationBusy){
      if(pendingRequest)await api(`/api/conversations/${pendingRequest.cid}/cancel`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:pendingRequest.id})});
      notify('正在停止本次请求…');
    }else if(currentTask&&['queued','running'].includes(currentTask.status)){
      const id=currentTask.id;await api(`/api/tasks/${id}/cancel`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
      if(currentTask?.id===id)await selectTask(id);
    }
  }catch(error){stopRequested=false;notify(error.message);}
  finally{stopInProgress=false;syncStopButton();}
}
$('stop-request').addEventListener('click',stopCurrentRequest);

async function conversationRequest(url,payload){
  if(stopRequested)return null;
  pendingRequest={cid:conversation.id,id:crypto.randomUUID()};syncStopButton();
  const token=sessionStorage.getItem('signal-access-token');
  const response=await fetch(url+'?stream=1',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':pendingRequest.id,
    ...(token?{Authorization:`Bearer ${token}`} : {}),'X-Store-ID':currentStore},body:JSON.stringify(payload)});
  if(!response.ok)throw new Error((await response.json()).error||'请求失败');
  const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='',text='',final=null,preview=null;
  const consume=frame=>{const data=frame.split('\n').filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trim()).join('\n');if(!data)return;
    const event=JSON.parse(data);
    if(event.type==='error'){const error=new Error(event.message);error.errorInfo=event.error_info;throw error;}
    if(event.type==='done')final=event.result;
    if(event.type==='delta'){
      if(!preview){resetTask();$('empty-state').hidden=true;$('summary').hidden=false;document.body.classList.add('has-task');$('session-title').textContent='与花迹对话';$('task-status').textContent='正在回复';preview=node('article','chat-answer');$('summary').replaceChildren(preview);preview.style.whiteSpace='pre-wrap';}
      text+=event.text;preview.textContent=text;
    }};
  try{while(true){const part=await reader.read();buffer+=decoder.decode(part.value||new Uint8Array(),{stream:!part.done});
    let end;while((end=buffer.indexOf('\n\n'))>=0){consume(buffer.slice(0,end));buffer=buffer.slice(end+2);}if(part.done)break;}
    if(!final)throw new Error('连接中断，当前回复可能不完整；请刷新会话状态后重试。');return final;
  }finally{reader.releaseLock();}
}

$("investigation-form").addEventListener("submit", async (event) => {
  event.preventDefault(); notify();
  const question = $("question").value.trim();
  if (!question) return;
  const payload = { question };
  if(pendingConfirmation?.question===question){for(const key of ['sku','intent','store_choice'])if(pendingConfirmation[key])payload[key]=pendingConfirmation[key];}
  if(conversationBusy)return;
  stopRequested=false;pendingRequest=null;
  conversationBusy=true;$("store-select").disabled=true;
  $("submit").disabled = true;
  syncStopButton();
  try {
    // 静态文件可即时更新，旧Python进程需重启；能力探测防止旧服务被新界面打断。
    if(!meta?.capabilities.conversation_state){
      if(currentTask&&["needs_clarification","needs_data","interrupted","failed","incomplete"].includes(currentTask.status))payload.parent_id=currentTask.id;
      const result=await api('/api/tasks',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()},body:JSON.stringify(payload)});
      // 仅在后端接受请求后清空已发送文字；网络失败或用户已输入新草稿时保留输入。
      if($("question").value.trim()===question)$("question").value='';
      selectedTab='summary';await selectTask(result.task.id);return;
    }
    if(!conversation)await startConversation();
    if(stopRequested){notify('发送前已停止，没有提交新请求。');return;}
    const result=await conversationRequest(`/api/conversations/${conversation.id}/turns`, {...payload,revision:conversation.revision,store_id:currentStore,mode:'auto'});
    if(!result)return;
    if($("question").value.trim()===question)$("question").value='';
    await adoptConversation(result.conversation);
    pendingConfirmation=null;
    if(result.task){selectedTab = "summary"; await selectTask(result.task.id);}
    else {resetTask();renderConversation();
      const last=result.conversation.turns?.at(-1);
      if(last?.kind==='chat'||last?.kind==='clarification'||last?.kind==='cancelled')renderChatReply(last);
      else notify(result.conversation.comparison?'':result.conversation.notice);
    }
  } catch (error) { notify(error.message); }
  finally { $("submit").disabled = false;conversationBusy=false;pendingRequest=null;stopRequested=false;$("store-select").disabled=false;syncStopButton(); }
});
$("new-task").addEventListener("click", async () => {
  // 在概览或推广页点击新建时，也要真正进入调查输入区。
  try{await startConversation();await showModule("investigation"); resetTask();renderConversation(); $("question").value = ""; $("question").focus();}catch(error){notify(error.message);}
});
$("refresh-history").addEventListener("click", () => loadHistory().catch((error) => notify(error.message)));
$("download-report").addEventListener("click", () => downloadArtifact("report.md"));
document.querySelectorAll("[data-tab]").forEach((button) => button.addEventListener("click", () => switchTab(button.dataset.tab)));
$("connect").addEventListener("click", () => { sessionStorage.setItem("signal-access-token", $("access-token").value); $("access-token").value = ""; initialize(); });
$("access-token").addEventListener("keydown", (event) => { if (event.key === "Enter") $("connect").click(); });
$("toggle-inspector").addEventListener("click", () => {renderInspector(currentTask);setInspector($("inspector").hidden, true);});
$("close-inspector").addEventListener("click", () => setInspector(false, true));
document.addEventListener("keydown", (event) => {if(event.key === "Escape") setInspector(false, true);});
$("toggle-sidebar").addEventListener("click", () => {
  if (window.matchMedia("(max-width: 640px)").matches) {
    document.body.classList.remove("sidebar-hidden");
    $("sidebar").classList.toggle("mobile-open");
    $("toggle-sidebar").setAttribute("aria-expanded", String($("sidebar").classList.contains("mobile-open")));
  } else {
    document.body.classList.toggle("sidebar-hidden");
    $("toggle-sidebar").setAttribute("aria-expanded", String(!document.body.classList.contains("sidebar-hidden")));
  }
});
document.addEventListener("click", (event) => {
  if (!$("sidebar").contains(event.target) && !$("toggle-sidebar").contains(event.target)) {
    $("sidebar").classList.remove("mobile-open");
    if (window.matchMedia("(max-width: 640px)").matches) $("toggle-sidebar").setAttribute("aria-expanded", "false");
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    setInspector(false, true);
    $("sidebar").classList.remove("mobile-open");
    if (window.matchMedia("(max-width: 640px)").matches) $("toggle-sidebar").setAttribute("aria-expanded", "false");
  }
});
// 首次渲染与 API 初始化分开，让口令尚未输入时也能展开一个真实空状态详情面板。
renderInspector();
$("correction-table").addEventListener("change",renderCorrectionFields);
$("material-form").addEventListener("submit",async(event)=>{
  event.preventDefault(); if (!currentCase) return; $("save-material").disabled=true;
  const caseId=currentCase.id;
  try {
    const table=$("correction-table").value, values={};
    for (const input of document.querySelectorAll("[data-correction]")) {
      values[input.dataset.correction]=input.dataset.valueType === "bool" ? input.value === "true" : input.dataset.valueType === "text" ? input.value : Number(input.value);
    }
    await api(`/api/reviews/${caseId}/materials`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({
      expected_revision:currentCase.revision,note:$("material-note").value,source:$("material-source").value,
      corrections:table ? [{table,date:$("correction-date").value,values}] : []})});
    if (currentCase?.id !== caseId) return;
    $("material-form").reset(); renderCorrectionFields(); await loadReview(caseId); notify("材料已保存；请点击重新调查，旧数据和旧报告不会被覆盖。");
  } catch(error){notify(error.message);} finally{$("save-material").disabled=false;}
});
$("recheck").addEventListener("click",async()=>{
  if (!currentCase) return; const caseId=currentCase.id; $("recheck").disabled=true;
  try {const result=await api(`/api/reviews/${caseId}/recheck`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({expected_revision:currentCase.revision})});
    if(currentCase?.id === caseId) await selectTask(result.task.id);
  }catch(error){notify(error.message);}finally{$("recheck").disabled=false;}
});
$("accept-form").addEventListener("submit",async(event)=>{
  event.preventDefault(); if(!currentCase) return; $("accept-case").disabled=true; const taskId=currentCase.latest_task_id;
  try {await api(`/api/reviews/${currentCase.id}/accept`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({expected_revision:currentCase.revision,task_id:taskId,note:$("accept-note").value})});
    await selectTask(taskId); notify("工单已人工验收关闭，历史材料和调查记录继续保留。");
  }catch(error){notify(error.message);}finally{$("accept-case").disabled=false;}
});
if (window.matchMedia("(max-width: 640px)").matches) $("toggle-sidebar").setAttribute("aria-expanded", "false");

async function initialize() {
  // 初始化先验证 API 可用，再恢复历史；浏览器存的 task ID 只是指针，数据库才是事实源。
  try { await loadStores(); await loadMeta(); await loadHistory(); await restoreConversation();notify(); await showModule("overview"); }
  catch (error) { notify(error.message); }
}
initialize();

async function loadStores() {
  const directory = await api("/api/stores");
  storeDirectory = directory.stores;
  if (!storeDirectory.length) {$("store-select").disabled = true; return;}
  if (!storeDirectory.some((shop)=>shop.id === currentStore&&shop.status!=='inactive')) currentStore = directory.default_store;
  localStorage.setItem("flower-store-id", currentStore);
  $("store-select").replaceChildren();
  for (const shop of storeDirectory) {
    const option = node("option", "", shop.name); option.value = shop.id;option.disabled=shop.status==='inactive'; $("store-select").append(option);
  }
  $("store-select").value = currentStore;
}

async function changeStore(fromConversation=false) {
  const previous = currentStore;
  currentStore = $("store-select").value;
  if (previous === currentStore) return;
  ++storeEpoch; ++adEpoch; clearTimeout(pollTimer); resetTask(); adReport = null;
  $("ad-shop-label").textContent = `${storeDirectory.find((shop)=>shop.id === currentStore)?.name || "当前门店"} · 正在读取推广账本`;
  $("ad-metrics").replaceChildren(); $("ad-chart").replaceChildren(); $("ad-table").replaceChildren(); $("ad-attention").replaceChildren(); $("ad-export").disabled = true;
  localStorage.setItem("flower-store-id", currentStore);
  try {await loadMeta(); await loadHistory();
    if(fromConversation!==true&&conversation){const result=await api(`/api/conversations/${conversation.id}/focus`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({store_id:currentStore,revision:conversation.revision})});conversation=result.conversation;renderConversation();}
    notify(); await showModule(activeModule);
    if(fromConversation!==true&&activeModule==="investigation"&&conversation?.stores[currentStore]?.task_id)await selectTask(conversation.stores[currentStore].task_id);
  }
  catch(error){notify(error.message); $("ad-notice").textContent = error.message;}
}

async function showModule(module) {
  activeModule=module;
  if(module==='imports')setInspector(false);
  if(module!=="investigation")clearTimeout(pollTimer);
  const views={overview:"overview-view",investigation:"investigation-workbench",advertising:"advertising-view",imports:"imports-view"};
  for(const [key,id] of Object.entries(views)){$(id).hidden=module!==key;$("nav-"+key).classList.toggle("selected",module===key);}
  $("session-title").textContent={overview:"门店经营概览",investigation:conversation?.comparison?"跨店比较":currentTask?.parsed.sku_id||"新调查",advertising:"推广费用分析",imports:"经营数据导入"}[module];
  if(module==='imports')await loadImports();
  if(module==="overview")await loadOverview();
  if(module==="advertising")await loadAdvertising();
}

const money = (cents) => `¥${(cents / 100).toLocaleString("zh-CN",{minimumFractionDigits:2,maximumFractionDigits:2})}`;
const adFlags = {high:"高于费用线",low:"低投入待核对",no_revenue:"有扣费无成交",normal:"正常观察"};

async function loadAdvertising() {
  const requestEpoch = ++adEpoch;
  if (activeModule === "advertising") $("session-title").textContent = "推广费用分析";
  $("ad-notice").textContent = "正在读取当前店铺小时账本…";
  $("ad-export").disabled = true;
  const day = $("ad-date").value || meta?.available_dates.at(-1);
  $("ad-date").value = day || "";
  $("ad-shop-label").textContent = `${storeDirectory.find((shop)=>shop.id === currentStore)?.name || "当前门店"} · 手动推广 / 自动推广 · 按广告点击扣费`;
  const query = new URLSearchParams({date:day || "",mode:$("ad-mode").value,high:$("ad-high").value,low:$("ad-low").value});
  try {
    const report = await api(`/api/advertising?${query}`);
    if (requestEpoch !== adEpoch) return;
    if (report.store_id !== currentStore) throw new Error("推广账本与所选门店不一致，请重新选择店铺。");
    adReport = report; renderAdvertising(report); $("ad-notice").textContent = ""; $("ad-export").disabled = false;
  } catch(error) {
    if (requestEpoch !== adEpoch) return;
    adReport = null; $("ad-notice").textContent = error.message;
    for (const id of ["ad-metrics","ad-chart","ad-table","ad-attention"]) $(id).replaceChildren();
  }
}

function smoothCurve(points) {
  // 单调三次Hermite曲线：拐点切线归零，避免普通样条把非负扣费画成负数。
  if(!points.length)return "";
  const d=points.slice(1).map((p,i)=>(p[1]-points[i][1])/(p[0]-points[i][0]));
  const m=points.map((p,i)=>i===0?d[0]||0:i===points.length-1?d.at(-1)||0:d[i-1]*d[i]<=0?0:2*d[i-1]*d[i]/(d[i-1]+d[i]));
  let path=`M ${points[0][0]} ${points[0][1]}`;
  for(let i=1;i<points.length;i++){const a=points[i-1],b=points[i],dx=(b[0]-a[0])/3;path+=` C ${a[0]+dx} ${a[1]+m[i-1]*dx}, ${b[0]-dx} ${b[1]-m[i]*dx}, ${b[0]} ${b[1]}`;}
  return path;
}

function renderAdvertisingCurve(report) {
  const chart=$("ad-chart");chart.replaceChildren();
  const ns="http://www.w3.org/2000/svg", make=(tag,attrs={},text="")=>{const el=document.createElementNS(ns,tag);for(const [key,value] of Object.entries(attrs))el.setAttribute(key,String(value));el.textContent=text;return el;};
  const svg=make("svg",{viewBox:"0 0 1000 290",role:"img","aria-label":"24小时推广扣费平滑曲线，可移动鼠标查看小时数据"});
  const left=66,right=978,top=24,bottom=242,max=Math.max(100,...report.hours.map(h=>h.spend_cents))*1.12;
  const x=h=>left+h/23*(right-left),y=c=>bottom-c/max*(bottom-top);
  for(let i=0;i<=4;i++){const value=max*i/4,cy=y(value);svg.append(make("line",{x1:left,x2:right,y1:cy,y2:cy,class:"curve-grid"}),make("text",{x:left-10,y:cy+4,"text-anchor":"end",class:"curve-label"},`¥${(value/100).toFixed(0)}`));}
  for(const h of [0,3,6,9,12,15,18,21,23])svg.append(make("text",{x:x(h),y:268,"text-anchor":"middle",class:"curve-label"},`${String(h).padStart(2,"0")}:00`));
  const active=report.hours.filter(h=>h.operating!==false);
  const points=active.map(h=>[x(h.hour),y(h.spend_cents)]),curve=smoothCurve(points);
  if(active.length)svg.append(make("path",{d:`${curve} L ${points.at(-1)[0]} ${bottom} L ${points[0][0]} ${bottom} Z`,class:"curve-area"}));
  for(const kind of ["manual","automatic","total"]){
    const values=active.map(h=>[x(h.hour),y(kind==="total"?h.spend_cents:h.modes.find(m=>m.mode===kind)?.spend_cents||0)]);
    svg.append(make("path",{d:smoothCurve(values),class:`curve-line curve-${kind}`,fill:"none"}));
  }
  const marker=make("line",{x1:0,x2:0,y1:top,y2:bottom,class:"curve-crosshair",visibility:"hidden"});
  const dot=make("circle",{cx:0,cy:0,r:5,class:"curve-hover-dot",visibility:"hidden"});svg.append(marker,dot);
  const tip=node("div","curve-tooltip");tip.hidden=true;tip.setAttribute("role","status");chart.append(svg,tip);
  const update=event=>{
    const rect=svg.getBoundingClientRect(),vx=(event.clientX-rect.left)/rect.width*1000;
    const index=Math.max(0,Math.min(23,Math.round((vx-left)/(right-left)*23))),h=report.hours[index];
    tip.replaceChildren(node("strong","",h.label));
    if(h.operating===false){tip.append(node("p","","未营业"));dot.setAttribute("visibility","hidden");}
    else{
      for(const [label,value] of [["总扣费",money(h.spend_cents)],["手动",h.modes.some(m=>m.mode==="manual")?money(h.modes.find(m=>m.mode==="manual").spend_cents):"未选"],["自动",h.modes.some(m=>m.mode==="automatic")?money(h.modes.find(m=>m.mode==="automatic").spend_cents):"未选"],["广告点击",`${h.clicks}次`],["平均CPC",h.cpc_cents===null?"—":money(h.cpc_cents)],["营业额",money(h.revenue_cents)],["费用率",h.spend_ratio_pct===null?"—":h.spend_ratio_pct+"%"],["推广",h.enabled?"投放中":"已停投"]]){const row=node("p");row.append(node("span","",label),node("b","",value));tip.append(row);}
      dot.setAttribute("cx",x(index));dot.setAttribute("cy",y(h.spend_cents));dot.setAttribute("visibility","visible");
    }
    marker.setAttribute("x1",x(index));marker.setAttribute("x2",x(index));marker.setAttribute("visibility","visible");
    tip.hidden=false;tip.style.left=`${Math.max(0,Math.min(chart.clientWidth-220,(event.clientX-chart.getBoundingClientRect().left)+14))}px`;tip.style.top="20px";
  };
  svg.addEventListener("pointermove",update);svg.addEventListener("pointerdown",update);
  svg.addEventListener("pointerleave",()=>{tip.hidden=true;marker.setAttribute("visibility","hidden");dot.setAttribute("visibility","hidden");});
}

function renderAdvertising(report) {
  // 后端计算费用率与关注状态；页面只做分币转元及图形缩放，不重复实现业务阈值。
  const profile=report.operating_profile;
  if(profile?.label) $("ad-shop-label").textContent=`${storeDirectory.find(s=>s.id===currentStore)?.name||"当前门店"} · ${profile.label} · ${profile.open===0&&profile.close===24?"24小时营业":`${profile.open}:00–${profile.close}:00营业`} · ${profile.ad_close===24?"深夜低价持续投放":`${profile.ad_close}:00后停投`}`;
  const total = report.totals, metrics = $("ad-metrics"); metrics.replaceChildren();
  const entries = [["推广扣费",money(total.spend_cents),Object.entries(report.by_mode).map(([kind,item])=>`${kind === "manual" ? "手动" : "自动"} ${money(item.spend_cents)}`).join(" / ")],
    ["店铺营业额",money(total.revenue_cents),"同店同日支付金额 · 非广告归因收入"],
    ["推广费用率",total.spend_ratio_pct === null ? "—" : `${total.spend_ratio_pct}%`,`高费用关注线 ${report.thresholds.high_pct}% · 低投入线 ${report.thresholds.low_pct}%`],
    ["广告点击",total.clicks.toLocaleString("zh-CN"),`平均 CPC ${total.clicks ? money(total.spend_cents / total.clicks) : "—"} / 次`]];
  for (const [label,value,note] of entries) {const card=node("article","ad-metric");card.append(node("span","",label),node("strong","",value),node("small","",note));metrics.append(card);}
  renderAdvertisingCurve(report);
  const attention = $("ad-attention"); attention.replaceChildren();
  let count = 0;
  for (const flag of ["high","no_revenue","low"]) {
    const hours = report.hours.filter((item)=>item.flag === flag); count += hours.length;
    const block=node("div",`ad-alert ${flag}`); block.append(node("strong","",`${adFlags[flag]} · ${hours.length} 个时段`));
    block.append(node("p","",hours.length ? hours.map((item)=>item.label).join("、") : "当前日期暂无此类时段"));
    attention.append(block);
  }
  $("ad-attention-count").textContent = `${count} 个时段`;
  const table=$("ad-table"); table.replaceChildren();
  for (const hour of report.hours) {
    const tr=node("tr",`ad-row-${hour.flag}`);
    const modeCost = (kind) => {const row=hour.modes.find((item)=>item.mode===kind);return row ? money(row.spend_cents) : "未选";};
    const values=[hour.label,hour.operating===false?"—":money(hour.revenue_cents),hour.operating===false?"—":hour.paid_orders??"—",hour.operating===false?"—":hour.visitors??"—",modeCost("manual"),modeCost("automatic"),money(hour.spend_cents),hour.clicks,
      hour.cpc_cents === null ? "—" : money(hour.cpc_cents),hour.spend_ratio_pct === null ? "—" : `${hour.spend_ratio_pct}%`,hour.operating===false ? "未营业" : hour.enabled ? adFlags[hour.flag] : "已停投"];
    for (const value of values) tr.append(node("td","",String(value)));
    table.append(tr);
  }
  $("ad-source").textContent = `${report.date} · 账本版本 ${report.fingerprint.slice(0,8)}`;

}

$("store-select").addEventListener("change",changeStore);
$("nav-investigation").addEventListener("click",async()=>{await showModule("investigation");if(!currentTask&&!conversation?.comparison){const last=conversation?.turns.at(-1);if(last?.kind==='chat'&&last.store_id===currentStore)renderChatReply(last);else if(conversation?.stores[currentStore]?.task_id)await selectTask(conversation.stores[currentStore].task_id);}});
$("new-task").addEventListener("click",()=>showModule("investigation"));
$("nav-advertising").addEventListener("click",()=>showModule("advertising"));
for(const module of ["overview"])$("nav-"+module).addEventListener("click",()=>showModule(module));
$("ad-filters").addEventListener("submit",(event)=>{event.preventDefault();loadAdvertising();});
$("ad-export").addEventListener("click",()=>{
  if (!adReport || adReport.store_id !== currentStore) return;
  const url=URL.createObjectURL(new Blob([JSON.stringify(adReport,null,2)],{type:"application/json"}));
  const link=node("a");link.href=url;link.download=`${adReport.store_id}-${adReport.date}-advertising.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});

async function loadOverview(){
  $("overview-notice").textContent="正在汇总门店经营快照…";
  const day=$("overview-date").value||meta.available_dates.at(-1);$("overview-date").value=day||"";
  try{
    const report=await api(`/api/overview?date=${encodeURIComponent(day||"")}`);
    $("store-cards").replaceChildren();$("overview-totals").replaceChildren();
    const ready=report.stores.filter((s)=>s.sales.ready),ads=report.stores.filter((s)=>s.advertising.ready);
    for(const [label,value,note] of [["门店营业额",ready.length?money(ready.reduce((a,s)=>a+s.sales.revenue_cents,0)):"—",`${ready.length}/${report.stores.length} 店有销售数据`],
      ["成品销量",ready.length?ready.reduce((a,s)=>a+s.sales.units,0).toLocaleString("zh-CN"):"—","束 / 桶 · 按成品计"],
      ["推广扣费",ads.length?money(ads.reduce((a,s)=>a+s.advertising.spend_cents,0)):"—",`${ads.length}/${report.stores.length} 店有推广账本`]]){
      const card=node("article","ad-metric");card.append(node("span","",label),node("strong","",String(value)),node("small","",note));$("overview-totals").append(card);
    }
    for(const shop of report.stores){
      const card=node("article",`store-card${shop.id===currentStore?" current":""}`),head=node("div","store-card-heading");
      head.append(node("h2","",shop.name),node("span","store-city",shop.city));card.append(head);
      const grid=node("div","store-mini-grid");
      for(const [label,value] of [["营业额",shop.sales.ready?money(shop.sales.revenue_cents):"缺数据"],["成品销量",shop.sales.ready?shop.sales.units+" 件":"缺数据"],["推广费用率",shop.advertising.ready&&shop.advertising.spend_ratio_pct!==null?shop.advertising.spend_ratio_pct+"%":"—"],["销量较前日",shop.units_change_pct===null?"基线不足":`${shop.units_change_pct>0?"+":""}${shop.units_change_pct}%`]]){
        const cell=node("div");cell.append(node("small","",label),node("strong","",String(value)));grid.append(cell);
      }
      card.append(grid,node("p","store-card-attention",shop.advertising.ready?`${shop.advertising.high_hours} 个高费用时段 · ${shop.advertising.no_revenue_hours} 个有扣费无成交时段` : "推广账本缺失"));
      const actions=node("div","store-card-actions");
      for(const [label,module] of [["进入店铺","investigation"],["看推广","advertising"]]){
        const button=node("button","text-button",label+" ↗");button.addEventListener("click",async()=>{$("store-select").value=shop.id;await changeStore();await showModule(module);});actions.append(button);
      }
      card.append(actions);$("store-cards").append(card);
    }
    $("overview-notice").textContent="";
  }catch(error){$("overview-notice").textContent=error.message;$("store-cards").replaceChildren();$("overview-totals").replaceChildren();}
}

$("overview-filters").addEventListener("submit",(event)=>{event.preventDefault();loadOverview();});

// 密钥只在提交的瞬间读取；不写入localStorage，也不从后端回显。
async function loadModelSettings(){
  $("account-settings-dialog").showModal();loadContextEditor();$("model-settings-notice").textContent="读取本地配置…";
  try{const settings=await api("/api/model-settings");$("model-nickname").value=settings.nickname;$("model-name").value=settings.model;$("model-enabled").checked=settings.enabled;$("model-api-key").value="";$("model-api-key").placeholder=settings.configured?"已配置 · 留空保留":"请输入 DeepSeek API 密钥";$("model-configuration-state").textContent=settings.configured?"密钥已在本机配置，不会返回浏览器。":"尚未配置密钥。";$("model-settings-notice").textContent="";}
  catch(error){$("model-settings-notice").textContent=error.message;}
}
async function saveModelSettings(){
  const payload={nickname:$("model-nickname").value,model:$("model-name").value,enabled:$("model-enabled").checked};
  if($("model-api-key").value.trim())payload.api_key=$("model-api-key").value.trim();
  try{const saved=await api("/api/model-settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});$("model-configuration-state").textContent=saved.configured?"密钥已在本机配置，不会返回浏览器。":"尚未配置密钥。";meta=await api("/api/meta");return saved;}finally{$("model-api-key").value="";}
}
$("open-account-settings").addEventListener("click",()=>{$("store-context-panel").open=false;loadModelSettings();});
$("close-account-settings").addEventListener("click",()=>{$("model-api-key").value="";$("account-settings-dialog").close();});
$("account-settings-dialog").addEventListener("close",()=>{$("model-api-key").value="";$("workspace-access-token").value="";});
$("model-settings-form").addEventListener("submit",async event=>{event.preventDefault();event.submitter.disabled=true;try{await saveModelSettings();$("model-settings-notice").textContent="已保存，对新调查生效。";}catch(error){$("model-settings-notice").textContent=error.message;}finally{event.submitter.disabled=false;}});
$("test-model-connection").addEventListener("click",async()=>{const button=$("test-model-connection");button.disabled=true;$("model-settings-notice").textContent="正在测试官方接口与工具调用…";try{await saveModelSettings();const result=await api("/api/model-test",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});$("model-settings-notice").textContent=`${result.model} 连接成功，Tool Calling 已验证。`;}catch(error){$("model-settings-notice").textContent=error.message;}finally{button.disabled=false;}});
$("connect-workspace").addEventListener("click",()=>{sessionStorage.setItem("signal-access-token",$("workspace-access-token").value);$("workspace-access-token").value="";$("account-settings-dialog").close();initialize();});

// 经营背景是人工来源的上下文，不提供模型自动写入或从报告自动总结入库的入口。
let contextSourceTask=null;
const contextLabels={night_ads_strategy:"夜间投放策略",budget_policy:"推广费用约定",response_preference:"汇报偏好",product_context:"商品经营背景",period_observation:"阶段观察"};
async function loadContextEditor(fromTask=false){
  contextSourceTask=fromTask&&currentTask?.status==="completed"?currentTask.id:null;
  $("context-store-label").textContent=`${storeDirectory.find(s=>s.id===currentStore)?.name||"本店"} · 旧备注不自动迁入；背景不改写指标或费用线。`;
  $("context-sku").replaceChildren(node("option","","本店全部商品"));$("context-sku").firstChild.value="";
  for(const product of meta?.products||[]){const option=node("option","",`${product.short_name} · ${product.variant}`);option.value=product.sku_id;$("context-sku").append(option);}
  $("context-memory-form").reset();
  const today=new Date().toLocaleDateString('en-CA',{timeZone:'Asia/Shanghai'});
  const end=new Date(Date.now()+30*86400000).toLocaleDateString('en-CA',{timeZone:'Asia/Shanghai'});
  $("context-from").value=fromTask?currentTask.parsed.target_date:today;$("context-expiry").value=end;
  if(fromTask){$("context-sku").value=currentTask.parsed.sku_id;$("context-slot").value="product_context";}
  $("context-source-task").textContent=contextSourceTask?`关联本店调查 ${contextSourceTask.slice(0,8)}；记录内容仍需人工核对。`:"阶段观察请从一条已完成调查的详情栏保存；其他背景可直接录入。";
  $("context-notice").textContent="";await refreshContextMemories();
}
async function refreshContextMemories(){
  try{const {memories}=await api('/api/store-context');$("context-memory-list").replaceChildren();
    const current=memories.filter(m=>['proposed','active'].includes(m.status));
    for(const memory of current){
      const card=node('article','context-memory-item'),state=memory.status==='proposed'?'待确认':memory.effective?'已生效':'已过期或未生效';
      card.append(node('strong','',`${contextLabels[memory.slot]} · ${state}`),node('p','',memory.content),node('small','',`${memory.sku_id||'本店全部商品'} · ${memory.valid_from} 至 ${memory.expires_on} · ${memory.source}`));
      if(memory.shadow_pending)card.append(node('small','context-shadow-note','与另一条同事项背景存在范围遮蔽，检索只取其一，请在下方裁决。'));
      const act=async action=>{try{await api(`/api/store-context/${memory.id}/action`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,expected_revision:memory.revision,note:action==='confirm'?'运营者核对来源与适用范围后确认':'运营者从本店背景管理中撤回'})});await refreshContextMemories();$("context-notice").textContent=action==='confirm'?'已确认，仅影响后续相关调查。':'已撤回，历史任务快照保持原样。';}catch(error){$("context-notice").textContent=error.message;}};
      if(memory.status==='proposed'){const confirm=node('button','text-button','确认用于后续调查');confirm.type='button';confirm.addEventListener('click',()=>act('confirm'));card.append(confirm);}
      const retract=node('button','text-button','撤回');retract.type='button';retract.addEventListener('click',()=>act('retract'));card.append(retract);$("context-memory-list").append(card);
    }
    if(!current.length)$("context-memory-list").append(node('p','account-note','本店暂无背景记录，需要时再添加。'));
    await refreshContextConflicts();
  }catch(error){$("context-notice").textContent=error.message;}
}
async function refreshContextConflicts(){
  const box=$("context-conflict-list");if(!box)return;box.replaceChildren();
  try{const {conflicts}=await api('/api/context-conflicts');
    if(!conflicts.length)return;
    box.append(node('p','account-note',`${conflicts.length} 组同事项背景存在范围遮蔽：检索默认只取商品级，门店级会被静默忽略。裁决后不再提示。`));
    for(const conflict of conflicts){
      const card=node('article','context-conflict-item');
      card.append(node('strong','',`${contextLabels[conflict.slot]||conflict.slot} · 待裁决`));
      for(const side of [['门店级',conflict.general],['商品级',conflict.specific]])if(side[1])card.append(node('p','',`${side[0]}：${side[1].content}`),node('small','',`${side[1].source} · ${side[1].valid_from} 至 ${side[1].expires_on}`));
      const resolve=async(resolution,label)=>{try{await api(`/api/context-conflicts/${conflict.id}/resolve`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({resolution,note:`运营者裁决：${label}`})});await refreshContextMemories();$("context-notice").textContent='已裁决，历史任务快照保持原样。';}catch(error){$("context-notice").textContent=error.message;}};
      for(const entry of [['keep_specific','保留商品级'],['keep_general','保留门店级'],['coexist','两者并存']]){const button=node('button','text-button',entry[1]);button.type='button';button.addEventListener('click',()=>resolve(entry[0],entry[1]));card.append(button);}
      box.append(card);
    }
  }catch(error){$("context-notice").textContent=error.message;}
}
$("context-memory-form").addEventListener('submit',async event=>{event.preventDefault();event.submitter.disabled=true;
  try{const payload={slot:$("context-slot").value,sku_id:$("context-sku").value,content:$("context-content").value,source:$("context-source").value,valid_from:$("context-from").value,expires_on:$("context-expiry").value};if(contextSourceTask)payload.source_task_id=contextSourceTask;
    await api('/api/store-context',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});await refreshContextMemories();$("context-notice").textContent='已保存为候选；核对后点击确认才会参与后续调查。';
  }catch(error){$("context-notice").textContent=error.message;}finally{event.submitter.disabled=false;}});
$("save-task-context").addEventListener('click',async()=>{await loadModelSettings();await loadContextEditor(true);$("store-context-panel").open=true;});
// 进入背景录入时收起API表单，让来源和有效期直接可见，不堆成冗长设置页面。
$("store-context-panel").addEventListener('toggle',()=>{
  const opened=$("store-context-panel").open;
  $("account-settings-dialog").classList.toggle('context-focus',opened);
  $("account-settings-title").textContent=opened?'本店经营背景':'账号与 API 设置';
  $("account-settings-dialog").scrollTop=0;
});
