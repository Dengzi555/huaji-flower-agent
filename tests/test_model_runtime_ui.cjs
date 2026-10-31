/* 使用真实渲染函数检查未知用量与估算预算没有混成实际账单。 */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(require('node:path').join(__dirname,'../ecommerce_agent/web_assets/app.js'),'utf8');
function node(tag,style='',text=''){return {tag,text,children:[],append(...children){this.children.push(...children)}};}
const context={node};vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function renderModelRuntime('),source.indexOf('function node(')),context);
const root=node('div'),usage={calls:2,reported:{prompt_tokens:null,completion_tokens:null,total_tokens:null},
  unknown_usage_calls:1,known_totals:{total_tokens:12},reserved_units:2400,budget:{max_calls:16,max_estimated_tokens:240000},
  records:[{sequence:2,purpose:'chat',model:'local',status:'cancelled',usage:{total_tokens:null},context:{removed_messages:2}}]};
context.renderModelRuntime(root,usage,'本轮');
const text=root.children[0].children.map(child=>child.text).join('\n');
assert.match(text,/总计 未知 tokens/);assert.match(text,/已知部分 12 tokens/);assert.match(text,/本地估算，非计费用量/);
assert.match(text,/已停止/);assert.match(text,/裁剪 2 条/);assert.equal(root.children[0].tag,'details');
context.renderModelRuntime(root,null);assert.equal(root.children.length,1);
console.log('模型用量展示：7项检查通过。');
