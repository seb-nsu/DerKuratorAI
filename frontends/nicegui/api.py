"""HTTP client of the NiceGUI front end: every call goes through the API contract, with the signed-in user's token.

By default the API runs in the same process and is called over ASGI (no network hop); `API_BASE_URL` points the
front end at a separately hosted API instead (e.g. https://kurator.internal/api/v1)."""
from __future__ import annotations

import base64
import json as jsonlib
import os
import time
from typing import Any

import httpx
from nicegui import app

_asgi_app = None  # set by frontends.nicegui.main when the API runs in this process
RENEW_BEFORE_S = 10 * 60  # a token with less left is renewed by the next call (the timeout acts on inactivity)


class ApiError(Exception):
    """An API answer other than 2xx. `message` is the API's own text, safe to show."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def use_asgi(api_app) -> None:
    global _asgi_app
    _asgi_app = api_app


def _client() -> httpx.AsyncClient:
    base = os.getenv("API_BASE_URL", "").rstrip("/")
    if base:
        return httpx.AsyncClient(base_url=base, timeout=300)
    if _asgi_app is None:
        raise RuntimeError("No API configured: set API_BASE_URL or start through frontends.nicegui.main")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_asgi_app), base_url="http://api/api/v1", timeout=300)


def token() -> str | None:
    return app.storage.user.get("token")


def signed_in() -> bool:
    return bool(token())


def sign_out() -> None:
    """Forgets the token in this browser session (the server already refused it, or it was revoked)."""
    app.storage.user.pop("token", None)
    app.storage.user.pop("user", None)


async def end_session() -> None:
    """Signs out on the server too: the token and every renewal of it are refused from now on."""
    if token():
        try:
            await request("POST", "/auth/sign-out")
        except (ApiError, httpx.HTTPError):
            pass  # already invalid or the API is unreachable: the token is forgotten either way
    sign_out()


def _remaining(current: str) -> float | None:
    """Seconds until the token expires, read from its signed claims (the API checks them again)."""
    body = current.split(".", 1)[0]
    try:
        claims = jsonlib.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return float(claims["exp"]) - time.time()
    except (ValueError, KeyError, TypeError):
        return None


async def _renew_if_due(path: str) -> None:
    current = token()
    left = _remaining(current) if current else None
    if path.startswith("/auth/") or left is None or left <= 0 or left > RENEW_BEFORE_S:
        return
    try:
        answer = await request("POST", "/auth/refresh")
    except ApiError:
        return  # refused (e.g. the maximum session length): the token runs out as before
    app.storage.user["token"] = answer["access_token"]


async def request(method: str, path: str, json: Any = None, params: dict | None = None,
                  anonymous: bool = False) -> Any:
    if not anonymous:
        await _renew_if_due(path)
    headers = {} if anonymous or not token() else {"Authorization": f"Bearer {token()}"}
    async with _client() as client:
        response = await client.request(method, path, json=json, params=params, headers=headers)
    if response.status_code == 401 and not anonymous:
        sign_out()  # expired or revoked: the page frame sends the user to the sign-in
    if response.is_success:
        return response.json() if response.content else None
    try:
        body = response.json()
        detail = body.get("detail") or body.get("message")  # an admin action answers kind + message
    except ValueError:
        detail = None
    if isinstance(detail, list):  # request validation: the first problem in plain words
        detail = "; ".join(str(d.get("msg", d)) for d in detail[:3])
    elif isinstance(detail, dict):  # a refusal with its list of problems: the message already names them
        detail = detail.get("message") or detail
    raise ApiError(response.status_code, str(detail or f"Request failed ({response.status_code})."))


async def get(path: str, **params) -> Any:
    return await request("GET", path, params={k: v for k, v in params.items() if v is not None} or None)


async def post(path: str, body: Any = None) -> Any:
    return await request("POST", path, json=body if body is not None else {})


async def sign_in(user_id: str, password: str) -> dict:
    answer = await request("POST", "/auth/token", json={"user_id": user_id, "password": password}, anonymous=True)
    app.storage.user.update(token=answer["access_token"], user=answer["user"])
    return answer["user"]


async def put(path: str, body: Any = None) -> Any:
    return await request("PUT", path, json=body if body is not None else {})


async def get_text(path: str) -> str:
    """A non-JSON answer (e.g. an HTML report) as text; same errors as `request`."""
    await _renew_if_due(path)
    headers = {"Authorization": f"Bearer {token()}"} if token() else {}
    async with _client() as client:
        response = await client.get(path, headers=headers)
    if response.status_code == 401:
        sign_out()
    if not response.is_success:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        raise ApiError(response.status_code, str(detail or f"Request failed ({response.status_code})."))
    return response.text
