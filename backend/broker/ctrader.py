"""Client for the cTrader Open API (Spotware), used to trade at BlackBull Markets.

Protocol: TLS over TCP, every message is a 4-byte big-endian length followed by a
ProtoMessage {payloadType, payload, clientMsgId}. Responses carry the clientMsgId of
the request. Prices in trend bars and relative stop-loss/take-profit are integers in
1/100000 of a price unit; volumes are in 1/100 of a unit ("cents").
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
import time
import uuid
from collections.abc import Callable

from ..data.providers import Candle
from .ctrader_messages import OpenApiCommonMessages_pb2 as C
from .ctrader_messages import OpenApiMessages_pb2 as M
from .ctrader_messages import OpenApiModelMessages_pb2 as MM

log = logging.getLogger(__name__)

HOSTS = {"demo": "demo.ctraderapi.com", "live": "live.ctraderapi.com"}
PORT = 5035
PRICE_SCALE = 100_000
HEARTBEAT_SECONDS = 10
HISTORY_SPACING = 0.25          # historical requests: at most 5 per second
MESSAGE_SPACING = 0.03          # other requests: well below 50 per second

HEARTBEAT = C.ProtoHeartbeatEvent().payloadType
ERROR_TYPES = {C.ProtoErrorRes().payloadType, M.ProtoOAErrorRes().payloadType, M.ProtoOAOrderErrorEvent().payloadType}
EXECUTION_EVENT = M.ProtoOAExecutionEvent().payloadType
EXEC = {v.name: v.number for v in MM.DESCRIPTOR.enum_types_by_name["ProtoOAExecutionType"].values}
EXEC_NAMES = {v: k for k, v in EXEC.items()}
BUY, SELL = MM.BUY, MM.SELL
MARKET = MM.MARKET
M1 = MM.M1


def _registry() -> dict[int, type]:
    reg = {}
    for module in (M, C):
        for cls in vars(module).values():
            if isinstance(cls, type) and hasattr(cls, "DESCRIPTOR"):
                field = cls.DESCRIPTOR.fields_by_name.get("payloadType")
                if field is not None and field.default_value:
                    reg[field.default_value] = cls
    return reg


MESSAGES = _registry()


class BrokerError(Exception):
    def __init__(self, code: str, description: str = ""):
        super().__init__(f"{code}: {description}" if description else code)
        self.code = code
        self.description = description


def encode(message, client_msg_id: str | None = None) -> bytes:
    wrapper = C.ProtoMessage(payloadType=message.payloadType, payload=message.SerializeToString())
    if client_msg_id:
        wrapper.clientMsgId = client_msg_id
    data = wrapper.SerializeToString()
    return len(data).to_bytes(4, "big") + data


def decode(data: bytes):
    """Returns (message, clientMsgId). Unknown payload types come back as the raw ProtoMessage."""
    wrapper = C.ProtoMessage()
    wrapper.ParseFromString(data)
    cls = MESSAGES.get(wrapper.payloadType)
    if cls is None:
        return wrapper, wrapper.clientMsgId
    msg = cls()
    msg.ParseFromString(wrapper.payload)
    return msg, wrapper.clientMsgId


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    size = int.from_bytes(await reader.readexactly(4), "big")
    if size > 15_000_000:
        raise BrokerError("FRAME_TOO_LARGE", f"{size} bytes")
    return await reader.readexactly(size)


def _error(msg) -> BrokerError:
    return BrokerError(getattr(msg, "errorCode", "ERROR") or "ERROR", getattr(msg, "description", "") or "")


def _opt(msg, name):
    return getattr(msg, name) if msg.HasField(name) else None


def _money(value: int, digits: int) -> float:
    return value / 10 ** digits


# ---------- conversions to plain dicts ----------

def position_dict(p) -> dict:
    td = p.tradeData
    digits = p.moneyDigits or 2
    return {
        "position_id": p.positionId,
        "symbol_id": td.symbolId,
        "volume": td.volume,
        "side": "long" if td.tradeSide == BUY else "short",
        "open_ts": td.openTimestamp // 1000,
        "label": td.label,
        "comment": td.comment,
        "price": p.price,
        "stop_loss": _opt(p, "stopLoss"),
        "take_profit": _opt(p, "takeProfit"),
        "swap": _money(p.swap, digits),
        "commission": _money(p.commission, digits),
        "status": p.positionStatus,
    }


def order_dict(o) -> dict:
    td = o.tradeData
    return {
        "order_id": o.orderId,
        "symbol_id": td.symbolId,
        "volume": td.volume,
        "side": "long" if td.tradeSide == BUY else "short",
        "label": td.label,
        "comment": td.comment,
        "client_order_id": o.clientOrderId,
        "order_type": o.orderType,
        "status": o.orderStatus,
        "position_id": o.positionId or None,
        "closing": o.closingOrder,
    }


def deal_dict(d) -> dict:
    digits = d.moneyDigits or 2
    close = None
    if d.HasField("closePositionDetail"):
        c = d.closePositionDetail
        cd = c.moneyDigits or digits
        close = {"entry_price": c.entryPrice, "gross": _money(c.grossProfit, cd), "swap": _money(c.swap, cd),
                 "commission": _money(c.commission, cd), "balance": _money(c.balance, cd),
                 "closed_volume": c.closedVolume}
    return {
        "deal_id": d.dealId, "order_id": d.orderId, "position_id": d.positionId, "volume": d.volume,
        "filled_volume": d.filledVolume, "symbol_id": d.symbolId, "ts": d.executionTimestamp // 1000,
        "price": d.executionPrice, "side": "long" if d.tradeSide == BUY else "short", "status": d.dealStatus,
        "commission": _money(d.commission, digits), "close": close,
    }


def trendbar_candle(tb) -> Candle:
    low = tb.low
    return Candle(tb.utcTimestampInMinutes * 60, (low + tb.deltaOpen) / PRICE_SCALE, (low + tb.deltaHigh) / PRICE_SCALE,
                  low / PRICE_SCALE, (low + tb.deltaClose) / PRICE_SCALE, float(tb.volume))


class CTraderClient:
    def __init__(self, host: str, port: int = PORT, use_ssl: bool = True, timeout: float = 15.0,
                 on_event: Callable | None = None, history_spacing: float = HISTORY_SPACING):
        self.host = host
        self.history_spacing = history_spacing
        self.port = port
        self.use_ssl = use_ssl
        self.timeout = timeout
        self.on_event = on_event
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.pending: dict[str, dict] = {}
        self.tasks: list[asyncio.Task] = []
        self.last_sent = 0.0
        self.last_history = 0.0
        self.send_lock = asyncio.Lock()
        self.closed_reason: str | None = None

    # ---------- connection ----------

    @property
    def connected(self) -> bool:
        return self.writer is not None and not self.writer.is_closing() and self.closed_reason is None

    async def connect(self) -> None:
        context = ssl.create_default_context() if self.use_ssl else None
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, ssl=context), self.timeout)
        self.closed_reason = None
        self.tasks = [asyncio.create_task(self._read_loop()), asyncio.create_task(self._heartbeat_loop())]

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.tasks = []
        if self.writer is not None:
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()
        self._fail_pending("verbinding gesloten")
        self.closed_reason = self.closed_reason or "gesloten"

    def _fail_pending(self, reason: str) -> None:
        for entry in self.pending.values():
            if not entry["future"].done():
                entry["future"].set_exception(BrokerError("DISCONNECTED", reason))
        self.pending.clear()

    async def _read_loop(self) -> None:
        try:
            while True:
                msg, cid = decode(await read_frame(self.reader))
                if msg.payloadType == HEARTBEAT:
                    continue
                entry = self.pending.get(cid) if cid else None
                if entry is None:
                    if self.on_event:
                        try:
                            self.on_event(msg)
                        except Exception:
                            log.exception("cTrader event handler failed")
                    continue
                future = entry["future"]
                if future.done():
                    continue
                if msg.payloadType in ERROR_TYPES:
                    future.set_exception(_error(msg))
                    continue
                entry["messages"].append(msg)
                until = entry["until"]
                if until is None:
                    future.set_result(msg)
                elif until(msg):
                    future.set_result(entry["messages"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.closed_reason = f"{type(exc).__name__}: {exc}"
            log.warning("cTrader connection lost: %s", self.closed_reason)
            self._fail_pending("verbinding verbroken")

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS / 2)
            if time.monotonic() - self.last_sent >= HEARTBEAT_SECONDS and self.connected:
                with contextlib.suppress(Exception):
                    await self._write(encode(C.ProtoHeartbeatEvent()))

    async def _write(self, data: bytes, history: bool = False) -> None:
        async with self.send_lock:
            now = time.monotonic()
            wait = max(self.last_sent + MESSAGE_SPACING - now,
                       (self.last_history + self.history_spacing - now) if history else 0.0)
            if wait > 0:
                await asyncio.sleep(wait)
            self.writer.write(data)
            await self.writer.drain()
            self.last_sent = time.monotonic()
            if history:
                self.last_history = self.last_sent

    async def request(self, message, until: Callable | None = None, timeout: float | None = None,
                      history: bool = False):
        """Send and wait for the answer with the same clientMsgId.

        until: keep collecting answers until until(msg) is true; then a list is returned."""
        if not self.connected:
            raise BrokerError("DISCONNECTED", self.closed_reason or "niet verbonden")
        cid = uuid.uuid4().hex[:16]
        future = asyncio.get_running_loop().create_future()
        self.pending[cid] = {"future": future, "until": until, "messages": []}
        try:
            await self._write(encode(message, cid), history)
            return await asyncio.wait_for(future, timeout or self.timeout)
        except asyncio.TimeoutError:
            raise BrokerError("TIMEOUT", "geen antwoord van cTrader") from None
        finally:
            self.pending.pop(cid, None)

    # ---------- authentication and account ----------

    async def app_auth(self, client_id: str, client_secret: str) -> None:
        await self.request(M.ProtoOAApplicationAuthReq(clientId=client_id, clientSecret=client_secret))

    async def account_auth(self, account_id: int, access_token: str) -> None:
        await self.request(M.ProtoOAAccountAuthReq(ctidTraderAccountId=account_id, accessToken=access_token))

    async def accounts(self, access_token: str) -> list[dict]:
        res = await self.request(M.ProtoOAGetAccountListByAccessTokenReq(accessToken=access_token))
        return [{"account_id": a.ctidTraderAccountId, "is_live": a.isLive, "login": a.traderLogin}
                for a in res.ctidTraderAccount]

    async def trader(self, account_id: int) -> dict:
        t = (await self.request(M.ProtoOATraderReq(ctidTraderAccountId=account_id))).trader
        digits = t.moneyDigits or 2
        return {
            "account_id": t.ctidTraderAccountId, "balance": _money(t.balance, digits), "login": t.traderLogin,
            "broker": t.brokerName, "leverage": t.leverageInCents / 100 if t.leverageInCents else None,
            "access_rights": t.accessRights, "deposit_asset_id": t.depositAssetId, "money_digits": digits,
        }

    async def symbols(self, account_id: int) -> list[dict]:
        res = await self.request(M.ProtoOASymbolsListReq(ctidTraderAccountId=account_id))
        return [{"symbol_id": s.symbolId, "name": s.symbolName, "enabled": s.enabled} for s in res.symbol]

    async def symbol_details(self, account_id: int, symbol_ids: list[int]) -> dict[int, dict]:
        res = await self.request(M.ProtoOASymbolByIdReq(ctidTraderAccountId=account_id, symbolId=symbol_ids))
        return {s.symbolId: {"symbol_id": s.symbolId, "digits": s.digits, "pip_position": s.pipPosition,
                             "lot_size": s.lotSize, "min_volume": s.minVolume, "step_volume": s.stepVolume,
                             "max_volume": s.maxVolume, "trading_mode": s.tradingMode,
                             "sl_distance": s.slDistance, "tp_distance": s.tpDistance}
                for s in res.symbol}

    async def deposit_currency(self, account_id: int, asset_id: int) -> str:
        res = await self.request(M.ProtoOAAssetListReq(ctidTraderAccountId=account_id))
        return next((a.name for a in res.asset if a.assetId == asset_id), "")

    # ---------- market data ----------

    async def trendbars(self, account_id: int, symbol_id: int, start: int, end: int, period: int = M1) -> list[Candle]:
        """Bid candles from start to end (unix seconds)."""
        res = await self.request(M.ProtoOAGetTrendbarsReq(
            ctidTraderAccountId=account_id, symbolId=symbol_id, period=period,
            fromTimestamp=start * 1000, toTimestamp=end * 1000), history=True)
        return sorted((trendbar_candle(tb) for tb in res.trendbar), key=lambda c: c.ts)

    # ---------- trading ----------

    async def reconcile(self, account_id: int) -> dict:
        res = await self.request(M.ProtoOAReconcileReq(ctidTraderAccountId=account_id))
        return {"positions": [position_dict(p) for p in res.position], "orders": [order_dict(o) for o in res.order]}

    async def market_order(self, account_id: int, symbol_id: int, side: str, volume: int, *, label: str,
                           comment: str, client_order_id: str, relative_sl: int | None = None,
                           relative_tp: int | None = None, position_id: int | None = None) -> dict:
        """Send a market order and wait until it is filled or refused.

        relative_sl/tp: distance from the fill price in 1/100000 of a price unit, so the
        stop-loss sits at the broker from the very first moment."""
        req = M.ProtoOANewOrderReq(ctidTraderAccountId=account_id, symbolId=symbol_id, orderType=MARKET,
                                   tradeSide=BUY if side == "long" else SELL, volume=int(volume), label=label,
                                   comment=comment, clientOrderId=client_order_id)
        if relative_sl:
            req.relativeStopLoss = int(relative_sl)
        if relative_tp:
            req.relativeTakeProfit = int(relative_tp)
        if position_id:
            req.positionId = position_id
        final = {EXEC["ORDER_FILLED"], EXEC["ORDER_REJECTED"], EXEC["ORDER_CANCELLED"], EXEC["ORDER_EXPIRED"]}
        events = await self.request(req, until=lambda m: m.payloadType == EXECUTION_EVENT and m.executionType in final)
        return execution_summary(events)

    async def amend_sltp(self, account_id: int, position_id: int, stop_loss: float | None,
                         take_profit: float | None) -> None:
        req = M.ProtoOAAmendPositionSLTPReq(ctidTraderAccountId=account_id, positionId=position_id)
        if stop_loss is not None:
            req.stopLoss = stop_loss
        if take_profit is not None:
            req.takeProfit = take_profit
        await self.request(req)

    async def close_position(self, account_id: int, position_id: int, volume: int) -> dict:
        final = {EXEC["ORDER_FILLED"], EXEC["ORDER_REJECTED"], EXEC["ORDER_CANCELLED"]}
        events = await self.request(
            M.ProtoOAClosePositionReq(ctidTraderAccountId=account_id, positionId=position_id, volume=int(volume)),
            until=lambda m: m.payloadType == EXECUTION_EVENT and m.executionType in final)
        return execution_summary(events)

    async def cancel_order(self, account_id: int, order_id: int) -> None:
        await self.request(M.ProtoOACancelOrderReq(ctidTraderAccountId=account_id, orderId=order_id))

    async def deals(self, account_id: int, start: int, end: int) -> list[dict]:
        res = await self.request(M.ProtoOADealListReq(ctidTraderAccountId=account_id, fromTimestamp=start * 1000,
                                                      toTimestamp=end * 1000), history=True)
        return [deal_dict(d) for d in res.deal]


def execution_summary(events: list) -> dict:
    last = events[-1]
    out = {"status": EXEC_NAMES.get(last.executionType, str(last.executionType)), "error": last.errorCode or None,
           "position": None, "order_id": None, "price": None, "deal": None}
    for e in events:
        if e.HasField("order"):
            out["order_id"] = e.order.orderId
        if e.HasField("position"):
            out["position"] = position_dict(e.position)
        if e.HasField("deal"):
            out["deal"] = deal_dict(e.deal)
            out["price"] = e.deal.executionPrice
    return out
