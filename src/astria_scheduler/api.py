"""API scheduler (`/svc/v1`, note app-scheduler 3.1). Semua endpoint wajib token layanan audiens `svc:scheduler`
dari `svc:api`; peran pengguna sudah diperiksa api (izin `digests:read` dan `digests:run`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from astria_common.audit_client import audit_event
from astria_common.auth import ServicePrincipal, require_service
from astria_common.db import Database
from astria_common.errors import AstriaError
from astria_common.outbox import enqueue
from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncEngine

from astria_scheduler.jadwal import WIB, format_wib
from astria_scheduler.orkestrator import Orkestrator
from astria_scheduler.store import METADATA_DIGEST, digest

router = APIRouter(prefix="/svc/v1", tags=["scheduler"])

DIGEST_ID = r"^DG-[0-9]{8}-[0-9]{4}$"
# Dokumen HTML tersimpan tidak boleh memuat skrip atau sumber luar walau dibuka langsung.
CSP = "default-src 'none'; style-src 'unsafe-inline'"

Caller = Annotated[ServicePrincipal, Depends(require_service("svc:scheduler", {"svc:api"}))]
DigestId = Annotated[str, Path(pattern=DIGEST_ID)]


def _engine(request: Request) -> AsyncEngine:
    db: Database | None = request.app.state.components.get("postgres")
    if db is None or not db.is_ready:
        raise AstriaError("SCHEDULER_TIDAK_SIAP", "basis data scheduler belum siap", 503)
    return db.engine


def _iso(waktu: datetime | None) -> str | None:
    return waktu.astimezone(WIB).isoformat() if waktu is not None else None


def _ringkas(baris: Row) -> dict[str, Any]:
    return {
        "digest_id": baris.digest_id,
        "jenis": baris.jenis,
        "periode": f"{format_wib(baris.periode_mulai)} sampai {format_wib(baris.periode_selesai)}",
        "periode_mulai": _iso(baris.periode_mulai),
        "periode_selesai": _iso(baris.periode_selesai),
        "status": baris.status,
        "terlambat": baris.terlambat,
        "tautan": f"/digests/{baris.digest_id}",
    }


async def _baris(engine: AsyncEngine, digest_id: str, *, dengan_html: bool = False) -> Row:
    kolom = [*METADATA_DIGEST, digest.c.html] if dengan_html else METADATA_DIGEST
    async with engine.connect() as conn:
        baris = (await conn.execute(select(*kolom).where(digest.c.digest_id == digest_id))).first()
    if baris is None:
        raise AstriaError("DIGEST_TIDAK_DITEMUKAN", "digest tidak ditemukan", 404)
    return baris


class PermintaanJalan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jenis: Literal["harian"] = "harian"
    periode_mulai: AwareDatetime | None = None
    periode_selesai: AwareDatetime | None = None
    alasan: str | None = Field(None, max_length=500)


@router.post("/digests/run")
async def run_digest(request: Request, _caller: Caller, body: PermintaanJalan | None = None) -> JSONResponse:
    """202 untuk periode baru (atau periode terlewat yang dijalankan ulang), 200 dengan ID sama bila sudah ada."""
    _engine(request)
    orkestrator: Orkestrator = request.app.state.orkestrator  # ada bila basis data ada (main.build_app)
    body = body or PermintaanJalan()
    baris, baru = await orkestrator.minta_manual(body.periode_mulai, body.periode_selesai)
    if baru and (penjadwal := request.app.state.components.get("penjadwal")) is not None:
        penjadwal.wake()
    hasil = {"digest_id": baris.digest_id, "status": baris.status, "terlambat": baris.terlambat}
    return JSONResponse(hasil, status_code=202 if baru else 200)


@router.get("/digests")
async def list_digests(
    request: Request,
    _caller: Caller,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(pattern=DIGEST_ID)] = None,
) -> dict[str, Any]:
    """Terbaru dulu. `cursor` = `digest_id` terakhir halaman sebelumnya (ID terurut menurut waktu)."""
    engine = _engine(request)
    query = select(*METADATA_DIGEST).order_by(digest.c.digest_id.desc()).limit(limit + 1)
    if cursor is not None:
        query = query.where(digest.c.digest_id < cursor)
    async with engine.connect() as conn:
        rows = (await conn.execute(query)).all()
    items = [_ringkas(r) for r in rows[:limit]]
    return {"items": items, "next_cursor": items[-1]["digest_id"] if len(rows) > limit else None}


@router.get("/digests/{digest_id}")
async def get_digest(digest_id: DigestId, request: Request, _caller: Caller) -> dict[str, Any]:
    engine = _engine(request)
    baris = await _baris(engine, digest_id)
    return _ringkas(baris) | {
        "terlambat_menit": baris.terlambat_menit,
        "dijadwalkan_pada": _iso(baris.dijadwalkan_pada),
        "dijalankan_pada": _iso(baris.dijalankan_pada),
        "percobaan": baris.percobaan,
        "mode_narasi": baris.mode_narasi,
        "sha256": baris.sha256,
        "versi_templat": baris.versi_templat,
    }


@router.get("/digests/{digest_id}/dokumen")
async def get_dokumen(
    digest_id: DigestId,
    request: Request,
    caller: Caller,
    fmt: Annotated[str, Query(alias="format")] = "html",
) -> HTMLResponse:
    """HTML digest yang terbit. PDF belum dibuat (ditunda); unduhan dicatat ke audit lewat outbox."""
    if fmt != "html":
        raise AstriaError("FORMAT_BELUM_DIDUKUNG", "format dokumen yang tersedia hanya html", 422)
    engine = _engine(request)
    baris = await _baris(engine, digest_id, dengan_html=True)
    if baris.status != "terbit":
        raise AstriaError("DIGEST_BELUM_TERBIT", "dokumen digest belum terbit", 409)
    pelaku = (caller.act or {}).get("sub") or caller.subject
    async with engine.begin() as conn:
        await enqueue(
            conn,
            audit_event(
                {
                    "idempotency_key": f"scheduler:digest.download:{digest_id}:{uuid.uuid4().hex}",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "actor": pelaku,
                    "actor_type": "user" if caller.act else "service",
                    "action": "digest.download",
                    "target_type": "digest",
                    "target_id": digest_id,
                    "via": "api",
                    "payload": {"format": "html", "sha256": baris.sha256},
                },
                correlation={"digest_id": digest_id},
            ),
        )
    headers = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "X-Astria-Sha256": baris.sha256}
    return HTMLResponse(baris.html, headers=headers)
