"""One isolated, bounded document parse. stdin/stdout are pipes, never ordinary logs."""
import base64
import csv
import io
import json
import resource
import sys
import zipfile
from pathlib import Path


def parse(raw, name, selected=None):
    from PIL import Image
    Image.MAX_IMAGE_PIXELS=16000000
    ext=Path(name).suffix.lower();sections=[]
    def add(label,text='',image=None):
        if selected and label not in selected:return
        if len(text)>250000:raise ValueError('section_too_large')
        sections.append({'label':label,'text':text,**({'image':image} if image else {})})
    if ext in ('.png','.jpg','.jpeg','.webp'):
        with Image.open(io.BytesIO(raw)) as image:
            if getattr(image,'is_animated',False):raise ValueError('unsupported_file')
            image=image.convert('RGB');image.thumbnail((1024,1024));out=io.BytesIO();image.save(out,format='JPEG',quality=85)
            add('image',image=base64.b64encode(out.getvalue()).decode())
    elif ext=='.pdf':
        from pypdf import PdfReader
        reader=PdfReader(io.BytesIO(raw),strict=True)
        if reader.is_encrypted:raise ValueError('encrypted_file')
        if len(reader.pages)>100 and not selected:return {'error':'selection_required','page_count':len(reader.pages),'selection_format':'page:N'}
        pdf=None
        for i,page in enumerate(reader.pages):
            label='page:'+str(i+1)
            if selected and label not in selected:continue
            if len(sections)>=100:raise ValueError('selection_required')
            text=page.extract_text() or ''
            if text.strip():add(label,text)
            else:
                import pypdfium2 as pdfium
                if pdf is None:pdf=pdfium.PdfDocument(raw)
                if sum('image' in s for s in sections)>=4:raise ValueError('selection_required')
                bitmap=pdf[i].render(scale=min(1,1024/max(float(page.mediabox.width),float(page.mediabox.height))))
                image=bitmap.to_pil().convert('RGB');out=io.BytesIO();image.save(out,format='JPEG',quality=85)
                add(label,image=base64.b64encode(out.getvalue()).decode())
        if pdf is not None:pdf.close()
    elif ext in ('.docx','.xlsx'):
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            if len(z.infolist())>2000 or sum(x.file_size for x in z.infolist())>80*1024**2:
                raise ValueError('expanded_file_limit')
        if ext=='.docx':
            from docx import Document
            doc=Document(io.BytesIO(raw));text='\n'.join(p.text for p in doc.paragraphs)
            text+='\n'+'\n'.join('\t'.join(c.text for c in row.cells) for t in doc.tables for row in t.rows)
            add('document',text)
        else:
            from openpyxl import load_workbook
            import itertools
            import re
            book=load_workbook(io.BytesIO(raw),read_only=True,data_only=False,keep_links=False)
            cells=0
            for sheet in book:
                ranges=[]
                if selected:
                    for label in selected:
                        match=re.fullmatch(re.escape(sheet.title)+r':rows:(\d+)-(\d+)',label)
                        if match:
                            lo,hi=map(int,match.groups())
                            if not 1<=lo<=hi<=1000000 or hi-lo>=5000:raise ValueError('selection_required')
                            ranges.append((lo,hi,label))
                    if sheet.title in selected:ranges.append((1,sheet.max_row,sheet.title))
                    if not ranges:continue
                else:
                    if sheet.max_row*sheet.max_column>100000:
                        return {'error':'selection_required','available_sheets':[{'name':s.title,'rows':s.max_row,'columns':s.max_column} for s in book],'selection_format':'SheetName:rows:1-500'}
                    ranges=[(lo,min(lo+499,sheet.max_row),sheet.title+':rows:'+str(lo)+'-'+str(min(lo+499,sheet.max_row))) for lo in range(1,sheet.max_row+1,500)]
                for lo,hi,label in ranges:
                    lines=[]
                    for row in itertools.islice(sheet.iter_rows(values_only=True),lo-1,hi):
                        cells+=len(row)
                        if cells>100000:raise ValueError('selection_required')
                        lines.append('\t'.join('' if v is None else str(v) for v in row))
                    text='\n'.join(lines)
                    if selected and label in selected:sections.append({'label':label,'text':text})
                    else:add(label,text)
            book.close()
    else:
        try:text=raw.decode('utf-8-sig')
        except UnicodeDecodeError:text=raw.decode('gb18030')
        if '\0' in text:raise ValueError('unsupported_file')
        lines=text.splitlines();size=500
        for i in range(0,len(lines),size):add('lines:'+str(i+1)+'-'+str(min(i+size,len(lines))),'\n'.join(lines[i:i+size]))
    if not sections or not any(p.get('text','').strip() or p.get('image') for p in sections):raise ValueError('empty_selection')
    result={'sections':sections}
    if len(json.dumps(result).encode())>4*1024**2:return {'error':'selection_required','available_sections':[{'label':p['label'],'characters':len(p.get('text',''))} for p in sections]}
    return result


if __name__=='__main__':
    resource.setrlimit(resource.RLIMIT_AS,(512*1024**2,512*1024**2))
    resource.setrlimit(resource.RLIMIT_CPU,(12,12))
    try:
        raw=sys.stdin.buffer.read(20*1024**2+1)
        if len(raw)>20*1024**2:raise ValueError('file_too_large')
        result=parse(raw,sys.argv[1],json.loads(sys.argv[2]))
    except Exception as exc:
        code=str(exc) if type(exc) is ValueError and str(exc) in ('selection_required','section_too_large','encrypted_file','expanded_file_limit','unsupported_file','empty_selection','file_too_large') else 'file_parse_failed'
        result={'error':code}
    sys.stdout.write(json.dumps(result,ensure_ascii=False))
