import io
import json
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import read_attachments as a
import check_access as access

URL='https://samchok.gwe.hs.kr/boardCnts/view.do?boardSeq=9654703'
FILE='https://samchok.gwe.hs.kr/boardCnts/fileDown.do?fileSeq=abcd'
HTML='''<script>function htmlDocTransView(k){var a='/boardCnts/selectDocFileInfoCk.do';var b='/streamdocs/view/sd;streamdocsId=';}</script>
<div class="board-text"><div class="tit">공지</div><div class="viewBox">첨부를 확인하세요</div><div class="fieldBox"><a href="/boardCnts/fileDown.do?fileSeq=abcd">안내.hwpx</a><a href="javascript:" onclick="htmlDocTransView('abcd')">미리보기</a></div></div>'''
NOW='2026-10-05T14:00:00+09:00'

def hwpx(text='신청기간: 2026. 10. 5. ~ 2026. 10. 8. 대상: 삼척 시민'):
 b=io.BytesIO()
 with zipfile.ZipFile(b,'w') as z:z.writestr('Contents/section0.xml','<root><p><t>'+text+'</t></p></root>')
 return b.getvalue()

class Fake:
 def __init__(self, raw=None, error=None):self.raw=raw or hwpx();self.error=error;self.calls=[]
 def fetch(self,url):
  if '/streamdocs/' in url:return access.Response(200,url,{'content-type':'text/html'},b'<sd-root></sd-root>')
  self.calls.append(url)
  return access.Response(200,url,{'content-type':'application/octet-stream'},self.raw,error=self.error)
 def preview_info(self,url,key):
  return access.Response(200,url,{},b'{"fileCk":"Y","value":"document1"}')

class AttachmentTests(unittest.TestCase):
 def test_preview_handler_pairs_with_exact_download_key(self):
  self.assertEqual(a.discover(HTML,URL),[{'url':FILE,'preview_key':'abcd'}])
 def test_no_preview_url_guess_if_handler_absent(self):
  self.assertIsNone(a.discover(HTML.replace('/streamdocs/view/sd;streamdocsId=',''),URL)[0]['preview_key'])
 def test_navigation_and_external_links_are_not_document_inputs(self):
  s=HTML+'<a href="/fileDown.do">outside</a>'
  s=s.replace('</div></div>','<a href="https://other.example/file">outside</a></div></div>')
  self.assertEqual(len(a.discover(s,URL)),1)
 def test_200_html_and_json_do_not_count_as_downloaded_document(self):
  for b in [b'<html>forbidden</html>',b'{"error":"file not found"}',b'',b'not file']:
   with self.assertRaises(ValueError):a.extract(b)
 def test_hwpx_text_reads_named_document_only(self):
  r=a.extract(hwpx());self.assertIn('삼척 시민',r['text']);self.assertEqual(r['method'],'xml_document_text')
 def test_generic_zip_is_not_hwpx(self):
  b=io.BytesIO()
  with zipfile.ZipFile(b,'w') as z:z.writestr('other.xml','<p>Fake</p>')
  with self.assertRaises(ValueError):a.extract(b.getvalue())
 def test_success_does_not_assign_eligibility_or_registration(self):
  r=a.enrich({'url':URL,'target':None,'status':'확인필요'},Fake(),NOW,html=HTML)
  self.assertIsNone(r['target']);self.assertEqual(r['status'],'확인필요')
  self.assertFalse(r['attachment_evidence']['fields_verified'])
  self.assertEqual(r['attachment_evidence']['status'],'extracted')
 def test_recheck_clock_does_not_change_content_signature(self):
  r=a.enrich({'url':URL},Fake(),NOW,html=HTML)
  s=a.enrich(r,Fake(),'2026-10-06T08:00:00+09:00',html=HTML)
  self.assertEqual(r['attachment_content_signature'],s['attachment_content_signature'])
 def test_changed_bytes_change_signature(self):
  r=a.enrich({'url':URL},Fake(),NOW,html=HTML)
  s=a.enrich(r,Fake(hwpx('다른 공고 날짜 2026. 11. 1.')) ,NOW,html=HTML)
  self.assertNotEqual(r['attachment_content_signature'],s['attachment_content_signature'])
 def test_failed_read_preserves_old_text_and_success_time(self):
  r=a.enrich({'url':URL},Fake(),NOW,html=HTML)
  old=r['attachment_evidence']['documents'][0]['text']
  s=a.enrich(r,Fake(error='timeout'),'2026-10-06T08:00:00+09:00',html=HTML)
  e=s['attachment_evidence'];self.assertEqual(e['status'],'failed')
  self.assertEqual(e['documents'][0]['status'],'stale');self.assertEqual(e['documents'][0]['text'],old)
  self.assertEqual(e['last_success_at'],NOW)
  self.assertEqual(r['attachment_content_signature'],s['attachment_content_signature'])
 def test_error_page_does_not_erase_previous_documents(self):
  r=a.enrich({'url':URL},Fake(),NOW,html=HTML)
  s=a.enrich(r,Fake(),NOW,html='<html>오류 안내</html>')
  self.assertEqual(len(s['attachment_evidence']['documents']),1)
  self.assertEqual(s['attachment_evidence']['documents'][0]['status'],'stale')
 def test_preview_shell_is_not_document(self):
  with self.assertRaises(ValueError):a.read_preview(Fake(),'https://samchok.gwe.hs.kr/streamdocs/view/sd;streamdocsId=x')
 def test_preview_text_layer_is_read_without_toolbar(self):
  class Viewer(Fake):
   def fetch(self,url):return access.Response(200,url,{'content-type':'text/html'},'<header>잘못된 모집중 문구</header><div class="textLayer">삼척시민 행사 신청기간 2026. 10. 5. ~ 10. 8.</div>'.encode())
  out=a.read_preview(Viewer(),'https://samchok.gwe.hs.kr/preview')
  self.assertIn('삼척시민',out['text']);self.assertNotIn('모집중',out['text'])
 def test_actual_school_hwp_fixture(self):
  p=Path(__file__).parents[2]/'source-verification/youth/348-attachment-1.hwp'
  if not p.exists():self.skipTest('로컬 실제 문서 fixture 미포함')
  try:import olefile
  except ImportError:self.skipTest('olefile 미설치')
  r=a.extract(p.read_bytes());self.assertTrue(len(r['text'])>100);self.assertEqual(r['method'],'hwp_body_text')

if __name__=='__main__':unittest.main()
