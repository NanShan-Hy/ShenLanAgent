import json,re
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont
from aiworkbench.documents import extract
root=Path('/home/nanshan/workspace/data/ai-workbench/dev/ocr-evaluation');root.mkdir(parents=True,exist_ok=True,mode=0o700)
font=ImageFont.truetype('/home/nanshan/workspace/tools/fonts/noto-cjk/20230817/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',38)
texts=['工业乙醇 纯度99% 单位kg','订单金额1380分 数量2','API鉴权失败 状态401','项目成员只读 不可编辑','源码存放projects 配置存放config','真实配置权限600 敏感目录700','扫描件低置信度需要复核','报价单价690分 数量2公斤','知识记忆确认后生效','备份校验SHA256 保留历史']
def norm(s):return re.sub(r'\s+','',re.sub(r'\[区域 [^\]]+\]','',s))
def distance(a,b):
 row=list(range(len(b)+1))
 for i,x in enumerate(a,1):
  new=[i]
  for j,y in enumerate(b,1):new.append(min(new[-1]+1,row[j]+1,row[j-1]+(x!=y)))
  row=new
 return row[-1]
results=[]
for i,text in enumerate(texts):
 img=Image.new('RGB',(1300,140),'white');ImageDraw.Draw(img).text((35,35),text,font=font,fill='black');path=root/f'clear-{i+1}.png';img.save(path);path.chmod(0o600)
 pages,review=extract(path);actual='\n'.join(t for _,t in pages);errors=distance(norm(text),norm(actual))
 results.append({'source':text,'recognized':actual,'character_errors':errors,'characters':len(norm(text)),'review_required':review})
report={'dataset':'10张合成清晰印刷图片，不代表低质量扫描件效果','character_error_rate':sum(r['character_errors'] for r in results)/sum(r['characters'] for r in results),'samples':results}
path=root/'evaluation.json';path.write_text(json.dumps(report,ensure_ascii=False,indent=2));path.chmod(0o600)
print('清晰印刷样本字符错误率:',report['character_error_rate']);assert report['character_error_rate']<=.05
