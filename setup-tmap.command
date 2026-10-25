#!/bin/bash
# Finder에서 두 번 클릭하면 TMap 키 입력창을 엽니다.
set -u
project_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)" || exit 1
cd -- "$project_directory" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Python 3를 찾지 못했어요. Python 3를 설치한 뒤 다시 열어 주세요.'
    read -r -p 'Enter를 누르면 닫습니다. ' _answer
    exit 1
fi
python3 scripts/setup_tmap.py
setup_result=$?
if [ "$setup_result" -ne 0 ]; then
    read -r -p 'Enter를 누르면 닫습니다. ' _answer
fi
exit "$setup_result"
