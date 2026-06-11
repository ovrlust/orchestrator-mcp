"""Tests for the message bus (broadcast + directed + read-since)."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import messages as mb  # noqa: E402
import coordination as coord  # noqa: E402


def test_broadcast_seen_by_all(tmp_path):
    w = str(tmp_path)
    mb.post_message(w, "a1", "hello all")
    assert [m["text"] for m in mb.read_messages(w, "a2")] == ["hello all"]
    assert [m["text"] for m in mb.read_messages(w, "")] == ["hello all"]


def test_directed_only_to_recipient(tmp_path):
    w = str(tmp_path)
    mb.post_message(w, "a1", "for you", to="a2")
    assert [m["text"] for m in mb.read_messages(w, "a2")] == ["for you"]
    assert mb.read_messages(w, "a3") == []  # not the recipient
    assert mb.read_messages(w, "")[0]["text"] == "for you"  # orchestrator sees all


def test_sender_sees_own_directed(tmp_path):
    w = str(tmp_path)
    mb.post_message(w, "a1", "q", to="a2")
    assert [m["text"] for m in mb.read_messages(w, "a1")] == ["q"]


def test_seq_increments_and_since_filters(tmp_path):
    w = str(tmp_path)
    assert mb.post_message(w, "a1", "one") == 1
    assert mb.post_message(w, "a1", "two") == 2
    later = mb.read_messages(w, "a2", since=1)
    assert [m["text"] for m in later] == ["two"]


def test_logs_event(tmp_path):
    w = str(tmp_path)
    mb.post_message(w, "a1", "hi", to="a2")
    assert any(e["type"] == "message" and e["to"] == "a2" for e in coord.events_tail(w))


def test_coord_clear_wipes_messages(tmp_path):
    w = str(tmp_path)
    mb.post_message(w, "a1", "hi")
    coord.coord_clear(w)
    assert mb.read_messages(w) == []
