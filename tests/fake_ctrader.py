"""A small stand-in for the cTrader Open API server, speaking the same protobuf protocol.

It keeps positions, orders and deals in memory, fills market orders at the current
price (bid for sells, ask for buys) and can simulate a stop-loss being hit.
Never connects to anything real.
"""

from __future__ import annotations

import asyncio
import contextlib

from backend.broker.ctrader import PRICE_SCALE, decode, encode, read_frame
from backend.broker.ctrader_messages import OpenApiCommonMessages_pb2 as C
from backend.broker.ctrader_messages import OpenApiMessages_pb2 as M
from backend.broker.ctrader_messages import OpenApiModelMessages_pb2 as MM

CLIENT_ID = "test-client"
CLIENT_SECRET = "test-secret"
TOKEN = "test-token"
ACCOUNT = 1001
GOLD_ID = 41
EURUSD_ID = 1


class FakeCTrader:
    def __init__(self, price=lambda symbol_id, ts: 2000.0, now=lambda: 0, spread=0.3, balance=1000.0,
                 is_live=False):
        self.price = price          # bid price for a symbol at a unix time
        self.now = now
        self.spread = spread
        self.balance = balance
        self.is_live = is_live
        self.positions: dict[int, MM.ProtoOAPosition] = {}
        self.deals: list[MM.ProtoOADeal] = []
        self.next_id = 500
        self.received: list = []
        self.reject_next: str | None = None
        self.drop_fill_reply = False   # simulate a crash right after the order was accepted
        self.server = None
        self.writers = []

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for w in self.writers:
            w.close()
        self.server.close()
        with contextlib.suppress(Exception):
            await self.server.wait_closed()

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    # ---------- helpers for tests ----------

    def ask(self, symbol_id: int, ts: int) -> float:
        return self.price(symbol_id, ts) + (self.spread if symbol_id == GOLD_ID else 0.00002)

    def hit_stop(self, position_id: int) -> None:
        """Close a position at its stop-loss, as the broker would."""
        p = self.positions[position_id]
        self._close(p, p.stopLoss, p.tradeData.volume, order_id=self._id())

    def _event(self, kind, order_id: int, trade, client_order_id: str = "", status=MM.ORDER_STATUS_ACCEPTED):
        ev = M.ProtoOAExecutionEvent(ctidTraderAccountId=ACCOUNT, executionType=kind)
        ev.order.orderId = order_id
        ev.order.orderType = MM.MARKET
        ev.order.orderStatus = status
        ev.order.tradeData.symbolId = trade.symbolId
        ev.order.tradeData.volume = trade.volume
        ev.order.tradeData.tradeSide = trade.tradeSide
        if client_order_id:
            ev.order.clientOrderId = client_order_id
        return ev

    def payloads(self, cls) -> list:
        return [m for m in self.received if isinstance(m, cls)]

    # ---------- protocol ----------

    async def _handle(self, reader, writer) -> None:
        self.writers.append(writer)
        try:
            while True:
                msg, cid = decode(await read_frame(reader))
                self.received.append(msg)
                for reply in self._answer(msg):
                    writer.write(encode(reply, cid))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass

    def _error(self, code: str, text: str = ""):
        return [M.ProtoOAErrorRes(errorCode=code, description=text)]

    def _answer(self, msg) -> list:
        t = msg.payloadType
        if t == C.ProtoHeartbeatEvent().payloadType:
            return []
        if isinstance(msg, M.ProtoOAApplicationAuthReq):
            if (msg.clientId, msg.clientSecret) != (CLIENT_ID, CLIENT_SECRET):
                return self._error("CH_CLIENT_AUTH_FAILURE", "bad client")
            return [M.ProtoOAApplicationAuthRes()]
        if isinstance(msg, M.ProtoOAGetAccountListByAccessTokenReq):
            res = M.ProtoOAGetAccountListByAccessTokenRes(accessToken=msg.accessToken)
            res.ctidTraderAccount.add(ctidTraderAccountId=ACCOUNT, isLive=self.is_live, traderLogin=5551234)
            return [res]
        if isinstance(msg, M.ProtoOAAccountAuthReq):
            if msg.accessToken != TOKEN:
                return self._error("CH_ACCESS_TOKEN_INVALID", "bad token")
            return [M.ProtoOAAccountAuthRes(ctidTraderAccountId=msg.ctidTraderAccountId)]
        if isinstance(msg, M.ProtoOATraderReq):
            res = M.ProtoOATraderRes(ctidTraderAccountId=ACCOUNT)
            res.trader.ctidTraderAccountId = ACCOUNT
            res.trader.balance = int(round(self.balance * 100))
            res.trader.moneyDigits = 2
            res.trader.traderLogin = 5551234
            res.trader.brokerName = "BlackBull Markets"
            res.trader.depositAssetId = 3
            res.trader.leverageInCents = 10000
            res.trader.accessRights = 0
            return [res]
        if isinstance(msg, M.ProtoOAAssetListReq):
            res = M.ProtoOAAssetListRes(ctidTraderAccountId=ACCOUNT)
            res.asset.add(assetId=3, name="EUR")
            res.asset.add(assetId=4, name="USD")
            return [res]
        if isinstance(msg, M.ProtoOASymbolsListReq):
            res = M.ProtoOASymbolsListRes(ctidTraderAccountId=ACCOUNT)
            res.symbol.add(symbolId=GOLD_ID, symbolName="XAUUSD", enabled=True)
            res.symbol.add(symbolId=EURUSD_ID, symbolName="EURUSD", enabled=True)
            return [res]
        if isinstance(msg, M.ProtoOASymbolByIdReq):
            res = M.ProtoOASymbolByIdRes(ctidTraderAccountId=ACCOUNT)
            for sid in msg.symbolId:
                gold = sid == GOLD_ID
                res.symbol.add(symbolId=sid, digits=2 if gold else 5, pipPosition=1 if gold else 4,
                               lotSize=10_000 if gold else 10_000_000, minVolume=100 if gold else 100_000,
                               stepVolume=100 if gold else 100_000, maxVolume=10_000_000_00)
            return [res]
        if isinstance(msg, M.ProtoOAGetTrendbarsReq):
            res = M.ProtoOAGetTrendbarsRes(ctidTraderAccountId=ACCOUNT, period=msg.period, symbolId=msg.symbolId,
                                           timestamp=self.now() * 1000)
            start, end = msg.fromTimestamp // 1000, min(msg.toTimestamp // 1000, self.now())
            ts = start - start % 60
            while ts < end:
                prices = [self.price(msg.symbolId, ts + s) for s in (0, 20, 40, 59)]
                low = round(min(prices) * PRICE_SCALE)
                res.trendbar.add(utcTimestampInMinutes=ts // 60, low=low, volume=10, period=msg.period,
                                 deltaOpen=round(prices[0] * PRICE_SCALE) - low,
                                 deltaHigh=round(max(prices) * PRICE_SCALE) - low,
                                 deltaClose=round(prices[-1] * PRICE_SCALE) - low)
                ts += 60
            return [res]
        if isinstance(msg, M.ProtoOAReconcileReq):
            res = M.ProtoOAReconcileRes(ctidTraderAccountId=ACCOUNT)
            for p in self.positions.values():
                res.position.add().CopyFrom(p)
            return [res]
        if isinstance(msg, M.ProtoOANewOrderReq):
            return self._new_order(msg)
        if isinstance(msg, M.ProtoOAAmendPositionSLTPReq):
            p = self.positions.get(msg.positionId)
            if p is None:
                return self._error("POSITION_NOT_FOUND")
            if msg.HasField("stopLoss"):
                p.stopLoss = msg.stopLoss
            if msg.HasField("takeProfit"):
                p.takeProfit = msg.takeProfit
            ev = M.ProtoOAExecutionEvent(ctidTraderAccountId=ACCOUNT, executionType=MM.ORDER_REPLACED)
            ev.position.CopyFrom(p)
            return [ev]
        if isinstance(msg, M.ProtoOAClosePositionReq):
            p = self.positions.get(msg.positionId)
            if p is None:
                return self._error("POSITION_NOT_FOUND")
            side = MM.SELL if p.tradeData.tradeSide == MM.BUY else MM.BUY
            now = self.now()
            price = self.price(p.tradeData.symbolId, now) if side == MM.SELL else self.ask(p.tradeData.symbolId, now)
            order_id = self._id()
            deal = self._close(p, price, msg.volume, order_id)
            accepted = self._event(MM.ORDER_ACCEPTED, order_id, p.tradeData)
            filled = self._event(MM.ORDER_FILLED, order_id, p.tradeData, status=MM.ORDER_STATUS_FILLED)
            filled.position.CopyFrom(p)
            filled.deal.CopyFrom(deal)
            return [accepted, filled]
        if isinstance(msg, M.ProtoOADealListReq):
            res = M.ProtoOADealListRes(ctidTraderAccountId=ACCOUNT, hasMore=False)
            for d in self.deals:
                if msg.fromTimestamp <= d.executionTimestamp <= msg.toTimestamp:
                    res.deal.add().CopyFrom(d)
            return [res]
        if isinstance(msg, M.ProtoOACancelOrderReq):
            return self._error("OA_ORDER_NOT_FOUND")
        return self._error("NOT_SUPPORTED", type(msg).__name__)

    def _new_order(self, msg) -> list:
        if self.reject_next:
            code, self.reject_next = self.reject_next, None
            ev = M.ProtoOAOrderErrorEvent(ctidTraderAccountId=ACCOUNT, errorCode=code, description="rejected")
            return [ev]
        now = self.now()
        long = msg.tradeSide == MM.BUY
        price = self.ask(msg.symbolId, now) if long else self.price(msg.symbolId, now)
        order_id, position_id = self._id(), self._id()
        p = MM.ProtoOAPosition(positionId=position_id, positionStatus=MM.POSITION_STATUS_OPEN, price=price,
                               swap=0, commission=-int(msg.volume / 10_000 * 300), moneyDigits=2,
                               utcLastUpdateTimestamp=now * 1000)
        p.tradeData.symbolId = msg.symbolId
        p.tradeData.volume = msg.volume
        p.tradeData.tradeSide = msg.tradeSide
        p.tradeData.openTimestamp = now * 1000
        p.tradeData.label = msg.label
        p.tradeData.comment = msg.comment
        sign = -1 if long else 1
        if msg.HasField("relativeStopLoss"):
            p.stopLoss = round(price + sign * msg.relativeStopLoss / PRICE_SCALE, 5)
        if msg.HasField("relativeTakeProfit"):
            p.takeProfit = round(price - sign * msg.relativeTakeProfit / PRICE_SCALE, 5)
        self.positions[position_id] = p
        deal = MM.ProtoOADeal(dealId=self._id(), orderId=order_id, positionId=position_id, volume=msg.volume,
                              filledVolume=msg.volume, symbolId=msg.symbolId, createTimestamp=now * 1000,
                              executionTimestamp=now * 1000, executionPrice=price, tradeSide=msg.tradeSide,
                              dealStatus=MM.FILLED, commission=p.commission, moneyDigits=2)
        self.deals.append(deal)
        self.balance += p.commission / 100
        accepted = self._event(MM.ORDER_ACCEPTED, order_id, p.tradeData, msg.clientOrderId)
        if self.drop_fill_reply:
            return [accepted]
        filled = self._event(MM.ORDER_FILLED, order_id, p.tradeData, msg.clientOrderId, MM.ORDER_STATUS_FILLED)
        filled.position.CopyFrom(p)
        filled.deal.CopyFrom(deal)
        return [accepted, filled]

    def _close(self, p, price: float, volume: int, order_id: int):
        now = self.now()
        units = volume / 100
        direction = 1 if p.tradeData.tradeSide == MM.BUY else -1
        gross = (price - p.price) * direction * units          # quote currency; tests use a 1:1 EUR rate
        commission = -int(volume / 10_000 * 300)
        self.balance += gross + commission / 100
        deal = MM.ProtoOADeal(dealId=self._id(), orderId=order_id, positionId=p.positionId, volume=volume,
                              filledVolume=volume, symbolId=p.tradeData.symbolId, createTimestamp=now * 1000,
                              executionTimestamp=now * 1000, executionPrice=price,
                              tradeSide=MM.SELL if direction == 1 else MM.BUY, dealStatus=MM.FILLED,
                              commission=commission, moneyDigits=2)
        deal.closePositionDetail.entryPrice = p.price
        deal.closePositionDetail.grossProfit = int(round(gross * 100))
        deal.closePositionDetail.swap = 0
        deal.closePositionDetail.commission = commission
        deal.closePositionDetail.balance = int(round(self.balance * 100))
        deal.closePositionDetail.closedVolume = volume
        deal.closePositionDetail.moneyDigits = 2
        self.deals.append(deal)
        p.positionStatus = MM.POSITION_STATUS_CLOSED
        del self.positions[p.positionId]
        return deal
