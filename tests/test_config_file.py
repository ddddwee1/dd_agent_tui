"""Config file loading: precedence, native types, malformed-file safety."""

import importlib
import os

import pytest

import ddtui.config as cfg

_PREFIXES = ("DDTUI_", "DEEPSEEK_", "CODEX_", "BRAVE_")


@pytest.fixture
def load_config(tmp_path):
    """Reload ddtui.config against a given config.toml + env overlay.

    Wipes DDTUI_/provider env vars first so a developer's shell can't
    leak into assertions; restores everything and reloads once more on
    teardown so later tests see pristine module state.
    """
    saved_env = dict(os.environ)

    def _load(toml_text=None, env=None):
        os.environ.clear()
        os.environ.update(saved_env)
        for key in list(os.environ):
            if key.startswith(_PREFIXES):
                del os.environ[key]
        if toml_text is None:
            os.environ["DDTUI_CONFIG_FILE"] = str(tmp_path / "absent.toml")
        else:
            target = tmp_path / "config.toml"
            target.write_text(toml_text)
            os.environ["DDTUI_CONFIG_FILE"] = str(target)
        os.environ.update(env or {})
        return importlib.reload(cfg)

    yield _load
    os.environ.clear()
    os.environ.update(saved_env)
    importlib.reload(cfg)


def test_defaults_when_no_file(load_config):
    c = load_config()
    assert c.DEEPSEEK_MODEL == "deepseek-v4-flash"
    assert c.AUTO_COMPACT_THRESHOLD == 500_000
    assert c.CONFIG_FILE_KEY_COUNT == 0
    assert c.CONFIG_FILE_ERROR == ""
    assert c.CONFIG_FILE_UNUSED_KEYS == ()


def test_file_value_used(load_config):
    c = load_config('DDTUI_AUTO_COMPACT_THRESHOLD = 0.3\n')
    assert c.AUTO_COMPACT_THRESHOLD == 0.3
    assert c.CONFIG_FILE_KEY_COUNT == 1
    assert c.CONFIG_FILE_UNUSED_KEYS == ()


def test_env_overrides_file(load_config):
    c = load_config(
        'DDTUI_AUTO_COMPACT_THRESHOLD = 0.3\n',
        env={"DDTUI_AUTO_COMPACT_THRESHOLD": "0.5"},
    )
    assert c.AUTO_COMPACT_THRESHOLD == 0.5
    # Env-shadowed keys still count as consumed, not as typos.
    assert c.CONFIG_FILE_UNUSED_KEYS == ()


@pytest.mark.parametrize("value, expected", [
    ("500000", 500_000),
    ('"250000"', 250_000),
    ("0", 0),
    ("0.99", 0.95),
    ('"invalid"', 500_000),
    ('""', 500_000),
])
def test_auto_compact_token_settings_and_fallback(load_config, value, expected):
    c = load_config(f"DDTUI_AUTO_COMPACT_THRESHOLD = {value}\n")
    assert c.AUTO_COMPACT_THRESHOLD == expected
    assert c.CONFIG_FILE_UNUSED_KEYS == ()


def test_toml_native_types(load_config):
    c = load_config(
        'DDTUI_CONFIRM_WRITES = false\n'
        'DDTUI_REMOTE_PORT = 12345\n'
        'DEEPSEEK_MODEL = "test-model"\n'
        'DDTUI_NO_RIPGREP = true\n'
    )
    assert c.CONFIRM_WRITES is False
    assert c.REMOTE_PORT == 12345
    assert c.MODEL == "test-model"
    assert c.NO_RIPGREP is True


def test_string_forms_still_parse(load_config):
    c = load_config(
        'DDTUI_CONFIRM_WRITES = "off"\n'
        'DDTUI_REMOTE_PORT = "23456"\n'
    )
    assert c.CONFIRM_WRITES is False
    assert c.REMOTE_PORT == 23456


def test_malformed_file_is_ignored_with_error(load_config):
    c = load_config('DDTUI_CONFIRM_WRITES = [unclosed\n')
    assert c.CONFIG_FILE_ERROR
    assert c.CONFIG_FILE_KEY_COUNT == 0
    assert c.CONFIRM_WRITES is True  # default, not crash


def test_unknown_keys_reported(load_config):
    c = load_config(
        'DDTUI_AUTO_COMPACT_THRESHOLD = 0.3\n'
        'DDTUI_AUTOCOMPACT_TRESHOLD = 0.4\n'  # typo on purpose
    )
    assert c.CONFIG_FILE_UNUSED_KEYS == ("DDTUI_AUTOCOMPACT_TRESHOLD",)
    assert c.AUTO_COMPACT_THRESHOLD == 0.3


def test_output_budgets_are_configurable(load_config):
    values = {
        "BASH_OUTPUT_MAX_CHARS": 64000,
        "READ_FILE_MAX_LINES": 1000,
        "READ_FILE_MAX_LINE_CHARS": 16000,
        "READ_FILE_MAX_TOTAL_CHARS": 96000,
        "READ_FILES_MAX_TOTAL_CHARS": 192000,
        "TOOL_OUTPUT_MAX_CHARS": 256000,
        "TOOL_HISTORY_MAX_CHARS": 512000,
        "TOOL_HISTORY_SNIPPET_CHARS": 16000,
    }
    c = load_config("\n".join(f"DDTUI_{key} = {value}" for key, value in values.items()),
                    env={"DDTUI_BASH_OUTPUT_MAX_CHARS": "80000"})
    for key, value in values.items():
        assert getattr(c, key) == (80000 if key == "BASH_OUTPUT_MAX_CHARS" else value)
    assert c.CONFIG_FILE_UNUSED_KEYS == ()


def test_output_budget_defaults_and_invalid_settings(load_config):
    c = load_config()
    assert c.BASH_OUTPUT_MAX_CHARS == 32000
    assert c.READ_FILE_MAX_LINES == 500
    assert c.READ_FILE_MAX_TOTAL_CHARS == 48000
    assert c.READ_FILES_MAX_TOTAL_CHARS == 96000
    assert c.TOOL_OUTPUT_MAX_CHARS >= c.READ_FILES_MAX_TOTAL_CHARS
    assert c.TOOL_HISTORY_MAX_CHARS == 0
    c = load_config('DDTUI_BASH_OUTPUT_MAX_CHARS = "invalid"\n'
                    'DDTUI_READ_FILE_MAX_LINES = -1\n'
                    'DDTUI_TOOL_OUTPUT_MAX_CHARS = 0\n'
                    'DDTUI_TOOL_HISTORY_MAX_CHARS = -1\n')
    assert c.BASH_OUTPUT_MAX_CHARS == 32000
    assert c.READ_FILE_MAX_LINES == 1
    assert c.TOOL_OUTPUT_MAX_CHARS == 512
    assert c.TOOL_HISTORY_MAX_CHARS == 0
