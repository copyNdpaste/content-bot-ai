# scheduler.py — 박재범 회차 자동 트리거 데몬

매 `ROUTINE_INTERVAL_HOURS` 시간마다 `content_pipeline.py` 를 호출하는 단순 데몬.
launchd 가 부팅 시 띄우고 계속 살아있게 유지한다 (`RunAtLoad=true`, `KeepAlive=true`).

## 설치

```bash
# 1) plist 복사 (이미 작성됨)
cp /Users/hoony/projects/money-ai/assets/tool-seeds/workflow/com.moneyai.content-scheduler.plist \
   ~/Library/LaunchAgents/

# 2) load
launchctl load -w ~/Library/LaunchAgents/com.moneyai.content-scheduler.plist

# 3) 상태 확인
launchctl list | grep content-scheduler
tail -f /tmp/moneyai-content-scheduler.log
```

이미 `~/Library/LaunchAgents/com.moneyai.content-scheduler.plist` 가 있다면 위 1단계 스킵.

## 종료 / 재시작

```bash
# 종료
launchctl unload ~/Library/LaunchAgents/com.moneyai.content-scheduler.plist

# 재시작 (env 바꾼 후)
launchctl unload ~/Library/LaunchAgents/com.moneyai.content-scheduler.plist
launchctl load ~/Library/LaunchAgents/com.moneyai.content-scheduler.plist
```

## 환경변수

| 변수 | 기본 | 설명 |
| --- | --- | --- |
| `ROUTINE_INTERVAL_HOURS` | 4 | 회차 주기 (0.25 ~ 24 사이로 clamp) |
| `ROUTINE_PLATFORMS` | `threads,instagram,x` | 매 회차 도는 채널 |
| `ROUTINE_ACCOUNTS` | `jp,kr` | 매 회차 도는 계정 |
| `TELEGRAM_BOT_TOKEN/CHAT_ID` | — | 시작·완료·실패 알림 (선택) |

`.env` (`_company/_agents/instagram/.env`) 에서 자동 로딩. launchd `EnvironmentVariables` 가 우선.

## 동작

1. 부팅 직후 1회 즉시 실행 (catch-up).
2. 이후 `interval` 만큼 1초 단위로 sleep (SIGTERM 빠른 반영).
3. 각 회차마다 텔레그램 알림: 시작 / 완료 (drafts·slack 카운트) / 실패.
4. 회차 최대 15분 (`subprocess timeout=900`) → 타임아웃 시 다음 회차로.

## 로그

- `/tmp/moneyai-content-scheduler.log` (스케줄러 자체 로그 + launchd StandardOutPath)
- `/tmp/moneyai-content-scheduler.err` (launchd StandardErrorPath)

## 트러블슈팅

- **plist load 실패** → 권한 확인 (`chmod 644`), syntax (`plutil ~/Library/LaunchAgents/com.moneyai.content-scheduler.plist`).
- **인터벌이 안 먹음** → `.env` 값을 launchd 가 못 봄. plist `EnvironmentVariables` 에 직접 박거나, `.env` 가 `_company/_agents/instagram/` 에 있는지 확인.
- **회차마다 cclude 미설치 에러** → PATH 보강 필요. plist `EnvironmentVariables.PATH` 에 `/Users/<user>/.nvm/versions/node/*/bin` 같은 claude 위치 추가.
- **회차가 멈춤** → `tail -f /tmp/moneyai-content-scheduler.log` 로 어디서 멈췄는지 확인. 보통 Claude CLI 응답 대기.
