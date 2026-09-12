"""Deterministic fixtures for dashboard behavior and calendar boundaries."""
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from lcost.live_metrics import ClockBucket


@pytest.fixture
def dashboard_clock(monkeypatch):
    @dataclass
    class Clock:
        now: datetime = datetime(2026, 9, 12, 12, 30, 30)

    clock = Clock()
    capture = ClockBucket.capture
    monkeypatch.setattr(ClockBucket, "capture", classmethod(
        lambda cls, now=None: capture(now or clock.now),
    ))
    return clock


@pytest.fixture
def dashboard_ledger(dashboard_clock):
    now = dashboard_clock.now
    return {
        "codex:cached": {
            "source": "codex", "ts": now.isoformat(),
            "model": "openai.gpt-5.6-luna", "modelProvider": "amazon-bedrock",
            "surface": "vscode", "project": "cached-project",
            "tokensIn": 1000, "tokensOut": 200, "cacheReads": 8000,
            "cacheWrites": 800, "reasoningTokens": 80, "cost": 1.0,
            "cacheSavings": 0.25, "pricingRef": "rate-card:test",
        },
        "cline:expensive": {
            "source": "cline", "ts": (now - timedelta(minutes=1)).isoformat(),
            "model": "claude-sonnet-4-6", "project": "expensive-project",
            "tokensIn": 40, "tokensOut": 60, "cacheReads": 0,
            "cacheWrites": 0, "cost": 9.0, "promptPreview": "An older query",
        },
    }
