"""Loop penjadwal dengan kunci sewa PostgreSQL (note app-scheduler 5.3): hanya pemegang sewa yang bekerja.

Sewa diperbarui setiap putaran dan sebelum setiap digest dijalankan. Bila pembaruan gagal, putaran berhenti;
digest yang sedang berjalan tidak bisa terbit karena langkah terbit bersyarat nomor percobaan (orkestrator).
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import uuid
from datetime import timedelta

from astria_common.component import Check
from astria_common.db import Database
from astria_common.log import get_logger

from astria_scheduler.orkestrator import Orkestrator
from astria_scheduler.store import ambil_sewa

log = get_logger(__name__)

NAMA_SEWA = "penjadwal"


class Penjadwal:
    name = "penjadwal"
    required = False

    def __init__(self, db: Database, orkestrator: Orkestrator, *, ttl: timedelta, perbarui: timedelta) -> None:
        self._db = db
        self._orkestrator = orkestrator
        self._ttl = ttl
        self._perbarui = perbarui
        self.pemegang = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        self._pegang = False
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._last_error: str | None = None

    def wake(self) -> None:
        self._wake.set()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="penjadwal")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def check(self) -> Check:
        if self._last_error:
            return Check("degraded", f"putaran terakhir gagal ({self._last_error})")
        return Check("ok", "pemegang sewa" if self._pegang else "menunggu sewa")

    async def _sewa(self) -> bool:
        async with self._db.engine.begin() as conn:
            self._pegang = await ambil_sewa(conn, NAMA_SEWA, self.pemegang, self._ttl)
        return self._pegang

    async def putaran(self) -> None:
        if not await self._sewa():
            return
        await self._orkestrator.jadwalkan()
        for digest_id in await self._orkestrator.tertunda():
            if not await self._sewa():
                log.warning("sewa_hilang", pemegang=self.pemegang)
                return
            await self._orkestrator.jalankan(digest_id)

    async def _run(self) -> None:
        await self._db.wait_ready()
        while True:
            try:
                await self.putaran()
                self._last_error = None
            except Exception as exc:  # noqa: BLE001 - dicoba lagi pada putaran berikutnya
                self._pegang = False
                self._last_error = type(exc).__name__
                log.warning("putaran_gagal", error=self._last_error)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._perbarui.total_seconds())
            self._wake.clear()
