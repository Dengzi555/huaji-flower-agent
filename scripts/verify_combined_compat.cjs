/* 直接提取前端实际函数，验证新任务、旧任务和不完整任务的组合事项读取。 */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(require('node:path').join(__dirname,'../ecommerce_agent/web_assets/app.js'),'utf8').replace(/\r\n/g,'\n');
const match=source.match(/function combinedOperations\(parsed\)\{[\s\S]*?\n}/);
assert(match,'组合范围视图需兼容旧任务协议');
const read=vm.runInNewContext('('+match[0]+')');
const value=p=>JSON.parse(JSON.stringify(read(p)));
assert.deepEqual(value({composite_operations:['sales','inventory','promotions']}),['sales','inventory','promotions']);
assert.deepEqual(value({routing:{operations:['sales','inventory','promotions']}}),['sales','inventory','promotions']);
for(const parsed of [undefined,{}, {composite_operations:null},{composite_operations:{}},{routing:{operations:'inventory'}}])assert.deepEqual(value(parsed),[]);
assert.deepEqual(value({composite_operations:['sales','sales','not-authorized']}),['sales']);
console.log('PASS: 新旧组合任务、缺字段、错误类型、重复及未知事项兼容');
