const {createRequire}=require('node:module');
const assert=require('node:assert/strict');
// 本机可用 PLAYWRIGHT_PACKAGE 指定已有安装；常规环境直接使用项目依赖。
const req=process.env.PLAYWRIGHT_PACKAGE?createRequire(process.env.PLAYWRIGHT_PACKAGE):require;
const {chromium}=req('playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 try{
  for(const width of [1440,900,390]){
   const page=await browser.newPage({viewport:{width,height:900}});
   await page.goto(process.env.WORKBENCH_URL||'http://127.0.0.1:8080/');
   await page.waitForFunction(()=>typeof meta!=='undefined'&&meta);
   await page.waitForLoadState('networkidle');
   assert.equal(await page.locator('.workspace-nav button').first().getAttribute('id'),'nav-investigation');
   await page.waitForFunction(()=>activeModule==='investigation');
   const box=()=>page.locator('.composer-area').boundingBox();
   const before=await box();assert(before.y+before.height<=901);
   // 用长回答验证真实布局，不调用收费模型，也不写业务数据。
   await page.evaluate(()=>{const panel=document.querySelector('#store-comparison');panel.hidden=false;panel.innerHTML='<p>长回答布局检查</p>'.repeat(150);});
   await page.locator('.content').evaluate(el=>el.scrollTop=el.scrollHeight);
   const after=await box();assert(Math.abs(before.y-after.y)<1,'结果滚动不应移动输入区');
   assert(await page.locator('.content').evaluate(el=>el.scrollTop>0));
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'不应横向溢出');
   await page.locator('#toggle-inspector').click();
   const inspector=await page.locator('#inspector').boundingBox(),dock=await box();
   assert(dock.y+dock.height<=901);assert(inspector.x+inspector.width<=dock.x+1||inspector.x>=dock.x+dock.width-1||inspector.y+inspector.height<=dock.y+1,'详情不覆盖输入框');
   await page.locator('#close-inspector').click();
   await page.locator('#theme-toggle').click();
   await page.screenshot({path:`runs/chat-layout-${width}.png`});
   // 切换模块后恢复正常页面滚动，再回到调查页。
   await page.locator('#nav-overview').evaluate(el=>el.click());
   await page.waitForFunction(()=>activeModule==='overview');
   assert.equal(await page.evaluate(()=>document.body.classList.contains('investigation-active')),false);
   await page.locator('#nav-investigation').evaluate(el=>el.click());
   await page.waitForFunction(()=>activeModule==='investigation');
   console.log(`PASS ${width}px: default, scrolling, dock, inspector, theme, navigation`);
   await page.close();
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
