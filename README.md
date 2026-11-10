<!-- astria-logo -->
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/astria-logo-reverse.svg">
    <img src=".github/assets/astria-logo.svg" alt="Astria" width="320">
  </picture>
</p>

# Astria scheduler

Penjadwalan digest dan tugas berkala.

> Status: Tahap 1 (scheduler#2). Digest harian setiap hari 07:00 WIB dalam mode narasi templat: statistik dan antrean dari `ticket`, HTML dengan autoescape dan SHA-256 di basis data, lalu `digest.published` dan `audit.record` lewat outbox. Repo ini adalah submodule dari workspace `triase-dfir` (folder `scheduler/`). Rancangan: `notes/2026-11-06-triase-dfir-app-scheduler.md` di repo root. Lajur developer: 2.

## Tech stack

Python 3.14, FastAPI, PostgreSQL 18, NATS JetStream, croniter, Jinja2, uv

## Perintah

```text
uv sync                                   # butuh akses ke repo privat contracts dan common (tag git)
uv run ruff format --check . && uv run ruff check .
uv run pytest
sh ../common/template/localtest.sh        # di kontainer py devenv: uji terhadap salinan kerja common dan contracts
```

Menjalankan bersama layanan lain: `pwsh ../devenv/scripts/dev.ps1 up`, lalu `dev.ps1 smoke`.

Uji integrasi (`tests/test_integration.py`) butuh `ASTRIA_TEST_DATABASE_URL`; di kontainer `py` devenv isi dengan
`postgresql+psycopg://$POSTGRES_USER:$POSTGRES_PASSWORD@postgres:5432/postgres`. Tanpa variabel itu uji tersebut dilewati.
`ticket` di semua uji adalah tiruan `httpx.MockTransport` (`tests/helpers.py`).

## Cara kerja

| Bagian | Isi |
|---|---|
| `jadwal.py` | Domain murni: `jadwal_berikutnya` dan `jadwal_terakhir` (croniter, cron dibaca dalam WIB +07:00 tetap), periode = jadwal sebelumnya sampai jadwal ini, `digest_id` = `DG-yyyymmdd-HHMM` (WIB) dari akhir periode, `tentukan_aksi` (Lewati, Tunggu, JalankanTerlambat, Terlewat), dan tabel transisi status. Jam disuntikkan (`Jam`) |
| `penjadwal.py` | Loop sendiri dengan kunci sewa `lease` di PostgreSQL (upsert, TTL 30 detik, diperbarui setiap 10 detik dan sebelum setiap digest). Hanya pemegang sewa yang menjadwalkan dan menjalankan |
| `orkestrator.py` | Jadwal terakhir dalam toleransi (360 menit) dicatat `terjadwal` sekali, dengan `terlambat` dan `terlambat_menit` bila lewat; lewat toleransi dicatat `terlewat` dan menunggu pemicu manual. Satu digest per periode (`UNIQUE (jenis, periode_mulai)`). Menjalankan: `berjalan`, ambil data `ticket`, render, lalu `terbit` + `digest.published` + `audit.record` dalam satu transaksi. Langkah terbit bersyarat nomor percobaan, jadi pelari yang sudah didahului tidak menerbitkan. Galat: `gagal`, dicoba lagi tiap putaran, `mati` setelah `percobaan_maks` |
| `ticket.py` | `GET /svc/v1/stats?dari=&sampai=` dan `GET /svc/v1/tickets?limit=10` dengan token layanan. Tidak terjangkau, galat, atau bentuk lain: digest tetap terbit dengan baris "Sumber tidak terbaca sejak <waktu>" |
| `render.py`, `templates/` | Jinja2 dengan autoescape dan `StrictUndefined`; tidak ada `|safe`. Narasi templat dari angka, tanpa model bahasa |
| `api.py` | Endpoint `/svc/v1` di bawah |

Status digest: `terjadwal -> berjalan -> terbit`; `berjalan -> gagal -> berjalan` (coba ulang) atau `gagal -> mati`;
`terlewat -> terjadwal` saat admin menjalankan manual. Baris `berjalan` yang tertinggal saat proses mati dihitung sebagai percobaan
gagal. Waktu "sumber tidak terbaca sejak" disimpan di memori proses (hilang saat restart atau pindah pemegang sewa).

## API

Semua endpoint wajib token layanan audiens `svc:scheduler` dari `svc:api` (`astria_common.auth`). Peran pengguna diperiksa api.

| Metode dan jalur | Isi |
|---|---|
| `POST /svc/v1/digests/run` | Badan opsional `{jenis, periode_mulai, periode_selesai, alasan}`; tanpa periode = periode jadwal terakhir. `202` bila periode baru (atau periode `terlewat` dijalankan ulang), `200` dengan `digest_id` sama bila sudah ada. Jawaban `{digest_id, status, terlambat}` |
| `GET /svc/v1/digests?limit=&cursor=` | Terbaru dulu: `{items: [{digest_id, jenis, periode, periode_mulai, periode_selesai, status, terlambat, tautan}], next_cursor}`. `cursor` = `digest_id` terakhir |
| `GET /svc/v1/digests/{digest_id}` | Metadata: ditambah `terlambat_menit`, `dijadwalkan_pada`, `dijalankan_pada`, `percobaan`, `mode_narasi`, `sha256`, `versi_templat` |
| `GET /svc/v1/digests/{digest_id}/dokumen?format=html` | HTML yang terbit (`Content-Security-Policy: default-src 'none'`, `X-Astria-Sha256`). Unduhan dicatat `audit.record` (`digest.download`) lewat outbox |

Kode galat: `PERIODE_TIDAK_VALID` (422: kosong, terbalik, masa depan, atau lebih dari 48 jam), `DIGEST_TIDAK_DITEMUKAN` (404),
`DIGEST_BELUM_TERBIT` (409), `FORMAT_BELUM_DIDUKUNG` (422, `format=pdf`), `SCHEDULER_TIDAK_SIAP` (503).

## Usulan kontrak untuk ticket (lajur 1)

Ticket belum punya kedua endpoint ini. Scheduler membaca bentuk minimal berikut (field lain diabaikan); contohnya ada di
`tests/fixtures/ticket_stats.json` dan `tests/fixtures/ticket_tickets.json`.

```text
GET /svc/v1/stats?dari=<ISO 8601>&sampai=<ISO 8601>     (audiens svc:ticket, pemanggil svc:scheduler)
{"total": 7, "per_status": {"baru": 2, "selesai": 2}, "per_band": {"KRITIS": 1, "TINGGI": 2}, "sla_terlanggar": 1}

GET /svc/v1/tickets?limit=10
{"items": [{"card_id": "IC-20261110-003", "judul": "...", "band": "TINGGI", "status": "diselidiki"}], "next_cursor": null}
```

## Konfigurasi

| Variabel | Bawaan | Isi |
|---|---|---|
| `ASTRIA_SCHEDULER_TICKET_URL` | `http://ticket:8000` | Basis URL ticket; wajib bersama `ASTRIA_SERVICE_KEY`/`_FILE` bila basis data diisi |
| `ASTRIA_SCHEDULER_DIGEST_CRON` | `0 7 * * *` | Jadwal digest dalam WIB (setiap hari 07:00) |
| `ASTRIA_SCHEDULER_TOLERANSI_MENIT` | `360` | Keterlambatan yang masih dijalankan |
| `ASTRIA_SCHEDULER_LEASE_TTL_DETIK` | `30` | Umur sewa penjadwal |
| `ASTRIA_SCHEDULER_LEASE_PERBARUI_DETIK` | `10` | Jeda putaran dan pembaruan sewa |
| `ASTRIA_SCHEDULER_PERCOBAAN_MAKS` | `3` | Percobaan sebelum `mati` |
| `ASTRIA_SCHEDULER_MAKS_PERIODE_JAM` | `48` | Batas panjang periode pemicu manual |

Belum dibuat (ditunda): PDF (WeasyPrint) dan berkas di volume, narasi dari `narrative`, serah terima shift, lembar tiket,
salinan jadwal dari `config.changed`, event `digest.gagal`/`digest.terlewat` (status hanya di tabel), dan `audit_head_hash`.
Tabel dibuat `create_all` saat start (belum ada Alembic).

## Alur git

Branch `{type}/{yyyyMMdd}-{taskId}-{nama}`; commit `{type}({yyyyMMddHHmm}): [{taskId}] {pesan}`, satu baris, tanpa trailer. `main` hanya berubah lewat pull request (merge rebase); jangan push langsung ke `main` dan jangan `--no-verify`.
