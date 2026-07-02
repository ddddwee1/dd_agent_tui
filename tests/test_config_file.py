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
    assert c.AUTO_COMPACT_THRESHOLD == 0.95
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
