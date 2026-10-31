/* 无浏览器验收发送处理器：成功清空、失败保留、等待期间的新草稿保留，两种API路径一致。 */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const app = fs.readFileSync(path.join(__dirname, '../ecommerce_agent/web_assets/app.js'), 'utf8');
const source = app.match(/\$\("investigation-form"\)\.addEventListener\("submit", (async \(event\) => \{[\s\S]*?\n\})\);/)[1];
async function check(conversational, failure = false, newDraft = false, chat = false) {
  let replies=0;
  const elements = Object.fromEntries(['question','sku','target-date','intent','submit','store-select'].map(id => [id, {value: id === 'question' ? '99红玫昨天销量怎么样' : '', disabled:false}]));
  const context = { $:id=>elements[id], notify:()=>{}, meta:{capabilities:{conversation_state:conversational}},
    currentTask:null,pendingConfirmation:null, conversationBusy:false, currentStore:'store-a',conversation:{id:'conversation-a',revision:0},
    crypto:{randomUUID:()=> 'test-key'},startConversation:async()=>{},adoptConversation:async()=>{},
    selectTask:async()=>{},resetTask:()=>{},renderConversation:()=>{},
    renderChatReply:()=>replies++,
    api:async()=>{if(failure)throw new Error('offline');if(newDraft)elements.question.value='正在输入的新问题';return chat?{task:null,conversation:{turns:[{kind:'chat',message:'你好'}]}}:{task:{id:'task-a'},conversation:{}};}};
  const handler=vm.runInNewContext('('+source+')',context);
  context.conversationRequest=async()=>context.api();
  await handler({preventDefault(){}});
  assert.equal(elements.question.value,failure?'99红玫昨天销量怎么样':newDraft?'正在输入的新问题':'');
  assert.equal(elements.submit.disabled,false);
  assert.equal(replies,chat&&!failure?1:0);
}
(async()=>{
  for(const conversational of [false,true]){await check(conversational);await check(conversational,true);await check(conversational,false,true);}
  await check(true,false,false,true);await check(true,false,true,true);
  assert.equal(app.includes('意图路由：'),false);
  assert.equal(app.includes('点击蓝色发送按钮继续调查'),false);
  assert.equal(fs.readFileSync(path.join(__dirname,'../ecommerce_agent/web_assets/index.html'),'utf8').includes('id="route-decision"'),false);
  const html=fs.readFileSync(path.join(__dirname,'../ecommerce_agent/web_assets/index.html'),'utf8');
  for(const id of ['scope-settings','sku','intent','target-date','question-mode'])assert.equal(html.includes(`id="${id}"`),false);
  assert.equal(app.includes("const questionMode="),false);
  assert.ok(app.includes('补充查询日期或时间段'));
  console.log('PASS: 8 composer cases, including visible chat reply and draft preservation');
})().catch(error=>{console.error(error);process.exitCode=1;});
