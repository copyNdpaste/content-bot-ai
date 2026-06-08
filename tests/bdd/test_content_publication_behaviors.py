from __future__ import annotations

import pathlib
import tempfile
import unittest
import urllib.parse
from unittest import mock

from src.application import draft_lifecycle
from src.application.draft_documents import (
    build_draft_frontmatter,
    build_draft_markdown,
    escape_frontmatter_value,
)
from src.domain import publication_targets, scheduling
from src.domain.content_rules import (
    ensure_required_landing_cta,
    fit_platform_limit,
    required_landing_cta,
)
from src.slack import slack_notifier
from src.uploaders import instagram_uploader
from src.workflow import content_pipeline


FEATURE_PATH = pathlib.Path(__file__).with_name("features") / "content_publication.feature"


class ContentPublicationBehaviorTest(unittest.TestCase):
    def test_feature_file_documents_expected_behaviors(self):
        feature = FEATURE_PATH.read_text(encoding="utf-8")
        self.assertIn("Feature: Content publication guardrails", feature)
        self.assertIn("Scenario: Korean generated text", feature)
        self.assertIn("Scenario: Scheduler restart", feature)
        self.assertIn("Scenario: Disabled account targets", feature)
        self.assertIn("Scenario: Cooldown upload failures", feature)
        self.assertIn("Scenario: Instagram and Threads auto upload while X stays manual", feature)
        self.assertIn("Scenario: X manual Slack card opens a prefilled compose flow", feature)

    def test_korean_text_always_ends_with_one_required_cta(self):
        # Given generated Korean text with an old landing link
        old = "오늘 일본 친구랑 카페 얘기하다가 생각난 것\nhttps://onlyfriends.tryproo.com/"

        # When the publication CTA rule is applied
        actual = ensure_required_landing_cta(old, account="kr", lang="ko")

        # Then the text ends with exactly one Korean OnlyFriends CTA
        cta = required_landing_cta("kr", "ko")
        self.assertTrue(actual.endswith(cta))
        self.assertEqual(actual.count(cta), 1)
        self.assertNotIn("\nhttps://onlyfriends.tryproo.com/\n", actual)

    def test_instagram_text_uses_app_store_search_cta_without_landing_url(self):
        # Given an Instagram caption with the old clickable-link CTA
        old = "일본 친구랑 카페 다녀온 이야기\n\n👉 일본 친구 진짜 만들어보고 싶으면 → https://onlyfriends.tryproo.com/"

        # When the Instagram CTA rule is applied
        actual = ensure_required_landing_cta(
            old,
            account="kr",
            lang="ko",
            platform="instagram",
        )

        # Then Instagram captions end with app-store search copy instead of a URL
        cta = required_landing_cta("kr", "ko", "instagram")
        self.assertTrue(actual.endswith(cta))
        self.assertEqual(actual.count(cta), 1)
        self.assertNotIn("onlyfriends.tryproo.com", actual)
        self.assertIn("앱스토어에서 OnlyFriends 검색하고 일본 친구 만들기", actual)

    def test_japanese_instagram_text_mentions_korean_friends_in_app_store_cta(self):
        cta = required_landing_cta("jp", "ja", "instagram")

        self.assertNotIn("onlyfriends.tryproo.com", cta)
        self.assertIn("韓国の友達作り", cta)

    def test_x_text_is_trimmed_without_losing_required_cta(self):
        # Given generated Korean text longer than the X character limit
        text = "한일 친구 이야기 " * 80

        # When the platform limit rule is applied for X
        actual = fit_platform_limit(text, platform="x", account="kr", lang="ko")

        # Then the result is within 280 characters and keeps the required CTA
        self.assertLessEqual(len(actual), 280)
        self.assertTrue(actual.endswith(required_landing_cta("kr", "ko")))

    def test_scheduler_restart_waits_for_remaining_two_hour_window(self):
        # Given a publication run started ten minutes ago
        now = 10_000
        last_started = now - 600
        min_hours, max_hours = scheduling.interval_bounds(min_hours="0.1", max_hours="0.5")

        # When the scheduler evaluates the minimum publication interval
        wait = scheduling.seconds_until_min_interval_elapsed(
            now=now,
            last_started_at=last_started,
            min_hours=min_hours,
        )

        # Then it waits for the remaining two hour cooldown window
        self.assertEqual((min_hours, max_hours), (2.0, 2.0))
        self.assertEqual(wait, 6600)

    def test_draft_markdown_is_built_by_application_layer(self):
        frontmatter = build_draft_frontmatter(
            platform="instagram",
            account="kr",
            lang="ko",
            theme="test",
            payload={
                "hook": "hook",
                "hashtags": ["one", "two"],
                "image_url": "https://example.com/image.png",
                "text": "body",
            },
            created_at="20260522-120000",
        )
        markdown = build_draft_markdown(frontmatter, "body")

        self.assertEqual(frontmatter["media_type"], "IMAGE")
        self.assertIn("platform: instagram", markdown)
        self.assertTrue(markdown.endswith("body\n"))

    def test_all_platforms_expand_from_environment_configuration(self):
        # Given a request to publish to all platforms
        requested = "all"

        # When the scheduler reads configured routine platforms
        actual = publication_targets.expand_targets(
            requested,
            env_value="instagram, threads",
            default=publication_targets.DEFAULT_PLATFORMS,
        )

        # Then only the configured platform list is selected in order
        self.assertEqual(actual, ["instagram", "threads"])

    def test_disabled_account_targets_are_skipped_before_generation(self):
        # Given instagram is disabled for the Japanese account
        disabled = publication_targets.disabled_target_set("instagram:jp, x:kr, malformed")

        # When the platform pack is prepared for the Japanese account
        actual = publication_targets.filter_disabled_targets(
            ["instagram", "threads", "x"],
            account="jp",
            disabled=disabled,
        )

        # Then instagram is excluded and the remaining platforms are kept
        self.assertEqual(actual, ["threads", "x"])

    def test_image_generation_can_be_enabled_for_every_platform(self):
        # Given IMAGE_PLATFORMS is configured as all
        config = "all"

        # When the workflow checks whether X needs an image
        actual = publication_targets.image_enabled_for("x", config)

        # Then image generation is enabled for X
        self.assertTrue(actual)

    def test_draft_markdown_round_trips_through_application_layer(self):
        # Given a draft with frontmatter and a body
        raw = "---\nstatus: pending\nplatform: threads\n---\n\nhello\nworld\n"

        # When the draft markdown is parsed and rendered again
        meta, body = draft_lifecycle.parse_draft_markdown(raw)
        rendered = draft_lifecycle.render_draft_markdown(meta, body)
        reparsed_meta, reparsed_body = draft_lifecycle.parse_draft_markdown(rendered)

        # Then the status and body are preserved
        self.assertEqual(reparsed_meta["status"], "pending")
        self.assertEqual(reparsed_meta["platform"], "threads")
        self.assertEqual(reparsed_body, "hello\nworld\n")

    def test_cooldown_upload_failures_queue_drafts_for_retry(self):
        # Given a draft upload fails with a platform cooldown
        meta = {"status": "pending", "platform": "instagram"}
        error = "rate limited\nCOOLDOWN_UNTIL=2026-05-22T12:00:00Z"

        # When the draft lifecycle queues the draft
        actual = draft_lifecycle.mark_queued(
            meta,
            queued_until="2026-05-22T12:00:00Z",
            error=error,
            queued_at="2026-05-22T10:00:00",
        )

        # Then the retry time, cooldown reason, and escaped error are stored
        self.assertEqual(actual["status"], "queued")
        self.assertEqual(actual["queued_until"], "2026-05-22T12:00:00Z")
        self.assertEqual(actual["queued_reason"], "cooldown")
        self.assertEqual(actual["last_error"], escape_frontmatter_value(error))
        self.assertEqual(meta["status"], "pending")

    def test_successful_uploads_mark_drafts_as_posted(self):
        # Given a draft upload returns a permalink and platform post id
        meta = {"status": "pending", "platform": "threads"}

        # When the draft lifecycle marks the draft as posted
        actual = draft_lifecycle.mark_posted(
            meta,
            posted_at="2026-05-22T10:30:00",
            permalink="https://threads.example/post/1",
            post_id="thread-1",
        )

        # Then the posted timestamp, permalink, and platform post id are stored
        self.assertEqual(actual["status"], "posted")
        self.assertEqual(actual["posted_at"], "2026-05-22T10:30:00")
        self.assertEqual(actual["permalink"], "https://threads.example/post/1")
        self.assertEqual(actual["platform_post_id"], "thread-1")
        self.assertEqual(meta["status"], "pending")

    def test_x_manual_upload_keeps_original_slack_card_available(self):
        # Given an X draft that already has a Slack card with text and image blocks
        raw = (
            "---\n"
            "status: auto_uploading\n"
            "platform: x\n"
            "image_url: https://example.com/x.png\n"
            "slack_channel: C123\n"
            "slack_ts: 123.456\n"
            "---\n\n"
            "manual upload text\n"
        )
        with tempfile.NamedTemporaryFile("w+", suffix=".md", encoding="utf-8") as f:
            f.write(raw)
            f.flush()

            # When the workflow marks X as manual upload required
            with mock.patch.object(content_pipeline, "_slack_update") as slack_update:
                result = content_pipeline._auto_upload_after_slack(f.name, "x", "kr")

            # Then it does not overwrite the Slack card with a status-only message
            self.assertEqual(result, {"ok": True, "manual": True})
            slack_update.assert_not_called()
            updated = pathlib.Path(f.name).read_text(encoding="utf-8")
            self.assertIn("status: manual_upload_required", updated)
            self.assertIn("image_url: https://example.com/x.png", updated)
            self.assertIn("manual upload text", updated)

    def test_instagram_threads_auto_upload_while_x_uses_manual_slack_mode(self):
        # Given generated drafts for Instagram, Threads, and X
        modes = {}
        uploads = []

        def fake_notify(path, platform, account, mode="approval"):
            modes[platform] = mode
            return {"ok": True, "result": {"channel": "C123", "ts": f"1.{platform}"}}

        def fake_auto_upload(path, platform, account):
            uploads.append(platform)
            if platform == "x":
                return {"ok": True, "manual": True}
            return {"ok": True, "permalink": f"https://example.com/{platform}"}

        patches = [
            mock.patch.object(content_pipeline, "_fetch_trends", return_value=[]),
            mock.patch.object(content_pipeline, "_load_style_context", return_value={}),
            mock.patch.object(content_pipeline, "_build_persona_prompt", return_value="prompt"),
            mock.patch.object(
                content_pipeline,
                "_call_codex_content",
                return_value={
                    "ok": True,
                    "text": "generated text",
                    "hook": "hook",
                    "hashtags": [],
                },
            ),
            mock.patch.object(content_pipeline, "_image_enabled_for", return_value=False),
            mock.patch.object(
                content_pipeline,
                "_write_draft",
                side_effect=lambda platform, account, lang, theme, payload: (
                    f"/tmp/{platform}-{account}.md"
                ),
            ),
            mock.patch.object(content_pipeline, "_insert_generation_artifact", return_value="artifact"),
            mock.patch.object(content_pipeline, "_notify_slack", side_effect=fake_notify),
            mock.patch.object(content_pipeline, "_update_generation_artifact"),
            mock.patch.object(content_pipeline, "_auto_upload_after_slack", side_effect=fake_auto_upload),
        ]

        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with patches[5], patches[6], patches[7], patches[8], patches[9]:
                for platform in ("instagram", "threads", "x"):
                    result = content_pipeline.run_round(platform, "kr", "theme")
                    self.assertTrue(result["ok"])

        # Then Instagram and Threads use automatic upload mode, while X stays manual
        self.assertEqual(modes, {
            "instagram": "auto",
            "threads": "auto",
            "x": "manual",
        })
        self.assertEqual(uploads, ["instagram", "threads", "x"])

    def test_x_manual_slack_card_has_prefilled_x_compose_button(self):
        body = "manual upload text\n\n👉 일본 친구 진짜 만들어보고 싶으면 → https://onlyfriends.tryproo.com/"
        blocks = slack_notifier._build_blocks(
            {"image_url": "https://example.com/x.png"},
            body,
            "x",
            "kr",
            "draft-1",
            mode="manual",
        )

        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        self.assertEqual(len(action_blocks), 1)
        buttons = action_blocks[0]["elements"]
        compose_url = buttons[0]["url"]
        parsed = urllib.parse.urlparse(compose_url)
        query = urllib.parse.parse_qs(parsed.query)

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "x.com")
        self.assertEqual(parsed.path, "/intent/tweet")
        self.assertEqual(query["text"], [body])
        self.assertEqual(buttons[1]["url"], "https://example.com/x.png")

    def test_instagram_publish_retries_when_media_is_not_ready(self):
        # Given Instagram accepts a media publish only after the container finishes processing
        calls = []

        def fake_post(url, payload):
            calls.append(payload)
            if len(calls) == 1:
                raise RuntimeError(
                    'IG API HTTP 400: {"error":{"message":"Media ID is not available",'
                    '"code":9007,"error_subcode":2207027}}'
                )
            return {"id": "media-1"}

        with mock.patch.object(instagram_uploader, "_http_post", side_effect=fake_post):
            with mock.patch.object(instagram_uploader, "_poll_container_status") as poll:
                with mock.patch.object(instagram_uploader.time, "sleep"):
                    actual = instagram_uploader._publish_container(
                        "ig-user", "creation-1", "token"
                    )

        self.assertEqual(actual, {"id": "media-1"})
        self.assertEqual(len(calls), 2)
        poll.assert_called_once_with("creation-1", "token", max_tries=6, interval=5)


if __name__ == "__main__":
    unittest.main()
