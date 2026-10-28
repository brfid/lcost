# lcost

lcost shows token usage and query costs from local Claude Code, Cline, and
Codex sessions. It combines their records in a ledger, with a terminal
dashboard for comparing projects, models, and periods of activity.

![lcost demo: task sessions in token units, a heatmap fitted to active hours, and the selected query's price and tokens](docs/lcost-demo.png)

The dashboard with synthetic data for a sample Monday afternoon. Press `m`
to switch the board between price and tokens; the selected query shows both.

## Install and try the demo

Requires Python 3.9 or later on macOS or Linux. Dashboard dependencies install
with the package. A [Nerd Font](https://www.nerdfonts.com/) provides the full
set of terminal glyphs.

Clone this repository from your Git host, then run:

```bash
cd lcost
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
lcost --demo
```

The demo supplies 60 days of sample history, with calls grouped into tasks,
breaks, quiet nights, and lighter weekends. Today's calls appear as their
scheduled times pass, so today can be empty before work or on a day off.
Demo mode neither scans personal sessions nor reads or writes your ledger.

## Track your usage

Run `lcost` to open the dashboard with your local session history. The
collectors read files written by Claude Code, Cline, and Codex, including
supported editor integrations. They leave those source files unchanged.

```bash
lcost
lcost --report
```

The text report defaults to the last 30 days with activity. Use
`lcost --demo --report` to print a report from the sample history.

Source and project filters work in both modes:

```bash
lcost --source codex
lcost --source claude-code
lcost --source cline --project example
lcost --report --days 7
lcost --report --from 2026-04-01 --to 2026-04-30
```

`--project` matches a case-insensitive substring of the stored project path.
Date bounds are inclusive. `--days N` selects calendar days in the dashboard
and active days in reports; see [date filtering](docs/reference.md#date-filtering)
for the defaults and interaction with other options. `lcost --help` lists
all command-line options.

Dashboard colors follow your terminal settings and `NO_COLOR` by default.
Use `lcost --color always` to enable the palette even when `NO_COLOR` is set,
or `lcost --color never` for monochrome.

## Read the dashboard

The board puts period summaries beside recent activity:

| Panel | Contents |
| --- | --- |
| Summary strip | Today, week to date, 30 days, token usage, cache efficiency, and daily/hourly usage rates |
| Trend | Daily usage over 30 calendar days by default, or hourly usage by model over the last 24 hours |
| Heatmap | Usage by hour and weekday; the hour range fits the selected data, retaining gaps between active hours |
| Hourly activity | Today's usage across the full 24 hours |
| Rankings | Today's leading projects, model mix, and stop reasons |
| Call log | The 100 most recent queries, with a price and token breakdown for the selected query |

On the board, `m` switches summaries, charts, project rankings, and call-log
values between price and total tokens. In token mode, the cache summary
shows the share of input served from cache. Query details always show both
units and the separate input, output, and cache buckets.

The heatmap uses the same hour range across units in both views. With no
activity, it shows the full day. The `h` key changes the hourly bar's metric
independently; the next board unit change brings it back into the board's
units.

Press `t` on the board or in the expanded trend to switch between daily
usage and a line for each model used in the rolling last 24 hours. The model
view uses the board's price or total-token setting; `m` changes that setting
in either the board or the expanded model view. Its clock hours include
partial hours at the edges; zero-usage hours leave gaps in the lines. Source, project,
and date filters apply to this view too.
Models contributing less than 1% of the selected unit's 24-hour total are
hidden from the chart and legend; the total still includes them. Lines and
legend entries follow descending usage, with distinct colors for the top four.

The dashboard refreshes every 30 seconds. New queries have
a `★` marker, and subagent calls have `↳`. The status bar shows the current
filters, units, entry count, and refresh state. In smaller terminals, the
board hides charts or the query inspector to leave room for the call log.

### Expand a panel

| Key | Panel | Metric control |
| --- | --- | --- |
| `1` | TREND | Expands the active trend view; `t` switches daily / models. Daily: `m` cycles price, input/output tokens, cache tokens, and savings. Models: `m` switches overall price / total tokens |
| `2` | HEATMAP | `m` cycles price, requests, and tokens |
| `3` | LOG | Uses the board's selected units |
| `4` | CALLS | Per-query price distribution |

### Controls

| Key | Action |
| --- | --- |
| `m` | Switch board units or cycle the expanded panel's metric, as listed above |
| `t` | Switch the trend between daily usage and models in the last 24 hours |
| `h` | Cycle the board's hourly bar through price, tokens, and requests |
| `0` | Return to the board |
| `Esc` | Close an open detail or help view, or return to the board |
| `]` / `[` | Next / previous expanded panel |
| `j` / `k` | Move down / up one query in the call log; scroll other panels |
| `J` / `K`, `Ctrl+d` / `Ctrl+u` | Move down / up ten queries, or scroll ten lines in other panels |
| `g` / `G` | First / last query, or top / bottom of the current panel |
| `Enter` | Open the selected query's details |
| `r` | Pause / resume automatic refresh |
| `?` | Show help for the current view |
| `q` / `Ctrl+c` | Quit |

## Understand the numbers

Cline supplies per-query costs in its session records. Claude Code and
Codex costs are estimates calculated from token counts and configured model
rates. These estimates do not allocate subscription fees or reproduce an
account invoice. Bedrock estimates for supported Claude and GPT versions account
for inference routing, prompt caching, context tiers, and recorded service tiers.
Unknown models and unsupported pricing remain unpriced. Unpriced usage
marks monetary totals as partial. See [tokens and pricing](docs/reference.md#tokens-and-pricing)
for the accounting rules and rate sources.

The ledger retains ingested usage after source sessions are deleted. Repeated
scans deduplicate entries and apply supported corrections. See the
[reference](docs/reference.md) for scanning, recovery, cost recalculation,
and stored data.

## Development

Install the commit hook to keep runtime usage files out of Git, then install
the test dependencies and run the suite:

```bash
./scripts/install-hooks.sh
pip install -e '.[test]'
python -m pytest -q
```

Tests use synthetic sessions and temporary ledgers. The
[GitHub workflow](.github/workflows/guard.yml) and
[GitLab pipeline](.gitlab-ci.yml) define the Python checks; GitHub also tests
macOS. The [module guide](docs/reference.md#module-responsibilities)
identifies where collection, storage, calculations, and presentation belong.
