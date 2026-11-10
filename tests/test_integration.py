"""Uji integrasi dengan PostgreSQL sungguhan (`ASTRIA_TEST_DATABASE_URL`, tersedia di kontainer py devenv)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import time
import uuid
from datetime import timedelta

import orjson
import pytest
from astria_common import outbox
from astria_common.auth import JwksSource, ServiceTokenVerifier, issue_service_token, load_or_create_signing_key
from astria_common.db import Database
from astria_common.errors import AstriaError
from astria_common.events import decode
from fastapi.testclient import TestClient
from helpers import JamUji, ticket_down, ticket_ok, wib
from pydantic import SecretStr
from sqlalchemy import func, select, text, update

from astria_scheduler.main import build_app
from astria_scheduler.orkestrator import Orkestrator
from astria_scheduler.penjadwal import Penjadwal
from astria_scheduler.settings import Settings
from astria_scheduler.store import ambil_sewa, digest, lease, metadata

DB_URL = os.environ.get("ASTRIA_TEST_DATABASE_URL")
CRON = "0 7 * * *"
TTL = timedelta(seconds=30)

pytestmark = pytest.mark.skipif(not DB_URL, reason="ASTRIA_TEST_DATABASE_URL tidak diisi")


@pytest.fixture
async def db():
    schema = f"t_{uuid.uuid4().hex[:8]}"
    database = Database(SecretStr(DB_URL or ""), schema=schema, metadata=[outbox.metadata, metadata])
    await database.start()
    await database.wait_ready(10)
    yield database
    async with database.engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    await database.stop()


def _ork(db: Database, sumber, jam: JamUji, percobaan_maks: int = 3) -> Orkestrator:
    return Orkestrator(
        db,
        sumber,
        cron=CRON,
        toleransi=timedelta(minutes=360),
        percobaan_maks=percobaan_maks,
        maks_periode=timedelta(hours=48),
        jam=jam,
    )


async def _jalankan_semua(ork: Orkestrator) -> None:
    for digest_id in await ork.tertunda():
        await ork.jalankan(digest_id)


async def _digests(db: Database) -> list:
    async with db.engine.connect() as conn:
        return (await conn.execute(select(digest).order_by(digest.c.digest_id))).all()


async def _outbox(db: Database) -> list:
    async with db.engine.connect() as conn:
        rows = (await conn.execute(select(outbox.outbox.c.envelope).order_by(outbox.outbox.c.created_at))).all()
    return [decode(orjson.dumps(r.envelope)) for r in rows]


# --- kunci sewa ---


@pytest.mark.anyio
async def test_lease_has_one_holder_until_it_expires(db: Database) -> None:
    async with db.engine.begin() as conn:
        assert await ambil_sewa(conn, "penjadwal", "a", TTL)
        assert not await ambil_sewa(conn, "penjadwal", "b", TTL)
        assert await ambil_sewa(conn, "penjadwal", "a", TTL)  # perpanjang
        await conn.execute(update(lease).values(kedaluwarsa_pada=func.now() - timedelta(seconds=1)))
        assert await ambil_sewa(conn, "penjadwal", "b", TTL)
        assert not await ambil_sewa(conn, "penjadwal", "a", TTL)
        assert (await conn.execute(select(lease.c.pemegang))).scalar() == "b"


@pytest.mark.anyio
async def test_two_replicas_publish_one_digest_per_period(db: Database) -> None:
    jam = JamUji(wib(2026, 11, 10, 7, 0, 20))
    replika = [Penjadwal(db, _ork(db, ticket_ok(), jam), ttl=TTL, perbarui=TTL) for _ in range(2)]
    await asyncio.gather(*(p.putaran() for p in replika))
    await asyncio.gather(*(p.putaran() for p in replika))

    assert sum(p._pegang for p in replika) == 1
    assert [(d.digest_id, d.status) for d in await _digests(db)] == [("DG-20261110-0700", "terbit")]
    assert [e.type for e, _ in await _outbox(db)] == ["digest.published", "audit.record"]


# --- orkestrator ---


@pytest.mark.anyio
async def test_on_time_digest_publishes_once_with_outbox_and_audit(db: Database) -> None:
    calls = []
    ork = _ork(db, ticket_ok(calls), JamUji(wib(2026, 11, 10, 7, 0, 30)))
    await ork.jadwalkan()
    await ork.jadwalkan()  # putaran berikutnya: periode sudah ada
    await _jalankan_semua(ork)
    await _jalankan_semua(ork)  # tidak ada yang tertunda lagi

    [row] = await _digests(db)
    assert (row.digest_id, row.status, row.percobaan, row.terlambat) == ("DG-20261110-0700", "terbit", 1, False)
    assert (row.periode_mulai, row.periode_selesai) == (wib(2026, 11, 9, 7, 0), wib(2026, 11, 10, 7, 0))
    assert row.sha256 == hashlib.sha256(row.html.encode()).hexdigest()
    assert "IC-20261110-003" in row.html and "&lt;script&gt;" in row.html
    assert len(calls) == 2

    [(pub, data), (aud, rec)] = await _outbox(db)
    assert (pub.type, pub.correlation.digest_id) == ("digest.published", row.digest_id)
    assert (aud.type, aud.correlation.digest_id) == ("audit.record", row.digest_id)
    assert (data.sha256, data.mode_narasi, data.terlambat, data.audit_head_hash) == (row.sha256, "templat", False, None)
    assert data.periode_selesai == wib(2026, 11, 10, 7, 0)
    assert (rec.actor, rec.action, rec.target_id, rec.payload["sumber_terbaca"]) == (
        "svc:scheduler",
        "digest.publish",
        row.digest_id,
        True,
    )


@pytest.mark.anyio
async def test_unreachable_ticket_still_publishes_with_notice(db: Database) -> None:
    ork = _ork(db, ticket_down(), JamUji(wib(2026, 11, 10, 7, 1)))
    await ork.jadwalkan()
    await _jalankan_semua(ork)

    [row] = await _digests(db)
    assert row.status == "terbit"
    assert "Sumber tidak terbaca sejak 2026-11-10 07:01 WIB" in row.html
    [(pub, data), (_, rec)] = await _outbox(db)
    assert pub.type == "digest.published" and data.sha256 == row.sha256
    assert rec.payload["sumber_terbaca"] is False


@pytest.mark.anyio
async def test_late_within_tolerance_runs_once_marked_late(db: Database) -> None:
    ork = _ork(db, ticket_ok(), JamUji(wib(2026, 11, 10, 9, 30)))
    await ork.jadwalkan()
    await _jalankan_semua(ork)

    [row] = await _digests(db)
    assert (row.status, row.terlambat, row.terlambat_menit) == ("terbit", True, 150)
    assert "terlambat 150 menit" in row.html
    [(_, data), _] = await _outbox(db)
    assert data.terlambat is True


@pytest.mark.anyio
async def test_missed_digest_is_recorded_and_manual_run_publishes_it(db: Database) -> None:
    ork = _ork(db, ticket_ok(), JamUji(wib(2026, 11, 10, 13, 1)))  # 361 menit
    await ork.jadwalkan()
    await _jalankan_semua(ork)
    assert [(d.digest_id, d.status) for d in await _digests(db)] == [("DG-20261110-0700", "terlewat")]
    assert await _outbox(db) == []

    baris, baru = await ork.minta_manual(None, None)
    assert (baris.digest_id, baris.status, baru) == ("DG-20261110-0700", "terjadwal", True)
    await _jalankan_semua(ork)
    assert [d.status for d in await _digests(db)] == ["terbit"]
    assert [e.type for e, _ in await _outbox(db)] == ["digest.published", "audit.record"]


@pytest.mark.anyio
async def test_concurrent_manual_triggers_create_one_row(db: Database) -> None:
    ork = _ork(db, ticket_ok(), JamUji(wib(2026, 11, 10, 8, 0)))
    mulai, selesai = wib(2026, 11, 8, 10, 0), wib(2026, 11, 9, 10, 0)
    hasil = await asyncio.gather(*(ork.minta_manual(mulai, selesai) for _ in range(4)))

    assert sorted(baru for _, baru in hasil) == [False, False, False, True]
    assert {b.digest_id for b, _ in hasil} == {"DG-20261109-1000"}
    assert len(await _digests(db)) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("mulai", "selesai"),
    [
        (wib(2026, 11, 9, 10, 0), wib(2026, 11, 9, 10, 0)),  # kosong
        (wib(2026, 11, 9, 10, 0), wib(2026, 11, 8, 10, 0)),  # terbalik
        (wib(2026, 11, 10, 7, 0), wib(2026, 11, 10, 9, 0)),  # masa depan
        (wib(2026, 11, 6, 10, 0), wib(2026, 11, 9, 10, 0)),  # lebih dari 48 jam
        (None, wib(2026, 11, 9, 10, 0)),  # hanya satu ujung
    ],
)
async def test_invalid_manual_period_is_rejected(db: Database, mulai, selesai) -> None:
    ork = _ork(db, ticket_ok(), JamUji(wib(2026, 11, 10, 8, 0)))
    with pytest.raises(AstriaError) as err:
        await ork.minta_manual(mulai, selesai)
    assert (err.value.code, err.value.status) == ("PERIODE_TIDAK_VALID", 422)


class SumberRusak:
    async def ambil(self, dari, sampai):
        raise RuntimeError("templat rusak")


@pytest.mark.anyio
async def test_failure_retries_then_dies_without_publishing(db: Database) -> None:
    ork = _ork(db, SumberRusak(), JamUji(wib(2026, 11, 10, 7, 0)), percobaan_maks=2)
    await ork.jadwalkan()
    statuses = []
    for _ in range(3):
        await _jalankan_semua(ork)
        statuses.append([(d.status, d.percobaan) for d in await _digests(db)][0])

    assert statuses == [("gagal", 1), ("gagal", 2), ("mati", 2)]
    assert await ork.tertunda() == []
    assert await _outbox(db) == []


@pytest.mark.anyio
async def test_runner_that_lost_its_attempt_does_not_publish(db: Database) -> None:
    class SumberDiambilAlih:
        """Saat mengambil data, pelari lain (pemegang sewa baru) sudah memulai percobaan berikutnya."""

        async def ambil(self, dari, sampai):
            async with db.engine.begin() as conn:
                await conn.execute(update(digest).values(percobaan=digest.c.percobaan + 1))
            return None

    ork = _ork(db, SumberDiambilAlih(), JamUji(wib(2026, 11, 10, 7, 0)))
    await ork.jadwalkan()
    await _jalankan_semua(ork)

    assert [(d.status, d.percobaan) for d in await _digests(db)] == [("berjalan", 2)]
    assert await _outbox(db) == []


@pytest.mark.anyio
async def test_stale_running_row_counts_as_failed_attempt(db: Database) -> None:
    ork = _ork(db, ticket_ok(), JamUji(wib(2026, 11, 10, 7, 0)), percobaan_maks=2)
    await ork.jadwalkan()
    async with db.engine.begin() as conn:  # proses mati di tengah percobaan terakhir
        await conn.execute(update(digest).values(status="berjalan", percobaan=2))
    await _jalankan_semua(ork)
    assert [(d.status, d.percobaan) for d in await _digests(db)] == [("mati", 2)]
    assert await _outbox(db) == []


# --- endpoint dengan basis data ---


@contextlib.asynccontextmanager
async def _database(schema: str):
    database = Database(SecretStr(DB_URL or ""), schema=schema)
    await database.start()
    await database.wait_ready(10)
    try:
        yield database
    finally:
        await database.stop()


async def _drop(schema: str) -> None:
    async with _database(schema) as database, database.engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


async def _outbox_skema(schema: str) -> list[tuple[str, dict]]:
    async with _database(schema) as database, database.engine.connect() as conn:
        rows = (await conn.execute(select(outbox.outbox.c.envelope))).all()
    return [(r.envelope["type"], r.envelope["data"]) for r in rows]


def _tunggu(kondisi, batas: float = 15) -> None:
    deadline = time.monotonic() + batas
    while not kondisi():
        assert time.monotonic() < deadline, "batas waktu menunggu"
        time.sleep(0.1)


def test_endpoints_run_list_get_and_document(tmp_path) -> None:
    key = load_or_create_signing_key(tmp_path / "identity.pem")
    verifier = ServiceTokenVerifier(JwksSource("http://identity.invalid", fetch=lambda: {"keys": [key.public_jwk()]}))
    token, _ = issue_service_token(key, subject="svc:api", audience="svc:scheduler", act={"sub": "usr_admin1"})
    auth = {"Authorization": f"Bearer {token}"}
    schema = f"t_{uuid.uuid4().hex[:8]}"
    settings = Settings(env="test", database_url=SecretStr(DB_URL or ""), db_schema=schema)
    app = build_app(settings, token_verifier=verifier, sumber=ticket_ok(), jam=JamUji(wib(2026, 11, 10, 8, 0)))
    try:
        with TestClient(app) as client:
            _tunggu(lambda: client.get("/ready").status_code == 200)
            periode = {"periode_mulai": "2026-11-08T10:00:00+07:00", "periode_selesai": "2026-11-09T10:00:00+07:00"}
            pertama = client.post("/svc/v1/digests/run", json=periode | {"alasan": "uji"}, headers=auth)
            assert pertama.status_code == 202
            assert pertama.json() == {"digest_id": "DG-20261109-1000", "status": "terjadwal", "terlambat": False}
            kedua = client.post("/svc/v1/digests/run", json=periode, headers=auth)
            assert (kedua.status_code, kedua.json()["digest_id"]) == (200, "DG-20261109-1000")
            masa_depan = client.post(
                "/svc/v1/digests/run", json={**periode, "periode_selesai": "2026-11-11T00:00:00+07:00"}, headers=auth
            )
            assert (masa_depan.status_code, masa_depan.json()["error"]["code"]) == (422, "PERIODE_TIDAK_VALID")

            def _status(digest_id: str) -> str:
                return client.get(f"/svc/v1/digests/{digest_id}", headers=auth).json().get("status", "")

            # Loop penjadwal juga menjalankan jadwal 07:00 (terlambat 60 menit pada jam uji 08:00).
            _tunggu(lambda: _status("DG-20261109-1000") == "terbit" and _status("DG-20261110-0700") == "terbit")
            # Tanpa badan (seperti perintah digest_run dari api): periode jadwal terakhir yang sudah ada.
            kosong = client.post("/svc/v1/digests/run", headers=auth)
            assert (kosong.status_code, kosong.json()["digest_id"]) == (200, "DG-20261110-0700")

            halaman = client.get("/svc/v1/digests?limit=1", headers=auth).json()
            assert [i["digest_id"] for i in halaman["items"]] == ["DG-20261110-0700"]
            assert halaman["items"][0]["tautan"] == "/digests/DG-20261110-0700"
            assert halaman["items"][0]["periode"] == "2026-11-09 07:00 WIB sampai 2026-11-10 07:00 WIB"
            berikut = client.get(f"/svc/v1/digests?limit=1&cursor={halaman['next_cursor']}", headers=auth).json()
            assert [i["digest_id"] for i in berikut["items"]] == ["DG-20261109-1000"]
            assert berikut["next_cursor"] is None

            meta = client.get("/svc/v1/digests/DG-20261110-0700", headers=auth).json()
            assert (meta["terlambat"], meta["terlambat_menit"], meta["mode_narasi"]) == (True, 60, "templat")
            dokumen = client.get("/svc/v1/digests/DG-20261110-0700/dokumen", headers=auth)
            assert dokumen.status_code == 200
            assert dokumen.headers["content-type"].startswith("text/html")
            assert dokumen.headers["content-security-policy"].startswith("default-src 'none'")
            assert hashlib.sha256(dokumen.content).hexdigest() == meta["sha256"] == dokumen.headers["x-astria-sha256"]
            assert "terlambat 60 menit" in dokumen.text

            hilang = client.get("/svc/v1/digests/DG-20200101-0700", headers=auth)
            assert (hilang.status_code, hilang.json()["error"]["code"]) == (404, "DIGEST_TIDAK_DITEMUKAN")

            unduh = [e for e in asyncio.run(_outbox_skema(schema)) if e[1].get("action") == "digest.download"]
            assert [(d["actor"], d["actor_type"], d["target_id"]) for _, d in unduh] == [
                ("usr_admin1", "user", "DG-20261110-0700")
            ]
    finally:
        asyncio.run(_drop(schema))
