"""按门店隔离的经营背景记忆：候选→人工确认→有界检索→任务快照。

不从模型回答自动学习，不将旧指标当作今日事实。新表与历史StoreMemory分开，
旧备注不会未经确认自动迁入。当前是结构化过滤/排序，不冒充向量语义检索。

冲突治理只覆盖「范围遮蔽」：同一事项的门店级与商品级记忆同时有效时，检索会
取商品级而静默忽略门店级。这不等于内容矛盾，本模块也没有语义判断能力，因此
只登记为待裁决的覆盖关系并如实披露，由人工决定保留哪条，绝不用最新覆盖最旧。
"""
import hashlib
import json
import uuid
from datetime import date,timedelta
from .clock import utc_now
from .missions import text,MissionConflict,MissionNotFound
from .routing import ROUTES
from .task_parser import shanghai_today

SLOTS={
    'night_ads_strategy':('夜间投放策略',{'investigate_sales_change'}),
    'budget_policy':('推广费用约定',{'investigate_sales_change'}),
    'response_preference':('汇报偏好',set(ROUTES)),
    'product_context':('商品经营背景',set(ROUTES)),
    'period_observation':('阶段观察',{'investigate_sales_change','query_sales_performance'}),
}
SCHEMA="""
CREATE TABLE IF NOT EXISTS context_memories(
 id TEXT PRIMARY KEY,slot TEXT NOT NULL,sku_id TEXT NOT NULL,content TEXT NOT NULL,
 source TEXT NOT NULL,source_task_id TEXT REFERENCES tasks(id),valid_from TEXT NOT NULL,
 expires_on TEXT NOT NULL,status TEXT NOT NULL,revision INTEGER NOT NULL,
 base_id TEXT,base_revision INTEGER,created_at TEXT NOT NULL,confirmed_at TEXT,
 fingerprint TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS context_active_slot ON context_memories(slot,sku_id) WHERE status='active';
CREATE INDEX IF NOT EXISTS context_retrieval ON context_memories(status,sku_id,slot,expires_on);
CREATE TABLE IF NOT EXISTS context_memory_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT,memory_id TEXT NOT NULL REFERENCES context_memories(id),
 created_at TEXT NOT NULL,type TEXT NOT NULL,payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS context_retrievals(
 task_id TEXT PRIMARY KEY REFERENCES tasks(id),created_at TEXT NOT NULL,receipt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_conflicts(
 id TEXT PRIMARY KEY,slot TEXT NOT NULL,sku_id TEXT NOT NULL,
 general_id TEXT NOT NULL REFERENCES context_memories(id),
 specific_id TEXT NOT NULL REFERENCES context_memories(id),
 status TEXT NOT NULL,resolution TEXT,note TEXT,
 created_at TEXT NOT NULL,resolved_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS memory_conflict_pair ON memory_conflicts(general_id,specific_id);
CREATE INDEX IF NOT EXISTS memory_conflict_open ON memory_conflicts(status,slot);
"""


class ContextMemory:
    """只操作所属service的数据库；客户端和模型不能用参数切换到别店。"""
    def __init__(self,service):self.service=service

    def event(self,db,mid,kind,**payload):
        """只追加事件，确认/撤回/替换不覆盖旧内容。"""
        db.execute('INSERT INTO context_memory_events(memory_id,created_at,type,payload) VALUES(?,?,?,?)',
                   (mid,utc_now(),kind,json.dumps(payload,ensure_ascii=False)))

    def checked(self,db,mid,revision=None):
        """ID在本店不存在返回404；乐观版本拒绝旧页面并发覆盖。"""
        row=db.execute('SELECT * FROM context_memories WHERE id=?',(mid,)).fetchone()
        if not row:raise MissionNotFound('当前门店没有这条经营背景。')
        if revision is not None and (type(revision) is not int or row['revision']!=revision):raise MissionConflict('经营背景版本已变化，请刷新。')
        return dict(row)

    def list(self):
        """管理列表包含待确认及历史项，过期为派生状态；返回范围仅限当前门店。"""
        today=shanghai_today().isoformat()
        with self.service.connection() as db:
            self._sweep(db,today)
            rows=[dict(r) for r in db.execute("SELECT * FROM context_memories ORDER BY CASE WHEN status IN ('active','proposed') THEN 0 ELSE 1 END,created_at DESC LIMIT 200")]
            shadowed={row['mid'] for row in db.execute("SELECT general_id AS mid FROM memory_conflicts WHERE status='pending' UNION SELECT specific_id AS mid FROM memory_conflicts WHERE status='pending'")}
        for row in rows:
            row['label']=SLOTS[row['slot']][0]
            row['effective']=row['status']=='active' and row['valid_from']<=today<=row['expires_on']
            row['verification']='operator_confirmed_context' if row['confirmed_at'] else 'pending_confirmation'
            row['shadow_pending']=row['id'] in shadowed
        return rows

    def get(self,mid):
        """管理接口可追溯确认、替换与撤回事件；不会把事件全文注入模型。"""
        with self.service.connection() as db:
            item=self.checked(db,mid)
            item['events']=[{**dict(row),'payload':json.loads(row['payload'])} for row in db.execute(
                'SELECT * FROM context_memory_events WHERE memory_id=? ORDER BY id',(mid,))]
            return item

    def propose(self,payload):
        """显式保存候选。来源任务须属本店；阶段观察必须限商品、已完成来源且最多30天。"""
        allowed={'slot','sku_id','content','source','source_task_id','valid_from','expires_on'}
        if not isinstance(payload,dict) or set(payload)-allowed:raise ValueError('经营背景字段不合法。')
        slot=payload.get('slot');sku=payload.get('sku_id') or ''
        if slot not in SLOTS:raise ValueError('请选择支持的经营背景事项。')
        if not isinstance(sku,str) or sku and sku not in self.service.catalog.products:raise ValueError('商品不在当前门店目录。')
        content=text(payload.get('content'),'经营背景',400);source=text(payload.get('source'),'来源',120)
        today=shanghai_today();expires=date.fromisoformat(payload.get('expires_on',''))
        valid_from=date.fromisoformat(payload.get('valid_from') or today.isoformat())
        maximum=30 if slot=='period_observation' else 365
        if not today<=expires<=today+timedelta(days=maximum):raise ValueError(f'有效期应在今天至{maximum}天内。')
        if not today-timedelta(days=365)<=valid_from<=expires:raise ValueError('生效日期应在过去一年内且不晚于到期日。')
        task_id=payload.get('source_task_id') or None
        if task_id is not None and (not isinstance(task_id,str) or len(task_id)!=32):raise ValueError('来源任务ID不合法。')
        if slot=='period_observation' and (not sku or not task_id):raise ValueError('阶段观察须选择具体商品并关联已完成调查。')
        fingerprint=hashlib.sha256(json.dumps([slot,sku,content,source,task_id,valid_from.isoformat(),expires.isoformat()],ensure_ascii=False).encode()).hexdigest()
        with self.service.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if task_id:
                task=db.execute('SELECT status,parsed_json FROM tasks WHERE id=?',(task_id,)).fetchone()
                if not task or task['status']!='completed':raise MissionNotFound('本店没有这条已完成来源调查。')
                if sku and json.loads(task['parsed_json']).get('sku_id')!=sku:raise ValueError('来源调查与所选商品不一致。')
                if slot=='period_observation' and valid_from.isoformat()<json.loads(task['parsed_json'])['target_date']:
                    raise ValueError('阶段观察不能在来源调查日期之前生效。')
            old=db.execute("SELECT id FROM context_memories WHERE fingerprint=? AND status IN ('proposed','active') AND expires_on>=?",(fingerprint,today.isoformat())).fetchone()
            if old:return {'memory':self.checked(db,old['id']),'reused':True}
            if db.execute("SELECT count(*) FROM context_memories WHERE status IN ('proposed','active') AND expires_on>=?",(today.isoformat(),)).fetchone()[0]>=100:raise MissionConflict('当前门店待确认和有效记忆已达100条，请先撤回旧项。')
            base=db.execute("SELECT id,revision FROM context_memories WHERE slot=? AND sku_id=? AND status='active'",(slot,sku)).fetchone()
            mid=uuid.uuid4().hex
            db.execute('INSERT INTO context_memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (mid,slot,sku,content,source,task_id,valid_from.isoformat(),expires.isoformat(),'proposed',1,
                 base['id'] if base else None,base['revision'] if base else None,utc_now(),None,fingerprint))
            self.event(db,mid,'proposed',source=source)
            return {'memory':self.checked(db,mid),'reused':False}

    def action(self,mid,payload):
        """人工确认或撤回。新版本确认时原子替换，过时的候选不能覆盖已更新的记忆。"""
        if not isinstance(payload,dict) or set(payload)-{'action','expected_revision','note'}:raise ValueError('操作字段不合法。')
        action=payload.get('action');revision=payload.get('expected_revision')
        if action not in ('confirm','retract') or type(revision) is not int:raise ValueError('请指定确认/撤回及当前版本。')
        note=text(payload.get('note'),'确认/撤回说明',200)
        with self.service.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            return self._apply(db,self.checked(db,mid,revision),action,note)

    def _apply(self,db,item,action,note):
        """在同一事务内执行确认或撤回；冲突裁决复用它，避免嵌套连接自锁。"""
        mid=item['id']
        if item['status'] not in ('proposed','active'):raise MissionConflict('该记录已被替换或撤回。')
        if action=='confirm':
            if item['status']!='proposed':raise MissionConflict('该经营背景已经确认。')
            if item['expires_on']<shanghai_today().isoformat():raise MissionConflict('已过期的候选不能确认。')
            current=db.execute("SELECT id,revision FROM context_memories WHERE slot=? AND sku_id=? AND status='active'",(item['slot'],item['sku_id'])).fetchone()
            if (current['id'] if current else None)!=item['base_id'] or (current['revision'] if current else None)!=item['base_revision']:
                raise MissionConflict('该事项已有其他更新，请重新提交候选后确认。')
            if current:
                db.execute("UPDATE context_memories SET status='superseded',revision=revision+1 WHERE id=?",(current['id'],))
                self.event(db,current['id'],'superseded',replacement_id=mid)
            db.execute("UPDATE context_memories SET status='active',revision=revision+1,confirmed_at=? WHERE id=?",(utc_now(),mid))
        else:db.execute("UPDATE context_memories SET status='retracted',revision=revision+1 WHERE id=?",(mid,))
        self.event(db,mid,action,note=note,attestation='operator_supplied_not_independently_verified')
        return self.checked(db,mid)

    def _sweep(self,db,today):
        """登记并清理范围遮蔽冲突；只比对范围与有效期，不判断哪条内容为真。

        同一事项的门店级与商品级记忆同时有效且时间重叠时登记一条 pending 冲突，
        因为检索会静默取商品级。这不是语义矛盾判定，本模块也没有这个能力。
        一方被撤回、取代或过期时自动关闭，避免留下无人认领的待裁决项；
        同一对记忆只保留一条记录，人工裁决为并存后不再重复提出。
        """
        for row in db.execute("SELECT id,general_id,specific_id FROM memory_conflicts WHERE status='pending'").fetchall():
            sides=db.execute("SELECT status,valid_from,expires_on FROM context_memories WHERE id IN (?,?)",(row['general_id'],row['specific_id'])).fetchall()
            live=len(sides)==2 and all(side['status']=='active' and side['valid_from']<=today<=side['expires_on'] for side in sides)
            if not live:db.execute("UPDATE memory_conflicts SET status='resolved',resolution='auto_closed',resolved_at=? WHERE id=?",(utc_now(),row['id']))
        groups={}
        for row in db.execute("SELECT id,slot,sku_id,valid_from,expires_on FROM context_memories WHERE status='active' AND valid_from<=? AND expires_on>=?",(today,today)).fetchall():
            groups.setdefault(row['slot'],[]).append(dict(row))
        for slot,items in groups.items():
            for base in [item for item in items if not item['sku_id']]:
                for other in items:
                    if not other['sku_id'] or not (base['valid_from']<=other['expires_on'] and other['valid_from']<=base['expires_on']):continue
                    db.execute('INSERT OR IGNORE INTO memory_conflicts VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (uuid.uuid4().hex,slot,other['sku_id'],base['id'],other['id'],'pending',None,None,utc_now(),None))

    def conflicts(self):
        """待裁决的范围遮蔽清单；两侧内容与来源一并给出，便于人工判断保留哪条。"""
        with self.service.connection() as db:
            self._sweep(db,shanghai_today().isoformat())
            rows=[dict(row) for row in db.execute("SELECT * FROM memory_conflicts WHERE status='pending' ORDER BY created_at")]
            for row in rows:
                row['label']=SLOTS.get(row['slot'],(row['slot'],set()))[0]
                for side,key in (('general','general_id'),('specific','specific_id')):
                    item=db.execute("SELECT id,content,source,sku_id,valid_from,expires_on FROM context_memories WHERE id=?",(row[key],)).fetchone()
                    row[side]=dict(item) if item else None
            return rows

    def resolve(self,cid,payload):
        """人工裁决范围遮蔽：保留一方或确认并存；不使用最新覆盖最旧。

        保留一方时撤回另一方而不是删除，历史任务快照与事件仍然可追溯。
        裁决结果写回冲突记录，两条记忆都留事件，便于后续审计谁决定了取舍。
        """
        if not isinstance(payload,dict) or set(payload)-{'resolution','note'}:raise ValueError('裁决字段不合法。')
        resolution=payload.get('resolution')
        if resolution not in ('keep_specific','keep_general','coexist'):raise ValueError('请选择保留商品级、保留门店级或确认两者并存。')
        note=text(payload.get('note'),'裁决说明',200)
        with self.service.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT * FROM memory_conflicts WHERE id=?",(cid,)).fetchone()
            if not row:raise MissionNotFound('当前门店没有这条待裁决冲突。')
            if row['status']!='pending':raise MissionConflict('该冲突已经裁决。')
            dropped=row['general_id'] if resolution=='keep_specific' else row['specific_id'] if resolution=='keep_general' else None
            if dropped:self._apply(db,self.checked(db,dropped),'retract',note or '范围遮蔽裁决后撤回不适用的一条')
            db.execute("UPDATE memory_conflicts SET status='resolved',resolution=?,note=?,resolved_at=? WHERE id=?",
                (resolution,note or None,utc_now(),cid))
            for mid in (row['general_id'],row['specific_id']):
                self.event(db,mid,'shadow_resolved',conflict_id=cid,resolution=resolution,note=note)
            return {'conflict':dict(db.execute("SELECT * FROM memory_conflicts WHERE id=?",(cid,)).fetchone())}

    def retrieve(self,db,task):
        """准入后按有效期、商品与意图过滤，再排序截断；无语义相似猜测。

        一个slot优先选择商品级覆盖门店级，最多3条且正文总长不超过1000字符。
        被覆盖的门店级背景若与商品级构成待裁决遮蔽，则在选中项与回执中显式披露，
        避免模型只看到一条却以为背景完整；裁决后不再披露。
        当前经营规则只作解释背景，不改变指标、阈值、权限或事实验证结果。
        """
        receipt={'strategy':'structured_scope_v1','selected':[],'eligible_count':0,'content_chars':0,
                 'retrieved_at':utc_now(),'limit':3,'content_budget':1000,'excluded_by_intent':0,
                 'excluded_by_selection':0,'conflict_pending':0,'shadowed':[]}
        if task.get('status')!='ready':return [],receipt
        today=shanghai_today().isoformat()
        self._sweep(db,today)
        rows=db.execute("SELECT * FROM context_memories WHERE status='active' AND valid_from<=? AND expires_on>=? AND sku_id IN ('',?) ORDER BY confirmed_at DESC,id",
            (today,today,task['sku_id'])).fetchall()
        receipt['excluded_by_intent']=sum(task['intent'] not in SLOTS[row['slot']][1] for row in rows)
        eligible=[dict(r) for r in rows if task['intent'] in SLOTS[r['slot']][1]]
        receipt['excluded_by_target_date']=sum(r['valid_from']>task['target_date'] for r in eligible)
        eligible=[r for r in eligible if r['valid_from']<=task['target_date']]
        receipt['eligible_count']=len(eligible)
        # 具体商品优先；同范围近期确认优先。稳定排序避免同轮随机变化。
        eligible.sort(key=lambda r:bool(r['sku_id']),reverse=True)
        # 管理页可查看本店全部冲突；任务上下文只能披露本轮真正适用的两侧记忆。
        # 仅按门店级ID匹配会把99朵商品的冲突挂到19朵上；历史目标日也要排除后来生效的记忆。
        eligible_ids={row['id'] for row in eligible}
        pending=[dict(row) for row in db.execute(
            "SELECT id,slot,general_id,specific_id FROM memory_conflicts WHERE status='pending' AND sku_id=?",
            (task['sku_id'],)).fetchall()
            if row['general_id'] in eligible_ids and row['specific_id'] in eligible_ids]
        shadow={}
        for row in pending:
            shadow[row['specific_id']]=('specific',row);shadow[row['general_id']]=('general',row)
        chosen=[];seen=set()
        for row in eligible:
            if row['slot'] in seen or len(chosen)>=3 or receipt['content_chars']+len(row['content'])>1000:
                receipt['excluded_by_selection']+=1;continue
            seen.add(row['slot']);receipt['content_chars']+=len(row['content'])
            reason='same_sku_and_intent' if row['sku_id'] else 'store_scope_and_intent'
            item={key:row[key] for key in ('id','revision','slot','sku_id','content','source','source_task_id','valid_from','expires_on','confirmed_at')}
            item.update(label=SLOTS[row['slot']][0],retrieval_reason=reason,authority='context_only_not_metric_evidence')
            if row['id'] in shadow:
                role,conflict=shadow[row['id']]
                item.update(shadow_pending=True,shadow_conflict_id=conflict['id'],
                    shadow_notice='同一事项还有一条'+('门店级' if role=='specific' else '商品级')+'背景未纳入本次上下文，需人工裁决。')
                receipt['shadowed'].append({'conflict_id':conflict['id'],'slot':row['slot'],'role':role,'injected':True,'reason':'scope_shadow_pending'})
            chosen.append(item);receipt['selected'].append({'id':row['id'],'revision':row['revision'],'reason':reason})
        reported={entry['conflict_id'] for entry in receipt['shadowed']}
        for conflict in pending:
            if conflict['id'] in reported or task['intent'] not in SLOTS.get(conflict['slot'],('',set()))[1]:continue
            receipt['shadowed'].append({'conflict_id':conflict['id'],'slot':conflict['slot'],'role':'both','injected':False,
                'reason':'scope_shadow_pending_not_injected'})
        receipt['conflict_pending']=len(receipt['shadowed'])
        return chosen,receipt
