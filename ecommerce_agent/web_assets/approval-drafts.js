/* 审批草稿独立加载与提交，使用当前门店的工作台 API。 */
let draftsRequest = 0;

async function loadApprovalDrafts() {
  const request = ++draftsRequest;
  const store = currentStore;
  const list = $('approval-drafts-list');
  const status = $('approval-drafts-status');
  const isVisibleStore = () => store === currentStore && activeModule === 'approval-drafts';
  const isCurrent = () => request === draftsRequest && isVisibleStore();
  list.replaceChildren();
  status.textContent = '正在读取待审批草稿';
  try {
    const {drafts} = await api('/api/approval-drafts?status=pending');
    if (!isCurrent()) return;
    status.textContent = drafts.length ? '' : '暂无待审批草稿';
    for (const draft of drafts) {
      const blocked = draft.policy.status === 'blocked';
      const card = node('article', blocked ? 'ops-panel recommendation recommendation--blocked' : 'ops-panel');
      const heading = node('h2', '', [draft.sku_id, draft.date].filter(Boolean).join(' · ') || '整改建议');
      card.append(heading, node('p', '', draft.description));
      if (blocked) card.append(node('strong', '', draft.policy.label), node('p', '', draft.policy.reason));

      const actions = node('div', 'draft-actions');
      const approve = node('button', 'draft-approve', '批准执行');
      const reject = node('button', 'draft-reject', '驳回');
      approve.type = reject.type = 'button';
      approve.disabled = blocked;
      const error = node('p', 'draft-error');
      error.setAttribute('role', 'alert');

      const decide = async action => {
        if (!isCurrent() || (action === 'approve' && blocked)) return;
        approve.disabled = reject.disabled = true;
        error.textContent = '';
        try {
          await api(`/api/approval-drafts/${encodeURIComponent(draft.id)}/decision`, {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action}),
          });
          if (isVisibleStore()) await loadApprovalDrafts();
        } catch (problem) {
          if (isCurrent()) error.textContent = problem.message;
        } finally {
          approve.disabled = blocked;
          reject.disabled = false;
        }
      };
      approve.addEventListener('click', () => decide('approve'));
      reject.addEventListener('click', () => decide('reject'));
      actions.append(approve, reject);
      card.append(actions, error);
      list.append(card);
    }
  } catch (error) {
    if (isCurrent()) status.textContent = error.message;
  }
}

$('nav-approval-drafts').addEventListener('click', () => showModule('approval-drafts'));
