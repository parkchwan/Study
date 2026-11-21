# 학습 앱 연동 — NAS 설치 안내 (DS216play)

이 기능으로 NAS가 하는 일:

1. **검색 API**
   - `http://192.168.0.250:8765/drive-index/api/search?q=검색어&k=8` → JSON
   - 같은 집 와이파이나 Tailscale에서만 받고, 인증은 없음
2. **드라이브 내보내기** (매일 01:00)
   - 책별 JSONL과 그림 조각(figs/)을 `건축시공기술사/_drive_index_export/` 에 올림
3. **답안 골격 자동 생성** (매일 01:00, 1번 다음)
   - 결과는 `gemini/out/골격_NNN회.json`
   - 하루 무료 한도가 차면 다음 날 이어서 함

그림 자르기와 1200px 페이지 이미지는 NAS가 못 하므로 **PC가 만들어서 NAS로 보냄**:

- 저장 위치: `\\PCHSNAS\web\drive-index\api\`
- 언제: 'NAS로 내보내기'를 누르거나 색인이 끝날 때
- 처음 한 번은 수천 쪽이라 오래 걸리지만, 그 뒤엔 바뀐 쪽만 만듦

## 0. PC에서 먼저

새 버전 ZIP으로 실행한 뒤 화면의 **NAS로 내보내기**를 한 번 누릅니다. 그림 조각은 뒤에서 계속 만들어져요.

다 됐는지 확인하는 방법:

- `\\PCHSNAS\web\drive-index\api\pages.jsonl` 이 있으면 됨
- `api\meta.json` 의 `pending_images` 가 0이면 그림 조각까지 끝난 것

## 1. 설치 (SSH 한 번)

1. **Python 3 준비**
   - DSM 7에는 보통 `python3` 이 들어 있어요. 설치 스크립트가 자동으로 찾아요.
   - 없다고 나오면 패키지 센터에서 **Python 3.9**를 설치하세요.
2. **SSH 켜기**: 제어판 → 터미널 및 SNMP → SSH 서비스 활성화 (설치가 끝나면 다시 끔)
3. **설치 파일 올리기**: ZIP 속 `nas` 폴더를 `\\PCHSNAS\web\drive-index-nas-setup` 로 복사합니다.
4. **설치 실행**: PC 명령 프롬프트에서 아래를 실행합니다.
   ```
   ssh 관리자계정@192.168.0.250
   sudo sh /volume1/web/drive-index-nas-setup/install.sh
   ```
   - 설치 위치는 `/volume1/drive-index-nas` 예요. web 폴더 밖이라 키와 드라이브 인증이 웹으로 새지 않아요.
   - rclone(ARM용)도 자동으로 받아요.
5. **Gemini API 키 넣기**:
   ```
   sudo sh /volume1/drive-index-nas/setkey.sh
   ```
   - 키는 여기에만 입력하세요. 채팅이나 드라이브에는 절대 넣지 마세요.
6. **설치용 폴더 지우기**: `\\PCHSNAS\web\drive-index-nas-setup` 을 삭제합니다.

## 2. 드라이브 연결 (rclone, 처음 한 번)

NAS에는 브라우저가 없어서 **PC에서 로그인한 결과를 붙여 넣는** 방식이에요.

1. NAS(SSH)에서 실행: `sudo /volume1/drive-index-nas/rclone config`
2. 질문마다 이렇게 답합니다:

   | 질문 | 답 |
   |---|---|
   | 맨 처음 | `n` (새로 만들기) |
   | name | `gdrive` |
   | Storage | `drive` (목록 번호 대신 단어로 입력) |
   | client_id, client_secret | 그냥 Enter |
   | scope | `1` (전체 접근) |
   | service_account_file | Enter |
   | Edit advanced config? | `n` |
   | Use web browser to automatically authenticate? | **`n`** |

3. 그러면 `rclone authorize "drive" "eyJ..."` 한 줄이 나와요. 이 줄을 PC에서 실행합니다.
   - PC에서 https://rclone.org/downloads/ 의 Windows 64bit ZIP을 받아 압축을 풉니다.
   - 그 폴더에서 명령 프롬프트를 열고, NAS에 나온 줄을 그대로 붙여 넣어 실행합니다.
   - 브라우저가 열리면 구글 로그인 후 허용합니다.
4. PC 창에 나온 긴 토큰을 NAS 창에 붙여 넣습니다.
5. 나머지 질문 답하기:

   | 질문 | 답 |
   |---|---|
   | Shared Drive? | `n` |
   | 저장 확인 | `y` |
   | 끝 | `q` |

6. 확인: `sudo /volume1/drive-index-nas/rclone lsd "gdrive:건축시공기술사"` 를 실행해서 폴더 목록이 나오면 성공이에요.

## 3. 작업 스케줄러 (DSM 제어판 → 작업 스케줄러)

**매일 01:00 작업**

- 만드는 곳: 생성 → 예약된 작업 → 사용자 정의 스크립트
- 일반: 작업 `drive-index 야간`, 사용자 **root**
- 일정: 매일, 01:00
- 작업 설정 → 사용자 정의 스크립트:
  ```
  sh /volume1/drive-index-nas/nightly.sh
  ```

**부팅 시 검색 API 켜기**

- 만드는 곳: 생성 → 트리거된 작업 → 사용자 정의 스크립트
- 사용자 **root**, 이벤트 **부트업**
- 스크립트:
  ```
  sh /volume1/drive-index-nas/api.sh start
  ```

DSM 방화벽을 켜 두었다면 **8765 포트를 192.168.0.0/24 에서만** 허용하세요. 공유기 포트포워딩은 하지 마세요.

## 4. 확인

- **검색 API**: 브라우저에서 `http://192.168.0.250:8765/drive-index/api/search?q=콘크리트 타설속도&k=8`
- **상태**: `http://192.168.0.250:8765/drive-index/api/health`
- **야간 작업 기록**:
  - 로그: `/volume1/drive-index-nas/work/nightly.log`
  - 마지막 결과: `/volume1/drive-index-nas/work/last_run.json`
- **바로 한 번 돌려 보기**: `sudo sh /volume1/drive-index-nas/nightly.sh`
  - 골격 1문제만 시험하려면:
    ```
    sudo sh -c '. /volume1/drive-index-nas/env.sh; $(cat /volume1/drive-index-nas/.python) /volume1/drive-index-nas/skeleton.py --limit 1'
    ```

## 참고

- **Tailscale(외부)에서 API 쓰기**:
  - 실행: `sudo tailscale serve --bg --set-path /drive-index/api http://127.0.0.1:8765/drive-index/api`
  - 그러면 `https://pchsnas.tailcd7f80.ts.net/drive-index/api/search?q=...` 로 열려요.
  - 이 명령은 실제 NAS에서 아직 시험하지 못했어요.
- **claude.ai 아티팩트(학습 앱)에서는 이 API를 직접 부를 수 없어요.** https 페이지에서 집 내부 주소(http://192.168…)로 가는 요청은 브라우저가 막아요. 그래서 학습 앱은 드라이브의 `_drive_index_export` 를 쓰는 게 맞아요.
- **골격 생성 규칙**:
  - 결과는 `work/raw2/<문제id>.json` 에 문제마다 저장돼요. 이미 만든 문제는 다시 만들지 않아요.
  - 예전 골격(gen.ps1으로 만든 것)은 새로 만들어질 때까지 그대로 남아요.
- **골격 JSON에 새로 붙는 값**:

  | 필드 | 붙는 곳 | 뜻 |
  |---|---|---|
  | `src_img`, `src_ref` | fig·figs | 가장 잘 맞는 교재 그림 조각과 출처 (`figs/파일명`, `책 p.쪽`) |
  | `nosrc: true` | 목차 항목, 표, 그림 | 그 안의 키워드·칸·글자 중 절반 넘게 교재에서 못 찾음 |
  | `src_check` | 문제마다 | `{"ok": 찾은 개수, "ng": [못 찾은 말들]}` |
  | `refs` | 문제마다 | 참고한 교재 페이지 목록 |
