"""CLI argument parsing, ledger stats, and main entry point."""

import argparse
import math
import os
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from .aggregation import aggregate_by_day, entry_local_dt, format_output, iter_entries
from .demo import build_demo_ledger
from .formatters import (
    SOURCES,
    SOURCE_CLI_CHOICES,
    SOURCE_DISPLAY,
    SOURCE_MAP,
    resolve_source_cli,
)
from .ingest_state import (
    get_ingest_state_path,
    hours_since_last_ingest,
    load_ingest_state,
    source_health,
    timestamp_age,
)
from .ledger import backup_dir, get_ledger_path, load_ledger, recalc_ledger_costs
from .pipeline import run_ingest


def _positive_days(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("days must be positive")
    return number


def _positive_hours(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("hours must be finite and positive")
    return number


def _iso_date(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD")
    return value


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Track local LLM usage across Codex, Claude Code, and Cline.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  lcost                       # Interactive dashboard (default)
  lcost --demo                # Populated dashboard with built-in sample data
  lcost --report              # Text report, last 30 active days
  lcost --report --days 7      # Last 7 active days
  lcost --report --all        # All days with activity
  lcost --source cline        # Dashboard, Cline only
  lcost --source claude-code  # Dashboard, Claude Code only
  lcost --source codex        # Dashboard, Codex / ChatGPT only
  lcost --color always        # Dashboard colors even when NO_COLOR is set
  lcost --cached              # Skip scanning live sources
  lcost --rescan              # Rescan all files from scratch
  lcost --deep                # Re-parse files and retain the ledger
  lcost --stats               # Print ledger stats and exit
  lcost --recalc --dry-run    # Preview cost correction without writing
  lcost --recalc              # Rewrite ledger costs using current rates
        """
    )
    mode = parser.add_mutually_exclusive_group()
    parser.add_argument(
        '--demo', action='store_true',
        help='use built-in sample data without reading or writing personal usage'
    )
    mode.add_argument(
        '--report', action='store_true',
        help='print text report instead of launching dashboard'
    )
    mode.add_argument(
        '--stats', action='store_true',
        help='print ledger stats (entry count, size, date range) and exit'
    )
    mode.add_argument(
        '--recalc', action='store_true',
        help='recompute cost/savings in ledger using current rates, then exit'
    )
    parser.add_argument(
        '--days', type=_positive_days, metavar='N',
        help='report: last N active days (default: 30); dashboard: last N calendar days'
    )
    parser.add_argument(
        '--all', action='store_true',
        help='show all days with activity (overrides --days)'
    )
    parser.add_argument(
        '--source', type=resolve_source_cli,
        choices=SOURCE_CLI_CHOICES, default='all',
        help='data source (default: all)'
    )
    parser.add_argument(
        '--project', metavar='SUBSTRING',
        help='filter to entries whose project path matches this substring'
    )
    parser.add_argument(
        '--from', dest='date_from', type=_iso_date, metavar='YYYY-MM-DD',
        help='include only entries on or after this date'
    )
    parser.add_argument(
        '--to', dest='date_to', type=_iso_date, metavar='YYYY-MM-DD',
        help='include only entries on or before this date'
    )
    parser.add_argument(
        '--cached', action='store_true',
        help='report from stored data only, skip scanning live sources'
    )
    parser.add_argument(
        '--color', choices=('auto', 'always', 'never'), default='auto',
        help='dashboard colors: auto respects NO_COLOR; always overrides it; '
             'never uses monochrome (default: auto)'
    )
    parser.add_argument(
        '--rescan', action='store_true',
        help='rescan all source files from scratch, ignoring stored state'
    )
    parser.add_argument(
        '--ledger-path', metavar='PATH',
        help='override default ledger file location'
    )
    parser.add_argument(
        '--quiet', '-q', action='store_true',
        help='suppress all status messages'
    )
    parser.add_argument(
        '--verbose', '-v', action='store_true',
        help='show detailed source discovery and error information'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='with --recalc: show what would change without writing to disk'
    )
    parser.add_argument(
        '--model', action='append', metavar='MODEL',
        help='with --recalc: select an exact model ID; repeat to select several'
    )
    parser.add_argument(
        '--provider', metavar='PROVIDER',
        help='with --recalc: select the recorded model provider'
    )
    parser.add_argument(
        '--only-unpriced', action='store_true',
        help='with --recalc: fill only entries whose cost is unknown'
    )
    parser.add_argument(
        '--include-source-reported', action='store_true',
        help='with --recalc: allow replacing Cline or other source-reported costs'
    )
    parser.add_argument(
        '--assume-bedrock-route', choices=('regional', 'us', 'global'),
        help='with --recalc: assume this route only when Bedrock routing is unrecorded'
    )
    parser.add_argument(
        '--deep', action='store_true',
        help='re-parse available sessions, retaining ledger entries and '
             'deduplicating by entry ID'
    )
    parser.add_argument(
        '--max-gap-hours', type=_positive_hours, default=24.0, metavar='H',
        help='if last ingest was more than H hours ago, auto-promote to '
             'deep rescan (default: 24)'
    )
    parser.add_argument(
        '--force', action='store_true',
        help='wait for another dashboard to release its lock, then start'
    )
    args = parser.parse_args()
    if args.demo and (args.stats or args.recalc):
        parser.error("--demo is available for the dashboard and --report")
    if not args.recalc and (
        args.model or args.provider or args.only_unpriced
        or args.include_source_reported or args.assume_bedrock_route
    ):
        parser.error("model/provider selection and pricing overrides require --recalc")
    if args.date_from and args.date_to and args.date_from > args.date_to:
        parser.error("--from must be on or before --to")
    return args


@contextmanager
def _dashboard_colors(mode: str):
    """Apply the dashboard's color preference for this invocation."""
    if mode == 'auto':
        yield
        return

    previous = os.environ.get('NO_COLOR')
    if mode == 'always':
        os.environ.pop('NO_COLOR', None)
    else:
        os.environ['NO_COLOR'] = '1'
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop('NO_COLOR', None)
        else:
            os.environ['NO_COLOR'] = previous


def _format_bytes(size_bytes: int) -> str:
    """Format a byte count as '12 B' / '1.5 KB' / '3.2 MB'."""
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def _format_age(timestamp: object) -> str:
    """Return a concise local age for persisted ingest timestamps."""
    if not isinstance(timestamp, str):
        return "never"
    elapsed = timestamp_age(timestamp)
    if elapsed is None:
        return "unknown"
    if elapsed < 60:
        return "now"
    if elapsed < 3600:
        return f"{int(elapsed // 60)}m ago"
    if elapsed < 86400:
        return f"{elapsed / 3600:.1f}h ago"
    return f"{elapsed / 86400:.1f}d ago"


def print_ledger_stats(ledger_path: Path, ledger: Dict[str, Dict]) -> None:
    """Print ledger file stats — size, entry counts, date range, projects."""
    try:
        size_bytes = ledger_path.stat().st_size
    except OSError:
        size_bytes = 0

    size_str = _format_bytes(size_bytes)

    by_source: Dict[str, int] = {}
    by_project: Dict[str, int] = {}
    min_dt: Optional[datetime] = None
    max_dt: Optional[datetime] = None
    subagent_count = 0

    for entry in ledger.values():
        if not isinstance(entry, dict):
            continue
        src = entry.get('source', '?')
        by_source[src] = by_source.get(src, 0) + 1
        proj = entry.get('project', '')
        if proj:
            by_project[proj] = by_project.get(proj, 0) + 1
        if entry.get('isSubagent'):
            subagent_count += 1
        try:
            dt = entry_local_dt(entry)
            if min_dt is None or dt < min_dt:
                min_dt = dt
            if max_dt is None or dt > max_dt:
                max_dt = dt
        except (ValueError, KeyError):
            continue

    print(f"Ledger file: {ledger_path}")
    print(f"Size:        {size_str}")
    print(f"Entries:     {len(ledger):,}")
    for src, count in sorted(by_source.items(), key=lambda x: x[1], reverse=True):
        print(f"  {src:<12} {count:,}")
    if subagent_count:
        print(f"  subagents   {subagent_count:,} (subset of cc)")
    if min_dt and max_dt:
        print(f"Range:       {min_dt.strftime('%Y-%m-%d')} → {max_dt.strftime('%Y-%m-%d')}")
    if by_project:
        print(f"Projects:    {len(by_project)}")
        for proj, count in sorted(by_project.items(), key=lambda x: x[1], reverse=True)[:10]:
            print(f"  {count:>6,}  {proj}")

    state = load_ingest_state(get_ingest_state_path(ledger_path))
    gap = hours_since_last_ingest(state)
    if gap is None:
        print("Last ingest: never")
    elif gap < 1:
        print(f"Last ingest: {int(gap * 60)}m ago")
    elif gap < 48:
        print(f"Last ingest: {gap:.1f}h ago")
    else:
        print(f"Last ingest: {gap / 24:.1f}d ago")

    bdir = backup_dir(ledger_path)
    backups = sorted(bdir.glob("ledger-*.json")) if bdir.exists() else []
    if backups:
        print(f"Backups:     {len(backups)} in {bdir}")
        print(f"  latest     {backups[-1].name}")

    print("Source health:")
    health_by_source = source_health(state)
    for source in SOURCES:
        health = health_by_source.get(source.short_name)
        if not isinstance(health, dict):
            print(f"  {source.display_name:<16} not yet scanned")
            continue
        if health.get("last_error"):
            print(f"  {source.display_name:<16} error · "
                  f"last success {_format_age(health.get('last_success_at'))}")
            continue
        seen = health.get("records_seen") or 0
        added = health.get("records_added") or 0
        print(f"  {source.display_name:<16} ok {_format_age(health.get('last_success_at'))}"
              f" · {seen:,} seen · {added:,} new")


def main():
    args = parse_arguments()

    if args.dry_run and not args.recalc:
        print("--dry-run only applies with --recalc")
        return 2

    ledger_path = None
    ledger = {}
    if args.report or args.stats or args.recalc:
        ledger_path = None if args.demo else get_ledger_path(args.ledger_path)
        ledger = build_demo_ledger() if args.demo else load_ledger(ledger_path)

    if args.recalc:
        from .pidfile import AlreadyRunning, single_instance

        dry = args.dry_run
        summary = {}
        source_filter = None if args.source == 'all' else SOURCE_MAP.get(args.source, args.source)
        try:
            with (nullcontext() if dry else single_instance()):
                # Reload after acquiring the writer lock to avoid a stale copy.
                ledger = load_ledger(ledger_path)
                old, new, changed, skipped = recalc_ledger_costs(
                    ledger_path, ledger, dry_run=dry, source_filter=source_filter,
                    model_filters=args.model, provider_filter=args.provider,
                    only_unpriced=args.only_unpriced,
                    include_source_reported=args.include_source_reported,
                    assume_route=args.assume_bedrock_route,
                    date_from=args.date_from, date_to=args.date_to,
                    project_filter=args.project, summary=summary,
                )
        except AlreadyRunning as exc:
            print(str(exc))
            print("Stop the dashboard before writing recalculated costs.")
            return 1
        label = "[dry run] " if dry else ""
        print(f"{label}Entries changed: {changed:,}")
        if skipped:
            print(f"{label}Entries skipped: {skipped:,} (incomplete token data or "
                  f"no configured pricing, or source-reported cost preserved)")
        print(f"{label}Old selected total: ${old:,.2f}")
        print(f"{label}New selected total: ${new:,.2f}")
        print(f"{label}Difference: ${new - old:,.2f}")
        for model, group in sorted(summary.items()):
            if model.startswith("_"):
                continue
            print(f"{label}{model}: {group['entries']:,} eligible, "
                  f"{group['changed']:,} changed, "
                  f"${group['old']:.4f} → ${group['new']:.4f}")
            for reference in sorted(group["references"]):
                print(f"  {reference}")
            for assumption in sorted(group["assumptions"]):
                print(f"  Assumption: {assumption}")
        if not dry and changed > 0:
            print(f"Ledger written to {ledger_path}")
            if "_backup" in summary:
                print(f"Backup: {summary['_backup']}")
        return 0

    if args.stats:
        print_ledger_stats(ledger_path, ledger)
        return 0

    if not args.report:
        try:
            from lcost.tui import CostTrackerApp
        except ImportError:
            print("Dashboard dependencies are missing. Reinstall with: pip install -e .")
            return 1
        from lcost.pidfile import AlreadyRunning, single_instance
        from lcost.terminal import compatible_app_class

        app_class = compatible_app_class(CostTrackerApp)
        try:
            with (nullcontext() if args.demo else single_instance(force=args.force)):
                with _dashboard_colors(args.color):
                    app = app_class(
                        ledger_path_override=args.ledger_path,
                        source_filter=args.source,
                        no_ingest=args.cached,
                        force_ingest=args.rescan,
                        project_filter=args.project,
                        date_from=args.date_from,
                        date_to=args.date_to,
                        days=None if args.all else args.days,
                        deep=args.deep,
                        max_gap_hours=args.max_gap_hours,
                        demo=args.demo,
                    )
                app.run()
        except AlreadyRunning as exc:
            print(str(exc))
            return 1
        return 0

    from .pidfile import AlreadyRunning, single_instance

    try:
        with (nullcontext() if args.demo or args.cached
              else single_instance(force=args.force)):
            if not args.demo and not args.cached:
                # Reports ingest too; reload only after acquiring the writer lock.
                ledger = load_ledger(ledger_path)
            added = 0 if args.demo else run_ingest(
                ledger_path, ledger, source=args.source,
                no_ingest=args.cached, force_ingest=args.rescan,
                verbose=args.verbose, quiet=args.quiet,
                max_gap_hours=args.max_gap_hours, deep=args.deep,
            )
    except AlreadyRunning as exc:
        print(str(exc))
        print("Use --report --cached to read stored usage while another instance runs.")
        return 1
    if added > 0 and not args.quiet:
        print(f"Ledger: {added} new entries ({len(ledger)} total)")

    source_filter = None if args.source == 'all' else SOURCE_MAP.get(args.source, args.source)

    limit_days = None if args.all or args.date_from or args.date_to else (args.days or 30)
    date_from = args.date_from
    date_to = args.date_to

    daily_data = aggregate_by_day(ledger, source_filter=source_filter,
                                  date_from=date_from, date_to=date_to,
                                  project_filter=args.project)

    sources_in_data = set()
    for _, _, entry in iter_entries(ledger):
        src = entry.get('source')
        if source_filter is None or src == source_filter:
            sources_in_data.add(src)

    title_parts = [SOURCE_DISPLAY.get(s, s) for s in sorted(sources_in_data)]
    title = (" + ".join(title_parts) + " " if title_parts else "") + "Daily Cost Summary"
    if args.demo:
        title = "DEMO DATA — " + title

    if not args.quiet:
        print()

    output = format_output(daily_data, limit_days=limit_days, title=title)
    print(output)

    return 0
