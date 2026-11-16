import json
import math
import re
import time
from pathlib import Path
from .core import terms,dump

MODEL='BAAI/bge-small-zh-v1.5'
_embedder=None


def embedder(settings):
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding
        _embedder=TextEmbedding(MODEL,cache_dir=str(settings.model_dir/'embeddings'),threads=1,local_files_only=True)
    return _embedder


def index_record(db,record_id,pages=None):
    record=db.one('SELECT * FROM records WHERE id=?',(record_id,))
    if not record:return
    pages=pages or [(record['locator'] or '正文',record['body'])]
    chunks=[]
    for locator,text in pages:
        # Small overlapping character windows keep Chinese passages below model limits.
        for start in range(0,len(text),300):
            value=text[start:start+400]
            if value.strip():chunks.append((record_id,record['version'],locator+' · '+str(start),value,terms(record['title']+' '+value)))
    with db.transaction() as c:
        old=[r[0] for r in c.execute('SELECT rowid FROM chunks WHERE record_id=?',(record_id,))]
        for pk in old:c.execute('DELETE FROM embeddings WHERE chunk_id=?',(pk,))
        c.execute('DELETE FROM chunks WHERE record_id=?',(record_id,))
        c.executemany('INSERT INTO chunks(record_id,version,locator,text,search_terms) VALUES(?,?,?,?,?)',chunks)


def index_vectors(db,record_id=None):
    import numpy as np
    sql='SELECT c.rowid,c.text FROM chunks c LEFT JOIN embeddings e ON c.rowid=e.chunk_id WHERE e.chunk_id IS NULL'
    args=()
    if record_id:sql+=' AND c.record_id=?';args=(record_id,)
    rows=db.all(sql+' LIMIT 64',args)
    if not rows:return 0
    vectors=embedder(db.settings).embed([r['text'] for r in rows],batch_size=8)
    with db.transaction() as c:
        for row,vec in zip(rows,vectors):
            v=np.asarray(vec,dtype=np.float32);v/=max(float(np.linalg.norm(v)),1e-12)
            c.execute('INSERT OR REPLACE INTO embeddings VALUES(?,?,?)',(row['rowid'],MODEL,v.tobytes()))
    return len(rows)


def search(db,access,query,projects=None,limit=6,semantic=True):
    allowed=set(access.visible_ids(search=True))
    if projects:
        for pk in projects:access.project(pk)
        allowed={pk for pk in allowed if access.meta[pk]['project_id'] in projects or set(json.loads(access.meta[pk]['deps']))&set(projects)}
    if not allowed:return {'items':[],'mode':'local-keyword','total':0}
    # Authorization predicate is applied in SQL before ranking/returning any text.
    expressions=list(dict.fromkeys(terms(query).split()))[:30]
    match=' OR '.join('"'+w.replace('"','""')+'"' for w in expressions)
    with db.connect() as c:
        c.create_function('visible',1,lambda pk:int(pk in allowed))
        lexical=[]
        if match:
            lexical=[dict(r) for r in c.execute('''SELECT rowid,record_id,version,locator,text,bm25(chunks) score FROM chunks
                WHERE chunks MATCH ? AND visible(record_id)=1 ORDER BY score LIMIT 40''',(match,))]
        ranked={r['rowid']:{**r,'rank_score':1/(60+i)} for i,r in enumerate(lexical,1)}
        mode='local-keyword'
        if semantic:
            try:
                import numpy as np
                vec=np.asarray(next(embedder(db.settings).query_embed(query)),dtype=np.float32)
                vec/=max(float(np.linalg.norm(vec)),1e-12)
                candidates=[]
                cursor=c.execute('''SELECT c.rowid,c.record_id,c.version,c.locator,c.text,e.vector FROM chunks c
                                     JOIN embeddings e ON c.rowid=e.chunk_id WHERE visible(c.record_id)=1 AND e.model=?''',(MODEL,))
                while rows:=cursor.fetchmany(256):
                    matrix=np.stack([np.frombuffer(r['vector'],dtype=np.float32) for r in rows])
                    for r,score in zip(rows,matrix@vec):
                        candidates.append((float(score),{k:r[k] for k in ('rowid','record_id','version','locator','text')}))
                    candidates=sorted(candidates,key=lambda x:x[0],reverse=True)[:40]
                for i,(_,r) in enumerate(candidates,1):
                    pk=r['rowid'];ranked.setdefault(pk,{**r,'rank_score':0});ranked[pk]['rank_score']+=1/(60+i)
                if candidates:mode='local-hybrid'
            except (ImportError,ValueError,RuntimeError,OSError):pass
        result=sorted(ranked.values(),key=lambda r:r['rank_score'],reverse=True)
    output=[];per_record={}
    for row in result:
        rid=row['record_id']
        if per_record.get(rid,0)>=2:continue
        record=access.record(rid)
        output.append({'record_id':rid,'version':record['version'],'title':record['title'],'kind':record['kind'],
                       'project_id':record['project_id'],'locator':row['locator'],'text':row['text'],'score':round(row['rank_score'],5)})
        per_record[rid]=per_record.get(rid,0)+1
        if len(output)>=limit:break
    return {'items':output,'mode':mode,'total':len(result)}
