#!/usr/bin/env python3
"""Read public notice attachments with robots checks, bounded extraction and provenance.
Text extraction is NOT field verification, eligibility, or proof of open registration.
"""
import argparse
import copy
import hashlib
import http.cookiejar
import io
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

from collect_boards import BudgetClient, ROOT, KST, atomic_write, public_url, ScopedNotice
import check_access as access

MAX_TEXT = 60000
MAX_UNPACKED = 20 * 1024 * 1024
MAX_FILES = 3


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def clean(text):
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text).strip()


class AttachmentClient(BudgetClient):
    def __init__(self, seconds=240, source_seconds=65):
        super().__init__(seconds, source_seconds)
        self.opener = urllib.request.build_opener(access.NoRedirect(),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def raw_get(self, url):
        # Never forward a page reference across origins on redirects.
        self.opener.addheaders = [(k,v) for k,v in self.opener.addheaders
            if k.lower()!='referer' or urllib.parse.urlsplit(v).netloc==urllib.parse.urlsplit(url).netloc]
        return super().raw_get(url)

    def preview_info(self, url, key):
        # This public site's observed preview button performs a read-only POST.
        allowed, note = self.robots_policy(url)
        if not allowed:
            return access.Response(None, url, policy=note)
        self.within_budget(lambda: access.time.sleep(max(0,
            self.request_intervals.get(urllib.parse.urlsplit(url).scheme+'://'+urllib.parse.urlsplit(url).netloc,
                access.MIN_REQUEST_INTERVAL) - (time.monotonic()-self.last_request))))
        self.last_request = time.monotonic()
        req = urllib.request.Request(url, data=urllib.parse.urlencode({'fileKey': key}).encode(),
            headers={'User-Agent': access.USER_AGENT, 'Accept': 'application/json',
                     'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            with self.opener.open(req, timeout=max(.1, min(8, self.remaining()))) as r:
                raw = r.read(access.MAX_BYTES+1)
                if len(raw)>access.MAX_BYTES:
                    raise ValueError('응답 크기 제한 초과')
                return access.Response(r.status, r.url, {k.lower():v for k,v in r.headers.items()}, raw)
        except Exception as exc:
            return access.Response(None, url, error=str(exc))


def discover(html, notice_url, parser='school_cms'):
    """Only article file links, and the site's observed preview handler; no JS eval."""
    page = ScopedNotice(html, parser)
    urls = [u for href in page.attachments if (u := public_url(href, notice_url, same_origin=True))]
    supported = ('/boardCnts/selectDocFileInfoCk.do' in html and
                 '/streamdocs/view/sd;streamdocsId=' in html)
    keys = set(re.findall(r"htmlDocTransView\(\s*['\"]([a-zA-Z0-9_-]+)['\"]\s*\)", html))
    files=[]
    for url in dict.fromkeys(urls):
        key = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get('fileSeq', [None])[0]
        files.append({'url':url, 'preview_key':key if supported and key in keys else None})
    return files


def inflate(raw):
    d = zlib.decompressobj(-15)
    out = d.decompress(raw, MAX_UNPACKED+1)
    if len(out)>MAX_UNPACKED or d.unconsumed_tail:
        raise ValueError('압축 해제 한도 초과')
    return out


def hwp_text(raw):
    import olefile
    out=[]
    with olefile.OleFileIO(io.BytesIO(raw)) as ole:
        if not ole.exists('FileHeader'):
            raise ValueError('HWP 파일이 아닌 OLE 문서')
        header=ole.openstream('FileHeader').read(256)
        flags=struct.unpack_from('<I',header,36)[0]
        if flags & 6:
            raise ValueError('암호화/배포용 HWP는 자동 해제하지 않음')
        paths=sorted((p for p in ole.listdir() if p[0]=='BodyText' and p[-1].startswith('Section')),
                     key=lambda p:int(re.sub(r'\D','',p[-1]) or 0))
        total=0
        for path in paths:
            data=ole.openstream(path).read(MAX_UNPACKED+1)
            if len(data)>MAX_UNPACKED:raise ValueError('HWP 스트림 크기 제한')
            data=inflate(data) if flags&1 else data
            total+=len(data)
            if total>MAX_UNPACKED:raise ValueError('HWP 전체 크기 제한')
            i=0
            while i+4<=len(data):
                v=struct.unpack_from('<I',data,i)[0];i+=4;tag=v&1023;size=v>>20
                if size==4095:
                    if i+4>len(data):raise ValueError('손상된 HWP 레코드')
                    size=struct.unpack_from('<I',data,i)[0];i+=4
                if i+size>len(data):raise ValueError('손상된 HWP 레코드 길이')
                chunk=data[i:i+size];i+=size
                if tag==67:
                    # HWP inline/extended controls occupy 8 UTF-16 code units.
                    units=struct.unpack('<'+'H'*(len(chunk)//2),chunk[:len(chunk)//2*2]);j=0;chars=[]
                    while j<len(units):
                        u=units[j]
                        if u in {1,2,3,4,5,6,7,8,9,11,12,14,15,16,17,18,19,20,21,22,23}:
                            chars.append(' ');j+=8
                        else:
                            chars.append(chr(u) if u>=32 or u in {10,13} else ' ');j+=1
                    out.append(clean(''.join(chars)))
    return '\n'.join(out)


def xml_text(raw):
    out=[]
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        if sum(i.file_size for i in z.infolist())>MAX_UNPACKED:
            raise ValueError('문서 압축 해제 한도 초과')
        names=[n for n in z.namelist() if re.fullmatch(r'Contents/section\d+\.xml|word/document\.xml',n)]
        if not names:raise ValueError('지원하는 HWPX/DOCX가 아닌 ZIP')
        for name in sorted(names):
            source=z.read(name)
            if b'<!DOCTYPE' in source or b'<!ENTITY' in source:raise ValueError('문서 XML 엔티티 금지')
            tree=ET.fromstring(source)
            for node in tree.iter():
                if node.tag.split('}')[-1] == 'p':
                    out.append(' '.join(''.join(t.itertext()) for t in node.iter()
                        if t.tag.split('}')[-1]=='t'))
    return '\n'.join(out)


def ocr(raw, pdf=False):
    if not shutil.which('tesseract'):raise ValueError('OCR 실행 도구 미설치')
    langs=subprocess.run(['tesseract','--list-langs'],capture_output=True,text=True,timeout=8).stdout.splitlines()
    if 'kor' not in langs:raise ValueError('한국어 OCR 데이터 미설치')
    with tempfile.TemporaryDirectory() as d:
        p=Path(d)/('input.pdf' if pdf else 'input.img');p.write_bytes(raw)
        if pdf:
            if not shutil.which('pdftoppm'):raise ValueError('PDF 이미지 변환 도구 미설치')
            subprocess.run(['pdftoppm','-f','1','-l','6','-scale-to','2000','-png',str(p),str(Path(d)/'page')],
                           check=True,capture_output=True,timeout=45)
            images=sorted(Path(d).glob('page-*.png'))
        else:images=[p]
        out=[]
        for image in images:
            r=subprocess.run(['tesseract',str(image),'stdout','-l','kor+eng'],
                             check=True,capture_output=True,text=True,timeout=30)
            out.append(r.stdout)
        return '\n'.join(out)


def extract(raw, content_type=''):
    """Return text + method + coverage, rejecting HTTP-200 error/preview app shells."""
    if not raw:raise ValueError('빈 첨부 응답')
    head=raw[:300].lstrip().lower()
    if head.startswith((b'<',b'{',b'[')):
        raise ValueError('문서 대신 HTML/JSON/XML 안내 또는 뷰어 화면 응답')
    if raw.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
        value,method,coverage=hwp_text(raw),'hwp_body_text','본문 텍스트; 삽입 이미지·도형 미추출'
    elif raw.startswith(b'PK\x03\x04'):
        value,method,coverage=xml_text(raw),'xml_document_text','본문 텍스트; 삽입 이미지·도형 미추출'
    elif raw.startswith(b'%PDF'):
        from pypdf import PdfReader
        pdf=PdfReader(io.BytesIO(raw))
        if pdf.is_encrypted:raise ValueError('암호화 PDF')
        value='\n'.join(p.extract_text() or '' for p in list(pdf.pages)[:20]);method='pdf_text'
        coverage='첫 최대 20쪽 텍스트; 이미지·표 배치 미검증'
        if len(clean(value))<25:
            value=ocr(raw,True);method='ocr';coverage='첫 최대 6쪽 OCR; 숫자·대상·날짜 원문 대조 필요'
    elif raw.startswith((b'\x89PNG',b'\xff\xd8\xff',b'GIF87a',b'GIF89a')):
        value,method,coverage=ocr(raw),'ocr','이미지 OCR; 숫자·대상·날짜 원문 대조 필요'
    else:raise ValueError('지원하지 않거나 확인되지 않은 첨부 형식')
    value=clean(value)
    if len(value)<8:raise ValueError('읽을 수 있는 본문 글자 부족; 이미지·표 확인 필요')
    return {'text':value[:MAX_TEXT], 'method':method, 'coverage':coverage,
            'truncated':len(value)>MAX_TEXT,'text_sha256':digest(value.encode()),'content_sha256':digest(raw)}


class PreviewDocument(HTMLParser):
    """Read explicit document text layers; never promote toolbar/app-shell text."""
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.depth=0; self.scopes=[]; self.parts=[]; self.links=[]
        self.feed(html)
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs); self.depth+=1
        if set(attrs.get('class','').split()) & {'textLayer','page-text','document-text'}:
            self.scopes.append(self.depth)
        if tag in {'iframe','embed','object'}:
            u=attrs.get('src') or attrs.get('data') or ''
            if re.search(r'\.pdf(?:[?#]|$)',u,re.I):self.links.append(u)
        if tag in {'img','br','hr','input','meta','link','embed'}:self.handle_endtag(tag)
    def handle_endtag(self,tag):
        self.scopes=[d for d in self.scopes if d<self.depth];self.depth=max(0,self.depth-1)
    def handle_data(self,value):
        if self.scopes:self.parts.append(value)


def read_preview(client,url):
    response=client.fetch(url);ok,note=access.base_verdict(response)
    if ok!=access.SUCCESS:raise ValueError(note)
    if response.raw.startswith((b'%PDF',b'PK\x03\x04',b'\xd0\xcf',b'\x89PNG',b'\xff\xd8')):
        return extract(response.raw,response.headers.get('content-type',''))
    doc=PreviewDocument(access.decode(response))
    value=clean(' '.join(doc.parts))
    if len(value)>=20:
        return {'text':value[:MAX_TEXT],'method':'preview_text','coverage':'문서 미리보기 글자층; 이미지·표 배치 미검증',
                'truncated':len(value)>MAX_TEXT,'text_sha256':digest(value.encode()),'content_sha256':digest(value.encode())}
    for ref in doc.links[:1]:
        link=public_url(ref,url,same_origin=True)
        if not link:continue
        pdf=client.fetch(link);ok,note=access.base_verdict(pdf)
        if ok!=access.SUCCESS:raise ValueError(note)
        return extract(pdf.raw,pdf.headers.get('content-type',''))
    raise ValueError('미리보기 실행 화면만 수신; 문서 글자층/변환 문서 미확보')


def enrich(record, client, now, parser='school_cms', html=None, evidence_dir=None):
    result=copy.deepcopy(record);prior=record.get('attachment_evidence',{});attempts=[];docs=[]
    result['attachment_evidence']=evidence={'last_attempt_at':now,'status':'failed','attempts':attempts,
        'documents':docs,'fields_verified':False}
    try:
        if html is None:
            resp=client.fetch(record['url']);verdict,note,page=access.html_page(resp)
            if verdict!=access.SUCCESS:raise ValueError(note)
            html=access.decode(resp)
        page=ScopedNotice(html,parser)
        if not page.body_seen:raise ValueError('공고 본문 범위 확인 실패; 오류 화면 가능')
        # Preserve the same public-page session and ordinary same-origin Referer as its links.
        if hasattr(client, 'opener'):
            client.opener.addheaders = [('Referer', record['url'])]
        files=discover(html,record['url'],parser)
        if not files:raise ValueError('본문 첨부 링크/미리보기 경로 확인 실패')
        # Links are evidence only; unchanged article facts/signatures are not rewritten here.
        evidence['found_count']=len(files);evidence['limited']=len(files)>MAX_FILES
        for item in files[:MAX_FILES]:
            doc={'url':item['url'],'status':'failed','attempts':[]}
            if item['preview_key']:
                endpoint=urllib.parse.urljoin(record['url'],'/boardCnts/selectDocFileInfoCk.do')
                try:
                    response=client.preview_info(endpoint,item['preview_key'])
                    ok,note=access.base_verdict(response)
                    if ok!=access.SUCCESS:raise ValueError(note)
                    data=json.loads(access.decode(response));file_id=str(data.get('value','')).replace('"','')
                    if data.get('fileCk')!='Y' or not re.fullmatch(r'[a-zA-Z0-9_-]{1,200}',file_id):
                        raise ValueError('미리보기 문서 식별자 확인 실패')
                    doc['preview_url']=urllib.parse.urljoin(record['url'],'/streamdocs/view/sd;streamdocsId='+file_id)
                    doc.update(read_preview(client,doc['preview_url']),status='extracted',last_success_at=now)
                    doc['attempts'].append({'route':'preview','status':'extracted'})
                except Exception as exc:
                    doc['attempts'].append({'route':'preview','status':'failed','reason':str(exc)})
            if doc['status']=='extracted':
                docs.append(doc)
                continue
            try:
                response=client.fetch(item['url']);ok,note=access.base_verdict(response)
                if ok!=access.SUCCESS:raise ValueError(note)
                extracted=extract(response.raw,response.headers.get('content-type',''))
                doc.update(extracted,status='extracted',last_success_at=now)
                doc['attempts'].append({'route':'original','status':'extracted'})
                if evidence_dir:
                    directory=Path(evidence_dir);directory.mkdir(parents=True,exist_ok=True)
                    (directory/(extracted['content_sha256']+'.bin')).write_bytes(response.raw)
            except Exception as exc:
                doc['attempts'].append({'route':'original','status':'failed','reason':str(exc)})
            docs.append(doc)
        succeeded=[d for d in docs if d['status']=='extracted']
        evidence['status']='extracted' if len(succeeded)==len(files) else 'partial' if succeeded else 'failed'
        if succeeded:
            result['attachment_text_extracted'] = True
            result['attachments_downloaded'] = any(any(a.get('route')=='original' and a.get('status')=='extracted' for a in d['attempts']) for d in succeeded)
            result['review_reason'] = record.get('review_reason','').replace('첨부파일 내용 미추출',
                '첨부 글자 추출; 삽입 이미지·표 의미·미추출 첨부 확인 필요')
            evidence['last_success_at']=now
            evidence['content_signature']=digest(json.dumps(sorted((d['url'],d['content_sha256']) for d in succeeded)).encode())
    except Exception as exc:
        attempts.append({'route':'notice','status':'failed','reason':str(exc)})
    # Preserve previous bytes/text AND their successful time, never silently refresh on a failed read.
    old_docs={d['url']:d for d in prior.get('documents',[]) if d.get('text') or d.get('preview_url')}
    current={d['url']:d for d in docs}
    for url,old in old_docs.items():
        if url not in current or current[url].get('status')!='extracted':
            kept=copy.deepcopy(old);kept['status']='stale' if kept.get('text') else 'failed';kept['last_attempt_at']=now
            kept['attempts']=current.get(url,{}).get('attempts',[])
            if url in current:docs.remove(current[url])
            docs.append(kept)
    if not evidence.get('last_success_at') and prior.get('last_success_at'):
        evidence['last_success_at']=prior['last_success_at']
    semantic = sorted((d['url'], d['content_sha256']) for d in docs if d.get('text') and d.get('content_sha256'))
    if semantic:
        result['attachment_content_signature'] = digest(json.dumps(semantic).encode())
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',type=Path,default=ROOT/'data/board-programs.json')
    p.add_argument('--source',default='samchokhs')
    p.add_argument('--id');p.add_argument('--limit',type=int,default=3)
    p.add_argument('--evidence-dir',type=Path,default=ROOT/'results/attachments')
    args=p.parse_args();data=json.loads(args.input.read_text());client=AttachmentClient()
    now=datetime.now(KST).isoformat();count=0
    ordered=sorted(enumerate(data['programs']),key=lambda pair:pair[1].get('attachment_evidence',{}).get('last_attempt_at',''))
    for i,record in ordered:
        if record.get('source_id')!=args.source or (args.id and record['id']!=args.id):continue
        if not record.get('attachment_urls'):continue
        if count>=args.limit:break
        client.begin_source()
        data['programs'][i]=enrich(record,client,now,evidence_dir=args.evidence_dir)
        e=data['programs'][i]['attachment_evidence'];print(record['title'],e['status'],flush=True);count+=1
    data['attachment_last_attempt_at']=now
    atomic_write(args.input,data)
    args.evidence_dir.mkdir(parents=True,exist_ok=True)
    atomic_write(args.evidence_dir/'latest.json',{'last_attempt_at':now,'attempted':count,
        'records':[{'id':r['id'],'title':r['title'],'evidence':r['attachment_evidence']} for r in data['programs'] if 'attachment_evidence' in r]})

if __name__=='__main__':main()
