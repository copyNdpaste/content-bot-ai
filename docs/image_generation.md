# AI 이미지 자동 생성 — Pollinations.ai

IG 게시 시 LLM (Claude Opus 4.7) 이 글 분위기에 맞는 영문 이미지 프롬프트를 생성 →
Pollinations.ai (FLUX 모델) 가 그 자리에서 이미지 생성 → IG 업로더가 자동 첨부.

## 왜 Pollinations?
- **무료** — API 키 0, 가입 0
- **FLUX 모델** — 현재 오픈 모델 중 최고 수준
- **GET 한 번** — URL 자체가 이미지. Meta IG API 가 그 URL 에서 다운로드.

## 어떻게 동작?
```python
url = f"https://image.pollinations.ai/prompt/{인코딩된_프롬프트}?width=1080&height=1080&model=flux&seed=...&nologo=true"
```
- 박재범이 만든 영문 프롬프트 (예: `"two friends, korean and japanese girls, cozy tokyo cafe, soft afternoon light, candid, no text, no watermark"`)
- 이 URL 그대로 IG 업로더에 전달 → IG API 가 다운로드 → 자동 게시

## 셋업 (없음)
환경변수·키 발급 불필요. content-bot-ai 가 즉시 동작.

## 폴백 (예정)
Pollinations 가 느리거나 실패할 경우 대비:
- **Fal.ai** (FLUX schnell, $0.003/장) — `FAL_API_KEY=` 채우면 자동 사용
- 사장님이 추후 결정

## 트러블슈팅
- IG 가 "Media not found" → Pollinations 응답 느림 (10초+) → 재시도
- 이미지가 너무 추상적 → image_keyword 프롬프트 더 구체적으로 (`_build_persona_prompt` 의 instagram 가이드 참고)
- 같은 이미지 반복 → seed 가 자동 랜덤이라 매번 다름. 확인은 URL 의 `seed=` 파라미터.

## 출력 예시
박재범이 만든 IG draft 의 frontmatter:
```yaml
image_keyword: "two friends, korean and japanese girls, cozy tokyo cafe, soft afternoon light, candid, no text, no watermark"
image_url: "https://image.pollinations.ai/prompt/two%20friends%2C%20korean..."
media_type: IMAGE
```
