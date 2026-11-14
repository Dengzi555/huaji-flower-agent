"""对话状态与多门店工作记忆：只继承查询条件，不继承指标、证据或权限。"""
import hashlib
import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import date

from .routing import route_question
from .task_parser import parse_dates, shanghai_today
from .time_scope import has_window
from .scope_patch import product_patch
from .query_plan import global_scope,ranking,resolve_time_conditions
from .store_comparison import start_comparison,comparison_view
from .task_repository import ConflictError
from .assistant_chat import local_reply,is_general_chat
from .cancellation import cancel_scope,current_token,checkpoint,CancelledError
from .model_runtime import model_scope,current_ledger
from .store_context import resolve_turn,ask_store,business_text
from .query_context import QueryContext,snapshot_scope,changes_baseline


class ConversationNotFound(ValueError):
    """对话不存在，不能误建为另一个对话。"""


class Conversations:
    """一个对话可引用多个门店；每次业务调用仍绑定单个门店的服务。"""
    def __init__(self, service, *, today=shanghai_today,default_query_scope=None):
        self.service=service;self.today=today;self.lock=threading.RLock();self.chat_responder=None;self.store_planner=None;self.query_planner=None
        self.default_query_scope=default_query_scope or getattr(service,'default_query_scope','current')
        if self.default_query_scope not in ('all','current'):raise ValueError('默认查询门店范围无效。')
        first=service.for_store() if hasattr(service,'for_store') else service
        self.path=first.database.parent/'conversations.sqlite3'
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, document TEXT NOT NULL)')

    @contextmanager
    def connection(self):
        """事务结束后显式关闭连接，避免Windows文件锁和重启恢复失败。"""
        db=sqlite3.connect(self.path)
        try:
            with db:yield db
        finally:db.close()

    def shops(self):
        """服务端目录是门店白名单；客户端不能指定任意数据路径。"""
        if hasattr(self.service,'stores'):return self.service.stores
        ctx=self.service.store.meta.get('business_context',{})
        return [{'id':ctx.get('store_id','local'),'name':ctx.get('store_name','当前门店')}]

    def active(self,sid):
        if sid not in {s['id'] for s in self.shops()}:raise ValueError('未知门店。')
        return self.service.for_store(sid) if hasattr(self.service,'for_store') else self.service

    def load(self,cid):
        with self.connection() as db:row=db.execute('SELECT document FROM conversations WHERE id=?',(cid,)).fetchone()
        if not row:raise ConversationNotFound('对话不存在。')
        return json.loads(row[0])

    def save(self,doc):
        with self.connection() as db:
            db.execute('INSERT OR REPLACE INTO conversations VALUES(?,?)',(doc['id'],json.dumps(doc,ensure_ascii=False)))

    def new(self,sid):
        """空对话显式绑定初始门店，不自动读取上一窗口的上下文。"""
        self.active(sid)
        doc={'id':uuid.uuid4().hex,'revision':0,'focus_store':sid,'query_store_ids':[],
            'stores':{},'turns':[],'waiting':[],'notice':'未指定门店时默认查询全部门店；可明确说“本店”或具体店名。' if self.default_query_scope=='all' else '未指定门店时查询当前选中店；也可以指定其他门店或全部门店。'}
        self.save(doc);return self.public(doc)

    def public(self,doc):
        """状态只返回查询条件与任务引用；当前任务状态实时读取，不复制指标到记忆。"""
        result=json.loads(json.dumps(doc));result.pop('replays',None)
        for turn in result['turns']:
            if turn.get('task_id'):
                task=self.active(turn['store_id']).get(turn['task_id'],include_events=False)
                turn['status']=task['status'] if task else 'missing'
        job=next((t['comparison'] for t in doc['turns'] if t.get('comparison') and t['comparison']['id']==doc.get('active_comparison')),None)
        if job:result['comparison']=comparison_view(self,job)
        return result

    def get(self,cid):
        with self.lock:return self.public(self.load(cid))

    @staticmethod
    def checked(doc,revision):
        if type(revision) is not int or revision!=doc['revision']:raise ConflictError('对话已变化，请刷新状态后再提交。')

    def focus(self,cid,payload):
        """手动选店只恢复该店查询条件，不运行工具，不把其他门店条件静默带过来。"""
        if not isinstance(payload,dict) or set(payload)-{'store_id','revision','task_id','comparison_id'} or not {'store_id','revision'}<=set(payload):raise ValueError('切店需指定store_id和revision。')
        with self.lock:
            doc=self.load(cid);self.checked(doc,payload['revision']);self.active(payload['store_id'])
            doc.pop('active_comparison',None)
            if payload.get('comparison_id'):
                job=next((t['comparison'] for t in doc['turns'] if t.get('comparison',{}).get('id')==payload['comparison_id']),None)
                if not job:raise ValueError('该比较不属于当前对话。')
                if payload['store_id']!=job['anchor_store']:raise ValueError('恢复比较时请使用该比较原来的参照店。')
                doc['active_comparison']=job['id']
            if payload.get('task_id'):
                turns=[t for t in doc['turns'] if t['store_id']==payload['store_id'] and t.get('task_id')==payload['task_id'] or any(c['store_id']==payload['store_id'] and c.get('task_id')==payload['task_id'] for c in t.get('comparison',{}).get('children',[]))]
                if not turns:raise ValueError('该调查不属于当前对话和门店。')
                task=self.active(payload['store_id']).get(payload['task_id'])
                if not task:raise ValueError('调查记录不存在。')
                parsed=task['parsed']
                doc['stores'][payload['store_id']]={'scope':turns[-1]['scope'],'task_id':task['id'],
                    'candidates':parsed.get('candidates',[]),'resolved_question':parsed.get('conversation_context',{}).get('resolved_question',task['question'])}
            doc.update(focus_store=payload['store_id'],revision=doc['revision']+1,waiting=[],notice='已恢复该门店的调查上下文；没有重新执行查询。')
            doc['query_store_ids']=[payload['store_id']]
            doc.pop('pending_store',None)
            doc.pop('pending_product_patch',None)
            self.save(doc);return {'conversation':self.public(doc)}

    def turn(self,cid,payload,key,on_delta=None,cancel_token=None):
        """一轮规划/对话共用资源账本；异步后台任务由worker建立自己的执行账本。"""
        with model_scope():
            return self._turn_with_cancel(cid,payload,key,on_delta,cancel_token)

    def _turn_with_cancel(self,cid,payload,key,on_delta=None,cancel_token=None):
        """为规划/普通对话绑定独立令牌；未创建任务的停止也持久化为对话事件。"""
        with cancel_scope(cancel_token or current_token()):
            try:return self._turn(cid,payload,key,on_delta)
            except CancelledError:
                with self.lock:
                    doc=self.load(cid)
                    if key in doc.get('replays',{}):return {'conversation':self.public(doc),'task':None,'reused':True}
                    token=current_token();problem=CancelledError().public()
                    entry={'question':payload['question'],'store_id':doc['focus_store'],'scope':{},'task_id':None,
                        'kind':'cancelled','message':problem['message'],'inherited':[],
                        'chat':{'mode':'cancelled','error_info':problem,'cancellation':token.details() if token else {}}}
                    doc['turns'].append(entry);doc['revision']+=1;doc['notice']='本次请求已停止，此前查询条件保留。'
                    digest=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                    self.record(doc,key,digest,entry);self.save(doc)
                    return {'conversation':self.public(doc),'task':None,'reused':False}

    def _turn(self,cid,payload,key,on_delta=None):
        """确定指代→继承缺失条件→原解析器重新准入→创建本店任务；歧义不跑工具。"""
        if not isinstance(payload,dict) or set(payload)-{'question','sku','date','intent','revision','store_id','store_choice','store_scope','mode'}:raise ValueError('不支持的对话字段。')
        if 'store_scope' in payload and payload['store_scope'] not in ('all','current'):raise ValueError('查询窗口范围无效。')
        question=payload.get('question','')
        if not isinstance(question,str) or not 1<=len(question.strip())<=2000:raise ValueError('问题需为1–2000字。')
        if not key or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',key):raise ValueError('需要合法的幂等键。')
        for field in ('sku','date','intent','store_id','store_choice'):
            if payload.get(field) is not None and (not isinstance(payload[field],str) or len(payload[field])>100):raise ValueError('查询条件必须是短字符串。')
        digest=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        with self.lock:
            doc=self.load(cid)
            replay=doc.get('replays',{}).get(key)
            if replay:
                if replay['hash']!=digest:raise ConflictError('同一幂等键不能用于不同的对话输入。')
                task=self.active(replay['store_id']).get(replay['task_id']) if replay.get('task_id') else None
                return {'conversation':self.public(doc),'task':task,'reused':True}
            self.checked(doc,payload.get('revision'))
            checkpoint()
            if len(doc['turns'])>=100:raise ConflictError('当前对话已达100轮，请新建对话。')
            active_job=next((t['comparison'] for t in doc['turns'] if t.get('comparison') and t['comparison']['id']==doc.get('active_comparison')),None)
            store_resolution,effective_question,conditions=resolve_turn(self,doc,payload,active_job)
            # 门店级推广无需商品SKU；在普通聊天判定前保留纯日期追问，但仍先校验门店。
            if store_resolution['status']=='resolved':
                from .advertising_query import reply as advertising_reply
                advertising_result=advertising_reply(self,doc,payload,key,digest,store_resolution,business_text(effective_question,self.shops()),on_delta)
                if advertising_result is not None:return advertising_result
            # 先区分普通交流，不能让“你好”继承上一SKU并要求补日期。
            service=self.active(doc['focus_store']);reply=local_reply(question)
            explicit=any(payload.get(field) for field in ('sku','date','intent'))
            if not explicit and 'date' in doc.get('waiting',[]):
                # 正在补时间时，“上个月”“10月20日至25日”是条件回复，继续原业务问题。
                explicit=resolve_time_conditions(question,self.today())['status']=='resolved'
            if payload.get('mode') not in (None,'auto','model'):raise ValueError('不支持的对话模式')
            if not payload.get('store_choice') and (payload.get('mode')=='model' or reply is not None or not explicit and store_resolution['source'] in ('context','default_all') and is_general_chat(question,service.catalog,self.today(),bool(doc['stores']))):
                return self.chat(doc,question,None if payload.get('mode')=='model' else reply,key,digest,on_delta)
            if store_resolution['status']!='resolved':return ask_store(self,doc,payload,key,digest,store_resolution)
            # 澄清后继续原始经营问题；确认话语仍保存在会话，业务任务保存原查询及确认依据。
            turn_question=question
            query_question=effective_question
            question=business_text(effective_question,self.shops());payload={**payload,**conditions}
            doc.pop('pending_store',None);doc.pop('store_choices',None)
            previous=doc['stores'].get(doc['focus_store'],{})
            if active_job:
                anchor_child=next((c for c in active_job['children'] if c['store_id']==doc['focus_store']),{})
                previous={'scope':active_job['scope'],'task_id':anchor_child.get('task_id'),'candidates':[],'resolved_question':question}
            names=store_resolution['store_ids'] if store_resolution['source']!='context' else []
            sid=store_resolution['store_ids'][0]
            service=self.active(sid)
            business_today=date.fromisoformat(store_resolution['business_today'])
            if store_resolution['action'] in ('compare','rank'):
                targets=list(dict.fromkeys(store_resolution['reference_store_ids']+store_resolution['store_ids']))
                return start_comparison(self,doc,query_question,payload,key,digest,targets,store_resolution=store_resolution,business_question=question)
            own=doc['stores'].get(sid,{})
            # “回到”恢复各店自身条件；“另一店呢”明确承接当前查询条件，但重新取证。
            resume=store_resolution['action']=='switch'
            source=previous if active_job and not resume else own if resume or not names else previous
            base=source.get('scope',{})
            switch_days,switch_errors=parse_dates(question,business_today)
            switch_identity=service.catalog.resolve(question,today=business_today)
            # 是否纯切店由共享日期/商品/意图解析器判断，不靠另一份日期关键词名单。
            # “切回南岸店10月24日...”不能被错误当成只导航而忽略日期。
            switch_only=(resume and not switch_days and not switch_errors and not has_window(question)
                and route_question(question)['status']!='selected'
                and not switch_identity['candidates'] and not switch_identity['issues']
                and re.fullmatch(r'(?:请|帮我|麻烦|先|再|切到|切回|回到|返回|切换到|切换|到|看一下|看看|呢|吧|一下|[，。！？?\s])*',question)
                and not any(payload.get(k) for k in ('sku','date','intent')))
            if switch_only:
                doc.update(focus_store=sid,revision=doc['revision']+1,waiting=[],notice='已回到该店上次调查，没有重新取数。')
                doc['query_store_ids']=[sid];doc.pop('active_comparison',None)
                entry={'question':turn_question,'store_id':sid,'scope':base,'task_id':own.get('task_id'),'kind':'resume','inherited':[],'store_resolution':store_resolution}
                doc['turns'].append(entry);self.record(doc,key,digest,entry);self.save(doc)
                return {'conversation':self.public(doc),'task':service.get(own['task_id']) if own.get('task_id') else None,'reused':False}
            scope_request=global_scope(question) or bool(re.search(r'哪(?:一)?款|哪(?:一)?个商品|销冠|排行|排名',question))
            decision=route_question(question)
            # 能力边界和独立多意图不能被继承意图覆盖。
            intent=payload.get('intent') or (decision['intent'] if decision['status']=='selected' else None)
            inherit_ok=decision['status']!='unsupported' and not decision['matched_rules'] and not scope_request
            inherited=[]
            if not intent and inherit_ok and base.get('intent'):
                intent=base['intent'];inherited.append('intent')
            days,errors=parse_dates(question,business_today)
            day=payload.get('date')
            baseline_patch=changes_baseline(question,base)
            inherited_date=(baseline_patch or not days and not errors and not has_window(question)) and not day and bool(base.get('date'))
            if inherited_date:
                day=base['date'];inherited.append('date')
            identity=service.catalog.resolve(question,today=business_today)
            explicit=bool(identity['candidates'] or identity['issues'] or identity.get('suggested_correction') or scope_request)
            sku=payload.get('sku');replacement=bool(re.search(r'换成(?!前天|昨天|今日|今天|日期|上周|本周|近|最近|过去|\d{4})|换个|换一款|换商品|另一个商品',question))
            patch=None
            if decision['status']!='unsupported':
                pending_patch=doc.get('pending_product_patch')
                if sku and pending_patch and pending_patch['store_id']==sid and pending_patch['question']==question:
                    if sku not in {p['sku_id'] for p in pending_patch['candidates']}:raise ValueError('所选商品不在本轮规格候选中。')
                    patch=pending_patch
                elif not sku:
                    patch=product_patch(question,service.catalog,base.get('sku'))
                    if patch:
                        patch.update(store_id=sid,question=question)
                        if patch['exact'] and len(patch['candidates'])==1:sku=patch['candidates'][0]['sku_id']
                        else:
                            source_task=source.get('task_id') if sid==doc['focus_store'] else None
                            doc['focus_store']=sid
                            doc['stores'][sid]={'scope':base,'task_id':source_task,'candidates':source.get('candidates',[]),'resolved_question':question}
                            return self.wait(doc,question,patch['message'],['sku'],key,digest,product_patch=patch)
            # “白色那款”只能在当前待确认候选内筛，不扫描别店/旧商品的候选。
            colors=re.findall(r'(白|红|粉|紫|黄|蓝|混)色',question)
            pending=source.get('candidates',[])
            if not sku and not explicit and colors and pending:
                candidates=[p for p in pending if all(c in p.get('variety','') for c in colors)]
                if len(candidates)==1:sku=candidates[0]['sku_id']
                else:return self.wait(doc,question,'颜色仍不能唯一确定商品，请从候选里选择具体规格。',['sku'],key,digest)
            elif not sku and not explicit and (replacement or colors):
                return self.wait(doc,question,'未确定新的商品，请提供商品名和规格，或选择具体商品。',['sku'],key,digest)
            elif not sku and not explicit and base.get('sku') and decision['status']!='unsupported':
                # 只接受有限、可解释的省略表达；陌生商品不能悄悄复用上一SKU。
                referential=bool(names or re.fullmatch(r'(?:那|它|这款|这个|这束|之前那款|刚才那款|换成|改查|再|也|帮我|查一下|查|看看|缺货|多久|库存|活动|状态|销量|情况|表现|怎么样|原因|分析|一下|的|呢|？|\?|对比|相比|比较|趋势|走势|曲线|取消|订单|完成|履约|率|一共|卖了|买了|多少|总共|星期|一|二|三|四|五|六|日|前天|昨天|今天|昨日|今日|上周|前周|本周|同一天|同日|最近|过去|近|天|[0-9年月日/\-\s])+',question))
                referential=referential or bool(ranking(question) and re.fullmatch(r'(?:那|再看|看看|看|最差|最好|最高|最低|最多|最少|呢|？|\?|的|销量|\s)+',question))
                if referential:sku=base['sku'];inherited.append('sku')
                else:
                    settings=getattr(service,'model_settings',None)
                    status=settings.public() if settings else {}
                    if not (status.get('enabled') and status.get('configured')):
                        return self.wait(doc,question,'未明确新的商品或指代，请选择本次商品，不会默认使用上一款。',['sku'],key,digest)
                    # 未知表达交后续分类/受限规划，而不是在对话继承层提前结束；不继承旧SKU。
            # 待澄清自然语言仍保留原句；已确认的条件通过结构化补丁进入统一准入。
            resolved=question
            if not explicit and not replacement and pending and not sku:
                resolved=source.get('resolved_question',question)+' '+question
            context=QueryContext.followup(question,base,identity,
                inherit_product=sku=='__all_matched__' and not explicit,
                inherit_time=inherited_date,baseline_patch=baseline_patch)
            body={'question':query_question}
            for field,value in [('sku',sku),('date',day),('intent',intent)]:
                if value:body[field]=value
            parent=source.get('task_id') if active_job else own.get('task_id')
            if parent and sid==doc['focus_store']:body['parent_id']=parent
            metadata={'conversation_id':cid,'turn':len(doc['turns'])+1,'resolved_question':resolved,'inherited':inherited,
                      'previous':{'store_id':doc['focus_store'],'task_id':previous.get('task_id')},'store_id':sid,
                      'store_resolution':store_resolution,'query_context':context.document()}
            if patch:metadata['local_patch']={k:patch[k] for k in ('constraints','changed')}
            result=service.create(body,'conv-'+hashlib.sha256((cid+key).encode()).hexdigest(),_conversation=metadata,_today=business_today)
            task=result['task'];parsed=task['parsed']
            # 唯一候选遇到错字确认仍不能显示为“已确认”。
            confirmed=parsed.get('sku_id') if not parsed.get('product_resolution',{}).get('suggested_correction') or sku else None
            scope=snapshot_scope(parsed,confirmed_sku=confirmed)
            candidates=parsed.get('candidates',[])
            preserve=task['status'] in ('unsupported','failed','cancelled','cancelling','incomplete')
            if not preserve:
                doc['stores'][sid]={'scope':scope,'task_id':task['id'],'candidates':candidates,'resolved_question':resolved}
            doc.pop('pending_product_patch',None)
            doc.pop('active_comparison',None)
            doc['query_store_ids']=[sid]
            # 规划器已区分真实缺失字段时直接使用；类别有多个候选并不等于缺少SKU。
            missing=parsed.get('clarification',{}).get('missing_fields')
            waiting=[{'product':'sku'}.get(k,k) for k in missing] if missing else [k for k in ('sku','date','intent') if not scope[k]]
            doc.update(focus_store=sid,revision=doc['revision']+1,waiting=[] if preserve else waiting,
                notice=((task.get('error_info') or parsed.get('error_info') or {}).get('title','本次要求不支持')+'；此前查询条件已保留。' if preserve else '沿用：'+('、'.join({'sku':'商品','date':'日期','intent':'查询事项'}[k] for k in inherited) if inherited else '无')+'；各店独立取证。'))
            entry={'question':turn_question,'store_id':sid,'scope':scope,'task_id':task['id'],'kind':'investigation','inherited':inherited,'store_resolution':store_resolution}
            doc['turns'].append(entry);self.record(doc,key,digest,entry);self.save(doc)
            return {'conversation':self.public(doc),'task':task,'reused':result['reused']}

    def record(self,doc,key,digest,entry):
        """用量跟随本轮持久化；幂等复用直接读取原记录，不重新计费或清零。"""
        if current_ledger():entry['model_runtime']=current_ledger().snapshot()
        doc.setdefault('replays',{})[key]={'hash':digest,'store_id':entry['store_id'],'task_id':entry.get('task_id')}

    def chat(self,doc,question,reply,key,digest,on_delta=None):
        """闲聊只追加消息与版本，不改业务槽位、待确认候选、基线或工具权限。"""
        metadata={'model_calls':0,'mode':'rules'}
        partial=[]
        def emit_delta(text):
            checkpoint()
            if sum(map(len,partial))<3000:partial.append(text)
            if on_delta:on_delta(text)
        if reply is None:
            settings=getattr(self.active(doc['focus_store']),'model_settings',None)
            status=settings.public() if settings else {}
            if self.chat_responder or status.get('enabled') and status.get('configured'):
                try:
                    responder=self.chat_responder or settings.chat
                    # 只取同店成功对话；失败/停止的半句不能作为下一轮已确认回答。
                    history=[turn for turn in doc['turns'] if turn.get('kind')=='chat' and turn.get('chat',{}).get('mode')!='advertising'
                        and turn.get('store_id')==doc['focus_store'] and not turn.get('chat',{}).get('error_info')][-12:]
                    checkpoint()
                    result=responder(question,history,on_delta=emit_delta) if on_delta and not self.chat_responder else responder(question,history)
                    checkpoint()
                    if not isinstance(result,dict) or not isinstance(result.get('text'),str) or not result['text'].strip():raise ValueError('无有效回复')
                    reply=result['text'][:3000];metadata={k:result[k] for k in ('model_calls','model','usage') if k in result};metadata['mode']='model'
                except Exception as exc:
                    from .errors import from_exception
                    problem=from_exception(exc,phase='chat',fallback='model_response_invalid').public()
                    reply=problem['message']+' 经营查询条件仍保留。'
                    metadata={'mode':'fallback','model_calls':1,'error_info':problem}
                    if problem['code']=='request_cancelled':
                        reply=(''.join(partial)+'\n\n已停止，以上仅为未完成的部分回复。') if partial else problem['message']
                        metadata.update(mode='cancelled',cancellation=current_token().details() if current_token() else {})
            else:
                reply='我在。当前普通对话模型未启用，可以在左下角设置中启用 DeepSeek 并配置 API；问候、使用帮助和已支持的经营查询仍可使用。'
                metadata={'mode':'fallback','model_calls':0}
                from .errors import AgentError
                metadata['error_info']=AgentError('model_not_configured',phase='chat').public()
        entry={'kind':'chat','question':question,'message':reply,'store_id':doc['focus_store'],'task_id':None,
            'scope':{},'inherited':[],'chat':metadata}
        doc['turns'].append(entry);doc['revision']+=1
        self.record(doc,key,digest,entry);self.save(doc)
        return {'conversation':self.public(doc),'task':None,'reused':False}

    def wait(self,doc,question,message,waiting,key,digest,choices=None,product_patch=None):
        """指代不清时只记录对话，不创建业务任务或启动任何分支。"""
        doc.update(revision=doc['revision']+1,waiting=waiting,notice=message,store_choices=choices or [])
        doc.pop('active_comparison',None)
        doc.pop('pending_product_patch',None)
        if product_patch:doc['pending_product_patch']=product_patch
        entry={'question':question,'store_id':doc['focus_store'],'kind':'clarification','message':message,'scope':{},'task_id':None,'inherited':[]}
        doc['turns'].append(entry);self.record(doc,key,digest,entry);self.save(doc)
        return {'conversation':self.public(doc),'task':None,'reused':False}
