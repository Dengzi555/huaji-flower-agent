/* 每5秒读取源码定义；新SVG与清单来自服务端的固定源码白名单，不执行源码文本。 */
"use strict";
const $=id=>document.getElementById(id);
const node=(tag,text)=>{const el=document.createElement(tag);el.textContent=text;return el;};
let version="",imageUrl=null,inFlight=false;
async function refresh(){
  if(inFlight)return;inFlight=true;
  try{
    const token=sessionStorage.getItem("signal-access-token");
    const response=await fetch('/api/flow',{headers:token?{Authorization:`Bearer ${token}`}:{}});
    if(!response.ok)throw new Error(response.status===401?'请先在工作台连接访问口令，再在同一标签页打开流程图。':'源码正在编辑或服务暂不可用，请保存后稍候重试。');
    const report=await response.json();
    $('flow-status').textContent=`源码版本 ${report.version} · ${report.model_enabled?'模型已启用':'模型未启用，复杂问题的模型规划不可用'} · ${report.restart_required?'检测到Python改动：清单已刷新，运行服务需重启后生效':'运行进程与源码版本一致'} · 每5秒检查`;
    $('flow-status').className=report.restart_required?'changed':'synced';
    $('flow-maintenance').textContent=report.maintenance;
    if(version===report.version)return;
    version=report.version;
    const previous=imageUrl;imageUrl=URL.createObjectURL(new Blob([report.svg],{type:'image/svg+xml'}));$('flow-image').src=imageUrl;
    if(previous)URL.revokeObjectURL(previous);
    $('stage-rows').replaceChildren(...report.stages.map(stage=>{const tr=node('tr','');for(const key of ['stage','input','output','source'])tr.append(node('td',stage[key]));return tr;}));
    $('capability-rows').replaceChildren(...Object.entries(report.capabilities?.operations||{}).map(([name,spec])=>{
      const row=node('tr','');for(const text of [name,spec.requires.join('、'),spec.produces.join('、'),spec.failure_policy])row.append(node('td',text));return row;}));
    for(const [id,entries] of [['route-list',report.routes],['tool-list',report.tools]]){
      $(id).replaceChildren(...entries.map(item=>{const row=node('article','');row.append(node('code',item.name),node('p',item.label||item.description));return row;}));
    }
  }catch(error){$('flow-status').textContent=error.message;$('flow-status').className='changed';}
  finally{inFlight=false;}
}
$('refresh-flow').addEventListener('click',refresh);refresh();setInterval(()=>{if(!document.hidden)refresh();},5000);
