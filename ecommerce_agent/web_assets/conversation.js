/* 对话UI只展示服务端状态，不自行拼接商品/日期或把历史指标塞给新任务。 */
let comparisonPollTimer=null;
async function startConversation(){
  if(!meta?.capabilities.conversation_state){conversation=null;conversationId=null;renderConversation();return;}
  const result=await api('/api/conversations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({store_id:currentStore})});
  conversation=result.conversation;conversationId=conversation.id;sessionStorage.setItem('flower-conversation-id',conversationId);renderConversation();
}
async function restoreConversation(){
  if(!meta?.capabilities.conversation_state){renderConversation();return;}
  if(!conversationId){renderConversation();return;}
  try{const result=await api(`/api/conversations/${conversationId}`);await adoptConversation(result.conversation);
    const latest=conversation.turns.at(-1);if(latest?.kind==='chat'||latest?.kind==='cancelled'){resetTask();renderChatReply(latest);}}
  catch(error){if(error.status===404){conversation=null;conversationId=null;sessionStorage.removeItem('flower-conversation-id');renderConversation();}else throw error;}
}
async function adoptConversation(state){
  conversation=state;conversationId=state.id;sessionStorage.setItem('flower-conversation-id',state.id);
  if(currentStore!==state.focus_store){$('store-select').value=state.focus_store;await changeStore(true);}
  renderConversation();
}
function scopeText(scope,sid){
  const product=meta?.products.find(p=>p.sku_id===scope?.sku);
  return [storeDirectory.find(s=>s.id===sid)?.name||sid,scope?.product_scope?.label||(product?`${product.short_name} · ${product.variant}`:'商品待确认'),conversationTime(scope),intentLabels[scope?.intent]||'事项待确认'].join(' / ');
}
function conversationTime(scope){return (scope?.time?.is_window?`${scope.time.trend_start} 至 ${scope.time.trend_end}`:scope?.date||'日期待确认')+(scope?.time?.comparison_date?` · 基线 ${scope.time.comparison_date}`:'');}
function renderConversation(){
  const sid=conversation?.focus_store||currentStore,scope=conversation?.comparison?.scope||conversation?.stores[sid]?.scope||{};
  let phase=$('conversation-phase');
  if(!phase){phase=node('span','conversation-phase');phase.id='conversation-phase';document.querySelector('.conversation-state-heading').firstElementChild.append(phase);}
  const latest=[...(conversation?.turns||[])].reverse().find(t=>t.store_id===sid&&t.task_id);
  const chatting=conversation?.turns.at(-1)?.kind==='chat';
  phase.textContent=chatting?'对话中':conversation?.comparison?(conversation.comparison.pending?'逐店查询中':'比较已完成'):!conversation?.turns.length?'等待提问':conversation.waiting.length?'等待确认':labels[latest?.status]||'条件已确认';
  if(conversation?.turns.at(-1)?.kind==='cancelled'||conversation?.turns.at(-1)?.chat?.mode==='cancelled')phase.textContent='已停止';
  $('conversation-scope').replaceChildren();
  const product=meta?.products.find(p=>p.sku_id===scope.sku);
  for(const [label,text,ready] of [['门店',conversation?.comparison?`${conversation.comparison.rows.length}家门店比较`:storeDirectory.find(s=>s.id===sid)?.name||'正在读取',true],['商品',scope.product_scope?.label||(product?`${product.short_name} · ${product.variant}`:'待确认'),!!scope.sku],['日期',conversationTime(scope),!!scope.date],['事项',intentLabels[scope.intent]||'待确认',!!scope.intent]]){
    const field={门店:'store',商品:'sku',日期:'date',事项:'intent'}[label],pending=conversation?.waiting.includes(field);
    const item=node('div',`conversation-slot ${ready&&!pending?'confirmed':'pending'}`);item.append(node('small','',label),node('strong','',pending?'待确认':text));$('conversation-scope').append(item);
  }
  $('conversation-notice').textContent=!meta?.capabilities.conversation_state?'当前服务尚未加载对话功能；重启后生效，原单次调查仍可使用。':conversation?.comparison?(conversation.comparison.pending?'正在按同一商品、日期和基线分别查询各店。':'各店查询已结束。只比较可用数据，独立证据可逐店核对。'):conversation?.notice||'直接提问。商品、日期和门店确认后，会显示在这里。';
  $('conversation-turn-count').textContent=String(conversation?.turns.length||0);$('conversation-turns').replaceChildren();
  for(const turn of conversation?.turns||[]){
    const row=node('article','conversation-turn'),index=(conversation.turns.indexOf(turn)+1);
    row.append(node('small','',`第${index}轮 · ${turn.kind==='chat'?'对话':turn.kind==='comparison'?'跨店比较':turn.kind==='resume'?'恢复上下文':turn.kind==='clarification'?'需要确认':labels[turn.status]||'调查请求'}`),node('p','',turn.question),node('small','',turn.message||scopeText(turn.scope,turn.store_id)));
    if(turn.kind==='chat'){const button=node('button','text-button','查看这轮回复 ↗');button.type='button';button.addEventListener('click',()=>{resetTask();renderChatReply(turn);});row.append(button);}
    if(turn.comparison){const button=node('button','text-button','查看本轮跨店比较 ↗');button.type='button';button.addEventListener('click',()=>openStoreComparison(turn.comparison).catch(error=>notify(error.message)));row.append(button);}
    if(turn.task_id){const button=node('button','text-button','恢复这轮上下文与结果 ↗');button.type='button';button.addEventListener('click',()=>openConversationTurn(turn).catch(error=>notify(error.message)));row.append(button);}
    $('conversation-turns').append(row);
  }
  $('conversation-store-choices').replaceChildren();
  if(conversation?.waiting.includes('store'))for(const id of conversation.store_choices||[]){
    const button=node('button','candidate-button',storeDirectory.find(s=>s.id===id)?.name||id);button.type='button';
    // 确认只补门店，不通过切店清空原问题；服务器用待确认版本继续原来的商品/日期。
    button.addEventListener('click',()=>{if(conversationBusy||!conversation.pending_store)return;
      const question=conversation.pending_store.question;$('question').value=question;
      pendingConfirmation={question,store_choice:id};$('investigation-form').requestSubmit();});$('conversation-store-choices').append(button);
  }
  if(conversation?.waiting.includes('sku')&&conversation.pending_product_patch)for(const product of conversation.pending_product_patch.candidates){
    const button=node('button','candidate-button',`确认改用 ${product.short_name} · ${product.variant}`);button.type='button';
    button.addEventListener('click',()=>{$('question').value=conversation.pending_product_patch.question;pendingConfirmation={question:conversation.pending_product_patch.question,sku:product.sku_id};$('investigation-form').requestSubmit();});$('conversation-store-choices').append(button);
  }
  renderStoreComparison();
}

function renderChatReply(turn){
  // 普通对话没有调查状态、数据证据或下载报告，不伪造“查询完成”。
  document.body.classList.add('has-task');$('session-title').textContent='与花迹对话';
  $('empty-state').hidden=true;$('summary').hidden=false;
  $('task-context').hidden=false;$('task-context').replaceChildren(node('strong','',turn.question));
  const problem=turn.chat?.error_info;
  $('task-status').textContent=problem?problem.title:'已回复';$('task-status').className=problem?'badge error':'badge';
  const answer=node('article','chat-answer');
  for(const paragraph of turn.message.split(/\n+/).filter(Boolean))answer.append(node('p','',paragraph));
  $('summary').replaceChildren(answer);
  renderRecoveryActions(answer,problem,turn.question);
  renderModelRuntime(answer,turn.model_runtime);
}
async function openConversationTurn(turn){
  if(conversationBusy)return;
  const result=await api(`/api/conversations/${conversation.id}/focus`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({store_id:turn.store_id,task_id:turn.task_id,revision:conversation.revision})});
  await adoptConversation(result.conversation);await selectTask(turn.task_id);
}
$('edit-conversation-scope').addEventListener('click',()=>{$('question').focus();notify('直接描述要修改的条件，例如：改看10月1日至15日，或只看抱抱桶。');});

async function openStoreComparison(job){
  const result=await api(`/api/conversations/${conversation.id}/focus`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({store_id:job.anchor_store,comparison_id:job.id,revision:conversation.revision})});
  await adoptConversation(result.conversation);resetTask();renderConversation();
}
function renderStoreComparison(){
  clearTimeout(comparisonPollTimer);
  const panel=$('store-comparison'),job=conversation?.comparison;document.body.classList.toggle('has-comparison',!!job);panel.replaceChildren();panel.hidden=!job;if(!job)return;
  if(activeModule==='investigation'&&!currentTask)$('session-title').textContent='跨店比较';
  const ranking=job.scope.product_scope?.ranking;
  const storeRanking=!!job.store_ranking;
  panel.append(node('p','eyebrow','STORE / COMPARISON'),node('h2','',`${storeRanking?'门店销量排名':ranking?'各店独立排名':'同一条件'} · ${job.rows.length}家门店`),node('p','comparison-summary',job.summary),node('p','comparison-scope',scopeText(job.scope,job.anchor_store)));
  const sales=['query_sales_performance','investigate_sales_change'].includes(job.scope.intent),inventory=job.scope.intent==='query_inventory',window=job.scope.time?.is_window;
  const directions=ranking?(ranking.direction==='both'?['desc','asc']:[ranking.direction]):[];
  const labels=storeRanking?['名次','门店','状态','成品销量（件）','证据']:ranking?['门店','状态',...directions.flatMap(d=>[d==='asc'?'销量最低商品':'销量最高商品','成品销量（件）']),'数据覆盖','证据']:sales?['门店','状态',...(window?['窗口销量']:[]),'末日销量','基线销量','变化','证据']:inventory?['门店','状态','缺货分钟','监测分钟','证据']:['门店','状态','活动状态','活动名称','证据'];
  const wrap=node('div','table-wrap'),table=node('table'),head=node('thead'),hr=node('tr');for(const title of labels)hr.append(node('th','',title));head.append(hr);table.append(head);const body=node('tbody');
  for(const row of job.rows){
    const tr=node('tr'),m=row.metrics,empty=value=>value===null||value===undefined?'—':String(value);
    const name=row.store_name+(!ranking&&!storeRanking&&row.store_id===job.anchor_store?' · 参照店':'');
    const values=[...(storeRanking?[empty(row.store_rank)]:[]),name,row.comparable?(ranking||storeRanking?'覆盖完整':'可比较'):row.status==='completed'?'数据不足':labelsStatus(row.status)];
    if(storeRanking)values.push(empty(m.units));
    else if(ranking){
      for(const direction of directions){const part=row.ranking?.partitions?.find(p=>p.direction===direction),leaders=part?.leaders||[];
        const names=leaders.slice(0,3).map(p=>p.name+' · '+p.variant).join('、');
        values.push(names+(leaders.length>3?` 等${leaders.length}款并列`:leaders.length>1?'（并列）':'')||'—',empty(leaders[0]?.units_sold));}
      values.push(row.ranking?`${row.ranking.eligible_count??0}/${row.ranking.candidate_count??'—'} ${ranking.group_by==='sku'?'款':'组'}`:'—');
    }else if(sales){if(window)values.push(empty(m.window_units));values.push(empty(m.units),empty(m.baseline_units),m.change_pct===null||m.change_pct===undefined?'—':`${m.change_pct>0?'+':''}${m.change_pct}%`);}
    else if(inventory)values.push(empty(m.out_of_stock_minutes),empty(m.monitored_minutes));
    else values.push(m.active===undefined?'—':m.active?'开启':'未开启',m.campaign_name||'—');
    for(const value of values)tr.append(node('td','',value));
    const cell=node('td');if(row.task_id){const button=node('button','text-button','查看本店证据 ↗');button.type='button';button.addEventListener('click',()=>openConversationTurn({store_id:row.store_id,task_id:row.task_id}).catch(error=>notify(error.message)));cell.append(button);}else cell.textContent='不可用';tr.append(cell);body.append(tr);
    if(row.note){const note=node('tr'),td=node('td','comparison-row-note',`${row.store_name}：${row.note}`);td.colSpan=labels.length;note.append(td);body.append(note);}
  }
  table.append(body);wrap.append(table);panel.append(wrap,node('p','comparison-scope',storeRanking?job.store_ranking.assumption:ranking?'按各店目录中所查期间在售商品分别排名；未上架不补零，缺失不参与冠军认定，并列保留。当前目录未必覆盖当时全部历史在售商品，点击证据可核对范围与明细。':'各店独立查询；缺数据与失败不按零处理，零基线不计算百分比。窗口汇总和末日对比分开显示，点击证据可核对原始记录。'));
  if(job.pending){const id=conversation.id;comparisonPollTimer=setTimeout(async()=>{if(conversation?.id!==id||conversationBusy)return;try{const result=await api(`/api/conversations/${id}`);if(conversation?.id===id&&result.conversation.revision>=conversation.revision){conversation=result.conversation;renderConversation();}}catch(error){notify(error.message);}},1000);}
}
function labelsStatus(status){return labels[status]||'不可用';}
