"""
Ouroboros context builder.

Assembles LLM context from prompts, memory, logs, and runtime state.
Extracted from agent.py to keep the agent thin and focused.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import pathlib
import re
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.utils import (
    utc_now_iso, read_text, clip_text, estimate_tokens, get_git_info,
)
from ouroboros.memory import Memory

log = logging.getLogger(__name__)


def _build_user_content(task: Dict[str, Any]) -> Any:
    """Build user message content. Supports text + optional image."""
    text = task.get("text", "")
    image_b64 = task.get("image_base64")
    image_mime = task.get("image_mime", "image/jpeg")
    image_caption = task.get("image_caption", "")

    if not image_b64:
        # Return fallback text if both text and image are empty
        if not text:
            return "(empty message)"
        return text

    # Multipart content with text + image
    parts = []
    # Combine caption and text for the text part
    combined_text = ""
    if image_caption:
        combined_text = image_caption
    if text and text != image_caption:
        combined_text = (combined_text + "\n" + text).strip() if combined_text else text

    # Always include a text part when there's an image
    if not combined_text:
        combined_text = "Analyze the screenshot"

    parts.append({"type": "text", "text": combined_text})
    parts.append({
        "type": "image_url",
        "image_url": {"url": f"data:{image_mime};base64,{image_b64}"}
    })
    return parts


def build_runtime_section(env: Any, task: Dict[str, Any]) -> str:
    """Build the runtime context section (utc_now, repo_dir, drive_root, git_head, git_branch, task info, budget info)."""
    # --- Git context ---
    try:
        git_branch, git_sha = get_git_info(env.repo_dir)
    except Exception:
        log.debug("Failed to get git info for context", exc_info=True)
        git_branch, git_sha = "unknown", "unknown"

    # --- Budget calculation ---
    budget_info = None
    try:
        state_json = safe_read(env.drive_path("state/state.json"), fallback="{}")
        state_data = json.loads(state_json)
        spent_usd = float(state_data.get("spent_usd", 0))
        total_usd = float(os.environ.get("TOTAL_BUDGET", "1"))
        remaining_usd = total_usd - spent_usd
        budget_info = {"total_usd": total_usd, "spent_usd": spent_usd, "remaining_usd": remaining_usd}
    except Exception:
        log.debug("Failed to calculate budget info for context", exc_info=True)
        pass

    # --- Runtime context JSON ---
    runtime_data = {
        "utc_now": utc_now_iso(),
        "repo_dir": str(env.repo_dir),
        "drive_root": str(env.drive_root),
        "git_head": git_sha,
        "git_branch": git_branch,
        "task": {"id": task.get("id"), "type": task.get("type")},
    }
    if budget_info:
        runtime_data["budget"] = budget_info
    runtime_ctx = json.dumps(runtime_data, ensure_ascii=False, indent=2)
    return "## Runtime context\n\n" + runtime_ctx


def build_memory_sections(memory: Memory) -> List[str]:
    """Build scratchpad, identity, dialogue summary sections."""
    sections = []

    scratchpad_raw = memory.load_scratchpad()
    sections.append("## Scratchpad\n\n" + clip_text(scratchpad_raw, 90000))

    identity_raw = memory.load_identity()
    sections.append("## Identity\n\n" + clip_text(identity_raw, 80000))

    # Dialogue summary (key moments from chat history)
    summary_path = memory.drive_root / "memory" / "dialogue_summary.md"
    if summary_path.exists():
        summary_text = read_text(summary_path)
        if summary_text.strip():
            sections.append("## Dialogue Summary\n\n" + clip_text(summary_text, 80000))

    return sections


def build_recent_sections(memory: Memory, env: Any, task_id: str = "") -> List[str]:
    """Build recent chat, recent progress, recent tools, recent events sections."""
    sections = []

    chat_summary = memory.summarize_chat(
        memory.read_jsonl_tail("chat.jsonl", 800))
    if chat_summary:
        sections.append("## Recent chat\n\n" + chat_summary)

    progress_entries = memory.read_jsonl_tail("progress.jsonl", 200)
    if task_id:
        progress_entries = [e for e in progress_entries if e.get("task_id") == task_id]
    progress_summary = memory.summarize_progress(progress_entries, limit=15)
    if progress_summary:
        sections.append("## Recent progress\n\n" + progress_summary)

    tools_entries = memory.read_jsonl_tail("tools.jsonl", 200)
    if task_id:
        tools_entries = [e for e in tools_entries if e.get("task_id") == task_id]
    tools_summary = memory.summarize_tools(tools_entries)
    if tools_summary:
        sections.append("## Recent tools\n\n" + tools_summary)

    events_entries = memory.read_jsonl_tail("events.jsonl", 200)
    if task_id:
        events_entries = [e for e in events_entries if e.get("task_id") == task_id]
    events_summary = memory.summarize_events(events_entries)
    if events_summary:
        sections.append("## Recent events\n\n" + events_summary)

    supervisor_summary = memory.summarize_supervisor(
        memory.read_jsonl_tail("supervisor.jsonl", 200))
    if supervisor_summary:
        sections.append("## Supervisor\n\n" + supervisor_summary)

    # -- Execution reflections (process memory) --
    reflections_path = pathlib.Path(memory.drive_root) / "logs" / "task_reflections.jsonl"
    if reflections_path.exists():
        try:
            raw_lines = reflections_path.read_text(encoding="utf-8").strip().splitlines()
            recent_reflections = []
            for raw_line in raw_lines[-20:]:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                    ts = str(entry.get("ts", ""))[:16]
                    ttype = entry.get("task_type", "task")
                    rounds = entry.get("rounds", 0)
                    cost = entry.get("cost_usd", 0)
                    markers = ", ".join(entry.get("key_markers", []))
                    reflection = str(entry.get("reflection", ""))[:500]
                    recent_reflections.append(
                        f"  [{ts}] {ttype} ({rounds}r, ${cost:.2f}) [{markers}]: {reflection}"
                    )
                except Exception:
                    pass
            if recent_reflections:
                sections.append(
                    "## Execution reflections (process memory)\n"
                    + "\n".join(recent_reflections)
                )
        except Exception:
            pass

    return sections


def build_health_invariants(env: Any) -> str:
    """Build health invariants section for LLM-first self-detection.

    Surfaces anomalies as informational text. The LLM (not code) decides
    what action to take based on what it reads here. (Bible P0+P3)
    """
    checks = []

    # 1. Version sync: VERSION file vs pyproject.toml, README badge, ARCHITECTURE.md header
    try:
        ver_file = read_text(env.repo_path("VERSION")).strip()
        desync_parts = []

        pyproject = read_text(env.repo_path("pyproject.toml"))
        pyproject_ver = ""
        for line in pyproject.splitlines():
            if line.strip().startswith("version"):
                pyproject_ver = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
        if ver_file and pyproject_ver and ver_file != pyproject_ver:
            desync_parts.append(f"pyproject.toml={pyproject_ver}")

        try:
            readme = read_text(env.repo_path("README.md"))
            readme_match = (
                re.search(r'version-(\d+\.\d+\.\d+)', readme, re.IGNORECASE)
                or re.search(r'\*\*Version:\*\*\s*(\d+\.\d+\.\d+)', readme)
            )
            if readme_match and readme_match.group(1) != ver_file:
                desync_parts.append(f"README={readme_match.group(1)}")
        except Exception:
            pass

        try:
            arch = read_text(env.repo_path("docs/ARCHITECTURE.md"))
            arch_match = re.search(r'# Ouroboros v(\d+\.\d+\.\d+)', arch)
            if arch_match and arch_match.group(1) != ver_file:
                desync_parts.append(f"ARCHITECTURE.md={arch_match.group(1)}")
        except Exception:
            pass

        if desync_parts:
            checks.append(f"CRITICAL: VERSION DESYNC — VERSION={ver_file}, {', '.join(desync_parts)}")
        elif ver_file:
            checks.append(f"OK: version sync ({ver_file})")
    except Exception:
        pass

    # 2. Budget drift
    try:
        state_json = read_text(env.drive_path("state/state.json"))
        state_data = json.loads(state_json)
        if state_data.get("budget_drift_alert"):
            drift_pct = state_data.get("budget_drift_pct", 0)
            our = state_data.get("spent_usd", 0)
            theirs = state_data.get("openrouter_total_usd", 0)
            checks.append(f"WARNING: BUDGET DRIFT {drift_pct:.1f}% — tracked=${our:.2f} vs OpenRouter=${theirs:.2f}")
        else:
            checks.append("OK: budget drift within tolerance")
    except Exception:
        pass

    # 3. Per-task cost anomalies
    try:
        from supervisor.state import per_task_cost_summary
        costly = [t for t in per_task_cost_summary(5) if t["cost"] > 5.0]
        for t in costly:
            checks.append(
                f"WARNING: HIGH-COST TASK — task_id={t['task_id']} "
                f"cost=${t['cost']:.2f} rounds={t['rounds']}"
            )
        if not costly:
            checks.append("OK: no high-cost tasks (>$5)")
    except Exception:
        pass

    # 4. Stale identity.md
    try:
        import time as _time
        identity_path = env.drive_path("memory/identity.md")
        if identity_path.exists():
            age_hours = (_time.time() - identity_path.stat().st_mtime) / 3600
            if age_hours > 8:
                checks.append(f"WARNING: STALE IDENTITY — identity.md last updated {age_hours:.0f}h ago")
            else:
                checks.append("OK: identity.md recent")
    except Exception:
        pass

    # 5. Memory health: thin identity, empty/bloated scratchpad
    try:
        identity_content = read_text(env.drive_path("memory/identity.md"))
        if len(identity_content.strip()) < 200:
            checks.append(f"WARNING: THIN IDENTITY — identity.md is only {len(identity_content)} chars. Cognitive decay signal.")
    except Exception:
        pass

    try:
        scratchpad_content = read_text(env.drive_path("memory/scratchpad.md"))
        sp_len = len(scratchpad_content.strip())
        if sp_len < 50:
            checks.append("WARNING: EMPTY SCRATCHPAD — scratchpad is nearly empty. Memory loss signal.")
        elif sp_len > 50000:
            checks.append(f"WARNING: BLOATED SCRATCHPAD — {sp_len} chars. Extract durable insights to knowledge base.")
        else:
            checks.append(f"OK: scratchpad size ({sp_len} chars)")
    except Exception:
        pass

    # 6. Crash rollback detection
    try:
        crash_report = env.drive_path("state/crash_report.json")
        if crash_report.exists():
            crash_data = json.loads(crash_report.read_text(encoding="utf-8"))
            checks.append(
                f"CRITICAL: RECENT CRASH ROLLBACK — rolled back from "
                f"{crash_data.get('rolled_back_from', '?')[:12]} to tag "
                f"{crash_data.get('tag', '?')} at {crash_data.get('ts', '?')}"
            )
    except Exception:
        pass

    # 7. Prompt-runtime drift: CONSCIOUSNESS.md references vs BG whitelist
    try:
        from ouroboros.consciousness import BackgroundConsciousness
        consciousness_md = safe_read(env.repo_path("prompts/CONSCIOUSNESS.md"))
        if consciousness_md:
            whitelist = BackgroundConsciousness._BG_TOOL_WHITELIST
            scan_text = re.sub(r'```.*?```', '', consciousness_md, flags=re.DOTALL)
            _TOOL_PREFIXES = (
                "schedule_", "update_", "knowledge_", "browse_", "analyze_",
                "web_", "send_", "repo_", "data_", "chat_", "list_", "get_",
                "wait_", "set_", "memory_",
            )
            prompt_tool_refs = set()
            for m in re.finditer(r'\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b', scan_text):
                candidate = m.group(1)
                if candidate in whitelist or any(candidate.startswith(p) for p in _TOOL_PREFIXES):
                    prompt_tool_refs.add(candidate)
            phantom = prompt_tool_refs - whitelist
            if phantom:
                checks.append(
                    f"WARNING: PROMPT-RUNTIME DRIFT — CONSCIOUSNESS.md references "
                    f"tools not in BG whitelist: {', '.join(sorted(phantom))}"
                )
            else:
                checks.append("OK: prompt-runtime sync (no phantom tools)")
    except Exception:
        pass

    # 8. Duplicate processing detection: same owner message text appearing in multiple tasks
    try:
        import hashlib
        msg_hash_to_tasks: Dict[str, set] = {}
        tail_bytes = 256_000

        def _scan_file_for_injected(path, type_field="type", type_value="owner_message_injected"):
            if not path.exists():
                return
            file_size = path.stat().st_size
            with path.open("r", encoding="utf-8") as f:
                if file_size > tail_bytes:
                    f.seek(file_size - tail_bytes)
                    f.readline()
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        if ev.get(type_field) != type_value:
                            continue
                        text = ev.get("text", "")
                        if not text and "event_repr" in ev:
                            # Historical entries in supervisor.jsonl lack "text";
                            # try to extract task_id at least for presence detection
                            text = ev.get("event_repr", "")[:200]
                        if not text:
                            continue
                        text_hash = hashlib.md5(text.encode()).hexdigest()[:12]
                        tid = ev.get("task_id") or "unknown"
                        if text_hash not in msg_hash_to_tasks:
                            msg_hash_to_tasks[text_hash] = set()
                        msg_hash_to_tasks[text_hash].add(tid)
                    except (json.JSONDecodeError, ValueError):
                        continue

        _scan_file_for_injected(env.drive_path("logs/events.jsonl"))
        # Also check supervisor.jsonl for historically unhandled events
        _scan_file_for_injected(
            env.drive_path("logs/supervisor.jsonl"),
            type_field="event_type",
            type_value="owner_message_injected",
        )

        dupes = {h: tids for h, tids in msg_hash_to_tasks.items() if len(tids) > 1}
        if dupes:
            checks.append(
                f"CRITICAL: DUPLICATE PROCESSING — {len(dupes)} message(s) "
                f"appeared in multiple tasks: {', '.join(str(sorted(tids)) for tids in dupes.values())}"
            )
        else:
            checks.append("OK: no duplicate message processing detected")
    except Exception:
        pass

    if not checks:
        return ""
    return "## Health Invariants\n\n" + "\n".join(f"- {c}" for c in checks)


def _build_registry_digest(env: Any) -> str:
    """Build a compact one-line-per-source digest from memory/registry.md.

    Returns a markdown table capped at 3000 chars, or empty string if
    the registry doesn't exist.
    """
    reg_path = env.drive_path("memory/registry.md")
    if not reg_path.exists():
        return ""
    try:
        text = reg_path.read_text(encoding="utf-8")
    except Exception:
        return ""

    rows: list = []
    current_id = ""
    fields: dict = {}
    for line in text.split("\n"):
        if line.startswith("### "):
            if current_id:
                rows.append(_registry_row(current_id, fields))
            current_id = line[4:].strip()
            fields = {}
        elif current_id and line.startswith("- **"):
            # Parse "- **Key:** value"
            m = re.match(r'^- \*\*(\w+):\*\*\s*(.*)', line)
            if m:
                fields[m.group(1).lower()] = m.group(2).strip()
    if current_id:
        rows.append(_registry_row(current_id, fields))

    if not rows:
        return ""

    header = "| source | path | updated | gaps |\n|---|---|---|---|"
    table = header + "\n" + "\n".join(rows)
    if len(table) > 3000:
        table = table[:2950] + "\n| ... | (truncated) | | |"
    return "## Memory Registry (what I know / don't know)\n\n" + table


def _registry_row(source_id: str, fields: dict) -> str:
    path = fields.get("path", "?")
    updated = fields.get("updated", "?")
    gaps = fields.get("gaps", "—")
    # Keep gaps short
    if len(gaps) > 60:
        gaps = gaps[:57] + "..."
    return f"| {source_id} | {path} | {updated} | {gaps} |"


def build_llm_messages(
    env: Any,
    memory: Memory,
    task: Dict[str, Any],
    review_context_builder: Optional[Any] = None,
    soft_cap_tokens: int = 200_000,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Build the full LLM message context for a task.

    Args:
        env: Env instance with repo_path/drive_path helpers
        memory: Memory instance for scratchpad/identity/logs
        task: Task dict with id, type, text, etc.
        review_context_builder: Optional callable for review tasks (signature: () -> str)

    Returns:
        (messages, cap_info) tuple:
            - messages: List of message dicts ready for LLM
            - cap_info: Dict with token trimming metadata
    """
    # --- Extract task type for adaptive context ---
    task_type = str(task.get("type") or "user")

    # --- Read base prompts and state ---
    base_prompt = safe_read(
        env.repo_path("prompts/SYSTEM.md"),
        fallback="You are Ouroboros. Your base prompt could not be loaded."
    )
    bible_md = safe_read(env.repo_path("BIBLE.md"))
    arch_md = safe_read(env.repo_path("docs/ARCHITECTURE.md"))
    dev_guide_md = safe_read(env.repo_path("docs/DEVELOPMENT.md"))
    readme_md = safe_read(env.repo_path("README.md"))
    checklists_md = safe_read(env.repo_path("docs/CHECKLISTS.md"))
    state_json = safe_read(env.drive_path("state/state.json"), fallback="{}")

    # --- Load memory ---
    memory.ensure_files()

    # --- Assemble messages with 3-block prompt caching ---
    # Block 1: Static content (all docs) — cached
    # Block 2: Semi-stable content (identity + scratchpad + knowledge) — cached
    # Block 3: Dynamic content (state + runtime + recent logs) — uncached
    #
    # All docs always included so the agent sees what reviewers judge by (Bible P5 DRY).
    # Caps: BIBLE 180k, ARCHITECTURE 60k, DEVELOPMENT 30k, README 10k, CHECKLISTS 5k.
    static_text = (
        base_prompt + "\n\n"
        + "## BIBLE.md\n\n" + clip_text(bible_md, 180000)
    )
    if arch_md.strip():
        static_text += "\n\n## ARCHITECTURE.md\n\n" + clip_text(arch_md, 60000)
    if dev_guide_md.strip():
        static_text += "\n\n## DEVELOPMENT.md\n\n" + clip_text(dev_guide_md, 30000)
    if readme_md.strip():
        static_text += "\n\n## README.md\n\n" + clip_text(readme_md, 10000)
    if checklists_md.strip():
        static_text += "\n\n## CHECKLISTS.md\n\n" + clip_text(checklists_md, 5000)

    # Semi-stable content: identity, scratchpad, knowledge
    # These change ~once per task, not per round
    semi_stable_parts = []
    semi_stable_parts.extend(build_memory_sections(memory))

    kb_index_path = env.drive_path("memory/knowledge/index-full.md")
    if kb_index_path.exists():
        kb_index = kb_index_path.read_text(encoding="utf-8")
        if kb_index.strip():
            semi_stable_parts.append("## Knowledge base\n\n" + clip_text(kb_index, 50000))

    # Pattern register — recurring error classes for process memory
    patterns_path = env.drive_path("memory/knowledge/patterns.md")
    try:
        if patterns_path.exists():
            patterns_text = patterns_path.read_text(encoding="utf-8")
            if patterns_text.strip():
                semi_stable_parts.append(
                    "## Known error patterns (Pattern Register)\n\n"
                    + clip_text(patterns_text, 5000)
                )
    except Exception:
        pass

    # Memory registry digest — compact metacognitive map
    registry_digest = _build_registry_digest(env)
    if registry_digest:
        semi_stable_parts.append(registry_digest)

    # Creator model — understanding of the creator for context-aware interaction
    creator_index_path = env.drive_path("creator/_index.md")
    if creator_index_path.exists():
        creator_index = creator_index_path.read_text(encoding="utf-8")
        if creator_index.strip():
            semi_stable_parts.append("## Creator model\n\n" + clip_text(creator_index, 10000))

    semi_stable_text = "\n\n".join(semi_stable_parts)

    # Dynamic content: changes every round
    dynamic_parts = [
        "## Drive state\n\n" + clip_text(state_json, 90000),
        build_runtime_section(env, task),
    ]

    # Health invariants — surfaces anomalies for LLM-first self-detection (Bible P0+P3)
    health_section = build_health_invariants(env)
    if health_section:
        dynamic_parts.append(health_section)

    dynamic_parts.extend(build_recent_sections(memory, env, task_id=task.get("id", "")))

    if str(task.get("type") or "") == "review" and review_context_builder is not None:
        try:
            review_ctx = review_context_builder()
            if review_ctx:
                dynamic_parts.append(review_ctx)
        except Exception:
            log.debug("Failed to build review context", exc_info=True)
            pass

    dynamic_text = "\n\n".join(dynamic_parts)

    # System message with 3 content blocks for optimal caching
    messages: List[Dict[str, Any]] = [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": static_text,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                },
                {
                    "type": "text",
                    "text": semi_stable_text,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": dynamic_text,
                },
            ],
        },
        {"role": "user", "content": _build_user_content(task)},
    ]

    # --- Soft-cap token trimming ---
    messages, cap_info = apply_message_token_soft_cap(messages, soft_cap_tokens)

    return messages, cap_info


def apply_message_token_soft_cap(
    messages: List[Dict[str, Any]],
    soft_cap_tokens: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Trim prunable context sections if estimated tokens exceed soft cap.

    Returns (pruned_messages, cap_info_dict).
    """
    def _estimate_message_tokens(msg: Dict[str, Any]) -> int:
        """Estimate tokens for a message, handling multipart content."""
        content = msg.get("content", "")
        if isinstance(content, list):
            # Multipart content: sum tokens from all text blocks
            total = 0
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    total += estimate_tokens(str(block.get("text", "")))
            return total + 6
        return estimate_tokens(str(content)) + 6

    estimated = sum(_estimate_message_tokens(m) for m in messages)
    info: Dict[str, Any] = {
        "estimated_tokens_before": estimated,
        "estimated_tokens_after": estimated,
        "soft_cap_tokens": soft_cap_tokens,
        "trimmed_sections": [],
    }

    if soft_cap_tokens <= 0 or estimated <= soft_cap_tokens:
        return messages, info

    # Prune log summaries from the dynamic text block in multipart system messages.
    # Order: least valuable first, chat history last (only removed as a last resort).
    prunable = ["## Supervisor", "## Recent events", "## Recent tools", "## Recent progress", "## Recent chat"]
    pruned = copy.deepcopy(messages)
    for prefix in prunable:
        if estimated <= soft_cap_tokens:
            break
        for i, msg in enumerate(pruned):
            content = msg.get("content")

            # Handle multipart content (trim from dynamic text block)
            if isinstance(content, list) and msg.get("role") == "system":
                # Find the dynamic text block (the block without cache_control)
                for j, block in enumerate(content):
                    if (isinstance(block, dict) and
                        block.get("type") == "text" and
                        "cache_control" not in block):
                        text = block.get("text", "")
                        if prefix in text:
                            # For chat history, try halving before full removal
                            if prefix == "## Recent chat":
                                paragraphs = text.split("\n\n")
                                # Locate the chat section paragraphs
                                chat_start = None
                                chat_end = len(paragraphs)
                                for k, para in enumerate(paragraphs):
                                    if para.startswith("## Recent chat"):
                                        chat_start = k
                                    elif chat_start is not None and para.startswith("##"):
                                        chat_end = k
                                        break
                                if chat_start is not None:
                                    # The first paragraph is the header; messages follow
                                    header_para = paragraphs[chat_start]
                                    msg_paras = paragraphs[chat_start + 1:chat_end]
                                    half = len(msg_paras) // 2
                                    halved_paras = (
                                        paragraphs[:chat_start]
                                        + [header_para]
                                        + msg_paras[half:]
                                        + paragraphs[chat_end:]
                                    )
                                    halved_text = "\n\n".join(halved_paras)
                                    halved_tokens = estimate_tokens(halved_text) + 6
                                    other_tokens = sum(
                                        _estimate_message_tokens(m) for m in pruned
                                        if m is not msg
                                    )
                                    if other_tokens + halved_tokens <= soft_cap_tokens:
                                        block["text"] = halved_text
                                        info["trimmed_sections"].append(prefix + " (halved)")
                                        estimated = sum(_estimate_message_tokens(m) for m in pruned)
                                        break

                            # Remove this section from the dynamic text entirely
                            lines = text.split("\n\n")
                            new_lines = []
                            skip_section = False
                            for line in lines:
                                if line.startswith(prefix):
                                    skip_section = True
                                    info["trimmed_sections"].append(prefix)
                                    continue
                                if line.startswith("##"):
                                    skip_section = False
                                if not skip_section:
                                    new_lines.append(line)

                            block["text"] = "\n\n".join(new_lines)
                            estimated = sum(_estimate_message_tokens(m) for m in pruned)
                            break
                break

            # Handle legacy string content (for backwards compatibility)
            elif isinstance(content, str) and content.startswith(prefix):
                pruned.pop(i)
                info["trimmed_sections"].append(prefix)
                estimated = sum(_estimate_message_tokens(m) for m in pruned)
                break

    info["estimated_tokens_after"] = estimated
    return pruned, info


_COMPACTION_PROTECTED_TOOLS = frozenset({
    "repo_commit", "repo_write_commit",
})


def _find_tool_name_for_result(msg: dict, messages: list) -> str:
    """Look up which tool produced a given tool-result message."""
    target_id = msg.get("tool_call_id", "")
    if not target_id:
        return ""
    msg_idx = None
    for idx, m in enumerate(messages):
        if m is msg:
            msg_idx = idx
            break
    if msg_idx is None:
        return ""
    for j in range(msg_idx - 1, -1, -1):
        prev = messages[j]
        if prev.get("role") != "assistant":
            continue
        for tc in (prev.get("tool_calls") or []):
            if tc.get("id") == target_id:
                return tc.get("function", {}).get("name", "")
        break
    return ""


def _compact_tool_result(msg: dict, content: str) -> dict:
    """Compact a single tool result message."""
    is_error = content.startswith("⚠️")
    # Create a short summary
    if is_error:
        summary = content[:200]  # Keep error details
    else:
        # Keep first line or first 80 chars
        first_line = content.split('\n')[0][:80]
        char_count = len(content)
        summary = f"{first_line}... ({char_count} chars)" if char_count > 80 else content[:200]

    return {**msg, "content": summary}


def _compact_assistant_msg(msg: dict) -> dict:
    """
    Compact assistant message content and tool_call arguments.

    Args:
        msg: Original assistant message dict

    Returns:
        Compacted message dict
    """
    compacted_msg = dict(msg)

    # Trim content (progress notes)
    content = msg.get("content") or ""
    if len(content) > 200:
        content = content[:200] + "..."
    compacted_msg["content"] = content

    # Compact tool_call arguments
    if msg.get("tool_calls"):
        compacted_tool_calls = []
        for tc in msg["tool_calls"]:
            compacted_tc = dict(tc)

            # Always preserve id and function name
            if "function" in compacted_tc:
                func = dict(compacted_tc["function"])
                args_str = func.get("arguments", "")

                if args_str:
                    compacted_tc["function"] = _compact_tool_call_arguments(
                        func["name"], args_str
                    )
                else:
                    compacted_tc["function"] = func

            compacted_tool_calls.append(compacted_tc)

        compacted_msg["tool_calls"] = compacted_tool_calls

    return compacted_msg


def compact_tool_history(messages: list, keep_recent: int = 6) -> list:
    """
    Compress old tool call/result message pairs into compact summaries.

    Keeps the last `keep_recent` tool-call rounds intact (they may be
    referenced by the LLM). Older rounds get their tool results truncated
    to a short summary line, and tool_call arguments are compacted.

    This dramatically reduces prompt tokens in long tool-use conversations
    without losing important context (the tool names and whether they succeeded
    are preserved).
    """
    # Find all indices that are tool-call assistant messages
    # (messages with tool_calls field)
    tool_round_starts = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tool_round_starts.append(i)

    if len(tool_round_starts) <= keep_recent:
        return messages  # Nothing to compact

    # Rounds to compact: all except the last keep_recent
    rounds_to_compact = set(tool_round_starts[:-keep_recent])

    # Build compacted message list
    result = []
    for i, msg in enumerate(messages):
        # Skip system messages with multipart content (prompt caching format)
        if msg.get("role") == "system" and isinstance(msg.get("content"), list):
            result.append(msg)
            continue

        if msg.get("role") == "tool" and i > 0:
            # Check if the preceding assistant message (with tool_calls)
            # is one we want to compact
            # Find which round this tool result belongs to
            parent_round = None
            for rs in reversed(tool_round_starts):
                if rs < i:
                    parent_round = rs
                    break

            if parent_round is not None and parent_round in rounds_to_compact:
                content = str(msg.get("content") or "")
                tool_name = _find_tool_name_for_result(msg, messages)
                if tool_name in _COMPACTION_PROTECTED_TOOLS or content.startswith("⚠️"):
                    result.append(msg)
                    continue
                result.append(_compact_tool_result(msg, content))
                continue

        if i in rounds_to_compact and msg.get("role") == "assistant":
            result.append(_compact_assistant_msg(msg))
            continue

        result.append(msg)

    return result


def compact_tool_history_llm(messages: list, keep_recent: int = 6) -> list:
    """LLM-driven compaction: summarize old tool results via a light model.

    Falls back to simple truncation (compact_tool_history) on any error.
    Called when the agent explicitly invokes the compact_context tool.
    """
    tool_round_starts = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tool_round_starts.append(i)

    if len(tool_round_starts) <= keep_recent:
        return messages

    rounds_to_compact = set(tool_round_starts[:-keep_recent])

    old_results = []
    protected_indices: set = set()
    for i, msg in enumerate(messages):
        if msg.get("role") != "tool" or i == 0:
            continue
        parent_round = None
        for rs in reversed(tool_round_starts):
            if rs < i:
                parent_round = rs
                break
        if parent_round is not None and parent_round in rounds_to_compact:
            content = str(msg.get("content") or "")
            tool_name = _find_tool_name_for_result(msg, messages)
            if tool_name in _COMPACTION_PROTECTED_TOOLS or content.startswith("⚠️"):
                protected_indices.add(i)
                continue
            if len(content) > 120:
                tool_call_id = msg.get("tool_call_id", "")
                old_results.append({"idx": i, "tool_call_id": tool_call_id, "content": content[:1500]})

    if not old_results:
        return compact_tool_history(messages, keep_recent=keep_recent)

    batch_text = "\n---\n".join(
        f"[{r['tool_call_id']}]\n{r['content']}" for r in old_results[:20]
    )
    prompt = (
        "Summarize each tool result below into 1-2 lines of key facts. "
        "Preserve errors, file paths, and important values. "
        "Output one summary per [id] block, same order.\n\n" + batch_text
    )

    try:
        from ouroboros.llm import LLMClient, DEFAULT_LIGHT_MODEL
        light_model = os.environ.get("OUROBOROS_MODEL_LIGHT") or DEFAULT_LIGHT_MODEL
        client = LLMClient()
        _use_local_light = os.environ.get("USE_LOCAL_LIGHT", "").lower() in ("true", "1")
        resp_msg, _usage = client.chat(
            messages=[{"role": "user", "content": prompt}],
            model=light_model,
            reasoning_effort="low",
            max_tokens=1024,
            use_local=_use_local_light,
        )
        summary_text = resp_msg.get("content") or ""
        if not summary_text.strip():
            raise ValueError("empty summary response")
    except Exception:
        log.warning("LLM compaction failed, falling back to truncation", exc_info=True)
        return compact_tool_history(messages, keep_recent=keep_recent)

    summary_lines = summary_text.strip().split("\n")
    summary_map: Dict[str, str] = {}
    current_id = None
    current_lines: list = []
    for line in summary_lines:
        stripped = line.strip()
        if stripped.startswith("[") and "]" in stripped:
            if current_id is not None:
                summary_map[current_id] = " ".join(current_lines).strip()
            bracket_end = stripped.index("]")
            current_id = stripped[1:bracket_end]
            rest = stripped[bracket_end + 1:].strip()
            current_lines = [rest] if rest else []
        elif current_id is not None:
            current_lines.append(stripped)
    if current_id is not None:
        summary_map[current_id] = " ".join(current_lines).strip()

    idx_to_summary = {}
    for r in old_results:
        s = summary_map.get(r["tool_call_id"])
        if s:
            idx_to_summary[r["idx"]] = s

    result = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "system" and isinstance(msg.get("content"), list):
            result.append(msg)
            continue
        if i in protected_indices:
            result.append(msg)
            continue
        if i in idx_to_summary:
            result.append({**msg, "content": idx_to_summary[i]})
            continue
        if msg.get("role") == "tool" and i > 0:
            parent_round = None
            for rs in reversed(tool_round_starts):
                if rs < i:
                    parent_round = rs
                    break
            if parent_round is not None and parent_round in rounds_to_compact:
                content = str(msg.get("content") or "")
                tool_name = _find_tool_name_for_result(msg, messages)
                if tool_name in _COMPACTION_PROTECTED_TOOLS or content.startswith("⚠️"):
                    result.append(msg)
                    continue
                result.append(_compact_tool_result(msg, content))
                continue
        if i in rounds_to_compact and msg.get("role") == "assistant":
            result.append(_compact_assistant_msg(msg))
            continue
        result.append(msg)

    return result


def _compact_tool_call_arguments(tool_name: str, args_json: str) -> Dict[str, Any]:
    """Compact tool call arguments for old rounds.

    For tools with large content payloads, replace the field with a
    length-tagged marker.  For other tools, truncate if > 500 chars.
    """
    LARGE_CONTENT_TOOLS = {
        "repo_write": "content",
        "repo_write_commit": "content",
        "data_write": "content",
        "claude_code_edit": "prompt",
        "update_scratchpad": "content",
    }

    try:
        args = json.loads(args_json)

        if tool_name in LARGE_CONTENT_TOOLS:
            large_field = LARGE_CONTENT_TOOLS[tool_name]
            if large_field in args and args[large_field]:
                v = args[large_field] if isinstance(args[large_field], str) else json.dumps(args[large_field], ensure_ascii=False)
                args[large_field] = f"<<CONTENT_OMITTED len={len(v)}>>"
                return {"name": tool_name, "arguments": json.dumps(args, ensure_ascii=False)}

        # For other tools, if args JSON is > 500 chars, truncate
        if len(args_json) > 500:
            truncated = args_json[:200] + "..."
            return {"name": tool_name, "arguments": truncated}

        # Otherwise return unchanged
        return {"name": tool_name, "arguments": args_json}

    except (json.JSONDecodeError, Exception):
        # If we can't parse JSON, leave it unchanged
        # But still truncate if too long
        if len(args_json) > 500:
            return {"name": tool_name, "arguments": args_json[:200] + "..."}
        return {"name": tool_name, "arguments": args_json}


def safe_read(path: pathlib.Path, fallback: str = "") -> str:
    """Read a file, returning fallback if it doesn't exist or errors."""
    try:
        if path.exists():
            return read_text(path)
    except Exception:
        log.debug(f"Failed to read file {path} in safe_read", exc_info=True)
        pass
    return fallback
