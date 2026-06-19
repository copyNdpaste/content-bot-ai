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

  Scenario: The scheduler expands all targets from environment configuration
    Given a request to publish to all platforms
    When the scheduler reads configured routine platforms
    Then only the configured platform list is selected in order

  Scenario: Disabled account targets are skipped before generation
    Given instagram is disabled for the Japanese account
    When the platform pack is prepared for the Japanese account
    Then instagram is excluded and the remaining platforms are kept

  Scenario: Image generation can be enabled for every platform
    Given IMAGE_PLATFORMS is configured as all
    When the workflow checks whether X needs an image
    Then image generation is enabled for X

  Scenario: Platform pack image is shared per scheduler round
    Given a platform pack for Korean and Japanese accounts
    When the account pack prepares Slack review cards
    Then every draft receives the same round image URL

  Scenario: Draft markdown round-trips through the application layer
    Given a draft with frontmatter and a body
    When the draft markdown is parsed and rendered again
    Then the status and body are preserved

  Scenario: Cooldown upload failures queue drafts for retry
    Given a draft upload fails with a platform cooldown
    When the draft lifecycle queues the draft
    Then the retry time, cooldown reason, and escaped error are stored

  Scenario: Successful uploads mark drafts as posted
    Given a draft upload returns a permalink and platform post id
    When the draft lifecycle marks the draft as posted
    Then the posted timestamp, permalink, and platform post id are stored

  Scenario: Instagram and Threads require Slack approval while X stays manual
    Given generated drafts for Instagram, Threads, and X
    When the workflow sends Slack publication notices
    Then Instagram and Threads use approval mode
    And X uses the manual compose mode

  Scenario: X manual Slack card opens a prefilled compose flow
    Given an X draft with text and an image URL
    When the manual Slack card is built
    Then the card has a prefilled X compose button
    And the card keeps a separate image button

  Scenario: Instagram publish retries while media is still processing
    Given Instagram reports that media is not yet available
    When the Instagram uploader publishes the media container
    Then it waits for the container and retries publishing
