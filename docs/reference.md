# lcost reference

See the [README](../README.md) for installation, the demo, and dashboard
controls. This reference covers filtering, stored data, and maintenance.

## Date filtering

`--from` and `--to` accept inclusive dates in local time. Either option takes
precedence over `--days`. Source and project filters combine with date filters.

In the dashboard, `--days N` selects the last N calendar days, including today,
for every panel. Without an explicit date filter, panels use their own periods:
the board's daily trend shows 30 days, period summaries show their named
windows, and the heatmap uses available history. `--all` removes a `--days`
filter; it does not expand every chart to the full history.

In reports, `--days N` selects the last N days that contain activity; the
default is 30. `--all` reports every active day. Explicit date bounds also
remove that default limit.

Dashboard charts retain idle calendar days as zero. Week-to-date comparisons
use the same elapsed weekdays in the preceding week.

## Scanning and recovery

Normal runs scan incrementally: unchanged files are skipped, and append-only
logs resume from their saved offsets. The collectors merge new entries and
supported corrections into the ledger. A failed source scan leaves the other
sources available; `lcost --stats` reports each source's scan state.

| Option | Effect |
| --- | --- |
| `--cached` | Read the ledger without scanning source sessions |
| `--deep` | Re-parse available sessions while retaining ledger entries and deduplicating by entry ID |
| `--rescan` | Ignore saved scan state and scan available files from the start |
| `--max-gap-hours H` | Trigger a deep scan after an ingestion gap longer than H hours; defaults to 24 |

Use `--deep` when recent usage appears to be missing, or `--rescan` after
recovering from unavailable or corrupt scan state. Both retain the ledger.
They can recover only data still present in the source sessions.

`lcost --stats` also reports ledger size, entry counts, date range, projects,
and backups. `--verbose` adds discovery and scan diagnostics to text reports;
`--quiet` suppresses their status messages. The dashboard reports scan state
in its source strip.

The dashboard holds a process lock while it runs. If another instance holds
the lock, `--force` waits for it to exit before starting. Closing the process
releases the lock.

## Stored data

The default data directory is `~/.local/share/lcost/`, or
`$XDG_DATA_HOME/lcost/` when that variable is set.

| File | Purpose |
| --- | --- |
| `ledger.json` | Usage records keyed by entry ID |
| `ingest_state.json` | File offsets, scan results, and the last ingestion time; can be rebuilt from available sources |
| `backups/ledger-YYYY-MM-DD.json` | Daily snapshots taken before ingestion changes the ledger; the seven most recent are retained |

`--ledger-path PATH` selects a different ledger. Its scan state and backups
live beside it. The dashboard lock is stored separately at
`~/.cache/lcost/tui.pid`, or `$XDG_CACHE_HOME/lcost/tui.pid`.

To restore a backup, stop the dashboard, preserve the current ledger, and
copy the selected backup over `ledger.json`. Open with `--cached` to inspect
the restored data before scanning again.

The former `claudit` data and cache directories migrate to `lcost` when the
new directories do not already exist. Existing `lcost` directories take
precedence. Use the `lcost` command after upgrading.

### Source records

Claude Code entries can include a prompt preview, tools, stop reason, project,
session, and subagent marker alongside their usage values. Cline supplies
per-request records, including provider-reported costs. Its provider-prefixed
model IDs are normalized for model-family matching.

Codex reads rollout logs under `$CODEX_HOME/sessions` and
`$CODEX_HOME/archived_sessions`, defaulting to `~/.codex/`. It stores token
observations and limited session metadata, including the model and surface.
It does not retain prompts, responses, tool payloads, or credentials.

The shared record fields and treatment of unknown values are defined in
[`UsageEntry` and normalization](../lcost/usage.py). Source-specific metadata
belongs to the [Claude Code and Cline collectors](../lcost/collectors.py) and
the [Codex collector](../lcost/codex_collector.py).

## Tokens and pricing

Total tokens are the sum of input, output, cache reads, and cache writes.
Input excludes the separately recorded cache buckets. Reasoning tokens are
part of output and are not counted again. Unknown values remain explicit
in stored records; summaries add only observed values.

For Claude Code and Codex, lcost applies its configured rates to those token
buckets. Cline costs are preserved from the source during ingestion. Rates
are in USD per million tokens; [pricing.py](../lcost/pricing.py) owns the model
families, rates, and their references. Keep rate changes there and update
`RATE_CARD_VERSION` so new estimates retain an identifiable rate card.

Model families match normalized model IDs, with more specific matches taking
precedence. A recognized family without configured rates has no calculated
price. An entirely unrecognized model currently falls back to the default
Sonnet rates; inspect the configured matching rules before relying on an
estimate for a new model.

### Recalculate stored costs

Preview recalculation before applying a rate change:

```bash
lcost --recalc --dry-run
lcost --recalc
```

Recalculation applies the current configured rates to eligible ledger entries,
including Cline entries that originally used provider-reported costs. Changed
entries are marked as rate-card estimates. Entries lacking a model, complete
token counts, or configured rates retain their stored costs. The command
reports old and new totals, changes, and skipped entries.

## Module responsibilities

| Responsibility | Location |
| --- | --- |
| Parse source sessions | [collectors.py](../lcost/collectors.py), [codex_collector.py](../lcost/codex_collector.py) |
| Normalize usage records | [usage.py](../lcost/usage.py) |
| Coordinate scans and source failures | [pipeline.py](../lcost/pipeline.py) |
| Persist usage and backups | [ledger.py](../lcost/ledger.py) |
| Track offsets and scan state | [ingest_state.py](../lcost/ingest_state.py) |
| Own model matching and rates | [pricing.py](../lcost/pricing.py) |
| Reduce records into summaries | [aggregation.py](../lcost/aggregation.py), [ops_data.py](../lcost/ops_data.py), [live_metrics.py](../lcost/live_metrics.py) |
| Format fields and define sources | [formatters.py](../lcost/formatters.py) |
| Parse options and select a mode | [cli.py](../lcost/cli.py) |
| Compose the dashboard and handle input | [tui.py](../lcost/tui.py) |
| Render widgets and bind them to summaries | [ops_widgets.py](../lcost/ops_widgets.py) |
