/* 用真实前端函数检查停止按钮和请求身份，避免仅中断浏览器而后台继续执行。 */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(require('node:path').join(__dirname,'../ecommerce_agent/web_assets/app.js'),'utf8');
const button={},calls=[];
const context={meta:{capabilities:{cancellation:true}},conversationBusy:false,currentTask:null,
  pendingRequest:null,stopRequested:false,stopInProgress:false,$:()=>button,notify(){},
  async api(url,options){calls.push({url,options});},async selectTask(){context.currentTask.status='cancelled';}};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function syncStopButton()'),source.indexOf("$('stop-request').addEventListener")),context);
(async()=>{
  context.syncStopButton();assert.equal(button.hidden,true);
  context.conversationBusy=true;context.syncStopButton();assert.equal(button.hidden,false);
  context.pendingRequest={cid:'conversation-1',id:'request-1'};
  await context.stopCurrentRequest();assert.equal(calls[0].url,'/api/conversations/conversation-1/cancel');
  assert.equal(JSON.parse(calls[0].options.body).request_id,'request-1');assert.equal(button.disabled,true);
  context.conversationBusy=false;context.stopRequested=false;context.currentTask={id:'job-1',status:'queued'};
  await context.stopCurrentRequest();assert.equal(calls[1].url,'/api/tasks/job-1/cancel');assert.equal(button.hidden,true);
  context.currentTask={status:'cancelling'};context.syncStopButton();assert.equal(button.disabled,true);
  context.conversationBusy=true;context.pendingRequest=null;const before=calls.length;
  await context.stopCurrentRequest();assert.equal(calls.length,before);assert.equal(context.stopRequested,true);
  context.stopInProgress=true;await context.stopCurrentRequest();assert.equal(calls.length,before);
  console.log('停止交互：8项检查通过。');
})().catch(error=>{console.error(error);process.exitCode=1;});
