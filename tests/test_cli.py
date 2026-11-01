import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from facepass import cli
from facepass.core.config import get_settings
from facepass.core.security import decrypt_secret
from facepass.db.models import Application
from tests.conftest import SECRET_KEY


@pytest.fixture
def cli_env(
    monkeypatch: pytest.MonkeyPatch,
    migrated_database: str,
    session_factory: sessionmaker[Session],
) -> Iterator[None]:
    monkeypatch.setenv("FACEPASS_DATABASE_URL", migrated_database)
    monkeypatch.setenv("FACEPASS_SECRET_KEY", SECRET_KEY)
    monkeypatch.setenv("FACEPASS_ENVIRONMENT", "development")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *args: str
) -> dict[str, str]:
    monkeypatch.setattr("sys.argv", ["facepass", *args])
    cli.main()
    lines = capsys.readouterr().out.splitlines()
    return {
        key.strip(): value.strip()
        for key, value in (line.split(":", 1) for line in lines if ":" in line)
    }


@pytest.mark.usefixtures("cli_env")
def test_created_key_works_until_revoked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], client: TestClient
) -> None:
    created = _run(monkeypatch, capsys, "apps", "create", "--name", "Shop")
    headers = {"Authorization": f"Bearer {created['api_key']}"}
    assert client.get("/v1/subjects", headers=headers).status_code == 200

    second = _run(monkeypatch, capsys, "keys", "create", created["application_id"])
    second_headers = {"Authorization": f"Bearer {second['api_key']}"}
    assert client.get("/v1/subjects", headers=second_headers).status_code == 200

    _run(monkeypatch, capsys, "keys", "revoke", created["api_key_id"])
    assert client.get("/v1/subjects", headers=headers).status_code == 401
    assert client.get("/v1/subjects", headers=second_headers).status_code == 200


@pytest.mark.usefixtures("cli_env")
def test_webhook_secret_is_encrypted_at_rest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], db: Session
) -> None:
    created = _run(
        monkeypatch,
        capsys,
        "apps",
        "create",
        "--name",
        "Shop",
        "--webhook-url",
        "https://hooks.example.com/x",
    )
    application = db.get_one(Application, uuid.UUID(created["application_id"]))
    stored = application.webhook_secret_encrypted
    assert stored is not None
    assert created["webhook_secret"] not in stored
    assert decrypt_secret(stored, SECRET_KEY) == created["webhook_secret"]


@pytest.mark.usefixtures("cli_env")
def test_production_refuses_plain_http_webhooks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FACEPASS_ENVIRONMENT", "production")
    get_settings.cache_clear()
    with pytest.raises(SystemExit, match="https"):
        _run(
            monkeypatch,
            capsys,
            "apps",
            "create",
            "--name",
            "Shop",
            "--webhook-url",
            "http://hooks.example.com/x",
        )
