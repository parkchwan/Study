# Drive 시각자료 색인 (Drive Visual Index)

Google Drive의 **특정 폴더**를 지정하면, 그 폴더(하위 폴더 포함) 안의 모든 문서에서
**그림 · 표 · 플로우차트 · 차트 · 다이어그램 · 사진 · 스크린샷**을 찾아 색인하고,
검색 결과를 누르면 해당 **페이지를 2초 안에(보통 0.1초 이내)** 띄워 주는 웹 앱입니다.

## 지원 파일
- PDF, 이미지(PNG/JPG/GIF/WebP 등)
- Google 문서 / 슬라이드 / 스프레드시트 / 드로잉 (PDF로 내보내 처리)
- 업로드된 Word·PowerPoint·Excel (LibreOffice가 설치돼 있으면 PDF로 변환)

## 크레딧 최소화 구조 (Gemini + ChatGPT 함께 사용)
| 단계 | 하는 일 | 비용 |
|---|---|---|
| 1. 로컬 분석 | PyMuPDF로 이미지·벡터도형·표 감지, “그림 3.”, “Table 2” 같은 캡션 추출 | **0원** |
| 2. 필터링 | 그림/표/도형이 없는 텍스트 페이지는 AI에 보내지 않음 | 호출 자체를 줄임 |
| 3. Gemini 우선 | 시각자료 페이지만 768px JPEG로 줄여 **Gemini Flash-Lite**(가장 저렴)에 분석 요청 | 매우 저렴 |
| 4. ChatGPT 폴백 | Gemini가 한도초과(429)/오류면 자동으로 **OpenAI(gpt-4.1-nano, low detail)** 로 전환 | 저렴 |
| 5. 캐시 | 같은 이미지는 해시로 캐시 → 다시 묻지 않음 | 재색인 0원 |
| 6. 증분 색인 | Drive의 md5/수정시각이 같으면 다운로드·분석 모두 생략 | 재색인 0원 |
| 7. 예산 상한 | `VISION_MAX_CALLS`로 1회 색인당 AI 호출 수 제한 | 폭주 방지 |

`VISION_PROVIDER=none`으로 두면 AI 없이 로컬 분석만으로도 동작합니다(완전 무료, 정확도는 낮음).

## 2초 이내 표시 비결
- 색인할 때 페이지를 **WebP로 미리 렌더링**해 저장 (요청 시 변환 없음)
- 검색은 로컬 SQLite → 수 ms
- 카드에 마우스를 올리면 전체 페이지를 **미리 받아두고**, 클릭 시 썸네일 → 고해상도 순으로 즉시 표시
- 이미지에 1년 캐시 헤더 → 다시 열 때 네트워크 없이 표시
- 뷰어에 실제 표시 시간이 나옵니다 (테스트 환경 측정: 0.01초)

## 설치 & 실행
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # 키 입력
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
브라우저에서 http://localhost:8000 → 폴더 URL 붙여넣기 → **색인 시작**.
진행 중에도 결과가 점점 채워지고, 상단에 Gemini/OpenAI/캐시 호출 수가 표시됩니다.

### 1) Google Drive 인증 (둘 중 하나)
- **서비스 계정(권장)**: Google Cloud 콘솔 → Drive API 사용 설정 → 서비스 계정 생성 → JSON 키를
  `service_account.json`으로 저장 → 색인할 Drive 폴더를 서비스 계정 이메일(`...@...iam.gserviceaccount.com`)과 **공유**(뷰어 권한).
- **OAuth**: OAuth 클라이언트(데스크톱 앱) JSON을 `credentials.json`으로 저장하고 `.env`에
  `GOOGLE_OAUTH_CLIENT_FILE=credentials.json` 설정 → 첫 실행 시 브라우저 로그인.

### 2) AI 키
- Gemini: https://aistudio.google.com/apikey → `GEMINI_API_KEY`
- OpenAI: https://platform.openai.com/api-keys → `OPENAI_API_KEY`
- 둘 중 하나만 있어도 됩니다. 모델명은 `GEMINI_MODEL`, `OPENAI_MODEL`로 바꿀 수 있습니다.

## 구조
```
app/
  main.py      FastAPI 서버 (API + 정적 이미지)
  drive.py     Drive 폴더 재귀 탐색, 다운로드/PDF 내보내기
  extract.py   로컬 분석(무료): 렌더링, 표/그림/도형 감지, 캡션 추출
  vision.py    Gemini → OpenAI 라우터, 캐시, 호출 상한
  indexer.py   백그라운드 색인 작업 (증분)
  db.py        SQLite 색인/검색
  static/index.html  검색·뷰어 화면
tests/test_app.py    Drive/AI 없이 전체 파이프라인 테스트
```

## 테스트
```bash
python -m pytest -q
```
