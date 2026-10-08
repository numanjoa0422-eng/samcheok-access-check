# 첨부 확인 (2026-10-05)

삼척고 첨부 문서 확인을 추가했습니다. collect_boards.py를 실행하면 공고 저장 뒤 read_attachments.py가 최대 3개 공고를 확인합니다. 같은 공고만 반복하지 않도록 첨부 확인 시각이 오래된 순서입니다. 공고당 최대 첨부 3개, 수집 요청 예산·robots.txt 확인을 유지합니다.

## 실행

- Python 의존성: `python -m pip install -r requirements-attachments.txt`
- 이미지 OCR: tesseract-ocr와 한국어 데이터(tesseract-ocr-kor), PDF 변환: poppler-utils
- 일반 수집: `python scripts/collect_boards.py`
- 특정 공고 재확인: `python scripts/read_attachments.py --id board:samchokhs:9654703 --limit 1`
- 워크플로에서는 `collect_boards.py --skip-attachments` 뒤 별도 첨부 단계를 실행합니다.

## 지원 범위와 한계

공식 페이지의 첨부 영역에서 URL을 확인하고 학교의 htmlDocTransView → selectDocFileInfoCk.do → StreamDocs 주소를 해석합니다. 미리보기의 문서 글자층/직접 제공된 문서가 있으면 읽고, 실행 화면만 반환하면 원본 다운로드로 이어집니다. 동적 StreamDocs의 내부 화면을 자동 조작하는 기능까지 구현한 것은 아닙니다.

원본 HWP 본문, HWPX/DOCX 텍스트, PDF 텍스트, PNG/JPEG/GIF 및 이미지 PDF의 한국어 OCR을 지원합니다. OCR 도구가 없으면 실패 사유를 남깁니다. HWP/HWPX 내부 삽입 이미지·도형은 추출하지 않습니다. PDF는 최대20쪽 텍스트, 이미지 PDF는 최대6쪽 OCR 한도입니다. 글자 추출은 표의 배열·의미·날짜·대상 검증을 뜻하지 않습니다. 추출만으로 필드를 자동 확정하지 않습니다.

첨부는 기본적으로 원 게시판과 같은 출처만 처리합니다. 미리보기 준비 요청은 학교 버튼과 같은 공개 조회용 POST이며 다른 기능·로그인·보호 해제는 실행하지 않습니다. HTTP200의 HTML/JSON 안내문은 파일로 인정하지 않습니다.

attachment_evidence에 경로별 시도·성공시각·추출방법·범위·해시를 기록하고, 실패하면 이전 문서 텍스트와 성공시각을 보존합니다. attachment_content_signature는 첨부 내용 변경을 기존 검증 무효화·오늘 업데이트에 연결하며, 재확인시각만으로 업데이트를 만들지 않습니다.

## 실제 확인

2026-10-05 삼척고 ‘학자금 지원제도 안내’의 HWP 원본 읽기 성공. 미리보기 주소를 확보했으나 동적 뷰어 글자층은 확보하지 못해 원본으로 처리했습니다. 앞선 시도에서 robots 조회 시간초과·다운로드 안내문·브라우저502가 있었으나 이후 실제 파일을 받았습니다. 일시 실패를 영구 불가능으로 보지 않습니다.

학교 첨부는 2026년9월 기준 제도 안내이며, 2027학년도 신입생 국가장학금 1차 일정은 미정입니다. 통상 11~12월이라는 문구를 확정 접수기간으로 만들지 않았습니다. 꿈사다리 장학금 초5~고3 안내와 대학생 제도를 구분했습니다. 개별 최신 자격·접수는 별도 공식 공고 확인이 필요합니다.

GitHub용 파일까지 준비했지만 저장소 쓰기·배포는 하지 않았습니다. 운영 Actions에 이미 연결됐다고 주장하지 않습니다.
