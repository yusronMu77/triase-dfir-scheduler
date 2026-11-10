"""Konfigurasi scheduler dari env `ASTRIA_*`."""

from astria_common.settings import AstriaSettings
from croniter import croniter
from pydantic import Field, field_validator


class Settings(AstriaSettings):
    service_name: str = "scheduler"

    scheduler_ticket_url: str = "http://ticket:8000"
    """Basis URL ticket untuk statistik dan antrean periode (ASTRIA_SCHEDULER_TICKET_URL)."""
    scheduler_digest_cron: str = "0 7 * * *"
    """Jadwal digest harian dalam WIB (keputusan PM: setiap hari 07:00). Salinan dari config menyusul."""
    scheduler_toleransi_menit: int = Field(360, ge=0)
    """Batas keterlambatan yang masih dijalankan; lewat dari ini digest dicatat terlewat (contoh rancangan)."""
    scheduler_lease_ttl_detik: int = Field(30, ge=5)
    scheduler_lease_perbarui_detik: int = Field(10, ge=1)
    scheduler_percobaan_maks: int = Field(3, ge=1)
    """Percobaan menyusun digest sebelum status `mati`."""
    scheduler_maks_periode_jam: int = Field(48, ge=1)
    """Batas panjang periode pemicu manual."""

    @field_validator("scheduler_digest_cron")
    @classmethod
    def _cron_valid(cls, value: str) -> str:
        if not croniter.is_valid(value):
            raise ValueError(f"ekspresi cron tidak valid: {value!r}")
        return value
