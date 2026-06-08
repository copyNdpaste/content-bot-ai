# AI 이미지 자동 생성 - 로컬 커맨드 연결

IG 게시 시 content pipeline 이 글 분위기에 맞는 영문 이미지 프롬프트를 생성하고,
설정된 로컬 이미지 생성 커맨드를 호출한다. 운영 기본값은 OpenAI Images API를 쓰지 않는
`scripts/generate_codex_image.py` 이며, 반드시 `prompt -> 로컬 이미지 파일` 계약을 지켜야 한다.

## 동작

1. Codex가 X/Threads/Instagram 채널별 본문을 먼저 만든다.
2. `_call_codex_image_prompt` 가 완성된 본문을 다시 읽고 `image_keyword` 를 만든다.
3. `_generate_codex_image` 가 그 동적 `image_keyword` 로 `CODEX_IMAGE_COMMAND` 를 실행한다.
4. 결과 이미지는 OpenAI API 없이 `/tmp/codex-image-*.png` 로 저장된다.
5. `_publish_image_url` 가 `IMAGE_PUBLIC_URL_COMMAND` 를 실행해 공개 HTTPS URL 을 받는다.
6. draft 에는 `image_local_path` 와 `image_url` 이 저장된다.
7. Slack에는 로컬 이미지 미리보기가 올라가고, 승인 시 각 플랫폼 업로더에는 `image_url` 이 전달된다.

실패해도 다른 이미지 생성 서비스로 폴백하지 않는다. Slack에서 결과를 보고 재시도한다.

## 셋업

`.env` 에 아래 값을 채운다.

```dotenv
CODEX_IMAGE_COMMAND=.venv/bin/python scripts/generate_codex_image.py --prompt {prompt} --output {output} --width {width} --height {height}
CODEX_IMAGE_TIMEOUT_SEC=600
IMAGE_PLATFORMS=instagram,threads,x

IMAGE_PUBLIC_URL_COMMAND=.venv/bin/python scripts/upload_cloudinary.py --file {file}
IMAGE_PUBLIC_URL_TIMEOUT_SEC=180
```

`CODEX_IMAGE_COMMAND` 는 고정 이미지 프롬프트가 아니다. 여기에 넣는 것은 이미지 생성 실행기이고,
매 회차 완성된 글에서 새로 만든 프롬프트가 `{prompt}` 로 전달된다. 기본 실행기
`scripts/generate_codex_image.py` 는 OpenAI API 키 없이 로컬에서 PNG 를 `{output}` 경로에 저장한다.

사용 가능한 플레이스홀더:

- `{prompt}`: 이미지 생성 프롬프트
- `{output}`: 생성된 이미지가 저장되어야 하는 PNG 경로
- `{width}`: 기본 `1024`
- `{height}`: 기본 `1024`

`IMAGE_PLATFORMS` 로 이미지 생성 대상 채널을 조절할 수 있다. 기본값은 `instagram,threads,x`.

`IMAGE_PUBLIC_URL_COMMAND` 는 `{file}` 플레이스홀더를 사용할 수 있고, stdout 마지막 줄에 `https://...`
형태의 공개 이미지 URL 을 출력해야 한다. Instagram Graph API 는 로컬 파일을 직접 받지 않기 때문에 이 단계가 필요하다.

## Cloudinary 설정

Cloudinary를 쓰려면 `.env` 에 아래 중 하나를 설정한다. API key/secret 이 있으면 signed upload 를 우선 사용한다.

Unsigned upload preset 방식:

```dotenv
CLOUDINARY_CLOUD_NAME=
CLOUDINARY_UPLOAD_PRESET=
CLOUDINARY_FOLDER=content-bot-ai
IMAGE_PUBLIC_URL_COMMAND=.venv/bin/python scripts/upload_cloudinary.py --file {file}
```

Signed upload 방식:

```dotenv
CLOUDINARY_CLOUD_NAME=
CLOUDINARY_API_KEY=
CLOUDINARY_API_SECRET=
CLOUDINARY_FOLDER=content-bot-ai
IMAGE_PUBLIC_URL_COMMAND=.venv/bin/python scripts/upload_cloudinary.py --file {file}
```

## 출력 예시

```yaml
image_keyword: "two friends, korean and japanese girls, cozy tokyo cafe, soft afternoon light, candid, no text, no watermark"
image_local_path: "/tmp/codex-image-1760000000-1234.png"
image_url: "https://cdn.example.com/content-bot/codex-image.png"
media_type: IMAGE
```
