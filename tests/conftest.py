"""Shared test setup: make `import ddtui` work from a bare checkout."""

from __future__ import annotations

import sys
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def isolated_history_archive(tmp_path, monkeypatch):
    """Compaction tests must never write into the user's real session archive."""
    import ddtui.history_archive as archive
    monkeypatch.setattr(archive, "HISTORY_ARCHIVE_DIR", tmp_path / "history_sources")


@pytest.fixture
def instant_api_retries(monkeypatch):
    """Record model backoff without delaying tests or changing UI timers."""
    from types import SimpleNamespace
    import ddtui.api_retry as retry

    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(retry, "API_MAX_RETRIES", 3)
    monkeypatch.setattr(retry, "asyncio", SimpleNamespace(sleep=sleep))
    return delays
