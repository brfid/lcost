"""Collect local Codex rollout usage without copying conversation content.

The Codex CLI and VS Code extension write JSONL rollouts below
``$CODEX_HOME/sessions`` (and may move completed rollouts to
``archived_sessions``).  We keep only session metadata and token-count
events: prompts, responses, tool payloads, and authentication data are never
stored in lcost's ledger.
"""

import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .formatters import (
    FIELD_CACHE_READS,
    FIELD_CACHE_SAVINGS,
    FIELD_CACHE_WRITES,
    FIELD_COST,
    FIELD_TOKENS_IN,
    FIELD_TOKENS_OUT,
)
from .ingest_state import file_needs_processing, update_file_state
from .pricing import (
    calculate_cache_savings,
    calculate_cost,
    family_for_model,
    has_priced_model,
)


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_ROLLOUT_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(?P<id>[0-9a-fA-F-]+)"
    r"\.jsonl$"
)


def get_codex_home() -> Path:
    """Return Codex's data root without creating it."""
    configured = os.environ.get("CODEX_HOME")
    return Path(configured) if configured else Path.home() / ".codex"


def detect_codex_surface(originator: object) -> str:
    """Map a rollout originator to a compact display surface."""
    if not isinstance(originator, str):
        return "codex"
    normalized = originator.strip().casefold().replace("_", "-")
    if "vscode" in normalized or "visual studio" in normalized:
        return "vscode"
    if "desktop" in normalized and "chatgpt" in normalized:
        return "chatgpt"
    if "desktop" in normalized:
        return "codex-desktop"
    if "cli" in normalized:
        return "codex-cli"
    if "chatgpt" in normalized:
        return "chatgpt"
    return "codex"


def find_codex_session_files(codex_home: Optional[Path] = None) -> List[Path]:
    """Find live and archived rollout JSONL files, collapsing duplicate copies."""
    root = codex_home or get_codex_home()
    candidates: List[Path] = []
    for dirname in ("sessions", "archived_sessions"):
        source_dir = root / dirname
        if source_dir.is_dir():
            candidates.extend(path for path in source_dir.rglob("*.jsonl")
                              if path.is_file())

    # A rollout may briefly exist in both locations while Codex archives it.
    # Its filename is stable, so keep the more complete copy.
    selected: Dict[str, Path] = {}
    for path in candidates:
        key = path.name
        current = selected.get(key)
        if current is None or _copy_rank(path) > _copy_rank(current):
            selected[key] = path
    return sorted(selected.values())


def parse_codex_session(session_file: Path,
                        verbose: bool = False) -> Tuple[Dict[str, Dict], int]:
    """Parse all token-count observations in one Codex rollout.

    A task can contain several model requests.  Each ``token_count`` event is
    keyed by the active turn and a request sequence number.  Re-reading a
    changed file from byte zero is intentional: it handles tasks that were
    still running at the prior ingest without retaining conversation state.
    """
    entries: Dict[str, Dict] = {}
    last_offset = 0
    rollout_id = _rollout_id_from_path(session_file) or session_file.stem
    session_id = rollout_id
    project = ""
    originator = ""
    model_provider = ""
    surface = "codex"
    is_subagent = False
    parent_session = ""
    active_turn = ""
    active_model: Optional[str] = None
    turn_contexts: Dict[str, Tuple[Optional[str], str]] = {}
    request_sequence = 0
    previous_usage_fingerprint: Optional[tuple] = None

    try:
        with session_file.open("r", encoding="utf-8") as handle:
            while True:
                line = handle.readline()
                if not line:
                    break
                line_end = handle.tell()
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # An active rollout can end on a partial line. Leave its
                    # offset behind so a later file growth retries it.
                    if not line.endswith("\n"):
                        break
                    last_offset = line_end
                    continue

                last_offset = line_end
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue

                record_type = record.get("type")
                if record_type == "session_meta":
                    candidate = payload.get("id") or payload.get("session_id")
                    if isinstance(candidate, str) and candidate:
                        session_id = candidate
                    cwd = payload.get("cwd")
                    if isinstance(cwd, str):
                        project = _project_from_cwd(cwd)
                    raw_originator = payload.get("originator")
                    if isinstance(raw_originator, str):
                        originator = raw_originator
                        surface = detect_codex_surface(raw_originator)
                    provider = payload.get("model_provider")
                    if isinstance(provider, str):
                        model_provider = provider
                    source = payload.get("source")
                    if isinstance(source, dict) and "subagent" in source:
                        is_subagent = True
                    elif source == "subagent":
                        is_subagent = True
                    if payload.get("thread_source") == "subagent":
                        is_subagent = True
                    parent = payload.get("parent_thread_id")
                    if isinstance(parent, str):
                        parent_session = parent
                    continue

                if record_type == "turn_context":
                    turn_id = payload.get("turn_id")
                    if not isinstance(turn_id, str) or not turn_id:
                        continue
                    known_model, known_project = turn_contexts.get(
                        turn_id, (None, "")
                    )
                    model = payload.get("model")
                    context_model = model if isinstance(model, str) else known_model
                    cwd = payload.get("cwd")
                    context_project = (
                        _project_from_cwd(cwd)
                        if isinstance(cwd, str)
                        else known_project
                    )
                    turn_contexts[turn_id] = (context_model, context_project)
                    if turn_id == active_turn:
                        active_model = context_model
                        if context_project:
                            project = context_project
                    continue

                if record_type != "event_msg":
                    continue
                event_type = payload.get("type")
                if event_type == "task_started":
                    turn_id = payload.get("turn_id")
                    if not isinstance(turn_id, str) or not turn_id:
                        continue
                    active_turn = turn_id
                    active_model, context_project = turn_contexts.get(
                        turn_id, (None, "")
                    )
                    if context_project:
                        project = context_project
                    request_sequence = 0
                    previous_usage_fingerprint = None
                    continue

                if event_type in ("task_complete", "turn_aborted"):
                    completed_turn = payload.get("turn_id")
                    if not completed_turn or completed_turn == active_turn:
                        active_turn = ""
                        active_model = None
                        request_sequence = 0
                        previous_usage_fingerprint = None
                    continue

                if event_type != "token_count" or not active_turn:
                    continue
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                usage = info.get("last_token_usage")
                if not isinstance(usage, dict):
                    continue

                fingerprint = _usage_fingerprint(info.get("total_token_usage"))
                if fingerprint is not None and fingerprint == previous_usage_fingerprint:
                    continue
                if fingerprint is not None:
                    previous_usage_fingerprint = fingerprint

                request_sequence += 1
                entry = _entry_from_usage(
                    usage=usage,
                    timestamp=record.get("timestamp"),
                    session_id=session_id,
                    rollout_id=rollout_id,
                    turn_id=active_turn,
                    request_sequence=request_sequence,
                    model=active_model,
                    project=project,
                    surface=surface,
                    originator=originator,
                    model_provider=model_provider,
                    is_subagent=is_subagent,
                    parent_session=parent_session,
                )
                if entry is not None:
                    entries[_entry_id(rollout_id, active_turn, request_sequence)] = entry
    except OSError as exc:
        if verbose:
            print(f"Warning: Could not read {session_file}: {exc}")
        return {}, 0

    return entries, last_offset


def collect_codex_data(verbose: bool,
                       ingest_state: Optional[Dict] = None,
                       max_workers: int = 8,
                       codex_home: Optional[Path] = None) -> Dict[str, Dict]:
    """Collect changed local Codex rollouts from the CLI and IDE extension."""
    root = codex_home or get_codex_home()
    if not root.exists():
        if verbose:
            print("Codex: data directory not found, skipping")
        return {}

    session_files = find_codex_session_files(root)
    if not session_files:
        if verbose:
            print("Codex: no rollout files found")
        return {}

    work: List[Path] = []
    skipped = 0
    for session_file in session_files:
        if ingest_state is not None:
            needs, _ = file_needs_processing(session_file, ingest_state)
            if not needs:
                skipped += 1
                continue
        work.append(session_file)

    all_entries: Dict[str, Dict] = {}
    if work:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for session_file, (entries, offset) in zip(
                    work, pool.map(lambda path: parse_codex_session(path, verbose), work)):
                # A parent rollout may be replayed in a child rollout. Files
                # are sorted chronologically, so retain the original owner.
                for entry_id, entry in entries.items():
                    all_entries.setdefault(entry_id, entry)
                if ingest_state is not None and offset:
                    update_file_state(ingest_state, session_file, offset)

    if verbose:
        print(f"Codex: parsed {len(work)} sessions ({skipped} skipped), "
              f"{len(all_entries)} model requests")
    return all_entries


def _entry_from_usage(*, usage: Dict, timestamp: object, session_id: str,
                      rollout_id: str, turn_id: str, request_sequence: int,
                      model: Optional[str], project: str, surface: str,
                      originator: str, model_provider: str,
                      is_subagent: bool, parent_session: str,
                      ) -> Optional[Dict]:
    ts = _normalize_timestamp(timestamp)
    if ts is None:
        return None

    reported_input = _nonnegative_int(usage.get("input_tokens"))
    cache_reads = _nonnegative_int(usage.get("cached_input_tokens"))
    cache_writes = _nonnegative_int(usage.get("cache_write_input_tokens"))
    tokens_in = max(0, reported_input - cache_reads - cache_writes)
    tokens_out = _nonnegative_int(usage.get("output_tokens"))
    reasoning_tokens = _nonnegative_int(usage.get("reasoning_output_tokens"))

    cost = None
    savings = 0.0
    if model and family_for_model(model) and has_priced_model(model):
        cost = calculate_cost(tokens_in, tokens_out, cache_writes, cache_reads, model)
        savings = calculate_cache_savings(cache_reads, model)

    return {
        "source": "codex",
        "ts": ts,
        "model": model,
        "project": project,
        "session": session_id,
        "rollout": rollout_id,
        "turnId": turn_id,
        "requestSequence": request_sequence,
        "surface": surface,
        "originator": originator,
        "modelProvider": model_provider,
        "isSubagent": is_subagent,
        "parentSession": parent_session,
        "reasoningTokens": reasoning_tokens,
        "reasoningTokensAdditive": False,
        "reportedInputTokens": reported_input,
        "promptPreview": "",
        "tools": [],
        FIELD_TOKENS_IN: tokens_in,
        FIELD_TOKENS_OUT: tokens_out,
        FIELD_CACHE_WRITES: cache_writes,
        FIELD_CACHE_READS: cache_reads,
        FIELD_COST: cost,
        FIELD_CACHE_SAVINGS: savings,
    }


def _entry_id(rollout_id: str, turn_id: str, sequence: int) -> str:
    if _UUID_RE.fullmatch(turn_id):
        return f"codex:{turn_id}:{sequence}"
    return f"codex:{rollout_id}:{turn_id}:{sequence}"


def _rollout_id_from_path(path: Path) -> Optional[str]:
    match = _ROLLOUT_RE.match(path.name)
    return match.group("id") if match else None


def _copy_rank(path: Path) -> tuple:
    try:
        stat = path.stat()
        return (stat.st_size, str(path))
    except OSError:
        return (-1, str(path))


def _project_from_cwd(cwd: str) -> str:
    home = str(Path.home())
    if cwd == home:
        return "~"
    if cwd.startswith(home + "/"):
        return "~" + cwd[len(home):]
    return cwd


def _normalize_timestamp(value: object) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if not math.isfinite(value):
        return 0
    return max(0, int(value))


def _usage_fingerprint(value: object) -> Optional[tuple]:
    if not isinstance(value, dict):
        return None
    fields = (
        "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
        "output_tokens", "reasoning_output_tokens", "total_tokens",
    )
    fingerprint = tuple(
        _nonnegative_int(value.get(field)) if field in value else None
        for field in fields
    )
    return fingerprint if any(value is not None for value in fingerprint) else None
