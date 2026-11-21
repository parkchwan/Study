#!/bin/sh
# Gemini API 키를 env.sh 에 넣음 (화면에 안 보이게 입력):  sudo sh /volume1/drive-index-nas/setkey.sh
DIR="$(cd "$(dirname "$0")" && pwd)"; F="$DIR/env.sh"
printf "Gemini API 키를 붙여넣고 Enter (입력해도 화면에 안 보여요): "
stty -echo 2>/dev/null; read K; stty echo 2>/dev/null; echo
[ -n "$K" ] || { echo "비어 있어서 그만둡니다"; exit 1; }
grep -v '^export GEMINI_API_KEY=' "$F" > "$F.tmp"
printf 'export GEMINI_API_KEY="%s"\n' "$K" >> "$F.tmp"
mv "$F.tmp" "$F"; chmod 600 "$F"
echo "저장했어요 ($F, 관리자만 읽기)"
sh "$DIR/api.sh" restart
