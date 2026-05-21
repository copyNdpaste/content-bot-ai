# X (Twitter) 자동 업로더 (멀티 계정 · OAuth 2.0)

X API v2 + OAuth 2.0 (PKCE / Confidential Client) 기반 자동 업로더.
외부 Python 의존성 없음 — `urllib.request` 만 사용.
LLM 호출 0회 (문구는 다른 에이전트가 미리 준비).

---

## ⭐ 권장 워크플로우 — token_manager 통합

`token_manager.py` 가 X 토큰도 같이 관리합니다 (Threads/IG 와 동일 패턴, 단 X 만 임계값이 30분).

```bash
# 1) .env 에 X_CLIENT_ID/SECRET + 계정별 X_OAUTH_TOKEN_/REFRESH_ 채움 (아래 절차 참조)
# 2) bootstrap (1회) → tokens.json 의 x 섹션 생성
python3 token_manager.py --bootstrap

# 3) 일상 사용
python3 x_uploader.py --text "안녕 X" --account jp
python3 x_uploader.py --text "오늘의 한 컷" \
  --media-url "https://cdn.example.com/foo.jpg" --media-type image --account jp

# 4) (옵션) draft 확인
python3 x_uploader.py --text "테스트" --account jp --dry-run
```

X access_token 은 2시간 만료 → token_manager 가 30분 이내면 자동 refresh.
업로더 실행 중 401 발생해도 inline refresh 후 1회 재시도.

---

## CLI 인자

| 인자 | 설명 | 기본 |
|------|------|------|
| `--text` | 트윗 본문 (≤ 280자, Free tier 기준) | (필수) |
| `--account` | tokens.json 의 `x[<account>]` 키 | `default` |
| `--media-url` | 미디어 URL (다중 가능) | (없음) |
| `--media-type` | `image` / `video` (media-url 있으면 필수) | (없음) |
| `--reply-to` | 답글 대상 tweet_id | (없음) |
| `--dry-run` | 토큰 있어도 강제 draft 저장 | — |

환경변수 `DRAFT_MODE=true` 도 draft 강제.

---

## X Developer Portal — OAuth 2.0 토큰 발급 6단계

> ⚠️ 신규: 2024년부터 Free tier 도 자동 게시 가능 (월 500 tweets/post 한도, 24h read 100).

### 1) 앱 생성
- https://developer.x.com → "Sign up for Free Account" (이미 계정 있으면 Dashboard)
- Projects & Apps → "Add App"

### 2) User authentication settings 활성화
- App → Settings → "User authentication settings" → "Set up"

### 3) OAuth 2.0 설정
- **Type**: `Confidential client` 선택 (Public client 는 refresh_token 만료 빠름)
- **App permissions**: `Read and write` (게시용)
- **Type of App**: Web App / Automated App or Bot
- **Callback URI**: `http://localhost:8080/callback` (PKCE 테스트용)
- **Website URL**: 본인 사이트 (없으면 GitHub 프로필 OK)

### 4) Client ID / Secret 발급
- 저장하면 Client ID + Client Secret 표시
- 즉시 `.env` 에:
  ```
  X_CLIENT_ID=...
  X_CLIENT_SECRET=...
  ```

### 5) PKCE Flow 로 user access_token + refresh_token 받기

**Authorize URL (브라우저)**:
```
https://twitter.com/i/oauth2/authorize?response_type=code&client_id={X_CLIENT_ID}&redirect_uri=http://localhost:8080/callback&scope=tweet.read%20tweet.write%20users.read%20offline.access&state=state&code_challenge=challenge&code_challenge_method=plain
```

> `scope` 에 `offline.access` 가 있어야 refresh_token 발급됩니다.
> `code_challenge_method=plain` + `code_challenge=challenge` (그대로 사용 가능 — 테스트용).

승인 → `http://localhost:8080/callback?code=AbCd...&state=state` 로 리다이렉트.
URL 의 `code=` 값 복사.

**Token Exchange (터미널)**:
```bash
CID=...; CSEC=...; CODE=...
curl -X POST https://api.x.com/2/oauth2/token \
  -u "${CID}:${CSEC}" \
  -d "grant_type=authorization_code" \
  -d "code=${CODE}" \
  -d "redirect_uri=http://localhost:8080/callback" \
  -d "code_verifier=challenge"
```

응답:
```json
{
  "token_type": "bearer",
  "expires_in": 7200,
  "access_token": "Vk1Q...",
  "refresh_token": "bWdHN..._...",
  "scope": "tweet.read tweet.write users.read offline.access"
}
```

### 6) 계정별로 `.env` 에 저장
```
X_OAUTH_TOKEN_JP=Vk1Q...
X_OAUTH_REFRESH_TOKEN_JP=bWdHN...
```

두 번째 계정은 X 에서 로그아웃 → 그 계정으로 다시 OAuth → 같은 절차 → `_KR` 같이 저장.

이후 `token_manager.py --bootstrap` → `tokens.json` 에 `x.jp` / `x.kr` 자동 등재.

---

## tokens.json 구조 예시

```json
{
  "threads": { ... },
  "instagram": { ... },
  "x": {
    "jp": {
      "access_token": "Vk1Q...",
      "refresh_token": "bWdHN...",
      "expires_at": "2026-05-21T13:30:00Z",
      "refreshed_at": "2026-05-21T11:30:00Z"
    },
    "kr": { ... }
  }
}
```

---

## 미디어 한도

| 타입 | 한도 | 메모 |
|------|------|------|
| Image | ≤ 5MB, jpg/png/gif/webp | v1.1 simple upload (`/media/upload.json`) |
| Video | ≤ 512MB, ≤ 140초, mp4 (H.264 + AAC) | v1.1 chunked upload (INIT/APPEND/FINALIZE + STATUS poll) |
| 다중 미디어 | image × 4 OR video × 1 OR gif × 1 | X 정책 |

업로더는 URL 다운로드 → bytes → 자동 분기 (image vs video) → media_id 추출 후 트윗 생성.

---

## 트러블슈팅

| 증상 | 원인 / 해결 |
|------|-------------|
| `HTTP 401: invalid_token` | access_token 만료. 업로더가 자동 refresh 재시도. 실패하면 .env 의 refresh_token 도 만료 → PKCE 재발급 |
| `HTTP 403: Forbidden` | App permissions 가 Read-only. Portal 에서 "Read and write" 로 바꾸고 토큰 재발급 (권한 변경 후 기존 토큰 무효화) |
| `HTTP 429: Too Many Requests` | Rate limit. Free tier: 월 500 tweets, 15분당 50 posts. 잠시 대기 |
| `text length exceeds 280` | Free tier 280자 제한 (Basic+ 는 25,000자). 본문 축소 |
| `media_id not finalized` | video 처리 중 — 업로더가 STATUS 폴링 30회까지 자동 대기 (≈2.5분). 90초 이내 영상 권장 |
| `code expired` (PKCE) | authorize code 는 30초 만료. authorize → token exchange 사이를 빠르게 |
| refresh_token 자체가 무효 | Confidential client 가 아니거나 `offline.access` scope 누락. Portal 설정 다시 확인 후 재발급 |

---

## 안전 수칙

- 토큰·시크릿은 stdout/stderr 평문 노출 절대 X (업로더는 mask 함수 사용)
- `.env` / `tokens.json` 은 `.gitignore` 적용 필수
- 처음 1주는 `--dry-run` 으로 문구 흐름 확인 후 자동 게시 전환
- 4개 계정 동시 운영 시 Rate limit 공유 — 분산 권장

---

## 공식 문서

- X API v2 : https://docs.x.com/x-api/introduction
- OAuth 2.0 PKCE : https://docs.x.com/resources/fundamentals/authentication/oauth-2-0/authorization-code
- Media Upload (v1.1) : https://docs.x.com/x-api/media/quickstart
- Posts (tweets) : https://docs.x.com/x-api/posts/creation-of-a-post
- Rate Limits : https://docs.x.com/x-api/fundamentals/rate-limits
