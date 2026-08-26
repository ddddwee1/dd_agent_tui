"""`/model` candidate lists: provider catalogs and their rendering."""

import json

import pytest

from ddtui import providers as prov
from ddtui.app_input import AppInputMixin
from ddtui.providers import (
    CodexResponsesProvider,
    DeepSeekProvider,
    LLMProvider,
    ModelChoice,
)
from ddtui.widgets import SlashPopup


def _codex():
    # Bypass __init__ so the test never touches Codex OAuth state.
    return CodexResponsesProvider.__new__(CodexResponsesProvider)


def _catalog(*models):
    return {"models": list(models)}


def _entry(slug, **kw):
    base = {
        "slug": slug,
        "visibility": "list",
        "supported_in_api": True,
        "context_window": 272000,
        "priority": 1,
    }
    base.update(kw)
    return base


class FakeApp(AppInputMixin):
    """Minimal stand-in exposing only what _model_choices_text reads."""

    def __init__(self, provider, model):
        self.provider = provider
        self.model = model


class TestDeepSeekCatalog:
    def test_lists_both_v4_models(self):
        ids = [c.id for c in DeepSeekProvider.__new__(
            DeepSeekProvider
        ).available_models()]
        assert ids == ["deepseek-v4-pro", "deepseek-v4-flash"]

    def test_every_entry_carries_a_note(self):
        for choice in DeepSeekProvider.MODELS:
            assert choice.note

    def test_default_model_is_in_the_catalog(self):
        ids = {c.id for c in DeepSeekProvider.MODELS}
        assert DeepSeekProvider.default_model in ids

    def test_caller_cannot_mutate_the_class_catalog(self):
        got = DeepSeekProvider.__new__(DeepSeekProvider).available_models()
        got.append(ModelChoice("bogus"))
        assert len(DeepSeekProvider.MODELS) == 2


class TestCodexCatalog:
    @pytest.fixture
    def cache(self, tmp_path, monkeypatch):
        path = tmp_path / "models_cache.json"
        monkeypatch.setattr(prov, "CODEX_MODELS_CACHE_PATH", path)
        return path

    def test_reads_slugs_from_the_cache(self, cache):
        cache.write_text(json.dumps(_catalog(_entry("gpt-5.5"))))
        assert [c.id for c in _codex().available_models()] == ["gpt-5.5"]

    def test_skips_hidden_and_non_api_models(self, cache):
        cache.write_text(json.dumps(_catalog(
            _entry("visible"),
            _entry("internal", visibility="hide"),
            _entry("cli-only", supported_in_api=False),
        )))
        assert [c.id for c in _codex().available_models()] == ["visible"]

    def test_orders_by_catalog_priority(self, cache):
        cache.write_text(json.dumps(_catalog(
            _entry("third", priority=16),
            _entry("first", priority=1),
            _entry("second", priority=7),
        )))
        assert [c.id for c in _codex().available_models()] == [
            "first", "second", "third"
        ]

    def test_unranked_models_sort_last(self, cache):
        cache.write_text(json.dumps(_catalog(
            _entry("unranked", priority=None),
            _entry("ranked", priority=42),
        )))
        assert [c.id for c in _codex().available_models()] == [
            "ranked", "unranked"
        ]

    def test_note_carries_context_and_efforts(self, cache):
        cache.write_text(json.dumps(_catalog(_entry(
            "gpt-5.5",
            supported_reasoning_levels=[
                {"effort": "low"}, {"effort": "xhigh"},
            ],
        ))))
        note = _codex().available_models()[0].note
        assert "272,000 ctx" in note
        assert "effort low/xhigh" in note

    def test_missing_cache_yields_no_candidates(self, cache):
        assert _codex().available_models() == []

    def test_malformed_cache_yields_no_candidates(self, cache):
        cache.write_text("{not json")
        assert _codex().available_models() == []

    def test_unexpected_shape_yields_no_candidates(self, cache):
        cache.write_text(json.dumps({"models": "nope"}))
        assert _codex().available_models() == []

    def test_entries_without_a_slug_are_dropped(self, cache):
        cache.write_text(json.dumps(_catalog(
            _entry("good"), _entry(""), {"visibility": "list"}, "junk",
        )))
        assert [c.id for c in _codex().available_models()] == ["good"]

    def test_context_limit_still_resolves_after_helper_extraction(self, cache):
        cache.write_text(json.dumps(_catalog(
            _entry("gpt-5.5", context_window=272000)
        )))
        assert _codex().context_limit_for_model("gpt-5.5") == 272000


class TestChoicesRendering:
    def test_marks_the_active_model(self):
        text = FakeApp(
            DeepSeekProvider.__new__(DeepSeekProvider), "deepseek-v4-flash"
        )._model_choices_text().plain
        assert "● deepseek-v4-flash" in text
        assert "○ deepseek-v4-pro" in text

    def test_shows_a_model_outside_the_catalog_as_current(self):
        text = FakeApp(
            DeepSeekProvider.__new__(DeepSeekProvider), "deepseek-chat"
        )._model_choices_text().plain
        assert "当前 model: deepseek-chat" in text
        assert "●" not in text

    def test_provider_without_catalog_degrades_to_usage_only(self):
        class Bare(LLMProvider):
            label = "Bare"

        text = FakeApp(Bare(), "anything")._model_choices_text().plain
        assert "当前 model: anything" in text
        assert "可选模型" not in text
        assert "/model <id>" in text


class ProbePopup(SlashPopup):
    """SlashPopup runnable outside a mounted app.

    Plain class attributes shadow Widget's `display` property and
    `update` method, so the filtering logic can be exercised without a
    textual runtime.
    """

    display = False

    def __init__(self, candidates=None):
        self.rendered = None
        self.arg_candidates = candidates

    def update(self, renderable, **kw):
        self.rendered = renderable

    @property
    def text(self):
        return self.rendered.plain if self.rendered is not None else ""


def _popup_for(model_provider):
    app = FakeApp(model_provider, "unused")
    return ProbePopup(app._slash_arg_candidates)


class TestSlashArgCandidates:
    def test_model_candidates_follow_the_provider(self):
        app = FakeApp(DeepSeekProvider.__new__(DeepSeekProvider), "x")
        assert [v for v, _ in app._slash_arg_candidates("/model")] == [
            "deepseek-v4-pro", "deepseek-v4-flash",
        ]

    def test_other_commands_have_no_candidates(self):
        app = FakeApp(DeepSeekProvider.__new__(DeepSeekProvider), "x")
        assert app._slash_arg_candidates("/save") == []


class TestSlashPopupArguments:
    def test_command_mode_still_matches_names(self):
        popup = ProbePopup()
        popup.update_for_text("/mod")
        assert popup.display
        assert "/model" in popup.text

    def test_space_switches_to_argument_mode(self):
        popup = _popup_for(DeepSeekProvider.__new__(DeepSeekProvider))
        popup.update_for_text("/model ")
        assert popup.display
        assert "/model 候选" in popup.text
        assert "deepseek-v4-pro" in popup.text
        assert "deepseek-v4-flash" in popup.text

    def test_argument_prefix_filters_candidates(self):
        popup = _popup_for(DeepSeekProvider.__new__(DeepSeekProvider))
        popup.update_for_text("/model deepseek-v4-f")
        assert popup.display
        assert "deepseek-v4-flash" in popup.text
        assert "deepseek-v4-pro" not in popup.text

    def test_no_match_hides(self):
        popup = _popup_for(DeepSeekProvider.__new__(DeepSeekProvider))
        popup.update_for_text("/model zzz")
        assert not popup.display

    def test_command_without_candidates_hides_after_space(self):
        popup = _popup_for(DeepSeekProvider.__new__(DeepSeekProvider))
        popup.update_for_text("/save my-session")
        assert not popup.display

    def test_second_token_hides(self):
        popup = _popup_for(DeepSeekProvider.__new__(DeepSeekProvider))
        popup.update_for_text("/model deepseek-v4-pro extra")
        assert not popup.display

    def test_newline_hides(self):
        popup = _popup_for(DeepSeekProvider.__new__(DeepSeekProvider))
        popup.update_for_text("/model\nsome prompt text")
        assert not popup.display

    def test_no_callback_configured_hides_after_space(self):
        popup = ProbePopup()
        popup.update_for_text("/model ")
        assert not popup.display

    def test_failing_callback_does_not_propagate(self):
        def boom(name):
            raise RuntimeError("catalog unreadable")

        popup = ProbePopup(boom)
        popup.update_for_text("/model ")
        assert not popup.display


class TestSlashPopupDoesNotShadowFrameworkHooks:
    def test_render_hook_is_textuals_own(self):
        """`_render` belongs to textual's Widget: it takes no arguments
        and must return a Visual. Shadowing it with a helper of our own
        crashes the app the first time the popup becomes visible."""
        from textual.widget import Widget

        assert SlashPopup._render is Widget._render


class TestSlashPopupCompletion:
    """Tab accepts the top row; `completion()` is what it would insert."""

    def test_bare_command_completes_without_trailing_space(self):
        popup = ProbePopup()
        popup.update_for_text("/cle")
        assert popup.completion() == "/clear"

    def test_command_with_placeholder_completes_into_argument_mode(self):
        popup = ProbePopup()
        popup.update_for_text("/mod")
        # Trailing space, so the very next update lands in argument mode
        # instead of re-matching the command name.
        assert popup.completion() == "/model "

    def test_placeholder_is_not_part_of_the_completion(self):
        popup = ProbePopup()
        popup.update_for_text("/sav")
        assert popup.completion() == "/save "

    def test_first_of_several_command_matches_wins(self):
        popup = ProbePopup()
        popup.update_for_text("/re")
        first = next(
            cmd for cmd, _ in SlashPopup.COMMANDS
            if cmd.startswith("/re")
        )
        assert popup.completion() == first.split(" ", 1)[0] + (
            " " if " " in first else ""
        )

    def test_argument_mode_completes_the_top_candidate(self):
        popup = _popup_for(DeepSeekProvider.__new__(DeepSeekProvider))
        popup.update_for_text("/model ")
        assert popup.completion() == "/model deepseek-v4-pro"

    def test_argument_completion_respects_the_typed_prefix(self):
        popup = _popup_for(DeepSeekProvider.__new__(DeepSeekProvider))
        popup.update_for_text("/model deepseek-v4-f")
        assert popup.completion() == "/model deepseek-v4-flash"

    def test_completing_twice_is_stable(self):
        popup = _popup_for(DeepSeekProvider.__new__(DeepSeekProvider))
        popup.update_for_text("/model dee")
        once = popup.completion()
        popup.update_for_text(once)
        assert popup.completion() == once

    def test_hidden_popup_offers_nothing(self):
        popup = _popup_for(DeepSeekProvider.__new__(DeepSeekProvider))
        popup.update_for_text("/model zzz")
        assert not popup.display
        assert popup.completion() is None

    def test_completion_clears_when_input_stops_being_a_command(self):
        popup = ProbePopup()
        popup.update_for_text("/cle")
        assert popup.completion() is not None
        popup.update_for_text("just a prompt")
        assert popup.completion() is None


class TestInputBindsTab:
    def test_tab_is_bound_with_priority(self):
        from ddtui.widgets import MultilineInput

        tab = [b for b in MultilineInput.BINDINGS if b.key == "tab"]
        assert len(tab) == 1
        # Without priority the App-level focus_next binding wins and the
        # completion action never fires.
        assert tab[0].priority
        assert tab[0].action == "do_complete"
