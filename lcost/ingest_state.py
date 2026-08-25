"""Per-run ingest state: file offsets, source health, and last-ingest time.

Kept separate from `ledger.py` because the two serve different lifecycles:
the ledger is the durable cost record; ingest state is bookkeeping that can
be discarded and rebuilt by re-parsing sources.
"""

from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

from ._json_io import atomic_json_write, load_json


def get_ingest_state_path(ledger_path: Path) -> Path:
    return ledger_path.parent / "ingest_state.json"


def new_ingest_state() -> Dict:
    return {
        "_version": 2,
        "files": {},
        "sources": {},
        "last_ingest_at": None,
    }


def load_ingest_state(path: Path) -> Dict:
    state = load_json(
        path, new_ingest_state(),
        validate=lambda d: isinstance(d, dict) and "files" in d,
    )
    return _normalize_state(state)


def save_ingest_state(path: Path, state: Dict) -> None:
    atomic_json_write(path, state)


def hours_since_last_ingest(state: Dict) -> Optional[float]:
    """Compute hours elapsed since the last successful ingest.

    Returns:
      Hours since the timestamp in ``state["last_ingest_at"]``, or ``None``
      if the field is missing or malformed.
    """
    ts = state.get("last_ingest_at")
    if not ts:
        return None
    try:
        prev = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    delta = datetime.now() - prev
    return delta.total_seconds() / 3600.0


def stamp_ingest(state: Dict) -> None:
    state["last_ingest_at"] = datetime.now().isoformat(timespec="seconds")


def record_source_success(state: Dict, source: str, records_seen: int,
                          records_added: int) -> None:
    """Persist a privacy-safe result for one collector invocation."""
    stamp = datetime.now().isoformat(timespec="seconds")
    health = _source_health_slot(state, source)
    health.update({
        "last_attempt_at": stamp,
        "last_success_at": stamp,
        "last_error": None,
        "records_seen": max(0, int(records_seen)),
        "records_added": max(0, int(records_added)),
    })


def record_source_failure(state: Dict, source: str, error: Exception) -> None:
    """Persist a bounded diagnostic without exposing collector contents."""
    health = _source_health_slot(state, source)
    health["last_attempt_at"] = datetime.now().isoformat(timespec="seconds")
    health["last_error"] = f"{type(error).__name__}: {str(error)[:160]}"


def source_health(state: Dict) -> Dict[str, Dict]:
    """Return the normalized per-source collector health mapping."""
    return _normalize_state(state)["sources"]


def prune_orphan_file_state(state: Dict) -> int:
    """Drop file-tracking entries for files no longer on disk.

    Ledger entries are untouched; only the per-file seek-offset bookkeeping
    in ``state["files"]`` is cleaned.

    Returns:
      Count of file entries pruned.
    """
    files = state.get("files", {})
    dead = [k for k in files if not Path(k).exists()]
    for k in dead:
        del files[k]
    return len(dead)


def file_needs_processing(filepath: Path, state: Dict) -> Tuple[bool, int]:
    """Decide whether a file must be re-read, and from where.

    Returns:
      ``(needs_processing, seek_offset)``. ``seek_offset`` is 0 for new or
      shrunken files and the stored byte offset when resuming an appended
      file.
    """
    file_key = str(filepath)
    try:
        stat = filepath.stat()
    except OSError:
        return False, 0

    stored = state.get("files", {}).get(file_key)
    if not stored:
        return True, 0

    stored_size = stored.get("size", 0)

    if stat.st_size == stored_size:
        return False, 0

    if stat.st_size < stored_size:
        return True, 0

    return True, stored.get("byte_offset", 0)


def update_file_state(state: Dict, filepath: Path, byte_offset: int,
                      last_user_text: str = "") -> None:
    try:
        stat = filepath.stat()
    except OSError:
        return
    state.setdefault("files", {})[str(filepath)] = {
        "byte_offset": byte_offset,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "last_user_text": last_user_text,
    }


def get_stored_user_text(state: Dict, filepath: Path) -> str:
    """Retrieve last captured user text for a file, for seeding incremental resumes."""
    return state.get("files", {}).get(str(filepath), {}).get("last_user_text", "")


def _normalize_state(state: Dict) -> Dict:
    """Adapt v1 state in memory without forcing an eager migration write."""
    state.setdefault("_version", 1)
    state["files"] = state.get("files") if isinstance(state.get("files"), dict) else {}
    state["sources"] = (
        state.get("sources") if isinstance(state.get("sources"), dict) else {}
    )
    state.setdefault("last_ingest_at", None)
    return state


def _source_health_slot(state: Dict, source: str) -> Dict:
    sources = source_health(state)
    current = sources.get(source)
    if not isinstance(current, dict):
        current = {}
        sources[source] = current
    return current
