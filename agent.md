# Agent Notes

## Architecture Boundaries

This project keeps the existing script entrypoints, but new code should follow
Clean Architecture boundaries:

- `src/domain/`: pure business rules only. No filesystem, network, Slack,
  subprocess, database, or environment access.
- `src/application/`: use-case helpers that compose domain rules into
  workflow-ready structures. No external API calls.
- `src/workflow/`, `src/slack/`, `src/uploaders/`, `scripts/`: adapters and
  orchestration. These modules may read env, write files, call subprocesses, and
  talk to external services.

Do not duplicate publication rules in adapters. Import domain/application
helpers instead.

## BDD

BDD scenarios live under `tests/bdd/features/`. Executable scenario tests live
under `tests/bdd/` and use the Python standard library so they can run without
installing extra packages.

`pyproject.toml` also declares optional `dev` dependencies for teams that want
native `pytest-bdd` later:

```bash
python3 -m pip install -e ".[dev]"
```

Run:

```bash
PYTHONPYCACHEPREFIX=/tmp/contentbot-ai-pycache python3 -m unittest discover -s tests -p "test_*.py"
```

Current BDD coverage focuses on publication guardrails:

- required OnlyFriends landing CTA normalization
- platform character limit trimming while preserving CTA
- scheduler restart spacing so publication cannot happen in minute intervals
- application-layer draft markdown assembly

## Next Refactor Candidates

The next clean architecture extractions should be incremental and preserve
current CLI/script compatibility:

- `src/core/drafts.py`: parse/rewrite draft frontmatter, iterate draft files,
  normalize status transitions. Current duplicated sources include
  `content_pipeline.py`, `slack_notifier.py`, and `slack_approval_worker.py`.
- `src/application/upload_draft.py`: use case for draft upload lifecycle:
  parse draft, call uploader port, update status, update Slack, update learning
  artifacts.
- `src/ports/uploaders.py` and `src/adapters/subprocess_uploaders.py`: replace
  duplicated subprocess command construction in workflow and Slack approval
  code.
- `src/core/config.py`: shared repo paths and `.env` loading.
- `src/core/notifications.py`: Telegram/Slack notification adapters.

Keep old private workflow function names as delegating shims during migration so
launchd jobs and scripts keep working.

## Runtime Data

Runtime outputs stay out of git:

- `drafts/`
- `.runtime/`
- `var/`
- `.vscode/`
