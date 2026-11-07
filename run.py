"""앱 실행기: 빈 포트를 찾아 서버를 켜고 브라우저를 엽니다.

8000번이 이미 쓰이고 있으면(예: 이전 창이 아직 열려 있음) 8001, 8002 … 로 자동 변경.
"""
import socket
import sys
import threading
import time
import webbrowser


def free_port(start: int = 8000, tries: int = 20) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("사용할 수 있는 포트가 없습니다. 열려 있는 다른 검은 창을 닫고 다시 실행하세요.")


def main() -> None:
    port = free_port()
    if port != 8000:
        print(f"※ 8000번 포트가 사용 중입니다 (이전 창이 열려 있을 수 있어요). {port}번으로 시작합니다.")
    url = f"http://localhost:{port}"
    print(f"[3/3] 서버 시작: {url}  (이 창을 닫으면 종료됩니다)")
    if "--no-browser" not in sys.argv:
        threading.Thread(target=lambda: (time.sleep(2), webbrowser.open(url)), daemon=True).start()
    import uvicorn
    # 화면 갱신용 요청(GET /api/status 등)은 찍지 않음 → 경고·오류가 잘 보이게
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, access_log=False)


if __name__ == "__main__":
    main()
