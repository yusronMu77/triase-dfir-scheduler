"""Tabel scheduler (note app-scheduler 4) dan operasi yang menjaga invarian: kunci sewa dan transisi status.

HTML digest disimpan di kolom `html` beserta SHA-256-nya; berkas dan PDF ditunda (note 5.9).
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    func,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from astria_scheduler.jadwal import TRANSISI, boleh

metadata = MetaData()

digest = Table(
    "digest",
    metadata,
    Column("digest_id", Text, primary_key=True),
    Column("jenis", Text, nullable=False),
    Column("periode_mulai", DateTime(timezone=True), nullable=False),
    Column("periode_selesai", DateTime(timezone=True), nullable=False),
    Column("dijadwalkan_pada", DateTime(timezone=True), nullable=False),
    Column("dijalankan_pada", DateTime(timezone=True), nullable=True),
    Column("status", Text, nullable=False),
    Column("percobaan", Integer, nullable=False, server_default="0"),
    Column("terlambat", Boolean, nullable=False, server_default="false"),
    Column("terlambat_menit", Integer, nullable=True),
    Column("mode_narasi", Text, nullable=True),
    Column("sha256", Text, nullable=True),
    Column("html", Text, nullable=True),
    Column("versi_templat", Text, nullable=False),
    Column("dibuat_pada", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("jenis", "periode_mulai"),
    CheckConstraint("jenis IN ('harian')", name="digest_jenis"),
    CheckConstraint(f"status IN ({', '.join(repr(s) for s in TRANSISI)})", name="digest_status"),
    CheckConstraint("mode_narasi IN ('online','lokal','replay','templat')", name="digest_mode_narasi"),
)

# Semua kolom kecuali isi dokumen; daftar dan metadata tidak perlu memuat HTML.
METADATA_DIGEST = [c for c in digest.c if c.name != "html"]

lease = Table(
    "lease",
    metadata,
    Column("nama", Text, primary_key=True),
    Column("pemegang", Text, nullable=False),
    Column("kedaluwarsa_pada", DateTime(timezone=True), nullable=False),
)


async def ambil_sewa(conn: AsyncConnection, nama: str, pemegang: str, ttl: timedelta) -> bool:
    """Ambil atau perbarui sewa. Waktu dari jam basis data, agar dua host dengan jam berbeda tidak saling merebut."""
    stmt = insert(lease).values(nama=nama, pemegang=pemegang, kedaluwarsa_pada=func.now() + ttl)
    stmt = stmt.on_conflict_do_update(
        index_elements=[lease.c.nama],
        set_={"pemegang": stmt.excluded.pemegang, "kedaluwarsa_pada": stmt.excluded.kedaluwarsa_pada},
        where=(lease.c.kedaluwarsa_pada < func.now()) | (lease.c.pemegang == stmt.excluded.pemegang),
    ).returning(lease.c.pemegang)
    return (await conn.execute(stmt)).first() is not None


async def ubah_status(
    conn: AsyncConnection, digest_id: str, dari: str, ke: str, *, saat_percobaan: int | None = None, **nilai: object
) -> bool:
    """Pindahkan status bila transisinya sah dan baris masih `dari` (dan `saat_percobaan`). False = didahului."""
    if not boleh(dari, ke):
        raise ValueError(f"transisi digest tidak sah: {dari} -> {ke}")
    syarat = [digest.c.digest_id == digest_id, digest.c.status == dari]
    if saat_percobaan is not None:
        syarat.append(digest.c.percobaan == saat_percobaan)
    hasil = await conn.execute(update(digest).where(*syarat).values(status=ke, **nilai))
    return hasil.rowcount == 1
