# workflow_runner.py — 수동 즉시 회차

스케줄러 안 기다리고 지금 1회만 돌리고 싶을 때 사용.
인자는 `content_pipeline.py` 와 100% 동일하게 위임된다.

## 언제 쓰나

- F5 후 박재범 채팅에서 사장님이 "지금 한 번 돌려" 요청 → 박재범이 이 스크립트 호출.
- 새 .env 값 (테마·계정 추가) 테스트.
- Slack 승인 카드가 잘 뜨는지 한 번 확인.
- `--dry-run` 으로 LLM 비용 없이 파이프라인 자체만 검증.

## 사용법

```bash
# 가장 흔한 케이스
python3 assets/tool-seeds/workflow/workflow_runner.py \
  --platform threads --account jp

# 6장 일괄
python3 assets/tool-seeds/workflow/workflow_runner.py \
  --platform all --account all

# 테마 강제
python3 assets/tool-seeds/workflow/workflow_runner.py \
  --platform instagram --account kr --theme "벚꽃 OOTD"

# 비용 없는 점검
python3 assets/tool-seeds/workflow/workflow_runner.py \
  --platform x --account jp --dry-run
```

## 출력

`content_pipeline.py` 의 stdout JSON 이 그대로 흘러나온다.
exit code 도 그대로 위임 (0 = 전부 성공, 2 = 일부 실패, 1 = 인자 오류).

## 트러블슈팅

- **`pipeline 없음`** → `content_pipeline.py` 가 같은 디렉토리에 있는지 확인.
- **Python 버전 오류** → 호출한 `python3` 가 3.8+ 인지 (`python3 --version`).
- **권한 거부** → 실행 권한 불필요 (Python 인터프리터가 직접 실행).
- **stdout 이 비어보임** — `content_pipeline.py` 가 마지막 라인에 JSON 한 줄만 찍음. 그 위 stderr 에 진행 로그.
