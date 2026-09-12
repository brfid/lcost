"""Deterministic synthetic usage for a populated, source-free demo."""

from datetime import datetime, timedelta, timezone
from random import Random
from typing import Dict, Optional

from .pricing import calculate_cache_savings, calculate_cost


# Keep each session on one task, project, and tool instead of rotating every call.
_TASKS = (
    ("codex", "openai.gpt-5.6-luna", "demo/atlas", "Add a usage summary"),
    ("codex", "openai.gpt-5.6-luna", "demo/cli", "Fix the date filter"),
    ("cc", "claude-sonnet-4-6", "demo/cli", "Test the report command"),
    ("cc", "claude-sonnet-4-6", "demo/docs", "Document the new filters"),
    ("cline", "claude-opus-4-7", "demo/site", "Improve the mobile layout"),
    ("cline", "claude-opus-4-7", "demo/atlas", "Review the summary view"),
)


def build_demo_ledger(now: Optional[datetime] = None) -> Dict:
    """Build a rolling 60-day sample of local work sessions in memory.

    Days have their own seeds and schedules: refreshing reveals new calls
    without moving old ones. Nights, breaks, and days off remain idle, even
    today. Never reads source sessions or writes a ledger.
    """
    now = now or datetime.now()
    if now.tzinfo is not None:
        now = now.astimezone().replace(tzinfo=None)
    ledger = {}
    for age in range(60):
        day = (now - timedelta(days=age)).replace(hour=0, minute=0, second=0, microsecond=0)
        rng = Random(day.date().isoformat())
        weekday = day.weekday()
        idle_chance = 0.85 if weekday == 6 else 0.55 if weekday == 5 else 0.10
        if rng.random() < idle_chance:
            continue

        if weekday >= 5:
            windows = [(10 * 60, 11 * 60 + 15)]  # An occasional short weekend task.
        else:
            windows = sorted(rng.sample([
                (8 * 60 + 30, 9 * 60 + 15),
                (10 * 60 + 45, 11 * 60 + 15),
                (13 * 60 + 30, 14 * 60),
                (15 * 60 + 30, 16 * 60 + 15),
            ], rng.randint(2, 4)))
            if rng.random() < 0.12:
                windows.append((20 * 60, 20 * 60 + 30))

        tasks = rng.sample(_TASKS, len(windows))
        for slot, ((earliest, latest), task) in enumerate(zip(windows, tasks)):
            source, model, project, prompt = task
            stamp = day + timedelta(minutes=rng.randint(earliest, latest),
                                    seconds=rng.randrange(60))
            duration = rng.randint(20, 40) if weekday >= 5 else rng.randint(30, 65)
            end = stamp + timedelta(minutes=duration)
            context = rng.randint(6000, 18000)
            call = 0
            while stamp < end:
                burst_size = rng.randint(2, 6)
                for turn in range(burst_size):
                    if stamp >= end:
                        break
                    tokens_in = rng.randint(300, 5000)
                    tokens_out = rng.randint(80, 2400)
                    reads = rng.randint(context // 2, context) if call else 0
                    writes = rng.randint(300, 4000) if source != "codex" and call % 5 == 0 else 0
                    context += tokens_in + tokens_out
                    # Claude's stored naive timestamps represent UTC; others have offsets.
                    ts = stamp.astimezone().isoformat()
                    if source == "cc":
                        ts = stamp.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
                    entry = {
                        "source": source, "ts": ts, "model": model,
                        "project": project, "session": f"demo-{day.date()}-{slot}",
                        "surface": "demo", "tokensIn": tokens_in, "tokensOut": tokens_out,
                        "cacheReads": reads, "cacheWrites": writes,
                        "cost": calculate_cost(tokens_in, tokens_out, writes, reads, model),
                        "cacheSavings": calculate_cache_savings(reads, model),
                        "pricingRef": "demo:illustrative-rate-card",
                        "promptPreview": prompt if source != "codex" else "",
                        "tools": ["Read", "Edit", "Bash"] if source != "codex" else [],
                        "stopReason": "end_turn" if turn == burst_size - 1 else "tool_use",
                    }
                    # Generate the complete schedule before filtering; RNG state must
                    # not depend on how much of today's work has elapsed.
                    if stamp <= now:
                        ledger[f"demo:{day.date()}:{slot}:{call}"] = entry
                    call += 1
                    stamp += timedelta(seconds=rng.randint(20, 100))
                # Time spent reading, testing, or thinking between assistant bursts.
                stamp += timedelta(minutes=rng.randint(5, 18))
    return ledger
