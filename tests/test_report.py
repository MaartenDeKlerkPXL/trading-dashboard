"""The analysis report of a backtest (Markdown download)."""

import pytest

from backend.backtest.report import build_report, drawdowns, excursions, group_stats, streaks
from tests.test_phase3 import PERIOD, client, sync  # noqa: F401  (fixture)


def test_report_download_has_every_section(client):  # noqa: F811
    sync(client, PERIOD)
    r = client.post("/api/backtest", json={**PERIOD, "strategy": "bjorgum_3commas@v2", "sizing_mode": "fractional",
                                           "oos_pct": 30, "params": {"trail_stop": True}}).json()
    res = client.get(f"/api/runs/{r['run_id']}/report")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/markdown")
    assert 'filename="backtest-bjorgum_3commas_v2-XAUUSD-H1-2024-01-01_2024-06-30-run' in res.headers["content-disposition"]
    text = res.text
    for heading in ("# Backtest-analyse: 3Commas Bot (Bjorgum, exact uit Pine) v2 op XAUUSD H1", "## Instellingen",
                    "### Parameters van de strategie", "## Resultaat", "### In-sample tegenover out-of-sample",
                    "## Long tegenover short", "## Uitstapredenen", "## Verdeling van de uitkomst in R",
                    "## Hoe ver liepen trades mee en tegen? (MFE / MAE)", "## Per uur van instap",
                    "## Per weekdag van instap", "## Per maand", "## Marktomstandigheden bij instap",
                    "## Reeksen en kosten", "## Diepste drawdowns", "## Alle trades"):
        assert heading in text, heading
    assert "`trail_stop` | ja" in text
    csv = text.split("```csv\n")[1].split("```")[0].strip().splitlines()
    assert csv[0].startswith("nr,richting,instap_unix") and len(csv) - 1 == len(r["trades"])
    assert r["trades"][0]["initial_stop"] is not None
    assert client.get("/api/runs/999999/report").status_code == 404


def test_report_for_an_import_and_without_trades(client):  # noqa: F811
    csv = ("Trade #,Type,Signal,Date/Time,Price USD,Contracts,Profit USD,Profit %\n"
           "1,Exit Long,Close,2024-01-03 10:00,2010,1,10,1\n1,Entry Long,Long,2024-01-02 10:00,2000,1,,\n")
    run = client.post("/api/import/tradingview", json={"content": csv, "symbol": "XAUUSD", "timeframe": "H1",
                                                       "capital": 1000}).json()
    text = client.get(f"/api/runs/{run['run_id']}/report").text
    assert "## Alle trades" in text and "tradingview" in text
    sync(client, PERIOD)
    r = client.post("/api/backtest", json={**PERIOD, "start": "2024-01-01", "end": "2024-01-02",
                                           "strategy": "sma_cross@v1"}).json()
    text = client.get(f"/api/runs/{r['run_id']}/report").text
    assert "Deze backtest had geen trades." in text


def test_report_helpers():
    from backend.strategies.base import Bar
    trade = {"side": "long", "entry_price": 100.0, "initial_stop": 98.0, "stop_loss": 99.0,
             "entry_ts": 60, "exit_ts": 180}
    bars = [Bar(0, 100, 100, 100, 100, 0), Bar(60, 100, 104, 99, 103, 0), Bar(120, 103, 103, 97.5, 98, 0),
            Bar(180, 98, 98, 98, 98, 0), Bar(240, 98, 120, 90, 100, 0)]
    mfe, mae = excursions(trade, bars)
    assert mfe == pytest.approx(2.0) and mae == pytest.approx(1.25)      # in R of the initial 2-point risk
    s = group_stats([{"pnl": 10, "r_multiple": 1.0}, {"pnl": -5, "r_multiple": -0.5}])
    assert s["winrate"] == 50 and s["profit_factor"] == 2 and s["avg_r"] == pytest.approx(0.25)
    assert streaks([{"pnl": 1}, {"pnl": 1}, {"pnl": -1}, {"pnl": 2}]) == (2, 1)
    eq = [{"time": t, "value": v} for t, v in enumerate([100, 110, 99, 105, 111, 90, 95])]
    dd = drawdowns(eq)
    assert dd[0]["depth"] == pytest.approx((111 - 90) / 111 * 100) and dd[0]["recovered"] is None
    assert dd[1]["depth"] == pytest.approx(10.0) and dd[1]["recovered"] == 4
    assert build_report({"id": 1, "settings": {"symbol": "XAUUSD", "timeframe": "H1"}, "metrics": {}}, None)
