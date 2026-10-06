"""Ledger file I/O, backup rotation, and cost recalculation.

The ledger is the durable cost record. Per-run bookkeeping (file seek
offsets, last-ingest timestamp) lives in `ingest_state.py`.
"""

import contextlib
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

from ._json_io import atomic_json_write, load_json
from .formatters import (
    FIELD_CACHE_READS,
    FIELD_CACHE_SAVINGS,
    FIELD_CACHE_WRITES,
    FIELD_COST,
    FIELD_TOKENS_IN,
    FIELD_TOKENS_OUT,
    data_dir,
)
# Preserve the original ledger-module imports for existing callers.
from .ingest_state import (
    file_needs_processing as file_needs_processing,
    get_ingest_state_path as get_ingest_state_path,
    get_stored_user_text as get_stored_user_text,
    hours_since_last_ingest as hours_since_last_ingest,
    load_ingest_state as load_ingest_state,
    new_ingest_state as new_ingest_state,
    prune_orphan_file_state as prune_orphan_file_state,
    save_ingest_state as save_ingest_state,
    stamp_ingest as stamp_ingest,
    update_file_state as update_file_state,
)
from .pricing import (
    canonical_model_id,
    estimate_entry,
)
from .usage import (
    COST_PROVENANCE_SOURCE_REPORTED,
    normalize_usage_entry,
)

BACKUP_RETAIN = 7


def get_ledger_path(override: Optional[str] = None) -> Path:
    if override:
        return Path(override)
    return data_dir() / "ledger.json"


def load_ledger(path: Path) -> Dict[str, Dict]:
    return load_json(path, {}, validate=lambda d: isinstance(d, dict))


def save_ledger(path: Path, ledger: Dict[str, Dict]) -> None:
    atomic_json_write(path, ledger)


def backup_dir(ledger_path: Path) -> Path:
    return ledger_path.parent / "backups"


def rotate_backup(ledger_path: Path, retain: int = BACKUP_RETAIN) -> Optional[Path]:
    """Snapshot the current ledger into ``backups/ledger-YYYY-MM-DD.json``.

    Idempotent per-day: skips when a backup for today already exists. After
    writing, prunes older snapshots down to the ``retain`` most recent.

    Args:
      ledger_path: Path to the live ledger file.
      retain: Number of snapshots to keep.

    Returns:
      Path to the backup that was written, or ``None`` if the source ledger
      is missing or a backup for today already exists.
    """
    if not ledger_path.exists():
        return None
    d = backup_dir(ledger_path)
    d.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    dest = d / f"ledger-{today}.json"
    if dest.exists():
        return None
    shutil.copy2(ledger_path, dest)
    backups = sorted(d.glob("ledger-*.json"))
    for old in backups[:-retain]:
        with contextlib.suppress(OSError):
            old.unlink()
    return dest


# Fields that may be backfilled from a later re-parse of a Cline task file.
# Cline writes api_req_started *before* the API call completes, then mutates
# the same message in-place with final cost/token data. An incremental ingest
# that ran while the call was in-flight will have stored zeros for these fields.
# When a subsequent full re-parse sees the completed values, we allow them to
# overwrite the stored zeros.
_CLINE_MUTABLE_FIELDS = frozenset({
    FIELD_COST, FIELD_CACHE_SAVINGS,
    FIELD_TOKENS_IN, FIELD_TOKENS_OUT,
    FIELD_CACHE_WRITES, FIELD_CACHE_READS,
})


@dataclass(frozen=True)
class MergeResult:
    added: int
    updated: int


def merge_entries(ledger: Dict[str, Dict], new_entries: Dict[str, Dict]) -> MergeResult:
    """Merge new entries into the ledger.

    For existing entries, fills in any keys that are missing — this covers
    schema evolution (e.g., ``model`` / ``project`` / ``promptPreview`` were
    added after some entries were first written).

    Cline-specific upsert: ``_CLINE_MUTABLE_FIELDS`` (cost, tokens, cache
    counts) are refreshed when the incoming value is nonzero and the stored
    value is zero. This heals entries that were captured while an API call was
    still in-flight — Cline backfills those fields in-place after the stream
    completes, so a later full re-parse of the same file will have the real
    numbers.

    Returns:
      Counts of added and updated records, so corrections are persisted too.
    """
    added = 0
    updated = 0
    for entry_id, entry_data in new_entries.items():
        entry_data = normalize_usage_entry(entry_data)
        if entry_id not in ledger:
            ledger[entry_id] = entry_data
            added += 1
        elif not isinstance(ledger[entry_id], dict):
            ledger[entry_id] = entry_data
            updated += 1
        else:
            existing = ledger[entry_id]
            changed = False
            is_cline = existing.get('source') == 'cline'
            for k, v in entry_data.items():
                # A reparse can use a newer rate card while an existing cost
                # remains pinned. Do not backfill metadata for a different price.
                if (k.startswith("pricing") or k == "costProvenance") and (
                    existing.get(FIELD_COST) is not None
                    and existing.get(FIELD_COST) != entry_data.get(FIELD_COST)
                ):
                    continue
                if k not in existing or existing[k] is None:
                    if v not in (None, ""):
                        existing[k] = v
                        changed = True
                elif is_cline and k in _CLINE_MUTABLE_FIELDS:
                    # Overwrite a stored zero with a real value from a
                    # completed (backfilled) Cline api_req message.
                    if v and not existing[k]:
                        existing[k] = v
                        changed = True
            updated += changed
    return MergeResult(added, updated)


def ingest(ledger: Dict[str, Dict], new_entries: Dict[str, Dict]) -> int:
    """Merge records and return the number added (legacy public interface)."""
    return merge_entries(ledger, new_entries).added


def recalc_ledger_costs(ledger_path: Path, ledger: Dict[str, Dict],
                        dry_run: bool = False, *,
                        source_filter: Optional[str] = None,
                        model_filters=None, provider_filter: Optional[str] = None,
                        only_unpriced: bool = False,
                        include_source_reported: bool = False,
                        assume_route: Optional[str] = None,
                        date_from: Optional[str] = None,
                        date_to: Optional[str] = None,
                        project_filter: Optional[str] = None,
                        summary: Optional[Dict] = None,
                        ) -> Tuple[float, float, int, int]:
    """Recompute selected entries, preserving source-reported costs by default.

    Writes the ledger to disk when any values change, unless ``dry_run`` is
    true. Entries with no token data (e.g. synthetic agent-spawn entries)
    are left alone. Entries without complete supported pricing are preserved.
    Source, exact model, provider, project, and explicit date filters limit the
    selection; ``only_unpriced`` excludes all previously priced entries.

    Args:
      ledger_path: Path used when writing the updated ledger.
      ledger: In-memory ledger; mutated in place when ``dry_run`` is false.
      dry_run: If true, skip the write.

    Returns:
      ``(old_total, new_total, entries_changed, entries_skipped)``. Skipped
      counts selected entries with incomplete tokens, unsupported pricing,
      or a preserved source-reported cost.
    """
    old_total = 0.0
    new_total = 0.0
    changed = 0
    skipped = 0

    from .aggregation import iter_entries

    models = {canonical_model_id(model) for model in model_filters or ()}
    # Keep timestamp-less legacy entries eligible when no date filter was asked.
    if date_from or date_to:
        candidates = (entry for _, _, entry in iter_entries(
            ledger, source_filter, date_from, date_to, project_filter,
        ))
    else:
        candidates = (entry for entry in ledger.values() if isinstance(entry, dict))
    for entry in candidates:
        if source_filter and entry.get("source") != source_filter:
            continue
        if models and canonical_model_id(entry.get("rawModel") or entry.get("model")) not in models:
            continue
        if provider_filter and str(entry.get("modelProvider") or "").casefold() != provider_filter.casefold():
            continue
        if project_filter and project_filter.casefold() not in str(entry.get("project") or "").casefold():
            continue
        if only_unpriced and entry.get(FIELD_COST) is not None:
            continue
        token_values = (
            entry.get(FIELD_TOKENS_IN),
            entry.get(FIELD_TOKENS_OUT),
            entry.get(FIELD_CACHE_WRITES),
            entry.get(FIELD_CACHE_READS),
        )
        model = entry.get('model')

        old_cost = entry.get(FIELD_COST) or 0.0
        old_total += old_cost

        if not include_source_reported and (
            entry.get("costProvenance") == COST_PROVENANCE_SOURCE_REPORTED
            or (entry.get("source") == "cline"
                and entry.get("costProvenance") != "rate_card_estimate"
                and entry.get(FIELD_COST) is not None)
        ):
            new_total += old_cost
            skipped += 1
            continue

        # Explicitly unknown token components cannot be safely repriced:
        # treating them as zero would understate a query's token-rate cost.
        if any(value is None for value in token_values):
            new_total += old_cost
            if model:
                skipped += 1
            continue

        ti, to, cw, cr = token_values
        if ti == 0 and to == 0 and cw == 0 and cr == 0:
            new_total += old_cost
            continue

        # Rescued/imported entries may lack a model field. Without it,
        # we'd silently fall back to Sonnet pricing and clobber correctly
        # priced historical costs. Skip them.
        if not model:
            new_total += old_cost
            continue

        estimate = estimate_entry(entry, assume_route=assume_route)
        if estimate is None:
            # Family known, rates not configured (placeholder). Preserve
            # whatever cost the collector stored — for Cline that's the
            # provider-reported cost; for CC it's the last-known rate.
            new_total += old_cost
            skipped += 1
            continue

        new_cost = estimate[FIELD_COST]
        new_total += new_cost
        updates = {key: value for key, value in estimate.items()
                   if entry.get(key) != value}
        if summary is not None:
            name = canonical_model_id(entry.get("rawModel") or model)
            group = summary.setdefault(name, {
                "entries": 0, "changed": 0, "old": 0.0, "new": 0.0,
                "references": set(), "assumptions": set(),
            })
            group["entries"] += 1
            group["changed"] += bool(updates)
            group["old"] += old_cost
            group["new"] += new_cost
            group["references"].add(estimate["pricingRef"])
            group["assumptions"].update(estimate.get("pricingAssumptions", []))
        if updates:
            if not dry_run:
                entry.update(updates)
            changed += 1

    if not dry_run and changed > 0:
        if ledger_path.exists():
            backups = backup_dir(ledger_path)
            backups.mkdir(parents=True, exist_ok=True)
            backup = backups / f"recalc-{datetime.now():%Y-%m-%dT%H%M%S%f}.json"
            shutil.copy2(ledger_path, backup)
            if summary is not None:
                summary["_backup"] = str(backup)
        save_ledger(ledger_path, ledger)

    return old_total, new_total, changed, skipped
