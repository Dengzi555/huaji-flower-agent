"""评测中心应用层：固定适配器、SQLite任务账本、后台执行和同条件比较。

与经营任务库隔离。客户端只能选择固定评测集和模式，不能传文件路径、代码或密钥。
取消保留已完成用例；重启把在途任务标为中断，不自动重发付费请求。
"""
import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .cancellation import CancelToken, CancelledError, cancel_scope


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def code_digest(root):
    """对运行和评分代码一并留指纹；运行期间修改代码的结果禁止直接比较。"""
    return digest(b''.join(p.name.encode()+p.read_bytes() for p in sorted((root/'ecommerce_agent').glob('*.py'))))


class EvalCenter:
    """一个服务实例最多一个评测线程；读写使用短连接，避免跨线程复用SQLite。"""
    def __init__(self, root, state_dir, *, runner=None, loaded_source=None):
        self.root=Path(root).resolve();self.folder=Path(state_dir).resolve()/'eval-center'
        self.folder.mkdir(parents=True,exist_ok=True);self.db=self.folder/'jobs.sqlite3'
        self.loaded_source=loaded_source or code_digest(self.root)
        self.lock=threading.RLock();self.thread=None;self.token=None;self.closed=False
        if runner is None:
            from .golden_evaluation import run_suite
            runner=run_suite
        self.runner=runner
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, document TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS reviews (job_id TEXT, case_id TEXT, status TEXT, note TEXT, updated TEXT, PRIMARY KEY(job_id,case_id))')
            for key,raw in db.execute('SELECT id,document FROM jobs').fetchall():
                job=json.loads(raw)
                if job['status'] in ('running','cancelling'):
                    job.update(status='interrupted',finished=now(),message='服务重启，保留已完成记录；请新建评测。')
                    db.execute('UPDATE jobs SET document=? WHERE id=?',(json.dumps(job,ensure_ascii=False),key))

    def connect(self):
        """调用方使用with事务；连接退出时显式关闭，兼容Windows文件清理。"""
        from contextlib import contextmanager
        @contextmanager
        def connection():
            db=sqlite3.connect(self.db,timeout=10)
            try:
                with db:yield db
            finally:db.close()
        return connection()

    def dataset(self):
        """只浏览固定Golden集；预期字段仅用于评测，不注入被测模型。"""
        from .golden_evaluation import load_suite
        path=self.root/'data/evaluation/golden_cases.json'
        document,audit=load_suite(path,self.root/'data/intent_examples.json')
        return {**document,'id':'flower-golden','sha256':digest(path.read_bytes()),'separation_audit':audit}

    def get(self, identity):
        if not isinstance(identity,str) or len(identity)!=32 or any(c not in '0123456789abcdef' for c in identity):raise ValueError('评测任务ID无效。')
        with self.connect() as db:row=db.execute('SELECT document FROM jobs WHERE id=?',(identity,)).fetchone()
        if not row:raise ValueError('评测任务不存在。')
        job=json.loads(row[0])
        with self.connect() as db:reviews=db.execute('SELECT case_id,status,note,updated FROM reviews WHERE job_id=?',(identity,)).fetchall()
        job['reviews']={r[0]:{'status':r[1],'note':r[2],'updated':r[3]} for r in reviews}
        return job

    def save(self,job):
        with self.connect() as db:db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?)',(job['id'],json.dumps(job,ensure_ascii=False)))

    def list(self):
        with self.connect() as db:rows=db.execute('SELECT document FROM jobs ORDER BY rowid DESC LIMIT 100').fetchall()
        return [{**{k:v for k,v in json.loads(r[0]).items() if k not in ('cases','reviews')},'done':len(json.loads(r[0])['cases'])} for r in rows]

    def create(self,payload):
        """冻结评测定义和指纹后执行。API不接受任意Adapter、路径或命令。"""
        if set(payload)-{'split','mode','max_model_calls','label','timeout_seconds'}:raise ValueError('评测参数包含未支持字段。')
        split=payload.get('split','regression');mode=payload.get('mode','offline');budget=payload.get('max_model_calls',0)
        label=payload.get('label','');timeout=payload.get('timeout_seconds',300)
        if split not in ('regression','holdout','all') or mode not in ('offline','live'):raise ValueError('评测模式或分组无效。')
        if type(budget) is not int or not 0<=budget<=30 or mode=='live' and budget<1:raise ValueError('规划调用预算需为0—30；真实模型模式至少1次。')
        if mode=='offline' and budget:raise ValueError('离线模式的模型预算必须为0。')
        if type(timeout) is not int or not 10<=timeout<=900:raise ValueError('任务时限为10—900秒。')
        if not isinstance(label,str) or len(label)>80 or any(ord(c)<32 for c in label):raise ValueError('版本备注最多80个可见字符。')
        model=None
        if mode=='live':
            from .local_model import LocalModelSettings
            public=LocalModelSettings(self.root).public()
            if not public['enabled'] or not public['configured']:raise ValueError('请先在本机配置并启用模型。')
            model={'provider':public['provider'],'model':public['model']}
        with self.lock:
            if self.closed:raise ValueError('评测中心已关闭。')
            if code_digest(self.root)!=self.loaded_source:raise ValueError('源码已修改，请重启工作台后再评测，避免旧进程代码与新指纹不一致。')
            if self.thread and self.thread.is_alive():raise ValueError('已有评测运行中，请等待或停止后重试。')
            data=self.dataset();identity=uuid.uuid4().hex;output=self.folder/identity;output.mkdir()
            (output/'dataset.json').write_text(json.dumps({k:v for k,v in data.items() if k not in ('sha256','id','separation_audit')},ensure_ascii=False),encoding='utf-8')
            # 指纹基于冻结文件，确保报告与比较使用同一字节版本。
            selected=[c for c in data['cases'] if split=='all' or c['split']==split]
            job={'id':identity,'status':'running','created':now(),'finished':None,'label':label or '当前工作区',
                 'split':split,'mode':mode,'max_model_calls':budget,'timeout_seconds':timeout,'total':len(selected),
                 'model':model,
                 'dataset_version':data['version'],'dataset_sha256':digest((output/'dataset.json').read_bytes()),
                 'fixture':data['fixture'],'source_sha256':code_digest(self.root),'cases':[], 'summary':None,
                 'scorer_sha256':digest(b''.join((self.root/'ecommerce_agent'/n).read_bytes() for n in ('golden_evaluation.py','quality_lab.py','claw_eval_adapter.py'))),
                 'claw':{'status':'pending'},'message':'正在运行固定评测集。'}
            self.save(job);self.token=CancelToken()
            self.thread=threading.Thread(target=self._run,args=(job,self.token),daemon=True,name='flower-evaluation')
            self.thread.start();return job

    def _run(self,job,token):
        """后台进度逐题保存；取消与失败保留已完成结果，不冒充完整报告。"""
        output=self.folder/job['id'];expired=threading.Event()
        def expire():
            expired.set();token.cancel()
        timer=threading.Timer(job['timeout_seconds'],expire);timer.daemon=True;timer.start()
        def progress(entry):
            token.checkpoint()
            row=entry.get('row') or {};parsed=row.get('parsed') or {}
            item={k:v for k,v in entry.items() if k!='row'}
            item.update(question=parsed.get('raw_question',''),category=row.get('category'),elapsed_ms=row.get('elapsed_ms'),
                        model_calls=row.get('model_calls',0),failed_checks=(row.get('grade') or {}).get('failed_checks',[]),
                        actual=parsed,checks=(row.get('grade') or {}).get('checks',{}))
            with self.lock:
                token.checkpoint()
                job['cases'].append(item);self.save(job)
        try:
            with cancel_scope(token):
                report=self.runner(self.root,output/'dataset.json',output,job['split'],job['mode']=='live',job['max_model_calls'],on_progress=progress)
                token.checkpoint()
                job['summary']=report['summary'];job['model_calls']=report['model_calls']
                job['security_gate_passed']=report['security_gate_passed'];job['report_source_sha256']=report['source_sha256']
                job['separation_audit']=report['separation_audit']
                try:
                    from .claw_eval_adapter import export_and_grade
                    result=export_and_grade(output/'quality-report.json',None,output/'claw')
                    job['claw']={'status':'completed','summary':{k:result.get(k) for k in ('integration','dataset','model_mode','cases','trials','model_calls','observed_all_trials_passed','security_gate_passed','limitations')}}
                except Exception as exc:
                    job['claw']={'status':'unavailable','reason':type(exc).__name__,'message':'本次Claw评分未完成，保留原评测结果。'}
                token.checkpoint()
                with self.lock:
                    token.checkpoint();job.update(status='completed',message='评测执行完毕，请查看通过、失败与跳过。')
        except CancelledError:
            job.update(status='cancelled',stop_reason='time_budget' if expired.is_set() else 'cancelled',message='评测达到时间上限，保留已完成用例。' if expired.is_set() else '评测已停止，保留已完成用例。')
        except Exception as exc:
            job.update(status='failed',message='评测运行失败：'+type(exc).__name__+'。请检查固定数据版本与评测环境。')
        finally:
            timer.cancel()
            with self.lock:
                if token.event.is_set() and job['status']=='completed':job.update(status='cancelled',message='评测已停止，保留已完成结果。')
                job.update(finished=now(),source_unchanged=code_digest(self.root)==job['source_sha256'])
                self.save(job)

    def cancel(self,identity):
        with self.lock:
            job=self.get(identity)
            if job['status'] in ('running','cancelling'):
                job.update(status='cancelling',message='正在停止评测。');self.save(job)
                if self.token:self.token.cancel()
            return job

    def review(self,identity,payload):
        """人工审核仅记录判断，不改预期答案、不将失败题自动写入向量库。"""
        if set(payload)!={'case_id','status','note'}:raise ValueError('需要case_id、status和note。')
        job=self.get(identity);case=next((c for c in job['cases'] if c['case_id']==payload['case_id']),None)
        if not case or case['status']!='failed':raise ValueError('只能审核本次运行的失败案例。')
        if payload['status'] not in ('needs_review','confirmed','dismissed'):raise ValueError('审核状态无效。')
        if not isinstance(payload['note'],str) or len(payload['note'])>1000:raise ValueError('审核说明最多1000字。')
        with self.connect() as db:db.execute('INSERT OR REPLACE INTO reviews VALUES (?,?,?,?,?)',(identity,payload['case_id'],payload['status'],payload['note'],now()))
        return self.get(identity)

    def case_detail(self,identity,case_id):
        """按冻结题目白名单读取预期、实际和原始工具轨迹，不接受文件路径。"""
        job=self.get(identity);folder=self.folder/identity
        data=json.loads((folder/'dataset.json').read_text(encoding='utf-8'))
        expected=next((c for c in data['cases'] if c['id']==case_id),None)
        if not expected:raise ValueError('该案例不属于本次评测集。')
        result={'definition':expected,'result':next((c for c in job['cases'] if c['case_id']==case_id),None),'evidence':None,'events':[]}
        report_path=folder/'quality-report.json'
        if report_path.is_file():
            row=next((r for r in json.loads(report_path.read_text(encoding='utf-8'))['rows'] if r['case_id']==case_id),{})
            result['execution']={k:row.get(k) for k in ('parsed','grade','trajectory','evaluation_scope','error_type')}
        base=(folder/case_id).resolve()
        if not base.is_relative_to(folder.resolve()):raise ValueError('案例路径无效。')
        # 每次运行独立目录，同一case只运行一次；会话状态题没有经营取数证据。
        evidence=list(base.glob('*/evidence.json')) if base.is_dir() else []
        if len(evidence)==1:
            result['evidence']=json.loads(evidence[0].read_text(encoding='utf-8'))
            events=evidence[0].parent/'events.jsonl'
            if events.is_file():result['events']=[json.loads(line) for line in events.read_text(encoding='utf-8').splitlines() if line.strip()]
        return result

    def compare(self,left,right):
        """相同数据、模式和预算才比较；每题跳过单列，不作为修复或退步。"""
        a,b=self.get(left),self.get(right)
        if any(j['status']!='completed' or not j.get('source_unchanged') for j in (a,b)):raise ValueError('仅能比较执行完整且运行期间源码未变化的评测。')
        keys=('dataset_sha256','split','mode','fixture','max_model_calls','scorer_sha256','model')
        if any(a.get(k)!=b.get(k) for k in keys):raise ValueError('评测集、分组、数据版本、评分器、模型或预算不同，不可直接比较。')
        aa={c['case_id']:c for c in a['cases']};bb={c['case_id']:c for c in b['cases']}
        if aa.keys()!=bb.keys():raise ValueError('案例覆盖不同，不可直接比较。')
        changes=[]
        for key,x in aa.items():
            y=bb[key];before=x['status'];after=y['status']
            state='skipped' if 'skipped' in (before,after) else 'fixed' if before=='failed' and after=='passed' else 'regressed' if before=='passed' and after=='failed' else 'unchanged'
            changes.append({'case_id':key,'before':before,'after':after,'change':state,'elapsed_delta_ms':round((y.get('elapsed_ms') or 0)-(x.get('elapsed_ms') or 0),2)})
        return {'left':left,'right':right,'same_source':a['source_sha256']==b['source_sha256'],
                'counts':{s:sum(c['change']==s for c in changes) for s in ('fixed','regressed','unchanged','skipped')},'cases':changes}

    def close(self):
        with self.lock:
            self.closed=True
            if self.token:self.token.cancel()
        if self.thread:self.thread.join(timeout=3)
