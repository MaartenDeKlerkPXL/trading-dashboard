"""Comparing saved runs: normalized equity, differences in setup, and trade-by-trade matching."""

from __future__ import annotations

from .data.instruments import TIMEFRAMES

SETUP_FIELDS = {
    "symbol": "instrument",
    "timeframe": "timeframe",
    "start": "startdatum",
    "end": "einddatum",
    "capital": "startkapitaal",
    "risk_pct": "risico per trade",
    "sizing_mode": "lotgrootte",
    "costs": "kosten",
}


def run_label(run: dict) -> str:
    s = run["settings"]
    name = run.get("name") or f"{s.get('strategy_label', s.get('strategy', '?'))} {s.get('version') or ''}".strip()
    return f"#{run['id']} {name}"


def normalized_equity(run: dict) -> list[dict]:
    capital = run["settings"].get("capital") or 1.0
    return [{"time": p["time"], "value": round((p["value"] / capital - 1) * 100, 3)} for p in run.get("equity", [])]


def setup_differences(runs: list[dict]) -> list[str]:
    """Dutch descriptions of settings that are not identical across the engine runs."""
    engine = [r for r in runs if r["source"] == "engine"]
    diffs = []
    for key, label in SETUP_FIELDS.items():
        values = {repr(r["settings"].get(key)) for r in engine}
        if len(values) > 1:
            diffs.append(label)
    imported = [r for r in runs if r["source"] != "engine"]
    for key in ("symbol", "timeframe"):
        values = {r["settings"].get(key) for r in runs}
        if imported and len(values) > 1 and SETUP_FIELDS[key] not in diffs:
            diffs.append(SETUP_FIELDS[key])
    return diffs


def match_trades(reference: list[dict], ours: list[dict], timeframe: str) -> dict:
    """Pair each reference (TradingView) trade with our trade in the same direction that opened
    at (nearly) the same time: at most one bar apart. Greedy on the smallest time difference."""
    bar = TIMEFRAMES[timeframe].seconds if timeframe in TIMEFRAMES else 3600
    pairs = []
    for i, a in enumerate(reference):
        for j, b in enumerate(ours):
            diff = abs(a["entry_ts"] - b["entry_ts"])
            if a["side"] == b["side"] and diff <= bar:
                pairs.append((diff, i, j))
    pairs.sort()
    used_a, used_b, matches = set(), set(), []
    for diff, i, j in pairs:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        a, b = reference[i], ours[j]
        matches.append({
            "reference_id": a["id"], "our_id": b["id"], "side": a["side"],
            "entry_ts": a["entry_ts"], "our_entry_ts": b["entry_ts"],
            "entry_price": a["entry_price"], "our_entry_price": b["entry_price"],
            "exit_ts": a["exit_ts"], "our_exit_ts": b["exit_ts"],
            "exit_price": a["exit_price"], "our_exit_price": b["exit_price"],
            "exit_bars_apart": round(abs(a["exit_ts"] - b["exit_ts"]) / bar, 1),
        })
    matches.sort(key=lambda m: m["entry_ts"])
    n_ref, n_ours, n = len(reference), len(ours), len(matches)
    price_diffs = [abs(m["entry_price"] - m["our_entry_price"]) for m in matches]
    same_exit = sum(1 for m in matches if m["exit_bars_apart"] <= 1)
    return {
        "reference_trades": n_ref,
        "our_trades": n_ours,
        "matched": n,
        "match_pct": n / max(n_ref, n_ours) * 100 if max(n_ref, n_ours) else 0.0,
        "only_reference": [reference[i]["id"] for i in range(n_ref) if i not in used_a],
        "only_ours": [ours[j]["id"] for j in range(n_ours) if j not in used_b],
        "avg_entry_price_diff": sum(price_diffs) / n if n else None,
        "same_exit_pct": same_exit / n * 100 if n else None,
        "matches": matches[:500],
    }


def build_comparison(runs: list[dict]) -> dict:
    out_runs = []
    for r in runs:
        out_runs.append({
            "id": r["id"], "label": run_label(r), "source": r["source"], "settings": r["settings"],
            "metrics": r["metrics"], "equity_pct": normalized_equity(r), "created_at": r["created_at"],
            "strategy_changed": r.get("strategy_changed", False),
        })

    warnings = []
    diffs = setup_differences(runs)
    if diffs:
        warnings.append("Deze runs zijn niet onder dezelfde omstandigheden gemaakt (verschil in: "
                        + ", ".join(diffs) + "). Vergelijk ze met voorzichtigheid.")
    if any(r.get("strategy_changed") for r in runs):
        warnings.append("Van minstens één run is het strategiebestand later gewijzigd: opnieuw draaien kan "
                        "een andere uitkomst geven.")

    matchings = []
    references = [r for r in runs if r["source"] == "tradingview"]
    engines = [r for r in runs if r["source"] == "engine"]
    for ref in references:
        for eng in engines:
            if eng["settings"].get("symbol") != ref["settings"].get("symbol"):
                continue
            result = match_trades(ref.get("trades", []), eng.get("trades", []), eng["settings"].get("timeframe"))
            result.update(reference_run=ref["id"], our_run=eng["id"],
                          reference_label=run_label(ref), our_label=run_label(eng))
            matchings.append(result)
    return {"runs": out_runs, "warnings": warnings, "matchings": matchings[:6]}
