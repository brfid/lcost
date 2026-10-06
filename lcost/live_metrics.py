"""Calendar-based price and token summaries shared by live dashboard widgets."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from textual.reactive import reactive
from textual.widget import Widget

from .aggregation import calendar_days
from .formatters import (
    FIELD_CACHE_READS, FIELD_CACHE_SAVINGS, FIELD_CACHE_WRITES, FIELD_COST,
    FIELD_REQUESTS, FIELD_TOKENS_IN, FIELD_TOKENS_OUT, SOURCES,
    FIELD_UNPRICED_REQUESTS, FIELD_UNPRICED_TOKENS,
    calculate_totals, total_tokens,
)
from .ops_data import (
    OpsView, SourceToday, aggregate_today, collect_entries, derive_ops_view,
    short_model, short_project, source_today,
)


@dataclass(frozen=True)
class ClockBucket:
    """One clock reading for every reduction in a refresh."""
    now: datetime
    minute_key: str
    hour_key: str
    day: str

    @classmethod
    def capture(cls, now: Optional[datetime] = None) -> "ClockBucket":
        now = now or datetime.now()
        return cls(now, now.strftime("%Y-%m-%dT%H:%M"),
                   now.strftime("%Y-%m-%dT%H"), now.strftime("%Y-%m-%d"))


@dataclass(frozen=True)
class PeriodMetrics:
    """The same periods and sparklines for either dollars or tokens."""
    today: float
    week: float
    month: float
    week_change: str
    burn_rate: float
    spark_7d: List[float]
    spark_4w: List[float]
    spark_30d: List[float]
    spark_burn: List[float]


def _periods(values: Dict, clock: ClockBucket) -> PeriodMetrics:
    days = calendar_days(clock.now, 36)
    series = [values.get(day, 0) for day in days]
    monday = clock.now - timedelta(days=clock.now.weekday())
    weeks = []
    for weeks_ago in (3, 2, 1, 0):
        start = monday - timedelta(days=7 * weeks_ago)
        length = 7 if weeks_ago else clock.now.weekday() + 1
        weeks.append(sum(values.get((start + timedelta(days=i)).strftime("%Y-%m-%d"), 0)
                         for i in range(length)))
    # Compare like-for-like portions of this week and the preceding week.
    previous = sum(values.get(day, 0) for day in calendar_days(
        clock.now - timedelta(days=7), clock.now.weekday() + 1,
    ))
    if previous:
        delta = (weeks[-1] / previous - 1) * 100
        change = f"{'↑' if delta >= 0 else '↓'} {abs(delta):.0f}% vs prior WTD"
    else:
        change = "no prior WTD activity"
    return PeriodMetrics(
        today=series[-1], week=weeks[-1], month=sum(series[-30:]),
        week_change=change, burn_rate=sum(series[-7:]) / 7,
        spark_7d=series[-7:], spark_4w=weeks, spark_30d=series[-30:],
        spark_burn=[sum(series[i - 6:i + 1]) / 7 for i in range(6, 36)],
    )


@dataclass(frozen=True)
class OverviewMetrics:
    cost: PeriodMetrics
    tokens: PeriodMetrics
    today_requests: int
    month_requests: int
    tokens_7d_in: int
    tokens_7d_out: int
    tokens_7d_cache: int
    cache_savings_30d: float
    cache_reads_30d: int
    cache_eff_label: str
    cache_hit_label: str
    spark_7d_cache: List[float]
    spark_7d_cache_hits: List[float]
    today_unpriced_requests: int
    month_unpriced_requests: int
    month_unpriced_tokens: int


def _overview(daily: Dict[str, Dict], clock: ClockBucket) -> OverviewMetrics:
    days = calendar_days(clock.now, 30)
    totals = calculate_totals({day: daily.get(day, {}) for day in days})
    week = calculate_totals({day: daily.get(day, {}) for day in days[-7:]})
    potential = totals[FIELD_COST] + totals[FIELD_CACHE_SAVINGS]
    input_total = (totals[FIELD_TOKENS_IN] + totals[FIELD_CACHE_READS]
                   + totals[FIELD_CACHE_WRITES])
    cache_sparks = []
    hit_sparks = []
    for day in days[-7:]:
        d = daily.get(day, {})
        savings = d.get(FIELD_CACHE_SAVINGS, 0)
        potential_day = d.get(FIELD_COST, 0) + savings
        reads = d.get(FIELD_CACHE_READS, 0)
        inputs = d.get(FIELD_TOKENS_IN, 0) + d.get(FIELD_CACHE_WRITES, 0) + reads
        cache_sparks.append(savings / potential_day * 100 if potential_day else 0)
        hit_sparks.append(reads / inputs * 100 if inputs else 0)
    return OverviewMetrics(
        cost=_periods({day: d[FIELD_COST] for day, d in daily.items()}, clock),
        tokens=_periods({day: total_tokens(d) for day, d in daily.items()}, clock),
        today_requests=daily.get(clock.day, {}).get(FIELD_REQUESTS, 0),
        month_requests=totals[FIELD_REQUESTS],
        tokens_7d_in=week[FIELD_TOKENS_IN], tokens_7d_out=week[FIELD_TOKENS_OUT],
        tokens_7d_cache=week[FIELD_CACHE_READS] + week[FIELD_CACHE_WRITES],
        cache_savings_30d=totals[FIELD_CACHE_SAVINGS],
        cache_reads_30d=totals[FIELD_CACHE_READS],
        cache_eff_label=(f"{totals[FIELD_CACHE_SAVINGS] / potential * 100:.0f}% saved"
                         if potential else "—"),
        cache_hit_label=(f"{totals[FIELD_CACHE_READS] / input_total * 100:.0f}% hits"
                         if input_total else "—"),
        spark_7d_cache=cache_sparks, spark_7d_cache_hits=hit_sparks,
        today_unpriced_requests=daily.get(clock.day, {}).get(FIELD_UNPRICED_REQUESTS, 0),
        month_unpriced_requests=totals[FIELD_UNPRICED_REQUESTS],
        month_unpriced_tokens=totals[FIELD_UNPRICED_TOKENS],
    )


@dataclass(frozen=True)
class MetricsSnapshot:
    """Replace the whole snapshot so widgets share one consistent refresh."""
    clock: ClockBucket
    overview: OverviewMetrics
    ops: OpsView
    ops_entries: List
    daily: Dict[str, Dict] = field(default_factory=dict)
    source_filter: Optional[str] = None
    source_today: Tuple[SourceToday, ...] = ()
    source_health: Dict[str, Dict] = field(default_factory=dict)


def compute_snapshot(ledger: Dict, daily: Dict[str, Dict],
                     source_filter: Optional[str],
                     clock: Optional[ClockBucket] = None,
                     source_health: Optional[Dict[str, Dict]] = None) -> MetricsSnapshot:
    clock = clock or ClockBucket.capture()
    entries = collect_entries(ledger, source_filter)
    stats = aggregate_today(entries, short_project, short_model, now=clock.now)
    source_names = ((source_filter,) if source_filter
                    else tuple(source.short_name for source in SOURCES))
    return MetricsSnapshot(
        clock=clock, overview=_overview(daily, clock),
        ops=derive_ops_view(entries, stats, now=clock.now), ops_entries=entries,
        daily=daily, source_filter=source_filter,
        source_today=source_today(entries, source_names, now=clock.now),
        source_health=dict(source_health or {}),
    )


class LiveMetrics(Widget):
    """Invisible reactive carrier watched by mounted dashboard widgets."""
    DEFAULT_CSS = "LiveMetrics { display: none; }"
    snapshot: reactive[Optional[MetricsSnapshot]] = reactive(None, layout=False)

    def update(self, snap: MetricsSnapshot) -> None:
        self.snapshot = snap
