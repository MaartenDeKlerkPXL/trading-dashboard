"""Connecting the dashboard to your cTrader ID (OAuth 2).

1. The dashboard sends you to cTrader, where you log in and allow the "trading" scope.
2. cTrader sends you back to the redirect URL with a one-time code.
3. The dashboard exchanges that code for an access token and a refresh token.

The tokens are stored in data/ctrader_token.json (never on GitHub, never in the logs).
An access token is valid for about 30 days and is renewed automatically before it expires.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

AUTH_URI = "https://openapi.ctrader.com/apps/auth"
TOKEN_URI = "https://openapi.ctrader.com/apps/token"
REFRESH_BEFORE = 3 * 86400


class OAuthError(Exception):
    pass


def auth_url(client_id: str, redirect_url: str) -> str:
    return f"{AUTH_URI}?{urlencode({'client_id': client_id, 'redirect_uri': redirect_url, 'scope': 'trading'})}"


def _normalize(data: dict, now: float) -> dict:
    if data.get("errorCode") or data.get("error"):
        raise OAuthError(str(data.get("description") or data.get("error_description") or data.get("errorCode")
                             or data.get("error")))
    access = data.get("accessToken") or data.get("access_token")
    if not access:
        raise OAuthError("cTrader gaf geen toegangssleutel terug.")
    expires = data.get("expiresIn") or data.get("expires_in") or 30 * 86400
    return {"access_token": access, "refresh_token": data.get("refreshToken") or data.get("refresh_token") or "",
            "expires_at": int(now + int(expires)), "obtained_at": int(now)}


async def _get(params: dict, transport: httpx.AsyncBaseTransport | None) -> dict:
    async with httpx.AsyncClient(timeout=20, transport=transport) as client:
        res = await client.get(TOKEN_URI, params=params, headers={"Accept": "application/json"})
    try:
        return res.json()
    except ValueError:
        raise OAuthError(f"Onverwacht antwoord van cTrader (HTTP {res.status_code}).") from None


async def exchange_code(client_id: str, secret: str, code: str, redirect_url: str,
                        transport: httpx.AsyncBaseTransport | None = None, now=time.time) -> dict:
    data = await _get({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_url,
                       "client_id": client_id, "client_secret": secret}, transport)
    return _normalize(data, now())


async def refresh(client_id: str, secret: str, refresh_token: str,
                  transport: httpx.AsyncBaseTransport | None = None, now=time.time) -> dict:
    data = await _get({"grant_type": "refresh_token", "refresh_token": refresh_token,
                       "client_id": client_id, "client_secret": secret}, transport)
    return _normalize(data, now())


class TokenStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save(self, token: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(token), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
