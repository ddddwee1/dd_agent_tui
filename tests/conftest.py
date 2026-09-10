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
