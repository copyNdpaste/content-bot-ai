# Slack 인터랙티브 승인 루프 — 셋업 가이드

콘텐츠 생성 → Slack 카드 미리보기 → ✅ 버튼 → 자동 업로드.
설정 시간: 약 5~10분.

## 0. 전체 그림

```
[Threads/IG/X 생성 도구]
        │  draft .md
        ▼
[slack_notifier.py]
        │  chat.postMessage (Block Kit + 버튼)
        ▼
   #콘텐츠-승인  ←  사장님이 ✅ 클릭
        │
        ▼
[slack_approval_worker.py] (Socket Mode 데몬, launchd)
        │  subprocess
        ▼
[threads_uploader / instagram_uploader / x_uploader]
        │
        ▼
   실제 게시 + Slack 메시지 update (permalink)
```

## 1. Slack App 만들기

1. <https://api.slack.com/apps> 접속 → **Create New App** → **From scratch**
2. App Name: `MoneyAI Approver` (자유) / Workspace: 본인 워크스페이스
3. 좌측 메뉴 진행:

### 1-1. OAuth & Permissions

**Bot Token Scopes** 에 다음 추가:
- `chat:write`
- `chat:write.public`
- `channels:read`
- `files:write`

### 1-2. Socket Mode

좌측 **Socket Mode** → **Enable Socket Mode**: ON

→ **App-Level Token** 발급 다이얼로그가 뜸:
- Token Name: `socket-mode`
- Scope: **`connections:write`** 추가
- **Generate** → `xapp-...` 토큰 복사해 둠 (한 번만 보임 — 즉시 .env 에 붙이기)

### 1-3. Interactivity & Shortcuts

좌측 **Interactivity & Shortcuts** → **Interactivity**: ON
(Request URL 은 Socket Mode 라서 비워둬도 됨)

### 1-4. Install App

좌측 **Install App** → **Install to Workspace** → 권한 승인
→ **Bot User OAuth Token** (`xoxb-...`) 복사

## 2. 채널에 봇 초대 + 채널 ID 복사

1. Slack 워크스페이스에서 채널 만들기 (예: `#money-ai-approval`)
2. 채널에서 `/invite @MoneyAI Approver` (앱 이름)
3. 채널 우클릭 → **View channel details** → 맨 아래 **Channel ID** (`C0...`) 복사

## 3. .env 채우기

`_company/_agents/instagram/.env` 끝 부분:

```bash
# === Slack 인터랙티브 (콘텐츠 승인 루프) ===
SLACK_BOT_TOKEN=xoxb-여기에-붙여넣기
SLACK_APP_TOKEN=xapp-여기에-붙여넣기
SLACK_CHANNEL_ID=C0여기에-붙여넣기
```

## 4. slack-sdk 설치

```bash
/opt/homebrew/bin/python3 -m pip install --user slack-sdk
```

## 5. 워커 데몬 등록 (launchd)

```bash
# 처음 한 번 (또는 plist 수정 시)
launchctl unload ~/Library/LaunchAgents/com.moneyai.slack-worker.plist 2>/dev/null
launchctl load   ~/Library/LaunchAgents/com.moneyai.slack-worker.plist

# 로그 확인
tail -f /tmp/moneyai-slack-worker.log /tmp/moneyai-slack-worker.err
```

> launchd 는 plist 의 `EnvironmentVariables` 만 봅니다.
> `.env` 의 SLACK_* 를 워커에 주입하려면 plist 에 직접 키를 추가하거나,
> 워커 안에서 `.env` 를 읽도록 하거나, `launchctl setenv SLACK_BOT_TOKEN xxx` 로 글로벌 등록.
> 권장: plist 의 `EnvironmentVariables` 에 SLACK_* 3개 직접 추가.

예시 (plist 수정 후 unload/load 다시):

```xml
<key>EnvironmentVariables</key>
<dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>SLACK_BOT_TOKEN</key><string>xoxb-...</string>
    <key>SLACK_APP_TOKEN</key><string>xapp-...</string>
    <key>SLACK_CHANNEL_ID</key><string>C0...</string>
</dict>
```

## 6. 동작 확인

```bash
# (1) draft 생성
python3 assets/tool-seeds/instagram/threads_uploader.py --text "Slack 승인 테스트" --account jp

# (2) Slack 카드 발송 (export 로 .env 값 잠시 주입)
export $(grep -v '^#' _company/_agents/instagram/.env | xargs)
python3 assets/tool-seeds/instagram/slack_notifier.py \
    --draft-path $(ls -t assets/tool-seeds/instagram/drafts/threads-*.md | head -1) \
    --platform threads \
    --account jp

# (3) Slack 채널에서 ✅ 클릭 → 워커가 처리 → 메시지가 "✅ 업로드 완료: <permalink>" 로 갱신
```

## 트러블슈팅

| 증상 | 원인 / 해결 |
|------|--------------|
| 메시지가 안 옴 | `SLACK_BOT_TOKEN` 미설정 또는 봇이 채널 미가입. `/invite @봇` 재실행. |
| 버튼 눌러도 무반응 | 워커 미실행 (`launchctl list \| grep moneyai`) 또는 App-Level Token `connections:write` 누락. |
| `not_in_channel` 에러 | 채널에 봇 초대. |
| `invalid_auth` | `xoxb-` 토큰 재발급 (App 재설치). |
| 업로드 실패 | `tail /tmp/moneyai-slack-worker.err`, `token_manager.py --status` 로 토큰 확인. |
| 워커 자꾸 죽음 | `KeepAlive true` 라 자동 재시작. err 로그로 원인 파악. |

## 보안 메모

- `SLACK_*` 토큰은 절대 git 커밋 X (`.env` 는 `.gitignore` 처리됨)
- 워커가 실행하는 uploader 는 실제 게시 → ❌ 거절 버튼 적극 활용
- 거절된 draft 는 `drafts/rejected/` 로 자동 이동 (검토 후 수동 삭제)
