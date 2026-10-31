/* 回答后的多日对比图：只读本次任务冻结的工具结果；平滑仅用于连线，缺口不连线。 */
"use strict";
function renderSalesTrend(container,task){
  const trend=task.result.sales_trend,calendar=new Map((task.result.calendar_context?.records||[]).map(r=>[r.date,r]));
  const make=(tag,className='',text='')=>{const el=document.createElement(tag);el.className=className;el.textContent=text;return el;};
  const section=make('section','sales-trend-section');
  section.append(make('p','trend-eyebrow','SALES / COMPARISON'),make('h2','',`${trend.display_label?trend.display_label+' · ':''}${trend.start_date} — ${trend.end_date} · 销量变化`),
    make('p','trend-coverage',`可用 ${trend.coverage.available_days}/${trend.coverage.requested_days} 天 · 单位：成品束/桶 · 缺失和未上架为断点，零销量为0`));
  const cards=make('div','baseline-cards');
  section.append(make('h3','trend-subtitle',`末日对比 · 目标日 ${trend.end_date}`));
  for(const item of trend.comparisons){const card=make('article',`baseline-card${item.is_primary?' primary-baseline':''}`);
    const change=item.relative_pct===null?item.zero_baseline?'基线为0，不计算降幅':'基线或目标数据不足':`${item.relative_pct>0?'+':''}${item.relative_pct}%`;
    card.append(make('span','',`${item.label} · ${item.date}${item.is_primary?' · 主基线':''}`),make('strong','',change),
      make('p','',`${item.baseline_units??'—'} → ${item.target_units??'—'} 束/桶`),make('small','',item.context_flags.join(' · ')||'暂无已知日历/活动差异'));
    cards.append(card);
  }
  section.append(cards);
  const controls=make('div','trend-controls'),choices=[['本期',0,'current'],['前一日',1,'previous'],['前两日',2,'two-before'],['上周同日',7,'week-before']],selected=new Set([0,7]);
  const chart=make('div','sales-trend-chart');
  const shift=(value,offset)=>{const d=new Date(value+'T00:00:00Z');d.setUTCDate(d.getUTCDate()+offset);return d.toISOString().slice(0,10);};
  const dates=[];for(let day=trend.start_date;day<=trend.end_date;day=shift(day,1))dates.push(day);
  const byDate=new Map(trend.records.map(r=>[r.date,r]));
  function draw(){
    chart.replaceChildren();
    const ns='http://www.w3.org/2000/svg',svgNode=(tag,attrs={},text='')=>{const n=document.createElementNS(ns,tag);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,String(v));n.textContent=text;return n;};
    const svg=svgNode('svg',{viewBox:'0 0 1000 310',role:'img','aria-label':'多日销量平滑曲线，悬停显示日期、销量、日历和活动'});
    const left=58,right=978,top=25,bottom=252,x=i=>dates.length===1?(left+right)/2:left+i/(dates.length-1)*(right-left);
    const currentChoices=choices.filter(c=>selected.has(c[1]));
    const value=(day,offset)=>{const row=byDate.get(shift(day,-offset));return row?.status==='ok'?row.units_sold:null;};
    const peak=Math.max(4,Math.ceil(Math.max(0,...currentChoices.flatMap(c=>dates.map(d=>value(d,c[1])??0)))/4)*4),y=v=>bottom-v/peak*(bottom-top);
    for(let i=0;i<=4;i++)svg.append(svgNode('line',{x1:left,x2:right,y1:y(peak*i/4),y2:y(peak*i/4),class:'trend-grid'}),svgNode('text',{x:left-10,y:y(peak*i/4)+4,'text-anchor':'end',class:'trend-label'},String(peak*i/4)));
    dates.forEach((day,i)=>{if(i%Math.ceil(dates.length/8)===0||i===dates.length-1)svg.append(svgNode('text',{x:x(i),y:278,'text-anchor':'middle',class:'trend-label'},day.slice(5)));
      const cal=calendar.get(day);if(cal?.day_type==='holiday_break'||cal?.is_makeup_workday)svg.append(svgNode('circle',{cx:x(i),cy:294,r:3,class:cal.is_makeup_workday?'calendar-makeup':'calendar-holiday'}));
    });
    for(const [label,offset,color] of currentChoices){let points=[];
      const flush=()=>{if(points.length>1)svg.append(svgNode('path',{d:smoothCurve(points),fill:'none',class:`trend-line ${color}`}));else if(points.length===1)svg.append(svgNode('circle',{cx:points[0][0],cy:points[0][1],r:4,class:`trend-point ${color}`}));points=[];};
      dates.forEach((day,i)=>{const v=value(day,offset);if(v===null)flush();else points.push([x(i),y(v)]);});flush();
    }
    const cross=svgNode('line',{y1:top,y2:bottom,class:'curve-crosshair',visibility:'hidden'});svg.append(cross);
    const tip=make('div','sales-trend-tooltip');tip.hidden=true;chart.append(svg,tip);
    const update=event=>{const box=svg.getBoundingClientRect(),vx=(event.clientX-box.left)/box.width*1000;
      const i=dates.length===1?0:Math.max(0,Math.min(dates.length-1,Math.round((vx-left)/(right-left)*(dates.length-1)))),day=dates[i],record=byDate.get(day),cal=calendar.get(day);
      tip.replaceChildren(make('strong','',`${day} ${cal?.weekday_label||''}`),make('small','',cal?.label||'日历安排未知'));
      for(const [label,offset]of currentChoices){const v=value(day,offset),line=make('p');line.append(make('span','',`${label}${offset?' '+shift(day,-offset):''}`),make('b','',v===null?'缺失/未上架':`${v} 束/桶`));tip.append(line);}
      tip.append(make('p','',`访客 ${record?.visitors??'—'} · 缺货 ${record?.stockout_minutes??'—'} 分钟`),
        make('p','',record?.campaign_active===null||record?.campaign_active===undefined?'活动数据缺失':record.campaign_active?(record.campaign_name||'活动开启'):'未开启活动'));
      if(cal?.occasions.length)tip.append(make('p','',cal.occasions.join(' · ')));
      cross.setAttribute('x1',x(i));cross.setAttribute('x2',x(i));cross.setAttribute('visibility','visible');
      tip.hidden=false;tip.style.left=`${Math.max(0,Math.min(chart.clientWidth-260,event.clientX-chart.getBoundingClientRect().left+14))}px`;tip.style.top='12px';
    };
    svg.addEventListener('pointermove',update);svg.addEventListener('pointerdown',update);svg.addEventListener('pointerleave',()=>{tip.hidden=true;cross.setAttribute('visibility','hidden');});
  }
  for(const [label,offset,color]of choices){const item=make('label',`trend-choice ${color}`),input=document.createElement('input');input.type='checkbox';input.checked=selected.has(offset);input.addEventListener('change',()=>{input.checked?selected.add(offset):selected.delete(offset);draw();});item.append(input,document.createTextNode(label));controls.append(item);}
  section.append(controls,chart,make('p','trend-legend','日期轴展示本期；对比线按前一日、前两日或上周同日对齐。金点为假期，绿点为调休工作日。'));
  for(const note of task.result.analysis?.comparison_notes||[])section.append(make('p','trend-context-note',note));
  const tableDetails=make('details','trend-daily-details');tableDetails.append(make('summary','','查看每日销量、日历与活动记录'));
  const table=make('table'),head=make('tr');for(const title of ['日期','日历','销量','访客','活动','数据状态'])head.append(make('th','',title));table.append(head);
  for(const day of dates){const r=byDate.get(day),cal=calendar.get(day),tr=make('tr');for(const v of [day,cal?.label||'年度安排未知',r?.units_sold??'—',r?.visitors??'—',r?.campaign_active===true?r.campaign_name:r?.campaign_active===false?'未开启':'缺失',r?.status||'缺失'])tr.append(make('td','',String(v)));table.append(tr);}
  tableDetails.append(table);section.append(tableDetails,make('p','trend-source','日历：国务院办公厅2026年放假调休安排；营销节点单独标记。趋势与日历工具原始返回均保存在本轮证据中。'));
  container.append(section);draw();
}
