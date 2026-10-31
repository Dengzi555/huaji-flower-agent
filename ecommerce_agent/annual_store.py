"""年度只读SQLite适配：保持日级数据契约，避免多门店百万记录常驻内存。

文件由年度生成器写完后发布，业务连接只读；任务冻结选择范围，不能拿评测答案取数。
"""
import json
import sqlite3
import threading
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path


class SqliteRecords(Mapping):
    """兼容旧records.get接口；扫描提供SQL过滤，禁止将整个年度映射缓存在进程内。"""
    def __init__(self,path):
        self.path=Path(path).resolve();self.local=threading.local()
        self._cached_get=lru_cache(maxsize=1024)(self._lookup)

    def connection(self):
        """每线程独立只读连接；页面缓存限量，数据库文件不得原地改写。"""
        if not hasattr(self.local,'db'):
            self.local.db=sqlite3.connect(self.path.as_uri()+'?mode=ro&immutable=1',uri=True)
            self.local.db.execute('PRAGMA cache_size=-1024')
        return self.local.db

    def __getitem__(self,key):
        return self._cached_get(key)

    def _lookup(self,key):
        """缓存属于单个库实例，不能要求Mapping本身可哈希。"""
        row=self.connection().execute('SELECT body FROM records WHERE domain=? AND sku=? AND day=?',key).fetchone()
        if row is None:raise KeyError(key)
        return json.loads(row[0])

    def __iter__(self):
        yield from self.connection().execute('SELECT domain,sku,day FROM records')

    def __len__(self):
        return self.connection().execute('SELECT count(*) FROM records').fetchone()[0]

    def rows(self,domain=None,sku=None,start=None,end=None,sku_ids=None):
        """仅拼接固定列名，所有用户或任务参数均绑定，不接受生成SQL。"""
        clauses=[];values=[]
        for column,value,operator in [('domain',domain,'='),('sku',sku,'='),('day',start,'>='),('day',end,'<=')]:
            if value is not None:clauses.append(column+operator+'?');values.append(value)
        if sku_ids is not None:
            if not sku_ids:return
            clauses.append('sku IN ('+','.join('?' for _ in sku_ids)+')');values.extend(sku_ids)
        sql='SELECT body FROM records'+(' WHERE '+' AND '.join(clauses) if clauses else '')+' ORDER BY domain,sku,day'
        for row in self.connection().execute(sql,values):yield json.loads(row[0])


def load_annual(store,path):
    """读取生成时校验过的年度文件元数据；文件版本由内容摘要标识。"""
    records=SqliteRecords(path)
    metadata=json.loads(records.connection().execute("SELECT value FROM metadata WHERE key='dataset'").fetchone()[0])
    if metadata.get('schema')!='flower-annual-sqlite-v1':raise ValueError('年度数据库版本不支持。')
    store.records=records;store.meta=metadata['meta'];store.skus=set(metadata['skus'])
    store.annual_metadata=metadata;store.content_fingerprint=metadata['content_sha256']
