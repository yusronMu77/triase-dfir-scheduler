"""Orkestrasi digest harian (note app-scheduler 5.4 sampai 5.6), mode narasi templat saja (note 12).

Satu digest per periode dijaga kunci unik `(jenis, periode_mulai)`. Setiap langkah status memakai `ubah_status`
yang bersyarat status dan nomor percobaan, jadi pelari yang kehilangan sewa tidak bisa menerbitkan. Status
`terbit`, `digest.published`, dan `audit.record` ditulis dalam satu transaksi (outbox).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from astria_common.audit_client import audit_event
from astria_common.db import Database
from astria_common.errors import AstriaError
from astria_common.events import new_event
from astria_common.log import get_logger
from astria_common.outbox import enqueue
from astria_common.tracing import new_trace_id
from sqlalchemy import Row, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from astria_scheduler.jadwal import (
    PERLU_JALAN,
    WIB,
    JalankanTerlambat,
    Jam,
    Terlewat,
    digest_id_untuk,
    jadwal_terakhir,
    jam_sistem,
    periode_untuk,
    tentukan_aksi,
)
from astria_scheduler.render import VERSI_TEMPLAT, render_digest, sha256_hex
from astria_scheduler.store import METADATA_DIGEST, digest, ubah_status
from astria_scheduler.ticket import SumberRingkasan

log = get_logger(__name__)

JENIS = "harian"
PELAKU = "svc:scheduler"
MODE_NARASI = "templat"  # narasi model ditunda (note 12)


class Orkestrator:
    def __init__(
        self,
        db: Database,
        sumber: SumberRingkasan,
        *,
        cron: str,
        toleransi: timedelta,
        percobaan_maks: int,
        maks_periode: timedelta,
        jam: Jam = jam_sistem,
    ) -> None:
        self._db = db
        self._sumber = sumber
        self._cron = cron
        self._toleransi = toleransi
        self._percobaan_maks = percobaan_maks
        self._maks_periode = maks_periode
        self._jam = jam
        self._tidak_terbaca_sejak: datetime | None = None

    async def jadwalkan(self) -> None:
        """Catat digest untuk jadwal terakhir bila belum ada: terjadwal (tepat atau terlambat) atau terlewat."""
        sekarang = self._jam()
        terjadwal = jadwal_terakhir(self._cron, sekarang)
        mulai, selesai = periode_untuk(self._cron, terjadwal)
        async with self._db.engine.begin() as conn:
            sudah_ada = await self._cari(conn, digest_id_untuk(selesai), mulai) is not None
            match tentukan_aksi(terjadwal, sekarang, sudah_ada, self._toleransi):
                case JalankanTerlambat(menit=menit):
                    nilai = {"status": "terjadwal", "terlambat": menit > 0, "terlambat_menit": menit or None}
                case Terlewat():
                    nilai = {"status": "terlewat"}
                case _:
                    return
            if await self._sisip(conn, mulai, selesai, dijadwalkan_pada=terjadwal, **nilai):
                log.info("digest_dijadwalkan", digest_id=digest_id_untuk(selesai), **nilai)

    async def minta_manual(self, mulai: datetime | None, selesai: datetime | None) -> tuple[Row, bool]:
        """Pemicu manual. Tanpa periode = periode jadwal terakhir. Mengembalikan (baris, baru)."""
        sekarang = self._jam()
        if mulai is None and selesai is None:
            mulai, selesai = periode_untuk(self._cron, jadwal_terakhir(self._cron, sekarang))
        if mulai is None or selesai is None or not mulai < selesai <= sekarang or selesai - mulai > self._maks_periode:
            raise AstriaError(
                "PERIODE_TIDAK_VALID", "periode kosong, terbalik, di masa depan, atau terlalu panjang", 422
            )
        async with self._db.engine.begin() as conn:
            baru = await self._sisip(conn, mulai, selesai, dijadwalkan_pada=sekarang, status="terjadwal")
            baris = await self._cari(conn, digest_id_untuk(selesai), mulai)
            assert baris is not None
            if not baru and baris.status == "terlewat":
                baru = await ubah_status(conn, baris.digest_id, "terlewat", "terjadwal")
                baris = await self._cari(conn, baris.digest_id, mulai)
        log.info("digest_diminta_manual", digest_id=baris.digest_id, baru=baru, status=baris.status)
        return baris, baru

    async def tertunda(self) -> list[str]:
        async with self._db.engine.connect() as conn:
            hasil = await conn.scalars(
                select(digest.c.digest_id).where(digest.c.status.in_(PERLU_JALAN)).order_by(digest.c.dijadwalkan_pada)
            )
            return list(hasil.all())

    async def jalankan(self, digest_id: str) -> None:
        async with self._db.engine.begin() as conn:
            baris = (await conn.execute(select(*METADATA_DIGEST).where(digest.c.digest_id == digest_id))).first()
            if baris is None or baris.status not in PERLU_JALAN:
                return
            status = baris.status
            if status == "berjalan":  # tertinggal saat proses mati: dihitung sebagai percobaan gagal
                await ubah_status(conn, digest_id, "berjalan", "gagal", saat_percobaan=baris.percobaan)
                status = "gagal"
            if status == "gagal" and baris.percobaan >= self._percobaan_maks:
                await ubah_status(conn, digest_id, "gagal", "mati")
                log.error("digest_mati", digest_id=digest_id, percobaan=baris.percobaan)
                return
            percobaan = baris.percobaan + 1
            if not await ubah_status(
                conn,
                digest_id,
                status,
                "berjalan",
                saat_percobaan=baris.percobaan,
                percobaan=percobaan,
                dijalankan_pada=self._jam(),
            ):
                return
        try:
            await self._terbitkan(baris, percobaan)
        except Exception as exc:  # noqa: BLE001 - dicoba lagi pada putaran berikutnya sampai percobaan habis
            log.exception("digest_gagal", digest_id=digest_id, percobaan=percobaan, error=type(exc).__name__)
            async with self._db.engine.begin() as conn:
                await ubah_status(conn, digest_id, "berjalan", "gagal", saat_percobaan=percobaan)

    async def _terbitkan(self, baris: Row, percobaan: int) -> None:
        ringkasan = await self._sumber.ambil(baris.periode_mulai, baris.periode_selesai)
        sekarang = self._jam()
        self._tidak_terbaca_sejak = None if ringkasan is not None else self._tidak_terbaca_sejak or sekarang
        html = render_digest(
            digest_id=baris.digest_id,
            periode_mulai=baris.periode_mulai,
            periode_selesai=baris.periode_selesai,
            disusun=sekarang,
            terlambat_menit=baris.terlambat_menit if baris.terlambat else None,
            ringkasan=ringkasan,
            tidak_terbaca_sejak=self._tidak_terbaca_sejak,
        )
        sha = sha256_hex(html)
        korelasi = {"digest_id": baris.digest_id}
        trace_id = new_trace_id()
        async with self._db.engine.begin() as conn:
            if not await ubah_status(
                conn,
                baris.digest_id,
                "berjalan",
                "terbit",
                saat_percobaan=percobaan,
                html=html,
                sha256=sha,
                mode_narasi=MODE_NARASI,
            ):
                log.warning("digest_didahului", digest_id=baris.digest_id, percobaan=percobaan)
                return
            data = {
                "jenis": baris.jenis,
                "periode_mulai": baris.periode_mulai.astimezone(WIB).isoformat(),
                "periode_selesai": baris.periode_selesai.astimezone(WIB).isoformat(),
                "sha256": sha,
                "mode_narasi": MODE_NARASI,
                "terlambat": baris.terlambat,
            }
            await enqueue(conn, new_event("digest.published", data, correlation=korelasi, trace_id=trace_id))
            await enqueue(
                conn,
                audit_event(
                    {
                        "idempotency_key": f"scheduler:digest.publish:{baris.digest_id}",
                        "occurred_at": sekarang.isoformat(),
                        "actor": PELAKU,
                        "actor_type": "service",
                        "action": "digest.publish",
                        "target_type": "digest",
                        "target_id": baris.digest_id,
                        "via": "system",
                        "payload": {
                            "sha256": sha,
                            "mode_narasi": MODE_NARASI,
                            "terlambat": baris.terlambat,
                            "sumber_terbaca": ringkasan is not None,
                        },
                    },
                    correlation=korelasi,
                ),
            )
        log.info("digest_terbit", digest_id=baris.digest_id, terlambat=baris.terlambat, sumber=ringkasan is not None)

    @staticmethod
    async def _cari(conn: AsyncConnection, digest_id: str, mulai: datetime) -> Row | None:
        kunci = (digest.c.jenis == JENIS) & (digest.c.periode_mulai == mulai)
        kolom = (digest.c.digest_id, digest.c.status, digest.c.terlambat)
        return (await conn.execute(select(*kolom).where(or_(kunci, digest.c.digest_id == digest_id)))).first()

    @staticmethod
    async def _sisip(
        conn: AsyncConnection, mulai: datetime, selesai: datetime, *, dijadwalkan_pada: datetime, **nilai: object
    ) -> bool:
        """Sisipkan baris; False bila periode atau ID sudah ada (dua pemicu serentak menghasilkan satu baris)."""
        hasil = await conn.execute(
            insert(digest)
            .values(
                digest_id=digest_id_untuk(selesai),
                jenis=JENIS,
                periode_mulai=mulai,
                periode_selesai=selesai,
                dijadwalkan_pada=dijadwalkan_pada,
                versi_templat=VERSI_TEMPLAT,
                **nilai,
            )
            .on_conflict_do_nothing()
            .returning(digest.c.digest_id)
        )
        return hasil.first() is not None
