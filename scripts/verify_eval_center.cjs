/* 本机真实浏览器验收：离线评测、逐题明细、运行对比、主题和窄屏。 */
const {createRequire}=require('node:module'),assert=require('node:assert/strict');
const {chromium}=createRequire('C:/Users/凳子/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/package.json')('playwright');
(async()=>{const browser=await chromium.launch({headless:true,channel:'msedge'});try{
 const page=await browser.newPage({viewport:{width:1500,height:1050}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto((process.env.EVAL_BASE_URL||'http://127.0.0.1:8081')+'/eval');await page.waitForFunction(()=>dataset?.cases.length>0);
 const datasetSize=await page.evaluate(()=>dataset.cases.length);
 await page.locator('[data-view=dataset]').click();assert.equal(await page.locator('#dataset-list details').count(),datasetSize);
 await page.locator('#dataset-search').fill('sales-single');assert(await page.locator('#dataset-list details').count()>0);
 await page.locator('[data-view=runs]').click();
 const ids=[];
 for(const label of ['离线基线验收','同版本重复验收']){
  const previous=await page.evaluate(()=>active?.id);
  await page.locator('#run-label').fill(label);await page.locator('#run-start').click();
  await page.waitForFunction(({label,previous})=>active?.id!==previous&&active?.label===label&&['completed','failed','cancelled'].includes(active.status),{label,previous},{timeout:180000});
  const job=await page.evaluate(()=>active);assert.equal(job.status,'completed',job.message);assert(job.cases.length>0);assert.equal(job.cases.length,job.total);assert.equal(job.model_calls,0);ids.push(job.id);
 }
 await page.locator('#case-rows button').first().click();await page.locator('#case-dialog').waitFor({state:'visible'});assert((await page.locator('#case-expected').innerText()).length>5);await page.locator('#case-close').click();
 await page.locator('#case-filter').selectOption('failed');const failures=await page.locator('#case-rows button').count();
 if(failures){await page.locator('#case-rows button').first().click();await page.locator('#review-status').selectOption('needs_review');await page.locator('#review-note').fill('浏览器验收：已记录实际与预期差异，待单独定位。');await page.locator('#review-form button[type=submit]').click();await page.waitForFunction(()=>!document.getElementById('case-dialog').open);}
 await page.locator('[data-view=compare]').click();await page.locator('#compare-left').selectOption(ids[0]);await page.locator('#compare-right').selectOption(ids[1]);await page.locator('#compare-form button').click();await page.locator('#comparison-result table').waitFor();assert.match(await page.locator('#comparison-result').innerText(),/源码指纹一致/);
 await page.screenshot({path:'runs/eval-center-compare.png',fullPage:true});await page.locator('[data-view=runs]').click();await page.locator('#theme-toggle').click();await page.screenshot({path:'runs/eval-center-dark.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'窄屏不应整页横向溢出');await page.screenshot({path:'runs/eval-center-mobile.png',fullPage:true});
 assert.deepEqual(errors,[]);console.log(JSON.stringify({passed:true,jobs:ids,failed_cases:failures,claw_status:await page.evaluate(()=>active.claw.status),summary:await page.evaluate(()=>({executed:active.summary.executed,passed:active.summary.passed,failed:active.summary.failed,skipped:active.summary.skipped}))},null,2));
 }finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1});
