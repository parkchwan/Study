#!/bin/sh
# 검색 API 켜기/끄기:  sh api.sh start | stop | restart | status
DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$DIR/env.sh" ] && . "$DIR/env.sh"
PY="$(cat "$DIR/.python" 2>/dev/null || command -v python3)"
PID="$DIR/work/api.pid"; LOG="$DIR/work/api.log"
mkdir -p "$DIR/work"
running() { [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; }
case "$1" in
  start)
    if running; then echo "이미 실행 중 (pid $(cat "$PID"))"; exit 0; fi
    [ -f "$LOG" ] && tail -n 500 "$LOG" > "$LOG.old" && mv "$LOG.old" "$LOG"
    nohup "$PY" "$DIR/api_server.py" >>"$LOG" 2>&1 &
    echo $! > "$PID"; sleep 3
    if running; then echo "검색 API 켜짐: http://$(hostname -i 2>/dev/null | cut -d' ' -f1):${API_PORT:-8765}/drive-index/api/search?q=..."; else echo "실패 — $LOG 확인"; tail -n 20 "$LOG"; exit 1; fi ;;
  stop) running && kill "$(cat "$PID")"; rm -f "$PID"; echo "꺼짐" ;;
  restart) sh "$0" stop; sleep 1; sh "$0" start ;;
  status) if running; then echo "실행 중 (pid $(cat "$PID"))"; else echo "꺼져 있음"; fi ;;
  *) echo "사용법: sh $0 start|stop|restart|status" ;;
esac
