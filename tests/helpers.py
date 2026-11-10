"""Tiruan ticket (httpx.MockTransport), token, dan jam untuk uji. Tidak ada panggilan jaringan sungguhan."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx

from astria_scheduler.jadwal import WIB
from astria_scheduler.ticket import TicketClient

FIXTURES = Path(__file__).parent / "fixtures"
TICKET_FIXTURES = {"/svc/v1/stats": "ticket_stats.json", "/svc/v1/tickets": "ticket_tickets.json"}


def wib(*args: int) -> datetime:
    return datetime(*args, tzinfo=WIB)


class JamUji:
    def __init__(self, waktu: datetime) -> None:
        self.waktu = waktu

    def __call__(self) -> datetime:
        return self.waktu


class FakeTokens:
    async def auth_headers(self, audience: str, act: dict | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer uji-{audience}"}


def _client(handler) -> TicketClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://ticket.invalid")
    return TicketClient(FakeTokens(), http)


def ticket_ok(calls: list[httpx.Request] | None = None) -> TicketClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        body = (FIXTURES / TICKET_FIXTURES[request.url.path]).read_bytes()
        return httpx.Response(200, content=body, headers={"Content-Type": "application/json"})

    return _client(handler)


def ticket_down() -> TicketClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("ticket tidak terjangkau", request=request)

    return _client(handler)


def ticket_status(code: int, body: bytes = b"{}") -> TicketClient:
    return _client(lambda request: httpx.Response(code, content=body))
