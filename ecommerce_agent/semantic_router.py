"""本地中文语义候选检索：BGE向量、余弦相似度、可追溯示例，不替代任务授权。

离线build命令显式下载权重与构建索引；在线请求只读本地缓存。
检索只建议意图，绝不从相似例句复制商品、日期或经营数值。
"""
import argparse
import hashlib
import json
import math
import re
import sys
import threading
import time
from functools import lru_cache
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
MODEL='BAAI/bge-small-zh-v1.5'
VERSION='flower-semantic-v1'
PREPROCESS_VERSION='mask-product-time-v1'
_lock=threading.RLock()


def load_runtime(root):
    """依赖限定在项目目录；未安装时由上层降级，不能在用户请求期间执行pip。"""
    path=str(Path(root)/'.runtime/semantic')
    if Path(path).is_dir():
        # 受限环境无法读取可选依赖时，不把不可访问目录插入全局模块搜索路径。
        # 否则检索虽降级，后续无关依赖也可能被该目录中的同名模块干扰。
        with (Path(path)/'fastembed/__init__.py').open('rb') as source:source.read(1)
    if path not in sys.path:sys.path.insert(0,path)
    import numpy as np
    from fastembed import TextEmbedding
    return np,TextEmbedding


def read_examples(path):
    """加载人工维护的少量路由示例；只允许注册意图，避免索引获得新增权限。"""
    from .routing import ROUTES
    raw=Path(path).read_bytes();document=json.loads(raw);examples=document['examples']
    if not 1<=len(examples)<=1000:raise ValueError('意图示例数无效。')
    ids=set()
    for example in examples:
        if example['id'] in ids or example['intent'] not in ROUTES or not isinstance(example['text'],str) or not 1<=len(example['text'])<=300:
            raise ValueError('示例ID、意图或文本无效。')
        ids.add(example['id'])
    return examples,hashlib.sha256(raw).hexdigest()


def entity_terms(root):
    """目录实体用于去偏：同一花名和日期不能比“缺货/下降”等真实诉求更影响相似度。"""
    raw=active_catalog_path(root).read_bytes();products=json.loads(raw)['products']
    terms={str(p.get(key,'')) for p in products for key in ('short_name','variety','series')}
    terms.update(alias for p in products for alias in p.get('aliases',[]) if len(alias)>=2)
    return sorted(terms-{''},key=len,reverse=True),hashlib.sha256(raw).hexdigest()


def active_catalog_path(root):
    """与工作台使用同一目录版本；年度库发布后索引须显式重建，不能混用旧词表。"""
    root=Path(root);active=root/'data/annual/active.json'
    if active.exists():
        version=json.loads(active.read_text(encoding='utf-8'))['version']
        if not re.fullmatch(r'[a-f0-9]{12}',version):raise ValueError('年度目录版本不合法。')
        return root/'data/annual'/version/'products.json'
    return root/'data/products.json'


def intent_text(question,terms):
    """只用于向量输入；任务规划继续读取原话，不能丢失日期或商品约束。"""
    text=re.sub('|'.join(re.escape(t) for t in terms),'商品',question) if terms else question
    text=re.sub(r'\d{4}-\d{1,2}-\d{1,2}|(?:\d{4}年)?\d{1,2}月\d{1,2}[日号]?|(?:最近|近)\d+[天周日]|昨天|昨日|前天|今天|本周|这周|上周[一二三四五六日天]?|上个月|本月|这个月|上月|教师节|母亲节|父亲节|情人节','',text)
    text=re.sub(r'\d+\s*(?:朵|枝|束|桶)?','',text)
    return re.sub(r'\s+','',text).strip('，。！？? ') or question


def rank_candidates(examples,similarities,top_k=3):
    """每个意图取最相似三例均值，避免示例数量多的意图天然占优。

    相似度和分差只是检索信号，不是概率；本版全部作为规划候选，不自动选路。
    """
    if len(examples)!=len(similarities):raise ValueError('向量与示例数量不一致。')
    groups={}
    for example,score in zip(examples,similarities):
        score=float(score)
        if not math.isfinite(score):raise ValueError('检索分数无效。')
        groups.setdefault(example['intent'],[]).append({'id':example['id'],'text':example['text'],'similarity':round(score,6)})
    candidates=[]
    for intent,hits in groups.items():
        hits.sort(key=lambda x:x['similarity'],reverse=True);hits=hits[:3]
        candidates.append({'intent':intent,'score':round(sum(h['similarity'] for h in hits)/len(hits),6),'examples':hits})
    candidates.sort(key=lambda x:(-x['score'],x['intent']))
    return candidates[:top_k]


class SemanticRouter:
    """一个进程共享模型和索引；推理串行化，查询向量只在限量内存中缓存。"""
    def __init__(self,root=ROOT):
        self.root=Path(root);self.folder=self.root/'artifacts/intent-index'
        self.examples,self.examples_hash=read_examples(self.root/'data/intent_examples.json')
        self.terms,product_hash=entity_terms(self.root)
        manifest=json.loads((self.folder/'manifest.json').read_text(encoding='utf-8'))
        if manifest['model']!=MODEL or manifest['examples_sha256']!=self.examples_hash or manifest.get('products_sha256')!=product_hash or manifest.get('preprocess')!=PREPROCESS_VERSION:raise ValueError('语义索引过期，请重新构建。')
        np,Embedding=load_runtime(self.root);self.np=np
        vector_path=self.folder/'vectors.npy'
        if hashlib.sha256(vector_path.read_bytes()).hexdigest()!=manifest['vectors_sha256']:raise ValueError('语义索引校验失败。')
        self.vectors=np.load(vector_path,allow_pickle=False)
        if self.vectors.shape!=(len(self.examples),512) or not np.isfinite(self.vectors).all():raise ValueError('语义索引维度或数值无效。')
        if not np.allclose(np.linalg.norm(self.vectors,axis=1),1,atol=.001):raise ValueError('语义索引未归一化。')
        self.encoder=Embedding(model_name=MODEL,cache_dir=str(self.root/'.runtime/models'),threads=2,local_files_only=True)
        self.manifest=manifest

    @lru_cache(maxsize=128)
    def _vector(self,question):
        """不把经营问题发送到Embedding API；新问题的向量在本机CPU生成。"""
        vector=next(iter(self.encoder.embed([question])))
        norm=self.np.linalg.norm(vector)
        if not self.np.isfinite(vector).all() or norm<=0:raise ValueError('查询向量无效。')
        return vector/norm

    def retrieve(self,question):
        """输出候选、示例来源、索引版本和耗时；不返回任何可执行工具指令。"""
        started=time.perf_counter()
        with _lock:scores=self.vectors@self._vector(intent_text(question,self.terms))
        candidates=rank_candidates(self.examples,scores)
        margin=candidates[0]['score']-candidates[1]['score'] if len(candidates)>1 else 0
        return {'status':'ok','version':VERSION,'model':MODEL,'examples_sha256':self.examples_hash,'preprocess':PREPROCESS_VERSION,
                'candidates':candidates,'margin':round(margin,6),'policy':'candidates_only',
                'duration_ms':round((time.perf_counter()-started)*1000,3),'network_calls':0}


@lru_cache(maxsize=2)
def _router(root,index_stamp,examples_stamp,products_stamp):
    """索引或例句更新后创建新实例，旧版不会继续给新版请求提供候选。"""
    return SemanticRouter(Path(root))


def retrieve_intents(question,root=ROOT):
    """依赖、索引缺失或损坏都降级到原模型规划，不能阻断经营查询。"""
    root=Path(root)
    try:
        manifest=root/'artifacts/intent-index/manifest.json';examples=root/'data/intent_examples.json'
        with _lock:router=_router(str(root),manifest.stat().st_mtime_ns,examples.stat().st_mtime_ns,active_catalog_path(root).stat().st_mtime_ns)
        return router.retrieve(question)
    except Exception as exc:
        return {'status':'unavailable','version':VERSION,'candidates':[],'policy':'model_fallback',
                'reason':type(exc).__name__,'network_calls':0}


def build_index(root=ROOT):
    """显式初始化本地模型和索引，下载只发生在该管理命令，不发生在聊天请求中。"""
    root=Path(root);examples,digest=read_examples(root/'data/intent_examples.json');np,Embedding=load_runtime(root)
    terms,product_hash=entity_terms(root)
    encoder=Embedding(model_name=MODEL,cache_dir=str(root/'.runtime/models'),threads=2)
    vectors=np.stack(list(encoder.embed([intent_text(e['text'],terms) for e in examples])))
    vectors=vectors/np.linalg.norm(vectors,axis=1,keepdims=True)
    folder=root/'artifacts/intent-index';folder.mkdir(parents=True,exist_ok=True)
    # 先写向量再写清单；中断时哈希不匹配将安全降级，不会使用半写入的索引。
    path=folder/'vectors.npy';np.save(path,vectors,allow_pickle=False)
    manifest={'version':VERSION,'model':MODEL,'dimensions':int(vectors.shape[1]),'examples':len(examples),
              'preprocess':PREPROCESS_VERSION,'products_sha256':product_hash,
              'examples_sha256':digest,'vectors_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
              'policy':'candidates_only','calibrated_for_direct_routing':False}
    (folder/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return manifest


def main():
    """构建或检查索引：python -m ecommerce_agent.semantic_router build/search。"""
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['build','search']);parser.add_argument('question',nargs='?',default='')
    args=parser.parse_args()
    result=build_index() if args.command=='build' else retrieve_intents(args.question)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
