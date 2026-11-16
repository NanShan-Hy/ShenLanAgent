"""Explicit setup only: fetch public model artifacts, never project documents."""
import hashlib
import json
import os
from pathlib import Path

os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
os.environ['HF_HUB_DISABLE_XET']='1'
ROOT=Path('/home/nanshan/workspace/tools/ai-models')
ROOT.mkdir(parents=True,exist_ok=True)
from fastembed import TextEmbedding
model=TextEmbedding('BAAI/bge-small-zh-v1.5',cache_dir=str(ROOT/'embeddings'),threads=1)
vector=next(model.embed(['本地知识库测试']))
print('本地嵌入模型维度:',len(vector),flush=True)
from rapidocr import RapidOCR
import numpy as np
ocr=RapidOCR(params={'Global.model_root_dir':str(ROOT/'ocr'),'EngineConfig.onnxruntime.intra_op_num_threads':1,'EngineConfig.onnxruntime.inter_op_num_threads':1})
ocr(np.full((80,300,3),255,dtype=np.uint8))
manifest={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in ROOT.rglob('*') if p.is_file() and p.name!='manifest.json' and not p.name.endswith('.lock')}
(ROOT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
print('已记录模型SHA-256:',len(manifest),'文件',flush=True)
