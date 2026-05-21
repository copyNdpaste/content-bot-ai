# Instagram 자동 업로더 (멀티 계정)

Meta 의 Instagram (사진/릴스) 에 캡션 + 미디어 URL 을 자동 게시하거나,
토큰이 없으면 **draft 모드** 로 `drafts/instagram-*.md` 파일에 저장해 주는 도구.

LLM 호출 0회 — 캡션 초안 + 이미지 준비는 다른 에이전트가 미리 처리.

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
python3 instagram_uploader.py --caption "오늘의 한 컷 #ai" \
  --image-url "https://cdn.example.com/photo.jpg" --account jp

python3 instagram_uploader.py --caption "안녕 #일상" \
  --image-url "https://..." --account kr

# 3) 강제 draft
python3 instagram_uploader.py --caption "..." --image-url "..." --account jp --dry-run
```

도구 카드 설정에선 **`DEFAULT_ACCOUNT`** 만 채워 두면 됩니다 (예: `jp`).
스케줄러가 호출할 때 자동으로 그 계정으로 게시.

업로더가 만료 임박/만료된 토큰을 감지하면 자동으로 `token_manager.py --refresh` 호출 후
재시도 → 이론상 영구 사용.

---

## Draft 모드 (토큰 없이 즉시 사용)

`tokens.json` 이 없거나 계정이 미설정이면 자동 draft:

```bash
python3 instagram_uploader.py --caption "오늘의 한 컷 #ai" \
  --image-url "https://example.com/photo.jpg"
```

결과:
```
{"status": "drafted", "account": "default", "path": ".../drafts/instagram-default-20260521-101530.md"}
```

---

## CLI 인자

| 인자 | 설명 | 기본 |
|------|------|------|
| `--caption` | 캡션 본문 (필수) | — |
| `--image-url` | 미디어 URL (REELS 면 video URL) (필수) | — |
| `--account` | tokens.json 의 instagram[<account>] 키 | `default` |
| `--media-type` | `IMAGE` / `REELS` / `CAROUSEL` (CAROUSEL 은 IMAGE 폴백) | `IMAGE` |
| `--dry-run` | 토큰 있어도 강제 draft | — |

---

## 이미지/영상 호스팅

Meta Graph API 는 **공개 URL** 만 받습니다. 로컬 파일은 못 올림.

| 서비스 | 무료 한도 | 메모 |
|--------|-----------|------|
| **Vercel Blob** | 1GB | Connect AI 가 Vercel 기반이라 추천 |
| **Cloudinary** | 25GB / 월 | 이미지 변환 자동 |
| **AWS S3** | 5GB 1년 | 익숙하면 좋음 |
| **Imgur** | 무제한 | 가장 빠름, 단 비공개 불가 |

URL 은 **HTTPS** + **24시간 이상 유지** (Meta 가 비동기 다운로드).

---

## REELS 주의사항

- 영상 URL 도 공개 + HTTPS
- 권장: `.mp4 (H.264 + AAC)`, 최대 90초, 9:16 (1080x1920)
- 영상 처리 1~3분 — 게시 직후 안 보여도 정상

---

## 캐러셀

현재 도구는 `--media-type CAROUSEL` 을 IMAGE 단일 게시로 폴백.
children container 처리는 향후 버전.

---

## 트러블슈팅

| 증상 | 원인 / 해결 |
|------|-------------|
| `❌ ... 401` / `190` / `expired` | 토큰 만료. 업로더가 자동 refresh 시도 → 실패 시 `token_manager.py --bootstrap` |
| `Media not found` | 이미지 URL 이 비공개거나 만료. 시크릿창에서 직접 열어 확인 |
| `(#10) Application does not have permission` | FB Page ↔ IG 비즈니스 계정 연결 안 됨 (token_manager 가이드 0단계) |
| `Instagram account not found` | IG user_id 가 틀림. `token_manager.py --bootstrap` 으로 재조회 |
| `(#100) image_url 무효` | HTTPS 가 아니거나 비공개 |
| `Aspect ratio not supported` | 1.91:1 ~ 4:5 범위. 정사각형 1:1 가장 안전 |
| `Caption length exceeds limit` | 캡션 2200자 제한 (해시태그 포함) |

---

## 안전 수칙

- 토큰은 `tokens.json` 에 저장 (.gitignore 적용됨)
- 처음엔 draft 모드로 며칠 돌려서 캡션·이미지 흐름 확인
- 인스타는 자동 게시에 민감 — 하루 25개 이상 임시 차단 위험
- 같은 캡션/이미지 반복은 스팸 판정

---

## (Legacy) 단일 계정 모드 — 환경변수 폴백

```bash
META_IG_ACCESS_TOKEN=EAA... \
META_IG_USER_ID=17841... \
python3 instagram_uploader.py --caption "..." --image-url "..."
```

단일 계정만 운영한다면 이 방식도 동작 (60일 갱신은 직접).

토큰 직접 발급 절차(참고):
1. **선행 조건**: FB Page 생성 + IG 계정을 Business/Creator 로 전환 + Page-IG 연결
2. Meta for Developers → 앱 생성 (유형: Business) → Instagram Graph API product 추가
3. 권한: `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement`
4. Graph API Explorer → Page Access Token 발급 (단기)
5. 장기 토큰 교환:
   ```bash
   curl "https://graph.facebook.com/v18.0/oauth/access_token?\
   grant_type=fb_exchange_token&\
   client_id=APP_ID&client_secret=APP_SECRET&\
   fb_exchange_token=SHORT_TOKEN"
   ```
6. IG Business User ID:
   ```bash
   # me/accounts → page_id
   curl ".../me/accounts?access_token=LONG_TOKEN"
   # page_id → instagram_business_account.id
   curl ".../{PAGE_ID}?fields=instagram_business_account&access_token=LONG_TOKEN"
   ```

---

## 공식 문서

- Instagram Graph API : https://developers.facebook.com/docs/instagram-api
- Content Publishing : https://developers.facebook.com/docs/instagram-api/guides/content-publishing
- Rate Limits : 24시간 25개 게시 / 200개 모든 API
- Reels : https://developers.facebook.com/docs/instagram-api/guides/content-publishing#reels-posts
