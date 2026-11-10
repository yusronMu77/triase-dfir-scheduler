import pytest
from helpers import ticket_down, ticket_ok, ticket_status, wib

from astria_scheduler.render import render_digest

pytestmark = pytest.mark.anyio

MULAI, SELESAI = wib(2026, 11, 9, 7, 0), wib(2026, 11, 10, 7, 0)


async def test_ticket_client_reads_stats_and_queue() -> None:
    calls = []
    ringkasan = await ticket_ok(calls).ambil(MULAI, SELESAI)
    assert ringkasan is not None
    assert (ringkasan.statistik.total, ringkasan.statistik.sla_terlanggar) == (7, 1)
    assert [t.card_id for t in ringkasan.tiket][:1] == ["IC-20261110-003"]
    stats = calls[0]
    assert stats.url.path == "/svc/v1/stats"
    assert stats.url.params["dari"] == MULAI.isoformat()
    assert stats.url.params["sampai"] == SELESAI.isoformat()
    assert stats.headers["authorization"] == "Bearer uji-svc:ticket"
    assert calls[1].url.params["limit"] == "10"


@pytest.mark.parametrize(
    "client",
    [ticket_down(), ticket_status(500), ticket_status(200, b'{"total": "banyak"}'), ticket_status(200, b"bukan json")],
    ids=["tidak_terjangkau", "galat_500", "bentuk_salah", "bukan_json"],
)
async def test_unreadable_ticket_returns_none(client) -> None:
    assert await client.ambil(MULAI, SELESAI) is None


async def test_render_escapes_ticket_text_and_marks_late_and_unreadable() -> None:
    ringkasan = await ticket_ok().ambil(MULAI, SELESAI)
    html = render_digest(
        digest_id="DG-20261110-0700",
        periode_mulai=MULAI,
        periode_selesai=SELESAI,
        disusun=wib(2026, 11, 10, 9, 30),
        terlambat_menit=150,
        ringkasan=ringkasan,
        tidak_terbaca_sejak=None,
    )
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; co" in html
    assert "terlambat 150 menit dari jadwal 2026-11-10 07:00 WIB" in html
    assert "Dalam periode ini tercatat 7 tiket; 1 di antaranya melewati batas SLA." in html
    assert "Sumber tidak terbaca" not in html

    kosong = render_digest(
        digest_id="DG-20261110-0700",
        periode_mulai=MULAI,
        periode_selesai=SELESAI,
        disusun=SELESAI,
        terlambat_menit=None,
        ringkasan=None,
        tidak_terbaca_sejak=wib(2026, 11, 10, 6, 0),
    )
    assert "Sumber tidak terbaca sejak 2026-11-10 06:00 WIB" in kosong
    assert "terlambat" not in kosong
