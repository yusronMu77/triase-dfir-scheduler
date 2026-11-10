# Changelog

## [Unreleased]

### Added

- scheduler#2: digest harian setiap hari 07:00 WIB (mode narasi templat). Loop penjadwal dengan kunci sewa PostgreSQL (`lease`), pengejaran satu kali dalam toleransi 360 menit dan status `terlewat` di luar itu, tabel `digest` dengan `UNIQUE (jenis, periode_mulai)` dan mesin status, statistik dan antrean dari `ticket` (`GET /svc/v1/stats`, `GET /svc/v1/tickets`) dengan baris "sumber tidak terbaca" bila gagal, HTML Jinja2 dengan autoescape dan SHA-256 di basis data, `digest.published` dan `audit.record` lewat outbox, dan endpoint `POST /svc/v1/digests/run`, `GET /svc/v1/digests`, `GET /svc/v1/digests/{id}`, `GET /svc/v1/digests/{id}/dokumen` untuk `svc:api`. Dependensi baru `croniter` dan `jinja2`. Butuh `astria-contracts` v0.3.0 (`digest.published`, correlation `digest_id`).

- Kerangka layanan Tahap 0 dari templat `common` v0.1.0: `/health`, `/ready`, `/metrics`, token layanan, Dockerfile non-root, CI (lint, uji, gitleaks, build image, smoke `/ready`).
- Logo Astria di kepala README (`.github/assets/`, dari kit logo di repo `triase-dfir-uiux`).
