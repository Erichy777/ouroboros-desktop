"""Tests for tool-history compaction protection (context.py)."""
from ouroboros.context import compact_tool_history, _COMPACTION_PROTECTED_TOOLS


def _make_messages(tool_name: str, result_content: str, num_rounds: int = 8):
    """Build a message list with num_rounds of tool calls, all using the same tool."""
    messages = [{"role": "system", "content": [{"type": "text", "text": "system"}]}]
    for i in range(num_rounds):
        tc_id = f"call_{i}"
        messages.append({
            "role": "assistant",
            "content": f"Round {i}",
            "tool_calls": [{
                "id": tc_id,
                "function": {"name": tool_name, "arguments": "{}"},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": tc_id,
            "content": result_content,
        })
    return messages


def test_protected_tool_results_survive_compaction():
    """repo_commit results must not be truncated even in old rounds."""
    original_result = "OK: committed to ouroboros: v3.19.0 review feedback applied"
    msgs = _make_messages("repo_commit", original_result, num_rounds=10)
    compacted = compact_tool_history(msgs, keep_recent=3)

    commit_results = [
        m["content"] for m in compacted
        if m.get("role") == "tool" and m["content"] == original_result
    ]
    assert len(commit_results) == 10, "All repo_commit results must survive compaction"


def test_warning_results_survive_compaction():
    """Results starting with warning emoji must not be truncated."""
    warn_result = "\u26a0\ufe0f REVIEW_BLOCKED: tests failed, commit rejected. Fix errors first."
    msgs = _make_messages("run_shell", warn_result, num_rounds=10)
    compacted = compact_tool_history(msgs, keep_recent=3)

    warning_results = [
        m["content"] for m in compacted
        if m.get("role") == "tool" and m["content"] == warn_result
    ]
    assert len(warning_results) == 10, "Warning-prefixed results must survive compaction"


def test_normal_tool_results_are_compacted():
    """Non-protected tool results in old rounds should be compacted."""
    long_result = "x" * 500
    msgs = _make_messages("repo_read", long_result, num_rounds=10)
    compacted = compact_tool_history(msgs, keep_recent=3)

    old_tool_results = [
        m for m in compacted
        if m.get("role") == "tool" and m.get("content") != long_result
    ]
    assert len(old_tool_results) >= 4, "Old repo_read results should be compacted"
