/* 从实际源码提取键盘处理器，验证回车、换行、输入法与重复提交；不复制实现。 */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(require('node:path').join(__dirname,'../ecommerce_agent/web_assets/app.js'),'utf8').replace(/\r\n/g,'\n');
const definition=source.match(/function bindComposerKeyboard\([\s\S]*?\n}\nbindComposerKeyboard/)[0].replace(/\nbindComposerKeyboard$/,'');
let now=1000;
function setup(){const events={},input={value:'你好',addEventListener:(name,fn)=>events[name]=fn},send={disabled:false};let busy=false,count=0;
  const bind=vm.runInNewContext('('+definition+')',{Date:{now:()=>now}});bind(input,{requestSubmit:()=>count++},send,()=>busy);
  const press=extra=>{let prevented=false;events.keydown({key:'Enter',preventDefault:()=>{prevented=true;},...extra});return prevented;};
  return {events,input,send,press,setBusy:value=>busy=value,count:()=>count};}
let t=setup();assert.equal(t.press(),true);assert.equal(t.count(),1);
t=setup();assert.equal(t.press({shiftKey:true}),false);assert.equal(t.count(),0);
t=setup();t.events.compositionstart();t.press();assert.equal(t.count(),0);t.events.compositionend();t.press();assert.equal(t.count(),0);now+=60;t.press();assert.equal(t.count(),1);
for(const extra of [{isComposing:true},{keyCode:229},{repeat:true},{ctrlKey:true},{metaKey:true},{altKey:true},{key:'a'}]){t=setup();t.press(extra);assert.equal(t.count(),0);}
t=setup();t.input.value='  ';t.press();assert.equal(t.count(),0);
t=setup();t.setBusy(true);t.press();assert.equal(t.count(),0);
t=setup();t.send.disabled=true;t.press();assert.equal(t.count(),0);
console.log('PASS: Enter/Shift+Enter, Chinese IME, empty draft, modifier keys and duplicate-send guards');
