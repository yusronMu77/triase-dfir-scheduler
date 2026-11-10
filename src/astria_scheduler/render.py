"""Render HTML digest dengan Jinja2 (note 5.9 dan 8): autoescape selalu aktif, tidak ada `|safe` untuk data."""

from __future__ import annotations

import hashlib
from datetime import datetime

from jinja2 import Environment, PackageLoader, StrictUndefined

from astria_scheduler.jadwal import format_wib
from astria_scheduler.ticket import Ringkasan

VERSI_TEMPLAT = "digest-harian-1"

_env = Environment(
    loader=PackageLoader("astria_scheduler", "templates"),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.filters["wib"] = format_wib


def render_digest(
    *,
    digest_id: str,
    periode_mulai: datetime,
    periode_selesai: datetime,
    disusun: datetime,
    terlambat_menit: int | None,
    ringkasan: Ringkasan | None,
    tidak_terbaca_sejak: datetime | None,
) -> str:
    return _env.get_template("digest_harian.html").render(
        digest_id=digest_id,
        periode_mulai=periode_mulai,
        periode_selesai=periode_selesai,
        disusun=disusun,
        terlambat=bool(terlambat_menit),
        terlambat_menit=terlambat_menit,
        ringkasan=ringkasan,
        tidak_terbaca_sejak=tidak_terbaca_sejak,
        versi_templat=VERSI_TEMPLAT,
    )


def sha256_hex(html: str) -> str:
    return hashlib.sha256(html.encode()).hexdigest()
