"""HistoryStore: stable identity, sequence behavior, serialization boundary."""

import json

import pytest

from ddtui.history_store import HistoryStore


def _msg(role, content):
    return {"role": role, "content": content}


def test_sequence_basics():
    store = HistoryStore([_msg("system", "s")])
    store.append(_msg("user", "u"))
    store.extend([_msg("assistant", "a")])
    assert len(store) == 3
    assert store[0]["role"] == "system"
    assert store[-1]["role"] == "assistant"
    assert [m["role"] for m in store] == ["system", "user", "assistant"]
    assert _msg("user", "u") in store


def test_slice_read_returns_plain_list():
    store = HistoryStore([_msg("system", "s"), _msg("user", "u")])
    span = store[0:2]
    assert type(span) is list
    assert span[0] is store[0]  # same dicts, shallow


def test_slice_assignment_mutates_in_place():
    store = HistoryStore(
        [_msg("system", "s"), _msg("user", "u"), _msg("assistant", "a")]
    )
    store[1:3] = [_msg("system", "summary")]
    assert [m["content"] for m in store] == ["s", "summary"]


def test_replace_all_keeps_identity():
    store = HistoryStore([_msg("system", "s")])
    alias = store
    store.replace_all([_msg("user", "fresh")])
    assert alias is store
    assert list(alias) == [_msg("user", "fresh")]


def test_replace_all_from_own_view_is_safe():
    store = HistoryStore([_msg("system", "s"), _msg("user", "u")])
    store.replace_all(store[:1] + [_msg("system", "sum")])
    assert [m["content"] for m in store] == ["s", "sum"]


def test_replace_span():
    store = HistoryStore(
        [_msg("system", "s"), _msg("user", "u"), _msg("assistant", "a"),
         _msg("assistant", "end")]
    )
    store.replace_span(1, 3, [_msg("system", "摘要")])
    assert [m["content"] for m in store] == ["s", "摘要", "end"]


def test_eq_against_list_and_store():
    store = HistoryStore([_msg("user", "u")])
    assert store == [_msg("user", "u")]
    assert store == HistoryStore([_msg("user", "u")])
    assert store != [_msg("user", "x")]


def test_snapshot_is_plain_list_shallow_copy():
    store = HistoryStore([_msg("user", "u")])
    snap = store.snapshot()
    assert type(snap) is list
    assert snap == list(store)
    snap.append(_msg("user", "extra"))
    assert len(store) == 1  # detached container
    assert snap[0] is store[0]  # shared message dicts
    json.dumps(snap)  # serializable


def test_store_itself_is_not_json_serializable():
    store = HistoryStore([_msg("user", "u")])
    with pytest.raises(TypeError):
        json.dumps(store)


def test_app_messages_assignment_is_in_place():
    """`self.messages = new` on the real app class must keep identity."""
    from ddtui.app import AgentApp

    app = object.__new__(AgentApp)  # skip heavyweight __init__
    app._history = HistoryStore([_msg("system", "s")])
    shared = app.messages  # what a running TurnEngine would capture

    app.messages = [_msg("system", "s"), _msg("user", "compacted")]

    assert app.messages is shared
    assert [m["content"] for m in shared] == ["s", "compacted"]
