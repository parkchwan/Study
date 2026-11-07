"""새 버전 폴더에서 처음 실행할 때, 예전 버전 폴더의 설정(.env)과 색인 결과(data)를 자동으로 가져옴.

ZIP을 새로 받아 압축을 풀 때마다 .env와 data를 손으로 옮기지 않아도 됨.
찾는 범위: 이 폴더의 상위 1~2단계 아래에 있는 다른 앱 폴더 (예: 다운로드 폴더 안의 예전 압축 해제 폴더)
"""
import shutil
import sys
from pathlib import Path

here = Path(__file__).resolve().parent


def candidates():
    seen = set()
    for root in (here.parent, here.parent.parent):
        for pattern in ("*/run.py", "*/*/run.py"):
            for run in root.glob(pattern):
                d = run.parent.resolve()
                if d == here or d in seen or not (d / ".env").exists():
                    continue
                seen.add(d)
                db = d / "data" / "index.sqlite3"
                yield (db.stat().st_mtime if db.exists() else (d / ".env").stat().st_mtime), d


def main() -> None:
    if (here / ".env").exists():
        return
    found = sorted(candidates(), reverse=True)
    if not found:
        return
    old = found[0][1]
    print(f"※ 예전 버전 폴더에서 설정을 가져옵니다: {old}")
    shutil.copy2(old / ".env", here / ".env")
    print("   - .env (API 키 등 설정) 복사 완료")
    if (old / "data").exists() and not (here / "data").exists():
        try:
            shutil.move(str(old / "data"), str(here / "data"))     # 같은 드라이브면 즉시 끝남
            print("   - data (색인 결과) 옮기기 완료")
        except OSError as e:
            print(f"   - data 옮기기 실패: {e}  → 예전 폴더의 data를 직접 옮겨 주세요")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:      # 가져오기에 실패해도 앱 실행은 계속
        print(f"※ 예전 설정 가져오기 건너뜀: {e}", file=sys.stderr)
