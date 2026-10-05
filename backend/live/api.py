"""HTTP endpoints for live trading at cTrader: connecting, choosing an account, a demo test order and
starting a live strategy (only after typing the amounts over)."""

from __future__ import annotations

import re
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..backtest.service import make_strategy, strategy_class
from ..broker.ctrader import PRICE_SCALE, BrokerError
from ..config import Settings
from ..data.instruments import INSTRUMENTS, TIMEFRAMES
from ..execution.base import CostModel
from ..execution.events import nl
from ..paper.engine import KillSwitchActive
from .engine import LiveEngine, client_order_id, label
from .link import describe

TEST_SYMBOL = "XAUUSD"
TEST_STOP_DISTANCE = 10.0     # price units (USD for gold) below the fill for the test order


class AccountChoice(BaseModel):
    account_id: int


class LiveStart(BaseModel):
    template_id: int | None = None      # a paper session to copy strategy, parameters, instrument and timeframe from
    strategy: str | None = None
    params: dict = Field(default_factory=dict)
    symbol: str | None = None
    timeframe: str | None = None
    capital: float
    risk_pct: float
    typed: dict | None = None           # {capital, risk_eur, daily_loss_eur} as typed over by you


def parse_amount(text) -> float | None:
    """'1.000,00', '1000', '€ 1.000', '1000.00' → 1000.0"""
    if isinstance(text, (int, float)):
        return float(text)
    s = re.sub(r"[€\s ]", "", str(text or ""))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def fmt_eur(v: float) -> str:
    return "€ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter()

    def live(request: Request) -> LiveEngine:
        return request.app.state.live

    def require_live_mode() -> None:
        if settings.execution.mode != "live":
            raise HTTPException(409, 'Live trading staat uit. Zet in config.toml execution.mode = "live" en herstart '
                                     "het dashboard.")

    async def broker(request: Request):
        try:
            return await live(request).link.ensure()
        except Exception as exc:
            raise HTTPException(502, describe(exc)) from None

    # ---------- connecting (OAuth) ----------

    @router.get("/ctrader/connect")
    async def connect(request: Request):
        link = live(request).link
        if not link.configured:
            return HTMLResponse("<p>Vul eerst CTRADER_CLIENT_ID en CTRADER_CLIENT_SECRET in .env in en herstart het "
                                "dashboard. Zie docs/LIVE_TRADING.md.</p>", status_code=400)
        return RedirectResponse(link.auth_url())

    @router.get("/ctrader/callback")
    async def callback(request: Request, code: str | None = None, error: str | None = None):
        link = live(request).link
        if error or not code:
            return RedirectResponse(f"/#live?fout={error or 'geen-code'}")
        try:
            await link.finish_oauth(code)
            await link.list_accounts()
        except Exception as exc:
            link.last_error = describe(exc)
            return RedirectResponse("/#live?fout=koppelen")
        return RedirectResponse("/#live?gekoppeld=1")

    # ---------- status and account ----------

    @router.get("/api/live/status")
    async def status(request: Request):
        eng = live(request)
        return {
            "mode": settings.execution.mode,
            "enabled": settings.execution.mode == "live",
            "allow_real_money": settings.live.allow_real_money,
            "max_capital": settings.live.max_capital,
            "risk": settings.risk.to_dict(),
            "link": eng.link.status(),
            "loop": eng.status,
            "kill_switch": eng.kill_switch(),
            "sessions": eng.list(),
            "server_time": int(time.time()),
        }

    @router.post("/api/live/accounts")
    async def accounts(request: Request):
        try:
            return await live(request).link.list_accounts()
        except Exception as exc:
            raise HTTPException(502, describe(exc)) from None

    @router.post("/api/live/account")
    async def choose(body: AccountChoice, request: Request):
        eng = live(request)
        if any(s["status"] in ("running", "paused") for s in eng.list()):
            raise HTTPException(409, "Stop eerst alle live-strategieën voordat je van account wisselt.")
        try:
            account = await eng.link.select(body.account_id)
            await eng.link.ensure()
        except Exception as exc:
            raise HTTPException(502, describe(exc)) from None
        return account

    @router.post("/api/live/disconnect")
    async def disconnect(request: Request):
        eng = live(request)
        if any(s["status"] in ("running", "paused") for s in eng.list()):
            raise HTTPException(409, "Stop eerst alle live-strategieën voordat je de koppeling verwijdert.")
        await eng.link.disconnect()
        eng.link.forget()
        return {"ok": True}

    @router.get("/api/live/account")
    async def account(request: Request):
        eng = live(request)
        client, acc = await broker(request)
        info = await eng.link.refresh_account()
        symbols = []
        for symbol, inst in INSTRUMENTS.items():
            try:
                d = await eng.link.symbol(symbol)
                symbols.append({"symbol": symbol, "broker_name": d["name"], "digits": d["digits"],
                                "lot_units": d["lot_size"] / 100, "contract_size": inst.contract_size,
                                "min_lots": d["min_volume"] / 100 / inst.contract_size,
                                "step_lots": d["step_volume"] / 100 / inst.contract_size,
                                "matches": abs(d["lot_size"] / 100 - inst.contract_size) < 1e-9})
            except BrokerError:
                symbols.append({"symbol": symbol, "broker_name": None})
        return {"account": acc, "info": info, "symbols": symbols}

    # ---------- demo test order ----------

    @router.post("/api/live/test-order")
    async def test_order(request: Request):
        """Open and close the smallest gold position on a DEMO account, to prove that orders and the
        broker-side stop-loss work. Refused on real-money accounts."""
        require_live_mode()
        eng = live(request)
        try:
            eng.ensure_trading_allowed()
        except KillSwitchActive as exc:
            raise HTTPException(409, str(exc)) from None
        client, acc = await broker(request)
        if acc["is_live"]:
            raise HTTPException(409, "De testorder kan alleen op een demo-account.")
        steps = []
        try:
            d = await eng.link.symbol(TEST_SYMBOL)
            coid = client_order_id(0, f"test:{int(time.time())}")
            res = await client.market_order(acc["account_id"], d["symbol_id"], "long", d["min_volume"],
                                            label="td-test", comment=coid, client_order_id=coid,
                                            relative_sl=int(TEST_STOP_DISTANCE * PRICE_SCALE))
            if res["status"] != "ORDER_FILLED" or not res["position"]:
                raise HTTPException(502, f"Testorder niet uitgevoerd: {res['error'] or res['status']}")
            pos = res["position"]
            digits = d["digits"]
            steps.append(f"Kooporder uitgevoerd: positie #{pos['position_id']} op {nl(res['price'], digits)}.")
            snapshot = await client.reconcile(acc["account_id"])
            at_broker = next((p for p in snapshot["positions"] if p["position_id"] == pos["position_id"]), None)
            if at_broker and at_broker["stop_loss"] is not None:
                steps.append(f"Stop-loss staat bij de broker op {nl(at_broker['stop_loss'], digits)}.")
            else:
                steps.append("LET OP: de broker meldt geen stop-loss op de testpositie.")
            closed = await client.close_position(acc["account_id"], pos["position_id"], pos["volume"])
            steps.append(f"Positie gesloten op {nl(closed['price'], digits)}." if closed["status"] == "ORDER_FILLED"
                         else f"Sluiten mislukt: {closed['error'] or closed['status']}. Sluit hem in cTrader.")
        except BrokerError as exc:
            raise HTTPException(502, f"{describe(exc)} (stappen tot nu toe: {' '.join(steps) or 'geen'})") from None
        return {"ok": True, "steps": steps}

    # ---------- starting a live strategy ----------

    def _plan(body: LiveStart, request: Request, acc: dict, info: dict) -> dict:
        eng = live(request)
        if body.template_id is not None:
            row = eng.row(body.template_id)
            if row is None:
                raise HTTPException(404, f"Sessie {body.template_id} bestaat niet.")
            tpl = eng.summary(row)
            strategy, params, symbol, timeframe = tpl["strategy"], tpl["params"], tpl["symbol"], tpl["timeframe"]
        else:
            strategy, params, symbol, timeframe = body.strategy, body.params, body.symbol, body.timeframe
        if symbol not in INSTRUMENTS or timeframe not in TIMEFRAMES:
            raise HTTPException(400, "Kies een bekend instrument en een bekende timeframe.")
        cls = strategy_class(strategy or "")
        strat = make_strategy(cls, params)
        risk = settings.risk
        if not 0 < body.risk_pct <= risk.max_risk_per_trade_pct:
            raise HTTPException(400, f"Risico per trade moet tussen 0 en {risk.max_risk_per_trade_pct:g}% liggen.")
        if not 10 <= body.capital <= settings.live.max_capital:
            raise HTTPException(400, f"Kapitaal moet tussen € 10 en {fmt_eur(settings.live.max_capital)} liggen "
                                     "(live.max_capital in config.toml).")
        if body.capital > info["balance"]:
            raise HTTPException(400, f"Het account heeft maar {fmt_eur(info['balance'])}.")
        if info.get("currency") and info["currency"] != "EUR":
            raise HTTPException(409, f"Dit account rekent in {info['currency']}. Het dashboard rekent in euro: "
                                     "kies een EUR-account.")
        if acc["is_live"] and not settings.live.allow_real_money:
            raise HTTPException(409, "Dit is een echt-geld-account. Live trading met echt geld staat uit "
                                     "(live.allow_real_money in config.toml). Test eerst op een demo-account.")
        inst = INSTRUMENTS[symbol]
        return {
            "strategy": cls.key(), "strategy_label": cls.label, "version": cls.version, "params": strat.p,
            "symbol": symbol, "timeframe": timeframe,
            "account": {"account_id": acc["account_id"], "login": acc["login"], "is_live": acc["is_live"],
                        "broker": info.get("broker"), "currency": info.get("currency"), "balance": info["balance"]},
            "capital": round(body.capital, 2),
            "risk_pct": body.risk_pct,
            "risk_eur": round(body.capital * body.risk_pct / 100, 2),
            "daily_loss_eur": round(body.capital * risk.max_daily_loss_pct / 100, 2),
            "max_lots": risk.lots_limit(symbol),
            "costs": {"spread": inst.spread, "slippage": inst.slippage, "commission_per_lot": inst.commission_per_lot,
                      "financing_pct": 6.0},
        }

    @router.post("/api/live/sessions/prepare")
    async def prepare(body: LiveStart, request: Request):
        require_live_mode()
        try:
            live(request).ensure_trading_allowed()
        except KillSwitchActive as exc:
            raise HTTPException(409, str(exc)) from None
        _, acc = await broker(request)
        info = await live(request).link.refresh_account()
        return _plan(body, request, acc, info)

    @router.post("/api/live/sessions")
    async def start(body: LiveStart, request: Request):
        require_live_mode()
        eng = live(request)
        try:
            eng.ensure_trading_allowed()
        except KillSwitchActive as exc:
            raise HTTPException(409, str(exc)) from None
        _, acc = await broker(request)
        info = await eng.link.refresh_account()
        plan = _plan(body, request, acc, info)
        typed = body.typed or {}
        for key, name in (("capital", "het kapitaal"), ("risk_eur", "het risico per trade"),
                          ("daily_loss_eur", "het maximale dagverlies")):
            value = parse_amount(typed.get(key))
            if value is None or abs(value - plan[key]) >= 0.005:
                raise HTTPException(400, f"Typ {name} exact over: {fmt_eur(plan[key])}.")
        try:
            await eng.link.symbol(plan["symbol"])
        except BrokerError as exc:
            raise HTTPException(400, describe(exc)) from None
        CostModel(**plan["costs"]).validate()
        session_id = eng.create({
            "symbol": plan["symbol"], "timeframe": plan["timeframe"], "strategy": plan["strategy"],
            "params": plan["params"], "capital": plan["capital"], "risk_pct": plan["risk_pct"],
            "sizing_mode": "realistic", "leverage": min(settings.account.leverage, INSTRUMENTS[plan["symbol"]].max_leverage),
            "costs": plan["costs"],
        }, broker={**plan["account"], "label": None})
        with eng.conn:
            eng.conn.execute("UPDATE paper_sessions SET broker=json_set(broker, '$.label', ?) WHERE id=?",
                             (label(session_id), session_id))
        request.app.state.keep_awake.update(True)
        return eng.summary(eng.row(session_id))

    @router.get("/api/live/orders")
    async def orders(request: Request, session_id: int | None = None):
        return live(request).orders(session_id)

    return router
