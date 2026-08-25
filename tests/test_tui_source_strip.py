"""Board-level regression tests for all-source visibility."""

import asyncio
from datetime import datetime

from lcost.ledger import save_ledger
from lcost.tui import CostTrackerApp


def test_board_source_strip_shows_all_sources_and_tracks_metric(tmp_path):
    async def scenario():
        now = datetime.now().replace(microsecond=0)
        ledger_path = tmp_path / "ledger.json"
        save_ledger(ledger_path, {
            "codex:one": {
                "source": "codex", "ts": now.isoformat(),
                "tokensIn": 1000, "tokensOut": 20, "cost": 0.50,
            },
            "cc:one": {
                "source": "cc", "ts": now.isoformat(),
                "tokensIn": 20, "tokensOut": 10, "cost": 0.10,
            },
            "cline:one": {
                "source": "cline", "ts": now.isoformat(),
                "tokensIn": 5, "tokensOut": 5, "cost": 0.05,
            },
        })
        app = CostTrackerApp(
            ledger_path_override=str(ledger_path), no_ingest=True,
        )
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            price_text = str(app.query_one("#hud-source-strip").render())
            assert "ALL SOURCES" in price_text
            assert "PRICE" in price_text
            assert "Codex" in price_text
            assert "Claude" in price_text
            assert "Cline" in price_text

            await pilot.press("m")
            await pilot.pause()
            token_text = str(app.query_one("#hud-source-strip").render())
            assert "TOKENS" in token_text
            assert "1.0 KTok" in token_text

    asyncio.run(scenario())


def test_board_selected_query_inspector_tracks_log_selection(tmp_path):
    async def scenario():
        now = datetime.now().replace(microsecond=0)
        ledger_path = tmp_path / "ledger.json"
        save_ledger(ledger_path, {
            "codex:newest": {
                "source": "codex", "ts": now.isoformat(),
                "model": "openai.gpt-5.6-luna",
                "modelProvider": "amazon-bedrock",
                "surface": "vscode",
                "project": "new-project",
                "promptPreview": "Newest request",
                "tools": ["shell"],
                "tokensIn": 1000, "tokensOut": 20,
                "cacheReads": 400, "cacheWrites": 100,
                "reasoningTokens": 5, "cost": 0.50,
                "pricingRef": "rate-card:test:luna",
            },
            "cc:older": {
                "source": "cc", "ts": (now.replace(second=max(0, now.second - 1))).isoformat(),
                "model": "claude-sonnet-4-6",
                "project": "older-project",
                "promptPreview": "Older request",
                "tokensIn": 20, "tokensOut": 10, "cost": 0.10,
            },
        })
        app = CostTrackerApp(
            ledger_path_override=str(ledger_path), no_ingest=True,
        )
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            detail = app.query_one("#query-detail-content")
            newest = str(detail.render())
            assert "QUERY PRICE" in newest
            assert "new-project" in newest
            assert "amazon-bedrock" in newest

            # First j selects the default top row; second advances to older row.
            await pilot.press("j", "j")
            await pilot.pause()
            selected = str(detail.render())
            assert "older-project" in selected
            assert "Older request" in selected

            await pilot.press("3")
            await pilot.pause()
            assert not app.query("#hud-log-detail")

    asyncio.run(scenario())


def test_board_selected_query_inspector_collapses_on_narrow_terminal(tmp_path):
    async def scenario():
        now = datetime.now().replace(microsecond=0)
        ledger_path = tmp_path / "ledger.json"
        save_ledger(ledger_path, {
            "codex:one": {
                "source": "codex", "ts": now.isoformat(),
                "tokensIn": 1000, "tokensOut": 20, "cost": 0.50,
            },
        })
        app = CostTrackerApp(
            ledger_path_override=str(ledger_path), no_ingest=True,
        )
        async with app.run_test(size=(100, 50)) as pilot:
            await pilot.pause()
            assert app.query_one("#hud-log-table").display
            assert not app.query_one("#hud-log-detail").display

    asyncio.run(scenario())
