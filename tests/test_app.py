import pytest
from astria_common.auth import (
    JwksSource,
    ServiceTokenVerifier,
    SigningKey,
    issue_service_token,
    load_or_create_signing_key,
)
from astria_common.envguard import EnvironmentGuardError
from fastapi.testclient import TestClient
from pydantic import SecretStr

from astria_scheduler.main import build_app
from astria_scheduler.settings import Settings

DG = "DG-20261110-0700"
ENDPOINTS = [
    ("POST", "/svc/v1/digests/run"),
    ("GET", "/svc/v1/digests"),
    ("GET", f"/svc/v1/digests/{DG}"),
    ("GET", f"/svc/v1/digests/{DG}/dokumen"),
]


@pytest.fixture(scope="module")
def key(tmp_path_factory: pytest.TempPathFactory) -> SigningKey:
    return load_or_create_signing_key(tmp_path_factory.mktemp("kunci") / "identity.pem")


@pytest.fixture
def client(key: SigningKey):
    verifier = ServiceTokenVerifier(JwksSource("http://identity.invalid", fetch=lambda: {"keys": [key.public_jwk()]}))
    with TestClient(build_app(Settings(env="test"), token_verifier=verifier)) as c:
        yield c


def _auth(key: SigningKey, subject: str = "svc:api", audience: str = "svc:scheduler") -> dict[str, str]:
    token, _ = issue_service_token(key, subject=subject, audience=audience)
    return {"Authorization": f"Bearer {token}"}


def test_health_and_ready_without_dependencies() -> None:
    with TestClient(build_app(Settings(env="test"))) as client:
        health = client.get("/health").json()
        assert health["service"] == "scheduler"
        assert health["env"] == "test"
        assert client.get("/ready").status_code == 200
        assert "astria_service_info" in client.get("/metrics").text


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_endpoints_require_service_token_from_api(client: TestClient, key: SigningKey, method: str, path: str) -> None:
    assert client.request(method, path).json()["error"]["code"] == "TOKEN_WAJIB"
    salah_audiens = client.request(method, path, headers=_auth(key, audience="svc:ticket"))
    assert salah_audiens.json()["error"]["code"] == "AUDIENS_SALAH"
    bukan_api = client.request(method, path, headers=_auth(key, subject="svc:notifier"))
    assert (bukan_api.status_code, bukan_api.json()["error"]["code"]) == (403, "PEMANGGIL_DITOLAK")
    lolos = client.request(method, path, headers=_auth(key))
    assert (lolos.status_code, lolos.json()["error"]["code"]) == (503, "SCHEDULER_TIDAK_SIAP")


def test_input_validation(client: TestClient, key: SigningKey) -> None:
    assert client.get("/svc/v1/digests/DG-2026111-0700", headers=_auth(key)).status_code == 400
    pdf = client.get(f"/svc/v1/digests/{DG}/dokumen?format=pdf", headers=_auth(key))
    assert (pdf.status_code, pdf.json()["error"]["code"]) == (422, "FORMAT_BELUM_DIDUKUNG")
    extra = client.post("/svc/v1/digests/run", json={"jenis": "mingguan"}, headers=_auth(key))
    assert extra.status_code == 400


DEV_DB = SecretStr("postgresql+psycopg://u:p@postgres:5432/scheduler_dev")


def _dev(**overrides) -> Settings:
    """Dibuat di dalam uji, sesudah conftest membersihkan env ASTRIA_* kontainer dev."""
    values = {"env": "dev", "database_url": DEV_DB, "service_key": SecretStr("ab" * 32)}
    return Settings(**values | overrides)


def test_scheduler_loop_is_wired_when_database_configured() -> None:
    app = build_app(_dev())
    assert "penjadwal" in app.state.components
    assert app.state.orkestrator is not None


def test_service_key_is_required_to_read_ticket() -> None:
    with pytest.raises(RuntimeError):
        build_app(_dev(service_key=None))


def test_invalid_cron_is_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(env="test", scheduler_digest_cron="setiap pagi")


def test_refuses_to_start_with_non_dev_database_in_dev() -> None:
    url = SecretStr("postgresql+psycopg://u:p@postgres:5432/astria")
    with pytest.raises(EnvironmentGuardError):
        build_app(_dev(database_url=url))
