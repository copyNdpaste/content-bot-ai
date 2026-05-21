#!/usr/bin/env bash
# KR 계정 1회차 컨텐츠 생성 + draft 저장 + (Slack 셋업돼있으면) Slack 카드 게시.
#
# 사용법:
#   ./scripts/test-kr.sh                  # 기본 threads
#   ./scripts/test-kr.sh instagram        # instagram
#   ./scripts/test-kr.sh x                # X (Twitter)
#   ./scripts/test-kr.sh threads --dry-run  # 추가 인자 그대로 전달
#
# 생성된 draft 확인:
#   ls -t _company/_agents/instagram/tools/drafts | head -3
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PLATFORM="${1:-threads}"
shift || true   # 첫 인자(PLATFORM) 제거. 없어도 OK.

echo "🇰🇷 KR / $PLATFORM 회차 시작..."
.venv/bin/python3 src/workflow/workflow_runner.py \
  --platform "$PLATFORM" --account kr "$@"
