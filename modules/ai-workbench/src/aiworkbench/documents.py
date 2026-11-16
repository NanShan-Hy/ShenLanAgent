"""Bounded, local-only document extraction. No macros or source execution."""
import os
import zipfile
from pathlib import Path

_ocr_engine=None
MAX_FILE=20*1024**2
TEXT_EXT={'.md','.txt','.json','.yaml','.yml','.py','.js','.ts','.html','.css','.sql','.toml','.ini','.xml','.csv','.java','.go','.rs','.sh'}
ALLOWED=TEXT_EXT|{'.pdf','.docx','.xlsx','.png','.jpg','.jpeg','.webp'}
EXCLUDED={'.git','.venv','venv','node_modules','__pycache__','.ssh','.codex','.npm','secrets','credentials','config','backups','archives','tools','downloads','tmp','licenses'}


def approved_path(path,roots):
    original=Path(path).expanduser()
    resolved=original.resolve()
    matching=[Path(root).resolve() for root in roots if resolved.is_relative_to(Path(root).resolve())]
    if not matching:raise ValueError('路径不在已登记来源根目录内')
    root=max(matching,key=lambda p:len(p.parts))
    relative=resolved.relative_to(root)
    visible=original.relative_to(root) if original.is_relative_to(root) else Path(original.name)
    if root.name in EXCLUDED or set(relative.parts)&EXCLUDED or set(visible.parts)&EXCLUDED:raise ValueError('凭据、配置、依赖或历史目录不进入知识库')
    if resolved.name.startswith('.') or resolved.suffix.lower() not in ALLOWED or resolved.name.endswith(('.key','.env')):raise ValueError('此类型不支持入库')
    if not resolved.is_file() or resolved.stat().st_size>MAX_FILE:raise ValueError('需要20MiB以内的普通文件')
    return resolved


def check_archive(path):
    with zipfile.ZipFile(path) as z:
        if len(z.infolist())>10000 or sum(i.file_size for i in z.infolist())>100*1024**2:raise ValueError('压缩文档展开体积过大')
        if any(i.flag_bits&1 for i in z.infolist()):raise ValueError('不支持加密文档')


def ocr_image(image):
    from rapidocr import RapidOCR
    import numpy as np
    global _ocr_engine
    if _ocr_engine is None:
        _ocr_engine=RapidOCR(params={'Global.model_root_dir':'/home/nanshan/workspace/tools/ai-models/ocr','EngineConfig.onnxruntime.intra_op_num_threads':1,'EngineConfig.onnxruntime.inter_op_num_threads':1})
    engine=_ocr_engine
    result=engine(np.asarray(image.convert('RGB')))
    if not result.txts:return '',True
    rows=[]
    for text,box in zip(result.txts,result.boxes):
        points=np.asarray(box);coords=[int(points[:,0].min()),int(points[:,1].min()),int(points[:,0].max()),int(points[:,1].max())]
        rows.append('[区域 '+','.join(map(str,coords))+'] '+text)
    text='\n'.join(rows)
    low=any(float(score)<.85 for score in result.scores)
    return text,low


def extract(path,progress=lambda text:None):
    path=Path(path);extension=path.suffix.lower();pages=[];review=False
    if extension in TEXT_EXT:
        text=path.read_text(encoding='utf-8-sig')
        for start in range(0,len(text.splitlines()),40):
            lines=text.splitlines();pages.append((f'行 {start+1}-{min(start+40,len(lines))}','\n'.join(lines[start:start+40])))
    elif extension=='.docx':
        check_archive(path)
        from docx import Document
        d=Document(path)
        pages=[(f'段落 {i+1}',p.text) for i,p in enumerate(d.paragraphs) if p.text.strip()]
        for i,table in enumerate(d.tables):
            pages.append((f'表格 {i+1}','\n'.join(' | '.join(cell.text for cell in row.cells) for row in table.rows)))
    elif extension=='.xlsx':
        check_archive(path)
        import openpyxl
        wb=openpyxl.load_workbook(path,read_only=True,data_only=False,keep_links=False)
        try:
            rows=0
            for sheet in wb:
                header=''
                for i,row in enumerate(sheet.iter_rows(values_only=True),1):
                    rows+=1
                    if rows>100000:raise ValueError('单次最多10万表格行')
                    text=' | '.join('' if v is None else str(v) for v in row[:100])
                    if i==1:header=text
                    if text.strip(' |'):pages.append((f'{sheet.title} · 行 {i}',('表头：'+header+'\n' if i>1 else '')+text))
        finally:wb.close()
    elif extension=='.pdf':
        import pypdfium2 as pdfium
        doc=pdfium.PdfDocument(path)
        try:
            if len(doc)>100:raise ValueError('单次PDF最多100页')
            for i in range(len(doc)):
                progress(f'解析第{i+1}/{len(doc)}页')
                page=doc[i];textpage=page.get_textpage()
                try:text=textpage.get_text_range()
                finally:textpage.close()
                if len(text.strip())<30:
                    bitmap=page.render(scale=min(2,2400/max(page.get_width(),page.get_height())))
                    try:
                        text,low=ocr_image(bitmap.to_pil());review=review or low
                    finally:bitmap.close()
                pages.append((f'页 {i+1}',text));page.close()
        finally:doc.close()
    elif extension in {'.png','.jpg','.jpeg','.webp'}:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS=16000000
        with Image.open(path) as image:
            if image.width*image.height>16000000:raise ValueError('图片超过1600万像素')
            image.thumbnail((2400,2400));text,review=ocr_image(image)
        pages=[('图片文字',text)]
    else:raise ValueError('不支持的文档类型')
    pages=[(loc,text) for loc,text in pages if text.strip()]
    if not pages:raise ValueError('没有提取到文字，请检查文件或OCR结果')
    if sum(len(t) for _,t in pages)>2000000:raise ValueError('提取文字超过单文档上限')
    return pages,review
