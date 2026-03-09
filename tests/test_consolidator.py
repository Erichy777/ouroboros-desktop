"""Tests for ouroboros.consolidator."""
import json
import pathlib
import pytest
from unittest.mock import MagicMock, patch

from ouroboros.consolidator import (
    should_consolidate,
    consolidate,
    _load_meta,
    _save_meta,
    _count_lines,
    _read_chat_entries,
    _format_entries_for_consolidation,
    CONSOLIDATION_THRESHOLD,
)


@pytest.fixture
def tmp_paths(tmp_path):
    chat_path = tmp_path / "chat.jsonl"
    summary_path = tmp_path / "dialogue_summary.md"
    meta_path = tmp_path / "dialogue_meta.json"
    return chat_path, summary_path, meta_path


def _write_chat_entries(path, count):
    """Write count fake chat entries."""
    with path.open("w") as f:
        for i in range(count):
            direction = "in" if i % 2 == 0 else "out"
            entry = {
                "ts": f"2026-02-25T{10 + i // 60:02d}:{i % 60:02d}:00Z",
                "direction": direction,
                "text": f"Message {i}",
            }
            f.write(json.dumps(entry) + "\n")


def test_should_consolidate_no_chat(tmp_paths):
    _, _, meta_path = tmp_paths
    chat_path = pathlib.Path("/nonexistent/chat.jsonl")
    assert should_consolidate(meta_path, chat_path) is False


def test_should_consolidate_not_enough_messages(tmp_paths):
    chat_path, _, meta_path = tmp_paths
    _write_chat_entries(chat_path, 5)
    assert should_consolidate(meta_path, chat_path) is False


def test_should_consolidate_enough_messages(tmp_paths):
    chat_path, _, meta_path = tmp_paths
    _write_chat_entries(chat_path, CONSOLIDATION_THRESHOLD + 5)
    assert should_consolidate(meta_path, chat_path) is True


def test_should_consolidate_respects_offset(tmp_paths):
    chat_path, _, meta_path = tmp_paths
    _write_chat_entries(chat_path, CONSOLIDATION_THRESHOLD + 5)
    # Set offset to near the end
    _save_meta(meta_path, {"last_consolidated_offset": CONSOLIDATION_THRESHOLD})
    assert should_consolidate(meta_path, chat_path) is False


def test_consolidate_creates_summary(tmp_paths):
    chat_path, summary_path, meta_path = tmp_paths
    _write_chat_entries(chat_path, CONSOLIDATION_THRESHOLD + 5)

    mock_llm = MagicMock()
    mock_llm.chat.return_value = (
        {"content": "### Episode: 2026-02-25 10:00 – 10:24\n\nTest summary of events."},
        {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.001},
    )

    usage = consolidate(chat_path, summary_path, meta_path, mock_llm)

    assert usage is not None
    assert usage["cost"] == 0.001
    assert summary_path.exists()
    summary = summary_path.read_text()
    assert "Episode" in summary
    assert "Test summary" in summary

    # Check meta updated
    meta = _load_meta(meta_path)
    assert meta["last_consolidated_offset"] == CONSOLIDATION_THRESHOLD + 5
    assert meta["total_episodes"] == 1


def test_consolidate_not_enough_messages(tmp_paths):
    chat_path, summary_path, meta_path = tmp_paths
    _write_chat_entries(chat_path, 5)

    mock_llm = MagicMock()
    result = consolidate(chat_path, summary_path, meta_path, mock_llm)
    assert result is None
    mock_llm.chat.assert_not_called()


def test_load_save_meta(tmp_paths):
    _, _, meta_path = tmp_paths
    assert _load_meta(meta_path) == {}

    _save_meta(meta_path, {"last_consolidated_offset": 42, "total_episodes": 3})
    meta = _load_meta(meta_path)
    assert meta["last_consolidated_offset"] == 42
    assert meta["total_episodes"] == 3


def test_count_lines(tmp_paths):
    chat_path = tmp_paths[0]
    _write_chat_entries(chat_path, 15)
    assert _count_lines(chat_path) == 15


def test_should_consolidate_handles_log_rotation(tmp_paths):
    """After log rotation, offset > file size. should_consolidate must treat all lines as new."""
    chat_path, _, meta_path = tmp_paths
    _write_chat_entries(chat_path, CONSOLIDATION_THRESHOLD + 5)
    # Simulate rotation: offset far exceeds current file
    _save_meta(meta_path, {"last_consolidated_offset": 9999})
    assert should_consolidate(meta_path, chat_path) is True


def test_consolidate_resets_offset_on_rotation(tmp_paths):
    """After rotation, consolidate() should reset offset and process all entries."""
    chat_path, summary_path, meta_path = tmp_paths
    _write_chat_entries(chat_path, CONSOLIDATION_THRESHOLD + 5)
    _save_meta(meta_path, {"last_consolidated_offset": 9999})

    mock_llm = MagicMock()
    mock_llm.chat.return_value = (
        {"content": "### Episode: rotation recovery\n\nRecovered after rotation."},
        {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.001},
    )

    usage = consolidate(chat_path, summary_path, meta_path, mock_llm)
    assert usage is not None
    meta = _load_meta(meta_path)
    assert meta["last_consolidated_offset"] == CONSOLIDATION_THRESHOLD + 5
    assert summary_path.exists()


def test_format_entries():
    entries = [
        {"ts": "2026-02-25T10:00:00Z", "direction": "in", "text": "Hello"},
        {"ts": "2026-02-25T10:01:00Z", "direction": "out", "text": "Hi there"},
    ]
    formatted = _format_entries_for_consolidation(entries)
    assert "User: Hello" in formatted
    assert "Ouroboros: Hi there" in formatted
    assert "2026-02-25T10:00:00" in formatted
