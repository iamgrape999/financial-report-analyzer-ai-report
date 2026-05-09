"""Backward-compatible import shim for the Gemini generation client.

The implementation was renamed to ``gemini_client`` to reflect the actual
provider used by the pipeline. This module remains temporarily so older tests
or integrations importing ``credit_report.generation.claude_client`` continue
to work while callers migrate.
"""

from credit_report.generation.gemini_client import (  # noqa: F401
    MAX_CONTINUATION_ROUNDS,
    _detect_continuation_token,
    _strip_continuation_token,
    generate_section_markdown,
)
