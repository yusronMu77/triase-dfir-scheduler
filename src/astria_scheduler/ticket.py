"""Sumber data digest dari ticket: `GET /svc/v1/stats?dari=&sampai=` dan `GET /svc/v1/tickets?limit=` (note 1, 5.6).

Bentuk jawaban yang diharapkan (usulan kontrak untuk ticket, lajur 1) ada di README. Ticket yang tidak terjangkau,
menjawab galat, atau menjawab bentuk lain mengembalikan None: digest tetap terbit dengan baris "sumber tidak
terbaca" (note 9).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Protocol

import httpx
from astria_common.errors import AstriaError
from astria_common.log import get_logger
from pydantic import BaseModel, ConfigDict, Field

log = get_logger(__name__)

STATS_PATH = "/svc/v1/stats"
TICKETS_PATH = "/svc/v1/tickets"
BATAS_TIKET = 10


class Statistik(BaseModel):
    model_config = ConfigDict(extra="ignore")
    total: int = Field(ge=0)
    per_status: dict[str, int]
    per_band: dict[str, int]
    sla_terlanggar: int = Field(ge=0)


class Tiket(BaseModel):
    model_config = ConfigDict(extra="ignore")
    card_id: str
    judul: str
    band: str
    status: str


class DaftarTiket(BaseModel):
    model_config = ConfigDict(extra="ignore")
    items: list[Tiket]


class Ringkasan(BaseModel):
    statistik: Statistik
    tiket: list[Tiket]


class TokenSource(Protocol):
    async def auth_headers(self, audience: str, act: dict[str, Any] | None = None) -> dict[str, str]: ...


class SumberRingkasan(Protocol):
    async def ambil(self, dari: datetime, sampai: datetime) -> Ringkasan | None: ...


class TicketClient:
    def __init__(self, tokens: TokenSource, http: httpx.AsyncClient) -> None:
        self._tokens = tokens
        self._http = http

    async def _get(self, path: str, params: dict[str, Any]) -> bytes:
        headers = await self._tokens.auth_headers("svc:ticket")
        resp = await self._http.get(path, params=params, headers=headers)
        resp.raise_for_status()
        return resp.content

    async def ambil(self, dari: datetime, sampai: datetime) -> Ringkasan | None:
        try:
            stats, tiket = await asyncio.gather(
                self._get(STATS_PATH, {"dari": dari.isoformat(), "sampai": sampai.isoformat()}),
                self._get(TICKETS_PATH, {"limit": BATAS_TIKET}),
            )
            return Ringkasan(
                statistik=Statistik.model_validate_json(stats),
                tiket=DaftarTiket.model_validate_json(tiket).items[:BATAS_TIKET],
            )
        except (httpx.HTTPError, AstriaError, ValueError) as exc:  # ValueError mencakup galat validasi pydantic
            log.warning("sumber_tidak_terbaca", sumber="ticket", error=type(exc).__name__)
            return None
