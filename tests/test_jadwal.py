from datetime import UTC, datetime, timedelta

import pytest
from helpers import wib

from astria_scheduler.jadwal import (
    TRANSISI,
    JalankanTerlambat,
    Lewati,
    Terlewat,
    Tunggu,
    boleh,
    digest_id_untuk,
    jadwal_berikutnya,
    jadwal_terakhir,
    periode_untuk,
    tentukan_aksi,
)

CRON = "0 7 * * *"
TOLERANSI = timedelta(minutes=360)


@pytest.mark.parametrize(
    ("sekarang", "harapan"),
    [
        (wib(2026, 11, 10, 23, 30), wib(2026, 11, 11, 7, 0)),  # lewat tengah malam WIB
        (datetime(2026, 11, 10, 20, 0, tzinfo=UTC), wib(2026, 11, 11, 7, 0)),  # tanggal UTC masih kemarin
        (wib(2026, 11, 10, 6, 59), wib(2026, 11, 10, 7, 0)),
        (wib(2026, 11, 10, 7, 0), wib(2026, 11, 11, 7, 0)),  # tepat jadwal: berikutnya besok
        (wib(2026, 12, 31, 8, 0), wib(2027, 1, 1, 7, 0)),  # lewat tahun
    ],
)
def test_next_run_in_wib_returned_as_utc(sekarang: datetime, harapan: datetime) -> None:
    hasil = jadwal_berikutnya(CRON, sekarang)
    assert hasil == harapan
    assert hasil.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    ("sekarang", "harapan"),
    [
        (wib(2026, 11, 10, 7, 0), wib(2026, 11, 10, 7, 0)),  # tepat jadwal termasuk
        (wib(2026, 11, 10, 7, 0, 30), wib(2026, 11, 10, 7, 0)),
        (wib(2026, 11, 10, 6, 59), wib(2026, 11, 9, 7, 0)),
        (wib(2026, 11, 11, 0, 30), wib(2026, 11, 10, 7, 0)),  # lewat tengah malam
    ],
)
def test_last_run_includes_exact_match(sekarang: datetime, harapan: datetime) -> None:
    assert jadwal_terakhir(CRON, sekarang) == harapan


def test_period_and_digest_id_follow_schedule_in_wib() -> None:
    terjadwal = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)  # 07:00 WIB
    assert periode_untuk(CRON, terjadwal) == (wib(2026, 11, 9, 7, 0), wib(2026, 11, 10, 7, 0))
    assert digest_id_untuk(terjadwal) == "DG-20261110-0700"


@pytest.mark.parametrize(
    ("menit", "sudah_ada", "harapan"),
    [
        (30, True, Lewati()),
        (-1, False, Tunggu()),
        (0, False, JalankanTerlambat(menit=0)),
        (150, False, JalankanTerlambat(menit=150)),
        (360, False, JalankanTerlambat(menit=360)),  # batas toleransi masih dijalankan
        (361, False, Terlewat()),
        (3 * 24 * 60, False, Terlewat()),  # laptop tidur tiga hari: tidak ada pengejaran berulang
    ],
)
def test_decide_action(menit: int, sudah_ada: bool, harapan: object) -> None:
    terjadwal = wib(2026, 11, 10, 7, 0)
    sekarang = terjadwal + timedelta(minutes=menit)
    assert tentukan_aksi(terjadwal, sekarang, sudah_ada, TOLERANSI) == harapan


def test_state_machine_terminal_states_and_forbidden_jumps() -> None:
    assert TRANSISI["terbit"] == TRANSISI["mati"] == frozenset()
    assert boleh("terjadwal", "berjalan") and boleh("gagal", "mati") and boleh("terlewat", "terjadwal")
    assert not boleh("terjadwal", "terbit")
    assert not boleh("terlewat", "terbit")
    assert all(ke in TRANSISI for tujuan in TRANSISI.values() for ke in tujuan)
