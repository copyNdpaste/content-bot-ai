"""Domain rules for content-bot-ai.

This package must stay free of Slack, HTTP, subprocess, filesystem, and
database concerns. Workflow scripts can import these rules, but domain modules
must not import workflow scripts.
"""

