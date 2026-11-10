"""Domain murni jadwal digest (note app-scheduler 5.2 sampai 5.5): tanpa FastAPI, SQLAlchemy, atau jam sistem.

Waktu selalu aware. Ekspresi cron dibaca dalam WIB, offset tetap +07:00 karena WIB tidak mengenal waktu musim panas.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from croniter import croniter

WIB = timezone(timedelta(hours=7))

Jam = Callable[[], datetime]
"""Port waktu: mengembalikan waktu sekarang (aware). Uji menyuntikkan jam tetap."""


def jam_sistem() -> datetime:
    return datetime.now(UTC)


def jadwal_berikutnya(ekspresi: str, sekarang: datetime) -> datetime:
    """Jadwal pertama setelah `sekarang` (UTC)."""
    waktu = croniter(ekspresi, sekarang.astimezone(WIB)).get_next(datetime)
    return waktu.astimezone(UTC)


def jadwal_terakhir(ekspresi: str, sekarang: datetime) -> datetime:
    """Jadwal terakhir yang jatuh pada atau sebelum `sekarang` (UTC)."""
    # get_prev melewati waktu yang tepat sama; mundur dari jadwal berikutnya menyertakannya.
    waktu = croniter(ekspresi, jadwal_berikutnya(ekspresi, sekarang).astimezone(WIB)).get_prev(datetime)
    return waktu.astimezone(UTC)


def periode_untuk(ekspresi: str, terjadwal: datetime) -> tuple[datetime, datetime]:
    """Periode digest: dari jadwal sebelumnya sampai jadwal ini."""
    mulai = croniter(ekspresi, terjadwal.astimezone(WIB)).get_prev(datetime)
    return mulai.astimezone(UTC), terjadwal.astimezone(UTC)


def format_wib(waktu: datetime) -> str:
    return f"{waktu.astimezone(WIB):%Y-%m-%d %H:%M} WIB"


def digest_id_untuk(periode_selesai: datetime) -> str:
    return f"DG-{periode_selesai.astimezone(WIB):%Y%m%d-%H%M}"


@dataclass(frozen=True)
class Lewati:
    """Digest periode ini sudah ada."""


@dataclass(frozen=True)
class Tunggu:
    """Jadwal belum tiba."""


@dataclass(frozen=True)
class JalankanTerlambat:
    menit: int


@dataclass(frozen=True)
class Terlewat:
    """Lewat toleransi: dicatat terlewat, admin menjalankan manual."""


Aksi = Lewati | Tunggu | JalankanTerlambat | Terlewat


def tentukan_aksi(terjadwal: datetime, sekarang: datetime, sudah_ada: bool, toleransi: timedelta) -> Aksi:
    if sudah_ada:
        return Lewati()
    selisih = sekarang - terjadwal
    if selisih < timedelta(0):
        return Tunggu()
    if selisih <= toleransi:
        return JalankanTerlambat(menit=int(selisih.total_seconds() // 60))
    return Terlewat()


# Mesin status digest (note 5.5). Tambahan terhadap diagram: `terlewat -> terjadwal` saat admin menjalankan manual
# (worker lalu memindahkannya ke berjalan). Baris `berjalan` yang tertinggal saat proses mati dipindah ke `gagal`.
TRANSISI: dict[str, frozenset[str]] = {
    "terjadwal": frozenset({"berjalan"}),
    "berjalan": frozenset({"terbit", "gagal"}),
    "gagal": frozenset({"berjalan", "mati"}),
    "terlewat": frozenset({"terjadwal"}),
    "terbit": frozenset(),
    "mati": frozenset(),
}
PERLU_JALAN = ("terjadwal", "berjalan", "gagal")


def boleh(dari: str, ke: str) -> bool:
    return ke in TRANSISI[dari]
