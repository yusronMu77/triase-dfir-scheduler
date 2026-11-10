"""Rakit aplikasi scheduler: komponen standar (PostgreSQL, NATS, relay outbox), loop penjadwal, dan router.

Dev memakai `uvicorn astria_scheduler.main:build_app --factory --reload`; image memakai `astria-scheduler`.
"""

from datetime import timedelta

from astria_common.app import create_app
from astria_common.auth import ServiceTokenClient, ServiceTokenVerifier
from astria_common.http import make_client
from astria_common.service import standard_runtime
from fastapi import FastAPI

from astria_scheduler import api
from astria_scheduler.jadwal import Jam, jam_sistem
from astria_scheduler.orkestrator import Orkestrator
from astria_scheduler.penjadwal import Penjadwal
from astria_scheduler.settings import Settings
from astria_scheduler.store import metadata
from astria_scheduler.ticket import SumberRingkasan, TicketClient


def _ticket(settings: Settings) -> TicketClient:
    key = settings.service_key_bytes()
    if key is None:
        raise RuntimeError("scheduler: ASTRIA_SERVICE_KEY atau _FILE wajib untuk membaca statistik dari ticket")
    tokens = ServiceTokenClient(settings.service_name, key, settings.identity_url)
    return TicketClient(tokens, make_client(settings.scheduler_ticket_url))


def build_app(
    settings: Settings | None = None,
    *,
    token_verifier: ServiceTokenVerifier | None = None,
    sumber: SumberRingkasan | None = None,
    jam: Jam = jam_sistem,
) -> FastAPI:
    settings = settings or Settings()
    runtime = standard_runtime(settings, metadata=[metadata])
    orkestrator = None
    if runtime.db is not None:
        orkestrator = Orkestrator(
            runtime.db,
            sumber or _ticket(settings),
            cron=settings.scheduler_digest_cron,
            toleransi=timedelta(minutes=settings.scheduler_toleransi_menit),
            percobaan_maks=settings.scheduler_percobaan_maks,
            maks_periode=timedelta(hours=settings.scheduler_maks_periode_jam),
            jam=jam,
        )
        runtime.add(
            Penjadwal(
                runtime.db,
                orkestrator,
                ttl=timedelta(seconds=settings.scheduler_lease_ttl_detik),
                perbarui=timedelta(seconds=settings.scheduler_lease_perbarui_detik),
            )
        )
    app = create_app(settings, components=runtime.components, routers=[api.router], token_verifier=token_verifier)
    app.state.orkestrator = orkestrator
    return app
