"""The connection with the broker: credentials, tokens, the chosen account and symbol details."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time

from ..broker import oauth
from ..broker.ctrader import HOSTS, BrokerError, CTraderClient
from ..broker.ctrader_messages import OpenApiMessages_pb2 as M
from ..db import get_state, set_state

log = logging.getLogger(__name__)

ERROR_TEXT = {
    "CH_CLIENT_AUTH_FAILURE": "Client ID of Secret klopt niet (zie .env).",
    "CH_CLIENT_NOT_AUTHENTICATED": "De app is niet aangemeld bij cTrader.",
    "CH_ACCESS_TOKEN_INVALID": "De koppeling met cTrader is ongeldig of verlopen. Koppel opnieuw.",
    "OA_AUTH_TOKEN_EXPIRED": "De koppeling met cTrader is verlopen. Koppel opnieuw.",
    "ACCOUNT_NOT_AUTHORIZED": "Dit account is niet gekoppeld aan deze app. Koppel opnieuw en kies het account.",
    "ALREADY_LOGGED_IN": "Dit account is al aangemeld.",
    "NOT_ENOUGH_MONEY": "Niet genoeg vrije marge op het account.",
    "MARKET_CLOSED": "De markt is gesloten.",
    "TRADING_DISABLED": "Handelen is uitgeschakeld voor dit instrument of account.",
    "TIMEOUT": "cTrader antwoordt niet.",
    "DISCONNECTED": "De verbinding met cTrader is verbroken.",
    "NO_TOKEN": "Nog niet gekoppeld met cTrader.",
    "NOT_CONFIGURED": "CTRADER_CLIENT_ID en CTRADER_CLIENT_SECRET ontbreken in .env.",
    "NO_ACCOUNT": "Nog geen cTrader-account gekozen.",
}
DROP_EVENTS = {M.ProtoOAAccountsTokenInvalidatedEvent().payloadType, M.ProtoOAClientDisconnectEvent().payloadType,
               M.ProtoOAAccountDisconnectEvent().payloadType}


def describe(exc: Exception) -> str:
    if isinstance(exc, BrokerError):
        text = ERROR_TEXT.get(exc.code)
        if text:
            return text
        return f"cTrader: {exc.code}" + (f" ({exc.description})" if exc.description else "")
    if isinstance(exc, oauth.OAuthError):
        return f"Koppelen mislukt: {exc}"
    return f"Verbinding met cTrader mislukt ({type(exc).__name__})."


def _key(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", name.upper())


class BrokerLink:
    def __init__(self, conn, settings, env: dict, token_store: oauth.TokenStore, client_factory=None,
                 oauth_transport=None, now=time.time):
        self.conn = conn
        self.settings = settings
        self.client_id = env.get("CTRADER_CLIENT_ID", "").strip()
        self.secret = env.get("CTRADER_CLIENT_SECRET", "").strip()
        self.env_token = env.get("CTRADER_ACCESS_TOKEN", "").strip()
        self.env_account = env.get("CTRADER_ACCOUNT_ID", "").strip()
        self.tokens = token_store
        self.client_factory = client_factory or (lambda is_live: CTraderClient(HOSTS["live" if is_live else "demo"]))
        self.oauth_transport = oauth_transport
        self.now = now
        self.client: CTraderClient | None = None
        self.client_is_live: bool | None = None
        self.app_authed = False
        self.authed_account: int | None = None
        self.account_info: dict | None = None
        self.symbol_cache: dict[int, dict] = {}
        self.details_cache: dict[tuple[int, int], dict] = {}
        self.lock = asyncio.Lock()
        self.last_error: str | None = None
        self.last_ok: int | None = None

    # ---------- configuration and tokens ----------

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.secret)

    def auth_url(self) -> str:
        return oauth.auth_url(self.client_id, self.settings.live.redirect_url)

    async def finish_oauth(self, code: str) -> None:
        token = await oauth.exchange_code(self.client_id, self.secret, code, self.settings.live.redirect_url,
                                          self.oauth_transport, self.now)
        self.tokens.save(token)
        set_state(self.conn, "broker_accounts", None)
        await self.disconnect()

    def token(self) -> dict | None:
        stored = self.tokens.load()
        if stored:
            return stored
        if self.env_token:
            return {"access_token": self.env_token, "refresh_token": "", "expires_at": None}
        return None

    async def access_token(self) -> str:
        token = self.token()
        if not token:
            raise BrokerError("NO_TOKEN")
        expires = token.get("expires_at")
        if expires and token.get("refresh_token") and expires - self.now() < oauth.REFRESH_BEFORE:
            token = await oauth.refresh(self.client_id, self.secret, token["refresh_token"], self.oauth_transport,
                                        self.now)
            self.tokens.save(token)
            log.info("cTrader access token renewed")
        return token["access_token"]

    def forget(self) -> None:
        self.tokens.clear()
        set_state(self.conn, "broker_account", None)
        set_state(self.conn, "broker_accounts", None)

    # ---------- account ----------

    def selected(self) -> dict | None:
        chosen = get_state(self.conn, "broker_account")
        if chosen:
            return chosen
        if self.env_account.isdigit():
            known = {a["account_id"]: a for a in (get_state(self.conn, "broker_accounts") or [])}
            account = known.get(int(self.env_account))
            if account:
                return account
        return None

    async def _client(self, is_live: bool) -> CTraderClient:
        if self.client and self.client.connected and self.client_is_live == is_live:
            return self.client
        await self._drop()
        if not self.configured:
            raise BrokerError("NOT_CONFIGURED")
        client = self.client_factory(is_live)
        client.on_event = self._on_event
        await client.connect()
        self.client, self.client_is_live = client, is_live
        await client.app_auth(self.client_id, self.secret)
        self.app_authed = True
        return client

    def _on_event(self, msg) -> None:
        if msg.payloadType in DROP_EVENTS:
            log.warning("cTrader ended the session (%s)", type(msg).__name__)
            self.authed_account = None
            if self.client:
                self.client.closed_reason = "cTrader beëindigde de sessie"

    async def _drop(self) -> None:
        if self.client:
            with contextlib.suppress(Exception):
                await self.client.close()
        self.client, self.client_is_live, self.app_authed, self.authed_account = None, None, False, None

    async def disconnect(self) -> None:
        async with self.lock:
            await self._drop()

    async def list_accounts(self) -> list[dict]:
        async with self.lock:
            try:
                client = await self._client(self.client_is_live if self.client_is_live is not None else False)
                accounts = await client.accounts(await self.access_token())
            except Exception as exc:
                self._failed(exc)
                raise
        set_state(self.conn, "broker_accounts", accounts)
        self._ok()
        return accounts

    async def select(self, account_id: int) -> dict:
        accounts = get_state(self.conn, "broker_accounts") or await self.list_accounts()
        account = next((a for a in accounts if a["account_id"] == account_id), None)
        if account is None:
            raise BrokerError("ACCOUNT_NOT_AUTHORIZED")
        set_state(self.conn, "broker_account", account)
        await self.disconnect()
        return account

    async def ensure(self) -> tuple[CTraderClient, dict]:
        """A connected client, logged in to the chosen account."""
        async with self.lock:
            account = self.selected()
            if account is None:
                raise BrokerError("NO_ACCOUNT")
            try:
                client = await self._client(account["is_live"])
                if self.authed_account != account["account_id"]:
                    await client.account_auth(account["account_id"], await self.access_token())
                    self.authed_account = account["account_id"]
                    info = await client.trader(account["account_id"])
                    info["currency"] = await client.deposit_currency(account["account_id"], info["deposit_asset_id"])
                    self.account_info = info
            except Exception as exc:
                self._failed(exc)
                raise
            self._ok()
            return client, account

    async def refresh_account(self) -> dict:
        client, account = await self.ensure()
        info = await client.trader(account["account_id"])
        info["currency"] = (self.account_info or {}).get("currency", "")
        self.account_info = info
        return info

    def _ok(self) -> None:
        self.last_error = None
        self.last_ok = int(self.now())

    def _failed(self, exc: Exception) -> None:
        self.last_error = describe(exc)
        log.warning("cTrader: %s", self.last_error)

    # ---------- symbols ----------

    async def symbol(self, symbol: str) -> dict:
        """Broker details for one of our instruments: symbol id, lot size and volume steps."""
        client, account = await self.ensure()
        acc = account["account_id"]
        if acc not in self.symbol_cache:
            self.symbol_cache[acc] = {s["name"]: s for s in await client.symbols(acc)}
        by_name = self.symbol_cache[acc]
        wanted = _key(symbol)
        match = next((s for name, s in by_name.items() if _key(name) == wanted), None)
        if match is None:
            match = next((s for name, s in sorted(by_name.items()) if _key(name).startswith(wanted)), None)
        if match is None:
            raise BrokerError("SYMBOL_NOT_FOUND", f"{symbol} staat niet op dit account")
        key = (acc, match["symbol_id"])
        if key not in self.details_cache:
            details = await client.symbol_details(acc, [match["symbol_id"]])
            self.details_cache[key] = {**details[match["symbol_id"]], "name": match["name"]}
        return self.details_cache[key]

    # ---------- status for the dashboard ----------

    def status(self) -> dict:
        token = self.token()
        account = self.selected()
        return {
            "configured": self.configured,
            "linked": token is not None,
            "token_expires_at": token.get("expires_at") if token else None,
            "account": account,
            "accounts": get_state(self.conn, "broker_accounts") or [],
            "connected": bool(self.client and self.client.connected and self.authed_account),
            "account_info": self.account_info if account and self.authed_account == account["account_id"] else None,
            "last_error": self.last_error,
            "last_ok": self.last_ok,
            "redirect_url": self.settings.live.redirect_url,
        }
