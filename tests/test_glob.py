"""Glob semantics: `**` spans directories, `*`/`?` never cross `/`,
patterns without `/` match basenames at any depth (rg convention)."""

from ddtui.tool_utils import _matches_glob


def test_doublestar_matches_root_level_files():
    # Regression: fnmatch-based impl missed top-level files entirely.
    assert _matches_glob("mod.py", "**/*.py")
    assert _matches_glob("a/mod.py", "**/*.py")
    assert _matches_glob("a/b/mod.py", "**/*.py")


def test_single_star_stays_within_one_component():
    # Regression: fnmatch's * crossed slashes.
    assert not _matches_glob("src/a/b.py", "src/*.py")
    assert _matches_glob("src/b.py", "src/*.py")


def test_basename_convenience_without_slash():
    assert _matches_glob("a/mod.py", "*.py")
    assert _matches_glob("mod.py", "*.py")
    assert not _matches_glob("a/mod.py", "*.txt")


def test_trailing_doublestar():
    assert _matches_glob("src/a/b.py", "src/**")
    assert not _matches_glob("other/a.py", "src/**")


def test_interior_doublestar_matches_zero_dirs():
    assert _matches_glob("a/z.py", "a/**/z.py")
    assert _matches_glob("a/b/c/z.py", "a/**/z.py")


def test_prefix_patterns():
    assert _matches_glob("test_a.py", "**/test_*.py")
    assert _matches_glob("x/test_a.py", "**/test_*.py")


def test_character_class():
    assert _matches_glob("a.py", "[ab].py")
    assert not _matches_glob("c.py", "[ab].py")


def test_question_mark_does_not_cross_slash():
    assert not _matches_glob("a/b", "a?b")
