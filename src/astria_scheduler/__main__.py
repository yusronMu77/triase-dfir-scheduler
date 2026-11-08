"""Jalankan layanan: `astria-scheduler` atau `python -m astria_scheduler`."""

from astria_common.app import run

from astria_scheduler.main import build_app
from astria_scheduler.settings import Settings


def main() -> None:
    settings = Settings()
    run(build_app(settings), settings)


if __name__ == "__main__":
    main()
