/* 使用真实弹窗函数验证确认边界；模拟DOM，不调用模型或改写门店数据。 */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(require('node:path').join(__dirname,'../ecommerce_agent/web_assets/app.js'),'utf8');
function element(tag='div',className='',textContent=''){
  return {tag,className,textContent,children:[],listeners:{},value:'',checked:false,append(...items){this.children.push(...items)},prepend(...items){this.children.unshift(...items)},
    setAttribute(){},addEventListener(name,fn){this.listeners[name]=fn},focus(){this.focused=true},showModal(){this.open=true},close(){this.open=false},remove(){this.removed=true}};
}
const body=element('body'),fields={question:{value:''},'investigation-form':{requestSubmit(){fields.sent=(fields.sent||0)+1}}};
const timers=[];
const context={node:element,document:{body},Promise,Set,setTimeout:fn=>timers.push(fn),conversationBusy:false,pendingConfirmation:null,
  $:id=>fields[id],notify:text=>{context.notice=text;}};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('let activeQuestionDialog='),source.indexOf('function notify(')),context);
const walk=(root,predicate)=>[...(predicate(root)?[root]:[]),...root.children.filter(x=>typeof x==='object').flatMap(x=>walk(x,predicate))];
const latest=()=>body.children.at(-1);
const submit=dialog=>walk(dialog,n=>n.tag==='form')[0].listeners.submit({preventDefault(){}});
async function main(){
  let pending=context.askQuestion({title:'确认背景',choices:[{label:'确认',value:'confirm'}]});
  let dialog=latest();submit(dialog);assert.equal(dialog.open,true,'未选择不能提交');
  assert.match(walk(dialog,n=>n.tag==='p').at(-1).textContent,/请选择/);
  const radios=walk(dialog,n=>n.type==='radio');assert.equal(radios.some(r=>r.checked),false,'没有预选');
  assert.equal(await context.askQuestion({title:'重复弹窗'}),null,'不能同时打开两个弹窗');
  radios[0].checked=true;submit(dialog);assert.equal((await pending).choice.value,'confirm');assert.equal(dialog.removed,true);

  pending=context.askQuestion({title:'时间范围',allowText:true});dialog=latest();submit(dialog);assert.equal(dialog.open,true);
  walk(dialog,n=>n.tag==='textarea')[0].value='10月1日至15日';submit(dialog);assert.equal((await pending).text,'10月1日至15日');

  pending=context.askQuestion({title:'取消',choices:[{label:'确认'}]});dialog=latest();dialog.listeners.cancel({preventDefault(){}});assert.equal(await pending,null);

  let valid=true;const box=element();
  context.mountFollowup(box,{key:'task:1',title:'哪家店',originalQuestion:'昨天销量',choices:[{label:'南岸店',payload:{store_choice:'cq'}}],valid:()=>valid});
  timers.shift()();dialog=latest();walk(dialog,n=>n.type==='radio')[0].checked=true;valid=false;submit(dialog);await new Promise(setImmediate);
  assert.equal(fields.sent||0,0,'上下文变更不能发送旧确认');assert.match(context.notice,/上下文已变化/);
  valid=true;box.children[0].onclick();dialog=latest();walk(dialog,n=>n.type==='radio')[0].checked=true;submit(dialog);await new Promise(setImmediate);
  assert.equal(fields.sent,1);assert.equal(context.pendingConfirmation.store_choice,'cq');assert.equal(fields.question.value,'昨天销量');
  context.mountFollowup(element(),{key:'task:1',valid:()=>true});assert.equal(timers.length,0,'重复渲染不重新弹窗');

  // 记忆确认关闭/拒绝不写入；切店后不能把旧确认发到新店。
  const calls=[];context.currentStore='cq';context.storeDirectory=[{id:'cq',name:'南岸店'}];context.meta={products:[]};
  context.api=async(...args)=>{calls.push(args);return {conflicts:[]};};context.refreshContextMemories=async()=>{};
  vm.runInContext(source.slice(source.indexOf('async function confirmContextMemory('),source.indexOf('async function loadContextEditor(')),context);
  const memory={id:'m1',sku_id:'sku1',content:'商品背景',source:'运营',valid_from:'2026-10-01',expires_on:'2026-11-01',revision:1};
  context.askQuestion=async()=>null;await context.confirmContextMemory(memory);assert.equal(calls.length,0);
  context.askQuestion=async()=>({choice:{label:'暂不确认'}});await context.confirmContextMemory(memory);assert.equal(calls.length,0);
  context.askQuestion=async()=>{context.currentStore='sz';return {choice:{label:'确认生效'}};};
  await assert.rejects(()=>context.confirmContextMemory(memory),/门店已切换/);assert.equal(calls.length,0);
  context.currentStore='cq';context.askQuestion=async()=>({choice:{label:'确认生效'}});await context.confirmContextMemory(memory);
  assert.equal(calls.length,2);assert.equal(JSON.parse(calls[0][1].body).expected_revision,1);
  console.log('确认弹窗：无预选、自由输入、取消、重入、过期上下文、单次发送和记忆写入边界检查通过。');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
