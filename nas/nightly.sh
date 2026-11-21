#!/bin/sh
# DSM 작업 스케줄러에서 매일 01:00 (사용자: root) 실행:  sh /volume1/drive-index-nas/nightly.sh
#  1) 색인 내보내기(책별 JSONL + 그림 조각)를 드라이브 _drive_index_export/ 로 올림 (바뀐 것만, 최대 2시간)
#  2) 답안 골격 생성 (문제별로 이어서, 하루 무료 한도가 차면 다음 날 이어서)
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/env.sh"
PY="$(cat "$DIR/.python" 2>/dev/null || command -v python3)"
export RCLONE="$DIR/rclone"
LOG="$DIR/work/nightly.log"
mkdir -p "$DIR/work"
[ -f "$LOG" ] && tail -n 3000 "$LOG" > "$LOG.old" && mv "$LOG.old" "$LOG"
exec >>"$LOG" 2>&1
echo "===== $(date '+%Y-%m-%d %H:%M') 시작 ====="

sh "$DIR/api.sh" status | grep -q "실행 중" || sh "$DIR/api.sh" start      # 검색 API가 꺼져 있으면 다시 켬

API="$DRIVE_INDEX_DIR/api"
export SKEL_DIR="${SKEL_DIR:-/volume1/gdrive}"
if [ -f "$SKEL_DIR/gem_input.json" ]; then
  echo "[1] 드라이브 올리기는 Cloud Sync가 함 ($API/export → _drive_index_export, $SKEL_DIR ↔ gemini)"
  [ -d "$API/figs" ] && [ ! -d "$API/export/figs" ] && mv "$API/figs" "$API/export/figs"     # 예전 위치 정리
elif [ -d "$API/export" ]; then
  echo "[1] 드라이브로 색인 내보내기"
  OPT="--transfers 2 --checkers 4 --tpslimit 8 --max-duration 90m --retries 3 --stats-one-line --stats 10m"
  "$RCLONE" sync "$API/export" "$EXPORT_REMOTE" --include "/*.jsonl" --include "/figs/*.jpg" $OPT
  echo "    끝 (못 올린 그림은 내일 이어서)"
else
  echo "[1] 건너뜀: $API/export 없음 (PC에서 'NAS로 내보내기')"
fi

echo "[2] 답안 골격 생성"
"$PY" "$DIR/skeleton.py"
echo "===== $(date '+%H:%M') 끝 ====="
# 로그 끝부분을 드라이브 gemini/_nas_status/ 에 올림 (NAS에 접속하지 않고도 결과 확인)
tail -n 300 "$LOG" > "$DIR/work/nightly_tail.log"
if [ -f "$SKEL_DIR/gem_input.json" ]; then
  mkdir -p "$SKEL_DIR/_nas_status" && cp "$DIR/work/nightly_tail.log" "$SKEL_DIR/_nas_status/nightly.log"
else
"$RCLONE" copyto "$DIR/work/nightly_tail.log" "${SKEL_REMOTE:-gdrive:건축시공기술사/_답안골격_교재그림/gemini}/_nas_status/nightly.log" >/dev/null 2>&1
fi
