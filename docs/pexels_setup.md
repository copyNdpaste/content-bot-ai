# Pexels API Key 발급 가이드 (5분, 무료)

Instagram 게시물에 자동 이미지 첨부용. `content_pipeline.py` 가 사용함.

## 왜 필요한가

Instagram Graph API 는 IMAGE 게시 시 외부에서 접근 가능한 `image_url` 을 요구함.
LLM 이 본문만 쓰면 IG 업로더가 "이미지 없음" 으로 실패함.
→ Pexels (무료 스톡 사진) API 에서 본문 분위기에 맞는 사진을 자동으로 찾아 첨부.

## 무료 티어 한도

- **시간당 200 요청**
- **월 20,000 요청**
- 회차당 IG 1~2 호출 × 일 6 회 = 월 ~360 호출. 한도의 2% 도 안 씀. 충분.

## 발급 순서

1. https://www.pexels.com/api/ 접속
2. 우측 상단 **"Your API Key"** 클릭 (로그인 안 돼있으면 Sign Up 먼저 — 이메일 + 비밀번호만)
3. 첫 사용이면 간단한 설문 노출:
   - "What's the name of your project?" → `OnlyFriends content pipeline`
   - "Describe your project" → 예시:
     ```
     automating social media posts for OnlyFriends — a Korea-Japan friendship
     matching service. We attach mood-matching photos from Pexels to our
     Instagram captions to improve organic reach.
     ```
   - URL → `https://onlyfriends.tryproo.com/`
4. 제출 즉시 API Key 발급됨 (승인 대기 없음)
5. 키 복사 (예: `<YOUR_PEXELS_API_KEY_HERE>`)

## .env 에 붙여넣기

```bash
# 파일: _company/_agents/instagram/.env
# 마지막 줄 PEXELS_API_KEY= 뒤에 붙여넣기
PEXELS_API_KEY=<YOUR_PEXELS_API_KEY_HERE>
```

저장 후 launchd 가 다음 회차에서 자동으로 새 키를 읽음. 재시작 불필요.

## 테스트

```bash
cd /Users/hoony/projects/money-ai
.venv/bin/python3 -c "
import sys
sys.path.insert(0, 'assets/tool-seeds/workflow')
from content_pipeline import _load_env_file, _fetch_pexels_image
_load_env_file('_company/_agents/instagram/.env')
print(_fetch_pexels_image('tokyo cafe'))
"
```

성공 시 출력 예시:
```
https://images.pexels.com/photos/.../pexels-photo-....jpeg?...&w=1080&h=1080&fit=crop
```

`None` 이 나오면:
- 키가 .env 에 안 들어있음 → 위 발급 순서 다시
- 네트워크 차단 → 다른 와이파이
- 응답 형식 변경 → `rtk proxy curl -H "Authorization: $PEXELS_API_KEY" "https://api.pexels.com/v1/search?query=tokyo&per_page=1"` 로 raw 확인

## 회차 테스트 (실제 IG draft 까지)

```bash
cd /Users/hoony/projects/money-ai
./scripts/test-jp.sh instagram          # JP 계정 IG 1회차
# 생성된 draft 확인
ls -t _company/_agents/instagram/tools/drafts | head -1 | \
  xargs -I{} cat "_company/_agents/instagram/tools/drafts/{}"
```

frontmatter 에 `image_url: https://images.pexels.com/...` 와 `media_type: IMAGE` 가
보이면 성공. Slack 카드에도 미리보기 이미지 노출됨.

## 트러블슈팅

### `429 Too Many Requests`

- 시간당 200 요청 초과. 거의 안 일어남.
- 30분 기다리거나, Pexels 대시보드에서 **"Regenerate Key"** 클릭 (즉시 reset)
- 향후 캐싱 추가 검토 (같은 키워드는 24h TTL 로 재사용)

### `401 Unauthorized`

- 키 오타. .env 의 `PEXELS_API_KEY=` 뒤에 공백·따옴표 없는지 확인.

### 이미지가 본문과 어울리지 않음

- LLM 이 출력한 `image_keyword` 가 너무 추상적 (`"feeling"`, `"vibe"` 등).
- `content_pipeline.py` 의 `channel_tone["instagram"]` 프롬프트 강화 필요
  (현재: "한일 친구·카페·여행·OOTD·셀프케어 등 인스타 검색 잘 되는 톤" 으로 유도 중).

### 사진이 너무 서양인 위주

- Pexels 는 글로벌 풀이라 한국·일본 컨텐츠가 상대적으로 적음.
- 키워드에 `"korean"` / `"japanese"` / `"seoul"` / `"tokyo"` / `"asian"` 등을
  LLM 이 자연스럽게 포함하도록 프롬프트에서 유도 중.
- 더 동양인 위주 풀이 필요하면 향후 Unsplash 병행 고려 (별도 키 발급).

## 향후 확장 아이디어

- 같은 키워드 24h 캐싱 (월 호출 1/10 로)
- Pexels Curated 엔드포인트로 fallback (검색 결과 0개 시)
- Video 지원 (IG REELS 자동화 시 Pexels Videos API 사용 가능)
- 사용자별 선호 사진 학습 (사장님이 Slack 에서 reject 한 사진 패턴 회피)
