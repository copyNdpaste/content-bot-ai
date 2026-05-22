Feature: Content publication guardrails
  The content bot should keep business publication rules in the domain layer
  so Slack, scheduler, and upload workflows can share them safely.

  Scenario: Korean generated text always ends with the required landing CTA
    Given generated Korean text with an old landing link
    When the publication CTA rule is applied
    Then the text ends with exactly one Korean OnlyFriends CTA

  Scenario: X text is trimmed without losing the required CTA
    Given generated Korean text longer than the X character limit
    When the platform limit rule is applied for X
    Then the result is within 280 characters
    And the result still ends with the required Korean OnlyFriends CTA

  Scenario: Scheduler restart cannot publish again within minutes
    Given a publication run started ten minutes ago
    When the scheduler evaluates the minimum publication interval
    Then it waits for the remaining two hour cooldown window

