/* 报表导入不经过模型：门店绑定、字段映射、整批校验、冻结预览后确认发布。 */
'use strict';
let importSchema=null,importStage=null,importPreview=null,importEpoch=0;
async function importPost(path,payload){return api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});}
function importNotice(text){$('import-notice').textContent=text;}
function invalidateImport(){importPreview=null;$('import-commit').disabled=true;$('import-commit').hidden=true;$('import-preview').replaceChildren();$('import-preview').hidden=true;}
async function loadImports(){
  const epoch=++importEpoch,container=$('imports-view');importStage=null;importPreview=null;
  container.replaceChildren();
  const heading=node('div','ad-heading'),intro=node('div');intro.append(node('h1','','数据导入'));heading.append(intro);container.append(heading);
  try{const schema=await api('/api/imports/schema');if(epoch!==importEpoch||activeModule!=='imports')return;importSchema=schema;
    const scope=node('p','import-scope',`当前导入门店：${storeDirectory.find(s=>s.id===currentStore)?.name||'本店'}。文件有门店列时会逐行核对，不会分配到其他店。`);container.append(scope);
    const form=node('section','import-panel'),row=node('div','import-upload-row');
    const kindLabel=node('label','','报表类型'),kind=node('select');kind.id='import-kind';
    for(const[key,label]of Object.entries(schema.kinds)){const option=node('option','',label);option.value=key;kind.append(option);}kindLabel.append(kind);
    const fileLabel=node('label','','选择后台导出文件'),file=node('input');file.id='import-file';file.type='file';file.accept='.csv,.xlsx';fileLabel.append(file);
    const sheetLabel=node('label','','Excel工作表序号'),sheet=node('input');sheet.id='import-sheet';sheet.type='number';sheet.min='1';sheet.value='1';sheetLabel.append(sheet);
    const headerLabel=node('label','','表头所在行'),headerRow=node('input');headerRow.type='number';headerRow.min='1';headerRow.max='30';headerRow.value='1';headerRow.id='import-header-row';headerLabel.append(headerRow);
    const upload=node('button','primary','读取文件');upload.type='button';upload.id='import-upload';row.append(kindLabel,fileLabel,sheetLabel,headerLabel,upload);form.append(row);
    const links=node('div','import-template-row');links.append(node('span','','预设模板（含一行示例，使用时请替换）：'));
    for(const fmt of ['csv','xlsx']){const button=node('button','text-button',`下载 ${fmt.toUpperCase()} 模板`);button.type='button';button.addEventListener('click',async()=>{
      try{const headers={};const token=sessionStorage.getItem('signal-access-token');if(token)headers.Authorization='Bearer '+token;headers['X-Store-ID']=currentStore;
        const response=await fetch(`/api/imports/templates/${kind.value}.${fmt}`,{headers});if(!response.ok)throw new Error('模板下载失败。');
        const url=URL.createObjectURL(await response.blob()),anchor=node('a');anchor.href=url;anchor.download=`huaji-${kind.value}-template.${fmt}`;anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
      }catch(error){importNotice(error.message);}});links.append(button);}form.append(links,node('p','account-note','CSV支持UTF-8/GB18030，Excel支持.xlsx。单次2MB、最多5000行；可按日期拆分。不会自动把未提供的指标补成0。'));
    const notice=node('p','ad-notice');notice.id='import-notice';notice.setAttribute('role','status');form.append(notice);container.append(form);
    const mapping=node('section','import-panel');mapping.id='import-mapping';mapping.hidden=true;container.append(mapping);
    const preview=node('section','import-panel');preview.id='import-preview';preview.hidden=true;container.append(preview);
    const commit=node('button','primary','确认导入当前门店');commit.id='import-commit';commit.disabled=true;commit.hidden=true;commit.type='button';container.append(commit);
    commit.addEventListener('click',async()=>{if(!importStage||!importPreview||epoch!==importEpoch)return;commit.disabled=true;
      try{const result=await importPost(`/api/imports/${importStage.id}/commit`,{confirm:true,preview_token:importPreview.preview_token});if(epoch!==importEpoch)return;
        importNotice(`已导入 ${result.imported_rows??importPreview.valid_rows} 行，新数据版本 ${result.version_id.slice(0,8)}。后续调查使用新版本，历史报告与已排队任务保持原快照。`);
        importStage=null;await loadMeta();await renderImportHistory(epoch);
      }catch(error){if(epoch===importEpoch)importNotice(error.message);}});
    const history=node('section','import-panel');history.id='import-history';container.append(history);
    kind.addEventListener('change',()=>{importStage=null;invalidateImport();mapping.hidden=true;});
    upload.addEventListener('click',async()=>{const selected=file.files[0];if(!selected)return importNotice('请选择报表文件。');if(selected.size>2000000)return importNotice('文件超过2MB，请按日期拆分。');upload.disabled=true;invalidateImport();
      try{const content=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=reject;reader.readAsDataURL(selected);});
        if(epoch!==importEpoch)return;
        const stage=await importPost('/api/imports/upload',{kind:kind.value,filename:selected.name,content_base64:content,sheet_index:Number(sheet.value)-1,header_row:Number(headerRow.value)});if(epoch!==importEpoch)return;
        importStage=stage;renderImportMapping(stage,epoch);importNotice(`已读取 ${stage.rows} 行${stage.sheets.length?'，工作表：'+stage.sheets[Number(sheet.value)-1]:''}。请核对列含义，点击校验预览。`);await renderImportHistory(epoch);
      }catch(error){if(epoch===importEpoch)importNotice(error.message);}finally{upload.disabled=false;}});
    await renderImportHistory(epoch);
  }catch(error){container.append(node('p','ad-notice',error.message));}
}
function renderImportMapping(stage,epoch){
  const panel=$('import-mapping');panel.hidden=false;panel.replaceChildren(node('h2','','匹配字段与商品'));
  panel.append(node('p','','金额按元、计数按整数；“支付买家数”不能用支付订单数代替。缺少必填指标时，请换用包含该指标的报表。'));
  const grid=node('div','import-field-grid');
  for(const field of importSchema.fields[stage.kind]){const label=node('label','',field.label+(field.required?' *':'')),select=node('select');select.dataset.importField=field.key;
    select.append(node('option','','不使用此列'));for(const header of stage.headers){const option=node('option','',header);option.value=header;select.append(option);}select.value=stage.mapping[field.key]||'';
    select.addEventListener('change',invalidateImport);label.append(select);grid.append(label);}panel.append(grid);
  const allowLabel=node('label','import-checkbox'),allow=node('input');allow.type='checkbox';allow.id='import-allow-new';allow.addEventListener('change',invalidateImport);
  allowLabel.append(allow,document.createTextNode('允许将文件中的新商品编码与名称加入本店目录（预览中列出后确认）'));panel.append(allowLabel);
  const cohortLabel=node('label','import-checkbox'),cohort=node('input');cohort.type='checkbox';cohort.addEventListener('change',invalidateImport);
  cohortLabel.append(cohort,document.createTextNode('确认本报表按支付日归组，取消/完成为同批支付订单在截止时间的状态。按取消发生日统计的日报不能直接代替。'));
  if(stage.kind==='order_cohorts')panel.append(cohortLabel);
  const unknown=node('div');unknown.id='import-product-map';panel.append(unknown);
  const button=node('button','primary','校验并预览');button.type='button';button.addEventListener('click',async()=>{button.disabled=true;invalidateImport();
    try{const mapping=Object.fromEntries([...grid.querySelectorAll('select')].filter(s=>s.value).map(s=>[s.dataset.importField,s.value]));
      const product_map=Object.fromEntries([...unknown.querySelectorAll('select')].filter(s=>s.value).map(s=>[s.dataset.externalProduct,s.value]));
      const result=await importPost(`/api/imports/${stage.id}/preview`,{mapping,product_map,allow_new_products:allow.checked,paid_cohort_confirmed:cohort.checked});if(epoch!==importEpoch)return;
      importPreview=result;renderImportPreview(result);$('import-commit').hidden=false;$('import-commit').disabled=!result.can_commit;
      // 保留运营者已选映射，只补充新出现的未匹配编码；不逐条填写商品数据。
      for(const name of [...new Set([...result.unmatched_products,...Object.keys(result.applied_product_map||{})])]){if([...unknown.querySelectorAll('select')].some(s=>s.dataset.externalProduct===name))continue;
        const label=node('label','',`文件商品“${name}”对应：`),select=node('select');select.dataset.externalProduct=name;select.append(node('option','','请选择本店商品'));
        for(const product of importSchema.products){const option=node('option','',`${product.name} · ${product.variant}`);option.value=product.sku_id;select.append(option);}select.value=result.applied_product_map?.[name]||'';select.addEventListener('change',invalidateImport);label.append(select);unknown.append(label);}
      importNotice(result.can_commit?'校验通过。请核对新增、覆盖数量及预览，再确认导入。':`发现 ${result.error_count} 行错误，整批尚未导入。修正映射或报表后重新校验。`);
    }catch(error){if(epoch===importEpoch)importNotice(error.message);}finally{button.disabled=false;}});panel.append(button);
}
function renderImportPreview(result){
  const panel=$('import-preview');panel.hidden=false;panel.replaceChildren(node('h2','','导入预览'),node('p','',`文件 ${result.total_rows} 行 · 有效 ${result.valid_rows} 行 · 新增 ${result.new_rows} 行 · 覆盖同商品同日期 ${result.replaced_rows} 行 · 新商品 ${result.new_products.length} 个`));
  for(const product of result.new_products)panel.append(node('p','',`新增商品：${product.sku_id} · ${product.name}`));
  for(const error of result.errors)panel.append(node('p','import-error',`第 ${error.line} 行：${error.message}`));
  const wrap=node('div','table-wrap'),table=node('table'),thead=node('thead'),body=node('tbody');const headers=['商品编码','日期','字段值'];const tr=node('tr');for(const header of headers)tr.append(node('th','',header));thead.append(tr);
  for(const row of result.sample){const item=node('tr');item.append(node('td','',row.sku_id),node('td','',row.date),node('td','',Object.entries(row).filter(([key])=>!['sku_id','date','source_id','observed_at','status','cohort_policy'].includes(key)).map(([key,value])=>`${importSchema.fields[importStage.kind].find(f=>f.key===key)?.label||key}: ${typeof value==='object'?JSON.stringify(value):value}`).join(' · ')));body.append(item);}
  table.append(thead,body);wrap.append(table);panel.append(wrap,node('p','account-note','这里只发布当前门店的数据版本。销售、库存、流量、活动、订单批次按来源分别导入；不会自动生成缺少的历史指标，也不会重算推广小时账本。'));
}
async function renderImportHistory(epoch){const response=await api('/api/imports');if(epoch!==importEpoch)return;const panel=$('import-history');panel.replaceChildren(node('h2','','本店导入记录'));
  for(const batch of response.imports){const item=node('article','import-history-item');item.append(node('strong','',batch.filename),node('p','',`${importSchema.kinds[batch.kind]} · ${batch.status==='committed'?'已发布版本 '+batch.version_id.slice(0,8):batch.status==='cancelled'?'已取消':'待处理'} · ${batch.created_at}`));
    if(batch.status==='staged'){const cancel=node('button','text-button','取消此暂存文件');cancel.type='button';cancel.addEventListener('click',async()=>{await importPost(`/api/imports/${batch.id}/cancel`,{});if(importStage?.id===batch.id){importStage=null;invalidateImport();$('import-mapping').hidden=true;}await renderImportHistory(epoch);});item.append(cancel);}panel.append(item);}
  if(!response.imports.length)panel.append(node('p','','本店尚未导入报表。'));
}
$('nav-imports').addEventListener('click',()=>showModule('imports'));
