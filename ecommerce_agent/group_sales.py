"""花材集合销量工具：冻结成员后逐款取数，保留缺口，不相加跨商品UV、买家或订单数。"""
from datetime import date,timedelta
from .sales_trend import sales_trend
from .business_calendar import calendar_range,day_context


def grouped_items(items,catalog,dimension):
    """按目录字段聚合SKU；一类中有任一成员缺失，就不把该类的部分销量当完整总计排名。"""
    if dimension=='sku':return items
    groups={}
    for item in items:
        label=catalog[item['sku_id']].get(dimension) or '未分类'
        groups.setdefault(label,[]).append(item)
    output=[]
    for label,members in groups.items():
        inactive=[row['sku_id'] for row in members if row['requested_days']==0]
        members=[row for row in members if row['requested_days']>0]
        if not members:continue
        complete=label!='未分类' and all(row['units_sold'] is not None and row['available_days']==row['requested_days'] for row in members)
        output.append({'sku_id':dimension+':'+label,'name':label,'variant':f'{len(members)}款商品合计',
            'member_skus':[row['sku_id'] for row in members],
            'inactive_member_skus':inactive,
            'available_days':sum(row['available_days'] for row in members),'requested_days':sum(row['requested_days'] for row in members),
            'units_sold':sum(row['units_sold'] for row in members) if complete else None,
            'paid_revenue':round(sum(row['paid_revenue'] for row in members),2) if complete else None})
    return output


def group_sales(store,scope,start,end,comparison,products):
    """输出逐款明细与完整覆盖日期的销量/商品金额汇总；部分缺失时总计保持未知。"""
    ids=scope['sku_ids']
    from .capabilities import LIMITS
    if not isinstance(ids,list) or not 1<=len(ids)<=LIMITS['max_products'] or len(set(ids))!=len(ids) or any(sku not in store.skus for sku in ids):
        raise ValueError(f"集合成员需为本店1–{LIMITS['max_products']}个不同的有效商品。")
    catalog={p['sku_id']:p for p in products}
    series={sku:sales_trend(store,sku,start,end,comparison,catalog.get(sku,{}).get('listed_at'),catalog.get(sku,{}).get('retired_at')) for sku in ids}
    by_sku={sku:{r['date']:r for r in trend['records']} for sku,trend in series.items()}
    rows=[];raw=[]
    for source in next(iter(series.values()))['records']:
        day=source['date'];members=[by_sku[sku][day] for sku in ids]
        # 未上架不补为零；历史目录是否覆盖当时全部在售商品也未验证。
        # 目录证明未在售的成员不当作数据丢失；只汇总当天实际在售且有完整记录的成员。
        active=[row for row in members if row['status']=='ok']
        complete=bool(active) and all(row['status'] in ('ok','not_listed','retired') for row in members)
        record={'date':day,'status':'ok' if complete else 'unavailable',
            'units_sold':sum(row['units_sold'] for row in active) if complete else None,
            'paid_revenue':round(sum(row['paid_revenue'] for row in active),2) if complete else None,
            'paid_orders':None,'paid_buyers':None,'visitors':None,'stockout_minutes':None,
            'campaign_active':None,'campaign_name':None}
        rows.append(record)
        for sku in ids:raw.append(dict(by_sku[sku][day]['records']['sales']))
    daily={r['date']:r for r in rows};target=daily[end];baselines=[]
    baseline_days=[(label,(date.fromisoformat(end)-timedelta(days=offset)).isoformat()) for label,offset in [('前一日',1),('前两日',2),('上周同日',7)]]
    if comparison and comparison not in {day for _,day in baseline_days}:baseline_days.append(('指定基线',comparison))
    for label,day in baseline_days:
        before=daily.get(day)
        valid=before and before['status']=='ok' and target['status']=='ok'
        value=before['units_sold'] if valid else None
        baselines.append({'label':label,'date':day,'target_date':end,'is_primary':day==comparison,
            'baseline_units':value,'target_units':target['units_sold'],
            'relative_pct':round((target['units_sold']-value)/value*100,2) if valid and value else None,
            'absolute_change':target['units_sold']-value if valid else None,'zero_baseline':bool(valid and value==0),
            'status':'ok' if valid else 'insufficient_data','context_flags':['商品组合的活动与库存需逐款核查'],
            'baseline_calendar':day_context(day),'target_calendar':day_context(end)})
    visible=[r for r in rows if start<=r['date']<=end];available=[r for r in visible if r['status']=='ok']
    trend={'table':'sales_trend','sku_id':'__all_matched__','start_date':start,'end_date':end,'records':rows,'comparisons':baselines,
        'coverage':{'available_days':len(available),'requested_days':len(visible),'missing_days':[r['date'] for r in visible if r['status']!='ok']},
        'summary':{'units_sum':sum(r['units_sold'] for r in available) if available else None,
            'revenue_sum':round(sum(r['paid_revenue'] for r in available),2) if available else None,'complete_window':len(available)==len(visible)}}
    items=[]
    for sku in ids:
        selected=[r for r in series[sku]['records'] if scope.get('summary_start',start)<=r['date']<=end and r['status'] not in ('not_listed','retired')];ok=[r for r in selected if r['status']=='ok']
        items.append({'sku_id':sku,'name':catalog.get(sku,{}).get('short_name',sku),'variant':catalog.get(sku,{}).get('variant',''),
            'available_days':len(ok),'requested_days':len(selected),'units_sold':sum(r['units_sold'] for r in ok) if ok else None,
            'paid_revenue':round(sum(r['paid_revenue'] for r in ok),2) if ok else None})
    ranking=scope.get('ranking');ranking_result=None
    if ranking:
        dimension=ranking.get('group_by','sku')
        if dimension not in ('sku','variety','product_type','series'):raise ValueError('不支持的类别汇总维度。')
        inactive=[item['sku_id'] for item in items if item['requested_days']==0]
        # 目录证明当时未上架/已下架的商品不应被当成历史数据缺失，也不补零参与排名。
        # 在售但缺记录的成员仍保留，必须使complete_scope=False。
        active_items=[item for item in items if item['requested_days']>0]
        rank_items=grouped_items(active_items,catalog,dimension)
        eligible=[item for item in rank_items if item['available_days']==item['requested_days'] and item['units_sold'] is not None]
        requested_dates=[row['date'] for row in rows if scope.get('summary_start',start)<=row['date']<=end]
        effective_dates=requested_dates;period_scope='requested_window'
        if not eligible and len(requested_dates)>1:
            # 所有商品都缺同一天时仍可给参考答案：仅比较同一组共同可用日，不把缺失补零。
            # 新品不和整窗老品拼凑不同曝光天数；存在老品时，先限于窗口起点前已上架的商品。
            cohort=[sku for sku in ids if not catalog.get(sku,{}).get('listed_at') or catalog[sku]['listed_at']<=requested_dates[0]] or ids
            effective_dates=[day for day in requested_dates if all(by_sku[sku][day]['status']=='ok' for sku in cohort)]
            if effective_dates:
                covered=[]
                for item in active_items:
                    valid=item['sku_id'] in cohort;selected=[by_sku[item['sku_id']][day] for day in effective_dates] if valid else []
                    covered.append({**item,'available_days':len(selected),'requested_days':len(effective_dates),
                        'requested_window_days':len(requested_dates),
                        'units_sold':sum(row['units_sold'] for row in selected) if selected else None,
                        'paid_revenue':round(sum(row['paid_revenue'] for row in selected),2) if selected else None})
                rank_items=grouped_items(covered,catalog,dimension)
                eligible=[item for item in rank_items if item['available_days']==item['requested_days'] and item['units_sold'] is not None]
                period_scope='shared_available_dates'
        partitions=[]
        for direction in (['desc','asc'] if ranking['direction']=='both' else [ranking['direction']]):
            ordered=sorted(eligible,key=lambda item:(item['units_sold'] if direction=='asc' else -item['units_sold'],item['sku_id']))
            # 零销量可排名，缺失不可；两端各自保留所有并列项。
            leaders=[item for item in ordered if item['units_sold']==ordered[0]['units_sold']] if ordered else []
            partitions.append({'direction':direction,'rows':ordered[:ranking['limit']],'leaders':leaders})
        all_leaders=list({item['sku_id']:item for part in partitions for item in part['leaders']}.values())
        ranking_result={**ranking,'rows':partitions[0]['rows'],'leaders':all_leaders,'partitions':partitions,'eligible_count':len(eligible),
            'period_scope':period_scope,'effective_dates':effective_dates,'requested_dates':requested_dates,
            'coverage_note':f'本次排名仅基于{len(effective_dates)}/{len(requested_dates)}个共同可用日期，优先比较窗口起点前已上架商品；不是完整期间全店结论。' if period_scope!='requested_window' else '',
            'complete_scope':bool(rank_items) and period_scope=='requested_window' and len(eligible)==len(rank_items),
            'candidate_count':len(rank_items),'inactive_skus':inactive,
            'excluded':[item['sku_id'] for item in rank_items if item not in eligible]}
    selected_comparison=None;selected_trends=[]
    if scope.get('compare_selected') and ranking_result:
        # 先在目标日/窗口排名，再固定这些SKU比较，不能用“基线日自己的第一名”偷换比较对象。
        comparisons=[]
        for item in ranking_result['leaders']:
            sku=item['sku_id']
            if item.get('member_skus'):
                # 类别胜出后固定其成员，再取该类别的历史总量；递归子查询没有排名，不会再次展开。
                selected_scope={'sku_ids':item['member_skus'],'summary_start':scope.get('summary_start',start)}
                trend_item=group_sales(store,selected_scope,start,end,comparison,products)['trend']
            else:trend_item=series[sku]
            baseline=next((row for row in trend_item['comparisons'] if row['date']==comparison),None)
            if baseline:
                comparisons.append({**item,**baseline,'name':item['name'],'sku_id':sku})
            if len(selected_trends)<3:selected_trends.append({**trend_item,'display_label':item['name']+' · '+item['variant']})
        selected_comparison={'selection':'all_tied_leaders','baseline_date':comparison,'rows':comparisons,
            'complete_scope':ranking_result['complete_scope'],
            'scope_note':'比较目标日入选的同一商品或固定成员类别；窗口查询的对比针对窗口末日，不代表整段窗口环比。',
            'chart_note':'并列商品全部列入对比表，最多展示前三款独立曲线。'}
    return {'table':'product_group_sales','sku_id':'__all_matched__','target_date':end,'scope':scope,'records':raw,'items':items,'trend':trend,
        'ranking':ranking_result,
        'selected_comparison':selected_comparison,'selected_trends':selected_trends,
        'calendar':calendar_range(rows[0]['date'],end),
        'limitations':['范围是本轮目录匹配到的商品，不保证包含节日当时全部历史在售商品。',
            '汇总仅覆盖全部成员数据齐全的日期；逐款明细可显示已取回部分，缺失不补零。',
            '跨商品访客、买家和订单可能重复，未去重时不提供品类独立UV、订单数或转化率。',
            '商品金额汇总要求来源按SKU分摊，不能把同一订单总额重复计入多款商品。']}
