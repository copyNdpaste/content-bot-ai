from __future__ import annotations

import pathlib
import unittest

from src.application.draft_documents import build_draft_frontmatter, build_draft_markdown
from src.domain import scheduling
from src.domain.content_rules import (
    ensure_required_landing_cta,
    fit_platform_limit,
    required_landing_cta,
)


FEATURE_PATH = pathlib.Path(__file__).with_name("features") / "content_publication.feature"


class ContentPublicationBehaviorTest(unittest.TestCase):
    def test_feature_file_documents_expected_behaviors(self):
        feature = FEATURE_PATH.read_text(encoding="utf-8")
        self.assertIn("Feature: Content publication guardrails", feature)
        self.assertIn("Scenario: Korean generated text", feature)
        self.assertIn("Scenario: Scheduler restart", feature)

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


if __name__ == "__main__":
    unittest.main()

