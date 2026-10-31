/* 用实际前端函数验证恢复动作：不自动发送请求，不把供应商鉴权当工作区登录。 */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../ecommerce_agent/web_assets/app.js'), 'utf8');
const code = source.slice(source.indexOf('function renderRecoveryActions('), source.indexOf('function render(task)'));
function element(){return {children:[],listeners:{},append(...items){this.children.push(...items)},addEventListener(name,fn){this.listeners[name]=fn}};}
const input={value:'',focus(){this.focused=true}};
const context={conversationBusy:false,pendingConfirmation:{sku:'old'},currentTask:null,meta:{available_dates:['2026-01-01','2026-10-28']},
  node:()=>element(),$:()=>input,notify(message){context.notice=message},loadModelSettings(){context.settingsOpened=true},switchTab(tab){context.tab=tab}};
vm.createContext(context);vm.runInContext(code,context);
const container=element();
context.renderRecoveryActions(container,{recovery_actions:[{type:'retry',label:'重试'},{type:'open_settings',label:'检查设置'},
  {type:'view_sources',label:'查看来源'},{type:'execute_tool',label:'非法动作'}]},'原始问题');
const buttons=container.children[0].children;
assert.equal(buttons.length,3);
buttons[0].listeners.click();
assert.equal(input.value,'原始问题');assert.equal(context.pendingConfirmation,null);assert.match(context.notice,/发送/);
buttons[1].listeners.click();assert.equal(context.settingsOpened,true);
buttons[2].listeners.click();assert.match(context.notice,/数据覆盖/);
context.currentTask={result:{}};buttons[2].listeners.click();assert.equal(context.tab,'evidence');
context.conversationBusy=true;input.value='正在输入';buttons[0].listeners.click();assert.equal(input.value,'正在输入');
console.log('错误恢复动作：6项检查通过，无自动模型请求。');
