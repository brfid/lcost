"""Ledger aggregation and CLI output formatting."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from .formatters import (
    FIELD_COST, FIELD_NAMES, FIELD_REQUESTS, FIELD_UNPRICED_REQUESTS,
    FIELD_UNPRICED_TOKENS, calculate_averages, calculate_totals,
    format_table_header, format_table_row, init_field_dict, total_tokens,
)


def entry_local_dt(entry: Dict) -> datetime:
    """Parse an entry timestamp into local-time ``datetime``.

    Claude Code entries are stored UTC-naive and converted here. Codex
    entries carry a UTC offset, while Cline entries are already local.
    """
    timestamp = entry.get('ts')
    if not isinstance(timestamp, str):
        raise ValueError("Missing or invalid entry timestamp")
    dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    if dt.tzinfo is None and entry.get('source') in ('cc', 'agent_spawn'):
        dt = dt.replace(tzinfo=timezone.utc)
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def iter_entries(ledger: Dict, source_filter=None, date_from=None,
                 date_to=None, project_filter=None):
    """Yield valid (local datetime, id, entry) tuples within the filters."""
    needle = project_filter.casefold() if project_filter else None
    for entry_id, entry in ledger.items():
        if not isinstance(entry, dict):
            continue
        if source_filter and entry.get('source') != source_filter:
            continue
        if needle and needle not in str(entry.get('project') or '').casefold():
            continue
        try:
            dt = entry_local_dt(entry)
        except (ValueError, OverflowError):
            continue
        day = dt.strftime('%Y-%m-%d')
        if date_from and day < date_from or date_to and day > date_to:
            continue
        yield dt, entry_id, entry


def calendar_days(now: datetime, days: int) -> list:
    """Ascending calendar days ending today, including idle days."""
    return [(now - timedelta(days=i)).strftime('%Y-%m-%d')
            for i in range(days - 1, -1, -1)]


def aggregate_by_day(ledger: Dict[str, Dict],
                     source_filter: Optional[str] = None,
                     date_from: Optional[str] = None,
                     date_to: Optional[str] = None,
                     project_filter: Optional[str] = None) -> Dict[str, Dict]:
    """Aggregate ledger entries by local-time day.

    Args:
      ledger: Ledger entries keyed by entry id.
      source_filter: If set, keep only entries whose ``source`` matches.
      date_from: Inclusive lower bound, ``YYYY-MM-DD`` local time.
      date_to: Inclusive upper bound, ``YYYY-MM-DD`` local time.
      project_filter: Case-insensitive substring matched against
        ``entry["project"]``.

    Returns:
      ``{day_key: totals_dict}`` with one entry per active day.
    """
    daily_data = defaultdict(init_field_dict)
    for dt, _, entry in iter_entries(
        ledger, source_filter, date_from, date_to, project_filter,
    ):
        if entry.get('source') == 'agent_spawn':
            continue
        day_key = dt.strftime('%Y-%m-%d')

        day = daily_data[day_key]

        for field in FIELD_NAMES:
            if field in (FIELD_REQUESTS, FIELD_UNPRICED_REQUESTS, FIELD_UNPRICED_TOKENS):
                continue
            day[field] += entry.get(field) or 0

        day[FIELD_REQUESTS] += entry.get(FIELD_REQUESTS) or 1
        if entry.get(FIELD_COST) is None:
            day[FIELD_UNPRICED_REQUESTS] += entry.get(FIELD_REQUESTS) or 1
            day[FIELD_UNPRICED_TOKENS] += total_tokens(entry)

    return dict(daily_data)


def format_output(daily_data: Dict[str, Dict], limit_days: Optional[int] = None,
                  title: str = "Daily Cost Summary") -> str:
    if not daily_data:
        return "No cost data found."

    sorted_days = sorted(daily_data.keys())
    if limit_days is not None and len(sorted_days) > limit_days:
        sorted_days = sorted_days[-limit_days:]

    lines = []
    if limit_days is None:
        lines.append(f"{title} (All {len(sorted_days)} Active Days)")
    else:
        lines.append(f"{title} (Last {len(sorted_days)} Active Days)")
    lines.append("")
    lines.append(format_table_header())

    lines.extend(format_table_row(day, daily_data[day]) for day in sorted_days)

    lines.append("")
    totals = calculate_totals({day: daily_data[day] for day in sorted_days})
    lines.append(format_table_row("TOTAL", totals))

    averages = calculate_averages(totals, len(sorted_days))
    lines.append(format_table_row("AVERAGE", averages))
    if totals[FIELD_UNPRICED_REQUESTS]:
        lines.append(
            f"\nPARTIAL PRICE: {totals[FIELD_UNPRICED_REQUESTS]:,} unpriced requests "
            f"({totals[FIELD_UNPRICED_TOKENS]:,} tokens). "
            "Monetary totals exclude this usage."
        )

    return "\n".join(lines)
