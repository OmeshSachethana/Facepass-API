import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from facepass.core.config import Settings
from facepass.core.security import encrypt_secret, generate_api_key, hash_token
from facepass.db.models import ApiKey, Application, Base
from facepass.db.session import build_engine, build_session_factory
from facepass.main import create_app
from tests.fakes import FakeFaceEngine

DATABASE_URL = os.environ.get(
    "FACEPASS_TEST_DATABASE_URL",
    "postgresql+psycopg://facepass:facepass@127.0.0.1:5435/facepass_test",
)
SECRET_KEY = "test-secret-key-that-is-long-enough-for-validation"
WEBHOOK_SECRET = "fp_whs_test"


def _create_database_if_missing(url: str) -> None:
    parsed = make_url(url)
    name = parsed.database or ""
    # The suite drops the schema, so never let it run against anything but a test database.
    if "test" not in name:
        raise RuntimeError(f"Refusing to run tests against database {name!r}")
    admin = create_engine(
        parsed.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 10},
    )
    with admin.connect() as connection:
        exists = connection.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
        )
        if not exists:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()


def alembic_config() -> Config:
    config = Config("alembic.ini")
    config.attributes["database_url"] = DATABASE_URL
    return config


@pytest.fixture(scope="session")
def migrated_database() -> str:
    _create_database_if_missing(DATABASE_URL)
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    command.upgrade(alembic_config(), "head")
    return DATABASE_URL


@pytest.fixture
def settings(migrated_database: str) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url=migrated_database,
        secret_key=SECRET_KEY,
        cors_origins=["https://app.example.com"],
        session_max_attempts=3,
        failed_verify_limit=3,
        max_faces_per_subject=2,
        rate_limit_per_minute=1000,
    )


@pytest.fixture
def session_factory(settings: Settings) -> Iterator[sessionmaker[Session]]:
    engine = build_engine(settings.database_url)
    with engine.begin() as connection:
        tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
        connection.execute(text(f"TRUNCATE {tables} CASCADE"))
    yield build_session_factory(engine)
    engine.dispose()


@pytest.fixture
def db(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


@dataclass
class Tenant:
    application_id: uuid.UUID
    api_key_id: uuid.UUID
    api_key: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}


@pytest.fixture
def make_tenant(
    session_factory: sessionmaker[Session], settings: Settings
) -> Callable[..., Tenant]:
    def factory(webhook_url: str | None = None) -> Tenant:
        application_id, key_id, key = uuid.uuid4(), uuid.uuid4(), generate_api_key()
        with session_factory() as session:
            session.add(
                Application(
                    id=application_id,
                    name="Test app",
                    webhook_url=webhook_url,
                    webhook_secret_encrypted=(
                        encrypt_secret(WEBHOOK_SECRET, settings.secret_key.get_secret_value())
                        if webhook_url
                        else None
                    ),
                )
            )
            session.flush()
            session.add(
                ApiKey(
                    id=key_id, application_id=application_id, name="test", key_hash=hash_token(key)
                )
            )
            session.commit()
        return Tenant(application_id, key_id, key)

    return factory


@pytest.fixture
def tenant(make_tenant: Callable[..., Tenant]) -> Tenant:
    created: Tenant = make_tenant()
    return created


@pytest.fixture
def client(settings: Settings, session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    app = create_app(settings, FakeFaceEngine())
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
