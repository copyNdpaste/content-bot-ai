# Threads 자동 업로더 (멀티 계정)

Meta 의 Threads 에 텍스트(+선택 이미지) 를 자동 게시하거나,
토큰이 없으면 **draft 모드** 로 `drafts/threads-*.md` 파일에 저장해 주는 도구.

LLM 호출 0회 — 문구 초안은 다른 에이전트가 미리 만들어서 `--text` 로 넘겨주세요.

---

## ⭐ 권장 워크플로우 — token_manager + tokens.json

OnlyFriends 같은 멀티 리전 운영자라면 계정 4개 (Threads jp/kr + IG jp/kr) 의
토큰을 `token_manager.py` 가 한꺼번에 관리합니다. 이 도구는 `tokens.json` 을 자동으로
읽어 쓰니까 도구 카드에 토큰을 직접 입력할 필요가 없습니다.

```bash
# 1) 최초 1회 — .env 작성 + bootstrap
#    → token_manager.md 참조
python3 token_manager.py --bootstrap

# 2) 일상 사용
python3 threads_uploader.py --text "안녕하세요" --account jp
python3 threads_uploader.py --text "こんにちは" --account jp
python3 threads_uploader.py --text "안녕하세요" --account kr

# 3) (옵션) 강제 draft
python3 threads_uploader.py --text "테스트" --account jp --dry-run
```

도구 카드 설정에선 **`DEFAULT_ACCOUNT`** 만 채워 두면 됩니다 (예: `jp`).
스케줄러가 호출할 때 자동으로 그 계정으로 게시.

만료 임박/만료된 토큰이 발견되면 업로더가 자동으로 `token_manager.py --refresh` 를
호출해서 갱신 후 재시도합니다 (이론상 영구 사용).

---

## Draft 모드 (토큰 없이 즉시 사용)

`tokens.json` 도 없고 환경변수도 없으면 자동으로 draft 모드:

```bash
python3 threads_uploader.py --text "오늘의 인사이트: ..."
```

결과:
```
{"status": "drafted", "account": "default", "path": ".../drafts/threads-default-20260521-101530.md", ...}
```

이 파일을 열어보고 직접 Threads 앱에 복붙해서 올릴 수 있습니다.

---

## CLI 인자

| 인자 | 설명 | 기본 |
|------|------|------|
| `--text` | 게시 본문 (필수) | — |
| `--account` | tokens.json 의 threads[<account>] 키 | `default` |
| `--image-url` | (선택) 공개 이미지 URL | (없음) |
| `--reply-control` | `everyone` / `mentioned` / `followers` | `everyone` |
| `--dry-run` | 토큰 있어도 강제 draft | — |

이미지 첨부:
```bash
python3 threads_uploader.py --text "..." --image-url "https://cdn.../foo.jpg" --account jp
```

---

## 트러블슈팅

| 증상 | 원인 / 해결 |
|------|-------------|
| `❌ ... 401` / `190` / `expired` | 토큰 만료. 업로더가 자동 refresh 시도하지만 실패하면 `token_manager.py --bootstrap` 다시 |
| `Account not found in tokens.json` | `--account jp` 인데 `.env` 에 `META_THREADS_SHORT_TOKEN_JP` 가 없음 |
| `400 — text length` | Threads 본문은 500자 제한 |
| `Media not supported` | `--image-url` 은 HTTPS + 공개 접근 가능해야 함 |
| `Rate limit exceeded` | 1시간 250개 / 24시간 1000개 한도 |

---

## 안전 수칙

- 토큰은 **`tokens.json` 에 저장 (.gitignore 적용됨)**, 도구 카드엔 노출 X
- 처음엔 무조건 **draft 모드** 로 며칠 돌려서 문구가 의도대로 나오는지 확인
- 자동 게시 시작 후 1~2주는 매일 결과를 직접 확인 권장

---

## (Legacy) 단일 계정 모드 — 환경변수 폴백

기존 `META_THREADS_ACCESS_TOKEN` + `META_THREADS_USER_ID` 환경변수도 그대로 동작합니다
(`tokens.json` 이 없거나 `--account` 가 일치하지 않을 때 폴백).

```bash
META_THREADS_ACCESS_TOKEN=TH... \
META_THREADS_USER_ID=1234567890 \
python3 threads_uploader.py --text "안녕"
```

단일 계정만 운영한다면 이 방식이 더 단순하지만, 60일 토큰 갱신을 직접 해야 합니다.
멀티 계정·자동 갱신이 필요하면 `token_manager.py` 워크플로우를 쓰세요.

토큰 직접 발급 절차(참고):
1. https://developers.facebook.com → 앱 생성 (유형: Other) → Threads API product 추가
2. 권한: `threads_basic`, `threads_content_publish`
3. Threads 앱 → 설정 → 계정 → 개발자 → 위 앱 연결
4. 단기 토큰 → 장기 토큰 교환 (60d):
   ```bash
   curl -X POST "https://graph.threads.net/access_token?\
   grant_type=th_exchange_token&\
   client_secret=APP_SECRET&\
   access_token=SHORT_TOKEN"
   ```
5. User ID 확인:
   ```bash
   curl "https://graph.threads.net/v1.0/me?fields=id&access_token=LONG_TOKEN"
   ```

---

## 공식 문서

- Threads API : https://developers.facebook.com/docs/threads
- Long-lived tokens : https://developers.facebook.com/docs/threads/long-lived-tokens
- Rate Limits : https://developers.facebook.com/docs/threads/troubleshooting
