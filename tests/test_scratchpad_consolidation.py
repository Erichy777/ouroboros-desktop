"""Tests for scratchpad auto-consolidation.

Verifies:
- Threshold is 30000 chars
- should_consolidate_scratchpad triggers correctly
- _rebuild_knowledge_index exists and works
- consolidate_scratchpad calls _rebuild_knowledge_index
"""
import importlib
import inspect
import os
import pathlib
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_consolidator():
    import sys
    sys.path.insert(0, REPO)
    return importlib.import_module("ouroboros.consolidator")


def test_consolidation_threshold_is_30000():
    """Scratchpad auto-consolidation must trigger at 30000."""
    mod = _get_consolidator()
    assert mod.SCRATCHPAD_CONSOLIDATION_THRESHOLD == 30000, (
        f"Expected 30000, got {mod.SCRATCHPAD_CONSOLIDATION_THRESHOLD}"
    )


def test_should_not_consolidate_small_scratchpad():
    mod = _get_consolidator()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("x" * 10000)
        f.flush()
        assert not mod.should_consolidate_scratchpad(pathlib.Path(f.name))
    os.unlink(f.name)


def test_should_consolidate_large_scratchpad():
    mod = _get_consolidator()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("x" * 35000)
        f.flush()
        assert mod.should_consolidate_scratchpad(pathlib.Path(f.name))
    os.unlink(f.name)


def test_should_not_consolidate_missing_file():
    mod = _get_consolidator()
    assert not mod.should_consolidate_scratchpad(pathlib.Path("/tmp/nonexistent_scratchpad.md"))


def test_rebuild_knowledge_index_exists():
    """_rebuild_knowledge_index function must exist in consolidator."""
    mod = _get_consolidator()
    assert hasattr(mod, "_rebuild_knowledge_index"), (
        "_rebuild_knowledge_index not found — knowledge index won't update after extraction"
    )


def test_rebuild_knowledge_index_creates_index():
    """_rebuild_knowledge_index must create index-full.md with topic entries."""
    mod = _get_consolidator()
    with tempfile.TemporaryDirectory() as tmpdir:
        kb_dir = pathlib.Path(tmpdir)
        (kb_dir / "topic-one.md").write_text("# Topic One\n\nSome content here.\n")
        (kb_dir / "topic-two.md").write_text("# Topic Two\n\nOther content.\n")
        mod._rebuild_knowledge_index(kb_dir)
        index_path = kb_dir / "index-full.md"
        assert index_path.exists(), "index-full.md was not created"
        index_text = index_path.read_text()
        assert "topic-one" in index_text
        assert "topic-two" in index_text


def test_rebuild_knowledge_index_skips_underscore_files():
    """Files starting with _ should be excluded from the index."""
    mod = _get_consolidator()
    with tempfile.TemporaryDirectory() as tmpdir:
        kb_dir = pathlib.Path(tmpdir)
        (kb_dir / "_private.md").write_text("# Private\n\nHidden.\n")
        (kb_dir / "visible.md").write_text("# Visible\n\nShown.\n")
        mod._rebuild_knowledge_index(kb_dir)
        index_text = (kb_dir / "index-full.md").read_text()
        assert "_private" not in index_text
        assert "visible" in index_text


def test_consolidate_scratchpad_calls_index_rebuild():
    """Scratchpad consolidation pipeline must call _rebuild_knowledge_index."""
    mod = _get_consolidator()
    source = inspect.getsource(mod._consolidate_scratchpad_locked)
    assert "_rebuild_knowledge_index" in source, (
        "scratchpad consolidation does not call _rebuild_knowledge_index — "
        "auto-extracted knowledge won't appear in context"
    )
