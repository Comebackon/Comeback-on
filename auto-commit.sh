#!/bin/bash

cd "$(dirname "$0")" || exit 1

# Git 작업이 진행 중이면 종료
if [ -f .git/index.lock ]; then
    exit 0
fi

# 현재 브랜치 확인
BRANCH=$(git branch --show-current)

if [ -z "$BRANCH" ]; then
    exit 1
fi

# 기존 추적 파일의 변경 사항 추가
git add -u || exit 1

# 변경 사항이 없으면 종료
if git diff --cached --quiet; then
    exit 0
fi

# 자동 커밋
git commit -m "Auto save: $(date '+%Y-%m-%d %H:%M:%S')" || exit 1

# GitHub에 푸시
git push origin "$BRANCH"