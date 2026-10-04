"""Parameter optimization that never looks at the out-of-sample period.

Single split: the last `oos_pct` of the bars is locked away. Every parameter
combination is tested on the in-sample part only; the best one is then run
once on the locked part. Walk-forward repeats this over several consecutive
windows (train on the past, test on the next unseen piece) and chains the
out-of-sample results.
"""

from __future__ import annotations

import math
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from itertools import product

from ..data.instruments import Instrument
from ..strategies import load_strategies
from ..strategies.base import Bar, Strategy
from .engine import BacktestConfig, RateSeries, run_backtest
from .service import MIN_SEGMENT_BARS, split_index

MAX_COMBINATIONS = 400
MAX_VALUES_PER_AXIS = 40
MAX_FOLDS = 8
MANY_VARIANTS = 50
PF_CAP = 10.0

TARGETS = {
    "sharpe": "Sharpe",
    "return_dd": "Rendement ÷ max. drawdown",
    "total_return_pct": "Totaal rendement",
    "profit_factor": "Profit factor",
    "expectancy_r": "Expectancy (R)",
}

SUMMARY_KEYS = ("total_return_pct", "max_drawdown_pct", "sharpe", "sortino", "profit_factor", "winrate_pct",
                "trades", "expectancy_r", "avg_trade", "no_losing_trades")


@dataclass(frozen=True)
class Axis:
    name: str
    label: str
    values: tuple


def build_axes(cls: type[Strategy], ranges: list[dict]) -> list[Axis]:
    if not 1 <= len(ranges) <= 2:
        raise ValueError("Kies één of twee parameters om te variëren.")
    params = {p.name: p for p in cls.params}
    axes, seen = [], set()
    for r in ranges:
        name = r.get("name")
        p = params.get(name)
        if p is None or p.kind == "bool":
            raise ValueError(f"Parameter '{name}' kan niet gevarieerd worden.")
        if name in seen:
            raise ValueError("Kies twee verschillende parameters.")
        seen.add(name)
        try:
            start, stop, step = float(r["start"]), float(r["stop"]), float(r["step"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"Vul van, tot en stap in voor '{p.label}'.") from None
        if step <= 0 or stop < start:
            raise ValueError(f"'{p.label}': 'tot' moet groter zijn dan 'van', en de stap groter dan 0.")
        count = int(math.floor((stop - start) / step + 1e-9)) + 1
        if count > MAX_VALUES_PER_AXIS:
            raise ValueError(f"'{p.label}' krijgt {count} waarden; maximaal {MAX_VALUES_PER_AXIS}. Vergroot de stap.")
        values = []
        for i in range(count):
            v = start + i * step
            v = int(round(v)) if p.kind == "int" else round(v, 10)
            if (p.min is not None and v < p.min) or (p.max is not None and v > p.max):
                raise ValueError(f"'{p.label}' moet tussen {p.min} en {p.max} liggen.")
            if v not in values:
                values.append(v)
        axes.append(Axis(name, p.label, tuple(values)))
    total = math.prod(len(a.values) for a in axes)
    if total > MAX_COMBINATIONS:
        raise ValueError(f"{total} combinaties is te veel (maximaal {MAX_COMBINATIONS}). Vergroot de stappen.")
    return axes


def score(metrics: dict, target: str, min_trades: int) -> float | None:
    if metrics["trades"] < min_trades:
        return None
    if target == "return_dd":
        dd = metrics["max_drawdown_pct"]
        return metrics["total_return_pct"] / max(dd, 0.5)
    if target == "profit_factor":
        pf = metrics["profit_factor"]
        if pf is None:
            return PF_CAP if metrics.get("no_losing_trades") else None
        return min(pf, PF_CAP)
    value = metrics.get(target)
    return None if value is None else float(value)


# ---------- evaluation (runs in worker processes) ----------

_WORKER: dict = {}


def _init_worker(bars, instrument, config, rates) -> None:
    _WORKER.update(bars=bars, instrument=instrument, config=config, rates=rates)


def _evaluate(key: str, params: dict, start: int, end: int) -> dict:
    cls = load_strategies()[key]
    w = _WORKER
    result = run_backtest(w["bars"][start:end], cls(**params), w["instrument"], w["config"], w["rates"])
    return {k: result["metrics"].get(k) for k in SUMMARY_KEYS}


class Evaluator:
    """Runs (params, start, end) jobs, in parallel processes when there are many."""

    def __init__(self, bars: list[Bar], instrument: Instrument, config: BacktestConfig, rates: RateSeries,
                 workers: int | None = None):
        self.args = (bars, instrument, config, rates)
        cpu = os.cpu_count() or 2
        self.workers = workers if workers is not None else max(1, min(cpu - 1, 8))
        self.pool: ProcessPoolExecutor | None = None

    def __enter__(self):
        if self.workers > 1:
            self.pool = ProcessPoolExecutor(self.workers, initializer=_init_worker, initargs=self.args)
        else:
            _init_worker(*self.args)
        return self

    def __exit__(self, *exc):
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)

    def map(self, key: str, jobs: list[tuple[dict, int, int]], on_done) -> list[dict]:
        results: list[dict | None] = [None] * len(jobs)
        if self.pool is None:
            for i, (params, start, end) in enumerate(jobs):
                results[i] = _evaluate(key, params, start, end)
                on_done()
            return results
        futures = {self.pool.submit(_evaluate, key, p, s, e): i for i, (p, s, e) in enumerate(jobs)}
        # A single backtest takes seconds at most; never wait forever on a stuck worker process.
        try:
            for future in as_completed(futures, timeout=120 + 30 * len(jobs)):
                results[futures[future]] = future.result()
                on_done()
        except FuturesTimeout:
            raise ValueError("De berekening liep vast. Herstart het dashboard en probeer het opnieuw.") from None
        return results


# ---------- optimization ----------

def _combinations(cls: type[Strategy], axes: list[Axis], fixed: dict) -> tuple[list[dict], list[str | None]]:
    """All parameter sets plus, per set, why it is invalid (None = valid)."""
    combos, invalid = [], []
    for values in product(*(a.values for a in axes)):
        params = {**fixed, **{a.name: v for a, v in zip(axes, values)}}
        try:
            params = cls(**params).p
            invalid.append(None)
        except ValueError as exc:
            invalid.append(str(exc))
        combos.append(params)
    return combos, invalid


def _neighbors(index: int, axes: list[Axis]) -> list[int]:
    sizes = [len(a.values) for a in axes]
    coords = []
    rest = index
    for size in reversed(sizes):
        coords.append(rest % size)
        rest //= size
    coords.reverse()
    out = []
    for delta in product(*([-1, 0, 1] for _ in sizes)):
        if not any(delta):
            continue
        c = [x + d for x, d in zip(coords, delta)]
        if all(0 <= x < s for x, s in zip(c, sizes)):
            flat = 0
            for x, s in zip(c, sizes):
                flat = flat * s + x
            out.append(flat)
    return out


def optimize(
    bars: list[Bar],
    cls: type[Strategy],
    instrument: Instrument,
    config: BacktestConfig,
    rates: RateSeries,
    fixed: dict,
    ranges: list[dict],
    target: str = "sharpe",
    min_trades: int = 10,
    oos_pct: float = 30.0,
    folds: int = 1,
    progress=lambda done, total: None,
    workers: int | None = None,
) -> dict:
    if target not in TARGETS:
        raise ValueError("Onbekend optimalisatiedoel.")
    if not 10 <= oos_pct <= 50:
        raise ValueError("Het out-of-sample-deel moet tussen 10 en 50% liggen.")
    if not 1 <= folds <= MAX_FOLDS:
        raise ValueError(f"Kies 1 tot {MAX_FOLDS} walk-forward-vensters.")
    config.validate()

    axes = build_axes(cls, ranges)
    fixed = cls(**fixed).p  # validates fixed values and fills defaults
    fixed = {k: v for k, v in fixed.items() if k not in {a.name for a in axes}}
    combos, invalid = _combinations(cls, axes, fixed)
    valid_idx = [i for i, why in enumerate(invalid) if why is None]
    if not valid_idx:
        raise ValueError(f"Geen enkele combinatie is geldig: {invalid[0]}")

    n = len(bars)
    split = split_index(n, oos_pct)
    max_warmup = max(cls(**combos[i]).warmup() for i in valid_idx)
    test_len = (n - split) // folds
    if split < max_warmup + MIN_SEGMENT_BARS or test_len < MIN_SEGMENT_BARS:
        raise ValueError("Te weinig candles voor deze verdeling. Kies een langere periode, een kleinere timeframe "
                         "of minder walk-forward-vensters.")

    windows = []
    for f in range(folds):
        test_start = split + f * test_len
        test_end = n if f == folds - 1 else test_start + test_len
        windows.append((test_start - split, test_start, test_end))  # train = [train_start, test_start)

    key = cls.key()
    total = folds * len(valid_idx) + folds
    done = 0

    def tick():
        nonlocal done
        done += 1
        progress(done, total)

    progress(0, total)
    fold_results = []
    first_grid = None
    if workers is None and total < 16:
        workers = 1  # starting worker processes costs more than it saves for a handful of runs
    with Evaluator(bars, instrument, config, rates, workers) as ev:
        for f, (train_start, test_start, test_end) in enumerate(windows):
            jobs = [(combos[i], train_start, test_start) for i in valid_idx]
            metrics_list = ev.map(key, jobs, tick)
            grid = [None] * len(combos)
            for i, m in zip(valid_idx, metrics_list):
                grid[i] = {"metrics": m, "score": score(m, target, min_trades)}
            scored = [i for i in valid_idx if grid[i]["score"] is not None]
            if not scored:
                raise ValueError(
                    f"Geen enkele combinatie haalde {min_trades} trades in de trainingsperiode. "
                    "Verlaag het minimum aantal trades of kies een langere periode."
                )
            best = max(scored, key=lambda i: grid[i]["score"])
            warm = cls(**combos[best]).warmup()
            oos = ev.map(key, [(combos[best], max(0, test_start - warm), test_end)], tick)[0]
            fold_results.append({
                "train_from": bars[train_start].ts, "test_from": bars[test_start].ts, "test_to": bars[test_end - 1].ts,
                "params": {a.name: combos[best][a.name] for a in axes},
                "in_sample": grid[best]["metrics"], "out_of_sample": oos, "score": grid[best]["score"],
                "best_index": best,
            })
            if f == 0:
                first_grid = grid

    best = fold_results[0]["best_index"]
    neighbor_scores = [first_grid[j]["score"] for j in _neighbors(best, axes)
                       if first_grid[j] is not None and first_grid[j]["score"] is not None]
    neighbors_avg = sum(neighbor_scores) / len(neighbor_scores) if neighbor_scores else None

    warnings = []
    variants = len(valid_idx)
    if variants >= MANY_VARIANTS:
        warnings.append(
            f"Er zijn {variants} varianten getest. Hoe meer varianten, hoe groter de kans dat de beste toevallig "
            "goed scoorde. Kijk vooral naar de out-of-sample-uitslag en naar de buren in de heatmap."
        )
    for a, value in ((a, combos[best][a.name]) for a in axes):
        if len(a.values) > 2 and value in (a.values[0], a.values[-1]):
            warnings.append(f"De beste waarde voor '{a.label}' ligt aan de rand van het bereik. "
                            "Breid het bereik uit om te zien of het daarbuiten nog beter (of slechter) wordt.")
    best_score = fold_results[0]["score"]
    if neighbors_avg is not None and best_score > 0 and neighbors_avg < best_score * 0.5:
        warnings.append("De beste combinatie staat alleen: de combinaties eromheen scoren veel slechter. "
                        "Dat wijst op toeval. Een robuuste strategie scoort ook met iets andere instellingen goed.")
    first = fold_results[0]
    if first["in_sample"]["total_return_pct"] > 0 and first["out_of_sample"]["total_return_pct"] <= 0:
        warnings.append("Winstgevend in de trainingsperiode, maar niet in de vergrendelde out-of-sample-periode.")

    walk_forward = None
    if folds > 1:
        growth = 1.0
        for fr in fold_results:
            growth *= 1 + fr["out_of_sample"]["total_return_pct"] / 100
        walk_forward = {
            "total_return_pct": (growth - 1) * 100,
            "trades": sum(fr["out_of_sample"]["trades"] for fr in fold_results),
            "profitable_windows": sum(1 for fr in fold_results if fr["out_of_sample"]["total_return_pct"] > 0),
            "distinct_params": len({tuple(sorted(fr["params"].items())) for fr in fold_results}),
        }

    return {
        "target": target,
        "target_label": TARGETS[target],
        "min_trades": min_trades,
        "oos_pct": oos_pct,
        "folds": folds,
        "axes": [{"name": a.name, "label": a.label, "values": list(a.values)} for a in axes],
        "fixed": fixed,
        "grid": [
            {
                "params": {a.name: combos[i][a.name] for a in axes},
                "invalid": invalid[i],
                "score": None if first_grid[i] is None else first_grid[i]["score"],
                "metrics": None if first_grid[i] is None else first_grid[i]["metrics"],
            }
            for i in range(len(combos))
        ],
        "best": {
            "params": {**fixed, **fold_results[0]["params"]},
            "score": best_score,
            "neighbors_avg": neighbors_avg,
            "neighbors_count": len(neighbor_scores),
            "in_sample": first["in_sample"],
            "out_of_sample": first["out_of_sample"],
            "split_ts": first["test_from"],
        },
        "windows": [{k: v for k, v in fr.items() if k != "best_index"} for fr in fold_results],
        "walk_forward": walk_forward,
        "variants": variants,
        "warnings": warnings,
    }
