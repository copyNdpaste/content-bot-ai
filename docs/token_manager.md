# Meta 토큰 자동 관리 (token_manager)

Threads + Instagram **멀티 계정** 토큰을 자동으로 발급·갱신하는 도구.
사장님이 한 번 `.env` 만 작성하면 이론상 영구 사용 가능.

---

## 왜 필요한가

- Meta 의 단기 토큰은 1~2시간, 장기 토큰은 60일 후 만료
- 계정마다 토큰을 따로 관리해야 함 (OnlyFriends 일·한 4개 = Threads jp/kr + IG jp/kr)
- 수동으로 매번 갱신하는 건 비현실적 → 단기→장기 교환 + 만료 7일 전 자동 갱신을 자동화

---

## 1단계 — `.env` 작성

`_company/_agents/instagram/.env` (없으면 새로 만드세요):

```dotenv
# ─── 앱 자격 (공통, 영구) ─────────────────────────
META_APP_ID=1327871115947327
META_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# ─── Threads 단기 토큰 (계정별, OAuth 직후 받은 값) ───
META_THREADS_SHORT_TOKEN_JP=TH...
META_THREADS_SHORT_TOKEN_KR=TH...

# ─── Instagram 단기 토큰 (계정별) ─────────────────
META_IG_SHORT_TOKEN_JP=EAA...
META_IG_SHORT_TOKEN_KR=EAA...

# ─── (옵션) 갱신 알림 ────────────────────────────
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

**계정 키 규칙**: `META_THREADS_SHORT_TOKEN_<계정>` / `META_IG_SHORT_TOKEN_<계정>` 형식.
`<계정>` 자리에 `JP`, `KR`, `US` 등 자유롭게. 대소문자 무관 (내부적으로 lower-case).

### 단기 토큰은 어디서 받나
- Threads: `threads_uploader.md` 1~3단계 참조 (Meta Developer Console → Threads API)
- Instagram: `instagram_uploader.md` 0~3단계 참조 (FB Page + IG 비즈니스 전환 + Graph API Explorer)
- 단기 토큰은 1~2시간 안에 부트스트랩해야 만료되지 않습니다.

---

## 2단계 — 부트스트랩 (1회)

```bash
cd _company/_agents/instagram/tools
python3 token_manager.py --bootstrap
```

성공 시 출력:
```
✅ Threads[jp] OK — token=TH..(190자) expires_in≈59d
✅ Threads[kr] OK — token=TH..(190자) expires_in≈59d
✅ Instagram[jp] OK — token=EAA.(220자) expires_in≈59d user_id=17841...
✅ Instagram[kr] OK — token=EAA.(220자) expires_in≈59d user_id=17841...
💾 저장 위치: .../tools/tokens.json
```

이 시점부터 `tokens.json` 에 60일짜리 장기 토큰이 저장됩니다.
앱 시크릿/토큰은 **stdout 에 절대 평문으로 노출되지 않습니다** (앞 4자 + 길이만 표시).

---

## 3단계 — 상태 확인 (언제든지)

```bash
python3 token_manager.py --status
```

출력 예:
```
   Platform   Account  Days left  Last refresh          User ID
   ---------  -------  ---------  --------------------  -------------------
✅ threads    jp       58.9       2026-05-21T10:00:00Z  1234567890
✅ threads    kr       58.9       2026-05-21T10:00:00Z  1234567891
⚠️ instagram  jp       6.2        2026-04-15T...        17841400000000000
❌ instagram  kr       -1.5       2026-03-22T...        17841400000000001
```

- `✅` 건강, `⚠️` 만료 7일 이내 (refresh 권장), `❌` 만료됨
- exit code: 만료된 토큰 있으면 `1`, 아니면 `0` (cron 알람에 활용)

---

## 4단계 — 자동 갱신 (반복)

만료 7일 이내 토큰만 자동 갱신:

```bash
python3 token_manager.py --refresh
```

- 갱신 필요 없으면: `✅ All tokens healthy`
- 일부 갱신: 갱신/스킵/실패를 각각 출력
- 텔레그램 환경변수가 있으면 결과 알림

### cron 등록 예 (매일 새벽 3시)
```cron
0 3 * * * cd /path/to/_company/_agents/instagram/tools && python3 token_manager.py --refresh
```

또는 Connect AI 확장의 스케줄러에 도구로 등록 (자동 시드되는 `token_manager.json` 의
`AUTO_REFRESH_HOURS` 옵션 활용).

---

## 일상 워크플로우

```
.env 작성 (1회)
   └─→ token_manager.py --bootstrap (1회)
         └─→ tokens.json 생성, 60d 토큰 확보
               ├─→ threads_uploader.py --account jp ...   ← 자동 사용
               ├─→ instagram_uploader.py --account kr ... ← 자동 사용
               └─→ (매일/매주) token_manager.py --refresh ← 영구화
```

---

## 트러블슈팅

| 증상 | 원인 / 해결 |
|------|-------------|
| `META_APP_SECRET 미설정` | `.env` 가 도구 폴더에 없거나 키 오타. `cat _company/_agents/instagram/.env` 로 확인 |
| `단기 토큰을 하나도 찾지 못함` | `META_THREADS_SHORT_TOKEN_<계정>` 형식 정확한지 확인 |
| `HTTP 400 ... already used` | 단기 토큰을 이미 한 번 교환했음 → OAuth 다시 받으세요 |
| `HTTP 400 ... expired_token` | 단기 토큰이 만료됨 (1~2시간 지남) → OAuth 다시 |
| `HTTP 400 ... Invalid app_secret` | `META_APP_SECRET` 오타. Meta Developer Console → 앱 설정 → 기본에서 재확인 |
| Instagram `user_id` 가 `(없음)` | FB Page 연결 안 됨. `instagram_uploader.md` 0단계 선행조건 확인 |

---

## 보안 경고

- **`.env` 는 반드시 `.gitignore` 에 추가하세요** (money-ai 의 `.gitignore` 에 이미 등록됨)
- **`tokens.json` 도 `.gitignore` 대상** — 절대 깃에 올리지 마세요
- **앱 시크릿은 어디에도 공유 X** — 이 도구는 stdout/stderr 에 평문으로 노출하지 않습니다
- `tokens.json` 은 자동으로 `0600` 권한 (owner-only read/write)

---

## CLI 요약

```bash
python3 token_manager.py --bootstrap   # 최초 1회
python3 token_manager.py --status      # 헬스체크
python3 token_manager.py --refresh     # 만료 7일 이내 갱신
```

LLM 호출 0회. 외부 Python 의존성 0개 (urllib.request 만 사용).
