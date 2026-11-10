/* 运营日程与风险雷达共用工作台的门店上下文和请求入口。 */
let scheduleRequest = 0;
let scheduleStore = null;
let scheduleView = 'calendar';
let scheduleMonth = null;
let scheduleToday = null;
let scheduleGroups = new Map();
let scheduleHolidays = {};
let riskSnapshot = null;
let riskRequest = 0;
const planKinds = {stocking: '备货计划', staffing: '员工排班'};
const riskKinds = {activity_gap: '掉活动未续', low_stock: '库存告急', sales_drop: '销量断崖下跌'};

function scheduleStatus(message) {
  const status = $('schedule-status');
  status.textContent = message;
  status.hidden = !message;
}

async function loadSchedule(month = scheduleMonth) {
  const request = ++scheduleRequest;
  const store = currentStore;
  const isCurrent = () => request === scheduleRequest && store === currentStore && activeModule === 'calendar';
  if (scheduleStore !== store) {
    $('schedule-form').reset();
    $('schedule-days').replaceChildren();
    $('schedule-plans').replaceChildren();
    scheduleStore = store;
  }
  scheduleStatus('正在读取运营计划');
  try {
    const [{plans}, calendar] = await Promise.all([
      api('/api/schedule-plans'),
      api('/api/schedule' + (month ? '?month=' + encodeURIComponent(month) : '')),
    ]);
    if (!isCurrent()) return;
    scheduleMonth = calendar.month;
    scheduleToday = calendar.today;
    scheduleHolidays = calendar.holidays;
    scheduleGroups = new Map();
    for (const plan of plans) {
      if (!scheduleGroups.has(plan.plan_date)) scheduleGroups.set(plan.plan_date, []);
      scheduleGroups.get(plan.plan_date).push(plan);
    }
    renderSchedule();
  } catch (error) {
    if (isCurrent()) scheduleStatus(error.message);
  }
}

function renderSchedule() {
  const calendar = scheduleView === 'calendar';
  $('schedule-calendar').hidden = $('schedule-month-controls').hidden = !calendar;
  $('schedule-plans').hidden = calendar;
  $('schedule-calendar-mode').setAttribute('aria-pressed', String(calendar));
  $('schedule-list-mode').setAttribute('aria-pressed', String(!calendar));
  if (!scheduleMonth) return;
  if (!calendar) {
    $('schedule-plans').replaceChildren(...Array.from(scheduleGroups, ([date, entries]) => {
      const group = node('section', 'ops-panel');
      const items = node('ul');
      for (const plan of entries) items.append(node('li', '', `${planKinds[plan.kind]} · ${plan.title}`));
      group.append(node('h2', '', date), items);
      return group;
    }));
    scheduleStatus(scheduleGroups.size ? '' : '暂无运营计划');
    return;
  }

  const [year, month] = scheduleMonth.split('-').map(Number);
  const start = new Date(Date.UTC(year, month - 1, 1));
  const count = new Date(Date.UTC(year, month, 0)).getUTCDate();
  $('schedule-month').textContent = `${year} 年 ${month} 月`;
  const grid = $('schedule-days');
  grid.replaceChildren();
  for (let offset = 0; offset < (start.getUTCDay() + 6) % 7; offset++) grid.append(node('span'));
  for (let day = 1; day <= count; day++) {
    const date = scheduleMonth + '-' + String(day).padStart(2, '0');
    const plans = scheduleGroups.get(date) || [];
    const holidays = scheduleHolidays[date] || [];
    const cell = node('button', 'schedule-day');
    cell.type = 'button';
    cell.dataset.date = date;
    cell.setAttribute('aria-label', [date, ...holidays, plans.length ? `${plans.length} 项计划` : ''].filter(Boolean).join('，'));
    if (date === scheduleToday) cell.setAttribute('aria-current', 'date');
    const title = node('span', 'schedule-day-title', String(day));
    if (holidays.length) title.append(node('span', 'schedule-holiday-dot'));
    cell.append(title);
    for (const plan of plans.slice(0, 2)) {
      const entry = node('span', 'schedule-entry', plan.title);
      entry.title = `${planKinds[plan.kind]} · ${plan.title}`;
      cell.append(entry);
    }
    if (plans.length > 2) cell.append(node('small', '', `+${plans.length - 2} 项`));
    cell.addEventListener('click', () => focusScheduleForm(date));
    grid.append(cell);
  }
  scheduleStatus('');
}

function focusScheduleForm(date) {
  if (date) $('schedule-date').value = date;
  (date ? $('schedule-kind') : $('schedule-date')).focus();
}

function moveScheduleMonth(offset) {
  if (!scheduleMonth) return;
  const [year, month] = scheduleMonth.split('-').map(Number);
  loadSchedule(new Date(Date.UTC(year, month - 1 + offset, 1)).toISOString().slice(0, 7));
}

const riskTimeFormat = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});

async function loadRiskAlerts() {
  const store = currentStore;
  const request = ++riskRequest;
  const isCurrent = () => request === riskRequest && store === currentStore && activeModule === 'radar';
  try {
    const result = await api('/api/risk-alerts');
    if (!isCurrent()) return;
    $('risk-status').textContent = result.alerts.length
      ? `${result.sample_data ? '模拟数据' : '门店数据'} · 最近数据日期 ${result.latest_data_date}`
      : '暂无风险预警';
    const snapshot = JSON.stringify([store, result.alerts]);
    if (snapshot === riskSnapshot) return;
    riskSnapshot = snapshot;
    const cards = result.alerts.map(alert => {
      const card = node('article', 'ops-panel ops-alert');
      const text = node('div');
      text.append(node('small', 'ops-alert-kind', riskKinds[alert.kind] || alert.kind),
                  node('h2', '', alert.product_name),
                  node('p', '', `SKU ${alert.sku_id} · 业务日期 ${alert.observed_date}`),
                  node('p', '', alert.detail));
      const updated = Date.parse(alert.updated_at);
      if (Number.isFinite(updated)) {
        const row = node('p');
        const time = node('time', '', riskTimeFormat.format(updated));
        time.dateTime = alert.updated_at;
        row.append('更新时间 ', time);
        text.append(row);
      }
      const button = node('button', 'primary', '去调查');
      button.type = 'button';
      button.addEventListener('click', async () => {
        if (currentStore !== store) return;
        button.disabled = true;
        try {
          await startConversation(store || meta?.business_context?.store_id || 'local');
          if (currentStore !== store || activeModule !== 'radar') return;
          resetTask();
          await showModule('investigation');
          const input = $('question');
          input.value = alert.kind === 'sales_drop'
            ? alert.question.replace('销量变化原因', '销量暴跌原因')
            : alert.question;
          pendingConfirmation = {question: input.value, sku: alert.sku_id};
          if (alert.kind === 'sales_drop') pendingConfirmation.intent = 'investigate_sales_change';
          input.dispatchEvent(new Event('input', {bubbles: true}));
          input.focus();
        } catch (error) {
          if (activeModule === 'radar') $('risk-status').textContent = error.message;
        } finally {
          button.disabled = false;
        }
      });
      card.append(text, button);
      return card;
    });
    $('risk-alerts').replaceChildren(...cards);
  } catch (error) {
    if (isCurrent()) $('risk-status').textContent = error.message;
  }
}

$('nav-calendar').addEventListener('click', () => showModule('calendar'));
$('nav-radar').addEventListener('click', () => {
  riskSnapshot = null;
  $('risk-alerts').replaceChildren();
  $('risk-status').textContent = '正在读取风险预警';
  showModule('radar');
});
$('schedule-new').addEventListener('click', () => focusScheduleForm());
$('schedule-today').addEventListener('click', () => loadSchedule(scheduleToday?.slice(0, 7)));
$('schedule-prev').addEventListener('click', () => moveScheduleMonth(-1));
$('schedule-next').addEventListener('click', () => moveScheduleMonth(1));
$('schedule-calendar-mode').addEventListener('click', () => { scheduleView = 'calendar'; renderSchedule(); });
$('schedule-list-mode').addEventListener('click', () => { scheduleView = 'list'; renderSchedule(); });
$('schedule-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const store = currentStore;
  const date = $('schedule-date').value;
  const kind = $('schedule-kind').value;
  const title = $('schedule-text').value.trim();
  if (!date || !kind || !title) {
    scheduleStatus('请填写日期、计划类型和具体内容。');
    return;
  }
  const submit = form.querySelector('[type="submit"]');
  if (submit.disabled) return;
  submit.disabled = true;
  scheduleStatus('');
  try {
    await api('/api/schedule', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({date, kind, title})});
    if (store !== currentStore) return;
    form.reset();
    if (activeModule === 'calendar') await loadSchedule(date.slice(0, 7));
  } catch (error) {
    if (store === currentStore) scheduleStatus(error.message);
  } finally {
    submit.disabled = false;
  }
});
setInterval(() => {
  if (activeModule === 'radar' && !document.hidden) loadRiskAlerts();
}, 15000);
